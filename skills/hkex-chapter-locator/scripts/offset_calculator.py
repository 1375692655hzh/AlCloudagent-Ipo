"""页码偏移量自动推算（章节定位 - 层 2 辅助）。

招股书有"印刷页码"（封皮标的 1/2/3...）和"PDF 物理页码"（0/1/2...）两套体系，
它们之间的偏移量 = 前置页数。本模块用"页脚投票"算法自动算出这个偏移量。

原理：每页底部几乎都印有页码（"– 100 –" 或 "- 1 -" 格式），扫所有页的页脚，
提取印刷页码，与 PDF 页码配对，求 (printed - pdf_index) 的众数，即偏移量。

调用方拿到偏移量后，就可以把 toc_parser.py 解析出来的"印刷页码"转换成
"PDF 物理页码"，喂给 Skill A/B 的 --pages 参数（或本 skill 的 slicer）。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


# Patterns for "– N –" / "- N -" / "— N —" page footers.
# We also accept the page number appearing alone in the last ~200 chars.
FOOTER_PATTERNS = [
    # "– 100 –" / "— 100 —" / "- 100 -"
    re.compile(r"[–\-—]\s*(\d{1,4})\s*[–\-—]"),
    # Bare digit on last line
    re.compile(r"\b(\d{1,4})\b\s*$"),
]

# Roman numerals (for "i, ii, iii" front-matter pages)
ROMAN_RE = re.compile(r"\b[ivxlcdm]{1,6}\b", re.IGNORECASE)


@dataclass
class OffsetResult:
    """Result of offset calculation."""
    offset: int | None              # printed_page - pdf_index; None if unknown
    samples: int                    # how many pages contributed to the vote
    confidence: float               # samples / total_pages_scanned, 0-1
    method: str                     # 'footer_vote' / 'roman_detector' / 'failed'

    def describe(self) -> str:
        if self.offset is None:
            return f"offset=unknown (method={self.method}, samples={self.samples})"
        return (f"offset={self.offset} (method={self.method}, "
                f"samples={self.samples}, confidence={self.confidence:.2f})")


def _extract_footer_page_num(text: str) -> int | None:
    """Try to find a printed page number in the last ~250 chars of a page."""
    if not text:
        return None
    tail = text[-250:]
    for pat in FOOTER_PATTERNS:
        m = pat.search(tail)
        if m:
            try:
                n = int(m.group(1))
                if 1 <= n <= 9999:
                    return n
            except (ValueError, IndexError):
                continue
    return None


def calculate_offset(
    pdf_path: Path,
    *,
    max_pages_to_scan: int = 600,
    start_page: int = 0,
) -> OffsetResult:
    """Walk PDF pages, extract footer numbers, vote for the most common offset.

    Args:
        pdf_path: PDF file
        max_pages_to_scan: hard cap (avoid scanning 1000+ page monsters)
        start_page: start scanning from this PDF page index (skip cover if known)

    Returns:
        OffsetResult. offset is None if vote was inconclusive (e.g. < 5 samples).
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return OffsetResult(offset=None, samples=0, confidence=0.0,
                            method="failed: pypdf unavailable")

    try:
        reader = PdfReader(str(pdf_path), strict=False)
    except Exception as exc:
        return OffsetResult(offset=None, samples=0, confidence=0.0,
                            method=f"failed: {exc}")

    total = len(reader.pages)
    end = min(total, start_page + max_pages_to_scan)

    votes: Counter[int] = Counter()
    samples = 0
    scanned = 0

    for i in range(start_page, end):
        scanned += 1
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception:
            continue
        printed = _extract_footer_page_num(text)
        if printed is None:
            continue
        delta = printed - i  # printed_page - pdf_index
        # Sanity: offset should be in [-5, +50] range (前置页不多)
        if -5 <= delta <= 50:
            votes[delta] += 1
            samples += 1

    if samples < 5:
        return OffsetResult(offset=None, samples=samples,
                            confidence=samples / max(1, scanned),
                            method="footer_vote (insufficient samples)")

    # Pick the mode
    offset, count = votes.most_common(1)[0]
    confidence = count / samples
    # If the top vote has less than 60% of samples, suspicious (mixed numbering)
    if confidence < 0.6:
        return OffsetResult(offset=None, samples=samples,
                            confidence=confidence,
                            method="footer_vote (low agreement)")
    return OffsetResult(offset=offset, samples=samples,
                        confidence=confidence, method="footer_vote")


def printed_to_pdf(printed_page: int, offset: int) -> int:
    """Convert a printed page number to 0-indexed PDF page."""
    # offset = printed - pdf_index  =>  pdf_index = printed - offset
    return printed_page - offset
