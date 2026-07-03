"""HKEX IPO state inference rules.

This module maps HKEX document types to one of 4 IPO lifecycle states.
To add a new data source in the future, only extend STATE_INFER_RULES here.
"""
from __future__ import annotations

# The 4 lifecycle states tracked by this skill.
STATES = ("遞表", "聆訊", "招股", "已上市")

# doc_type (as printed on HKEX page) -> lifecycle state.
# Use the exact traditional Chinese string shown on the page.
STATE_INFER_RULES: dict[str, str] = {
    # 招股 source (predefineddocuments=6)
    "全球發售": "招股",
    "全球发售": "招股",
    "公開招股": "招股",
    "發售以供認購": "招股",
    "發售現有證券": "招股",
    # 已上市 source (predefineddocuments=5, not yet wired)
    "配發結果": "已上市",
    "新上市股份配發結果": "已上市",
    # 遞表 source (上市申請人, not yet wired)
    "上市申請人": "遞表",
    # 聆訊 (PHIP, not yet wired)
    "聆訊後資料集": "聆訊",
    "聆訊后资料集": "聆訊",
}

# The current source (predefineddocuments=6) only emits these doc types that
# qualify as "招股". Anything else on that page (重組方案 / 介紹 / 股份發售 /
# 招股章程－債務證券) is NOT a global offering and must be excluded.
CURRENT_SOURCE_WHITELIST = frozenset({"全球發售", "全球发售"})


def infer_state(doc_type: str | None) -> str | None:
    """Return the lifecycle state for a doc_type, or None if not mapped.

    >>> infer_state("全球發售")
    '招股'
    >>> infer_state("重組方案")
    >>> infer_state(None)
    """
    if not doc_type:
        return None
    return STATE_INFER_RULES.get(doc_type.strip())


def is_current_source_ipo(doc_type: str | None) -> bool:
    """True iff this doc_type should be downloaded from predefineddocuments=6.

    Stricter than infer_state: only the global-offering whitelist counts,
    not 發售以供認購 / 發售現有證券 (which are also on the page but represent
    non-global-offering filings).
    """
    if not doc_type:
        return False
    return doc_type.strip() in CURRENT_SOURCE_WHITELIST
