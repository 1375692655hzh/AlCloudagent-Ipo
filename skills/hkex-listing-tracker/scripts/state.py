"""HKEX IPO state inference rules.

This module maps HKEX document types to one of 4 IPO lifecycle states.
To add a new data source in the future, only extend STATE_INFER_RULES here.

NOTE: This file is shared between three sibling skills:
  - skills/hkex-offering-tracker/scripts/state.py       (招股发行抓取)
  - skills/hkex-application-tracker/scripts/state.py    (递表聆讯抓取)
  - skills/hkex-listing-tracker/scripts/state.py        (配发结果抓取)
Each skill keeps its own copy to avoid cross-directory imports; the three
copies must stay in sync (the state-machine.md reference document is the
canonical spec).
"""
from __future__ import annotations

# The 4 lifecycle stages tracked by these skills. Stored in the
# `companies.listing_stage` column (with `current_state` as a legacy alias).
STATES = ("遞表", "聆訊", "招股", "已上市")

# doc_type (as printed on HKEX page or returned in JSON `nF` field) -> stage.
# Use the exact traditional Chinese string shown on the page.
#
# Three data sources are now wired:
#   1. predefineddocuments=6 (HTML, traditional Chinese) — offering-tracker (招股)
#   2. appindex JSON (modern traditional Chinese with parenthesised
#      submission ordinal like 「（第一次呈交）」) — application-tracker (遞表/聆訊)
#   3. predefineddocuments=4 (HTML, traditional Chinese) — listing-tracker (已上市)
STATE_INFER_RULES: dict[str, str] = {
    # 招股 source (predefineddocuments=6, HTML)
    "全球發售": "招股",
    "全球发售": "招股",
    "公開招股": "招股",
    "發售以供認購": "招股",
    "發售現有證券": "招股",

    # 遞表 source (appindex JSON `nF` field, 申請版本 / Application Proof)
    "申請版本": "遞表",
    "申請版本（第一次呈交）": "遞表",
    "申請版本（第二次呈交）": "遞表",
    "申請版本（第三次呈交）": "遞表",
    "申请版本": "遞表",  # simplified variant, just in case

    # 聆訊 source (appindex JSON `nF` field, 聆訊後資料集 / PHIP)
    "聆訊後資料集": "聆訊",
    "聆訊後資料集（第一次呈交）": "聆訊",
    "聆訊後資料集（第二次呈交）": "聆訊",
    "聆讯后资料集": "聆訊",  # simplified variant
    "聆訊后资料集": "聆訊",  # mixed variant

    # 已上市 source (predefineddocuments=4, 新上市股份配發結果)
    "配發結果": "已上市",
    "配發結果公告": "已上市",
    "分配結果公告": "已上市",
    "最終發售價及配發結果公告": "已上市",
    "新上市股份配發結果": "已上市",
    # simplified variants
    "配发结果": "已上市",
    "配发结果公告": "已上市",
    "分配结果公告": "已上市",
    "最终发售价及配发结果公告": "已上市",
    "新上市股份配发结果": "已上市",
}

# ---------------------------------------------------------------------------
# Per-source whitelists (what each tracker should actually download)
# ---------------------------------------------------------------------------

# offering source (predefineddocuments=6): only 全球發售 counts; 重组/介绍/
# 债务证券/发售现有证券 are NOT IPO public offerings.
CURRENT_SOURCE_WHITELIST = frozenset({"全球發售", "全球发售"})

# application source (appindex JSON): only 申請版本 + 聆訊後資料集 count;
# 整體協調人公告 / 警告聲明 are ancillary filings.
APPLICATION_SOURCE_WHITELIST = frozenset({
    # 申請版本 (Application Proof) -> 遞表
    "申請版本",
    "申請版本（第一次呈交）",
    "申請版本（第二次呈交）",
    "申請版本（第三次呈交）",
    "申请版本",
    # 聆訊後資料集 (Post-Hearing Information Pack) -> 聆訊
    "聆訊後資料集",
    "聆訊後資料集（第一次呈交）",
    "聆訊後資料集（第二次呈交）",
    "聆讯后资料集",
    "聆訊后资料集",
})

# listing source (predefineddocuments=4): only IPO allotment results count;
# 供股 (rights issue) / 配售 (private placement) by *already-listed*
# companies must be excluded — they show up on the same page but are NOT
# new-listing events.
LISTING_SOURCE_WHITELIST = frozenset({
    "配發結果", "配发结果",
    "配發結果公告", "配发结果公告",
    "分配結果公告", "分配结果公告",
    "最終發售價及配發結果公告", "最终发售价及配发结果公告",
    "新上市股份配發結果", "新上市股份配发结果",
})

# Title patterns that indicate a NON-IPO allotment (rights issue / private
# placement by an existing listed company). Used to reject rows whose
# doc_type happens to contain 配發結果 but is actually about 供股/配售.
LISTING_EXCLUDED_TITLE_PATTERNS = ("供股", "配售", "公開發售增發")


def infer_state(doc_type: str | None) -> str | None:
    """Return the lifecycle stage for a doc_type, or None if not mapped.

    >>> infer_state("全球發售")
    '招股'
    >>> infer_state("申請版本（第一次呈交）")
    '遞表'
    >>> infer_state("聆訊後資料集")
    '聆訊'
    >>> infer_state("配發結果公告")
    '已上市'
    >>> infer_state("重組方案")
    >>> infer_state(None)
    """
    if not doc_type:
        return None
    return STATE_INFER_RULES.get(doc_type.strip())


def is_current_source_ipo(doc_type: str | None) -> bool:
    """True iff this doc_type should be downloaded from predefineddocuments=6."""
    if not doc_type:
        return False
    return doc_type.strip() in CURRENT_SOURCE_WHITELIST


def is_application_source_evidence(doc_type: str | None) -> bool:
    """True iff this doc_type is primary 遞表/聆訊 evidence from appindex JSON."""
    if not doc_type:
        return False
    return doc_type.strip() in APPLICATION_SOURCE_WHITELIST


def is_listing_source_evidence(doc_type: str | None) -> bool:
    """True iff this doc_type is primary 已上市 evidence from predefineddocuments=4.

    Stricter than infer_state: only the IPO-allotment whitelist counts, and
    explicitly rejects titles that contain 供股 / 配售 (rights issues or
    private placements by existing listed companies, which appear on the
    same page but are NOT new IPO allotments).
    """
    if not doc_type:
        return False
    s = doc_type.strip()
    # Negative filter: reject rights issues and private placements even if
    # the title also mentions 配發結果 (e.g. "供股結果（包括補償安排）").
    for pat in LISTING_EXCLUDED_TITLE_PATTERNS:
        if pat in s:
            return False
    return s in LISTING_SOURCE_WHITELIST
