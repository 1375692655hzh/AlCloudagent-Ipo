"""PDF 书签读取（章节定位 - 层 1）。

最可靠的章节定位来源：HKEX 招股书 PDF 大多数带原生 outline（书签），
书签里有章节名 + 指向的 PDF 物理页（0-indexed）。如果有书签，
本模块直接返回 [(章节名, pdf_page_index), ...]，不需要任何 LLM 或文本解析。

层 1 失败（书签为空）的情况由 toc_parser.py + offset_calculator.py 处理。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Bookmark:
    """One PDF outline entry."""
    title: str           # 书签标题（章节名）
    pdf_page: int        # PDF 物理页（0-indexed）
    level: int = 0       # 嵌套层级（0=顶层，1=二级，...）


def read_bookmarks(pdf_path: Path) -> list[Bookmark]:
    """Read PDF outline (bookmarks). Returns [] if PDF has no outline.

    Uses pypdf. pypdf's outline is a tree of Destination objects; we flatten it
    in DFS order, recording level by depth tracking.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return []

    try:
        reader = PdfReader(str(pdf_path), strict=False)
    except Exception:
        return []

    try:
        outline = reader.outline
    except Exception:
        return []

    if not outline:
        return []

    bookmarks: list[Bookmark] = []

    def _walk(items: list[Any], level: int) -> None:
        for item in items:
            if isinstance(item, list):
                # Nested children
                _walk(item, level + 1)
            else:
                # Destination object
                try:
                    page_num = reader.get_destination_page_number(item)
                except Exception:
                    continue
                title = getattr(item, "title", None) or str(item)
                if page_num is None or page_num < 0:
                    continue
                bookmarks.append(Bookmark(
                    title=title.strip(),
                    pdf_page=page_num,
                    level=level,
                ))

    _walk(outline, 0)
    return bookmarks


def find_chapter_by_bookmark(
    bookmarks: list[Bookmark],
    chapter_query: str,
    *,
    fuzzy_threshold: float = 0.5,
) -> tuple[Bookmark, Bookmark | None] | None:
    """Find a chapter by name. Returns (start_bookmark, end_bookmark_or_None).

    end_bookmark is the next top-level bookmark after start (chapter ends where
    the next sibling chapter begins). None means the chapter runs to the end
    of the document.

    Matching strategy:
      1. Normalize both sides (strip spaces, lowercase, drop punctuation noise)
      2. Try exact match first
      3. Then substring match (query appears in title or vice versa)
      4. Fuzzy fallback via simple character overlap ratio

    Returns None if no bookmark matches with score >= fuzzy_threshold.
    """
    if not bookmarks:
        return None

    def _norm(s: str) -> str:
        # Strip spaces/punctuation/zero-width chars, lowercase
        import re
        s = re.sub(r"[\s\-—\-·・:：•]+", "", s)
        s = s.replace("\u3000", "")  # full-width space
        return s.lower()

    nq = _norm(chapter_query)
    if not nq:
        return None

    top_level = [b for b in bookmarks if b.level == 0] or bookmarks

    # Pass 1: exact normalized match
    for i, b in enumerate(top_level):
        if _norm(b.title) == nq:
            end = top_level[i + 1] if i + 1 < len(top_level) else None
            return b, end

    # Pass 2: substring either direction
    for i, b in enumerate(top_level):
        nb = _norm(b.title)
        if not nb:
            continue
        if nq in nb or nb in nq:
            end = top_level[i + 1] if i + 1 < len(top_level) else None
            return b, end

    # Pass 3: fuzzy - character overlap ratio
    best_score = 0.0
    best_idx = -1
    for i, b in enumerate(top_level):
        nb = _norm(b.title)
        if not nb:
            continue
        # Simple Jaccard on character sets
        set_q = set(nq)
        set_b = set(nb)
        if not set_q or not set_b:
            continue
        score = len(set_q & set_b) / len(set_q | set_b)
        if score > best_score:
            best_score = score
            best_idx = i

    if best_score >= fuzzy_threshold and best_idx >= 0:
        i = best_idx
        end = top_level[i + 1] if i + 1 < len(top_level) else None
        return top_level[i], end

    return None
