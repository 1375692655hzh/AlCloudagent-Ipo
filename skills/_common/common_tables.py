"""common_tables — HTML <table> 解析（展开 rowspan/colspan）。

来源：HKIPO 项目 extract_fields.py 的 parse_html_table。
把 MinerU 输出的 <table><tr><td>...</td></tr></table> 摊平成二维文本矩阵。

MinerU 经常输出嵌套表头（colspan="3" + rowspan="2"），LLM 凭语义猜不准；
本解析器把所有合并单元格展开，让后续字段提取走纯文本正则。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_TAG_RE = re.compile(r"<[^>]+>")
_ATTR_RE = re.compile(r"(\w+)\s*=\s*\"?(\w+)\"?")


def _parse_attrs(attrs_str: str) -> dict:
    return dict(_ATTR_RE.findall(f"<td {attrs_str}>"))


def parse_html_table(html: str) -> list[list[str]]:
    """解析 <table>...</table>，返回展开 rowspan/colspan 后的二维文本矩阵。

    rowspan/colspan 的填充规则：
      - 单元格本身占 (col, col+colspan-1) 这 colspan 列
      - rowspan>1 时，下面 (rowspan-1) 行的同列也填同样的文本
    返回的每行长度被补齐到最长行。
    """
    rows_raw = re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S)
    if not rows_raw:
        return []

    grid: list[list[dict]] = []
    rowspan_fill: dict[int, tuple[str, int]] = {}

    for row_html in rows_raw:
        cells = re.findall(r"<td([^>]*)>(.*?)</td>", row_html, flags=re.S)
        row: list[dict] = []
        col = 0
        for attrs_str, cell_html in cells:
            attrs = _parse_attrs(attrs_str)
            rowspan = int(attrs.get("rowspan", 1))
            colspan = int(attrs.get("colspan", 1))
            text = _TAG_RE.sub("", cell_html)
            text = re.sub(r"\s+", " ", text).strip()
            # 补齐上方 rowspan 占用
            while col in rowspan_fill:
                row.append({"text": rowspan_fill[col][0], "filled": True})
                rowspan_fill[col] = (rowspan_fill[col][0], rowspan_fill[col][1] - 1)
                if rowspan_fill[col][1] <= 0:
                    del rowspan_fill[col]
                col += 1
            row.append({"text": text, "filled": False})
            for _ in range(colspan - 1):
                col += 1
                row.append({"text": text, "filled": True})
            if rowspan > 1:
                rowspan_fill[col] = (text, rowspan - 1)
            col += 1
        while col in rowspan_fill:
            row.append({"text": rowspan_fill[col][0], "filled": True})
            rowspan_fill[col] = (rowspan_fill[col][0], rowspan_fill[col][1] - 1)
            if rowspan_fill[col][1] <= 0:
                del rowspan_fill[col]
            col += 1
        grid.append(row)

    max_cols = max((len(r) for r in grid), default=0)
    out: list[list[str]] = []
    for r in grid:
        cells_str = [c["text"] for c in r]
        while len(cells_str) < max_cols:
            cells_str.append("")
        out.append(cells_str)
    return out


@dataclass
class ParsedTable:
    html: str
    rows: list[list[str]]
    context_before: list[str] = field(default_factory=list)
    start_pos: int = 0


def extract_all_tables(md: str) -> list[ParsedTable]:
    """抽所有 <table>，每个含 html/rows/context_before。"""
    tables: list[ParsedTable] = []
    for m in re.finditer(r"<table[^>]*>.*?</table>", md, flags=re.S):
        start = m.start()
        before = md[:start].rstrip()
        ctx_lines = [ln.strip() for ln in before.splitlines() if ln.strip()][-4:]
        tables.append(
            ParsedTable(
                html=m.group(0),
                rows=parse_html_table(m.group(0)),
                context_before=ctx_lines,
                start_pos=start,
            )
        )
    return tables


def flatten_md_for_scalar(md: str) -> str:
    """把 HTML table 标签转成换行，让 scalar 正则在表格内也能命中。

    <td>有效申請數目</td><td>252,640</td> → "有效申請數目\\n252,640\\n"
    """
    s = re.sub(r"</tr>", "\n", md)
    s = re.sub(r"<td[^>]*>", "", s)
    s = re.sub(r"</td>", "\n", s)
    s = _TAG_RE.sub("", s)
    return s


def _norm_for_anchor(s: str) -> str:
    """规范化用于匹配：去空格、去标点。"""
    return re.sub(r"[\s，。:：/／、（）()]+", "", s)


def match_anchor(candidate_text: str, anchor: str | list[str] | None) -> bool:
    """判断候选文本是否匹配 anchor 规范。

    - anchor 是 list[str]：any-of 模式（命中任一关键词即可）
    - anchor 是 str：all-keywords 模式（用 .* 切分，全部需命中）
    """
    if anchor is None:
        return False
    cand = _norm_for_anchor(candidate_text)
    if isinstance(anchor, list):
        return any(_norm_for_anchor(k) in cand for k in anchor)
    a = _norm_for_anchor(anchor)
    keywords = [w for w in re.split(r"\.\*", a) if w]
    if not keywords:
        return False
    return all(k in cand for k in keywords)


def find_table_by_anchor(
    tables: list[ParsedTable],
    anchor: str | list[str],
    *,
    not_anchor: str | list[str] | None = None,
    last_context_only: bool = False,
    last_context_n: int = 1,
    context_anchor: str | list[str] | None = None,
    scan_full_table: bool = True,
) -> ParsedTable | None:
    """按表头 anchor 找第一张匹配的表。

    Args:
        tables: 已解析的表列表
        anchor: 表头 anchor
        not_anchor: 排除规则，匹配的表会被跳过（用于消歧）
        last_context_only: 仅用紧邻的最后 N 行上下文
        last_context_n: 配合 last_context_only，取最后 N 行
        context_anchor: 仅对最后 1 行上下文（紧邻的 section header）单独匹配。
            用于「表头结构相同、靠前一行 section header 区分」的场景。
        scan_full_table: 把整张表的所有 row（不只 row0）拼进 candidate。
            MinerU 经常把表头切碎到多行 cell，row0 可能只是 `colspan` 占位。
            默认 True。
    """
    for t in tables:
        if not t.rows:
            continue
        if last_context_only:
            ctx = t.context_before[-last_context_n:]
        else:
            ctx = t.context_before
        if scan_full_table:
            head = " ".join(" ".join(r) for r in t.rows)
        else:
            head = " ".join(t.rows[0])
        candidate = " ".join([head] + ctx)
        if not match_anchor(candidate, anchor):
            continue
        if not_anchor is not None and match_anchor(candidate, not_anchor):
            continue
        if context_anchor is not None:
            last_ctx = t.context_before[-1] if t.context_before else ""
            if not match_anchor(last_ctx, context_anchor):
                continue
        return t
    return None


def normalize_md_for_matching(md: str) -> str:
    """预清洗：去掉中文字间空格、统一标点，让正则更鲁棒。"""
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", md)
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[，。：；、）」])", "", s)
    s = re.sub(r"(?<=[，（。：；、「])\s+(?=[\u4e00-\u9fff])", "", s)
    s = s.replace("\u3000", "")
    return s
