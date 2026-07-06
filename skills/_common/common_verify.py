"""common_verify — 双源比对 + 业务规则引擎。

来源：HKIPO 项目 compare.py + business_checks.py。
设计原则：
  - 比对只标记不一致，绝不改值（避免校验器擅改正确值）
  - 业务规则用容差表达"约等"，所有规则都返回 {passed, expected, actual, ...}
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# 数值归一化
# ---------------------------------------------------------------------------


def to_num(x: Any) -> float | None:
    """任意值 → float。失败/空 → None。"""
    if x is None:
        return None
    try:
        s = re.sub(r"[^\d.\-]", "", str(x))
        return float(s) if s else None
    except (ValueError, TypeError):
        return None


def normalize_for_compare(s: Any) -> str:
    """字段比对前的归一化：去逗号、空格、全角、统一单位。"""
    if s is None:
        return ""
    s = str(s).strip()
    s = s.replace(",", "").replace(" ", "")
    s = s.translate(str.maketrans("０１２３４５６７８９．％", "0123456789.%"))
    s = re.sub(r"(港元|港幣|HKD|hkd)", "港元", s)
    return s


# ---------------------------------------------------------------------------
# 双源比对
# ---------------------------------------------------------------------------


@dataclass
class FieldCompare:
    id: str
    name: str
    primary: Any  # 主源值（如 MinerU）
    verify: Any   # 校验源值（如 doubao vision）
    status: str   # matched | mismatch | verify_missing | primary_missing | both_missing
    match: bool
    note: str = ""


def compare_value(p: Any, v: Any) -> tuple[str, bool, str]:
    """单字段比对 → (status, match, note)。"""
    if p is None and v is None:
        return "both_missing", False, "两源均未提取到"
    if v is None or v == "":
        return "verify_missing", False, "校验源未提供值"
    if p is None or p == "":
        return "primary_missing", False, "主源未提取到"

    np = normalize_for_compare(p)
    nv = normalize_for_compare(v)
    if np == nv:
        return "matched", True, ""

    pn = to_num(np)
    vn = to_num(nv)
    if pn is not None and vn is not None and abs(pn - vn) < 0.01:
        return "matched", True, "数值等价"
    return "mismatch", False, f"primary={p!r} vs verify={v!r}"


def compare_scalars(
    primary: dict[str, Any],
    verify: dict[str, Any],
    *,
    name_map: dict[str, str] | None = None,
) -> dict:
    """两组 scalar 字段比对。返回 {fields: [...], summary: {...}}。

    primary/verify 形如 {field_id: value} 或 {field_id: {"value": ..., "name": ...}}。
    """
    name_map = name_map or {}
    fields: list[dict] = []

    def _get(d: dict, fid: str) -> tuple[Any, str]:
        v = d.get(fid)
        if isinstance(v, dict):
            return v.get("value"), v.get("name", fid)
        return v, name_map.get(fid, fid)

    all_ids = list(primary.keys()) + [k for k in verify.keys() if k not in primary]
    for fid in all_ids:
        p_val, name = _get(primary, fid)
        v_val, _ = _get(verify, fid)
        status, ok, note = compare_value(p_val, v_val)
        fields.append({
            "id": fid,
            "name": name,
            "primary": p_val,
            "verify": v_val,
            "status": status,
            "match": ok,
            "note": note,
        })
    summary = {
        "total": len(fields),
        "matched": sum(1 for f in fields if f["match"]),
        "mismatch": sum(1 for f in fields if f["status"] == "mismatch"),
        "verify_missing": sum(1 for f in fields if f["status"] == "verify_missing"),
        "primary_missing": sum(1 for f in fields if f["status"] == "primary_missing"),
    }
    return {"fields": fields, "summary": summary}


# ---------------------------------------------------------------------------
# 业务规则引擎
# ---------------------------------------------------------------------------


@dataclass
class RuleResult:
    id: str
    name: str
    passed: bool
    note: str = ""
    expected: Any = None
    actual: Any = None
    tolerance_pct: float = 0.0
    diff_pct: float = 0.0


def _row_total(
    table_rows: list[list[str]],
    col_idx: int,
    *,
    skip_header: bool = True,
    total_keywords: tuple[str, ...] = ("總計", "總计", "总计", "小計", "小计"),
) -> tuple[float | None, float | None, bool]:
    """对表的某列求和。

    返回 (sum_excluding_total_row, total_row_value, found_total_row)
    """
    if not table_rows:
        return None, None, False
    body = table_rows[1:] if skip_header else list(table_rows)
    found_total = False
    last_value: Any = None
    end = len(body)
    if body:
        last = body[-1]
        first_cell = str(last[0]).strip() if last else ""
        if any(k in first_cell for k in total_keywords):
            found_total = True
            end = len(body) - 1
            if last and col_idx < len(last):
                last_value = last[col_idx]
    total_sum = 0.0
    has_any = False
    for r in body[:end]:
        if not r or col_idx >= len(r):
            continue
        n = to_num(r[col_idx])
        if n is not None:
            total_sum += n
            has_any = True
    return (total_sum if has_any else None), to_num(last_value), found_total


def check_approx_equal(
    actual: float | None,
    expected: float | None,
    *,
    tolerance_pct: float,
    name: str,
    rule_id: str,
) -> RuleResult:
    """通用"约等"规则。"""
    if actual is None or expected is None:
        return RuleResult(id=rule_id, name=name, passed=False, note="字段缺失无法校验")
    diff_pct = abs(actual - expected) / expected * 100 if expected else 0
    passed = diff_pct < tolerance_pct
    note = "" if passed else f"差 {actual-expected:+,.2f}（{diff_pct:.2f}%）"
    return RuleResult(
        id=rule_id, name=name, passed=passed,
        expected=expected, actual=actual,
        tolerance_pct=tolerance_pct, diff_pct=round(diff_pct, 4),
        note=note,
    )


def check_row_sum_vs_total(
    table_rows: list[list[str]],
    col_idx: int,
    *,
    name: str,
    rule_id: str,
    tolerance_pct: float = 5.0,
) -> RuleResult:
    """行加总 ≈ 总计行。"""
    s, total, found = _row_total(table_rows, col_idx)
    if s is not None and found and total is not None:
        return check_approx_equal(
            s, total, tolerance_pct=tolerance_pct, name=name, rule_id=rule_id
        )
    note = (
        f"未找到「總計」行（可能跨页丢失）；行加总 {s:,.0f}（仅供参考）"
        if s else "表数据不完整"
    )
    return RuleResult(
        id=rule_id, name=name, passed=True,
        note=note, actual=s, expected=total,
    )


def check_max_value(
    table_rows: list[list[str]],
    col_idx: int,
    *,
    threshold: float,
    name: str,
    rule_id: str,
) -> RuleResult:
    """表中某列的最大值必须 < threshold（如最大承配人占比 < 25%）。"""
    if not table_rows:
        return RuleResult(id=rule_id, name=name, passed=False, note="表为空")
    body = table_rows[1:] if len(table_rows) > 1 else []
    max_val: float | None = None
    for r in body:
        if col_idx < len(r):
            n = to_num(r[col_idx])
            if n is not None and (max_val is None or n > max_val):
                max_val = n
    if max_val is None:
        return RuleResult(id=rule_id, name=name, passed=False, note="该列无有效数值")
    passed = max_val < threshold
    return RuleResult(
        id=rule_id, name=name, passed=passed,
        actual=max_val, expected=threshold,
        note="" if passed else f"最大值 {max_val}% 超阈值 {threshold}%",
    )


# ---------------------------------------------------------------------------
# 通用业务规则集（ IPO 配售结果场景）
# ---------------------------------------------------------------------------


def run_allotment_business_checks(
    scalars: dict[str, Any],
    table_a_rows: list[list[str]],
    table_b_rows: list[list[str]],
    *,
    table_placee_rows: list[list[str]] | None = None,
) -> dict:
    """运行 IPO 配售结果的 6 条标准业务规则。"""
    def _get(d, fid):
        v = d.get(fid)
        return v.get("value") if isinstance(v, dict) else v

    results: list[RuleResult] = []

    # 1. 香港 + 国际 = 全球
    hk = to_num(_get(scalars, "shares_hk_final"))
    intl = to_num(_get(scalars, "shares_intl_final"))
    glob = to_num(_get(scalars, "shares_global"))
    if None not in (hk, intl, glob):
        results.append(check_approx_equal(
            hk + intl, glob, tolerance_pct=0.1,
            name="香港+國際=全球發售股份", rule_id="hk_split",
        ))
    else:
        results.append(RuleResult(id="hk_split", name="香港+國際=全球發售股份",
                                  passed=False, note="字段缺失"))

    # 2 & 3. 占比 10% / 90%
    if hk and glob:
        results.append(check_approx_equal(
            hk / glob * 100, 10.0, tolerance_pct=1.0,
            name="香港公開發售佔比 ≈ 10%", rule_id="hk_pct",
        ))
    if intl and glob:
        results.append(check_approx_equal(
            intl / glob * 100, 90.0, tolerance_pct=1.0,
            name="國際配售佔比 ≈ 90%", rule_id="intl_pct",
        ))

    # 4 & 5. 甲/乙组行加总
    results.append(check_row_sum_vs_total(
        table_a_rows, col_idx=1,
        name="甲組有效申請行加總 ≈ 總計", rule_id="a_total",
        tolerance_pct=5.0,
    ))
    results.append(check_row_sum_vs_total(
        table_b_rows, col_idx=1,
        name="乙組有效申請行加總 ≈ 總計", rule_id="b_total",
        tolerance_pct=5.0,
    ))

    # 6. 最大承配人 < 25%
    if table_placee_rows:
        results.append(check_max_value(
            table_placee_rows, col_idx=2,
            threshold=25.0,
            name="最大承配人 < 25%（公眾持股量規則）", rule_id="placee_max",
        ))

    return _summarize(results)


def _summarize(results: list[RuleResult]) -> dict:
    return {
        "checks": [r.__dict__ for r in results],
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r.passed),
            "failed": sum(1 for r in results if not r.passed),
        },
    }


# ---------------------------------------------------------------------------
# 招股书档位表专用校验
# ---------------------------------------------------------------------------


PRICE_FEE_RATIO = 1.0085065  # 经纪 1.0% + 交易征费 0.0027% + 联交所 0.00565% + 会财局 0.00015%
LINEAR_RESIDUAL_TOL = 0.02


def validate_schedule_table(
    rows: list[list[str]],
    *,
    shares_col: int = 1,
    amount_col: int = 2,
    offer_price: float | None = None,
    min_rows: int = 10,
) -> dict:
    """招股书档位表校验。

    Args:
        rows: [["甲组", "500", "5,131.24", "", ""], ...]
        shares_col: 股数列索引
        amount_col: 金额列索引
        offer_price: 已知招股价（用于线性校验）
    """
    issues: list[str] = []
    n = len(rows)
    if n < min_rows:
        issues.append(f"行数过少（{n} 行，应 ≥ {min_rows}），可能漏识别")

    for i in range(1, n):
        prev_shares = to_num(rows[i - 1][shares_col]) if shares_col < len(rows[i - 1]) else None
        cur_shares = to_num(rows[i][shares_col]) if shares_col < len(rows[i]) else None
        prev_amt = to_num(rows[i - 1][amount_col]) if amount_col < len(rows[i - 1]) else None
        cur_amt = to_num(rows[i][amount_col]) if amount_col < len(rows[i]) else None
        if cur_shares is not None and prev_shares is not None and cur_shares <= prev_shares and cur_shares > 0:
            issues.append(f"行 {i+1}: 股数非递增（{prev_shares} → {cur_shares}）")
        if cur_amt is not None and prev_amt is not None and cur_amt <= prev_amt and cur_amt > 0:
            issues.append(f"行 {i+1}: 金额非递增（{prev_amt} → {cur_amt}）")

    if offer_price and offer_price > 0:
        expected_ratio = offer_price * PRICE_FEE_RATIO
        bad_rows = []
        for i, r in enumerate(rows):
            shares = to_num(r[shares_col]) if shares_col < len(r) else None
            amt = to_num(r[amount_col]) if amount_col < len(r) else None
            if not shares or not amt:
                continue
            expected = shares * expected_ratio
            if expected > 0:
                residual = abs(amt - expected) / expected
                if residual > LINEAR_RESIDUAL_TOL:
                    bad_rows.append(i + 1)
        if bad_rows:
            issues.append(
                f"金额与股数×单价({offer_price})×费率 偏差 > {LINEAR_RESIDUAL_TOL*100:.0f}% 的行: {bad_rows[:5]}"
                + ("..." if len(bad_rows) > 5 else "")
            )

    seen = list(dict.fromkeys(issues))
    return {
        "issues": seen,
        "issue_count": len(seen),
        "confidence": "high" if not seen else "low",
    }
