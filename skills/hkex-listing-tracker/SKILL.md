---
name: hkex-listing-tracker
description: "配发结果抓取工具：抓取 HKEX 新上市股份配发结果页面，下载配发结果公告 PDF。仅覆盖 IPO 生命周期的『已上市』阶段；递表聆讯招股见姐妹工具 hkex-application-tracker / hkex-offering-tracker。默认仅填充已跟踪公司的配发结果，避免供股/配售污染。"
disable-model-invocation: true
metadata:
  hermes:
    tags: [finance, scraping, ipo, hkex, listing, allotment]
    category: research
platforms: [linux, macos, windows]
---

# 配发结果抓取工具（HKEX Listing Tracker）

抓取港交所「新上市股份配發結果」页面，识别处于「已上市」状态的公司，下载配发结果公告 PDF。

本 skill 仅覆盖 IPO 生命周期中的「已上市」阶段（招股后的最终一步）。**递表、聆讯、招股阶段的数据请使用姐妹工具 [`hkex-application-tracker`](../hkex-application-tracker/SKILL.md) 与 [`hkex-offering-tracker`](../hkex-offering-tracker/SKILL.md)**。三者共用同一个 `data/state.db`，状态机贯通（遞表 → 聆訊 → 招股 → 已上市）。

## When to Use

用户提到以下任一场景时触发：

- "抓配发结果" / "下载配发公告" / "查最终发售价"
- "XX 公司上市了吗？分配结果出来了吗？"
- "我们跟踪的招股公司里有哪些已上市了"
- 处理 HKEX 链接 `hkexnews.hk/search/predefineddoc.xhtml?predefineddocuments=4`

**不要在此工具询问**：递表、聆讯、招股 —— 请转用对应的姐妹工具。

## Data Source

页面入口：`https://www1.hkexnews.hk/search/predefineddoc.xhtml?lang=zh&predefineddocuments=4`

该页面是 JSF 渲染的静态 HTML 表格（与招股页面 `predefineddocuments=6` 结构完全一致），无需 JavaScript 执行。默认显示最近 7 天（HKEX 页面右侧「显示筛选」控制，目前固定 7 天）。

PDF 路径前缀：`https://www1.hkexnews.hk/` + 表格 anchor 的 `/listedco/listconews/.../*.pdf` 相对路径。

详细字段定义见 [references/page-anatomy.md](references/page-anatomy.md)。

## State Machine

本 skill 跟踪 IPO 生命周期 4 个状态，**本工具只填充「已上市」**：

| 状态 | 数据源 | 由本工具抓取？ |
|------|--------|---------------|
| 遞表 | appindex JSON | 否（见 `hkex-application-tracker`） |
| 聆訊 | appindex JSON | 否（见 `hkex-application-tracker`） |
| 招股 | `predefineddocuments=6` | 否（见 `hkex-offering-tracker`） |
| **已上市** | `predefineddocuments=4`（本工具） | **是** |

状态推断规则见 [`hkex-offering-tracker/scripts/state.py`](../hkex-offering-tracker/scripts/state.py)（三个 skill 共享同一份规则）。

## 双层过滤（关键设计）

`predefineddocuments=4` 页面会**混杂三类**配发相关公告，本工具必须用双层过滤避免污染数据库：

| 过滤层 | 检查项 | 通过条件 | 目的 |
|--------|--------|---------|------|
| **Layer 1: 文件类型** | `doc_type` | ∈ `LISTING_SOURCE_WHITELIST`（配發結果 / 新上市股份配發結果 / 最終發售價及配發結果公告 等） | 排除供股、配售、公开增发等非 IPO 文件 |
| **Layer 2: 已跟踪** | `stock_code` | 已在 `companies` 表中（之前经递表/聆讯/招股被跟踪） | 排除老公司的配股操作（与新股 IPO 无关） |

也就是说：**本工具只推进我们跟踪过的公司从「招股 → 已上市」**。如果一家公司从没经过递表/聆讯/招股阶段被本系统跟踪过，它的配发结果不会被抓。

调试时可用 `--include-unknown` 关掉 Layer 2（仅 debug，正常不要用，会污染数据库）。

Layer 1 还有一个**负向过滤**：即使 `doc_type` 命中白名单关键词，只要标题里含「供股」「配售」「公開發售增發」就一律拒绝。例如「供股結果（包括補償安排）」会被过滤掉。

## Procedure

### 1. 安装依赖（首次或环境更新时）

```bash
pip install -r skills/hkex-listing-tracker/scripts/requirements.txt
```

### 2. 运行抓取

```bash
python skills/hkex-listing-tracker/scripts/fetch_listings.py
```

脚本会：

1. 抓取 `predefineddocuments=4` 页面 HTML
2. 解析表格行（与 offering-tracker 同一解析逻辑）
3. **双层过滤**：仅保留 `doc_type` 在白名单且 `stock_code` 已在 DB 中的行
4. 流式下载新 PDF（并发上限继承 common.py）
5. UPSERT 共享 SQLite 状态库（`data/state.db`，与其他两个工具共用）
6. 导出 JSON 三件套到 `data/`

可选参数：

- `--data-dir DIR` — 自定义数据目录（默认 `<repo>/data`）
- `--dry-run` — 解析 + 过滤 + 打印，不下载、不写库
- `--include-unknown` — 关掉 Layer 2 过滤（debug，**正常不要用**）
- `--window 7d` — 时间窗（预留，目前固定 7 天）

### 3. 读取结果（agent 主接口）

agent 永远读 JSON，不查数据库。本 skill 与其他两个 tracker 共享同一个 `manifest.json`。

**全量概览** — 读 [`data/manifest.json`](../../../data/manifest.json)：

```json
{
  "by_stage": {"遞表": 525, "聆訊": 16, "招股": 16, "已上市": 3},
  "companies": [
    {"stock_code": "06951", "company_name": "...",
     "listing_stage": "已上市", ...}
  ]
}
```

**单公司详情** — 读 `data/companies/<code>_<name>/company.json`，其中 `documents[]` 会包含该公司的全生命周期 PDF（递表 → 聆讯 → 招股 → 配发结果）。

**三库架构（v2.2）** — 公司库分三层存储，详见姐妹工具的 [state-machine.md](../hkex-offering-tracker/references/state-machine.md)：

| 库 | 位置 | 内容 | 由本工具维护？ |
|---|---|---|---|
| Raw 素材库 | `companies/<code>_<name>/docs/` | 配发结果 PDF | ✅ **本工具写入** |
| Derived 信息库 | `companies/<code>_<name>/info/` | PDF 提取的结构化数据 | 否（PDF 读取工具） |
| Analysis 报告库 | `companies/<code>_<name>/reports/` | 人工/AI 分析报告 | 否（你/AI 写） |

每公司的 `company.json` 同时索引三库（含 DB 注册条目 + 文件系统扫描容错），Agent 单点拿到全貌。跨公司聚合视图在 `data/views/by_stage.json`、`by_method.json`、`by_type.json`。

## Reading the Output

| Agent 想做的事 | 读什么 |
|---------------|--------|
| "哪些招股公司已经上市了？" | `manifest.json` → 过滤 `listing_stage == "已上市"` |
| "XX 公司分配结果出来了吗？" | `companies/<code>_<name>/company.json` → 查 `documents[]` 是否含 `配發結果*` |
| "XX 公司最终发售价？" | 该公司 `company.json.documents[].local_path` 找 `最終發售價及配發結果公告*` PDF → 由 PDF 读取工具提取 |
| "招股 → 上市的转换" | `state_history` 表（通过 company.json.state_history）看 stage 变化 |

## Pitfalls

- **双层过滤是设计意图**：本工具**故意**只跟踪我们跟踪过的公司。如果用户问"为什么某公司配发结果没抓到"，先看它是否在 DB 中（即是否经过递表/聆讯/招股被本系统跟踪）。
- **供股/配售易混淆**：`predefineddocuments=4` 同页面会混入老公司的供股结果、配售公告。这些 doc_type 标题含「供股」「配售」，会被 Layer 1 负向过滤拒绝。即便含「配發結果」字样也拒（如「供股結果（包括補償安排）」）。
- **7 天窗口限制**：HKEX 该页面默认只显示最近 7 天。如果你跟踪的公司跨越了 7 天窗口（例如某公司在第 8 天才出配发结果），会漏抓。后续可加 `--window` 参数与 JSF POST 扩展，详见 [references/page-anatomy.md](references/page-anatomy.md)。
- **繁体匹配**：HKEX 页面是繁体中文，过滤关键词用繁体（「配發結果」非「配发结果」）。脚本已对两者都匹配。
- **公司名非法字符**：繁体公司名可能含 `/`、`:` 等，`build_company_dir` 已替换为 `_`，但 agent 引用路径时仍需注意。
- **状态字段命名**：本工具更新 `listing_stage` 为「已上市」（同时镜像到 `current_state` 兼容旧 agent）。`listing_type` / `listing_method` / `confirmed_name` 继承自前序阶段，本工具不修改。
- **GEM 板 URL 路径不同**：`predefineddocuments=4` 含 GEM 板配发结果，其 PDF URL 路径含 `/gem/` 而非 `/sehk/`，脚本会自动识别，无需特殊处理。

## Verification

成功运行后：

1. `data/manifest.json` 存在，`by_stage.已上市 > 0`（仅当有招股公司在 7 天内上市）
2. `data/companies/<code>_<name>/docs/` 下出现 `配發結果_已上市_*.pdf`
3. 终端输出 `Filter stats: {total: N, kept: K, ...}` 与 `Summary: X new, Y skipped, Z failed`
4. 若 `kept=0` 且 `total>0`：要么是这 7 天内没有跟踪公司上市（正常），要么是双层过滤过严（需检查 `dropped_untracked` 数）

## References

- [HKEX 配发结果页面解析](references/page-anatomy.md)
- 姐妹工具：[hkex-application-tracker](../hkex-application-tracker/SKILL.md)（递表聆讯抓取）
- 姐妹工具：[hkex-offering-tracker](../hkex-offering-tracker/SKILL.md)（招股发行抓取）
- 共享状态机定义：[hkex-offering-tracker/references/state-machine.md](../hkex-offering-tracker/references/state-machine.md)
