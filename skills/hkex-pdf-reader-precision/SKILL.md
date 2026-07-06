---
name: hkex-pdf-reader-precision
description: "PDF 精准读取工具（Skill B）：用 MinerU 精准 API 把 docs/*.pdf 高保真转 Markdown，输出到 info/precision/。默认 pipeline 模型（财务数字零幻觉），配发结果/复杂嵌套表格场景可用 --model vlm。高价值小批量场景（深度分析、财务报表精读、配发结果精读）专用；批量入库请用姐妹工具 hkex-pdf-reader-batch。"
disable-model-invocation: true
metadata:
  hermes:
    tags: [finance, pdf, extraction, mineru, precision]
    category: research
platforms: [linux, macos, windows]
---

# PDF 精准读取工具（Precision PDF Reader，Skill B）

把 `companies/<code>_<name>/docs/*.pdf` 用 **MinerU 精准 API**（pipeline 模型）转换，输出到 `info/precision/<pdf_stem>.md`，注册到 `extractions` 表（`extractor='mineru_pipeline_v1'`）。

本 skill 与姐妹工具 [`hkex-pdf-reader-batch`](../hkex-pdf-reader-batch/SKILL.md)（Skill A，MarkItDown）和 [`hkex-pdf-field-extractor`](../hkex-pdf-field-extractor/SKILL.md)（Skill C，LLM 抽字段）配套。**核心设计原则：价值分层**——本工具只服务"高价值、小批量"场景，用户**主动**触发，不自动批量。

## When to Use

用户提到以下任一场景时触发：

- "对 X 公司做深度分析" / "精读 X 公司招股书"
- "把 X 公司的财务报表抽准" / "财务表必须保真"
- "X 公司配发结果公告精读"（中签数字关键）
- "用 MinerU 处理" / "高精度转换"
- 用户明确表达"质量优先" / "不怕慢"

**不要在此工具询问**：全库批量入库、历史回填、招股书预览 —— 请转用 [`hkex-pdf-reader-batch`](../hkex-pdf-reader-batch/SKILL.md)（Skill A）。

## 引擎与限制

| 项 | 值 |
|---|---|
| 引擎 | **MinerU 精准 API**（`/api/v4/file-urls/batch`） |
| 模型 | **pipeline**（默认，永不幻觉）或 **vlm**（配发结果/复杂嵌套表格，`--model vlm`） |
| Token | **必填**（环境变量 `MINERU_TOKEN` 或 `~/.mineru/config.yaml`） |
| 文件大小上限 | 200 MB |
| 单次页数上限 | 200 页（超限自动分段 page_ranges） |
| 批量上传 | 单 batch ≤ 50 个文件 |
| 速度 | 每页 5-15 秒（一本 250 页招股书约 20-40 分钟） |
| 表格精度 | **高**（合并单元格、跨页表都能识别） |
| 公式识别 | 关闭（招股书无公式） |
| OCR | 可选（招股书不需要，扫描件可开启） |
| 成本 | **每日 1000 页免费优先级额度**（足够 4-5 本招股书/天） |
| 隐私 | ⚠️ **PDF 上传到 mineru.net OSS**（阿里云上海） |

## Token 配置（首次必读）

获取 token：访问 https://mineru.net/apiManage/token （需登录）。

任选一种配置方式：

```bash
# 方式 1：环境变量
export MINERU_TOKEN="your_token_here"

# 方式 2：配置文件（推荐，跨会话）
mkdir -p ~/.mineru
cat > ~/.mineru/config.yaml <<EOF
token: your_token_here
EOF

# 方式 3：命令行参数
python skills/hkex-pdf-reader-precision/scripts/precision_extract.py --token ...
```

## 三件套关系图

```
docs/*.pdf（Raw 库）
    │
    ├──→ Skill A (batch)         ──→ info/<stem>.md            [extractor=markitdown_batch_v1]
    │    低价值、大批量、零成本
    │
    ├──→ Skill B (本工具)        ──→ info/precision/<stem>.md  [extractor=mineru_pipeline_v1]
    │    高价值、小批量、用户主动    （与 Skill A 输出并行存在，不覆盖）
    │
    └──→ Skill C (field-extractor)
         LLM 字段抽取，优先读 Skill B 输出，回退 Skill A
         ──→ info/<field>.json   [extractor=pdf_field_v1]
```

## Procedure

### 1. 安装依赖

```bash
pip install -r skills/hkex-pdf-reader-precision/scripts/requirements.txt
```

### 2. 配置 token（见上文）

### 3. 运行精准转换

```bash
# 处理某公司所有 PDF
python skills/hkex-pdf-reader-precision/scripts/precision_extract.py --company 06951

# 仅处理某一份 PDF
python skills/hkex-pdf-reader-precision/scripts/precision_extract.py --company 06951 \
    --pdf "全球發售_招股_20260630_065700.pdf"

# 预览分段方案（不上传、不消耗额度）
python skills/hkex-pdf-reader-precision/scripts/precision_extract.py --company 06951 --dry-run

# 自定义分段大小（默认 200，官方上限）
python skills/hkex-pdf-reader-precision/scripts/precision_extract.py --company 06951 --page-chunk 150
```

### 4. 输出位置

- Markdown：`data/companies/<code>_<name>/info/precision/<pdf_stem>.md`
- 临时工作区：`data/companies/<code>_<name>/info/precision/_work_<pdf_stem>/seg_*/`（含 layout.json、content_list.json、images/，便于 debug；可手动删）
- DB 注册：`extractions` 表新增一行，`field_name='markdown_precision'`，`extractor='mineru_pipeline_v1'`
- Manifest 自动刷新：`data/manifest.json` 与 `data/companies/<code>_<name>/company.json` 的 `extractions` 段会包含新条目

## 招股书分段处理（关键设计）

招股书通常 300-500 页，超过 MinerU 单次 200 页上限。本工具自动分段：

```
250 页招股书:
  segment 0: page_ranges="0-199"     (前 200 页，通常含摘要/风险/业务)
  segment 1: page_ranges="200-249"   (后 50 页，通常含财务/附录)

每段独立上传 + 解析 + 下载，最后拼接为一个 .md
拼接处插入 <!-- === segment N === --> 标记，便于 LLM 定位
```

**分段不破坏页码**：每段的 `full.md` 自带页码上下文，拼接后 LLM 仍可定位到具体页。

## Reading the Output

| Agent 想做的事 | 读什么 |
|---------------|--------|
| "X 公司有 precision 版本吗？" | `company.json.extractions[]` 找 `extractor='mineru_pipeline_v1'` |
| "X 公司 precision markdown 路径" | 同上，取 `output_path`（在 `info/precision/` 下） |
| "Skill A 与 Skill B 哪个先跑？" | 默认 Skill A（快、免费），Skill B 按需补 |
| "为什么某段没抽到？" | 看 `_work_<pdf_stem>/seg_<i>/` 下的 layout.json 是否含该页 |

## Pitfalls

- **默认 pipeline，配发结果用 vlm**：pipeline 模型无幻觉，是招股书/财务数字场景的安全选择。配发结果公告的甲/乙组分配基准表有嵌套表头（colspan/rowspan），pipeline 在某些 PDF 上会出现"数字撕裂"现象（如 `12,982` 被错切成 `212,982`），此时用 `--model vlm` 可避免。vlm 偶尔会"美化"内容，配发结果场景的关键数字应该靠下游 `hkex-allotment-basis` 的双源校验把关，而不是单凭 MinerU 一源。
- **每日额度有限**：每天 1000 页免费优先级。一本 300 页招股书约占 1/3 额度。耗尽后 MinerU 返回 `-60018`，本工具会停止后续 PDF。建议每日最多处理 3-4 家公司。
- **PDF 上传到云**：所有 PDF 都 PUT 到 mineru.net 的 OSS（阿里云上海）。HKEX 招股书是公开文件，但**上传动作存在**。涉及保密文件请勿用本工具。
- **分段上传重复**：一份 PDF 分 N 段处理时，会 N 次完整上传该文件到 OSS（每段单独的 PUT）。OSS PUT 幂等无副作用，但耗带宽。
- **临时目录占空间**：`_work_*/` 含每段的完整 zip 解压（layout.json、content_list.json、images/）。一本招股书可能占 50-200 MB 临时空间，可手动删。
- **网络依赖**：完全依赖 mineru.net，断网即不可用。Skill A 是离线兜底。
- **配发结果公告也走精准 API**：虽然小文件可走 Agent 轻量 API（免 token），但为简化代码，本工具统一走精准 API。配发结果公告几页，耗额度可忽略。
- **失败不中断其他 PDF**：单个 PDF 失败（除额度耗尽外）不影响同 batch 其他 PDF。

## Verification

成功运行后：

1. `data/companies/<code>_<name>/info/precision/<pdf_stem>.md` 存在
2. `company.json.extractions[]` 包含 `extractor='mineru_pipeline_v1'` 条目
3. 终端输出 `Summary: X OK, Y failed`，`Y == 0` 为成功
4. 临时目录 `_work_<pdf_stem>/` 下每段都有 `full.md`

## References

- 姐妹工具：[hkex-pdf-reader-batch](../hkex-pdf-reader-batch/SKILL.md)（Skill A，MarkItDown 批量）
- 姐妹工具：[hkex-pdf-field-extractor](../hkex-pdf-field-extractor/SKILL.md)（Skill C，LLM 抽字段）
- [MinerU 精准 API 接口规范](references/mineru-api.md)
- 共享 schema：[hkex-offering-tracker/references/state-machine.md](../hkex-offering-tracker/references/state-machine.md)
- 项目长期规划：[docs/ROADMAP.md](../../docs/ROADMAP.md) §4.4
