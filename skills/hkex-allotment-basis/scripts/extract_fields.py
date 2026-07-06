#!/usr/bin/env python3
"""从 MinerU 输出的 Markdown 抽取配发结果公告关键字段。

输入：
  --md        MinerU 输出的 markdown 路径
  --fields    fields.yaml 路径
  --out       fields.json 输出路径

输出 JSON 结构：
  {
    "source_md": "...",
    "scalars": { "<id>": {"value": ..., "name": ..., "source_line": ..., "matched_pattern": ...} },
    "tables":   { "<id>": {"rows": [[...]], "header": [...], "name": ...} },
    "business_checks_spec": [...]
  }
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "_common"))

import yaml  # type: ignore

from common_tables import (  # noqa: E402
    extract_all_tables,
    find_table_by_anchor,
    flatten_md_for_scalar,
    normalize_md_for_matching,
)


@dataclass
class ScalarResult:
    value: str | None
    source_line: str | None
    matched_pattern: str | None
    name: str


def _extract_scalar(flat_md: str, field_spec: dict) -> ScalarResult:
    """按上下文锚点裁切文本后再跑 patterns。"""
    text = flat_md
    name = field_spec.get("name", field_spec["id"])

    ca = field_spec.get("context_after")
    if ca:
        sec_pat = re.compile(r"^##\s*" + re.escape(ca) + r"\s*$", re.M)
        m = sec_pat.search(text)
        if m:
            text = text[m.end():]
        else:
            idx = text.find(ca)
            if idx != -1:
                text = text[idx:]

    cb = field_spec.get("context_before")
    if cb:
        sec_pat = re.compile(r"^##\s*" + re.escape(cb) + r"\s*$", re.M)
        m = sec_pat.search(text)
        if m:
            text = text[: m.start()]
        else:
            idx = text.find(cb)
            if idx != -1:
                text = text[: idx + len(cb) + 50]

    for pat in field_spec.get("patterns", []):
        try:
            m = re.search(pat, text)
        except re.error as e:
            return ScalarResult(None, None, None, name)
        if not m:
            continue
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        if line_end == -1:
            line_end = len(text)
        source_line = text[line_start:line_end].strip()
        value = m.group("v").strip() if "v" in m.groupdict() else m.group(0).strip()
        vre = field_spec.get("value_re")
        if vre and value:
            vm = re.search(vre, value)
            if vm:
                value = (vm.group("v") if "v" in vm.groupdict() else vm.group(0)).strip()
        return ScalarResult(value, source_line, pat, name)
    return ScalarResult(None, None, None, name)


def extract_scalars(flat_md: str, specs: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for s in specs:
        sid = s["id"]
        r = _extract_scalar(flat_md, s)
        out[sid] = {
            "value": r.value,
            "name": r.name,
            "source_line": r.source_line,
            "matched_pattern": r.matched_pattern,
        }
    return out


def extract_tables(parsed_tables, table_specs: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for t in table_specs:
        tid = t["id"]
        anchor = t.get("header_anchor")
        if anchor is None:
            out[tid] = {"rows": [], "header": [], "name": t.get("name", tid)}
            continue
        match = find_table_by_anchor(
            parsed_tables,
            anchor,
            not_anchor=t.get("not_anchor"),
            last_context_only=t.get("last_context_only", False),
            last_context_n=t.get("last_context_n", 1),
            context_anchor=t.get("context_anchor"),
        )
        if match is None:
            out[tid] = {"rows": [], "header": [], "name": t.get("name", tid)}
            continue
        out[tid] = {
            "rows": match.rows,
            "header": match.rows[0] if match.rows else [],
            "name": t.get("name", tid),
            "context_before": match.context_before,
            "anchor_matched": True,
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", type=Path, required=True)
    ap.add_argument("--fields", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    raw_md = args.md.read_text(encoding="utf-8")
    norm_md = normalize_md_for_matching(raw_md)
    flat_md = flatten_md_for_scalar(norm_md)

    spec = yaml.safe_load(args.fields.read_text(encoding="utf-8"))
    scalar_specs = [s for s in spec.get("scalars", []) if s.get("type", "scalar") == "scalar"]
    table_specs = spec.get("tables", [])

    parsed_tables = extract_all_tables(raw_md)
    scalars = extract_scalars(flat_md, scalar_specs)
    tables = extract_tables(parsed_tables, table_specs)

    out = {
        "source_md": str(args.md),
        "scalars": scalars,
        "tables": tables,
        "business_checks_spec": spec.get("business_checks", []),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    s_count = sum(1 for v in scalars.values() if v.get("value"))
    t_count = sum(1 for v in tables.values() if v.get("rows"))
    print(f"OK: {args.out}")
    print(f"  scalars matched: {s_count}/{len(scalars)}")
    print(f"  tables matched:  {t_count}/{len(tables)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
