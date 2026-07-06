"""字段词典 + prompt 模板 + 校验规则（Skill C 内部使用）。

每个字段定义抽取规则，Skill C 的 extract_fields.py 按字段单独提问，
而不是整本招股书丢给 LLM 总结（那种方式幻觉率极高）。

字段分两类：
  - simple：单值字段（如 listing_type、issue_price_range）
  - complex：多元素结构（如 use_of_proceeds、cornerstone_investors）

每个字段的 prompt 遵循三段式：
  1. 角色 + 任务定义
  2. 输出格式约束（强制 JSON schema）
  3. 不确定时的行为（"找不到返回 null" / "不要编造"）

校验规则在 extract_fields.py 里执行，把不合规则的 LLM 输出标记为
"needs_review"，写回 notes 字段。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


# ---------------------------------------------------------------------------
# Field definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldDef:
    """One extractable field."""

    name: str  # extractions.field_name (also info/<name>.json)
    description_zh: str  # 中文说明（给用户看）
    priority_source: str  # "招股书" | "配发结果" | "PHIP" | "any"
    extractor_label: str = "pdf_field_v1"
    needs_review_on_miss: bool = True  # 找不到时是否标记人工复核

    # The system+user prompt template. {markdown} will be replaced with
    # the source markdown text. Must ask for strict JSON output.
    prompt: str = ""

    # Python validators: list[(value) -> error_msg_or_None]. If any returns
    # a non-None string, the extraction is marked needs_review.
    validators: tuple[Callable[[object], str | None], ...] = ()


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _v_listing_type(v) -> str | None:
    if v is None:
        return "listing_type is null"
    if v not in ("AH", "非-AH", "H 股", "红筹", "待确认"):
        return f"listing_type unexpected value: {v!r}"
    return None


def _v_price_range(v) -> str | None:
    """Issue price range should look like '8.50-9.20 HKD' or single number."""
    if v is None:
        return "issue_price_range is null"
    s = str(v)
    # Allow formats: "8.5-9.2", "8.50 HKD - 9.20 HKD", "8.5 港元"
    has_digit = any(c.isdigit() for c in s)
    if not has_digit:
        return f"issue_price_range has no digit: {s!r}"
    return None


def _v_use_of_proceeds(v) -> str | None:
    """Use of proceeds should be a list of {purpose, percentage}."""
    if v is None:
        return "use_of_proceeds is null"
    if not isinstance(v, list):
        return "use_of_proceeds not a list"
    if not v:
        return "use_of_proceeds empty list"
    total = 0.0
    for item in v:
        if not isinstance(item, dict):
            return "use_of_proceeds item not a dict"
        if "purpose" not in item or "percentage" not in item:
            return "use_of_proceeds item missing purpose/percentage"
        try:
            total += float(item["percentage"])
        except (TypeError, ValueError):
            return f"use_of_proceeds percentage not numeric: {item['percentage']!r}"
    # 招股书用途百分比之和应在 95-105 区间（容许四舍五入）
    if not (90.0 <= total <= 110.0):
        return f"use_of_proceeds percentages sum={total:.1f}% (expect ~100%)"
    return None


def _v_cornerstones(v) -> str | None:
    if v is None:
        return None  # 没有基石也是合法的
    if not isinstance(v, list):
        return "cornerstone_investors not a list"
    for item in v:
        if not isinstance(item, dict):
            return "cornerstone item not a dict"
        if "name" not in item:
            return "cornerstone item missing name"
    return None


def _v_top_shareholders(v) -> str | None:
    if v is None:
        return "top_shareholders is null"
    if not isinstance(v, list):
        return "top_shareholders not a list"
    if not v:
        return "top_shareholders empty"
    for item in v:
        if not isinstance(item, dict) or "name" not in item:
            return "top_shareholders item missing name"
    return None


# ---------------------------------------------------------------------------
# Field dictionary
# ---------------------------------------------------------------------------


PROMPT_LISTING_TYPE = """你是港股 IPO 分析助手。请阅读以下招股书 Markdown，
判断这家公司是不是 AH 股（A 股 + H 股两地上市）。

判定依据：
- 文中明确提到"本公司 A 股" / "上海证券交易所" / "深圳证券交易所" / "科创板"
  / "AH 股" / "A+H" → 输出 "AH"
- 文中只提港股 / 香港联交所，没有 A 股相关字样 → 输出 "非-AH"
- 文中提到红筹架构 / VIE → 输出 "红筹"
- 文中提到"H 股"且无 A 股字样 → 输出 "H 股"
- 完全无法判断 → 输出 "待确认"

严格按以下 JSON 输出（无任何额外文字）：
{{"listing_type": "AH"}}

不确定时返回 "待确认"，**不要编造**。

招股书内容：
{markdown}
"""


PROMPT_ISSUE_PRICE = """你是港股 IPO 分析助手。请从以下招股书 / 配发结果 Markdown 中
提取本次公开发售的招股价区间（issue price range）。

要求：
- 若是区间，格式为 "<low>-<high> <currency>"，例如 "8.50-9.20 HKD"
- 若是单一价格，格式为 "<price> <currency>"，例如 "8.50 HKD"
- 货币用 ISO 代码：HKD / USD / CNY
- 找不到招股价信息 → 返回 null

严格按以下 JSON 输出：
{{"issue_price_range": "8.50-9.20 HKD"}}

**只从原文抽取，不要计算或编造**。

文档内容：
{markdown}
"""


PROMPT_USE_OF_PROCEEDS = """你是港股 IPO 分析助手。请从以下招股书 Markdown 的
"募集资金用途" / "募集款项所得" / "Use of Proceeds" 章节提取每项用途及占比。

要求：
- 输出 JSON 数组，每项 {{\"purpose\": \"<用途描述（简短）>\", \"percentage\": <0-100 数字>}}
- 百分比是本次募集款项净额的分配比例，总和应接近 100
- 找不到该章节 → 返回 null

严格按以下 JSON 输出：
{{\"use_of_proceeds\": [{{\"purpose\": \"...\", \"percentage\": 50.0}}, ...]}}

**不要编造用途**。原文没有的不要写。

招股书内容：
{markdown}
"""


PROMPT_CORNERSTONES = """你是港股 IPO 分析助手。请从以下招股书 Markdown 中
提取基石投资者列表（cornerstone investors）。

要求：
- 输出 JSON 数组，每项至少 {{\"name\": \"<基石全名>\"}}
- 若有认购金额 / 占比 / 锁定期信息，加 "amount_usd_m" / "percentage" / "lockup_days"
- 没有基石投资者 → 返回空数组 []
- 完全找不到该章节 → 返回 null

严格按以下 JSON 输出：
{{\"cornerstone_investors\": [{{\"name\": \"...\", ...}}, ...]}}

**只列原文明确称为"基石投资者"的**，不要把普通股东当基石。

招股书内容：
{markdown}
"""


PROMPT_TOP_SHAREHOLDERS = """你是港股 IPO 分析助手。请从以下招股书 Markdown 中
提取公司前 5-10 大股东（截至上市前）。

要求：
- 输出 JSON 数组，每项 {{\"name\": \"<股东名>\", \"percentage\": <0-100 数字>, \"role\": \"<创始人/机构/...可选>\"}}
- 按 percentage 降序
- 找不到 → 返回 null

严格按以下 JSON 输出：
{{\"top_shareholders\": [{{\"name\": \"...\", \"percentage\": 30.5, \"role\": \"创始人\"}}, ...]}}

**只从原文"主要股东" / "主要股东及管理层" / "Major Shareholders" 章节抽**。

招股书内容：
{markdown}
"""


PROMPT_CONFIRMED_NAME = """你是港股 IPO 分析助手。请从以下招股书 Markdown 封皮
或"股份簡稱" / "stock short name" 字段提取正式的股份简称。

要求：
- 输出繁体中文（HKEX 标准）
- 通常以 " - B" / " - W" / " - P" 后缀结尾（如有）
- 完全找不到 → 返回 null

严格按以下 JSON 输出：
{{\"confirmed_name\": \"立訊精密\"}}

**只抄原文，不要改写或翻译**。

招股书内容：
{markdown}
"""


FIELDS: dict[str, FieldDef] = {
    "listing_type": FieldDef(
        name="listing_type",
        description_zh="AH 类型（AH / 非-AH / H 股 / 红筹）",
        priority_source="招股书",
        prompt=PROMPT_LISTING_TYPE,
        validators=(_v_listing_type,),
        needs_review_on_miss=True,
    ),
    "issue_price_range": FieldDef(
        name="issue_price_range",
        description_zh="招股价区间（如 8.50-9.20 HKD）",
        priority_source="招股书",
        prompt=PROMPT_ISSUE_PRICE,
        validators=(_v_price_range,),
        needs_review_on_miss=False,
    ),
    "use_of_proceeds": FieldDef(
        name="use_of_proceeds",
        description_zh="募资用途及占比（数组）",
        priority_source="招股书",
        prompt=PROMPT_USE_OF_PROCEEDS,
        validators=(_v_use_of_proceeds,),
        needs_review_on_miss=True,
    ),
    "cornerstone_investors": FieldDef(
        name="cornerstone_investors",
        description_zh="基石投资者列表",
        priority_source="招股书",
        prompt=PROMPT_CORNERSTONES,
        validators=(_v_cornerstones,),
        needs_review_on_miss=False,
    ),
    "top_shareholders": FieldDef(
        name="top_shareholders",
        description_zh="前 5-10 大股东",
        priority_source="招股书",
        prompt=PROMPT_TOP_SHAREHOLDERS,
        validators=(_v_top_shareholders,),
        needs_review_on_miss=True,
    ),
    "confirmed_name": FieldDef(
        name="confirmed_name",
        description_zh="正式股份简称（繁体）",
        priority_source="招股书",
        prompt=PROMPT_CONFIRMED_NAME,
        validators=(),
        needs_review_on_miss=False,
    ),
}


# Fields whose successful extraction should also UPDATE the companies table.
COMPANIES_TABLE_FIELDS = {
    "listing_type": "listing_type",
    "confirmed_name": "confirmed_name",
}


def get_field(name: str) -> FieldDef:
    if name not in FIELDS:
        raise KeyError(f"unknown field {name!r}; known: {list(FIELDS)}")
    return FIELDS[name]


def list_fields() -> list[str]:
    return list(FIELDS)
