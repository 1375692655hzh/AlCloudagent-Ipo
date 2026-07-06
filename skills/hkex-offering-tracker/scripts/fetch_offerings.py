#!/usr/bin/env python3
"""招股发行抓取工具 — HKEX Offering Tracker.

抓取港交所「招股文件」页面，识别处于「招股」状态的公司，
下载全球发售招股书 PDF，并写入共享 SQLite 状态库 + JSON 三件套。

数据源：
  https://www1.hkexnews.hk/search/predefineddoc.xhtml?lang=zh&predefineddocuments=6

仅覆盖 IPO 生命周期中的「招股」（与预留的「已上市」）阶段。
递表、聆讯阶段请见姐妹工具 hkex-application-tracker。

Pipeline:
  1. GET the predefined-documents page (HTML, JSF-rendered static).
  2. Parse the table with BeautifulSoup, climbing parents to find row labels.
  3. Infer the lifecycle stage via state.infer_state(); only rows whose
     doc_type is in CURRENT_SOURCE_WHITELIST are downloaded.
  4. Stream-download each new PDF (concurrency-limited), hashing content.
  5. UPSERT into shared SQLite (companies / ipo_documents / state_history).
  6. Export manifest.json + per-company company.json via common.export_json.

Usage:
    python skills/hkex-offering-tracker/scripts/fetch_offerings.py [--data-dir DIR]

Output lands under <data-dir>/ (default: <repo>/data/), shared with the
sibling hkex-application-tracker skill.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import httpx
from bs4 import BeautifulSoup

# Make sibling modules importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    HKEX_BASE,
    USER_AGENT,
    REQUEST_TIMEOUT,
    DOWNLOAD_TIMEOUT,
    SCHEMA,
    open_db,
    export_json,
    process_rows,
    upsert_company,
    download_pdf,
    parse_release_time_iso,
    sanitize_filename,
    url_hash,
    build_doc_filename,
    build_company_dir,
    company_json_rel,
)
from state import infer_state, is_current_source_ipo  # noqa: E402

HKEX_LISTING_URL = (
    "https://www1.hkexnews.hk/search/predefineddoc.xhtml"
    "?lang=zh&predefineddocuments=6"
)


# ---------------------------------------------------------------------------
# Data records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HkexRow:
    """One parsed row of the predefined documents table."""

    stock_code: str
    company_name: str
    release_time_raw: str  # "30/06/2026 06:57"
    doc_type: str  # "全球發售", "重組方案", ...
    pdf_url: str  # absolute URL
    file_size_label: str  # "(11MB)" — informational only


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------


def parse_listing_html(html: str) -> list[HkexRow]:
    """Parse the predefined-documents page into structured rows.

    The page renders rows where each row has labeled spans:
      "發放時間: 30/06/2026 06:57"
      "股份代號: 06951"
      "股份簡稱: 三環集團"
      "文件: 全球發售 (11MB)"  with an <a href> to the PDF
    """
    soup = BeautifulSoup(html, "lxml")
    rows: list[HkexRow] = []

    # Each result row typically lives in a <tr> or a moving div; HKEX uses
    # a JSF-rendered table. We scan for anchors that link to PDFs in
    # /listedco/listconews/ and reconstruct the row from surrounding text.
    anchors = soup.find_all("a", href=re.compile(r"/listedco/listconews/.*\.pdf"))
    for a in anchors:
        href = a["href"]
        pdf_url = HKEX_BASE + href if href.startswith("/") else href

        # Climb up to find the row container holding all labels.
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

        # doc_type is the anchor's own text (e.g. "全球發售"), possibly with
        # trailing "(11MB)" size label appended.
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

    # De-duplicate by pdf_url (page can repeat anchors).
    seen: set[str] = set()
    unique: list[HkexRow] = []
    for r in rows:
        if r.pdf_url in seen:
            continue
        seen.add(r.pdf_url)
        unique.append(r)
    return unique


def _extract_field(row_text: str, label: str) -> str | None:
    """Extract the value following a label like '股份代號:'.

    For most fields the value is a single token (stock code, company name).
    For '發放時間' the value contains a space-separated time, so we allow the
    pattern to extend through a time token as well.
    """
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
# Row handling (filled in for common.process_rows)
# ---------------------------------------------------------------------------


def filter_offering_rows(rows: Iterable[HkexRow]) -> list[HkexRow]:
    """Keep only rows that represent a current-source global offering."""
    return [r for r in rows if is_current_source_ipo(r.doc_type)]


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

    stage = infer_state(row.doc_type) or "招股"
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
    print(f"OK   {row.stock_code} {row.company_name} -> {rel_path}")


# ---------------------------------------------------------------------------
# Page fetch
# ---------------------------------------------------------------------------


async def fetch_page(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url)
    resp.raise_for_status()
    # HKEX pages are UTF-8; force-decode to avoid mojibake of traditional chars.
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
             "Default: <repo>/data (shared with hkex-application-tracker).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse the page and print what would be downloaded, but do not "
             "download or write to the DB.",
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

    offering_rows = filter_offering_rows(rows)
    excluded = len(rows) - len(offering_rows)
    print(f"Filtered to {len(offering_rows)} global-offering rows "
          f"({excluded} excluded by state rules)")

    if args.dry_run:
        print("\n--dry-run: would process:")
        for r in offering_rows:
            print(f"  {r.stock_code} {r.company_name} | {r.doc_type} | "
                  f"{r.release_time_raw} | {r.pdf_url}")
        return 0

    data_root.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        stats = await process_rows(offering_rows, conn, companies_root,
                                   repo_root, _handle_row)
        export_json(conn, data_root, repo_root,
                    source_label="HKEX predefineddocuments=6")
    finally:
        conn.close()

    print(
        f"\nSummary: {stats['new']} new, {stats['skipped']} skipped, "
        f"{stats['failed']} failed"
    )
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
