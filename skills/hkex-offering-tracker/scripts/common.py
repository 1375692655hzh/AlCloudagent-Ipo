"""Shared helpers for HKEX IPO tracking skills.

This module is reused by:
  - skills/hkex-offering-tracker/scripts/fetch_offerings.py   (招股发行抓取)
  - skills/hkex-application-tracker/scripts/fetch_applications.py  (递表聆讯抓取)

Both skills share the same data/state.db so the IPO lifecycle state machine
(遞表 → 聆訊 → 招股 → 已上市) stays coherent across the two scrapers.

The company schema carries 4 dimensions:
  - listing_stage    : filled by these scrapers from doc_type inference
  - listing_type     : AH / 非-AH  (default 待确认, to be filled by a PDF reader tool)
  - listing_method   : 创业板 / 机制A / 机制B / 18C特专科 (default 待确认)
  - confirmed_name   : final stock name (default null)

The legacy `current_state` column is kept and mirrors `listing_stage`
for backward compatibility with older agents reading the manifest.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)


# ---------------------------------------------------------------------------
# Network constants (shared)
# ---------------------------------------------------------------------------

HKEX_BASE = "https://www1.hkexnews.hk"
USER_AGENT = "Mozilla/5.0 (compatible; hkex-tracker/2.0; +https://github.com/)"

CONCURRENCY = 4
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=15.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(180.0, connect=15.0)

# Windows / POSIX illegal filename characters.
ILLEGAL_FN_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')

# HKEX release time format: "30/06/2026 06:57"
RELEASE_TIME_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})")

# HKEX filing date format: "08/06/2026" (no time component)
FILING_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


# ---------------------------------------------------------------------------
# Time / filename helpers
# ---------------------------------------------------------------------------


def parse_release_time_iso(raw: str) -> str | None:
    """Convert "30/06/2026 06:57" -> ISO8601 with +08:00 (HK time)."""
    m = RELEASE_TIME_RE.search(raw or "")
    if not m:
        return None
    dd, mm, yyyy, hh, mi = m.groups()
    hk_tz = timezone(timedelta(hours=8))
    try:
        dt = datetime(int(yyyy), int(mm), int(dd), int(hh), int(mi), tzinfo=hk_tz)
    except ValueError:
        return None
    return dt.isoformat()


def parse_filing_date_iso(raw: str) -> str | None:
    """Convert "08/06/2026" -> ISO8601 date with +08:00 (HK time, midnight)."""
    m = FILING_DATE_RE.search(raw or "")
    if not m:
        return None
    dd, mm, yyyy = m.groups()
    hk_tz = timezone(timedelta(hours=8))
    try:
        dt = datetime(int(yyyy), int(mm), int(dd), tzinfo=hk_tz)
    except ValueError:
        return None
    return dt.isoformat()


def sanitize_filename(name: str) -> str:
    """Replace filesystem-illegal characters with underscore."""
    return ILLEGAL_FN_CHARS.sub("_", name).strip().rstrip(".")


def url_hash(pdf_url: str) -> str:
    return hashlib.sha256(pdf_url.encode("utf-8")).hexdigest()[:32]


def build_doc_filename(doc_type: str, state: str, release_time_raw: str) -> str:
    """Build '<doc_type>_<state>_<YYYYMMDD_HHMMSS>.pdf'.

    Falls back to filing-date granularity when no time component is present
    (the application-tracker source only exposes a date, not a time).
    """
    iso = parse_release_time_iso(release_time_raw) or parse_filing_date_iso(release_time_raw)
    if iso:
        ts = iso.replace("-", "").replace(":", "")[:15]  # YYYYMMDDTHHMMSS or YYYYMMDDT00:00:00
        ts = ts.replace("T", "_")
    else:
        ts = "unknown"
    return f"{sanitize_filename(doc_type)}_{state}_{ts}.pdf"


def build_company_dir(stock_code: str, company_name: str) -> str:
    return f"{sanitize_filename(stock_code)}_{sanitize_filename(company_name)}"


def _try_relative(path: Path, base: Path) -> str:
    """Return path relative to base if possible, else absolute string."""
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return str(path)


def company_json_rel(companies_root: Path, company_dir: str, repo_root: Path) -> str:
    p = companies_root / company_dir / "company.json"
    return _try_relative(p, repo_root)


# ---------------------------------------------------------------------------
# SQLite schema (extended v2)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    stock_code        TEXT PRIMARY KEY,
    company_name      TEXT NOT NULL,
    current_state     TEXT NOT NULL,           -- legacy field, mirrors listing_stage
    listing_stage     TEXT,                    -- 遞表/聆訊/招股/已上市
    listing_type      TEXT,                    -- AH / 非-AH (default 待确认)
    listing_method    TEXT,                    -- 创业板 / 机制A / 机制B / 18C特专科
    confirmed_name    TEXT,                    -- final confirmed stock name
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

-- Derived-info index: structured data extracted from PDFs (financials,
-- shareholders, use-of-proceeds, etc.). Each row points to a JSON/TXT file
-- under companies/<code>_<name>/info/. Written by the (future) PDF-reader
-- skill; consumed by analysis reports.
CREATE TABLE IF NOT EXISTS extractions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code      TEXT NOT NULL,
    source_pdf_hash TEXT,                       -- FK -> ipo_documents.url_hash (nullable if manual)
    extractor       TEXT NOT NULL,              -- 'pdf_reader_v1', 'manual', 'gpt-4', ...
    field_name      TEXT NOT NULL,              -- 'financials', 'shareholders', 'use_of_proceeds', ...
    output_path     TEXT NOT NULL,              -- relative to repo root
    extracted_at    TEXT NOT NULL,
    content_sha256  TEXT,
    notes           TEXT,
    FOREIGN KEY (stock_code) REFERENCES companies(stock_code)
);

-- Analysis-reports index: human or AI authored documents (valuation, risk
-- assessment, listing-quality notes). Files live under
-- companies/<code>_<name>/reports/. NOT auto-rebuilt — these are主观产出.
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code      TEXT NOT NULL,
    report_type     TEXT NOT NULL,              -- 'valuation', 'risk', 'quality', ...
    title           TEXT,
    author          TEXT,                       -- 'human', 'gpt-4', 'claude', ...
    version         INTEGER DEFAULT 1,
    file_path       TEXT NOT NULL,              -- relative to repo root
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    source_extractions TEXT,                    -- JSON array of extraction IDs
    FOREIGN KEY (stock_code) REFERENCES companies(stock_code)
);

CREATE INDEX IF NOT EXISTS idx_doc_stock ON ipo_documents(stock_code);
CREATE INDEX IF NOT EXISTS idx_doc_state ON ipo_documents(inferred_state);
CREATE INDEX IF NOT EXISTS idx_hist_stock ON state_history(stock_code);
CREATE INDEX IF NOT EXISTS idx_ext_stock ON extractions(stock_code);
CREATE INDEX IF NOT EXISTS idx_ext_field ON extractions(field_name);
CREATE INDEX IF NOT EXISTS idx_rep_stock ON reports(stock_code);
CREATE INDEX IF NOT EXISTS idx_rep_type ON reports(report_type);
"""

# Incremental upgrades for existing v1 databases (idempotent: each ADD COLUMN
# fails silently if the column already exists).
MIGRATIONS = [
    "ALTER TABLE companies ADD COLUMN listing_stage TEXT",
    "ALTER TABLE companies ADD COLUMN listing_type TEXT",
    "ALTER TABLE companies ADD COLUMN listing_method TEXT",
    "ALTER TABLE companies ADD COLUMN confirmed_name TEXT",
]


def open_db(db_path: Path) -> sqlite3.Connection:
    """Open (or create) the shared state DB and migrate to the v2 schema.

    For pre-existing v1 DBs we backfill the new columns with sensible defaults:
      - listing_stage   = current_state
      - listing_type    = '待确认' (JSON 无法识别 AH，留给 PDF 工具)
      - listing_method  = 由 infer_method_from_name(stock_code, company_name) 重算
      - confirmed_name  = company_name（初始值；PDF 工具可后续覆盖）
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.execute(
        "UPDATE companies SET listing_stage = current_state WHERE listing_stage IS NULL"
    )
    conn.execute(
        "UPDATE companies SET listing_type = '待确认' WHERE listing_type IS NULL"
    )
    # Backfill listing_method for rows still at the old hardcoded default
    # ('待确认' from a previous v2.0 run) by recomputing via the new heuristic.
    # This upgrades pre-existing rows to the v2.1 suffix-based logic.
    rows = conn.execute(
        "SELECT stock_code, company_name FROM companies "
        "WHERE listing_method IS NULL OR listing_method = '待确认'"
    ).fetchall()
    for code, name in rows:
        method = infer_method_from_name(name or "", code or "")
        conn.execute(
            "UPDATE companies SET listing_method = ? WHERE stock_code = ?",
            (method, code),
        )
    # Backfill confirmed_name with company_name (initial value).
    conn.execute(
        "UPDATE companies SET confirmed_name = company_name "
        "WHERE confirmed_name IS NULL"
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Listing-method heuristics
# ---------------------------------------------------------------------------


def infer_method_from_name(company_name: str, stock_code: str = "") -> str:
    """Heuristic mapping from HKEX applicant/symbol metadata to a listing method.

    HKEX encodes the Chapter 8 listing route in the company name suffix:
      "- B" -> Chapter 18A (未盈利生物科技公司, 机制B)
      "- P" -> Chapter 18C (特专科技公司, 18C特专科)
      "- W" -> Chapter 8A  (同股不同权 / Weighted Voting Rights)
    GEM (创业板) codes start with "08" or "09"; application-stage codes use
    the synthetic "APP-{id}" form and do NOT carry GEM information by code,
    so for APP-* rows we rely solely on the board flag passed by the caller.

    Returns one of: '创业板' / '机制B' / '18C特专科' / 'WVR' / '待确认'.
    Future PDF-reader tooling may override this with the authoritative value.
    """
    if stock_code:
        if stock_code.startswith(("08", "09")):
            return "创业板"
    name = (company_name or "").strip()
    # HKEX suffixes are typically " - B", " - P", " - W" (with the literal
    # " - " separator before the letter). Match either ASCII or fullwidth hyphen.
    if name.endswith((" - B", " -b", " -b".upper())):
        return "机制B"
    if name.endswith(" - P"):
        return "18C特专科"
    if name.endswith(" - W"):
        return "WVR"
    return "待确认"


# ---------------------------------------------------------------------------
# UPSERT helpers
# ---------------------------------------------------------------------------


def upsert_company(
    conn: sqlite3.Connection,
    stock_code: str,
    company_name: str,
    new_stage: str,
    now_iso: str,
    company_json_path: str,
) -> None:
    """UPSERT a company row, appending a state_history entry on stage change.

    Writes both `current_state` (legacy) and `listing_stage` (new) so the row
    is consistent for old and new readers. Also fills the 3 reserved dims:
      - listing_type     : '待确认' (JSON 没有此字段，留给 PDF 工具)
      - listing_method   : 由 infer_method_from_name() 启发式推断
      - confirmed_name   : 初始值 = company_name（PDF 工具可后续覆盖）
    """
    method = infer_method_from_name(company_name, stock_code)
    existing = conn.execute(
        "SELECT listing_stage FROM companies WHERE stock_code = ?",
        (stock_code,),
    ).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO companies
               (stock_code, company_name, current_state, listing_stage,
                listing_type, listing_method, confirmed_name,
                first_seen, last_updated, company_json_path)
               VALUES (?, ?, ?, ?, '待确认', ?, ?, ?, ?, ?)""",
            (stock_code, company_name, new_stage, new_stage,
             method, company_name, now_iso, now_iso, company_json_path),
        )
        conn.execute(
            """INSERT INTO state_history
               (stock_code, old_state, new_state, changed_at, evidence)
               VALUES (?, NULL, ?, ?, 'initial observation')""",
            (stock_code, new_stage, now_iso),
        )
    else:
        old_stage = existing[0]
        conn.execute(
            """UPDATE companies SET company_name = ?, current_state = ?,
               listing_stage = ?, listing_method = ?, confirmed_name = ?,
               last_updated = ?, company_json_path = ?
               WHERE stock_code = ?""",
            (company_name, new_stage, new_stage, method, company_name,
             now_iso, company_json_path, stock_code),
        )
        if old_stage != new_stage:
            conn.execute(
                """INSERT INTO state_history
                   (stock_code, old_state, new_state, changed_at, evidence)
                   VALUES (?, ?, ?, ?, ?)""",
                (stock_code, old_stage, new_stage, now_iso,
                 f"state changed {old_stage} -> {new_stage}"),
            )


# ---------------------------------------------------------------------------
# Derived-info & analysis-report upserts (v2.2 three-store architecture)
# ---------------------------------------------------------------------------


def upsert_extraction(
    conn: sqlite3.Connection,
    stock_code: str,
    field_name: str,
    output_path: str,
    extractor: str,
    extracted_at: str,
    source_pdf_hash: str | None = None,
    content_sha256: str | None = None,
    notes: str | None = None,
) -> int:
    """Insert or update one extraction record (keyed on stock_code + field_name).

    Re-running the same extractor on the same company overwrites the previous
    output_path / sha / timestamp, keeping history minimal. Returns the row id.
    """
    existing = conn.execute(
        "SELECT id FROM extractions WHERE stock_code = ? AND field_name = ?",
        (stock_code, field_name),
    ).fetchone()
    if existing is None:
        cur = conn.execute(
            """INSERT INTO extractions
               (stock_code, source_pdf_hash, extractor, field_name,
                output_path, extracted_at, content_sha256, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (stock_code, source_pdf_hash, extractor, field_name,
             output_path, extracted_at, content_sha256, notes),
        )
        return cur.lastrowid
    rid = existing[0]
    conn.execute(
        """UPDATE extractions SET
           source_pdf_hash = ?, extractor = ?, output_path = ?,
           extracted_at = ?, content_sha256 = ?, notes = ?
           WHERE id = ?""",
        (source_pdf_hash, extractor, output_path,
         extracted_at, content_sha256, notes, rid),
    )
    return rid


def upsert_report(
    conn: sqlite3.Connection,
    stock_code: str,
    report_type: str,
    file_path: str,
    created_at: str,
    title: str | None = None,
    author: str | None = None,
    version: int = 1,
    source_extractions: list[int] | None = None,
    updated_at: str | None = None,
) -> int:
    """Insert a new report version. Reports are append-mostly by design —
    each (stock_code, report_type, version) is unique so historical drafts
    are preserved. Pass version=2/3/... explicitly when iterating."""
    src_json = json.dumps(source_extractions) if source_extractions else None
    cur = conn.execute(
        """INSERT INTO reports
           (stock_code, report_type, title, author, version, file_path,
            created_at, updated_at, source_extractions)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (stock_code, report_type, title, author, version, file_path,
         created_at, updated_at or created_at, src_json),
    )
    return cur.lastrowid


def index_company_subdir(
    company_dir: Path,
    subdir: str,
    repo_root: Path,
    suffixes: tuple[str, ...] = (".json", ".md", ".txt"),
) -> list[dict]:
    """Scan companies/<code>_<name>/<subdir>/ and return one entry per file.

    Used by export_json to surface info/ and reports/ contents in company.json
    without requiring a DB query. Returns [] if the subdir doesn't exist.
    """
    out: list[dict] = []
    d = company_dir / subdir
    if not d.is_dir():
        return out
    for f in sorted(d.iterdir()):
        if not f.is_file() or f.suffix.lower() not in suffixes:
            continue
        try:
            rel = f.relative_to(repo_root).as_posix()
        except ValueError:
            rel = str(f)
        out.append({
            "name": f.name,
            "path": rel,
            "size_bytes": f.stat().st_size,
            "modified_at": datetime.fromtimestamp(
                f.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
        })
    return out


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
    """Stream-download a PDF to dest, returning (sha256_hex, size_bytes).

    Atomic write: download to dest+'.part', then os.replace() to the final name
    so partial downloads never leave a corrupted 'real' file.
    """
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
# Concurrency-limited row processing (shared skeleton)
# ---------------------------------------------------------------------------


async def process_rows(
    rows: list,
    conn: sqlite3.Connection,
    companies_root: Path,
    repo_root: Path,
    handle_row,
) -> dict[str, int]:
    """Run handle_row(row, conn, client, companies_root, repo_root, now_iso, stats)
    for each row, with a global concurrency cap.

    `handle_row` must be async and is responsible for its own UPSERT logic.
    """
    stats = {"new": 0, "skipped": 0, "failed": 0}
    now_iso = datetime.now(timezone.utc).astimezone().isoformat()
    sem = asyncio.Semaphore(CONCURRENCY)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"}

    async with httpx.AsyncClient(
        headers=headers, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True
    ) as client:
        async def runner(row):
            async with sem:
                await handle_row(row, conn, client, companies_root,
                                 repo_root, now_iso, stats)
        await asyncio.gather(*[runner(r) for r in rows])
    conn.commit()
    return stats


# ---------------------------------------------------------------------------
# JSON export (manifest + per-company)
# ---------------------------------------------------------------------------


def export_json(conn: sqlite3.Connection, data_root: Path, repo_root: Path,
                source_label: str = "HKEX") -> None:
    """Rebuild manifest.json + all company.json from SQLite (idempotent).

    `source_label` lets each fetcher identify itself in the manifest.
    The manifest itself is shared, so the latest writer's label wins; this is
    fine because we always recompute from the full DB.
    """
    companies_root = data_root / "companies"
    companies_root.mkdir(parents=True, exist_ok=True)

    now_iso = datetime.now(timezone.utc).astimezone().isoformat()
    all_companies = conn.execute(
        "SELECT stock_code, company_name, current_state, listing_stage, "
        "       listing_type, listing_method, confirmed_name, last_updated "
        "FROM companies ORDER BY stock_code"
    ).fetchall()

    by_stage: dict[str, int] = {s: 0 for s in ("遞表", "聆訊", "招股", "已上市")}
    manifest_entries = []

    for (code, name, current_state, stage, ltype, lmethod, cname,
         last_updated) in all_companies:
        # Prefer listing_stage; fall back to current_state for legacy rows.
        effective_stage = stage or current_state
        by_stage[effective_stage] = by_stage.get(effective_stage, 0) + 1
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
            # New 4-dimension state model:
            "listing_stage": effective_stage,
            "listing_type": ltype or "待确认",
            "listing_method": lmethod or "待确认",
            "confirmed_name": cname,
            # Legacy compatibility:
            "current_state": effective_stage,
            "state_history": state_history,
            "documents": documents,
            # Three-store architecture (v2.2):
            # - documents: Raw素材库 (HKEX PDFs, indexed above)
            # - extractions: Derived信息库 (PDF提取的结构化数据)
            # - reports: Analysis报告库 (人工/AI 分析)
            "extractions": [
                {
                    "field_name": e[0],
                    "extractor": e[1],
                    "output_path": e[2],
                    "extracted_at": e[3],
                    "source_pdf_hash": e[4],
                    "content_sha256": e[5],
                }
                for e in conn.execute(
                    "SELECT field_name, extractor, output_path, extracted_at, "
                    "       source_pdf_hash, content_sha256 "
                    "FROM extractions WHERE stock_code = ? "
                    "ORDER BY field_name",
                    (code,),
                ).fetchall()
            ],
            "reports": [
                {
                    "report_type": r[0],
                    "title": r[1],
                    "author": r[2],
                    "version": r[3],
                    "file_path": r[4],
                    "updated_at": r[5],
                }
                for r in conn.execute(
                    "SELECT report_type, title, author, version, file_path, updated_at "
                    "FROM reports WHERE stock_code = ? "
                    "ORDER BY report_type, version DESC",
                    (code,),
                ).fetchall()
            ],
            # Filesystem scan: lists any files present in info/ and reports/
            # subdirectories even if not yet registered in the DB tables above.
            # This lets the directory be the source of truth during early
            # manual authoring before tools register entries.
            "info_files": index_company_subdir(
                companies_root / company_dir, "info", repo_root
            ),
            "report_files": index_company_subdir(
                companies_root / company_dir, "reports", repo_root
            ),
        }
        company_json_abs.parent.mkdir(parents=True, exist_ok=True)
        company_json_abs.write_text(
            json.dumps(company_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        manifest_entries.append({
            "stock_code": code,
            "company_name": name,
            "listing_stage": effective_stage,
            "listing_type": ltype or "待确认",
            "listing_method": lmethod or "待确认",
            "confirmed_name": cname,
            "current_state": effective_stage,  # legacy
            "last_updated": last_updated,
            "doc_count": len(documents),
            "extraction_count": len(company_json["extractions"]),
            "report_count": len(company_json["reports"]),
            "company_json": _try_relative(company_json_abs, repo_root),
        })

    manifest = {
        "generated_at": now_iso,
        "source": source_label,
        "schema_version": "2.2",
        "architecture": {
            "raw_store": "companies/<code>_<name>/docs/   (HKEX PDFs, append-only)",
            "derived_store": "companies/<code>_<name>/info/   (PDF-extracted structured data)",
            "analysis_store": "companies/<code>_<name>/reports/ (human/AI analysis reports)",
            "index_db": "state.db   (companies / ipo_documents / extractions / reports)",
        },
        "total_companies": len(all_companies),
        "by_stage": by_stage,
        "by_state": by_stage,  # legacy alias
        "by_method": _build_dimension_index(manifest_entries, "listing_method"),
        "by_type": _build_dimension_index(manifest_entries, "listing_type"),
        "companies": manifest_entries,
    }
    manifest_path = data_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {_try_relative(manifest_path, repo_root)} "
          f"({manifest['total_companies']} companies, {by_stage})")

    # Also write per-dimension slice views under data/views/ for direct access.
    views_root = data_root / "views"
    views_root.mkdir(parents=True, exist_ok=True)
    _write_dimension_view(views_root / "by_stage.json", manifest_entries,
                          "listing_stage", now_iso, repo_root)
    _write_dimension_view(views_root / "by_method.json", manifest_entries,
                          "listing_method", now_iso, repo_root)
    _write_dimension_view(views_root / "by_type.json", manifest_entries,
                          "listing_type", now_iso, repo_root)


def _build_dimension_index(entries: list[dict], field: str) -> dict:
    """Build {value: count} summary for one dimension across all companies."""
    out: dict[str, int] = {}
    for e in entries:
        v = e.get(field) or "待确认"
        out[v] = out.get(v, 0) + 1
    return out


def _write_dimension_view(
    out_path: Path,
    entries: list[dict],
    field: str,
    now_iso: str,
    repo_root: Path,
) -> None:
    """Write a per-dimension slice file: {value: [company summaries]}.

    Lets an Agent ask "show me all 机制B companies" by reading one file
    instead of filtering the full manifest.
    """
    grouped: dict[str, list[dict]] = {}
    for e in entries:
        v = e.get(field) or "待确认"
        grouped.setdefault(v, []).append({
            "stock_code": e["stock_code"],
            "company_name": e["company_name"],
            "listing_stage": e.get("listing_stage"),
            "listing_method": e.get("listing_method"),
            "listing_type": e.get("listing_type"),
            "confirmed_name": e.get("confirmed_name"),
            "doc_count": e.get("doc_count", 0),
            "extraction_count": e.get("extraction_count", 0),
            "report_count": e.get("report_count", 0),
            "company_json": e.get("company_json"),
        })
    view = {
        "generated_at": now_iso,
        "dimension": field,
        "counts": {k: len(v) for k, v in grouped.items()},
        "groups": grouped,
    }
    out_path.write_text(
        json.dumps(view, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {_try_relative(out_path, repo_root)} "
          f"(dimension={field}, groups={list(grouped.keys())})")
