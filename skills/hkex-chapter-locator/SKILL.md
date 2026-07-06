---
name: hkex-chapter-locator
description: "章节定位 + PDF 切片工具。把'招股书第 X 章节'转换为'PDF 第 N-M 页'，并切出子 PDF。三层定位：书签直读 / 目录 LLM 解析+页脚偏移 / 手动页范围。专为局部精读场景设计（不读全本）。"
disable-model-invocation: true
metadata:
  hermes:
    tags: [finance, pdf, locator, chapter, slice]
    category: research
platforms: [linux, macos, windows]
---

# 章节定位 + PDF 切片工具

把"我要招股书的财务资料章节"这种**语义请求**转换成**"PDF 第 N-M 页"**或**"已切好的子 PDF 文件"**，让 Skill A/B 只精读局部、不读全本。

## Why This Exists

招股书动辄 400-600 页，全本处理代价高：
- Skill A 全本 → 表格散架严重，财务章节基本不可用
- Skill B 全本 → 烧 MinerU 额度（每日 1000 页上限，一家招股书就吃掉一半）+ 30 分钟等待
- Skill C 全本 → Markdown 截断到 120k 字符，**尾部财务报表被砍**

**解决方案**：精读前先定位"我要的章节在哪几页"，切出子 PDF，再让 Skill A/B/C 处理这个 30-80 页的小文件。

## 三层定位策略（按可靠性递减）

| 层 | 方法 | 准确率 | 是否需要 LLM | 适用 |
|---|---|---|---|---|
| 1 | PDF 书签（outline）直读 | ★★★★★ | 否 | PDF 自带书签（HKEX 约 60-80%） |
| 2 | 目录 LLM 解析 + 页脚投票算偏移量 | ★★★★ | 是 | 无书签 / 书签残缺 |
| 3 | 手动 `--pages N-M` | ★★★★★ | 否 | 用户已知页范围（最稳） |

工具自动尝试 1 → 2，3 由用户主动调用。

## 关键概念：三套页码体系（务必看 [`references/page-number-systems.md`](references/page-number-systems.md)）

招股书有三种"页码"，互不对应：
- **PDF 物理页码**（0-indexed）—— 程序用这套
- **封面/前言印刷页码**（罗马 i/ii/iii 或无）—— 前置
- **正文印刷页码**（阿拉伯 1/2/3）—— 目录写的是这套

**层 2 的关键**是自动算偏移量（前置页数）= `PDF 物理页码 − 印刷页码`，用"页脚投票"算法在所有页扫描印刷页码，取众数。

## When to Use

- "我要 X 公司的财务资料章节" → `--chapter "財務資料" --slice`
- "把招股书前 10 页切出来快速预览" → `--pages 0-9 --slice`
- "这家招股书有哪些章节？" → `--list`
- "诊断：库内 PDF 哪些带书签？" → `--probe-bookmarks`
- Skill A/B/C 调用方需要"局部读取"时本 skill 是前置

**不要在此工具做**：PDF → Markdown 转换（用 Skill A/B）、字段抽取（用 Skill C）。

## Procedure

### 1. 安装依赖

```bash
pip install -r skills/hkex-chapter-locator/scripts/requirements.txt
```

### 2. 探测书签覆盖率（一次性诊断）

```bash
python skills/hkex-chapter-locator/scripts/locate_chapter.py --probe-bookmarks
```

输出每家公司的 PDF 是否带书签。覆盖率 > 60% 时层 1 主路径就够用。

### 3. 定位章节并切片

```bash
# 列出某公司招股书的章节（书签或目录）
python locate_chapter.py --company 06951 --list

# 找"財務資料"章节并切出子 PDF
python locate_chapter.py --company 06951 --chapter "財務資料" --slice

# 手动指定页范围（最稳，跳过 LLM）
python locate_chapter.py --company 06951 --pages 211-290 --slice --label "財務資料"
```

### 4. 切片后的产物

切片存到 `data/companies/<code>_<name>/docs/_slices/`，文件名格式：

```
<原PDF名>_p<start>-<end>_<章节名>.pdf
```

例：`全球發售_招股_20260630_065700_p211-290_財務資料.pdf`

DB 的 `extractions` 表新增一行：`extractor='chapter_locator_v1'`、`field_name='chapter_map'`、`notes` 是 JSON（含章节名、页范围、定位方法、切片路径）。

### 5. 把切片喂给下游

切片文件可被 Skill A/B 直接处理：

```bash
# Skill A（批量，零成本）
python skills/hkex-pdf-reader-batch/scripts/batch_extract.py \
    --company 06951 \
    --pdf "data/companies/06951_三環集團/docs/_slices/全球發售_招股_..._p211-290_財務資料.pdf" \
    --label "招股書_p211-290_財務資料"

# Skill B（精准，烧额度）
python skills/hkex-pdf-reader-precision/scripts/precision_extract.py \
    --company 06951 \
    --pdf "data/companies/06951_三環集團/docs/_slices/全球發售_招股_..._p211-290_財務資料.pdf" \
    --label "招股書_p211-290_財務資料"
```

Skill A/B 会用 `--label` 覆盖输出 `.md` 文件名（避免与全本 markdown 混淆）。

### 6. 把切片 markdown 喂给 Skill C

```bash
python skills/hkex-pdf-field-extractor/scripts/extract_fields.py \
    --company 06951 \
    --fields listing_type,issue_price_range \
    --source-file "data/companies/06951_三環集團/info/招股書_p211-290_財務資料.md"
```

`--source-file` 让 Skill C 跳过自动选最长 markdown，直接读这份章节切片。

## 输出示例

成功定位 + 切片：

```
=== Location ===
Chapter       : 業務
PDF pages     : 112-199 (0-indexed, 88 pages)
Printed pages : 100-186 (in 招股书印刷页码)
Source        : toc+footer
Detail        : toc match '業務' printed=100-150; offset: offset=12 (method=footer_vote, samples=234, confidence=0.95)

=== Slice ===
Wrote: data/companies/06951_三環集團/docs/_slices/全球發售_招股_..._p112-199_業務.pdf
  rel: data/companies/06951_三環集團/docs/_slices/全球發售_招股_..._p112-199_業務.pdf

Next step: feed to Skill A or B with:
  python skills/hkex-pdf-reader-batch/scripts/batch_extract.py \
    --company 06951 --pdf "..." --label "業務_p112-199"
```

## LLM 配置（仅层 2 需要）

层 2 调用 LLM 解析目录，复用 Skill C 的环境变量：

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `LLM_MODEL` | 模型名 | `glm-5.2` |
| `LLM_BASE_URL` | API base URL | — |
| `LLM_API_KEY` | API key | — |

层 1 和层 3 不需要 LLM。

## Pitfalls

- **书签可能只有顶层**：HKEX 部分招股书只给"招股章程"/"附錄"两个书签，章节定位退化为层 2。
- **页脚扫描可能失败**：扫描版 PDF（图片化）没有文本层，`extract_text()` 返回空，偏移量算法失效。这种 PDF 应该先走 OCR。
- **附录页码独立编号**（I-1, II-1）：当前算法在附录段偏移量会变，可能算偏。建议定位附录时用层 3 手动指定。
- **目录跨页**：pypdf 抽目录文本可能拼接错乱（表格被识别成空列），LLM 容错好但非 100% 准。
- **章节匹配是模糊的**：用户输入"业务"，目录写"業務"，会自动 normalize；但极特殊命名（缩写、英文混排）可能匹配不到，可手动 `--pages`。
- **切片不入 `ipo_documents`**：子 PDF 只存 `_slices/` 目录，不注册到 `ipo_documents` 表（保持原始素材表的纯净）。注册到 `extractions` 表便于追溯。

## Verification

成功运行后：
1. 切片文件存在于 `docs/_slices/`
2. `extractions` 表有 `extractor='chapter_locator_v1'` 行
3. 输出"Next step"提示了下游命令

## References

- [三套页码体系说明](references/page-number-systems.md)
- 姐妹工具：[hkex-pdf-reader-batch](../hkex-pdf-reader-batch/SKILL.md)（Skill A，接 `--pdf`）
- 姐妹工具：[hkex-pdf-reader-precision](../hkex-pdf-reader-precision/SKILL.md)（Skill B，接 `--pdf`）
- 姐妹工具：[hkex-pdf-field-extractor](../hkex-pdf-field-extractor/SKILL.md)（Skill C，接 `--source-file`）
- 项目长期规划：[docs/ROADMAP.md](../../docs/ROADMAP.md)
