#!/usr/bin/env python3
"""用 doubao vision 对配发结果 PDF 做第二源字段提取。

策略（与 HKIPO verify_fields.py 一致）：
  1. PyMuPDF 在每页搜索关键词，定位字段所在页
  2. 渲染该页成 PNG，调 vision 用结构化 prompt 抽字段（要求 JSON）
  3. 输出 verify.json：{ field_id: value }

用法：
    python verify_vision.py --pdf <file.pdf> --out <verify.json> [--no-tables]
    python verify_vision.py --pdf <file.pdf> --out <verify.json> --backend ark
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "_common"))

from common_env import load_env  # noqa: E402
from common_llm import (  # noqa: E402
    VisionConfig, vision_chat_json, call_with_retry,
)
from common_pdf import find_pages_by_keywords, render_page  # noqa: E402


# 每字段一组关键词（第一次出现的页 = 字段所在页）
FIELD_LOCATORS = [
    ("offer_price",           ["發售價", "每股發售股份"]),
    ("shares_global",         ["全球發售的發售股份數目"]),
    ("shares_hk_final",       ["香港公開發售的發售股份最終數目",
                               "香港公開發售項下最終發售股份"]),
    ("shares_intl_final",     ["國際配售的發售股份最終數目",
                               "國際配售項下最終發售股份"]),
    ("valid_applications",    ["有效申請數目"]),
    ("accepted_applications", ["獲接納申請數目"]),
    ("placees_count",         ["承配人數目"]),
    ("oversub_hk",            ["認購水平"]),
    ("oversub_intl",          ["認購水平"]),
]

# 分配基准表/集中度表 — 直接抽取行（第二源对表格也做完整抽取）
TABLE_LOCATORS = {
    "allotment_basis_a": ["分配基準", "甲組", "甲组"],
    "allotment_basis_b": ["分配基準", "乙組", "乙组"],
    "placee_concentration": ["承配人", "獲配發", "配發佔"],
}


FIELD_DESCRIPTIONS = {
    "offer_price": "發售價（格式「X.XX港元」）",
    "shares_global": "全球發售的發售股份數目（纯数字带逗号）",
    "shares_hk_final": "香港公開發售的發售股份最終數目（纯数字带逗号）",
    "shares_intl_final": "國際配售的發售股份最終數目（纯数字带逗号）",
    "valid_applications": "香港公開發售的「有效申請數目」（纯数字带逗号）",
    "accepted_applications": "「獲接納申請數目」（纯数字带逗号）",
    "placees_count": "國際配售的「承配人數目」（纯数字）",
    "oversub_hk": "香港公開發售的「認購水平」（格式「X,XXX.XX 倍」）",
    "oversub_intl": "國際配售的「認購水平」（注意区分于香港那个，格式「X.XX 倍」）",
}


def build_scalar_prompt(fields: list[str]) -> str:
    lines = [
        "请仔细看这张PDF页面，提取以下字段的值。",
        "严格按 JSON 格式输出，不要 markdown 代码块标记，不要任何解释。",
        "如果某字段在当前页面看不到，输出 null。",
        "数字原样保留（含逗号、百分号、单位）。",
        "",
        "需要提取的字段：",
    ]
    for f in fields:
        lines.append(f'  "{f}": {FIELD_DESCRIPTIONS.get(f, f)}')
    lines.extend([
        "",
        "输出格式示例：",
        '{"offer_price":"7.20港元","valid_applications":"252,640"}',
        "",
        "只输出 JSON 对象本身。",
    ])
    return "\n".join(lines)


def build_table_prompt(table_id: str) -> str:
    if table_id == "allotment_basis_a":
        return (
            "这是港股配发结果公告的「香港公开发售分配基准—甲组」表格。\n"
            "请抽取每一行（不含表头、总计、脚注），输出严格 JSON：\n"
            '{"rows": [["申请H股数目", "有效申请数目", "分配/抽签基准", "占所申请H股总数的概约百分比"], ...]}\n'
            "数字原样保留（含逗号/百分号）。\n"
            "若当前页看不到甲组表，输出 {\"rows\": []}。"
        )
    if table_id == "allotment_basis_b":
        return (
            "这是港股配发结果公告的「香港公开发售分配基准—乙组」表格。\n"
            "请抽取每一行（不含表头、总计、脚注），输出严格 JSON：\n"
            '{"rows": [["申请H股数目", "有效申请数目", "分配/抽签基准", "概约百分比"], ...]}\n'
            "数字原样保留。若当前页看不到乙组表，输出 {\"rows\": []}。"
        )
    if table_id == "placee_concentration":
        return (
            "这是港股配发结果公告的「承配人集中度」表格。\n"
            "请抽取前 25 名承配人的所有行，输出严格 JSON：\n"
            '{"rows": [["排名", "獲配發H股數目", "配發佔國際配售百分比", ...], ...]}\n'
            "若当前页看不到该表，输出 {\"rows\": []}。"
        )
    return ""


def find_scalar_pages(pdf: Path) -> dict[int, list[str]]:
    """field_id → 第一个命中页（1-based）。多个 field 落同页则聚合。"""
    page_to_fields: dict[int, list[str]] = {}
    for fid, keywords in FIELD_LOCATORS:
        pages = find_pages_by_keywords(pdf, keywords, min_hits=1, max_pages=1)
        if pages:
            pno = pages[0]
            page_to_fields.setdefault(pno, []).append(fid)
        else:
            print(f"WARN: field {fid} not located", file=sys.stderr)
    return page_to_fields


def find_table_pages(pdf: Path) -> dict[str, int]:
    """table_id → 最匹配的页（1-based）。"""
    out: dict[str, int] = {}
    for tid, keywords in TABLE_LOCATORS.items():
        pages = find_pages_by_keywords(pdf, keywords, min_hits=2, max_pages=3)
        if pages:
            out[tid] = pages[0]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--no-scalars", action="store_true",
                    help="跳过 scalar 字段抽取")
    ap.add_argument("--no-tables", action="store_true",
                    help="跳过表格字段抽取（默认会抽，提高双源覆盖）")
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args()

    load_env()
    cfg = VisionConfig.from_env()
    if cfg is None:
        print("ERROR: ARK_API_KEY not set (vision verifier needs it)", file=sys.stderr)
        # Write empty so downstream still works
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps({"source_pdf": str(args.pdf), "scalars": {}, "tables": {}},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 2

    if not args.pdf.is_file():
        print(f"PDF not found: {args.pdf}", file=sys.stderr)
        return 1

    print(f"PDF: {args.pdf}", file=sys.stderr)
    scalars: dict[str, object] = {}
    tables: dict[str, dict] = {}

    if not args.no_scalars:
        print("Locating scalar pages...", file=sys.stderr)
        page_to_fields = find_scalar_pages(args.pdf)
        print(f"  pages: {sorted(page_to_fields.keys())}", file=sys.stderr)

        for pno, fields in sorted(page_to_fields.items()):
            fields = list(dict.fromkeys(fields))
            print(f"Page {pno}: rendering + vision for {fields}", file=sys.stderr)
            try:
                img = render_page(args.pdf, pno, dpi=args.dpi)
            except Exception as e:
                print(f"  render failed: {e}", file=sys.stderr)
                for f in fields:
                    scalars[f] = None
                continue
            prompt = build_scalar_prompt(fields)
            try:
                parsed, raw = call_with_retry(
                    lambda: vision_chat_json(cfg, img, prompt),
                    retries=2, label=f"scalar p{pno}",
                )
            except Exception as e:
                print(f"  vision failed: {e}", file=sys.stderr)
                for f in fields:
                    scalars[f] = None
                continue
            print(f"  parsed keys: {list(parsed.keys()) if parsed else 'None'}",
                  file=sys.stderr)
            if isinstance(parsed, dict):
                for f in fields:
                    scalars[f] = parsed.get(f)
            else:
                for f in fields:
                    scalars[f] = None

    if not args.no_tables:
        print("Locating table pages...", file=sys.stderr)
        table_pages = find_table_pages(args.pdf)
        print(f"  tables: {table_pages}", file=sys.stderr)
        for tid, pno in table_pages.items():
            print(f"Page {pno}: vision for table {tid}", file=sys.stderr)
            try:
                img = render_page(args.pdf, pno, dpi=args.dpi)
            except Exception as e:
                print(f"  render failed: {e}", file=sys.stderr)
                continue
            prompt = build_table_prompt(tid)
            try:
                parsed, _ = call_with_retry(
                    lambda: vision_chat_json(cfg, img, prompt),
                    retries=2, label=f"table {tid}",
                )
            except Exception as e:
                print(f"  vision failed: {e}", file=sys.stderr)
                continue
            if isinstance(parsed, dict) and "rows" in parsed:
                tables[tid] = {
                    "rows": parsed["rows"],
                    "name": tid,
                    "source_page": pno,
                }
                print(f"  {tid}: {len(parsed['rows'])} rows", file=sys.stderr)

    out = {
        "source_pdf": str(args.pdf),
        "backend": cfg.model,
        "scalars": scalars,
        "tables": tables,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: {args.out}")
    print(f"  scalars: {sum(1 for v in scalars.values() if v)}/{len(scalars)}")
    print(f"  tables:  {sum(1 for v in tables.values() if v.get('rows'))}/{len(tables)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
