# 字段词典与校验规则

本文件是 [`scripts/field_dictionary.py`](../scripts/field_dictionary.py) 的可读规范。
新增字段时**先改本文件**（设计），再改 `field_dictionary.py`（实现）。

## 设计原则

1. **按字段单独提问**：每个字段独立的 prompt，强制 JSON 输出。
   不做"整本招股书总结"，幻觉率太高。

2. **三段式 prompt**：
   - 角色 + 任务定义（"你是港股 IPO 分析助手..."）
   - 输出格式约束（强制 JSON schema 示例）
   - 不确定行为（"找不到返回 null"、"不要编造"）

3. **校验规则**：
   - 用 Python 函数对 LLM 输出做格式/范围校验
   - 校验失败标 `needs_review=true`，不阻塞流程
   - Agent 看到 needs_review 触发人工复核

4. **关键字段反向更新**：
   - `listing_type` → `companies.listing_type`
   - `confirmed_name` → `companies.confirmed_name`
   - 见 `field_dictionary.COMPANIES_TABLE_FIELDS`

## 字段定义

### `listing_type`

| 项 | 值 |
|---|---|
| 中文 | AH 类型 |
| 取值 | `"AH"` / `"非-AH"` / `"H 股"` / `"红筹"` / `"待确认"` |
| 优先文档 | 招股书 |
| 反向 update | ✅ `companies.listing_type` |
| 校验 | 值必须在上述枚举内 |

判定依据：
- 文中提到 A 股 / 上交所 / 深交所 / 科创板 / AH 股 / A+H → `"AH"`
- 文中只提港股 / 香港联交所，无 A 股字样 → `"非-AH"`
- 文中提到红筹架构 / VIE → `"红筹"`
- 文中提到 H 股且无 A 股字样 → `"H 股"`
- 完全无法判断 → `"待确认"`

### `issue_price_range`

| 项 | 值 |
|---|---|
| 中文 | 招股价区间 |
| 格式 | `"<low>-<high> <currency>"` 或 `"<price> <currency>"` |
| 货币 | HKD / USD / CNY（ISO 代码） |
| 优先文档 | 招股书 / 配发结果公告 |
| 反向 update | 否 |
| 校验 | 字符串必须含至少 1 个数字 |

示例：
- `"8.50-9.20 HKD"`
- `"8.50 HKD"`（单一价时）

### `use_of_proceeds`

| 项 | 值 |
|---|---|
| 中文 | 募资用途及占比 |
| 格式 | JSON 数组 |
| 优先文档 | 招股书"募集资金用途"章节 |
| 反向 update | 否 |
| 校验 | 各项 `percentage` 之和必须在 90-110% 区间（容许四舍五入） |

每项结构：

```json
{
  "purpose": "<用途描述（简短）>",
  "percentage": <0-100 数字>
}
```

### `cornerstone_investors`

| 项 | 值 |
|---|---|
| 中文 | 基石投资者列表 |
| 格式 | JSON 数组（可为空 `[]` 表示无基石） |
| 优先文档 | 招股书"基石投资者"章节 |
| 反向 update | 否 |
| 校验 | 每项必须含 `name` 字段 |

每项结构（可选字段）：

```json
{
  "name": "<基石全名>",
  "amount_usd_m": <认购金额，百万美元>,
  "percentage": <占本次发行比例>,
  "lockup_days": <锁定期天数>
}
```

### `top_shareholders`

| 项 | 值 |
|---|---|
| 中文 | 前 5-10 大股东 |
| 格式 | JSON 数组（按 percentage 降序） |
| 优先文档 | 招股书"主要股东"章节 |
| 反向 update | 否 |
| 校验 | 非空数组，每项含 `name` |

每项结构：

```json
{
  "name": "<股东名>",
  "percentage": <0-100 数字>,
  "role": "<创始人/机构/...可选>"
}
```

### `confirmed_name`

| 项 | 值 |
|---|---|
| 中文 | 正式股份简称 |
| 格式 | 繁体中文（HKEX 标准） |
| 优先文档 | 招股书封皮 / 股份簡稱字段 |
| 反向 update | ✅ `companies.confirmed_name` |
| 校验 | 无（字符串即可） |

通常以 ` - B` / ` - W` / ` - P` 后缀结尾（如有）。

## 添加新字段

1. 在本文件加字段定义（设计）。
2. 在 `field_dictionary.py`：
   - 加 `FieldDef(...)` 条目
   - 加 validator 函数（如需校验）
   - 如需反向 update `companies`，加进 `COMPANIES_TABLE_FIELDS`
3. （可选）在 `references/` 加该字段的详细 prompt 工程笔记。
4. 跑 `extract_fields.py --fields <new_field> --company <test>` 验证。

## LLM 调用约束

| 项 | 值 |
|---|---|
| temperature | `0.0`（确定性输出） |
| system msg | "你是港股 IPO 分析助手。严格按 JSON 格式输出，不要加任何额外文字。" |
| Markdown 长度上限 | 120,000 字符（约 30-40k 中文 token） |
| 超长处理 | 头部截断（保留封皮/风险/业务/募资/股东等关键章节） |

## 校验失败的应对

校验失败的字段：
1. 仍写 `info/<field>.json`（保留 LLM 原始输出便于人工诊断）
2. `needs_review=true`
3. `validation_errors` 字段列出所有失败原因
4. **不**反向 update `companies` 表（避免污染主表）
5. Agent 看到 needs_review 应提示用户人工复核

## 字段版本管理

当前设计：同字段重跑覆盖（key = stock_code + field_name）。

如需保留历史版本（如招股书版本变更后重抽），未来可：
- 在 `extractions` 表加 `version` 列（与 `reports` 一致）
- 或在 `info/` 下加 `archive/<field>_<timestamp>.json`

当前未实现，原因：招股书版本变更罕见，覆盖即可。
