#!/usr/bin/env python3
"""招股书档位表抽取 — 文本直抽 + 视觉兜底（HKIPO 项目验证过的设计）。

档位表是中签率计算的核心输入：
  申请股数 N → 应缴款项 M
  M ≈ N × offer_price × 1.0085（含经纪/交易征费/联交所/会财局）

策略（直接搬 HKIPO extract_schedule_pdf.py）：
  1. PyMuPDF 关键词定位档位表页（申請認購/應繳款項/股份數目/入場費）
  2. 文本直抽：扫所有数字对 (N, M)，算 ratio = M/N，量化到 0.5 取众数过滤噪声
  3. 文本不足 → 渲染 PNG → doubao vision → MiniMax 兜底
  4. 校验：单调性 + 线性关系（金额 ≈ 股数 × 单价 × 1.0085）

输入：--pdf 招股书.pdf --offer-price 8.30
输出：info/winrate_schedule_fields.json
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "_common"))

from common_env import load_env  # noqa: E402
from common_pdf import find_pages_by_keywords, render_page, page_text  # noqa: E402
from common_llm import VisionConfig, vision_chat_json, LLMConfig, chat_json, call_with_retry  # noqa: E402
from common_verify import validate_schedule_table, to_num  # noqa: E402

# 费率常数（HKIPO 项目一致）：经纪 1% + 交易征费 0.0027% + 联交所 0.00565% + 会财局 0.00015%
PRICE_FEE_RATIO = 1.0085065

# 档位表关键词（招股书繁简混合，全覆盖）
SCHEDULE_KEYWORDS = [
    "申請認購", "申请认购",
    "應繳款項", "应缴款项",
    "股份數目", "股份数目",
    "入場費", "入场费",
    "招股章程",  # 用于排除"配发结果公告"（无招股章程字样）
]


@dataclass
class ScheduleRow:
    shares: int       # 申请股数
    amount: float     # 应缴款项（港元）
    raw_shares: str
    raw_amount: str


def locate_schedule_pages(pdf: Path, *, max_pages: int = 5) -> list[int]:
    """定位档位表所在页。要求至少命中 2 个关键词。"""
    return find_pages_by_keywords(
        pdf, SCHEDULE_KEYWORDS, min_hits=2, max_pages=max_pages,
    )


def extract_from_text(pdf: Path, pages: list[int]) -> list[ScheduleRow]:
    """文本路径：扫所有数字对，按比值众数过滤。"""
    pairs: list[tuple[int, float]] = []
    for pno in pages:
        text = page_text(pdf, pno)
        # 数字带逗号或空格分隔：4,000 / 4 000 / 4000
        # 寻找所有"申请股数 ... 应缴款项"邻近对
        # 简化策略：抓所有 <整数> ... <金额> 配对，相距 < 200 字符
        nums = list(re.finditer(
            r"(?<![0-9])(\d{1,3}(?:[,\s]\d{3})+|\d{4,})(?![0-9])", text,
        ))
        # 金额通常带小数点
        amounts = list(re.finditer(
            r"(?<![0-9])(\d{1,3}(?:,\d{3})*\.\d{1,2})(?![0-9])", text,
        ))
        # 配对：对每个金额，找最近的整数（位于金额之前）
        for am in amounts:
            av = float(am.group(1).replace(",", ""))
            # 在该金额之前 200 字符内找最大的整数
            best_n: int | None = None
            best_dist = 999
            for nm in nums:
                if nm.end() > am.start():
                    continue
                dist = am.start() - nm.end()
                if dist > 200:
                    continue
                nv = int(nm.group(1).replace(",", "").replace(" ", ""))
                if nv < 100:  # 太小，可能是行号
                    continue
                if dist < best_dist:
                    best_dist = dist
                    best_n = nv
            if best_n is not None and av > 100:
                pairs.append((best_n, av))

    if not pairs:
        return []

    # 量化 ratio 到 0.5 一档取众数
    ratios = [a / n for n, a in pairs if n > 0]
    if not ratios:
        return []
    bucketed = [round(r * 2) / 2 for r in ratios]
    try:
        mode_ratio = statistics.mode(bucketed)
    except statistics.StatisticsError:
        mode_ratio = statistics.median(ratios)

    # 过滤偏差 > 2% 的配对
    rows: list[ScheduleRow] = []
    seen_shares: set[int] = set()
    for n, a in pairs:
        if n <= 0:
            continue
        ratio = a / n
        if abs(ratio - mode_ratio) / mode_ratio > 0.02:
            continue
        if n in seen_shares:
            continue
        seen_shares.add(n)
        rows.append(ScheduleRow(
            shares=n, amount=a,
            raw_shares=f"{n:,}", raw_amount=f"{a:,.2f}",
        ))

    rows.sort(key=lambda r: r.shares)
    return rows


def extract_from_vision(
    pdf: Path, pages: list[int], *, vcfg: VisionConfig
) -> list[ScheduleRow]:
    """视觉路径：渲染 PNG → doubao vision → JSON 抽表。"""
    rows: list[ScheduleRow] = []
    prompt = (
        "这是港股招股书的「申请认购档位表」页面。\n"
        "请抽取每一行（不含表头、脚注），输出严格 JSON：\n"
        '{"rows": [["申请股数", "应缴款项港元"], ...]}\n'
        "申请股数原样保留（不带逗号）；应缴款项保留 2 位小数。\n"
        "若当前页看不到档位表，输出 {\"rows\": []}。"
    )
    seen_shares: set[int] = set()
    for pno in pages:
        try:
            img = render_page(pdf, pno)
        except Exception as e:
            print(f"  render p{pno} failed: {e}", file=sys.stderr)
            continue
        try:
            parsed, _ = call_with_retry(
                lambda: vision_chat_json(vcfg, img, prompt),
                retries=2, label=f"vision p{pno}",
            )
        except Exception as e:
            print(f"  vision p{pno} failed: {e}", file=sys.stderr)
            continue
        if not isinstance(parsed, dict):
            continue
        for r in parsed.get("rows", []):
            if not isinstance(r, list) or len(r) < 2:
                continue
            n = to_num(r[0])
            a = to_num(r[1])
            if n is None or a is None or n <= 0:
                continue
            n_int = int(n)
            if n_int in seen_shares:
                continue
            seen_shares.add(n_int)
            rows.append(ScheduleRow(
                shares=n_int, amount=float(a),
                raw_shares=str(r[0]), raw_amount=str(r[1]),
            ))
    rows.sort(key=lambda x: x.shares)
    return rows


def extract_from_llm(
    pdf: Path, pages: list[int], *, lcfg: LLMConfig,
) -> list[ScheduleRow]:
    """LLM 兜底：把页面文本喂 LLM 让它返回 JSON 表。"""
    rows: list[ScheduleRow] = []
    seen_shares: set[int] = set()
    sys_prompt = (
        "你是港股招股书解析器。给定档位表页面文本，抽取所有「申请股数 → 应缴款项」对，"
        "严格输出 JSON：{\"rows\": [[<股数整数>, <金额数字>], ...]}，不要 markdown 标记。"
    )
    for pno in pages:
        text = page_text(pdf, pno)
        if not text.strip():
            continue
        try:
            parsed, _ = call_with_retry(
                lambda: chat_json(lcfg, sys_prompt, text[:8000]),
                retries=2, label=f"llm p{pno}",
            )
        except Exception as e:
            print(f"  llm p{pno} failed: {e}", file=sys.stderr)
            continue
        if not isinstance(parsed, dict):
            continue
        for r in parsed.get("rows", []):
            if not isinstance(r, list) or len(r) < 2:
                continue
            n = to_num(r[0])
            a = to_num(r[1])
            if n is None or a is None or n <= 0:
                continue
            n_int = int(n)
            if n_int in seen_shares:
                continue
            seen_shares.add(n_int)
            rows.append(ScheduleRow(
                shares=n_int, amount=float(a),
                raw_shares=str(r[0]), raw_amount=str(r[1]),
            ))
    rows.sort(key=lambda x: x.shares)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", type=Path, required=True)
    ap.add_argument("--offer-price", type=float, default=None,
                    help="已知招股价，用于线性校验（强烈建议传入）")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--min-rows", type=int, default=10,
                    help="文本路径行数少于此则降级到 vision（默认 10）")
    ap.add_argument("--no-vision", action="store_true",
                    help="不调 vision 兜底")
    ap.add_argument("--no-llm", action="store_true",
                    help="不调 LLM 兜底")
    ap.add_argument("--max-pages", type=int, default=5)
    args = ap.parse_args()

    load_env()

    if not args.pdf.is_file():
        print(f"ERROR: {args.pdf} not found", file=sys.stderr)
        return 2

    print(f"PDF: {args.pdf}")
    print(f"Offer price: {args.offer_price}")

    # Step 1: 定位
    pages = locate_schedule_pages(args.pdf, max_pages=args.max_pages)
    if not pages:
        print("ERROR: 定位不到档位表页（关键词未命中）", file=sys.stderr)
        # 写空结果
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({
            "source_pdf": str(args.pdf),
            "offer_price": args.offer_price,
            "rows": [],
            "engine": "none",
            "issues": ["定位不到档位表页"],
            "extracted_at": datetime.now(timezone.utc).astimezone().isoformat(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1
    print(f"Located pages: {pages}")

    # Step 2: 文本直抽
    rows = extract_from_text(args.pdf, pages)
    engine = "text"
    print(f"Text path: {len(rows)} rows")

    # Step 3: 文本不足 → vision
    vcfg = None if args.no_vision else VisionConfig.from_env()
    if len(rows) < args.min_rows and vcfg is not None:
        print(f"  rows < {args.min_rows}, falling back to doubao vision",
              file=sys.stderr)
        v_rows = extract_from_vision(args.pdf, pages, vcfg=vcfg)
        if len(v_rows) > len(rows):
            print(f"  vision: {len(v_rows)} rows (replacing text result)",
                  file=sys.stderr)
            rows = v_rows
            engine = "vision"
        else:
            print(f"  vision: {len(v_rows)} rows (no improvement)", file=sys.stderr)

    # Step 4: 还不够 → LLM 兜底
    if len(rows) < args.min_rows and not args.no_llm:
        lcfg = LLMConfig.from_env()
        if lcfg.api_key:
            print(f"  rows < {args.min_rows}, falling back to LLM ({lcfg.model})",
                  file=sys.stderr)
            l_rows = extract_from_llm(args.pdf, pages, lcfg=lcfg)
            if len(l_rows) > len(rows):
                print(f"  llm: {len(l_rows)} rows (replacing)", file=sys.stderr)
                rows = l_rows
                engine = "llm"

    # Step 5: 校验
    table_rows = [[r.raw_shares, r.raw_amount, "", ""] for r in rows]
    validation = validate_schedule_table(
        table_rows, shares_col=0, amount_col=1,
        offer_price=args.offer_price, min_rows=args.min_rows,
    )

    # 输出
    out_rows = [
        {"组别": "", "股数": r.shares, "金额": r.amount, "中签率": "", "平均分配": ""}
        for r in rows
    ]
    out = {
        "source_pdf": str(args.pdf),
        "offer_price": args.offer_price,
        "engine": engine,
        "pages_scanned": pages,
        "rows": out_rows,
        "issues": validation["issues"],
        "confidence": validation["confidence"],
        "extracted_at": datetime.now(timezone.utc).astimezone().isoformat(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    print(f"\nOK: {args.out}")
    print(f"  engine:     {engine}")
    print(f"  rows:       {len(out_rows)}")
    print(f"  confidence: {validation['confidence']}")
    if validation["issues"]:
        print(f"  issues:")
        for i in validation["issues"]:
            print(f"    - {i}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
