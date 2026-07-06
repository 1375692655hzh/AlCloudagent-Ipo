#!/usr/bin/env python3
"""配发结果公告分配基准表 skill — 主入口。

流水线（双源校验）：
  1. 调 Skill B（hkex-pdf-reader-precision）的 MinerU vlm 模式 提取 MD
  2. extract_fields.py 从 MD 抽 scalars + tables（HTML table parser）
  3. verify_vision.py 用 doubao vision 第二源识图（scalars + tables）
  4. compare.py 双源比对
  5. business_checks.py 业务规则校验
  6. render_output.py 渲染最终 MD + 校验报告

用法：
    python skills/hkex-allotment-basis/scripts/parse_allotment.py \
        --company <code> [--pdf <file>] [--out-dir <path>]

    # 不走公司目录，直接对任意 PDF 处理：
    python skills/hkex-allotment-basis/scripts/parse_allotment.py \
        --pdf-path /path/to/allotment.pdf \
        --out-dir /path/to/output

可跳过某步骤（开发调试）：--skip mineru|verify|compare|business|render
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
sys.path.insert(0, str(_REPO / "skills" / "_common"))

from common_env import load_env  # noqa: E402

_EXTRACTOR_NAME = "allotment_v1"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _run(cmd: list[str], *, env: dict | None = None) -> int:
    """Run a subprocess, streaming output to current stdout/stderr."""
    print(f"\n$ {' '.join(str(c) for c in cmd)}", file=sys.stderr)
    return subprocess.call(cmd, env=env or os.environ.copy())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--company", type=str, default=None,
                    help="stock_code (走公司目录模式)")
    ap.add_argument("--pdf", type=str, default=None,
                    help="PDF filename under docs/ (use with --company)")
    ap.add_argument("--pdf-path", type=Path, default=None,
                    help="Arbitrary PDF path (bypass company mode)")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Output directory (bypass company mode)")
    ap.add_argument("--data-dir", type=Path, default=None)
    ap.add_argument("--skip", nargs="*", default=[],
                    choices=["mineru", "verify", "compare", "business", "render"],
                    help="Skip step(s)")
    ap.add_argument("--no-tables-vision", action="store_true",
                    help="Skip table extraction in vision verify (only scalars)")
    ap.add_argument("--mineru-model", choices=["pipeline", "vlm"], default="vlm",
                    help="MinerU model (default vlm, better for nested headers)")
    args = ap.parse_args()

    repo_root = load_env()
    data_root = (args.data_dir or repo_root / "data").resolve()

    # Resolve PDF & out dir based on mode
    if args.pdf_path is not None:
        # Arbitrary PDF mode
        if not args.pdf_path.is_file():
            print(f"ERROR: {args.pdf_path} not found", file=sys.stderr)
            return 2
        pdf_path = args.pdf_path.resolve()
        out_dir = (args.out_dir or pdf_path.parent / "_allotment_full").resolve()
        company_code = None
    else:
        # Company mode
        if not args.company:
            print("ERROR: --company or --pdf-path required", file=sys.stderr)
            return 2
        sys.path.insert(0, str(_REPO / "skills" / "hkex-offering-tracker" / "scripts"))
        from common import open_db, build_company_dir  # type: ignore
        db_path = data_root / "state.db"
        conn = open_db(db_path)
        try:
            row = conn.execute(
                "SELECT stock_code, company_name FROM companies WHERE stock_code = ?",
                (args.company,),
            ).fetchone()
            if row is None:
                print(f"ERROR: company {args.company} not in DB", file=sys.stderr)
                return 2
            code, name = row
            company_code = code
            cdir = data_root / "companies" / build_company_dir(code, name)
            docs_dir = cdir / "docs"
            if args.pdf:
                pdf_path = (docs_dir / args.pdf).resolve()
            else:
                # Find an allotment-like PDF
                cands = sorted(docs_dir.glob("*.pdf"))
                allot_cands = [p for p in cands
                               if any(k in p.name for k in
                                      ["配售结果", "配发结果", "分配结果", "公布"])]
                pdf_path = (allot_cands or cands)[0]
            if not pdf_path.is_file():
                print(f"ERROR: PDF {pdf_path} not found", file=sys.stderr)
                return 2
            out_dir = (args.out_dir or cdir / "info" / "allotment_full").resolve()
        finally:
            conn.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"PDF:     {pdf_path}")
    print(f"OutDir:  {out_dir}")
    print(f"Model:   mineru={args.mineru_model}")

    stem = pdf_path.stem
    mineru_md = out_dir / "mineru" / f"{stem}.md"
    fields_json = out_dir / "fields.json"
    verify_json = out_dir / "verify.json"
    compare_json = out_dir / "compare.json"
    business_json = out_dir / "business.json"
    final_md = out_dir / f"{stem}.md"
    report_md = out_dir / "校验报告.md"

    # Step 1: MinerU (调 Skill B 的 precision_extract.py)
    if "mineru" not in args.skip:
        print("\n=== Step 1/5  MinerU 提取 (调 Skill B) ===",
              file=sys.stderr)
        if args.company:
            cmd = [
                sys.executable,
                str(_REPO / "skills" / "hkex-pdf-reader-precision" / "scripts" / "precision_extract.py"),
                "--company", args.company,
                "--pdf", str(pdf_path),
                "--label", stem,
                "--model", args.mineru_model,
                "--data-dir", str(data_root),
            ]
        else:
            # Standalone mode: Skill B requires --company; we just run mineru_client directly
            cmd = [
                sys.executable,
                str(_HERE / "_run_mineru_standalone.py"),
                "--pdf", str(pdf_path),
                "--out", str(mineru_md),
                "--model", args.mineru_model,
            ]
        rc = _run(cmd)
        if rc != 0:
            print(f"MinerU failed (exit {rc})", file=sys.stderr)
            return rc
        # In --company mode, Skill B writes to info/precision/<stem>.md
        if args.company:
            sys.path.insert(0, str(_REPO / "skills" / "hkex-offering-tracker" / "scripts"))
            from common import build_company_dir as _bcd  # type: ignore
            actual = (data_root / "companies" / _bcd(company_code or args.company, "")
                      / "info" / "precision" / f"{stem}.md")
            if not actual.is_file():
                # fallback: search
                cands = list((data_root / "companies").rglob(f"{stem}.md"))
                if cands:
                    actual = cands[0]
            if actual.is_file():
                mineru_md.parent.mkdir(parents=True, exist_ok=True)
                mineru_md.write_text(actual.read_text(encoding="utf-8"), encoding="utf-8")

    if not mineru_md.is_file():
        print(f"ERROR: MinerU output {mineru_md} not found", file=sys.stderr)
        return 2

    # Step 2: extract_fields.py
    print("\n=== Step 2/5  字段抽取 (extract_fields.py) ===", file=sys.stderr)
    rc = _run([
        sys.executable, str(_HERE / "extract_fields.py"),
        "--md", str(mineru_md),
        "--fields", str(_HERE.parent / "fields.yaml"),
        "--out", str(fields_json),
    ])
    if rc != 0:
        return rc

    # Step 3: verify_vision.py
    if "verify" not in args.skip:
        print("\n=== Step 3/5  doubao vision 校验 (verify_vision.py) ===",
              file=sys.stderr)
        cmd = [
            sys.executable, str(_HERE / "verify_vision.py"),
            "--pdf", str(pdf_path),
            "--out", str(verify_json),
        ]
        if args.no_tables_vision:
            cmd.append("--no-tables")
        _run(cmd)  # non-fatal

    # Step 4: compare.py
    if "compare" not in args.skip and verify_json.is_file():
        print("\n=== Step 4a/5  双源比对 (compare.py) ===", file=sys.stderr)
        _run([
            sys.executable, str(_HERE / "compare.py"),
            "--primary", str(fields_json),
            "--verify", str(verify_json),
            "--out", str(compare_json),
        ])

    # Step 4b: business_checks.py
    if "business" not in args.skip:
        print("\n=== Step 4b/5  业务规则校验 (business_checks.py) ===",
              file=sys.stderr)
        _run([
            sys.executable, str(_HERE / "business_checks.py"),
            "--fields", str(fields_json),
            "--out", str(business_json),
        ])

    # Step 5: render_output.py
    if "render" not in args.skip:
        print("\n=== Step 5/5  渲染最终输出 (render_output.py) ===",
              file=sys.stderr)
        cmd = [
            sys.executable, str(_HERE / "render_output.py"),
            "--mineru-md", str(mineru_md),
            "--fields", str(fields_json),
            "--out", str(final_md),
            "--report", str(report_md),
        ]
        if compare_json.is_file():
            cmd += ["--compare", str(compare_json)]
        if business_json.is_file():
            cmd += ["--business", str(business_json)]
        rc = _run(cmd)
        if rc != 0:
            return rc

    # Register in DB if company mode
    if company_code:
        try:
            sys.path.insert(0, str(_REPO / "skills" / "hkex-offering-tracker" / "scripts"))
            from common import open_db, upsert_extraction, export_json  # type: ignore
            db_path = data_root / "state.db"
            conn = open_db(db_path)
            try:
                rel = final_md.relative_to(repo_root).as_posix()
            except ValueError:
                rel = str(final_md)
            sha = str(hash(final_md.read_text(encoding="utf-8")))
            upsert_extraction(
                conn=conn, stock_code=company_code,
                field_name="allotment_full",
                output_path=rel, extractor=_EXTRACTOR_NAME,
                extracted_at=_utcnow_iso(),
                source_pdf_hash=None, content_sha256=sha,
                notes=f"pdf={pdf_path.name}; mineru={args.mineru_model}",
            )
            conn.commit()
            export_json(conn, data_root, repo_root,
                        source_label=_EXTRACTOR_NAME)
            conn.close()
        except Exception as e:
            print(f"WARN: DB registration skipped: {e}", file=sys.stderr)

    print("\n=== 完成 ===", file=sys.stderr)
    print(f"  最终 MD:   {final_md}")
    print(f"  校验报告:  {report_md}")
    print(f"  原始数据:  {out_dir}/{{fields,verify,compare,business}}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
