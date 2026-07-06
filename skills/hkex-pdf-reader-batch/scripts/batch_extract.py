#!/usr/bin/env python3
"""PDF 批量读取工具（Skill A）—— MarkItDown 引擎。

把 companies/<code>_<name>/docs/*.pdf 批量转成 Markdown，存到
info/<pdf_stem>.md，注册到 extractions 表（extractor='markitdown_batch_v1'）。

设计原则：
  - 低价值、大批量、零成本（不消耗任何云 API 额度）
  - 跳过已处理的 PDF（除非 --force）
  - 并发 4（默认），可调
  - 失败的 PDF 记录但不中断其他

适用场景：
  - 新股持续入库（招股书 / PHIP / 配发结果公告）
  - 历史招股书回填（bulk）
  - 快速预览（不追求表格保真度）

不适用场景（请用 Skill B `hkex-pdf-reader-precision`）：
  - 深度分析单家公司
  - 财务报表表格高保真
  - 配发结果精读

与 Skill C `hkex-pdf-field-extractor` 的关系：
  本工具产出的 .md 是 Skill C 的"低优先级数据源"。Skill C 默认优先
  读 info/precision/ 下的 MinerU 高精度版（如果存在），没有则回退到本工具的输出。

Usage:
    python skills/hkex-pdf-reader-batch/scripts/batch_extract.py [opts]

Options:
    --data-dir DIR        Override data directory (default <repo>/data)
    --company <code>      Only process one company
    --stage <stage>       Only process companies at this stage (招股/已上市/...)
    --limit N             Process at most N PDFs this run
    --concurrency N       Parallel workers (default 4)
    --force               Re-process even if already extracted
    --dry-run             List what would be processed, don't run
    --pdf <path>          处理任意 PDF（绝对或相对路径，绕过公司目录扫描）
                          与 hkex-chapter-locator 切出的子 PDF 配合使用
    --label <text>        与 --pdf 配合：覆盖输出 .md 文件名（默认沿用 PDF stem）
                          例：--label "招股書_p211-290_財務資料"
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Sibling imports: common.py lives in offering-tracker (canonical home).
_HERE = Path(__file__).resolve().parent
_OFFERING_SCRIPTS = _HERE.parent.parent / "hkex-offering-tracker" / "scripts"
sys.path.insert(0, str(_OFFERING_SCRIPTS))

from common import (  # noqa: E402
    open_db,
    export_json,
    upsert_extraction,
    build_company_dir,
)

# MarkItDown + pypdf are imported lazily so `--help` / `--dry-run` work
# without the optional deps installed. Actual conversion requires both.
_md_module = None
_PypdfReader = None


def _ensure_markitdown():
    global _md_module
    if _md_module is not None:
        return _md_module.MarkItDown
    try:
        import markitdown as _md_module  # type: ignore[import]
    except ImportError:
        sys.stderr.write(
            "ERROR: markitdown not installed. Run:\n"
            "  pip install 'markitdown[pdf]'\n"
        )
        raise
    return _md_module.MarkItDown


def _ensure_pypdf():
    global _PypdfReader
    if _PypdfReader is not None:
        return _PypdfReader
    try:
        from pypdf import PdfReader as _PypdfReader  # type: ignore[import]
    except ImportError:
        _PypdfReader = None  # probe degrades gracefully
    return _PypdfReader


EXTRACTOR_NAME = "markitdown_batch_v1"
FIELD_NAME = "markdown_raw"  # extractions.field_name; Skill C looks for this
DEFAULT_CONCURRENCY = 4


# ---------------------------------------------------------------------------
# Data records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    """One PDF to convert."""

    stock_code: str
    company_name: str
    company_dir: str  # relative dir name like "06951_三環集團"
    pdf_path: Path  # absolute
    pdf_stem: str  # filename without .pdf
    pdf_url_hash: str  # ipo_documents.url_hash for source_pdf_hash linking
    pdf_size: int


# ---------------------------------------------------------------------------
# Discovery + filtering
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def discover_jobs(
    conn,
    companies_root: Path,
    *,
    company: str | None,
    stage: str | None,
    limit: int | None,
    force: bool,
) -> list[Job]:
    """Build the job list.

    Filters:
      - company: only this stock_code
      - stage: only companies whose listing_stage matches
      - limit: cap the result
      - force: include even if extractions already has a row for this
               (stock_code, field_name='markdown_raw', source_pdf_hash)
    """
    where = []
    args: list = []
    if company:
        where.append("stock_code = ?")
        args.append(company)
    if stage:
        where.append("COALESCE(listing_stage, current_state) = ?")
        args.append(stage)
    sql = (
        "SELECT stock_code, company_name FROM companies"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY stock_code"
    )
    rows = conn.execute(sql, args).fetchall()

    # Existing extractions: (stock_code, source_pdf_hash) -> True
    if force:
        existing: set[tuple[str, str]] = set()
    else:
        ex = conn.execute(
            "SELECT stock_code, source_pdf_hash FROM extractions "
            "WHERE extractor = ? AND field_name = ?",
            (EXTRACTOR_NAME, FIELD_NAME),
        ).fetchall()
        existing = {(r[0], r[1]) for r in ex if r[1]}

    jobs: list[Job] = []
    for code, name in rows:
        cdir = companies_root / build_company_dir(code, name)
        docs_dir = cdir / "docs"
        if not docs_dir.is_dir():
            continue
        # Map PDF url_hash via ipo_documents table
        docs_rows = conn.execute(
            "SELECT url_hash, local_path FROM ipo_documents WHERE stock_code = ?",
            (code,),
        ).fetchall()
        path_to_hash = {Path(r[1]).name: r[0] for r in docs_rows if r[1]}
        # Also scan filesystem for any PDFs not yet in DB
        for pdf in sorted(docs_dir.glob("*.pdf")):
            uh = path_to_hash.get(pdf.name)
            if uh is None:
                # PDF on disk but not in ipo_documents; synthesize a hash from path
                uh = hashlib.sha256(str(pdf).encode("utf-8")).hexdigest()[:32]
            if (code, uh) in existing:
                continue
            jobs.append(Job(
                stock_code=code,
                company_name=name,
                company_dir=build_company_dir(code, name),
                pdf_path=pdf,
                pdf_stem=pdf.stem,
                pdf_url_hash=uh,
                pdf_size=pdf.stat().st_size,
            ))
            if limit and len(jobs) >= limit:
                return jobs
    return jobs


# ---------------------------------------------------------------------------
# Probe (Layer 0): page count + has-text-layer detection
# ---------------------------------------------------------------------------


def probe_pdf(pdf_path: Path) -> dict:
    """Quick read of page count and whether the PDF has extractable text.

    Used only for logging/stats; the conversion is the same regardless.
    """
    PypdfReader = _ensure_pypdf()
    if PypdfReader is None:
        return {"pages": None, "has_text": None, "probe": "pypdf_unavailable"}
    try:
        r = PypdfReader(str(pdf_path), strict=False)
        pages = len(r.pages)
        # Check text on first 3 pages
        has_text = False
        for p in r.pages[: min(3, pages)]:
            if (p.extract_text() or "").strip():
                has_text = True
                break
        return {"pages": pages, "has_text": has_text, "probe": "ok"}
    except Exception as exc:
        return {"pages": None, "has_text": None, "probe": f"error: {exc}"}


# ---------------------------------------------------------------------------
# Conversion (Layer 1): MarkItDown single-file
# ---------------------------------------------------------------------------


# MarkItDown is sync; we wrap it in to_thread to run concurrently under asyncio.
_md_instance = None


def _get_md():
    global _md_instance
    if _md_instance is None:
        MarkItDown = _ensure_markitdown()
        _md_instance = MarkItDown(enable_plugins=False)
    return _md_instance


def convert_one_sync(pdf_path: Path) -> tuple[str, str]:
    """Return (markdown_text, content_sha256_hex). Raises on failure."""
    md = _get_md()
    result = md.convert(str(pdf_path))
    text = result.text_content or ""
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text, sha


async def convert_one(pdf_path: Path) -> tuple[str, str]:
    return await asyncio.to_thread(convert_one_sync, pdf_path)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


async def worker(
    job: Job,
    companies_root: Path,
    repo_root: Path,
    stats: dict[str, int],
    sem: asyncio.Semaphore,
) -> None:
    async with sem:
        info_dir = companies_root / job.company_dir / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        out_md = info_dir / f"{job.pdf_stem}.md"

        # Probe for logging
        probe = probe_pdf(job.pdf_path)
        size_mb = job.pdf_size / (1024 * 1024)
        print(
            f"START {job.stock_code} {job.pdf_stem} "
            f"({size_mb:.1f}MB, pages={probe.get('pages')}, "
            f"text={probe.get('has_text')})"
        )

        try:
            text, sha = await convert_one(job.pdf_path)
        except Exception as exc:
            print(f"FAIL  {job.stock_code} {job.pdf_stem}: {exc}", file=sys.stderr)
            stats["failed"] += 1
            return

        out_md.write_text(text, encoding="utf-8")
        try:
            rel_path = out_md.relative_to(repo_root).as_posix()
        except ValueError:
            rel_path = str(out_md)

        # Register in DB (commit done by caller in batch)
        stats["_pending_upserts"].append(
            dict(
                stock_code=job.stock_code,
                field_name=FIELD_NAME,
                output_path=rel_path,
                extractor=EXTRACTOR_NAME,
                extracted_at=_utcnow_iso(),
                source_pdf_hash=job.pdf_url_hash,
                content_sha256=sha,
                notes=f"pages={probe.get('pages')}, size_bytes={job.pdf_size}",
            )
        )
        stats["new"] += 1
        print(f"OK    {job.stock_code} {job.pdf_stem} -> {rel_path} ({len(text)} chars)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--company", type=str, default=None,
                        help="Only process this stock_code")
    parser.add_argument("--stage", type=str, default=None,
                        help="Only companies at this listing_stage")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap number of PDFs this run")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Parallel workers (default {DEFAULT_CONCURRENCY})")
    parser.add_argument("--force", action="store_true",
                        help="Re-process even if already extracted")
    parser.add_argument("--dry-run", action="store_true",
                        help="List jobs, don't run")
    parser.add_argument("--pdf", type=Path, default=None,
                        help="Process arbitrary PDF path (bypass company scan; "
                             "use with --company for DB registration)")
    parser.add_argument("--label", type=str, default=None,
                        help="Override output .md filename stem (use with --pdf)")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[3]
    data_root = (args.data_dir or repo_root / "data").resolve()
    companies_root = data_root / "companies"
    db_path = data_root / "state.db"

    print(f"Repo root : {repo_root}")
    print(f"Data root : {data_root}")

    if not db_path.exists():
        print(f"ERROR: {db_path} does not exist. Run a tracker first.")
        return 2

    conn = open_db(db_path)
    try:
        # ---- Direct-PDF mode (--pdf bypasses discovery) ----
        if args.pdf is not None:
            pdf_path = args.pdf.resolve()
            if not pdf_path.is_file():
                print(f"ERROR: {pdf_path} not found")
                return 2
            # Resolve company for DB registration: --company if given, else
            # try to infer from path components "<code>_<name>/docs/_slices/..."
            code = args.company
            name = ""
            if code is None:
                # walk up looking for "<code>_<name>" segment
                for parent in pdf_path.parents:
                    pname = parent.name
                    if "_" in pname and pname.split("_", 1)[0].isdigit():
                        code = pname.split("_", 1)[0]
                        break
            if code is None:
                print("ERROR: --pdf requires either --company <code> or "
                      "the PDF to live under companies/<code>_<name>/",
                      file=sys.stderr)
                return 2
            row = conn.execute(
                "SELECT company_name FROM companies WHERE stock_code = ?",
                (code,),
            ).fetchone()
            name = row[0] if row else code
            cdir_name = build_company_dir(code, name)

            label = args.label or pdf_path.stem
            # Synthesize url_hash from path so re-runs are idempotent
            uh = hashlib.sha256(str(pdf_path).encode("utf-8")).hexdigest()[:32]
            size = pdf_path.stat().st_size
            job = Job(
                stock_code=code,
                company_name=name,
                company_dir=cdir_name,
                pdf_path=pdf_path,
                pdf_stem=label,
                pdf_url_hash=uh,
                pdf_size=size,
            )
            jobs = [job]
            if args.force:
                pass  # always re-process in --pdf mode
            else:
                ex = conn.execute(
                    "SELECT 1 FROM extractions WHERE extractor=? AND field_name=? "
                    "AND source_pdf_hash=?",
                    (EXTRACTOR_NAME, FIELD_NAME, uh),
                ).fetchone()
                if ex:
                    print(f"Already processed (hash={uh}); use --force to redo")
                    jobs = []

            print(f"Direct-PDF mode: {code} {label}")
            print(f"Discovered {len(jobs)} PDF(s) to process")
        else:
            # ---- Standard discovery mode ----
            jobs = discover_jobs(
                conn, companies_root,
                company=args.company, stage=args.stage,
                limit=args.limit, force=args.force,
            )
            print(f"Discovered {len(jobs)} PDF(s) to process")

        if args.dry_run:
            for j in jobs:
                size_mb = j.pdf_size / (1024 * 1024)
                print(f"  {j.stock_code} {j.pdf_stem} ({size_mb:.1f}MB)")
            return 0

        if not jobs:
            print("Nothing to do.")
            # Refresh manifest anyway
            export_json(conn, data_root, repo_root,
                        source_label="hkex-pdf-reader-batch")
            return 0

        stats: dict = {
            "new": 0,
            "failed": 0,
            "_pending_upserts": [],
        }
        sem = asyncio.Semaphore(max(1, args.concurrency))
        await asyncio.gather(*[
            worker(j, companies_root, repo_root, stats, sem) for j in jobs
        ])

        # Commit DB upserts in one transaction
        for u in stats["_pending_upserts"]:
            upsert_extraction(conn=conn, **u)
        conn.commit()

        # Refresh manifest
        export_json(conn, data_root, repo_root,
                    source_label="hkex-pdf-reader-batch")

        print(
            f"\nSummary: {stats['new']} new, {stats['failed']} failed"
        )
        return 0 if stats["failed"] == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
