#!/usr/bin/env python3
"""章节定位 + PDF 切片工具。

把"我要招股书的财务资料章节"这种语义请求转换成"PDF 第 211-290 页"或"已切好的子 PDF"。

三层定位策略（按可靠性递减）：
  层 1：PDF 书签直读（read_bookmarks）
        - 最准，但招股书不一定都有书签
  层 2：目录页文本 + 偏移量推算（extract_toc_text + calculate_offset）
        - 用 LLM 解析目录得到印刷页码，页脚投票算偏移量
  层 3：手动指定页码范围（--pages 211-290）
        - 用户自己看一眼 PDF 给出范围，工具只负责切片

切片功能（改动 2）：
  locate 完成后用 pypdf 切出子 PDF，存到 docs/_slices/。
  这个子 PDF 直接喂给 Skill A/B 的 --pdf 参数即可。

Usage:
    # 1. 列出某公司招股书的所有章节（从书签或目录）
    python locate_chapter.py --company 06951 --list

    # 2. 找"財務資料"章节的页范围
    python locate_chapter.py --company 06951 --chapter "財務資料"

    # 3. 找章节并切片（输出 docs/_slices/..._財務資料.pdf）
    python locate_chapter.py --company 06951 --chapter "財務資料" --slice

    # 4. 手动指定页范围切片（跳过定位，绕过 LLM）
    python locate_chapter.py --company 06951 --pages 211-290 --slice --label "財務資料"

    # 5. 探测书签覆盖率（诊断用，看库内 PDF 是否带书签）
    python locate_chapter.py --probe-bookmarks
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_OFFERING_SCRIPTS = _HERE.parent.parent / "hkex-offering-tracker" / "scripts"
sys.path.insert(0, str(_OFFERING_SCRIPTS))
sys.path.insert(0, str(_HERE))

from common import open_db, build_company_dir, upsert_extraction, export_json  # noqa: E402
from bookmark_reader import read_bookmarks, find_chapter_by_bookmark, Bookmark  # noqa: E402
from offset_calculator import calculate_offset, printed_to_pdf, OffsetResult  # noqa: E402
from toc_parser import (  # noqa: E402
    extract_toc_text,
    parse_toc_with_llm,
    find_chapter_in_toc,
    TOCEntry,
)

EXTRACTOR_NAME = "chapter_locator_v1"
FIELD_NAME = "chapter_map"


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


@dataclass
class ChapterLocation:
    """Where a chapter is in a PDF."""
    chapter: str
    pdf_start: int            # 0-indexed, inclusive
    pdf_end: int              # 0-indexed, inclusive
    printed_start: int | None # the printed page number, if known (None for bookmarks)
    printed_end: int | None
    source: str               # 'bookmark' / 'toc+footer' / 'manual' / 'toc+estimated_offset'
    method_detail: str        # human-readable details

    @property
    def page_count(self) -> int:
        return self.pdf_end - self.pdf_start + 1

    def to_slice_range(self) -> str:
        """Range string for slicing (start..end inclusive, 0-indexed)."""
        return f"{self.pdf_start}-{self.pdf_end}"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _find_pdf_for_company(
    conn, companies_root: Path, code: str, *, doc_keyword: str = "招股",
) -> Path | None:
    """Find the most likely IPO PDF for a company.

    Strategy: look up ipo_documents for the company; prefer files whose
    local_path contains the doc_keyword (招股 = prospectus); fall back to first.
    """
    rows = conn.execute(
        "SELECT local_path FROM ipo_documents WHERE stock_code = ?",
        (code,),
    ).fetchall()
    paths = [Path(r[0]) for r in rows if r[0]]
    if not paths:
        return None
    data_root = companies_root.parent
    resolved = []
    for p in paths:
        candidates: list[Path] = []
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates.append((data_root / p).resolve())
            candidates.append(p.resolve())  # cwd-relative legacy
        for cand in candidates:
            if cand.is_file():
                resolved.append(cand)
                break
    if not resolved:
        return None
    # Prefer招股 / PHIP / 全球發售
    for kw in ("招股", "PHIP", "全球發售", "聆訊"):
        for p in resolved:
            if kw in p.name:
                return p
    return resolved[0]


# --------------------------------------------------------------------------- #
# Layer 1: bookmark-based
# --------------------------------------------------------------------------- #


def locate_via_bookmarks(
    pdf_path: Path,
    chapter_query: str,
    *,
    total_pages: int,
) -> ChapterLocation | None:
    """Layer 1: use PDF outline to find chapter."""
    bms = read_bookmarks(pdf_path)
    if not bms:
        return None
    found = find_chapter_by_bookmark(bms, chapter_query)
    if found is None:
        return None
    start_bm, end_bm = found
    pdf_start = start_bm.pdf_page
    pdf_end = (end_bm.pdf_page - 1) if end_bm else (total_pages - 1)
    if pdf_end < pdf_start:
        pdf_end = pdf_start
    return ChapterLocation(
        chapter=start_bm.title,
        pdf_start=pdf_start,
        pdf_end=pdf_end,
        printed_start=None,  # bookmarks don't carry printed page info
        printed_end=None,
        source="bookmark",
        method_detail=f"matched bookmark '{start_bm.title}' "
                      f"(level={start_bm.level}); "
                      f"end={end_bm.title if end_bm else 'EOF'}",
    )


# --------------------------------------------------------------------------- #
# Layer 2: TOC + offset
# --------------------------------------------------------------------------- #


def locate_via_toc(
    pdf_path: Path,
    chapter_query: str,
    *,
    total_pages: int,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
) -> ChapterLocation | None:
    """Layer 2: parse TOC text via LLM, compute offset via footer vote."""
    toc_text = extract_toc_text(pdf_path)
    if not toc_text:
        return None
    toc_result = parse_toc_with_llm(
        toc_text, model=model, base_url=base_url, api_key=api_key,
    )
    if toc_result.error or not toc_result.entries:
        return None

    found = find_chapter_in_toc(toc_result.entries, chapter_query)
    if found is None:
        return None
    start_e, end_e = found

    # Compute offset
    offset_res = calculate_offset(pdf_path)
    if offset_res.offset is None:
        # Fallback: assume offset 0 (best effort)
        offset_val = 0
        offset_desc = "unknown (assumed 0)"
    else:
        offset_val = offset_res.offset
        offset_desc = offset_res.describe()

    pdf_start = printed_to_pdf(start_e.printed_page, offset_val)
    if end_e:
        pdf_end = printed_to_pdf(end_e.printed_page, offset_val) - 1
    else:
        pdf_end = total_pages - 1

    # Clamp
    pdf_start = max(0, min(pdf_start, total_pages - 1))
    pdf_end = max(pdf_start, min(pdf_end, total_pages - 1))

    return ChapterLocation(
        chapter=start_e.chapter,
        pdf_start=pdf_start,
        pdf_end=pdf_end,
        printed_start=start_e.printed_page,
        printed_end=end_e.printed_page if end_e else None,
        source="toc+footer" if offset_res.offset is not None else "toc+estimated_offset",
        method_detail=(
            f"toc match '{start_e.chapter}' printed={start_e.printed_page}"
            + (f"-{end_e.printed_page}" if end_e else "-EOF")
            + f"; offset: {offset_desc}"
        ),
    )


# --------------------------------------------------------------------------- #
# Layer 3: manual page range (user-supplied)
# --------------------------------------------------------------------------- #


def locate_manual(
    pages_range: str,
    total_pages: int,
    chapter_label: str,
) -> ChapterLocation:
    """Layer 3: user supplied --pages start-end (1-indexed or 0-indexed flexible)."""
    m = re.match(r"^(\d+)\s*[-–]\s*(\d+)$", pages_range.strip())
    if not m:
        raise ValueError(f"invalid --pages '{pages_range}'; expected 'N-M'")
    start = int(m.group(1))
    end = int(m.group(2))
    if start < 0 or end < start or end >= total_pages:
        raise ValueError(
            f"--pages {pages_range} out of range (PDF has {total_pages} pages, "
            f"valid 0..{total_pages - 1})"
        )
    return ChapterLocation(
        chapter=chapter_label,
        pdf_start=start,
        pdf_end=end,
        printed_start=None,
        printed_end=None,
        source="manual",
        method_detail=f"user-supplied range {pages_range}",
    )


# --------------------------------------------------------------------------- #
# Slicing
# --------------------------------------------------------------------------- #


def slice_pdf(
    src_pdf: Path,
    location: ChapterLocation,
    companies_root: Path,
    company_dir: str,
) -> Path:
    """Slice the source PDF and save under docs/_slices/. Returns slice path."""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        raise RuntimeError("pypdf not installed; run: pip install pypdf")

    reader = PdfReader(str(src_pdf), strict=False)
    writer = PdfWriter()
    # pypdf pages list is 0-indexed
    for i in range(location.pdf_start, location.pdf_end + 1):
        if i < len(reader.pages):
            writer.add_page(reader.pages[i])

    slices_dir = companies_root / company_dir / "docs" / "_slices"
    slices_dir.mkdir(parents=True, exist_ok=True)

    # Filename: <pdf_stem>_p<start>-<end>_<safe_chapter>.pdf
    safe_ch = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", location.chapter)[:30]
    out_name = f"{src_pdf.stem}_p{location.pdf_start}-{location.pdf_end}_{safe_ch}.pdf"
    out_path = slices_dir / out_name
    with out_path.open("wb") as f:
        writer.write(f)
    return out_path


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(
        description="章节定位 + PDF 切片工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--company", type=str, default=None,
                        help="stock_code (required unless --probe-bookmarks)")
    parser.add_argument("--chapter", type=str, default=None,
                        help="Chapter name to locate (e.g. '財務資料')")
    parser.add_argument("--list", action="store_true",
                        help="List all chapters (from bookmarks or TOC) and exit")
    parser.add_argument("--pages", type=str, default=None,
                        help="Manual page range 'START-END' (0-indexed, "
                             "inclusive); bypass locator")
    parser.add_argument("--slice", action="store_true",
                        help="Slice the PDF at the located range and save to "
                             "docs/_slices/")
    parser.add_argument("--label", type=str, default=None,
                        help="Override chapter label (used in slice filename "
                             "and DB notes)")
    parser.add_argument("--pdf", type=Path, default=None,
                        help="Override: use this PDF directly (skip DB lookup)")
    parser.add_argument("--probe-bookmarks", action="store_true",
                        help="Scan all PDFs in DB and report bookmark coverage")
    parser.add_argument("--model", type=str, default=None,
                        help="LLM model (default $LLM_MODEL)")
    parser.add_argument("--base-url", type=str, default=None)
    parser.add_argument("--api-key", type=str, default=None)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[3]
    data_root = (args.data_dir or repo_root / "data").resolve()
    companies_root = data_root / "companies"
    db_path = data_root / "state.db"

    # ----- Probe mode -----
    if args.probe_bookmarks:
        if not db_path.exists():
            print("No state.db; nothing to probe")
            return 0
        conn = open_db(db_path)
        try:
            rows = conn.execute(
                "SELECT DISTINCT stock_code, local_path FROM ipo_documents "
                "WHERE local_path IS NOT NULL"
            ).fetchall()
            with_bm, without_bm, total = 0, 0, 0
            for code, lp in rows:
                p = Path(lp)
                if not p.is_absolute():
                    # Resolve relative to data_root
                    p = (data_root / p).resolve()
                if not p.is_file():
                    # Also try cwd-relative (legacy)
                    cwd_p = Path(lp).resolve()
                    if cwd_p.is_file():
                        p = cwd_p
                    else:
                        continue
                total += 1
                bms = read_bookmarks(p)
                if bms:
                    with_bm += 1
                    top_count = sum(1 for b in bms if b.level == 0)
                    print(f"  OK {code} {p.name}  ({top_count} top-level, "
                          f"{len(bms)} total)")
                else:
                    without_bm += 1
                    print(f"  -- {code} {p.name}  (no bookmarks)")
            print(f"\nBookmark coverage: {with_bm}/{total} "
                  f"({with_bm * 100 // max(1, total)}%)")
            return 0
        finally:
            conn.close()

    # ----- Normal mode -----
    if not args.company:
        print("ERROR: --company required (or --probe-bookmarks)",
              file=sys.stderr)
        return 2
    if not db_path.exists():
        print(f"ERROR: {db_path} does not exist", file=sys.stderr)
        return 2

    conn = open_db(db_path)
    try:
        row = conn.execute(
            "SELECT company_name FROM companies WHERE stock_code = ?",
            (args.company,),
        ).fetchone()
        if row is None:
            print(f"ERROR: company {args.company} not in DB", file=sys.stderr)
            return 2
        company_name = row[0]
        cdir_name = build_company_dir(args.company, company_name)

        # Resolve PDF
        if args.pdf:
            pdf_path = args.pdf.resolve()
        else:
            pdf_path = _find_pdf_for_company(conn, companies_root, args.company)
        if pdf_path is None or not pdf_path.is_file():
            print(f"ERROR: no PDF found for {args.company} "
                  f"(set --pdf explicitly)", file=sys.stderr)
            return 2

        # Count pages
        try:
            from pypdf import PdfReader
            total_pages = len(PdfReader(str(pdf_path), strict=False).pages)
        except Exception as exc:
            print(f"ERROR: cannot read PDF {pdf_path}: {exc}", file=sys.stderr)
            return 2

        print(f"PDF: {pdf_path.name} ({total_pages} pages)")

        # ----- --list mode -----
        if args.list:
            print("\nBookmarks (Layer 1):")
            bms = read_bookmarks(pdf_path)
            if bms:
                for b in bms:
                    if b.level == 0:
                        print(f"  p{b.pdf_page:4d}  {b.title}")
                    elif b.level == 1:
                        print(f"    p{b.pdf_page:4d}    {b.title}")
            else:
                print("  (no bookmarks; would need LLM TOC parse)")
                toc_text = extract_toc_text(pdf_path)
                if toc_text:
                    print(f"  (TOC text available, {len(toc_text)} chars; "
                          f"re-run with --chapter X to use LLM parse)")
            return 0

        # ----- Locate -----
        location: ChapterLocation | None = None
        method_used = ""

        if args.pages:
            # Layer 3: manual
            try:
                location = locate_manual(
                    args.pages, total_pages,
                    chapter_label=args.label or "manual_range",
                )
                method_used = "manual"
            except ValueError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 2
        else:
            if not args.chapter:
                print("ERROR: --chapter or --pages required", file=sys.stderr)
                return 2

            # Try Layer 1 first
            print(f"\n[Layer 1] Trying bookmarks for '{args.chapter}'...")
            location = locate_via_bookmarks(
                pdf_path, args.chapter, total_pages=total_pages,
            )
            if location:
                method_used = "bookmark"
                print(f"  ✓ found via bookmark")
            else:
                print(f"  ✗ no bookmark match; falling back to Layer 2")

            # Fall back to Layer 2
            if location is None:
                print(f"[Layer 2] Parsing TOC with LLM + footer-vote offset...")
                location = locate_via_toc(
                    pdf_path, args.chapter,
                    total_pages=total_pages,
                    model=args.model, base_url=args.base_url,
                    api_key=args.api_key,
                )
                if location:
                    method_used = location.source
                    print(f"  ✓ found via TOC ({location.source})")
                else:
                    print(f"  ✗ TOC parse failed or chapter not in TOC")

        if location is None:
            print(f"\nFAIL: could not locate '{args.chapter}'")
            print("Hints:")
            print("  - Run with --list to see available bookmarks/TOC")
            print("  - Pass --pages START-END (0-indexed) to slice manually")
            return 1

        print(f"\n=== Location ===")
        print(f"Chapter       : {location.chapter}")
        print(f"PDF pages     : {location.pdf_start}-{location.pdf_end} "
              f"(0-indexed, {location.page_count} pages)")
        if location.printed_start is not None:
            print(f"Printed pages : {location.printed_start}"
                  + (f"-{location.printed_end}" if location.printed_end else "")
                  + " (in招股书印刷页码)")
        print(f"Source        : {location.source}")
        print(f"Detail        : {location.method_detail}")

        # ----- Slice (改动 2) -----
        if args.slice:
            slice_path = slice_pdf(
                pdf_path, location, companies_root, cdir_name,
            )
            print(f"\n=== Slice ===")
            print(f"Wrote: {slice_path}")
            try:
                rel = slice_path.relative_to(repo_root).as_posix()
            except ValueError:
                rel = str(slice_path)
            print(f"  rel: {rel}")
            print(f"\nNext step: feed to Skill A or B with:")
            print(f"  python skills/hkex-pdf-reader-batch/scripts/batch_extract.py \\")
            print(f"    --company {args.company} --pdf \"{slice_path}\" \\")
            print(f"    --label \"{location.chapter}_p{location.pdf_start}-{location.pdf_end}\"")

            # Register in DB
            notes = {
                "source_pdf": str(pdf_path),
                "chapter": location.chapter,
                "pdf_pages": [location.pdf_start, location.pdf_end],
                "page_count": location.page_count,
                "locate_method": location.source,
                "slice_path": rel,
            }
            upsert_extraction(
                conn=conn,
                stock_code=args.company,
                field_name=FIELD_NAME,
                output_path=rel,
                extractor=EXTRACTOR_NAME,
                extracted_at=_utcnow_iso(),
                source_pdf_hash=None,
                content_sha256=None,
                notes=json.dumps(notes, ensure_ascii=False),
            )
            conn.commit()
            export_json(conn, data_root, repo_root,
                        source_label="hkex-chapter-locator")

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
