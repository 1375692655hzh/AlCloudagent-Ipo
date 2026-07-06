#!/usr/bin/env python3
"""配发结果抓取工具 — HKEX Listing Tracker.

抓取港交所「新上市股份配發結果」页面（predefineddocuments=4），
识别处于「已上市」状态的公司，下载配发结果公告 PDF。

数据源：
  https://www1.hkexnews.hk/search/predefineddoc.xhtml?lang=zh&predefineddocuments=4

仅覆盖 IPO 生命周期中的「已上市」阶段（招股后的最终一步）。
递表、聆讯、招股阶段请见姐妹工具：
  - hkex-application-tracker（递表 + 聆讯）
  - hkex-offering-tracker（招股）
三者共用同一个 data/state.db。

【双层过滤】（防止污染数据库）：
  1. doc_type 必须在 LISTING_SOURCE_WHITELIST 内（排除供股/配售）
  2. stock_code 必须已在 companies 表中（排除未跟踪的老公司）

也就是说，本工具**只推进我们跟踪过的公司**从招股 → 已上市。
若一家公司没经过递表/聆讯/招股阶段，其配发结果不会被抓取。

Pipeline:
  1. GET the predefineddocuments=4 page (HTML, JSF-rendered static).
  2. Parse the table the same way as offering-tracker.
  3. For each row: apply is_listing_source_evidence() (negative filter on
     供股/配售) + check stock_code is already in companies table.
  4. Stream-download each new PDF (concurrency-limited), hashing content.
  5. UPSERT into shared SQLite; export_json regenerates manifest + views.

Usage:
    python skills/hkex-listing-tracker/scripts/fetch_listings.py [opts]

Options:
    --data-dir DIR     Override data directory (default <repo>/data)
    --dry-run          Parse + print, no download
    --include-unknown  Disable the tracked-company filter (debug only).
                       Lets through allotment results for companies not
                       yet in our DB. Useful for one-off backfill.
    --window 7d        Reserved for future (longer history). Currently
                       always uses HKEX default 7-day view.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx
from bs4 import BeautifulSoup

# Sibling imports: this script lives in skills/hkex-listing-tracker/scripts/
# state.py is local; common.py is imported from the offering-tracker sibling
# (canonical home for shared helpers).
_HERE = Path(__file__).resolve().parent
_OFFERING_SCRIPTS = _HERE.parent.parent / "hkex-offering-tracker" / "scripts"
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_OFFERING_SCRIPTS))

from common import (  # noqa: E402
    HKEX_BASE,
    USER_AGENT,
    REQUEST_TIMEOUT,
    open_db,
    export_json,
    process_rows,
    upsert_company,
    download_pdf,
    parse_release_time_iso,
    url_hash,
    build_doc_filename,
    build_company_dir,
    company_json_rel,
)
from state import infer_state, is_listing_source_evidence  # noqa: E402

HKEX_LISTING_URL = (
    "https://www1.hkexnews.hk/search/predefineddoc.xhtml"
    "?lang=zh&predefineddocuments=4"
)


# ---------------------------------------------------------------------------
# Data records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HkexRow:
    """One parsed row of the predefined documents table (same shape as
    offering-tracker's HkexRow)."""

    stock_code: str
    company_name: str
    release_time_raw: str  # "30/06/2026 06:57"
    doc_type: str  # "配發結果公告", "供股結果", ...
    pdf_url: str  # absolute URL
    file_size_label: str  # "(357KB)" — informational only


# ---------------------------------------------------------------------------
# HTML parsing (identical to offering-tracker)
# ---------------------------------------------------------------------------


def parse_listing_html(html: str) -> list[HkexRow]:
    """Parse the predefineddocuments=4 page into structured rows.

    Same structure as predefineddocuments=6 (JSF-rendered table with
    發放時間 / 股份代號 / 股份簡稱 / 文件 anchor).
    """
    soup = BeautifulSoup(html, "lxml")
    rows: list[HkexRow] = []

    anchors = soup.find_all("a", href=re.compile(r"/listedco/listconews/.*\.pdf"))
    for a in anchors:
        href = a["href"]
        pdf_url = HKEX_BASE + href if href.startswith("/") else href

        container = a
        row_text = ""
        for _ in range(6):
            container = container.parent
            if container is None:
                break
            row_text = container.get_text(" ", strip=True)
            if "股份代號" in row_text and "發放時間" in row_text:
                break

        if "股份代號" not in row_text:
            continue

        stock_code = _extract_field(row_text, "股份代號")
        company_name = _extract_field(row_text, "股份簡稱")
        release_time = _extract_field(row_text, "發放時間")
        if not (stock_code and company_name and release_time):
            continue

        anchor_text = a.get_text(" ", strip=True)
        size_label = ""
        doc_type = anchor_text
        size_match = re.search(r"\((\d+(?:\.\d+)?[KMG]B)\)", anchor_text)
        if size_match:
            size_label = f"({size_match.group(1)})"
            doc_type = anchor_text[: size_match.start()].strip()

        rows.append(HkexRow(
            stock_code=stock_code.strip(),
            company_name=company_name.strip(),
            release_time_raw=release_time.strip(),
            doc_type=doc_type.strip(),
            pdf_url=pdf_url,
            file_size_label=size_label,
        ))

    # De-duplicate by pdf_url.
    seen: set[str] = set()
    unique: list[HkexRow] = []
    for r in rows:
        if r.pdf_url in seen:
            continue
        seen.add(r.pdf_url)
        unique.append(r)
    return unique


def _extract_field(row_text: str, label: str) -> str | None:
    """Extract the value following a label like '股份代號:'."""
    if label == "發放時間":
        m = re.search(
            rf"{re.escape(label)}\s*[:：]\s*"
            r"(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2})",
            row_text,
        )
        if m:
            return m.group(1).strip()
    m = re.search(rf"{re.escape(label)}\s*[:：]\s*([^\s,;]+)", row_text)
    if not m:
        return None
    return m.group(1).strip()


# ---------------------------------------------------------------------------
# Two-layer filter
# ---------------------------------------------------------------------------


def _is_tracked_company(conn, stock_code: str) -> bool:
    """True if stock_code already exists in companies table."""
    cur = conn.execute(
        "SELECT 1 FROM companies WHERE stock_code = ?",
        (stock_code,),
    ).fetchone()
    return cur is not None


def filter_listing_rows(
    rows: Iterable[HkexRow],
    conn,
    include_unknown: bool = False,
) -> tuple[list[HkexRow], dict[str, int]]:
    """Apply two-layer filter, return (kept_rows, stats).

    Layer 1: doc_type must be IPO allotment evidence (whitelist + negative
             filter on 供股/配售).
    Layer 2 (unless include_unknown=True): stock_code must already be in
             companies table (i.e. previously tracked through遞表/聆訊/招股).

    stats keys: total, dropped_rights_issue, dropped_untracked, kept
    """
    stats = {"total": 0, "dropped_rights_issue": 0,
             "dropped_untracked": 0, "kept": 0}
    kept: list[HkexRow] = []
    for r in rows:
        stats["total"] += 1
        # Layer 1: whitelist + negative patterns
        if not is_listing_source_evidence(r.doc_type):
            stats["dropped_rights_issue"] += 1
            continue
        # Layer 2: tracked-company check
        if not include_unknown and not _is_tracked_company(conn, r.stock_code):
            stats["dropped_untracked"] += 1
            continue
        stats["kept"] += 1
        kept.append(r)
    return kept, stats


# ---------------------------------------------------------------------------
# Row handling
# ---------------------------------------------------------------------------


async def _handle_row(
    row: HkexRow,
    conn,
    client: httpx.AsyncClient,
    companies_root: Path,
    repo_root: Path,
    now_iso: str,
    stats: dict[str, int],
) -> None:
    uh = url_hash(row.pdf_url)
    cur = conn.execute(
        "SELECT content_sha256 FROM ipo_documents WHERE url_hash = ?",
        (uh,),
    ).fetchone()
    if cur is not None:
        stats["skipped"] += 1
        return

    stage = "已上市"
    company_dir = build_company_dir(row.stock_code, row.company_name)
    docs_dir = companies_root / company_dir / "docs"
    filename = build_doc_filename(row.doc_type, stage, row.release_time_raw)
    dest = docs_dir / filename

    try:
        sha, size = await download_pdf(client, row.pdf_url, dest)
    except Exception as exc:
        print(f"FAIL {row.stock_code} {row.company_name}: {exc}", file=sys.stderr)
        stats["failed"] += 1
        return

    try:
        rel_path = dest.relative_to(repo_root).as_posix()
    except ValueError:
        rel_path = str(dest)
    release_iso = parse_release_time_iso(row.release_time_raw)

    upsert_company(conn, row.stock_code, row.company_name, stage, now_iso,
                   company_json_rel(companies_root, company_dir, repo_root))

    conn.execute(
        """INSERT OR REPLACE INTO ipo_documents
           (url_hash, stock_code, company_name, doc_type, inferred_state,
            release_time, release_time_iso, pdf_url, local_path,
            content_sha256, fetched_at, file_size)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uh, row.stock_code, row.company_name, row.doc_type, stage,
         row.release_time_raw, release_iso, row.pdf_url, rel_path,
         sha, now_iso, size),
    )
    stats["new"] += 1
    print(f"OK   {row.stock_code} {row.company_name} | {row.doc_type} -> {rel_path}")


# ---------------------------------------------------------------------------
# Page fetch
# ---------------------------------------------------------------------------


async def fetch_page(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url)
    resp.raise_for_status()
    if resp.encoding is None or resp.encoding.lower() not in {"utf-8", "utf8"}:
        resp.encoding = "utf-8"
    return resp.text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Where to store state.db, manifest.json, companies/. "
             "Default: <repo>/data (shared with sibling trackers).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse the page and print what would be downloaded, but do not "
             "download or write to the DB.",
    )
    parser.add_argument(
        "--include-unknown",
        action="store_true",
        help="Disable the tracked-company filter (debug). Lets through "
             "allotment results for companies not yet in our DB.",
    )
    parser.add_argument(
        "--window",
        choices=["7d"],
        default="7d",
        help="Time window (reserved for future expansion). Currently only "
             "supports HKEX default 7-day view.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[3]
    data_root = (args.data_dir or repo_root / "data").resolve()
    companies_root = data_root / "companies"
    db_path = data_root / "state.db"

    print(f"Repo root : {repo_root}")
    print(f"Data root : {data_root}")

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}
    async with httpx.AsyncClient(
        headers=headers, timeout=REQUEST_TIMEOUT, follow_redirects=True
    ) as client:
        print(f"Fetching {HKEX_LISTING_URL}")
        html = await fetch_page(client, HKEX_LISTING_URL)

    rows = parse_listing_html(html)
    print(f"Parsed {len(rows)} rows from page")

    if args.dry_run:
        # For dry-run, we still want to filter — but the tracked-company
        # check needs the DB. Open it read-only-style (we won't write).
        if args.include_unknown:
            print("\n--include-unknown: skipping tracked-company filter")
            filtered = [r for r in rows if is_listing_source_evidence(r.doc_type)]
            stats = {
                "total": len(rows),
                "dropped_rights_issue": len(rows) - len(filtered),
                "dropped_untracked": 0,
                "kept": len(filtered),
            }
        else:
            if not db_path.exists():
                print(f"\nNOTE: {db_path} does not exist; running as --include-unknown "
                      "since there is no DB to check against.")
                filtered = [r for r in rows if is_listing_source_evidence(r.doc_type)]
                stats = {
                    "total": len(rows),
                    "dropped_rights_issue": len(rows) - len(filtered),
                    "dropped_untracked": 0,
                    "kept": len(filtered),
                }
            else:
                conn = open_db(db_path)
                filtered, stats = filter_listing_rows(rows, conn, include_unknown=False)
                conn.close()

        print(f"\nFilter stats: {stats}")
        print("\n--dry-run: would process:")
        for r in filtered:
            print(f"  {r.stock_code} {r.company_name} | {r.doc_type} | "
                  f"{r.release_time_raw} | {r.pdf_url}")
        return 0

    data_root.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        filtered, stats = filter_listing_rows(
            rows, conn, include_unknown=args.include_unknown
        )
        print(f"Filter stats: {stats}")

        if not filtered:
            print("No rows passed the filter; nothing to download.")
            # Still refresh the manifest in case other trackers updated the DB.
            export_json(conn, data_root, repo_root,
                        source_label="HKEX predefineddocuments=4")
            return 0

        proc_stats = await process_rows(filtered, conn, companies_root,
                                        repo_root, _handle_row)
        export_json(conn, data_root, repo_root,
                    source_label="HKEX predefineddocuments=4")
    finally:
        conn.close()

    print(
        f"\nSummary: {proc_stats['new']} new, {proc_stats['skipped']} skipped, "
        f"{proc_stats['failed']} failed"
    )
    return 0 if proc_stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
