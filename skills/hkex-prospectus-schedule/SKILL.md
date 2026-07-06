---
name: hkex-prospectus-schedule
description: "招股书档位表 skill：从招股书 PDF 抽「申请股数 → 应缴款项」档位表。文本直抽首选（PyMuPDF + 比值众数过滤，零成本）；不足则 doubao vision 兜底，再不足则 LLM 兜底。输出 5 列：组别/股数/金额/中签率/平均分配（后两列留空给 allotment-basis 反填）。是中签率计算的核心输入。"
disable-model-invocation: true
metadata:
  hermes:
    tags: [finance, pdf, schedule, winrate, mineru, vision]
    category: research
platforms: [linux, macos, windows]
---

# 招股书档位表 skill（hkex-prospectus-schedule）

从招股书 PDF 抽**申请股数 → 应缴款项**档位表，输出中签率计算所需的标准 5 列结构：

| 组别 | 股数 | 金额 | 中签率 | 平均分配 |
|---|---|---|---|---|
| 甲组 | 500 | 5,131.24 | （留空） | （留空） |
| 甲组 | 1,000 | 10,262.48 | | |

后两列由 `hkex-allotment-basis` 抽到的甲组分配基准表反填，最终用于中签率分析。

## When to Use

- "X 招股书的档位表" / "X 的入场费档位" / "抽 X 的申请股数对应金额"
- "X 中签率分析"（前置：要先有档位表）
- "X 的认购成本表"

## 三路抽取流水线

```
招股书 PDF
   │
   ▼
PyMuPDF 关键词定位（申請認購/應繳款項/股份數目/入場費 至少命中 2 个）
   │
   ├── 文本路径（首选）──► 比值众数过滤（容差 2%）──► 行数 ≥ 10？
   │                                                       │
   │                                          NO ┌──────────┴──────────┐
   │                                            ▼                     ▼
   │                                  doubao vision            LLM 兜底
   │                                  （PNG + JSON）         （页面文本 + JSON）
   │                                            └──────────┬──────────┘
   │                                                       │
   ▼                                                       │
校验：单调性 + 金额 ≈ 股数 × offer_price × 1.0085 ◄──────┘
   │
   ▼
winrate_schedule_fields.json
```

**为什么文本路径优先**：招股书档位表是**强线性结构**——每行金额 = 股数 × 单价 × 1.0085（含经纪 1% + 交易征费 0.0027% + 联交所 0.00565% + 会财局 0.00015%）。这种关系使得：

1. 用 PyMuPDF 抽所有数字对 `(N, M)`
2. 算 `ratio = M/N`
3. 量化到 0.5 一档取**众数**（绝大多数对的 ratio 应该相等）
4. 过滤偏差 > 2% 的对（噪声）

零 LLM 成本，准确率与 vision 接近（HKIPO 项目里实测过的设计）。

## 用法

```bash
# 推荐用法（带 offer-price 做线性校验）
python skills/hkex-prospectus-schedule/scripts/extract_schedule.py \
    --pdf data/companies/02335_陝西麥科奧特醫藥-B/docs/招股書.pdf \
    --offer-price 18.20 \
    --out data/companies/02335_陝西麥科奧特醫藥-B/info/winrate_schedule_fields.json

# 不带 offer-price（仍可工作，但缺线性校验，confidence 会降）
python skills/hkex-prospectus-schedule/scripts/extract_schedule.py \
    --pdf 招股書.pdf --out schedule.json

# 只调二次校验
python skills/hkex-prospectus-schedule/scripts/verify_schedule.py \
    --input schedule.json --out verify.json
```

**输出目录约定**：写到 `info/winrate_schedule_fields.json`，被中签率阅读 skill 消费。

## 字段定义

每行 5 列，后两列留空：

| 列 | 字段 | 来源 |
|---|---|---|
| 1 | 组别 | 招股书通常不区分，留空，由 allotment 反填 |
| 2 | 股数 | 本 skill 抽取 |
| 3 | 金额（港元） | 本 skill 抽取 |
| 4 | 中签率 | 留空，由 allotment-basis 的"获配发占所申请百分比"反填 |
| 5 | 平均分配 | 留空，由 allotment-basis 反填 |

## 引擎选择

| 引擎 | 何时用 | 成本 |
|---|---|---|
| `text` | 文本路径有 ≥10 行通过校验 | 0（本地） |
| `vision` | 文本路径行数不足 | 每页 1 次 doubao vision |
| `llm` | vision 也失败 | 每页 1 次 LLM 调用 |
| `none` | 关键词完全未命中 | 0 |

输出 JSON 的 `engine` 字段标明实际用的引擎。

## .env 必填

```bash
ARK_API_KEY=ark-...                  # 仅在需要 vision 兜底时
ARK_VISION_MODEL=doubao-seed-1-6-vision-250815
LLM_API_KEY=...                      # 仅在需要 LLM 兜底时
LLM_MODEL=glm-5.2
```

## 设计参考

直接搬用 [`D:\AI项目\HKIPO`](D:\AI项目\HKIPO) 项目里 `hkipotool/allotment_parser/scripts/extract_schedule_pdf.py` 的设计。原始算法在 HKIPO 项目已验证有效。

## 已知限制

- **多张档位表合并**：招股书有时分 A 股/H 股各一张档位表，本 skill 默认只取众数最显著的那张
- **价格未提供时无法做线性校验**：传 `--offer-price` 是强烈建议
- **关键词漏命中**：极少数招股书用"申請手數"而非"申請認購"，可手动加 `--max-pages 10` 扫更多页或调关键词列表
