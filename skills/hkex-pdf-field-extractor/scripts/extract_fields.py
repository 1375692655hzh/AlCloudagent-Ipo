#!/usr/bin/env python3
"""字段抽取工具（Skill C）—— LLM 引擎。

从已转换的 Markdown（Skill A 或 Skill B 产出）抽取结构化字段，
写到 info/<field>.json，注册到 extractions 表（extractor='pdf_field_v1'）。

设计原则：
  - 优先读 info/precision/<stem>.md（Skill B，高精度）
  - 回退到 info/<stem>.md（Skill A，标准）
  - 按字段单独提问（不是整本总结），降低幻觉
  - 数值字段做正则/范围校验，异常标记 needs_review
  - listing_type / confirmed_name 抽到后反向 UPDATE companies 表

适用场景：
  - 抽招股价、募资用途、基石、主要股东
  - 补 listing_type、confirmed_name
  - 全库字段批量补全

Usage:
    python skills/hkex-pdf-field-extractor/scripts/extract_fields.py [opts]

Options:
    --data-dir DIR        Override data directory (default <repo>/data)
    --company <code>      必填：处理这一家
    --fields a,b,c        指定字段（默认全部）；用 --list 看全集
    --source precision|batch|auto   数据源选择（默认 auto：precision 优先）
    --source-file <path>  直接指定 markdown 文件路径（绕过 --source 自动选择）
                          用于读取 hkex-chapter-locator 切出的章节 markdown
                          例：--source-file info/precision/招股書_p211-290_財務資料.md
    --model <name>        LLM 模型名（默认从环境变量读）
    --base-url <url>      OpenAI-compatible API base（默认从环境变量读）
    --api-key <key>       API key（默认从环境变量读）
    --list                列出所有可用字段并退出
    --dry-run             打印计划不调 LLM
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_OFFERING_SCRIPTS = _HERE.parent.parent / "hkex-offering-tracker" / "scripts"
sys.path.insert(0, str(_OFFERING_SCRIPTS))
sys.path.insert(0, str(_HERE))

from common import (  # noqa: E402
    open_db,
    export_json,
    upsert_extraction,
    build_company_dir,
)
from field_dictionary import (  # noqa: E402
    FIELDS,
    COMPANIES_TABLE_FIELDS,
    FieldDef,
)

try:
    from openai import OpenAI  # OpenAI-compatible (GLM, MiniMax, Claude-compat, ...)
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]


EXTRACTOR_NAME = "pdf_field_v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _read_md_for_company(
    companies_root: Path,
    company_dir: str,
    *,
    prefer: str = "auto",
) -> tuple[str | None, str | None, str | None]:
    """Return (markdown_text, source_label, abs_md_path).

    prefer='auto' (default):
      - If info/precision/*.md exists for any docs/*.pdf, use the longest one
      - Else if info/*.md exists, use the longest one
      - Else None
    prefer='precision': only precision
    prefer='batch': only batch
    """
    cdir = companies_root / company_dir
    precision_md = _pick_longest_md(cdir / "info" / "precision")
    batch_md = _pick_longest_md(cdir / "info")

    if prefer == "precision":
        if precision_md is None:
            return None, None, None
        return precision_md.read_text(encoding="utf-8"), "precision", precision_md
    if prefer == "batch":
        if batch_md is None:
            return None, None, None
        return batch_md.read_text(encoding="utf-8"), "batch", batch_md
    # auto
    if precision_md is not None:
        return precision_md.read_text(encoding="utf-8"), "precision", precision_md
    if batch_md is not None:
        return batch_md.read_text(encoding="utf-8"), "batch", batch_md
    return None, None, None


def _pick_longest_md(d: Path) -> Path | None:
    """Pick the largest .md file in directory (heuristic: most content)."""
    if not d.is_dir():
        return None
    cands = list(d.glob("*.md"))
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_size)


def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


# Allow up to ~120k chars of markdown (≈ 30-40k tokens for Chinese).
# Larger招股书 will be head-truncated; LLM抽取主要靠前几章 + 财务摘要.
MAX_MD_CHARS = 120_000


def call_llm(
    client,
    model: str,
    field: FieldDef,
    markdown: str,
) -> tuple[object, str | None]:
    """Return (raw_value, error_msg). raw_value may be dict/list/str/None.

    error_msg is set if LLM call failed or output didn't parse as JSON.
    """
    md = markdown[:MAX_MD_CHARS] if len(markdown) > MAX_MD_CHARS else markdown
    user_msg = field.prompt.format(markdown=md)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system",
                 "content": "你是港股 IPO 分析助手。严格按 JSON 格式输出，不要加任何额外文字。"},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.0,  # deterministic
        )
    except Exception as exc:
        return None, f"LLM call failed: {exc}"
    text = resp.choices[0].message.content or ""
    return _extract_field_from_response(text, field.name)


def _extract_field_from_response(text: str, field_name: str) -> tuple[object, str | None]:
    """Parse LLM response into (value, error). Tries to find {field_name: ...}."""
    # Strip code fences if any
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Remove ```json ... ``` wrapper
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback: regex search for {"<field>": ...}
        m = re.search(
            rf'\{{\s*["\']?{re.escape(field_name)}["\']?\s*:\s*(.+?)\}}',
            text, re.DOTALL,
        )
        if not m:
            return None, f"response not JSON: {text[:200]!r}"
        try:
            value = json.loads(m.group(1).rstrip(","))
            return {field_name: value}, None
        except json.JSONDecodeError:
            return None, f"response JSON malformed: {text[:200]!r}"
    if not isinstance(obj, dict) or field_name not in obj:
        return None, f"response missing key {field_name!r}: {text[:200]!r}"
    return obj[field_name], None


# ---------------------------------------------------------------------------
# Per-field pipeline
# ---------------------------------------------------------------------------


@dataclass
class FieldResult:
    field_name: str
    value: object
    needs_review: bool
    notes: str
    rel_path: str | None  # path to info/<field>.json, None on failure


def process_field(
    field: FieldDef,
    markdown: str,
    source_label: str,
    *,
    client,
    model: str,
    info_dir: Path,
    repo_root: Path,
) -> FieldResult:
    value, err = call_llm(client, model, field, markdown)
    notes_parts = [f"source={source_label}", f"model={model}"]
    needs_review = False

    if err is not None:
        notes_parts.append(f"error={err}")
        needs_review = True
        return FieldResult(
            field_name=field.name, value=None,
            needs_review=needs_review, notes="; ".join(notes_parts),
            rel_path=None,
        )

    # Run validators
    validation_errors = []
    for v in field.validators:
        e = v(value)
        if e:
            validation_errors.append(e)
    if validation_errors:
        notes_parts.append("validation_failed: " + "; ".join(validation_errors))
        needs_review = True
    else:
        notes_parts.append("validation_ok")

    # Write JSON file
    info_dir.mkdir(parents=True, exist_ok=True)
    out_path = info_dir / f"{field.name}.json"
    payload = {
        "field_name": field.name,
        "value": value,
        "extracted_at": _utcnow_iso(),
        "source_label": source_label,
        "model": model,
        "needs_review": needs_review,
        "validation_errors": validation_errors,
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:
        rel_path = out_path.relative_to(repo_root).as_posix()
    except ValueError:
        rel_path = str(out_path)

    return FieldResult(
        field_name=field.name, value=value,
        needs_review=needs_review, notes="; ".join(notes_parts),
        rel_path=rel_path,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--company", type=str, default=None,
                        help="stock_code to process")
    parser.add_argument("--fields", type=str, default=None,
                        help="Comma-separated field names; default = all")
    parser.add_argument("--source", choices=["auto", "precision", "batch"],
                        default="auto",
                        help="Which markdown source (default: auto = precision first)")
    parser.add_argument("--source-file", type=Path, default=None,
                        help="Direct path to a markdown file (bypass --source). "
                             "Use with chapter-slice markdown from hkex-chapter-locator.")
    parser.add_argument("--model", type=str,
                        default=os.environ.get("LLM_MODEL", "glm-5.2"),
                        help="LLM model name (default: $LLM_MODEL or 'glm-5.2')")
    parser.add_argument("--base-url", type=str,
                        default=os.environ.get("LLM_BASE_URL"),
                        help="OpenAI-compatible API base URL (default: $LLM_BASE_URL)")
    parser.add_argument("--api-key", type=str,
                        default=os.environ.get("LLM_API_KEY"),
                        help="LLM API key (default: $LLM_API_KEY)")
    parser.add_argument("--list", action="store_true",
                        help="List available fields and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan, don't call LLM")
    args = parser.parse_args()

    if args.list:
        for name, f in FIELDS.items():
            print(f"{name:24s}  {f.description_zh}  (source={f.priority_source})")
        return 0

    if not args.company:
        print("ERROR: --company is required (or use --list)", file=sys.stderr)
        return 2

    if OpenAI is None:
        print("ERROR: openai package not installed. Run:\n"
              "  pip install openai", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parents[3]
    data_root = (args.data_dir or repo_root / "data").resolve()
    companies_root = data_root / "companies"
    db_path = data_root / "state.db"

    if not db_path.exists():
        print(f"ERROR: {db_path} does not exist.")
        return 2

    # Determine fields
    if args.fields:
        wanted = [f.strip() for f in args.fields.split(",") if f.strip()]
        unknown = [f for f in wanted if f not in FIELDS]
        if unknown:
            print(f"ERROR: unknown fields {unknown}; use --list to see options")
            return 2
    else:
        wanted = list(FIELDS)

    conn = open_db(db_path)
    try:
        row = conn.execute(
            "SELECT stock_code, company_name FROM companies WHERE stock_code = ?",
            (args.company,),
        ).fetchone()
        if row is None:
            print(f"ERROR: company {args.company} not in DB")
            return 2
        code, name = row
        cdir_name = build_company_dir(code, name)
        info_dir = companies_root / cdir_name / "info"

        if args.source_file is not None:
            # Direct-file mode (for chapter-slice markdown)
            sf = args.source_file
            if not sf.is_absolute():
                # Try resolving relative to repo root, then cwd
                cand = (repo_root / sf).resolve()
                if cand.is_file():
                    sf = cand
                else:
                    sf = sf.resolve()
            if not sf.is_file():
                print(f"ERROR: source-file {sf} not found", file=sys.stderr)
                return 2
            md_text = sf.read_text(encoding="utf-8")
            source_label = f"file:{sf.name}"
            print(f"Source: explicit file {sf}")
        else:
            md_text, source_label, _ = _read_md_for_company(
                companies_root, cdir_name, prefer=args.source,
            )
            if md_text is None:
                print(
                    f"ERROR: no markdown found for {code} under info/ or info/precision/. "
                    f"Run Skill A (hkex-pdf-reader-batch) or Skill B (precision) first, "
                    f"or pass --source-file for a chapter slice.",
                    file=sys.stderr,
                )
                return 2

        print(f"Plan for {code} {name}:")
        print(f"  source: {source_label} ({len(md_text)} chars)")
        print(f"  fields ({len(wanted)}): {wanted}")
        print(f"  model: {args.model}")

        if args.dry_run:
            return 0

        if not args.api_key:
            print("ERROR: --api-key or $LLM_API_KEY required", file=sys.stderr)
            return 2

        client = OpenAI(api_key=args.api_key,
                        base_url=args.base_url) if args.base_url else OpenAI(api_key=args.api_key)

        succeeded = 0
        failed = 0
        needs_review_count = 0
        companies_updates: dict[str, object] = {}

        for fname in wanted:
            field = FIELDS[fname]
            print(f"\n[extract] {fname} ...", file=sys.stderr)
            result = process_field(
                field, md_text, source_label,
                client=client, model=args.model,
                info_dir=info_dir, repo_root=repo_root,
            )
            if result.rel_path is None:
                failed += 1
                print(f"  FAIL {fname}: {result.notes}")
                continue
            succeeded += 1
            if result.needs_review:
                needs_review_count += 1
                print(f"  REVIEW {fname}: {result.notes}")
            else:
                print(f"  OK {fname} -> {result.rel_path}")

            # Register in DB
            content_sha = _sha256_str(
                json.dumps(result.value, ensure_ascii=False, sort_keys=True)
            )
            upsert_extraction(
                conn=conn,
                stock_code=code,
                field_name=fname,
                output_path=result.rel_path,
                extractor=EXTRACTOR_NAME,
                extracted_at=_utcnow_iso(),
                source_pdf_hash=None,  # we don't pin to a single PDF
                content_sha256=content_sha,
                notes=result.notes,
            )
            conn.commit()

            # Stage companies-table update
            if fname in COMPANIES_TABLE_FIELDS and not result.needs_review:
                col = COMPANIES_TABLE_FIELDS[fname]
                # value for these fields is a string under the same key
                v = result.value
                if isinstance(v, str):
                    companies_updates[col] = v

        # Apply companies-table updates
        if companies_updates:
            sets = ", ".join(f"{c} = ?" for c in companies_updates)
            vals = list(companies_updates.values()) + [code]
            conn.execute(
                f"UPDATE companies SET {sets}, last_updated = ? WHERE stock_code = ?",
                vals + [_utcnow_iso(), code],
            )
            conn.commit()
            print(f"\nUpdated companies table: {companies_updates}")

        export_json(conn, data_root, repo_root,
                    source_label="hkex-pdf-field-extractor")
        print(
            f"\nSummary: {succeeded} ok, {failed} failed, "
            f"{needs_review_count} needs review"
        )
        return 0 if failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
