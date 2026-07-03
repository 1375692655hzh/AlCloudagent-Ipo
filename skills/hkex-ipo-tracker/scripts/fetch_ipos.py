#!/usr/bin/env python3
"""Fetch HKEX global-offering IPO prospectuses.

Pipeline:
  1. GET https://www1.hkexnews.hk/search/predefineddoc.xhtml?lang=zh&predefineddocuments=6
  2. Parse the results table with BeautifulSoup.
  3. For each row, infer the IPO state via state.infer_state().
     Only rows whose doc_type is in CURRENT_SOURCE_WHITELIST are downloaded.
  4. Stream-download each new PDF (concurrency-limited), hashing content.
  5. UPSERT into SQLite (companies / ipo_documents / state_history).
  6. Export manifest.json + per-company company.json.

Usage:
    python skills/hkex-ipo-tracker/scripts/fetch_ipos.py [--data-dir DIR]

Output lands under <data-dir>/ (default: <repo>/data/).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import httpx
from bs4 import BeautifulSoup
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

# Make state.py importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from state import infer_state, is_current_source_ipo  # noqa: E402

HKEX_LISTING_URL = (
    "https://www1.hkexnews.hk/search/predefineddoc.xhtml"
    "?lang=zh&predefineddocuments=6"
)
HKEX_BASE = "https://www1.hkexnews.hk"
USER_AGENT = (
    "Mozilla/5.0 (compatible; hkex-ipo-tracker/1.0; +https://github.com/)"
)
CONCURRENCY = 4
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=15.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(180.0, connect=15.0)

# Windows / POSIX illegal filename characters.
ILLEGAL_FN_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')

# HKEX release time format: "30/06/2026 06:57"
RELEASE_TIME_RE = re.compile(
    r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})"
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


def parse_release_time_iso(raw: str) -> str | None:
    """Convert "30/06/2026 06:57" -> ISO8601 with +08:00 (HK time)."""
    m = RELEASE_TIME_RE.search(raw or "")
    if not m:
        return None
    dd, mm, yyyy, hh, mi = m.groups()
    try:
        dt = datetime(int(yyyy), int(mm), int(dd), int(hh), int(mi),
                      tzinfo=timezone.utc)
    except ValueError:
        return None
    # HKEX times are Hong Kong local (+08:00); normalize to ISO with offset.
    from datetime import timedelta
    hk_tz = timezone(timedelta(hours=8))
    dt = dt.replace(tzinfo=hk_tz)
    return dt.isoformat()


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
# Filename helpers
# ---------------------------------------------------------------------------


def sanitize_filename(name: str) -> str:
    """Replace filesystem-illegal characters with underscore."""
    return ILLEGAL_FN_CHARS.sub("_", name).strip().rstrip(".")


def url_hash(pdf_url: str) -> str:
    return hashlib.sha256(pdf_url.encode("utf-8")).hexdigest()[:32]


def build_doc_filename(doc_type: str, state: str, release_time_raw: str) -> str:
    """Build '<doc_type>_<state>_<YYYYMMDD_HHMMSS>.pdf'."""
    iso = parse_release_time_iso(release_time_raw)
    if iso:
        ts = iso.replace("-", "").replace(":", "")[:15]  # YYYYMMDDTHHMMSS
        ts = ts.replace("T", "_")
    else:
        ts = "unknown"
    return f"{sanitize_filename(doc_type)}_{state}_{ts}.pdf"


def build_company_dir(stock_code: str, company_name: str) -> str:
    return f"{sanitize_filename(stock_code)}_{sanitize_filename(company_name)}"


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    stock_code        TEXT PRIMARY KEY,
    company_name      TEXT NOT NULL,
    current_state     TEXT NOT NULL,
    first_seen        TEXT NOT NULL,
    last_updated      TEXT NOT NULL,
    company_json_path TEXT
);

CREATE TABLE IF NOT EXISTS ipo_documents (
    url_hash        TEXT PRIMARY KEY,
    stock_code      TEXT NOT NULL,
    company_name    TEXT NOT NULL,
    doc_type        TEXT NOT NULL,
    inferred_state  TEXT NOT NULL,
    release_time    TEXT,
    release_time_iso TEXT,
    pdf_url         TEXT NOT NULL UNIQUE,
    local_path      TEXT,
    content_sha256  TEXT,
    fetched_at      TEXT NOT NULL,
    file_size       INTEGER,
    FOREIGN KEY (stock_code) REFERENCES companies(stock_code)
);

CREATE TABLE IF NOT EXISTS state_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code  TEXT NOT NULL,
    old_state   TEXT,
    new_state   TEXT NOT NULL,
    changed_at  TEXT NOT NULL,
    evidence    TEXT
);

CREATE INDEX IF NOT EXISTS idx_doc_stock ON ipo_documents(stock_code);
CREATE INDEX IF NOT EXISTS idx_doc_state ON ipo_documents(inferred_state);
CREATE INDEX IF NOT EXISTS idx_hist_stock ON state_history(stock_code);
"""


def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


class DownloadError(Exception):
    pass


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type((httpx.HTTPError, DownloadError)),
    reraise=True,
)
async def download_pdf(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
) -> tuple[str, int]:
    """Stream-download a PDF to dest, returning (sha256_hex, size_bytes)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    size = 0
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise DownloadError(f"HTTP {resp.status_code} for {url}")
            with tmp.open("wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    f.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return h.hexdigest(), size


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


async def fetch_page(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url)
    resp.raise_for_status()
    # HKEX pages are UTF-8; force-decode to avoid mojibake of traditional chars.
    if resp.encoding is None or resp.encoding.lower() not in {"utf-8", "utf8"}:
        resp.encoding = "utf-8"
    return resp.text


def filter_ipo_rows(rows: Iterable[HkexRow]) -> list[HkexRow]:
    """Keep only rows that represent a current-source global offering."""
    return [r for r in rows if is_current_source_ipo(r.doc_type)]


async def process_rows(
    rows: list[HkexRow],
    conn: sqlite3.Connection,
    companies_root: Path,
    repo_root: Path,
) -> dict[str, int]:
    """Download each new row, upsert DB. Returns stats dict."""
    stats = {"new": 0, "skipped": 0, "failed": 0}
    now_iso = datetime.now(timezone.utc).astimezone().isoformat()

    sem = asyncio.Semaphore(CONCURRENCY)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"}

    async with httpx.AsyncClient(
        headers=headers, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True
    ) as client:
        async def handle(row: HkexRow) -> None:
            async with sem:
                await _handle_row(row, conn, client, companies_root,
                                  repo_root, now_iso, stats)

        await asyncio.gather(*[handle(r) for r in rows])
    conn.commit()
    return stats


async def _handle_row(
    row: HkexRow,
    conn: sqlite3.Connection,
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

    state = infer_state(row.doc_type) or "招股"
    company_dir = build_company_dir(row.stock_code, row.company_name)
    docs_dir = companies_root / company_dir / "docs"
    filename = build_doc_filename(row.doc_type, state, row.release_time_raw)
    dest = docs_dir / filename

    try:
        sha, size = await download_pdf(client, row.pdf_url, dest)
    except Exception as exc:
        print(f"FAIL {row.stock_code} {row.company_name}: {exc}", file=sys.stderr)
        stats["failed"] += 1
        return

    rel_path = dest.relative_to(repo_root).as_posix()
    release_iso = parse_release_time_iso(row.release_time_raw)

    # UPSERT company + append state history if changed.
    _upsert_company(conn, row.stock_code, row.company_name, state, now_iso,
                    company_json_rel(companies_root, company_dir, repo_root))

    conn.execute(
        """INSERT OR REPLACE INTO ipo_documents
           (url_hash, stock_code, company_name, doc_type, inferred_state,
            release_time, release_time_iso, pdf_url, local_path,
            content_sha256, fetched_at, file_size)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uh, row.stock_code, row.company_name, row.doc_type, state,
         row.release_time_raw, release_iso, row.pdf_url, rel_path,
         sha, now_iso, size),
    )
    stats["new"] += 1
    print(f"OK   {row.stock_code} {row.company_name} -> {rel_path}")


def company_json_rel(companies_root: Path, company_dir: str, repo_root: Path) -> str:
    return (companies_root / company_dir / "company.json") \
        .relative_to(repo_root).as_posix()


def _upsert_company(
    conn: sqlite3.Connection,
    stock_code: str,
    company_name: str,
    new_state: str,
    now_iso: str,
    company_json_path: str,
) -> None:
    existing = conn.execute(
        "SELECT current_state FROM companies WHERE stock_code = ?",
        (stock_code,),
    ).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO companies
               (stock_code, company_name, current_state,
                first_seen, last_updated, company_json_path)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (stock_code, company_name, new_state, now_iso, now_iso,
             company_json_path),
        )
        conn.execute(
            """INSERT INTO state_history
               (stock_code, old_state, new_state, changed_at, evidence)
               VALUES (?, NULL, ?, ?, 'initial observation')""",
            (stock_code, new_state, now_iso),
        )
    else:
        old_state = existing[0]
        conn.execute(
            """UPDATE companies SET company_name = ?, current_state = ?,
               last_updated = ?, company_json_path = ? WHERE stock_code = ?""",
            (company_name, new_state, now_iso, company_json_path, stock_code),
        )
        if old_state != new_state:
            conn.execute(
                """INSERT INTO state_history
                   (stock_code, old_state, new_state, changed_at, evidence)
                   VALUES (?, ?, ?, ?, ?)""",
                (stock_code, old_state, new_state, now_iso,
                 f"state changed {old_state} -> {new_state}"),
            )


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------


def export_json(conn: sqlite3.Connection, data_root: Path, repo_root: Path) -> None:
    """Rebuild manifest.json + all company.json from SQLite (idempotent)."""
    companies_root = data_root / "companies"
    companies_root.mkdir(parents=True, exist_ok=True)

    now_iso = datetime.now(timezone.utc).astimezone().isoformat()
    all_companies = conn.execute(
        "SELECT stock_code, company_name, current_state, last_updated "
        "FROM companies ORDER BY stock_code"
    ).fetchall()

    by_state: dict[str, int] = {s: 0 for s in ("遞表", "聆訊", "招股", "已上市")}
    manifest_entries = []

    for code, name, state, last_updated in all_companies:
        by_state[state] = by_state.get(state, 0) + 1
        company_dir = build_company_dir(code, name)
        company_json_abs = companies_root / company_dir / "company.json"

        docs_rows = conn.execute(
            "SELECT doc_type, inferred_state, release_time, release_time_iso, "
            "       pdf_url, local_path, content_sha256, fetched_at, file_size "
            "FROM ipo_documents WHERE stock_code = ? "
            "ORDER BY release_time_iso DESC",
            (code,),
        ).fetchall()

        history_rows = conn.execute(
            "SELECT old_state, new_state, changed_at, evidence "
            "FROM state_history WHERE stock_code = ? ORDER BY changed_at",
            (code,),
        ).fetchall()

        documents = [
            {
                "doc_type": d[0],
                "state": d[1],
                "release_time": d[2],
                "release_time_iso": d[3],
                "pdf_url": d[4],
                "local_path": d[5],
                "content_sha256": d[6],
                "fetched_at": d[7],
                "file_size_bytes": d[8],
            }
            for d in docs_rows
        ]

        state_history = [
            {
                "old_state": h[0],
                "new_state": h[1],
                "changed_at": h[2],
                "evidence": h[3],
            }
            for h in history_rows
        ]

        company_json = {
            "stock_code": code,
            "company_name": name,
            "current_state": state,
            "state_history": state_history,
            "documents": documents,
        }
        company_json_abs.parent.mkdir(parents=True, exist_ok=True)
        company_json_abs.write_text(
            json.dumps(company_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        manifest_entries.append({
            "stock_code": code,
            "company_name": name,
            "current_state": state,
            "last_updated": last_updated,
            "doc_count": len(documents),
            "company_json": (company_json_abs.relative_to(repo_root).as_posix()),
        })

    manifest = {
        "generated_at": now_iso,
        "source": "HKEX predefineddocuments=6",
        "schema_version": "1",
        "total_companies": len(all_companies),
        "by_state": by_state,
        "companies": manifest_entries,
    }
    manifest_path = data_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {manifest_path.relative_to(repo_root)} "
          f"({manifest['total_companies']} companies, {by_state})")


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
             "Default: <repo>/data",
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

    ipo_rows = filter_ipo_rows(rows)
    excluded = len(rows) - len(ipo_rows)
    print(f"Filtered to {len(ipo_rows)} global-offering rows "
          f"({excluded} excluded by state rules)")

    if args.dry_run:
        print("\n--dry-run: would process:")
        for r in ipo_rows:
            print(f"  {r.stock_code} {r.company_name} | {r.doc_type} | "
                  f"{r.release_time_raw} | {r.pdf_url}")
        return 0

    data_root.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        stats = await process_rows(ipo_rows, conn, companies_root, repo_root)
        export_json(conn, data_root, repo_root)
    finally:
        conn.close()

    print(
        f"\nSummary: {stats['new']} new, {stats['skipped']} skipped, "
        f"{stats['failed']} failed"
    )
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
