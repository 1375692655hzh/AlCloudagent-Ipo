#!/usr/bin/env python3
"""渲染最终 MD + 校验报告。

输入：
  --mineru-md   MinerU 提取的 MD（基础底稿）
  --fields      extract_fields.py 的 fields.json
  --compare     compare.py 的 compare.json（可选）
  --business    business_checks.py 的 business.json（可选）
  --out         最终 MD 输出路径
  --report      校验报告 MD 输出路径

输出：
  最终 MD = MinerU 原文 + 顶部追加「字段交叉校验」摘要表
  校验报告 = 字段级 + 业务规则详细列表
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def render_final_md(mineru_md: str, fields: dict, compare: dict | None,
                    business: dict | None) -> str:
    parts = ["# 配发结果（含字段交叉校验）\n"]
    parts.append("> 主提取：MinerU vlm  |  校验来源：doubao vision  |  "
                 "详细见同目录 `校验报告.md`\n")

    if compare and compare.get("scalars"):
        comp = compare["scalars"]
        cs = comp.get("summary", {})
        parts.append("\n## 字段交叉校验摘要\n")
        parts.append(f"- 总字段：**{cs.get('total',0)}**  "
                     f"一致：**{cs.get('matched',0)}**  "
                     f"分歧：**{cs.get('mismatch',0)}**  "
                     f"校验缺失：**{cs.get('verify_missing',0)}**\n")
        parts.append("\n| 字段 | MinerU 主提取 | doubao vision | 状态 |")
        parts.append("|---|---|---|---|")
        for r in comp.get("fields", []):
            flag = ("✅ 一致" if r.get("match") else
                    "⚠️ 分歧" if r["status"] == "mismatch" else
                    "⚠️ 校验缺失" if r["status"] == "verify_missing" else
                    "ℹ️ 仅校验有")
            m = r.get("primary") or "—"
            v = r.get("verify") or "—"
            parts.append(f"| {r['name']} | `{m}` | `{v}` | {flag} |")
        parts.append("")

        # 表格行数对比
        tcmp = compare.get("tables", {}) or {}
        if tcmp:
            parts.append("\n## 表格行数校验\n")
            parts.append("| 表 | MinerU 行数 | doubao 行数 | 状态 |")
            parts.append("|---|---|---|---|")
            for tid, info in tcmp.items():
                flag = "✅" if info.get("row_count_match") else "⚠️ 行数不一致"
                parts.append(f"| {tid} | {info['primary_rows']} | "
                             f"{info['verify_rows']} | {flag} |")
            parts.append("")

    if business:
        checks = business.get("checks", [])
        bsum = business.get("summary", {})
        parts.append("\n## 业务规则校验\n")
        parts.append(f"- 总规则：**{bsum.get('total',0)}**  "
                     f"通过：**{bsum.get('passed',0)}**  "
                     f"未通过：**{bsum.get('failed',0)}**\n")
        if any(not c.get("passed") for c in checks):
            parts.append("\n| 规则 | 状态 | 说明 |")
            parts.append("|---|---|---|")
            for c in checks:
                if not c.get("passed"):
                    parts.append(f"| {c['name']} | ❌ | {c.get('note','')} |")
            parts.append("")

    parts.append("\n---\n")
    parts.append("\n## 原文（MinerU vlm 提取）\n")
    parts.append(mineru_md.rstrip())
    return "\n".join(parts) + "\n"


def render_report(fields: dict, compare: dict | None, business: dict | None) -> str:
    parts = ["# 配发结果校验报告\n"]

    if compare and compare.get("scalars"):
        cs = compare["scalars"]
        s = cs.get("summary", {})
        parts.append("## 1. 字段交叉校验\n")
        parts.append(f"总 {s.get('total',0)} 字段，一致 {s.get('matched',0)}，"
                     f"分歧 {s.get('mismatch',0)}，校验缺失 {s.get('verify_missing',0)}。\n")
        mismatches = [r for r in cs.get("fields", []) if r["status"] == "mismatch"]
        if not mismatches:
            parts.append("\n✅ 所有可比字段均一致。\n")
        else:
            parts.append("\n### ⚠️ 分歧字段\n")
            for r in mismatches:
                parts.append(f"\n**{r['name']}** (`{r['id']}`)")
                parts.append(f"- MinerU 主提取：`{r.get('primary')}`")
                parts.append(f"- doubao vision：`{r.get('verify')}`")
                parts.append(f"- 说明：{r.get('note','')}")
                parts.append(f"- 建议：人工核对原文确认正确值\n")
        vm = [r for r in cs.get("fields", []) if r["status"] == "verify_missing"]
        if vm:
            parts.append("\n### ℹ️ 校验源缺失（仅 MinerU 单方提取）\n")
            for r in vm:
                parts.append(f"- **{r['name']}** (`{r['id']}`)：MinerU=`{r.get('primary')}`")
    else:
        parts.append("## 1. 字段交叉校验\n\n（未运行 doubao vision 校验，跳过）\n")

    if compare and compare.get("tables"):
        parts.append("\n## 2. 表格行数校验\n")
        parts.append("| 表 | MinerU 行数 | doubao 行数 | 状态 |")
        parts.append("|---|---|---|---|")
        for tid, info in compare["tables"].items():
            flag = "✅" if info.get("row_count_match") else "⚠️ 不一致"
            parts.append(f"| {tid} | {info['primary_rows']} | "
                         f"{info['verify_rows']} | {flag} |")

    if business:
        checks = business.get("checks", [])
        s = business.get("summary", {})
        parts.append("\n## 3. 业务规则校验\n")
        parts.append(f"总 {s.get('total',0)} 规则，通过 {s.get('passed',0)}，"
                     f"未通过 {s.get('failed',0)}。\n\n")
        parts.append("| 规则 | 状态 | 详情 |")
        parts.append("|---|---|---|")
        for c in checks:
            flag = "✅" if c.get("passed") else "❌"
            note = c.get("note", "") or "—"
            details = []
            for k, v in c.items():
                if k in ("id", "name", "passed", "note"):
                    continue
                details.append(f"{k}={v}")
            detail_str = note + ("；" + "，".join(details) if details else "")
            parts.append(f"| {c['name']} | {flag} | {detail_str} |")

    return "\n".join(parts) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mineru-md", type=Path, required=True)
    ap.add_argument("--fields", type=Path, required=True)
    ap.add_argument("--compare", type=Path, default=None)
    ap.add_argument("--business", type=Path, default=None)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    args = ap.parse_args()

    md = args.mineru_md.read_text(encoding="utf-8")
    fields = json.loads(args.fields.read_text(encoding="utf-8"))
    compare = (json.loads(args.compare.read_text(encoding="utf-8"))
               if args.compare and args.compare.is_file() else None)
    business = (json.loads(args.business.read_text(encoding="utf-8"))
                if args.business and args.business.is_file() else None)

    final_md = render_final_md(md, fields, compare, business)
    report_md = render_report(fields, compare, business)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(final_md, encoding="utf-8")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report_md, encoding="utf-8")
    print(f"OK: wrote {args.out} + {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
