#!/usr/bin/env python3
"""通用文字场景 skill — 从 Markdown 抽业务摘要、风险、股东等结构化字段。

演化自 hkex-pdf-field-extractor (Skill C)。差异：
  1. 新增 summary 字段：业务概览、行业地位、历史与重组、募资用途摘要
  2. 完全 YAML 驱动（fields.yaml），不再硬编码 prompt
  3. 用 _common.common_llm 替代 openai SDK，支持多模型路由
  4. 仍可调用 Skill C 已有的字段（listing_type / issue_price_range 等），
     通过 --legacy-fields 标志开启

输入：
  --md        已转换的 MD（来自 Skill A 或 Skill B）
  --fields    fields.yaml
  --out-dir   输出目录（每个字段一个 JSON）

或公司模式：
  --company <code> [--source auto|precision|batch] [--source-file <path>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "_common"))
sys.path.insert(0, str(_HERE.parent.parent / "hkex-offering-tracker" / "scripts"))

import yaml  # type: ignore

from common_env import load_env  # noqa: E402
from common_llm import LLMConfig, chat_json, call_with_retry  # noqa: E402
from common import (  # type: ignore  # noqa: E402
    open_db, build_company_dir, upsert_extraction, export_json,
)


EXTRACTOR_NAME = "pdf_summary_v1"
MAX_MD_CHARS = 120_000


@dataclass
class FieldSpec:
    id: str
    name: str
    type: str  # scalar | list
    description: str
    prompt: str


def load_fields(yaml_path: Path) -> list[FieldSpec]:
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    out: list[FieldSpec] = []
    for section in ("scalars", "lists"):
        for f in data.get(section, []) or []:
            out.append(FieldSpec(
                id=f["id"], name=f.get("name", f["id"]),
                type=f.get("type", section.rstrip("s")),
                description=f.get("description", ""),
                prompt=f["prompt"],
            ))
    return out


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _pick_longest_md(d: Path) -> Path | None:
    if not d.is_dir():
        return None
    cands = list(d.glob("*.md"))
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_size)


def _resolve_md_for_company(
    companies_root: Path, company_dir: str, prefer: str,
) -> tuple[str | None, str | None, Path | None]:
    cdir = companies_root / company_dir
    precision_md = _pick_longest_md(cdir / "info" / "precision")
    batch_md = _pick_longest_md(cdir / "info")
    if prefer == "precision" and precision_md:
        return precision_md.read_text(encoding="utf-8"), "precision", precision_md
    if prefer == "batch" and batch_md:
        return batch_md.read_text(encoding="utf-8"), "batch", batch_md
    # auto
    if precision_md:
        return precision_md.read_text(encoding="utf-8"), "precision", precision_md
    if batch_md:
        return batch_md.read_text(encoding="utf-8"), "batch", batch_md
    return None, None, None


def extract_field(
    cfg: LLMConfig, field: FieldSpec, md: str,
) -> tuple[object, str | None]:
    """抽一个字段。返回 (value, error)。"""
    snippet = md[:MAX_MD_CHARS] if len(md) > MAX_MD_CHARS else md
    sys_prompt = (
        "你是港股 IPO 分析助手。严格按 JSON 格式输出，不要 markdown 代码块标记，"
        "不要任何额外文字。"
    )
    user = field.prompt.replace("{markdown}", snippet)
    try:
        parsed, raw = call_with_retry(
            lambda: chat_json(cfg, sys_prompt, user),
            retries=2, label=f"field:{field.id}",
        )
    except Exception as e:
        return None, f"LLM call failed: {e}"
    if not isinstance(parsed, dict):
        return None, f"response not JSON: {raw[:200]!r}"
    if field.id not in parsed:
        return None, f"response missing key {field.id!r}: {raw[:200]!r}"
    return parsed[field.id], None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path, default=None)
    ap.add_argument("--company", type=str, default=None,
                    help="stock_code (公司模式)")
    ap.add_argument("--md", type=Path, default=None,
                    help="直接指定 markdown 文件（绕过公司模式）")
    ap.add_argument("--source", choices=["auto", "precision", "batch"],
                    default="auto")
    ap.add_argument("--source-file", type=Path, default=None,
                    help="直接指定 markdown 路径（与 --md 等效，与 Skill C 兼容）")
    ap.add_argument("--fields-yaml", type=Path, default=None,
                    help="字段配置（默认用本 skill 目录下 fields.yaml）")
    ap.add_argument("--only", type=str, default=None,
                    help="只抽指定字段（逗号分隔）")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="输出目录（默认 info/summary/ 或当前目录）")
    ap.add_argument("--model", type=str, default=None)
    ap.add_argument("--base-url", type=str, default=None)
    ap.add_argument("--api-key", type=str, default=None)
    ap.add_argument("--list", action="store_true",
                    help="列出所有字段并退出")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    fields_yaml = args.fields_yaml or _HERE.parent / "fields.yaml"
    fields = load_fields(fields_yaml)

    if args.list:
        for f in fields:
            print(f"  {f.id:32s}  ({f.type:6s})  {f.name}")
        return 0

    if args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        fields = [f for f in fields if f.id in wanted]
        if not fields:
            print(f"ERROR: no fields matched --only {args.only}", file=sys.stderr)
            return 2

    repo_root = load_env()
    data_root = (args.data_dir or repo_root / "data").resolve()
    cfg = LLMConfig.from_env(
        model=args.model, base_url=args.base_url, api_key=args.api_key,
    )
    if not cfg.api_key:
        print("ERROR: LLM_API_KEY required", file=sys.stderr)
        return 2

    # Resolve markdown source
    company_code: str | None = None
    if args.md is not None:
        md_text = args.md.read_text(encoding="utf-8")
        md_label = f"file:{args.md.name}"
        out_dir = args.out_dir or args.md.parent / "summary"
    elif args.source_file is not None:
        sf = args.source_file
        if not sf.is_absolute():
            cand = (repo_root / sf).resolve()
            sf = cand if cand.is_file() else sf.resolve()
        md_text = sf.read_text(encoding="utf-8")
        md_label = f"file:{sf.name}"
        out_dir = args.out_dir or sf.parent / "summary"
    elif args.company:
        company_code = args.company
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
            cdir = build_company_dir(code, name)
            md_text, md_label, _ = _resolve_md_for_company(
                data_root / "companies", cdir, args.source,
            )
            if md_text is None:
                print(
                    f"ERROR: no markdown found for {code}. "
                    f"Run Skill A or B first.", file=sys.stderr,
                )
                return 2
            out_dir = args.out_dir or (data_root / "companies" / cdir / "info" / "summary")
        finally:
            conn.close()
    else:
        print("ERROR: --md or --company or --source-file required", file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Source: {md_label} ({len(md_text)} chars)")
    print(f"Model:  {cfg.model}")
    print(f"Fields: {[f.id for f in fields]}")
    print(f"OutDir: {out_dir}")

    if args.dry_run:
        return 0

    # Open DB if company mode
    conn = None
    if company_code:
        conn = open_db(data_root / "state.db")

    succeeded = 0
    failed = 0
    try:
        for f in fields:
            print(f"\n[extract] {f.id} ...", file=sys.stderr)
            value, err = extract_field(cfg, f, md_text)
            if err:
                print(f"  FAIL {f.id}: {err}", file=sys.stderr)
                failed += 1
                continue
            succeeded += 1
            payload = {
                "field_name": f.id,
                "display_name": f.name,
                "type": f.type,
                "value": value,
                "extracted_at": _utcnow_iso(),
                "source_label": md_label,
                "model": cfg.model,
            }
            out_path = out_dir / f"{f.id}.json"
            out_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                rel = out_path.relative_to(repo_root).as_posix()
            except ValueError:
                rel = str(out_path)
            print(f"  OK {f.id} -> {rel}")

            if conn and company_code:
                sha = _sha(json.dumps(value, ensure_ascii=False, sort_keys=True))
                upsert_extraction(
                    conn=conn, stock_code=company_code,
                    field_name=f.id, output_path=rel,
                    extractor=EXTRACTOR_NAME,
                    extracted_at=_utcnow_iso(),
                    source_pdf_hash=None, content_sha256=sha,
                    notes=f"model={cfg.model}; source={md_label}",
                )
                conn.commit()

        if conn and company_code:
            export_json(conn, data_root, repo_root,
                        source_label=EXTRACTOR_NAME)
    finally:
        if conn:
            conn.close()

    print(f"\nSummary: {succeeded} ok, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
