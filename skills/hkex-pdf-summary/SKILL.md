---
name: hkex-pdf-summary
description: "通用文字场景 skill：从招股书/配发结果的 Markdown 抽取业务概览、行业地位、风险因素、主要股东、募资用途、基石投资者等结构化字段。完全 YAML 驱动（fields.yaml 定义字段+prompt）。演化自 hkex-pdf-field-extractor (Skill C)，新增 summary 字段类，保留 OpenAI 兼容 LLM 调用。包揽 skill-1/2/3 不处理的所有「文字场景」。"
disable-model-invocation: true
metadata:
  hermes:
    tags: [finance, pdf, llm, summary, glm, minimax, deepseek]
    category: research
platforms: [linux, macos, windows]
---

# 通用文字场景 skill（hkex-pdf-summary）

从已转换的 Markdown（Skill A 或 Skill B 产出）抽取结构化字段。**包揽 skill-1/2/3 不处理的所有场景**：

- 业务概览（300-500 字摘要）
- 行业地位（市场份额、竞争对手）
- 历史与重组
- 主要风险（前 5 大）
- 主要股东（前 5 大）
- 基石投资者
- 募资用途摘要

## When to Use

- "X 公司业务概览" / "X 是做什么的"
- "X 的主要风险" / "X 的基石投资者"
- "X 的主要股东" / "X 的募资用途"
- "X 公司历史" / "X 的重组"
- 凡是不属于「档位表 / 分配基准表 / 财务报表」的招股书内容，都走这里

## 演化自 Skill C 的差异

| 维度 | Skill C (hkex-pdf-field-extractor) | Skill 4 (hkex-pdf-summary) |
|---|---|---|
| 字段定义 | 硬编码在 `field_dictionary.py` | YAML 驱动（`fields.yaml`） |
| 字段范围 | listing_type / issue_price_range / cornerstone_investors / use_of_proceeds 等 | 上述 + 新增 summary 字段：business_overview / industry_position / history_and_restructuring 等 |
| LLM 客户端 | openai SDK | `_common/common_llm`（httpx 直调，多模型路由） |
| 输出 | `info/<field>.json` | `info/summary/<field>.json` |
| 调用方式 | 单字段单独提问（降低幻觉） | 同左 |

**两者并存**：Skill C 的 listing_type / confirmed_name 反向更新 companies 表的能力，Skill 4 暂不复制；用户需要补这些字段时仍可调 Skill C。

## 用法

### 公司模式（推荐）

```bash
# 抽全部 summary 字段
python skills/hkex-pdf-summary/scripts/extract_summary.py --company 02335

# 只抽业务概览和主要风险
python skills/hkex-pdf-summary/scripts/extract_summary.py \
    --company 02335 --only business_overview,key_risks

# 用 Skill A 的 batch MD（默认 auto 优先 precision）
python skills/hkex-pdf-summary/scripts/extract_summary.py \
    --company 02335 --source batch
```

### 任意 MD 模式

```bash
# 配合 hkex-chapter-locator 切出的章节 MD
python skills/hkex-pdf-summary/scripts/extract_summary.py \
    --source-file info/precision/招股書_p30-80_業務.md \
    --only business_overview,industry_position
```

### 列出所有字段

```bash
python skills/hkex-pdf-summary/scripts/extract_summary.py --list
```

## 字段定义

见 [`fields.yaml`](fields.yaml)。当前覆盖：

| 字段 ID | 类型 | 说明 |
|---|---|---|
| `business_overview` | scalar | 业务概览（300-500 字） |
| `industry_position` | scalar | 行业地位（市场份额/排名） |
| `history_and_restructuring` | scalar | 历史与重组（200-400 字） |
| `use_of_proceeds_summary` | scalar | 募资用途摘要 |
| `key_risks` | list | 主要风险（前 5 大） |
| `top_shareholders` | list | 主要股东（前 5 大） |
| `cornerstone_investors` | list | 基石投资者 |

## 输出

```
info/summary/
├── business_overview.json
├── industry_position.json
├── history_and_restructuring.json
├── use_of_proceeds_summary.json
├── key_risks.json
├── top_shareholders.json
└── cornerstone_investors.json
```

每个 JSON 结构：

```json
{
  "field_name": "business_overview",
  "display_name": "业务概览",
  "type": "scalar",
  "value": "...300-500 字业务概览文字...",
  "extracted_at": "2026-...",
  "source_label": "precision",
  "model": "glm-5.2"
}
```

并自动注册到 `extractions` 表（`extractor='pdf_summary_v1'`）。

## .env 必填

```bash
LLM_API_KEY=...
LLM_MODEL=glm-5.2          # 或 minimax-m3 / deepseek-chat
LLM_BASE_URL=https://...   # OpenAI 兼容 base
```

## 自定义字段

修改 `fields.yaml` 加新字段即可。每个字段需要：

- `id`: 唯一标识（写到 `info/<id>.json` 和 `extractions.field_name`）
- `name`: 中文显示名
- `type`: `scalar` 或 `list`
- `prompt`: LLM 提示词，必须指定严格 JSON 输出格式

## 设计原则

1. **单字段单独提问**：不整本招股书丢给 LLM 总结，每个字段独立调一次 LLM（降低幻觉）
2. **YAML 驱动**：加字段不改代码，改 yaml 即可
3. **多模型路由**：默认 GLM-5.2，可切 MiniMax-M3 / DeepSeek / doubao
4. **MD 截断**：超过 120K 字符的招股书会被头部截断（业务概览通常在前 1/3）
5. **重试 + 长度容错**：LLM 调用失败自动重试 2 次，响应非 JSON 会标错
