"""common_pdf — PyMuPDF 关键词定位 + PNG 渲染。

依赖：PyMuPDF (import fitz)
所有函数都延迟 import fitz，避免 import _common 时强依赖。
"""
from __future__ import annotations

from pathlib import Path


def _open_fitz():
    try:
        import fitz  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "PyMuPDF (fitz) required: pip install PyMuPDF"
        ) from e
    return fitz


def find_pages_by_keywords(
    pdf: Path,
    keywords: list[str],
    *,
    min_hits: int = 1,
    max_pages: int = 5,
) -> list[int]:
    """返回疑似目标页码（1-based），按命中关键词数降序。

    Args:
        pdf: PDF 路径
        keywords: 关键词列表（每个含 1 个关键词；繁/简应分别列出）
        min_hits: 单页至少命中几个关键词才算目标页
        max_pages: 最多返回多少页
    """
    fitz = _open_fitz()
    doc = fitz.open(pdf)
    scored: list[tuple[int, int]] = []
    for pno in range(len(doc)):
        text = doc.load_page(pno).get_text() or ""
        hits = sum(1 for k in keywords if k in text)
        if hits >= min_hits:
            scored.append((pno + 1, hits))
    doc.close()
    scored.sort(key=lambda x: (-x[1], x[0]))
    return [p for p, _ in scored[:max_pages]]


def render_page(pdf: Path, page_num: int, *, dpi: int = 200) -> Path:
    """渲染指定页（1-based）为 PNG，返回临时文件路径。"""
    import tempfile

    fitz = _open_fitz()
    doc = fitz.open(pdf)
    page = doc.load_page(page_num - 1)
    zoom = dpi / 72
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    out_dir = Path(tempfile.mkdtemp(prefix="hkipdf_"))
    out = out_dir / f"page{page_num}.png"
    pix.save(out)
    doc.close()
    return out


def page_count(pdf: Path) -> int:
    fitz = _open_fitz()
    doc = fitz.open(pdf)
    n = len(doc)
    doc.close()
    return n


def page_text(pdf: Path, page_num: int) -> str:
    """1-based page_num → text。"""
    fitz = _open_fitz()
    doc = fitz.open(pdf)
    text = doc.load_page(page_num - 1).get_text() or ""
    doc.close()
    return text
