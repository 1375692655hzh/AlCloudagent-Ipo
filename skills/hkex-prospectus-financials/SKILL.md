---
name: hkex-prospectus-financials
description: "招股书财务表格 skill：从财务章节 MD 抽三大报表（损益/资产负债/现金流）的 11 个关键字段 × 多年度。MinerU pipeline 表格保真 + YAML 字段配置 + row_anchor 定位 + 会计恒等式校验（资产=负债+权益、毛利率/净利率区间）。依赖 hkex-chapter-locator 切片后再调。"
disable-model-invocation: true
metadata:
  hermes:
    tags: [finance, pdf, financials, mineru, prospectus]
    category: research
platforms: [linux, macos, windows]
---

# 招股书财务表格 skill（hkex-prospectus-financials）

从招股书「财务资料」章节抽**三大报表**的 11 个关键字段，每个字段按年度对齐。

## When to Use

- "X 招股书的营收/净利润/总资产"
- "X 最近三年的财务数据"
- "X 的现金流" / "X 的资产负债表"
- "X 的财务报表"

## 抽取流水线

```
招股书 PDF
   │
   ▼ (建议先调用)
hkex-chapter-locator ──► 切出「財務資料」章节 PDF
   │
   ▼ (Skill B pipeline 模式)
MinerU pipeline MD ──► HTML table parser
   │
   ▼
按 fields.yaml 定位三大报表（损益/资产负债/现金流）
   │
   ▼
按 row_anchor 在表里定位每行（"收入"/"毛利"/"年內溢利" 等）
   │
   ▼
按表头识别"2023/2024/2025"年度列，对齐抽值
   │
   ▼
会计恒等式 + 利润率区间合理性检查
   │
   ▼
financials.json
```

**与 skill-2 的差异**：财务表跨页多、表头层次深，但**没有强算术约束**（不像分配基准表的"行加总=总计"），主要靠：
1. **MinerU pipeline 模式**保真（不用 vlm，避免幻觉）
2. **YAML 配置驱动**：每个字段配 row_anchor，加字段不改代码
3. **多年度列对齐**：从表头识别"2023/2024/2025"，把每年抽成独立值
4. **会计恒等式**：资产 = 负债 + 权益（容差 1%）
5. **利润率区间**：毛利率 ∈ [0%, 100%]，净利率 ∈ [-200%, 100%]

## 用法

### 完整流程（推荐）

```bash
# Step 1: 切出财务章节
python skills/hkex-chapter-locator/scripts/locate_chapter.py \
    --company 02335 --chapter "財務資料" --slice

# Step 2: 把切片 PDF 转 MD（pipeline 模式）
python skills/hkex-pdf-reader-precision/scripts/precision_extract.py \
    --company 02335 \
    --pdf "招股書_p211-290_財務資料.pdf" \
    --label "招股書_p211-290_財務資料" \
    --model pipeline

# Step 3: 抽财务字段（本 skill）
python skills/hkex-prospectus-financials/scripts/extract_financials.py \
    --md data/companies/02335_*/info/precision/招股書_p211-290_財務資料.md \
    --fields skills/hkex-prospectus-financials/fields.yaml \
    --out data/companies/02335_*/info/financials/financials.json

# Step 4: 单独跑合理性检查
python skills/hkex-prospectus-financials/scripts/sanity_checks.py \
    --input financials.json --out sanity.json
```

### 跳过切片（直接喂整本招股书）

```bash
python skills/hkex-prospectus-financials/scripts/extract_financials.py \
    --md info/precision/招股書.md \
    --fields fields.yaml \
    --out financials.json
```

注意：整本招股书 MD 通常 > 200K 字符，财务章节位置变化大，定位成功率比切片后低。

## 字段定义

见 [`fields.yaml`](fields.yaml)。当前覆盖：

| 报表 | 字段 |
|---|---|
| 損益表 | 营业收入 / 销售成本 / 毛利 / 经营盈利 / 年内溢利 |
| 資產負債表 | 资产总额 / 负债总额 / 权益总额 |
| 現金流量表 | 经营活动现金流 / 投资活动现金流 / 融资活动现金流 |

每个字段输出多年度：

```json
{
  "revenue": {
    "name": "营业收入",
    "values": {"2023": 1234.5, "2024": 1456.7, "2025": 1789.2},
    "matched": true,
    "row": ["收入", "1,234.5", "1,456.7", "1,789.2"],
    "statement": "income_statement"
  }
}
```

## 业务规则（合理性检查）

| 规则 | 容差 | 抓什么错 |
|---|---|---|
| 資產 = 負債 + 權益 | 1% | 抽错行 / 单位不一致 / 年份错位 |
| 毛利率 ∈ [0%, 100%] | 硬阈值 | 毛利 / 营收算错 |
| 淨利率 ∈ [-200%, 100%] | 硬阈值 | 净利润 / 营收算错 |
| 經營現金流与淨利潤符號大體一致 | 50% | CFO / NP 抽错行 |

## 已知限制

- **跨页续表**：MinerU pipeline 在跨页表头丢失的场景下可能把"营业收入"和"销售成本"识别为同张表的两行，row_anchor 仍能定位，但年份列可能错位
- **单位识别**：招股书有用"百万元"/"千元"/"人民币元"的区别，本 skill 假设同一报表单位一致；跨报表对比前用户需自行核对单位
- **不依赖 doubao vision**：与 skill-2 不同，本 skill 不做视觉校验。原因：财务表跨页多、用 vision 一页页抽会失真；会计恒等式 + 利润率区间已经能抓到大多数错误
- **重组调整科目**：上市重组前的"非经常性损益"不在抽取范围，用户如需请自行加 fields.yaml 字段
