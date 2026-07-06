---
name: hkex-pdf-reader-batch
description: "PDF 批量读取工具（Skill A）：用 MarkItDown 把 docs/*.pdf 批量转 Markdown，零成本、本地、并发4。低价值大批量场景（招股书入库、历史回填）专用；深度分析请用姐妹工具 hkex-pdf-reader-precision。"
disable-model-invocation: true
metadata:
  hermes:
    tags: [finance, pdf, extraction, markitdown, batch]
    category: research
platforms: [linux, macos, windows]
---

# PDF 批量读取工具（Batch PDF Reader，Skill A）

把 `companies/<code>_<name>/docs/*.pdf` **批量**转成 Markdown，存到 `info/<pdf_stem>.md`，注册到 `extractions` 表（`extractor='markitdown_batch_v1'`）。

本 skill 与姐妹工具 [`hkex-pdf-reader-precision`](../hkex-pdf-reader-precision/SKILL.md)（Skill B，MinerU 精准）和 [`hkex-pdf-field-extractor`](../hkex-pdf-field-extractor/SKILL.md)（Skill C，LLM 抽字段）配套，三者共同实现 ROADMAP §4.4「公司状态补全工具」。**核心设计原则：价值分层**——本工具只服务"低价值、大批量"场景，不消耗任何云 API 额度。

## When to Use

用户提到以下任一场景时触发：

- "把所有招股书转 Markdown" / "批量入库 PDF"
- "全库 historical 回填" / "批量预处理"
- "X 公司所有素材先快速过一遍"
- "招股书太多，先把文本层抽出来"
- 用户明确说"用 MarkItDown" / "批量" / "不用花钱"

**不要在此工具询问**：单家深度分析、财务报表高保真、配发结果精读 —— 请转用 [`hkex-pdf-reader-precision`](../hkex-pdf-reader-precision/SKILL.md)（Skill B）。

## 引擎与限制

| 项 | 值 |
|---|---|
| 引擎 | **MarkItDown 基础版**（pdfplumber，纯本地） |
| 模型依赖 | 无（零 ML 模型） |
| 网络 | 无（完全离线） |
| 成本 | 0 |
| 速度 | 每本招股书 5-30 秒（取决于页数） |
| 表格精度 | 标准（合并单元格、跨页表会丢信息） |
| 公式识别 | 无（招股书不需要） |
| OCR | 无（招股书都是数字 PDF，不需要） |
| 并发 | 4（默认，`--concurrency` 可调） |

## 三件套关系图

```
docs/*.pdf（Raw 库，三个 tracker 写入）
    │
    ├──→ Skill A (本工具)        ──→ info/<stem>.md           [extractor=markitdown_batch_v1]
    │    MarkItDown，零成本       ──→ field_name=markdown_raw
    │    低价值、大批量
    │
    ├──→ Skill B (precision)     ──→ info/precision/<stem>.md [extractor=mineru_pipeline_v1]
    │    MinerU 精准 API          ──→ field_name=markdown_precision
    │    高价值、小批量（用户主动）   （不覆盖 Skill A 的输出，并行存在）
    │
    └──→ Skill C (field-extractor)
         LLM 字段抽取，优先读 Skill B 输出，回退 Skill A
         ──→ info/<field>.json   [extractor=pdf_field_v1]
         ──→ UPDATE companies SET listing_type=, confirmed_name=
```

**关键**：Skill A 与 Skill B 的输出**分目录存**（`info/` vs `info/precision/`），不互相覆盖。Agent 通过 `extractions` 表的 `extractor` 字段区分来源。

## Procedure

### 1. 安装依赖

```bash
pip install -r skills/hkex-pdf-reader-batch/scripts/requirements.txt
```

### 2. 运行批量转换

```bash
# 全库所有未处理的 PDF（跳过已处理的）
python skills/hkex-pdf-reader-batch/scripts/batch_extract.py

# 仅处理某公司
python skills/hkex-pdf-reader-batch/scripts/batch_extract.py --company 06951

# 仅处理某阶段（招股 / 已上市 / ...）
python skills/hkex-pdf-reader-batch/scripts/batch_extract.py --stage 招股

# 限制本次最多 50 个 PDF
python skills/hkex-pdf-reader-batch/scripts/batch_extract.py --limit 50

# 强制重跑（默认跳过已处理）
python skills/hkex-pdf-reader-batch/scripts/batch_extract.py --force

# 预览（不执行）
python skills/hkex-pdf-reader-batch/scripts/batch_extract.py --dry-run

# 自定义并发
python skills/hkex-pdf-reader-batch/scripts/batch_extract.py --concurrency 8
```

### 3. 输出位置

- Markdown：`data/companies/<code>_<name>/info/<pdf_stem>.md`
- DB 注册：`extractions` 表新增一行，`field_name='markdown_raw'`，`extractor='markitdown_batch_v1'`，`source_pdf_hash` 关联到 `ipo_documents.url_hash`
- Manifest 自动刷新：`data/manifest.json` 与 `data/companies/<code>_<name>/company.json` 的 `extractions` 段会包含新条目

## Reading the Output

| Agent 想做的事 | 读什么 |
|---------------|--------|
| "X 公司招股书转完了吗？" | `company.json.extractions[]` 找 `extractor='markitdown_batch_v1'` |
| "X 公司招股书的 Markdown 路径" | 同上，取 `output_path` |
| "哪些公司还没批量处理？" | `manifest.json.companies[]` 过滤 `extractions` 不含 `markdown_raw` 的 |
| "Skill A 与 Skill B 都跑了，看哪个版本" | `company.json.extractions[]` 按 `extractor` 区分 |

## Pitfalls

- **表格保真度低**：MarkItDown 用 pdfplumber，复杂合并单元格、跨页表格会错位。如果下游需要精确数字（如招股价、持股比例），请配合 Skill B 或 Skill C 的数值校验。
- **跳过逻辑基于 `(stock_code, source_pdf_hash)`**：同一 PDF 重跑默认跳过。如果 PDF 文件被替换但 url_hash 没变（罕见），需要 `--force`。
- **PDF 不在 ipo_documents 表中也能处理**：脚本会扫盘 `docs/*.pdf`，对 DB 里没记录的 PDF 用路径 hash 兜底（这是为了支持手动放进 docs/ 的素材）。但这种情况下 `source_pdf_hash` 不是真实的 url_hash，可能与 Skill B 不对齐。
- **大文件可能慢**：300+ 页招股书单本 30 秒以上。并发 4 时 4 本同时跑可能占满 CPU。
- **失败不中断**：单个 PDF 转换失败会记录到 stderr 但不影响其他 PDF。结束时看 `Summary: X new, Y failed`。
- **首次运行需先有 DB**：本 skill 不抓取 PDF，只处理 `docs/` 下已有的。先跑某个 tracker（如 `hkex-offering-tracker`）建立 DB 和 docs/。

## 与 Skill C 的协作

Skill C（`hkex-pdf-field-extractor`）默认**优先读 `info/precision/`（Skill B 输出），回退到 `info/`（本工具输出）**。所以本工具跑完后，Skill C 即可工作（精度稍低），后续用户对个别公司做 Skill B 后，Skill C 重跑会自动用上高精度版本。

## Verification

成功运行后：

1. `data/companies/<code>_<name>/info/<pdf_stem>.md` 存在
2. `company.json.extractions[]` 包含 `extractor='markitdown_batch_v1'` 条目
3. 终端输出 `Summary: X new, Y failed`，`Y == 0` 为成功

## References

- 姐妹工具：[hkex-pdf-reader-precision](../hkex-pdf-reader-precision/SKILL.md)（Skill B，MinerU 精准）
- 姐妹工具：[hkex-pdf-field-extractor](../hkex-pdf-field-extractor/SKILL.md)（Skill C，LLM 抽字段）
- 共享 schema：[hkex-offering-tracker/references/state-machine.md](../hkex-offering-tracker/references/state-machine.md)
- 项目长期规划：[docs/ROADMAP.md](../../docs/ROADMAP.md) §4.4
