"""目录文本解析（章节定位 - 层 2 主路径，配合 offset_calculator）。

当 PDF 没有书签（或书签残缺）时，从招股书前几页提取目录文本，
用 LLM 解析为 [{chapter, printed_page}, ...] 的结构化数据。

目录文本由调用方先用 pypdf 抽 PDF 前 N 页得到（招股书目录通常在第 4-12 页）。

LLM 配置同 Skill C（OpenAI-compatible，环境变量 LLM_MODEL / LLM_BASE_URL / LLM_API_KEY）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TOCEntry:
    """One table-of-contents entry."""
    chapter: str        # 章节名（保留原文，繁/简/英）
    printed_page: int   # 印刷页码（不是 PDF 物理页码！）


@dataclass
class TOCParseResult:
    entries: list[TOCEntry]
    raw_response: str
    error: str | None = None


PROMPT_TOC_PARSE = """你是港股招股书目录解析助手。下面是一份招股书的目录原文，
请把每个章节提取为 JSON 数组，每项 {{"chapter": "<章节名>", "printed_page": <页码数字>}}。

要求：
- 章节名保留原文（繁体/简体/英文都保留）
- printed_page 是目录里写的数字（如果只写"100 頁"就取 100；如果是"APPENDIX I, 1 頁"就取 1）
- 跳过明显不是章节的（如纯页码、纯公司名、目录自己的标题）
- 如果是罗马数字（i, ii, iii），转成阿拉伯数字
- 按目录顺序输出

严格按以下 JSON 输出（无任何额外文字）：
{{"toc": [{{"chapter": "...", "printed_page": 1}}, ...]}}

目录原文：
{toc_text}
"""


def extract_toc_text(pdf_path: Path, *, max_pages: int = 15) -> str | None:
    """Read first N pages of PDF, concat text, return as TOC candidate.

    HKEX 招股书目录通常在第 4-12 页（前面是封面、重要提示、释义）。
    读前 15 页足够覆盖。
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(str(pdf_path), strict=False)
    except Exception:
        return None
    n = min(max_pages, len(reader.pages))
    parts: list[str] = []
    for i in range(n):
        try:
            parts.append(reader.pages[i].extract_text() or "")
        except Exception:
            continue
    text = "\n\n".join(parts).strip()
    return text or None


def parse_toc_with_llm(
    toc_text: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    max_chars: int = 8_000,
) -> TOCParseResult:
    """Call LLM to parse TOC text into structured entries.

    Args:
        toc_text: raw text extracted from TOC pages
        model: LLM model name (default $LLM_MODEL)
        base_url: OpenAI-compatible API base (default $LLM_BASE_URL)
        api_key: API key (default $LLM_API_KEY)
        max_chars: truncate TOC text to this many chars (avoid huge prompts)

    Returns:
        TOCParseResult. error is None on success.
    """
    try:
        from openai import OpenAI
    except ImportError:
        return TOCParseResult(entries=[], raw_response="",
                              error="openai package not installed")

    model = model or os.environ.get("LLM_MODEL", "glm-5.2")
    base_url = base_url or os.environ.get("LLM_BASE_URL")
    api_key = api_key or os.environ.get("LLM_API_KEY")

    if not api_key:
        return TOCParseResult(entries=[], raw_response="",
                              error="LLM_API_KEY not set")

    # Truncate to avoid blowing context with junk from cover pages
    truncated = toc_text[:max_chars] if len(toc_text) > max_chars else toc_text
    user_msg = PROMPT_TOC_PARSE.format(toc_text=truncated)

    try:
        client = OpenAI(api_key=api_key, base_url=base_url) if base_url \
            else OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system",
                 "content": "你是港股招股书解析助手。严格按 JSON 格式输出。"},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.0,
        )
    except Exception as exc:
        return TOCParseResult(entries=[], raw_response="", error=str(exc))

    raw = resp.choices[0].message.content or ""
    entries = _parse_toc_json(raw)
    if not entries:
        return TOCParseResult(entries=[], raw_response=raw,
                              error="failed to parse JSON")
    return TOCParseResult(entries=entries, raw_response=raw, error=None)


def _parse_toc_json(raw: str) -> list[TOCEntry]:
    """Parse LLM response into TOCEntry list. Tolerates code-fence wrappers."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback: regex find {"toc": [...]}
        m = re.search(r'\{\s*["\']?toc["\']?\s*:\s*\[(.+?)\]\s*\}',
                      raw, re.DOTALL)
        if not m:
            return []
        try:
            obj = json.loads("[" + m.group(1) + "]")
            obj = {"toc": obj}
        except json.JSONDecodeError:
            return []
    items = obj.get("toc", []) if isinstance(obj, dict) else []
    entries: list[TOCEntry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        chapter = item.get("chapter") or item.get("name") or ""
        page = item.get("printed_page") or item.get("page")
        if not chapter or not isinstance(page, int):
            continue
        if page < 0 or page > 9999:
            continue
        entries.append(TOCEntry(chapter=str(chapter).strip(), printed_page=page))
    return entries


def find_chapter_in_toc(
    entries: list[TOCEntry],
    chapter_query: str,
) -> tuple[TOCEntry, TOCEntry | None] | None:
    """Find a chapter in TOC. Returns (start_entry, end_entry_or_None).

    end_entry is the next entry in TOC order, or None if it's the last.
    """
    if not entries:
        return None

    def _norm(s: str) -> str:
        s = re.sub(r"[\s\-—\-·・:：•]+", "", s)
        s = s.replace("\u3000", "")
        return s.lower()

    nq = _norm(chapter_query)
    if not nq:
        return None

    # Pass 1: exact
    for i, e in enumerate(entries):
        if _norm(e.chapter) == nq:
            end = entries[i + 1] if i + 1 < len(entries) else None
            return e, end
    # Pass 2: substring
    for i, e in enumerate(entries):
        ne = _norm(e.chapter)
        if not ne:
            continue
        if nq in ne or ne in nq:
            end = entries[i + 1] if i + 1 < len(entries) else None
            return e, end
    # Pass 3: fuzzy
    best_score = 0.0
    best_idx = -1
    for i, e in enumerate(entries):
        ne = _norm(e.chapter)
        if not ne:
            continue
        set_q, set_e = set(nq), set(ne)
        if not set_q or not set_e:
            continue
        score = len(set_q & set_e) / len(set_q | set_e)
        if score > best_score:
            best_score, best_idx = score, i
    if best_score >= 0.5 and best_idx >= 0:
        end = entries[best_idx + 1] if best_idx + 1 < len(entries) else None
        return entries[best_idx], end
    return None
