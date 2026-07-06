---
name: hkex-application-tracker
description: "递表聆讯抓取工具：抓取 HKEX 上市申请人页面背后的静态 JSON API，下载申请版本与聆讯后资料集 PDF。仅覆盖 IPO 生命周期的『遞表』『聆訊』阶段；招股发行见姐妹工具 hkex-offering-tracker。"
disable-model-invocation: true
metadata:
  hermes:
    tags: [finance, scraping, ipo, hkex, application, hearing]
    category: research
platforms: [linux, macos, windows]
---

# 递表聆讯抓取工具（HKEX Application Tracker）

抓取港交所「上市申请人」页面背后的静态 JSON，识别处于「遞表」「聆訊」阶段的公司，下载申请版本（Application Proof）与聆讯后资料集（PHIP）PDF。

本 skill 仅覆盖 IPO 生命周期中的「遞表」「聆訊」两个阶段。**招股、已上市阶段的数据请使用姐妹工具 [`hkex-offering-tracker`](../hkex-offering-tracker/SKILL.md)**。两者共用同一个 `data/state.db`，状态机贯通（遞表 → 聆訊 → 招股 → 已上市）。

## When to Use

用户提到以下任一场景时触发：

- "抓港交所递表名单" / "最近哪些公司递表了" / "上市申请人有哪些"
- "下载聆讯后资料集" / "PHIP" / "通过听证的公司"
- "XX 公司递过表吗？什么时候递的？" / "XX 走到聆讯了吗"
- 处理 HKEX 链接 `hkexnews.hk/app/appindex.html`

**不要在此工具询问**：招股书、配发结果、全球发售 —— 请转用 `hkex-offering-tracker`。

## Data Source

页面入口：`https://www1.hkexnews.hk/app/appindex.html?lang=zh`

实际数据来自 HKEX 暴露的静态 JSON 文件，无需 JavaScript 执行：

| 端点 | 板块 | 内容 |
|------|------|------|
| `ncms/json/eds/appactive_appphip_sehk_c.json` | 主板（sehk） | 递表 + 聆讯全部记录（PHIP 标志聆讯） |
| `ncms/json/eds/appactive_appphip_gem_c.json` | 创业板（gem） | 同上 |
| `ncms/json/eds/appactive_app_sehk_c.json` | 主板 | 仅申请版本（无 PHIP） |
| `ncms/json/eds/appactive_app_gem_c.json` | 创业板 | 同上 |

默认抓取 `_appphip_` 端点（覆盖递表 + 聆讯），中文版 `_c.json`。

PDF 路径前缀：`https://www1.hkexnews.hk/app/` + JSON 中的 `ls[].u1` 相对路径。

详细字段定义见 [references/json-api.md](references/json-api.md)。

## State Machine

本 skill 跟踪 IPO 生命周期 4 个状态，**本工具只填充「遞表」「聆訊」**：

| 状态 | 数据源 | 由本工具抓取？ |
|------|--------|---------------|
| **遞表** | appindex JSON 的 `申請版本` | **是** |
| **聆訊** | appindex JSON 的 `聆訊後資料集`（PHIP） | **是** |
| 招股 | `predefineddocuments=6` | 否（见 `hkex-offering-tracker`） |
| 已上市 | 新上市股份配發結果 | 预留，不抓 |

状态推断规则见 [`hkex-offering-tracker/scripts/state.py`](../hkex-offering-tracker/scripts/state.py)（两个 skill 共享同一份规则）。

## Procedure

### 1. 安装依赖（首次或环境更新时）

```bash
pip install -r skills/hkex-application-tracker/scripts/requirements.txt
```

### 2. 运行抓取

```bash
python skills/hkex-application-tracker/scripts/fetch_applications.py
```

脚本会：

1. 并行下载 2 个 JSON 端点（主板 + 创业板的 `_appphip_` 版本）
2. 合并去重，对每条记录的 `ls[]` 列表逐项推断 `doc_type → listing_stage`
3. 仅下载 `doc_type` 在白名单（申請版本 / 聆訊後資料集）的 PDF
4. UPSERT 共享 SQLite 状态库（`data/state.db`，与 `hkex-offering-tracker` 共用）
5. 导出 JSON 三件套到 `data/`

可选参数：

- `--data-dir DIR` — 自定义数据目录（默认 `<repo>/data`）
- `--dry-run` — 解析 + 打印，不下载
- `--no-gem` — 跳过创业板端点（仅抓主板）
- `--app-only` — 抓 `appactive_app_*`（仅申请版本，无 PHIP），默认是 `appphip`

### 3. 读取结果（agent 主接口）

agent 永远读 JSON，不查数据库。本 skill 与 offering-tracker 共享同一个 `manifest.json`。

**全量概览** — 读 [`data/manifest.json`](../../../data/manifest.json)：

```json
{
  "by_stage": {"遞表": 525, "聆訊": 16, "招股": 16, "已上市": 0},
  "companies": [
    {"stock_code": "APP-108261", "company_name": "立訊精密工業股份有限公司",
     "listing_stage": "聆訊", ...}
  ]
}
```

**单公司详情** — 读 `data/companies/<code>_<name>/company.json`，含完整 `state_history` 与 `documents` 清单，以及 4 维状态：`listing_stage`（已填）、`listing_method`（启发式填充，`-B`→机制B、`-P`→18C特专科、`-W`→WVR）、`confirmed_name`（=公司名）、`listing_type`（`待确认`，JSON 无此字段）。

**三库架构（v2.2）** — 公司库分三层存储，详见姐妹工具的 [state-machine.md](../hkex-offering-tracker/references/state-machine.md)：

| 库 | 位置 | 内容 | 由本工具维护？ |
|---|---|---|---|
| Raw 素材库 | `companies/<code>_<name>/docs/` | 申请版本 / 聆讯后资料集 PDF | ✅ **本工具写入** |
| Derived 信息库 | `companies/<code>_<name>/info/` | PDF 提取的结构化数据 | 否（PDF 读取工具） |
| Analysis 报告库 | `companies/<code>_<name>/reports/` | 人工/AI 分析报告 | 否（你/AI 写） |

每公司的 `company.json` 同时索引三库（含 DB 注册条目 + 文件系统扫描容错），Agent 单点拿到全貌。跨公司聚合视图在 `data/views/by_stage.json`、`by_method.json`、`by_type.json`。

## Reading the Output

| Agent 想做的事 | 读什么 |
|---------------|--------|
| "现在哪些公司递表了？" | `manifest.json` → 过滤 `listing_stage == "遞表"` |
| "哪些公司通过聆讯了？" | `manifest.json` → 过滤 `listing_stage == "聆訊"` |
| "XX 公司递表了吗？" | `manifest.json` 找该公司；或 `companies/APP-<id>_<name>/company.json` |
| "XX 公司的申请版本 PDF 路径？" | 该公司 `company.json.documents[].local_path`（`doc_type == "申請版本..."`） |
| "XX 的 PHIP 在哪？" | 该公司 `company.json.documents[].local_path`（`doc_type == "聆訊後資料集..."`） |

## Pitfalls

- **申请阶段无股份代号**：HKEX JSON 此阶段不分配 stock_code（`st` 字段仅在上市后 `applisted_*.json` 才出现）。本工具用 `APP-{applicant_id}` 作临时主键，公司目录命名为 `APP-108261_立訊精密工業`。一旦该公司后续出现在招股发行或已上市数据源，由后续工具通过公司名匹配合并主键。
- **PHIP 标志聆讯**：JSON 的 `hasPhip=true` 是粗略标志；本工具按 `ls[].nF == "聆訊後資料集"` 精确识别，而非依赖 `hasPhip`（因为后者可能在聆讯前为 true）。
- **多档提交**：同一公司可能有「申請版本（第一次呈交）」「（第二次呈交）」等多个版本，全部保留并下载。文件名带提交档数，便于区分。
- **整体协调人公告 / 警告声明不抓**：JSON 的 `ls[]` 含 `整體協調人公告－委任`、`警告聲明` 等辅助文档，不在白名单内，会被过滤。
- **上市方式自动识别**：`listing_method` 由公司名后缀启发式填充（`-B`→机制B、`-P`→18C特专科、`-W`→WVR）。注意申请阶段 stock_code 是 `APP-{id}`，**无法用代码识别 GEM 板**，所以申请阶段的 GEM 公司会被标为 `待确认`，需 PDF 工具补全。HKEX 命名规范保证 `-B/-P/-W` 后缀在递表阶段就已确定。
- **繁简匹配**：HKEX 中文版 JSON 用繁体（`申請版本`/`聆訊後資料集`），脚本已同时匹配简体变体（`申请版本`/`聆讯后资料集`）作为容错。
- **多档 PDF 文件名**：同一公司不同档数的 PHIP 可能在同一秒落盘，文件名带 `YYYYMMDD_HHMMSS` 时间戳。由于 JSON 端点只给日期不给时间，时间戳会回退到当日 00:00:00，可能重名 → 脚本在文件名后追加 `_<seq>` 防覆盖。

## Verification

成功运行后：

1. `data/manifest.json` 存在，`by_stage.遞表 > 0` 且 `by_stage.聆訊 > 0`
2. `data/companies/APP-*` 下有对应公司子目录
3. 每个公司子目录有 `company.json` 和 `docs/申請版本*.pdf` 或 `docs/聆訊後資料集*.pdf`
4. 终端输出 `Summary: X new, Y skipped, Z failed`

## References

- [JSON API 端点与字段定义](references/json-api.md)
- 姐妹工具：[hkex-offering-tracker](../hkex-offering-tracker/SKILL.md)（招股发行抓取）
- 共享状态机定义：[hkex-offering-tracker/references/state-machine.md](../hkex-offering-tracker/references/state-machine.md)
