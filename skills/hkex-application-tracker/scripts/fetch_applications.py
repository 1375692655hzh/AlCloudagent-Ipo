#!/usr/bin/env python3
"""递表聆讯抓取工具 — HKEX Application Tracker.

抓取港交所「上市申请人」页面背后的静态 JSON API，
识别处于「遞表」「聆訊」阶段的公司，
下载申请版本（Application Proof）与聆讯后资料集（PHIP）PDF。

数据源：
  https://www1.hkexnews.hk/ncms/json/eds/appactive_appphip_sehk_{board}_{lang}.json

仅覆盖 IPO 生命周期中的「遞表」「聆訊」两个阶段。
招股发行、已上市阶段请见姐妹工具 hkex-offering-tracker。
两者共用同一个 data/state.db。

Pipeline:
  1. 并行 GET 2 个 JSON 端点（主板 sehk + 创业板 gem，appphip 版本）。
  2. 解析 JSON 中每条申请人记录的 `ls[]` 子文档列表。
  3. 对每个 ls[] 条目，按 nF 字段推断 listing_stage
     (申請版本 -> 遞表, 聆訊後資料集 -> 聆訊)。
  4. 仅下载 doc_type 在 APPLICATION_SOURCE_WHITELIST 内的 PDF。
  5. 用 applicant_id 作临时 stock_code (APP-{id})，UPSERT 共享 SQLite。
  6. 导出 manifest.json + per-company company.json (via common.export_json)。

Usage:
    python skills/hkex-application-tracker/scripts/fetch_applications.py [opts]

Output lands under <data-dir>/ (default: <repo>/data/), shared with the
sibling hkex-offering-tracker skill.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

# Sibling imports: this script lives in skills/hkex-application-tracker/scripts/
# It pulls shared helpers from its own directory (state.py, common.py shim).
#
# common.py is *not* duplicated; instead we import it from the sibling skill
# via a sys.path entry. This keeps the rule "state.py is duplicated, common.py
# is unique" — state is small and stable; common is larger and evolves.
_HERE = Path(__file__).resolve().parent
_OFFERING_SCRIPTS = _HERE.parent.parent / "hkex-offering-tracker" / "scripts"
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_OFFERING_SCRIPTS))

from common import (  # noqa: E402
    USER_AGENT,
    REQUEST_TIMEOUT,
    open_db,
    export_json,
    download_pdf,
    upsert_company,
    parse_filing_date_iso,
    url_hash,
    build_doc_filename,
    build_company_dir,
    company_json_rel,
)
from state import infer_state, is_application_source_evidence  # noqa: E402


# ---------------------------------------------------------------------------
# Endpoint matrix
# ---------------------------------------------------------------------------

HKEX_JSON_BASE = "https://www1.hkexnews.hk/ncms/json/eds"
HKEX_APP_PDF_BASE = "https://www1.hkexnews.hk/app"

# (board, variant) -> relative JSON path (Chinese version).
# variant: "appphip" = Application Proof + PHIP (递表+聆讯全集)
#          "app"     = Application Proof only (仅递表)
_ENDPOINTS = {
    ("sehk", "appphip"): "appactive_appphip_sehk_c.json",
    ("gem", "appphip"): "appactive_appphip_gem_c.json",
    ("sehk", "app"):    "appactive_app_sehk_c.json",
    ("gem", "app"):    "appactive_app_gem_c.json",
}


# ---------------------------------------------------------------------------
# Data records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppDocRow:
    """One PDF-bearing document from an applicant's `ls[]` list."""

    applicant_id: int       # JSON `id` (e.g. 108261)
    company_name: str       # JSON `a` (e.g. "立訊精密工業股份有限公司")
    filing_date: str        # JSON `d` (top-level, e.g. "23/06/2026")
    board: str              # "sehk" or "gem"
    status_code: str        # JSON `s` (A=Active, LT=Listed, IR=Inactive)
    has_phip: bool          # JSON `hasPhip`
    doc_type: str           # JSON `ls[].nF` (e.g. "聆訊後資料集（第一次呈交）")
    doc_subtype: str        # JSON `ls[].nS1` (e.g. "全文檔案")
    doc_date: str           # JSON `ls[].d` (e.g. "23/06/2026")
    pdf_url: str            # absolute URL
    inferred_stage: str     # 遞表 / 聆訊


# ---------------------------------------------------------------------------
# JSON fetching & parsing
# ---------------------------------------------------------------------------


async def fetch_json(client: httpx.AsyncClient, url: str) -> dict:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.json()


def parse_applicants(payload: dict, board: str) -> list[AppDocRow]:
    """Flatten one JSON payload into a list of PDF-bearing doc rows.

    Skips ls[] entries that:
      - have no `u1` (no PDF link, e.g. multi-file HTML index only)
      - have an `nF` not in APPLICATION_SOURCE_WHITELIST (OC announcements,
        warning statements, etc.)
    """
    rows: list[AppDocRow] = []
    for app in payload.get("app", []):
        applicant_id = app.get("id")
        company_name = app.get("a", "").strip()
        filing_date = app.get("d", "")
        status_code = app.get("s", "")
        has_phip = bool(app.get("hasPhip", False))

        if applicant_id is None or not company_name:
            continue

        for ls in app.get("ls", []):
            u1 = ls.get("u1")
            if not u1:
                continue
            doc_type = (ls.get("nF") or "").strip()
            if not is_application_source_evidence(doc_type):
                continue

            stage = infer_state(doc_type)
            if stage is None:
                continue

            pdf_url = f"{HKEX_APP_PDF_BASE}/{u1.lstrip('/')}"
            rows.append(AppDocRow(
                applicant_id=int(applicant_id),
                company_name=company_name,
                filing_date=filing_date,
                board=board,
                status_code=status_code,
                has_phip=has_phip,
                doc_type=doc_type,
                doc_subtype=(ls.get("nS1") or "").strip(),
                doc_date=ls.get("d", filing_date),
                pdf_url=pdf_url,
                inferred_stage=stage,
            ))
    return rows


def dedupe_rows(rows: list[AppDocRow]) -> list[AppDocRow]:
    """Dedupe by pdf_url (the same applicant may appear in both `app` and
    `appphip` endpoints, or twice within a payload)."""
    seen: set[str] = set()
    out: list[AppDocRow] = []
    for r in rows:
        if r.pdf_url in seen:
            continue
        seen.add(r.pdf_url)
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Stock-code resolution
# ---------------------------------------------------------------------------


def resolve_stock_code(conn, applicant_id: int, company_name: str) -> str:
    """Decide which stock_code key to use for this applicant.

    Strategy:
      1. If we previously resolved this applicant_id to a real stock_code
         (recorded in the `applicant_id_map` lookup table), reuse it.
      2. Else fall back to a synthetic `APP-{id}` key.

    Step 1 lets us merge records once the company gets a real stock_code in
    the offering-tracker or listed endpoint — but for v1 we only do the
    fallback. The applicant_id_map table is created here as forward scaffolding.
    """
    cur = conn.execute(
        "SELECT stock_code FROM applicant_id_map WHERE applicant_id = ?",
        (applicant_id,),
    ).fetchone()
    if cur is not None:
        return cur[0]
    return f"APP-{applicant_id}"


# Schema extension for applicant_id <-> stock_code mapping (forward-looking).
_APPLICANT_MAP_DDL = """
CREATE TABLE IF NOT EXISTS applicant_id_map (
    applicant_id INTEGER PRIMARY KEY,
    stock_code   TEXT NOT NULL,
    company_name TEXT,
    first_seen   TEXT NOT NULL,
    resolved_at  TEXT
);
"""


# ---------------------------------------------------------------------------
# Row handling
# ---------------------------------------------------------------------------


async def _handle_row(
    row: AppDocRow,
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

    stock_code = resolve_stock_code(conn, row.applicant_id, row.company_name)
    stage = row.inferred_stage

    # Record applicant_id mapping (idempotent).
    conn.execute(
        """INSERT OR IGNORE INTO applicant_id_map
           (applicant_id, stock_code, company_name, first_seen)
           VALUES (?, ?, ?, ?)""",
        (row.applicant_id, stock_code, row.company_name, now_iso),
    )

    company_dir = build_company_dir(stock_code, row.company_name)
    docs_dir = companies_root / company_dir / "docs"
    filename = build_doc_filename(row.doc_type, stage, row.doc_date)
    dest = docs_dir / filename

    # If two PDFs land on the same filename (same doc_date, same stage),
    # append a numeric suffix to avoid clobbering.
    if dest.exists():
        seq = 1
        stem = dest.stem
        suffix = dest.suffix
        while dest.with_name(f"{stem}_{seq}{suffix}").exists():
            seq += 1
        dest = dest.with_name(f"{stem}_{seq}{suffix}")

    try:
        sha, size = await download_pdf(client, row.pdf_url, dest)
    except Exception as exc:
        print(f"FAIL {stock_code} {row.company_name}: {exc}", file=sys.stderr)
        stats["failed"] += 1
        return

    try:
        rel_path = dest.relative_to(repo_root).as_posix()
    except ValueError:
        rel_path = str(dest)
    release_iso = parse_filing_date_iso(row.doc_date)

    upsert_company(conn, stock_code, row.company_name, stage, now_iso,
                   company_json_rel(companies_root, company_dir, repo_root))

    conn.execute(
        """INSERT OR REPLACE INTO ipo_documents
           (url_hash, stock_code, company_name, doc_type, inferred_state,
            release_time, release_time_iso, pdf_url, local_path,
            content_sha256, fetched_at, file_size)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uh, stock_code, row.company_name, row.doc_type, stage,
         row.doc_date, release_iso, row.pdf_url, rel_path,
         sha, now_iso, size),
    )
    stats["new"] += 1
    print(f"OK   {stock_code} {row.company_name} | {row.doc_type} -> {rel_path}")


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
             "Default: <repo>/data (shared with hkex-offering-tracker).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse the JSON and print what would be downloaded, but do not "
             "download or write to the DB.",
    )
    parser.add_argument(
        "--no-gem",
        action="store_true",
        help="Skip the GEM (创业板) endpoint; only fetch 主板 (sehk).",
    )
    parser.add_argument(
        "--app-only",
        action="store_true",
        help="Fetch the appactive_app_* endpoint (Application Proof only, "
             "no PHIP) instead of the default appactive_appphip_* "
             "(Application Proof + PHIP).",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[3]
    data_root = (args.data_dir or repo_root / "data").resolve()
    companies_root = data_root / "companies"
    db_path = data_root / "state.db"

    print(f"Repo root : {repo_root}")
    print(f"Data root : {data_root}")

    variant = "app" if args.app_only else "appphip"
    boards = ["sehk"] if args.no_gem else ["sehk", "gem"]

    endpoints: list[tuple[str, str]] = []
    for board in boards:
        rel = _ENDPOINTS.get((board, variant))
        if rel:
            endpoints.append((board, f"{HKEX_JSON_BASE}/{rel}"))

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,*/*",
        "Referer": "https://www1.hkexnews.hk/app/appindex.html",
    }
    all_rows: list[AppDocRow] = []
    async with httpx.AsyncClient(
        headers=headers, timeout=REQUEST_TIMEOUT, follow_redirects=True
    ) as client:
        for board, url in endpoints:
            print(f"Fetching {url}")
            payload = await fetch_json(client, url)
            rows = parse_applicants(payload, board)
            print(f"  parsed {len(rows)} evidence rows from {len(payload.get('app', []))} applicants")
            all_rows.extend(rows)

    all_rows = dedupe_rows(all_rows)
    print(f"After dedupe: {len(all_rows)} unique PDFs")

    by_stage: dict[str, int] = {"遞表": 0, "聆訊": 0}
    for r in all_rows:
        by_stage[r.inferred_stage] = by_stage.get(r.inferred_stage, 0) + 1
    print(f"  stage breakdown: {by_stage}")

    if args.dry_run:
        print("\n--dry-run: would process:")
        for r in all_rows:
            print(f"  APP-{r.applicant_id} {r.company_name} | {r.doc_type} "
                  f"| {r.doc_date} | {r.pdf_url}")
        return 0

    data_root.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    # Forward-looking: ensure the applicant_id_map table exists.
    conn.executescript(_APPLICANT_MAP_DDL)
    conn.commit()

    try:
        from common import process_rows  # late import to avoid pulling asyncio at module load
        stats = await process_rows(all_rows, conn, companies_root,
                                   repo_root, _handle_row)
        export_json(conn, data_root, repo_root,
                    source_label=f"HKEX appindex ({variant}, boards={boards})")
    finally:
        conn.close()

    print(
        f"\nSummary: {stats['new']} new, {stats['skipped']} skipped, "
        f"{stats['failed']} failed"
    )
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
