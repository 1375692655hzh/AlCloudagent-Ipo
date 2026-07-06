#!/usr/bin/env python3
"""PDF 精准读取工具（Skill B）—— MinerU 精准 API 引擎。

把 companies/<code>_<name>/docs/*.pdf 用 MinerU pipeline 模型转换，
输出到 info/precision/<pdf_stem>.md，注册到 extractions 表
（extractor='mineru_pipeline_v1'）。

设计原则：
  - 高价值、小批量（用户主动触发，不自动批量）
  - 全程 pipeline 模型（**永不 vlm**，避免财务数字幻觉）
  - 招股书 > 200 页自动分段提交，下载后拼接
  - 必须配置 MINERU_TOKEN 环境变量或 ~/.mineru/config.yaml
  - PDF 上传到 mineru.net OSS（公开文件可接受，但需在 SKILL.md 写明）

适用场景：
  - 单家公司深度分析
  - 财务报表表格高保真抽取
  - 配发结果精读（中签数字关键）
  - 用户明确要求"高精度"

不适用场景（请用 Skill A `hkex-pdf-reader-batch`）：
  - 全库批量入库（烧额度）
  - 历史回填（量大优先 MarkItDown）

Usage:
    python skills/hkex-pdf-reader-precision/scripts/precision_extract.py [opts]

Options:
    --data-dir DIR        Override data directory (default <repo>/data)
    --company <code>      必填：仅处理这一家公司
    --pdf <filename|path> 可选：仅处理这一份 PDF
                          - 若为相对名（如 "招股書.pdf"），从 docs/ 下查找
                          - 若为绝对路径或存在路径，直接处理该文件
                          （用于 hkex-chapter-locator 切出的子 PDF）
    --label <text>        可选：覆盖输出 .md 文件名（默认沿用 PDF stem）
                          例：--label "招股書_p211-290_財務資料"
    --token <token>       MinerU token（默认从 $MINERU_TOKEN 或 ~/.mineru/ 读）
    --page-chunk N        分段大小（默认 200，按官方上限）
    --language ch         文档语言（默认 ch，繁简混合）
    --model pipeline|vlm  MinerU 模型（默认 pipeline；配发结果/复杂表格可用 vlm）
    --dry-run             列出处理计划，不调用 API
    --no-upload           不上传，只探测页数并打印分段方案
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_OFFERING_SCRIPTS = _HERE.parent.parent / "hkex-offering-tracker" / "scripts"
sys.path.insert(0, str(_OFFERING_SCRIPTS))

from common import (  # noqa: E402
    open_db,
    export_json,
    upsert_extraction,
    build_company_dir,
)

from mineru_client import (  # noqa: E402
    MinerUClient,
    MinerUError,
    MinerUQuotaError,
    MinerULimitError,
    compute_page_ranges,
)

try:
    from pypdf import PdfReader as PypdfReader
except ImportError:  # pragma: no cover
    PypdfReader = None  # _count_pages degrades gracefully


EXTRACTOR_NAME = "mineru_pipeline_v1"
FIELD_NAME = "markdown_precision"
DEFAULT_CHUNK = 200  # 官方上限 200 页
VALID_MODELS = ("pipeline", "vlm")


# ---------------------------------------------------------------------------
# Data records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrecisionJob:
    stock_code: str
    company_name: str
    company_dir: str
    pdf_path: Path
    pdf_stem: str
    pdf_url_hash: str
    pdf_size: int
    total_pages: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _count_pages(pdf_path: Path) -> int | None:
    if PypdfReader is None:
        return None
    try:
        return len(PypdfReader(str(pdf_path), strict=False).pages)
    except Exception:
        return None


def _read_md_segments(dest_dir: Path, segment_count: int, pdf_stem: str) -> str:
    """Concatenate per-segment full.md into one Markdown string.

    Each segment downloads its zip into dest_dir/seg_<i>/; we concat
    full.md from each in order. Page headers/footers de-dup is left to
    the downstream LLM (Skill C) — we keep raw concat for fidelity.
    """
    parts: list[str] = []
    for i in range(segment_count):
        seg_dir = dest_dir / f"seg_{i}"
        md = seg_dir / "full.md"
        if not md.is_file():
            # Fallback: any .md in seg dir
            cands = list(seg_dir.rglob("*.md"))
            if not cands:
                raise MinerUError(f"Segment {i} markdown missing at {seg_dir}")
            md = cands[0]
        parts.append(f"\n\n<!-- === segment {i} === -->\n\n")
        parts.append(md.read_text(encoding="utf-8"))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Single-PDF pipeline
# ---------------------------------------------------------------------------


def process_one_pdf(
    client: MinerUClient,
    job: PrecisionJob,
    companies_root: Path,
    repo_root: Path,
    *,
    page_chunk: int,
    language: str,
    model_version: str = "pipeline",
) -> tuple[str, str, int]:
    """Process one PDF. Returns (rel_path, content_sha256, char_count).

    Raises MinerUError on failure.
    """
    precision_dir = companies_root / job.company_dir / "info" / "precision"
    work_dir = precision_dir / f"_work_{job.pdf_stem}"
    work_dir.mkdir(parents=True, exist_ok=True)

    pages = job.total_pages
    if pages is None or pages <= page_chunk:
        # 单段提交
        ranges = [None] if pages is None else [f"0-{pages - 1}"]
    else:
        ranges = compute_page_ranges(pages, page_chunk)
    seg_count = len(ranges)
    print(
        f"  {job.pdf_stem}: {pages} pages -> {seg_count} segment(s) "
        f"{ranges} (model={model_version})", file=sys.stderr
    )

    # 1. 申请 batch 上传 URL（一次 batch 包含所有分段）
    files = [(job.pdf_path.name, pr) for pr in ranges]
    subs = client.request_upload_urls(files, language=language, model_version=model_version)

    # 2. 上传（每段都上传整个文件，让 server 按 page_range 处理）
    #    MinerU OSS PUT 是幂等的，同一文件多次上传 OK
    for sub in subs:
        client.upload_file(sub, job.pdf_path)

    # 3. 轮询直到全部完成
    def cb(results, elapsed):
        states = {}
        for r in results:
            states[r.state] = states.get(r.state, 0) + 1
        print(f"  [{int(elapsed)}s] {job.pdf_stem}: {states}", file=sys.stderr)

    results = client.poll_until_done(subs[0].batch_id, progress_cb=cb)

    # 4. 检查失败
    for r in results:
        if r.state == "failed":
            raise MinerUFailedTask(
                f"{job.pdf_stem} segment failed: {r.err_msg}"
            )

    # 5. 下载每段 zip 到 work_dir/seg_<i>/
    for i, r in enumerate(results):
        client.download_markdown(r.full_zip_url, work_dir / f"seg_{i}")

    # 6. 拼接所有段
    text = _read_md_segments(work_dir, seg_count, job.pdf_stem)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    # 7. 写最终 .md（命名带 .precision 后缀避免与 Skill A 输出冲突）
    out_md = precision_dir / f"{job.pdf_stem}.md"
    out_md.write_text(text, encoding="utf-8")
    # 清理 work 目录（可选；保留用于 debug）
    # import shutil; shutil.rmtree(work_dir, ignore_errors=True)

    try:
        rel_path = out_md.relative_to(repo_root).as_posix()
    except ValueError:
        rel_path = str(out_md)
    return rel_path, sha, len(text)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--company", type=str, required=True,
                        help="stock_code to process (required)")
    parser.add_argument("--pdf", type=str, default=None,
                        help="PDF filename (under docs/) OR absolute path; "
                             "default = all PDFs for this company")
    parser.add_argument("--label", type=str, default=None,
                        help="Override output .md filename stem "
                             "(use with --pdf absolute path)")
    parser.add_argument("--token", type=str, default=None,
                        help="MinerU API token (default: $MINERU_TOKEN or ~/.mineru/)")
    parser.add_argument("--page-chunk", type=int, default=DEFAULT_CHUNK,
                        help=f"Page chunk size for splitting (default {DEFAULT_CHUNK})")
    parser.add_argument("--language", type=str, default="ch",
                        help="Document language (default ch)")
    parser.add_argument("--model", choices=VALID_MODELS, default="pipeline",
                        help="MinerU model (default: pipeline; "
                             "vlm for配发结果/复杂嵌套表格)")
    parser.add_argument("--dry-run", action="store_true",
                        help="List plan, do not call API")
    parser.add_argument("--no-upload", action="store_true",
                        help="Probe only; print segmentation plan, no API call")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[3]
    data_root = (args.data_dir or repo_root / "data").resolve()
    companies_root = data_root / "companies"
    db_path = data_root / "state.db"

    print(f"Repo root : {repo_root}")
    print(f"Data root : {data_root}")

    if not db_path.exists():
        print(f"ERROR: {db_path} does not exist.")
        return 2

    conn = open_db(db_path)
    try:
        # 解析公司
        row = conn.execute(
            "SELECT stock_code, company_name FROM companies WHERE stock_code = ?",
            (args.company,),
        ).fetchone()
        if row is None:
            print(f"ERROR: company {args.company} not in DB")
            return 2
        code, name = row
        cdir = companies_root / build_company_dir(code, name)
        docs_dir = cdir / "docs"
        if not docs_dir.is_dir():
            print(f"ERROR: {docs_dir} does not exist")
            return 2

        # 收集 PDFs
        if args.pdf:
            pdf_arg = Path(args.pdf)
            # If it's an existing path (absolute or relative-to-cwd), use directly.
            # Otherwise treat as a filename under docs/.
            if pdf_arg.exists():
                pdfs = [pdf_arg.resolve()]
                if not pdfs[0].is_file():
                    print(f"ERROR: {pdfs[0]} not a file")
                    return 2
            else:
                pdfs = [docs_dir / args.pdf]
                if not pdfs[0].is_file():
                    print(f"ERROR: {pdfs[0]} not found")
                    return 2
        else:
            pdfs = sorted(docs_dir.glob("*.pdf"))

        if not pdfs:
            print(f"No PDFs to process under {docs_dir}")
            return 0

        # url_hash 查表
        path_to_hash = {
            Path(r[1]).name: r[0]
            for r in conn.execute(
                "SELECT url_hash, local_path FROM ipo_documents WHERE stock_code = ?",
                (code,),
            ).fetchall()
            if r[1]
        }

        jobs: list[PrecisionJob] = []
        for p in pdfs:
            uh = path_to_hash.get(p.name) or hashlib.sha256(
                str(p).encode("utf-8")).hexdigest()[:32]
            pages = _count_pages(p)
            # Use --label to override output stem (only meaningful with single --pdf)
            stem = args.label if (args.label and len(pdfs) == 1) else p.stem
            jobs.append(PrecisionJob(
                stock_code=code, company_name=name,
                company_dir=build_company_dir(code, name),
                pdf_path=p, pdf_stem=stem,
                pdf_url_hash=uh, pdf_size=p.stat().st_size,
                total_pages=pages or 0,
            ))

        # 打印计划
        print(f"\nPlan for {code} {name}: {len(jobs)} PDF(s)")
        for j in jobs:
            size_mb = j.pdf_size / (1024 * 1024)
            n_seg = 1
            if j.total_pages and j.total_pages > args.page_chunk:
                n_seg = len(compute_page_ranges(j.total_pages, args.page_chunk))
            print(f"  {j.pdf_stem} | {size_mb:.1f}MB | "
                  f"{j.total_pages or '?'} pages | {n_seg} segment(s)")

        if args.dry_run or args.no_upload:
            return 0

        # 准备 client
        try:
            client = MinerUClient(token=args.token)
        except Exception as exc:
            print(f"ERROR: token setup failed: {exc}", file=sys.stderr)
            return 2

        # 顺序处理（不并发：避免触发 IP 限频 / 额度耗尽）
        succeeded = 0
        failed = 0
        model_version_for_jobs = args.model
        for j in jobs:
            try:
                rel_path, sha, char_count = process_one_pdf(
                    client, j, companies_root, repo_root,
                    page_chunk=args.page_chunk, language=args.language,
                    model_version=args.model,
                )
                extractor_label = (
                    EXTRACTOR_NAME if model_version_for_jobs == "pipeline"
                    else EXTRACTOR_NAME.replace("pipeline", "vlm")
                )
                upsert_extraction(
                    conn=conn,
                    stock_code=j.stock_code,
                    field_name=FIELD_NAME,
                    output_path=rel_path,
                    extractor=extractor_label,
                    extracted_at=_utcnow_iso(),
                    source_pdf_hash=j.pdf_url_hash,
                    content_sha256=sha,
                    notes=(
                        f"model={model_version_for_jobs}; "
                        f"pages={j.total_pages}, size={j.pdf_size}, "
                        f"chars={char_count}"
                    ),
                )
                conn.commit()
                succeeded += 1
                print(f"OK    {j.stock_code} {j.pdf_stem} -> {rel_path}")
            except MinerUQuotaError as exc:
                print(f"QUOTA {j.pdf_stem}: {exc}", file=sys.stderr)
                print("Stopping: daily quota exhausted. Try tomorrow.",
                      file=sys.stderr)
                failed += 1
                break
            except MinerULimitError as exc:
                print(f"LIMIT {j.pdf_stem}: {exc}", file=sys.stderr)
                failed += 1
                continue
            except MinerUError as exc:
                print(f"FAIL  {j.pdf_stem}: {exc}", file=sys.stderr)
                failed += 1
                continue

        export_json(conn, data_root, repo_root,
                    source_label="hkex-pdf-reader-precision")
        print(f"\nSummary: {succeeded} OK, {failed} failed")
        return 0 if failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
