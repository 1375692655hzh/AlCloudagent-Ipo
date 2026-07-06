---
name: hkex-pdf-field-extractor
description: "PDF 字段抽取工具（Skill C）：用 LLM 从 Markdown 抽结构化字段（招股价/募资用途/基石/主要股东/listing_type/confirmed_name），写 info/<field>.json，关键字段反向更新 companies 表。优先读 Skill B precision 版 Markdown，回退 Skill A batch 版。"
disable-model-invocation: true
metadata:
  hermes:
    tags: [finance, pdf, llm, extraction, fields]
    category: research
platforms: [linux, macos, windows]
---

# PDF 字段抽取工具（Field Extractor，Skill C）

从已转换的 Markdown（Skill A 或 Skill B 产出）抽取结构化字段，写到 `info/<field>.json`，注册到 `extractions` 表（`extractor='pdf_field_v1'`）。**关键字段（listing_type / confirmed_name）抽到后反向 UPDATE `companies` 表**，闭合 4 维状态模型。

本 skill 是三件套的最后一环：Skill A/B 把 PDF 变成 Markdown，**本工具把 Markdown 变成结构化数据**。

## When to Use

用户提到以下任一场景时触发：

- "把 X 公司的招股价抽出来"
- "全库补 listing_type"
- "生成 X 公司的基石投资者列表"
- "X 公司募资用途分析"
- "抽主要股东"
- 用户明确说"字段抽取" / "结构化数据" / "LLM 抽取"

**不要在此工具询问**：PDF → Markdown 转换 —— 请先用 Skill A 或 Skill B。

## 数据源选择（关键）

本工具读 Markdown 的优先级：

| `--source` 参数 | 行为 |
|----------------|------|
| `auto`（默认） | 优先读 `info/precision/*.md`（Skill B）；没有则读 `info/*.md`（Skill A） |
| `precision` | 仅读 `info/precision/`（找不到直接报错） |
| `batch` | 仅读 `info/`（找不到直接报错） |

**推荐 `auto`**：用户对个别公司做了 Skill B 精读后，重跑本工具会自动用上高精度版本。

## 支持的字段

跑 `--list` 查看：

```bash
python skills/hkex-pdf-field-extractor/scripts/extract_fields.py --list
```

当前内置：

| 字段 | 含义 | 反向更新 companies？ |
|------|------|--------------------|
| `listing_type` | AH / 非-AH / H 股 / 红筹 | ✅ 是 |
| `issue_price_range` | 招股价区间（如 8.50-9.20 HKD） | 否 |
| `use_of_proceeds` | 募资用途及占比（数组） | 否 |
| `cornerstone_investors` | 基石投资者列表 | 否 |
| `top_shareholders` | 前 5-10 大股东 | 否 |
| `confirmed_name` | 正式股份简称（繁体） | ✅ 是 |

字段定义、prompt 模板、校验规则见 [`scripts/field_dictionary.py`](scripts/field_dictionary.py) 与 [`references/field-dictionary.md`](references/field-dictionary.md)。

## LLM 配置（OpenAI 兼容）

本工具用 OpenAI 客户端，兼容任何 OpenAI-compatible API：

| 环境变量 | 含义 | 示例 |
|---------|------|------|
| `LLM_MODEL` | 模型名 | `glm-5.2`（默认）、`MiniMax-M3`、`gpt-4o` |
| `LLM_BASE_URL` | API base URL | GLM/MiniMax/其他兼容服务的端点 |
| `LLM_API_KEY` | API key | 你的 key |

或用命令行参数 `--model`、`--base-url`、`--api-key` 覆盖。

## Procedure

### 1. 安装依赖

```bash
pip install -r skills/hkex-pdf-field-extractor/scripts/requirements.txt
```

### 2. 配置 LLM 环境变量

```bash
export LLM_MODEL="glm-5.2"
export LLM_BASE_URL="https://..."
export LLM_API_KEY="sk-..."
```

### 3. 先跑 Skill A 或 Skill B 把 PDF 转成 Markdown

```bash
# 推荐：先全库批量（Skill A）
python skills/hkex-pdf-reader-batch/scripts/batch_extract.py

# 或对个别公司精读（Skill B）
python skills/hkex-pdf-reader-precision/scripts/precision_extract.py --company 06951
```

### 4. 跑字段抽取

```bash
# 全字段
python skills/hkex-pdf-field-extractor/scripts/extract_fields.py --company 06951

# 仅抽某几个字段
python skills/hkex-pdf-field-extractor/scripts/extract_fields.py --company 06951 \
    --fields listing_type,issue_price_range

# 强制用 Skill A 输出（不走 precision）
python skills/hkex-pdf-field-extractor/scripts/extract_fields.py --company 06951 --source batch

# 列出所有可用字段
python skills/hkex-pdf-field-extractor/scripts/extract_fields.py --list

# 预览计划不调 LLM
python skills/hkex-pdf-field-extractor/scripts/extract_fields.py --company 06951 --dry-run
```

### 5. 输出位置

- 字段 JSON：`data/companies/<code>_<name>/info/<field>.json`
- DB 注册：`extractions` 表新增一行，`extractor='pdf_field_v1'`，`field_name=<field>`
- Manifest 自动刷新
- 关键字段（listing_type / confirmed_name）同步 UPDATE `companies` 表

字段 JSON 示例：

```json
{
  "field_name": "issue_price_range",
  "value": "8.50-9.20 HKD",
  "extracted_at": "2026-07-05T...",
  "source_label": "precision",
  "model": "glm-5.2",
  "needs_review": false,
  "validation_errors": []
}
```

## 设计原则（关键）

### 1. **按字段单独提问，不整本总结**

每个字段用独立的 prompt 调 LLM，prompt 严格约束 JSON 输出。这比"读完整本招股书然后总结所有字段"**幻觉率低一个数量级**。

### 2. **数值字段做校验**

- `issue_price_range` 必须含数字
- `use_of_proceeds` 各项百分比之和必须在 90-110% 区间（容许四舍五入）
- `top_shareholders` 必须是非空数组，每项含 name
- 校验失败的写入 `needs_review=true`，不阻塞流程，但 Agent 看到该标记会触发人工复核

### 3. **关键字段反向更新**

`listing_type` 和 `confirmed_name` 抽到且校验通过后，UPDATE `companies` 表对应列。这让 4 维状态模型从「抓取工具给初始值」升级到「PDF 工具给确认值」（ROADMAP §4.4 的核心目标）。

### 4. **Markdown 长度限制**

LLM 上下文有限（GLM-5.2 / gpt-4o 等 128k token），本工具把 Markdown 截断到 **120,000 字符**（约 30-40k 中文 token）。招股书大部分关键信息（封皮、风险因素、业务、募资用途、基石、主要股东、财务摘要）都在前 200 页内，截断后仍能抽出关键字段。完整财务报表深读请配 Skill B 分段后单独处理。

## Reading the Output

| Agent 想做的事 | 读什么 |
|---------------|--------|
| "X 公司 listing_type 是什么？" | `companies.listing_type`（已 update）或 `company.json.extractions[]` 找 `field_name='listing_type'` |
| "X 公司招股价？" | `companies/<code>_<name>/info/issue_price_range.json` |
| "哪些字段需要人工复核？" | 各 `info/<field>.json` 的 `needs_review=true` |
| "X 公司抽了哪些字段？" | `company.json.extractions[]` 过滤 `extractor='pdf_field_v1'` |

## Pitfalls

- **必须先跑 Skill A 或 Skill B**：本工具只读 `info/*.md`，没有 Markdown 直接报错。
- **截断可能漏字段**：招股书 >120k 字符时，超长部分被截掉。财务报表附录的细节可能丢，但主要字段不受影响。
- **LLM 幻觉不可完全消除**：即便校验通过，仍建议关键字段（issue_price、listing_type）人工抽检。
- **基础模型非 vlm**：本工具调的是 LLM 文本模型，**不是 vlm 视觉模型**。视觉模型用于 OCR/图像识别，文本字段抽取用 LLM 文本模型即可。
- **API 成本**：每字段一次 LLM 调用，6 个字段 = 6 次调用。glm-5.2 等国内模型费用极低，但仍按 token 计费。
- **JSON 解析容错**：LLM 偶尔输出非严格 JSON（带 ```json 代码块、多余文字），本工具用 regex 兜底解析。如果还失败，标 `needs_review` 让人看原文。
- **重跑覆盖**：同字段重跑会覆盖 `info/<field>.json` 与 DB 行（key = stock_code + field_name）。如需保留历史版本，手动备份。

## Verification

成功运行后：

1. `data/companies/<code>_<name>/info/<field>.json` 存在
2. `company.json.extractions[]` 含 `extractor='pdf_field_v1'` 条目
3. 若抽到 listing_type / confirmed_name 且校验通过，`companies` 表对应列已更新
4. 终端输出 `Summary: X ok, Y failed, Z needs review`

## References

- 姐妹工具：[hkex-pdf-reader-batch](../hkex-pdf-reader-batch/SKILL.md)（Skill A）
- 姐妹工具：[hkex-pdf-reader-precision](../hkex-pdf-reader-precision/SKILL.md)（Skill B）
- [字段词典与校验规则](references/field-dictionary.md)
- 共享 schema：[hkex-offering-tracker/references/state-machine.md](../hkex-offering-tracker/references/state-machine.md)
- 项目长期规划：[docs/ROADMAP.md](../../docs/ROADMAP.md) §4.4
