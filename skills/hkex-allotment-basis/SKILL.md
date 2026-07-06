---
name: hkex-allotment-basis
description: "配发结果公告分配基准表 skill：从配发结果 PDF 抽取甲组/乙组分配基准表 + 关键 scalar 字段（發售價/股數/認購水平/承配人數目）。MinerU vlm 主提取 + doubao vision 第二源异构校验 + 业务规则（行加总=总计、香港占≈10%、最大承配人<25%）。分歧不动数据只加 ⚠️，输出最终 MD + 校验报告。"
disable-model-invocation: true
metadata:
  hermes:
    tags: [finance, pdf, allotment, mineru, vision, doubao, verification]
    category: research
platforms: [linux, macos, windows]
---

# 配发结果公告分配基准表 skill（hkex-allotment-basis）

从配发结果公告 PDF 抽取**甲组/乙组分配基准表**及 9 个关键 scalar 字段。
针对配发结果场景**数字必须 100% 准确**的核心需求，采用**异构双源交叉校验**：
MinerU（文本结构化）vs doubao vision（视觉识图），两路一致即放行，分歧标 ⚠️ 不改值。

## When to Use

- "X 公司配发结果精读" / "抽 X 的分配基准表"
- "X 的中签率分析"（中签率基于分配基准表 + 档位表交叉算）
- "配售结果数字必须准确" / "数字校验"
- 用户明确说"配发结果" / "分配基准" / "甲组乙组"

## 双源校验流水线

```
配发结果 PDF
   │
   ├──► MinerU vlm 主提取 ──► MD ──► extract_fields.py ──► fields.json
   │                                              (HTML table parser 展开 rowspan/colspan)
   │
   └──► doubao vision 第二源 ──► verify.json
        (PyMuPDF 定位关键字段页 → 渲染 PNG → Ark doubao vision 识图)
                              │
                              ▼
                       compare.py (异构双源比对)
                              │
                       business_checks.py (业务规则)
                              │
                       render_output.py
                              │
                              ▼
                       最终 MD + 校验报告
```

**为什么是异构双源**：MinerU 走文本结构化（容易在嵌套表头出现"数字撕裂"，如 `12,982` → `212,982`），doubao vision 走整页识图（不依赖文本层）。两路犯同样错的概率极低。

**业务规则**（兜底，即使双源都错也能抓到）：

| 规则 | 容差 | 抓什么错 |
|---|---|---|
| 香港 + 国际 = 全球发售股份 | 0.1% | 股数撕裂 |
| 香港占 ≈ 10% | 1pp | 股数漏识 |
| 国际占 ≈ 90% | 1pp | 股数漏识 |
| 甲组行加总 ≈ 总计行 | 5% | 行漏 / 数字撕裂 |
| 乙组行加总 ≈ 总计行 | 5% | 行漏 / 数字撕裂 |
| 最大承配人 < 25% | 硬阈值 | 公众持股量违规 |

## 用法

### 公司目录模式（推荐）

```bash
python skills/hkex-allotment-basis/scripts/parse_allotment.py \
    --company 02335 \
    --pdf "陝西麥科奧特配售结果.pdf"
```

输出：

| 文件 | 用途 |
|---|---|
| `info/allotment_full/<pdf_stem>.md` | **最终交付**：MinerU 全文 + 顶部校验摘要 |
| `info/allotment_full/校验报告.md` | 字段分歧 + 业务规则详情 |
| `info/allotment_full/fields.json` | MinerU 抽取的 scalars + tables |
| `info/allotment_full/verify.json` | doubao vision 第二源 |
| `info/allotment_full/compare.json` | 双源比对结果 |
| `info/allotment_full/business.json` | 业务规则结果 |
| `info/allotment_full/mineru/<pdf_stem>.md` | MinerU 原始输出 |

### 任意 PDF 模式

```bash
python skills/hkex-allotment-basis/scripts/parse_allotment.py \
    --pdf-path "D:/Downloads/麦克医药配售结果.pdf" \
    --out-dir "_tmp_allotment_out"
```

### 跳过 doubao 校验（开发调试）

```bash
python skills/hkex-allotment-basis/scripts/parse_allotment.py \
    --company 02335 --skip verify
```

### 用 pipeline 模式（默认是 vlm）

```bash
python skills/hkex-allotment-basis/scripts/parse_allotment.py \
    --company 02335 --mineru-model pipeline
```

## 单步调试

```bash
# 只重跑 doubao 校验
python skills/hkex-allotment-basis/scripts/verify_vision.py \
    --pdf <pdf> --out verify.json

# 只重跑业务规则
python skills/hkex-allotment-basis/scripts/business_checks.py \
    --fields fields.json --out business.json

# 只重跑渲染
python skills/hkex-allotment-basis/scripts/render_output.py \
    --mineru-md mineru/foo.md --fields fields.json \
    --out final.md --report 报告.md
```

## 字段定义

见 [`fields.yaml`](fields.yaml)。当前覆盖：

| 类别 | 字段 |
|---|---|
| 价格 | 發售價 |
| 股本 | 全球發售股份數目 / 香港公開發售最終 / 國際配售最終 |
| 配發結果 | 有效申請數目 / 獲接納申請數目 / 認購水平（香港+國際）/ 承配人數目 |
| 表格 | 甲組分配基準 / 乙組分配基準 / 承配人集中度 |

## .env 必填

```bash
MINERU_TOKEN=...                 # https://mineru.net/apiManage/token
ARK_API_KEY=ark-...              # 火山方舟 doubao vision
ARK_VISION_MODEL=doubao-seed-1-6-vision-250815  # 可选，默认即此
```

## 校验报告示例

字段全一致：

```
## 1. 字段交叉校验
总 9 字段，一致 9，分歧 0，校验缺失 0。
✅ 所有可比字段均一致。

## 2. 表格行数校验
| 表 | MinerU 行数 | doubao 行数 | 状态 |
| allotment_basis_a | 30 | 30 | ✅ |
| allotment_basis_b | 12 | 12 | ✅ |

## 3. 业务规则校验
总 6 规则，通过 6，未通过 0。
```

字段有分歧：

```
### ⚠️ 分歧字段
**香港公開發售認購水平** (`oversub_hk`)
- MinerU 主提取：`7,181.21 倍`
- doubao vision：`7,180.21 倍`
- 说明：primary='7,181.21 倍' vs verify='7,180.21 倍'
- 建议：人工核对原文确认正确值
```

## 设计参考

本 skill 直接搬用 [`D:\AI项目\HKIPO`](D:\AI项目\HKIPO) 项目里 `hkipotool/allotment_parser` 的设计：
- [`parse_allotment.ps1`](D:\AI项目\HKIPO\hkipotool\allotment_parser\parse_allotment.ps1) → 本 skill 的 `parse_allotment.py` 编排逻辑
- [`extract_fields.py`](D:\AI项目\HKIPO\hkipotool\allotment_parser\scripts\extract_fields.py) → 本 skill 的 `extract_fields.py` + `_common/common_tables.py`
- [`verify_fields.py`](D:\AI项目\HKIPO\hkipotool\allotment_parser\scripts\verify_fields.py) → 本 skill 的 `verify_vision.py`
- [`compare.py`](D:\AI项目\HKIPO\hkipotool\allotment_parser\scripts\compare.py) → 本 skill 的 `compare.py` + `_common/common_verify.py`
- [`business_checks.py`](D:\AI项目\HKIPO\hkipotool\allotment_parser\scripts\business_checks.py) → `_common/common_verify.py:run_allotment_business_checks`
- [`render_output.py`](D:\AI项目\HKIPO\hkipotool\allotment_parser\scripts\render_output.py) → 本 skill 的 `render_output.py`

## 已知限制

- **跨页续表**：MinerU 对跨页表头丢失的密集表（甲组 25,000 股之后）可能降级为纯文本，业务规则会标"未找到總計行"并降级为"行加总仅供参考"
- **Vision 校验成本**：每页一次 vision 调用，配发结果 PDF 一般 5-15 页 → 5-15 次调用（doubao 计费）
- **MinerU 上传到云**：所有 PDF 都上传到 mineru.net OSS（公开文件可接受，保密文件慎用）
- **表格逐格比对未实现**：表格仅做行数比对 + 业务规则兜底；逐格 vision 比对成本过高
