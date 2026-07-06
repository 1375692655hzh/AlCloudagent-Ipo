#!/usr/bin/env python3
"""招股书财务表格抽取 — MinerU pipeline + YAML 字段配置 + LLM 后处理。

依赖：
  - Skill B (hkex-pdf-reader-precision) 已把财务章节转 MD（推荐用 hkex-chapter-locator 切片后转）
  - 或直接喂整本招股书 MD（截断到 120K 字符）

输入：
  --md        已转换的财务章节 MD（推荐 hkex-chapter-locator 切片后的）
  --fields    fields.yaml
  --out       financials.json

输出 JSON：
  {
    "source_md": "...",
    "years": ["2023", "2024", "2025"],
    "statements": {
      "income_statement": {"rows": [[...]], "header": [...]},
      ...
    },
    "fields": {
      "revenue":          {"name": "营业收入", "values": {"2023": .., "2024": .., "2025": ..}},
      "net_profit":       {...},
      ...
    },
    "sanity_checks": [...]
  }
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "_common"))

import yaml  # type: ignore

from common_tables import (  # noqa: E402
    extract_all_tables, find_table_by_anchor, normalize_md_for_matching,
)
from common_verify import to_num, check_approx_equal, RuleResult  # noqa: E402


def find_statement_table(parsed_tables, stmt_spec: dict):
    """用 anchor + fallback_anchors 找报表表。"""
    primary = stmt_spec.get("header_anchor")
    if primary:
        m = find_table_by_anchor(parsed_tables, primary)
        if m:
            return m
    for fb in stmt_spec.get("fallback_anchors", []) or []:
        m = find_table_by_anchor(parsed_tables, fb)
        if m:
            return m
    return None


def extract_years_from_header(header: list[str]) -> list[str]:
    """从表头识别年度列。

    表头例: ['兆人民币', '2023年', '2024年', '2025年']
    或: ['', '截至12月31日止年度', '2023', '2024', '2025']
    """
    years: list[str] = []
    for cell in header:
        m = re.search(r"(20\d{2})", str(cell))
        if m:
            years.append(m.group(1))
    return years


def find_row_by_anchor(rows: list[list[str]], row_anchor: list[str]) -> list[str] | None:
    """按 row_anchor 在表里找行。row_anchor 是 any-of，命中任一关键词即匹配。"""
    for r in rows:
        joined = " ".join(str(c) for c in r)
        for kw in row_anchor:
            if kw in joined:
                return r
    return None


def extract_field_value(
    row: list[str], years: list[str], *, year_count: int = 3,
) -> dict[str, float | None]:
    """从一行抽 N 个年度的值。

    假设：第 0 列是行名，后 N 列是各年值。
    """
    n = year_count
    out: dict[str, float | None] = {}
    for i, y in enumerate(years[:n]):
        col_idx = i + 1  # skip row name column
        if col_idx < len(row):
            v = to_num(row[col_idx])
            out[y] = v
        else:
            out[y] = None
    return out


def run_sanity_checks(
    fields: dict, check_specs: list[dict],
) -> list[dict]:
    """运行会计恒等式 + 合理性检查。"""
    def _get(fid: str, year: str) -> float | None:
        f = fields.get(fid)
        if not f:
            return None
        return (f.get("values") or {}).get(year)

    results: list[dict] = []
    years = sorted({y for f in fields.values() for y in (f.get("values") or {})})
    for spec in check_specs:
        rid = spec.get("id", spec["name"])
        name = spec["name"]
        tol = spec.get("tolerance_pct", 0)
        for y in years:
            if spec["id"] == "accounting_equation":
                a = _get("total_assets", y)
                l = _get("total_liabilities", y)
                e = _get("total_equity", y)
                if None in (a, l, e):
                    continue
                rr = check_approx_equal(a, l + e,
                                        tolerance_pct=tol, name=name, rule_id=rid)
            elif spec["id"] == "gross_margin_range":
                rev = _get("revenue", y)
                gp = _get("gross_profit", y)
                if rev is None or gp is None or not rev:
                    continue
                ratio = gp / rev
                rr = RuleResult(id=rid, name=f"{name} ({y})",
                                passed=0 <= ratio <= 1,
                                actual=ratio, expected="[0,1]",
                                note="" if 0 <= ratio <= 1
                                else f"毛利率 {ratio:.2%} 越界")
            elif spec["id"] == "net_margin_range":
                rev = _get("revenue", y)
                np = _get("net_profit", y)
                if rev is None or np is None or not rev:
                    continue
                ratio = np / rev
                rr = RuleResult(id=rid, name=f"{name} ({y})",
                                passed=-2 <= ratio <= 1,
                                actual=ratio, expected="[-2,1]",
                                note="" if -2 <= ratio <= 1
                                else f"净利率 {ratio:.2%} 越界")
            elif spec["id"] == "cfo_sign_consistent_with_net_profit":
                cfo = _get("cfo", y)
                np = _get("net_profit", y)
                if cfo is None or np is None:
                    continue
                sign_ok = (cfo >= 0) == (np >= 0)
                size_ok = abs(cfo) > 0.5 * abs(np) if np else False
                rr = RuleResult(id=rid, name=f"{name} ({y})",
                                passed=sign_ok or size_ok,
                                note="" if sign_ok or size_ok
                                else f"CFO={cfo:,.0f} 与 NP={np:,.0f} 符号/规模不符")
            else:
                continue
            # tag with year
            d = rr.__dict__
            d["year"] = y
            d["name"] = f"{name} ({y})"
            results.append(d)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--md", type=Path, required=True,
                    help="财务章节 MD（推荐 hkex-chapter-locator 切片后）")
    ap.add_argument("--fields", type=Path, required=True,
                    help="fields.yaml 路径")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--year-count", type=int, default=3,
                    help="抽取几年的数据（默认 3）")
    args = ap.parse_args()

    raw_md = args.md.read_text(encoding="utf-8")
    norm_md = normalize_md_for_matching(raw_md)
    parsed_tables = extract_all_tables(norm_md)

    spec = yaml.safe_load(args.fields.read_text(encoding="utf-8"))

    # 抽三大报表
    statements_out: dict[str, dict] = {}
    years_found: list[str] = []
    for stmt in spec.get("statements", []):
        sid = stmt["id"]
        match = find_statement_table(parsed_tables, stmt)
        if match is None:
            print(f"WARN: 报表 {sid} 未匹配", file=sys.stderr)
            statements_out[sid] = {"rows": [], "header": [], "matched": False}
            continue
        header = match.rows[0] if match.rows else []
        years = extract_years_from_header(header)
        if years and not years_found:
            years_found = years
        statements_out[sid] = {
            "rows": match.rows,
            "header": header,
            "matched": True,
            "years": years,
            "name": stmt.get("name", sid),
        }
        print(f"OK: {sid} 匹配 {len(match.rows)} 行, 年度 {years}", file=sys.stderr)

    # 抽字段
    fields_out: dict[str, dict] = {}
    for fspec in spec.get("fields", []):
        fid = fspec["id"]
        fname = fspec.get("name", fid)
        stmt_id = fspec.get("statement")
        anchor = fspec.get("row_anchor") or []
        stmt = statements_out.get(stmt_id) or {}
        rows = stmt.get("rows") or []
        row = find_row_by_anchor(rows, anchor) if rows else None
        if row is None:
            print(f"WARN: 字段 {fid} 未匹配（statement={stmt_id}）", file=sys.stderr)
            fields_out[fid] = {
                "name": fname, "values": {}, "matched": False, "row": None,
            }
            continue
        # use statement's years if available else default
        years = stmt.get("years") or years_found or []
        values = extract_field_value(row, years, year_count=args.year_count)
        fields_out[fid] = {
            "name": fname, "values": values, "matched": True,
            "row": row, "statement": stmt_id,
        }
        print(f"OK: {fid} -> {values}", file=sys.stderr)

    # 业务规则
    sanity_results = run_sanity_checks(fields_out, spec.get("sanity_checks", []))

    out = {
        "source_md": str(args.md),
        "years": years_found,
        "statements": statements_out,
        "fields": fields_out,
        "sanity_checks": sanity_results,
        "sanity_summary": {
            "total": len(sanity_results),
            "passed": sum(1 for r in sanity_results if r.get("passed")),
            "failed": sum(1 for r in sanity_results if not r.get("passed")),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\nOK: {args.out}")
    print(f"  years:  {years_found}")
    print(f"  fields: {sum(1 for f in fields_out.values() if f.get('matched'))}/{len(fields_out)} matched")
    s = out["sanity_summary"]
    print(f"  sanity: {s['passed']}/{s['total']} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
