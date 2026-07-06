---
name: hkex-offering-tracker
description: "招股发行抓取工具：抓取 HKEX 招股文件页面，下载全球发售招股书 PDF。仅覆盖 IPO 生命周期的『招股』『已上市』阶段；递表聆讯见姐妹工具 hkex-application-tracker。"
disable-model-invocation: true
metadata:
  hermes:
    tags: [finance, scraping, ipo, hkex, offering]
    category: research
platforms: [linux, macos, windows]
---

# 招股发行抓取工具（HKEX Offering Tracker）

抓取港交所招股文件页面，识别处于「招股」状态的公司，下载全球发售招股书 PDF，并以 JSON 三件套形式向 agent 暴露全量状态。

本 skill 仅覆盖 IPO 生命周期中的「招股」「已上市」两个阶段。**递表、聆讯阶段的数据请使用姐妹工具 [`hkex-application-tracker`](../hkex-application-tracker/SKILL.md)**。两者共用同一个 `data/state.db`，状态机贯通（遞表 → 聆訊 → 招股 → 已上市）。

## When to Use

用户提到以下任一场景时触发：

- "抓港交所新股" / "下载招股书" / "更新 IPO 列表"
- "看看现在哪些公司在招股"
- "查 XX 公司的招股文件"
- 处理 HKEX 链接 `hkexnews.hk/search/predefineddoc.xhtml`

**不要在此工具询问**：递表名单、聆讯后资料集（PHIP）—— 请转用 `hkex-application-tracker`。

## State Machine

本 skill 跟踪 IPO 生命周期 4 个状态，**本工具只填充「招股」**：

| 状态 | 数据源 | 由本工具抓取？ |
|------|--------|---------------|
| 遞表 | 上市申请人页面（`appindex.html`） | 否（见 `hkex-application-tracker`） |
| 聆訊 | PHIP 反推（`appindex.html`） | 否（见 `hkex-application-tracker`） |
| **招股** | `predefineddocuments=6`（本工具） | **是** |
| 已上市 | 新上市股份配發結果 | 预留，不抓 |

状态定义和推断规则见 [references/state-machine.md](references/state-machine.md)。

## Procedure

### 1. 安装依赖（首次或环境更新时）

```bash
pip install -r skills/hkex-offering-tracker/scripts/requirements.txt
```

### 2. 运行抓取

```bash
python skills/hkex-offering-tracker/scripts/fetch_offerings.py
```

脚本会：

1. 抓取 HKEX 招股文件页 HTML
2. 解析表格行，推断每行状态
3. 只下载状态为「招股」且 `doc_type` 在白名单（全球發售）的 PDF
4. 写入共享 SQLite 状态库（`data/state.db`，与 `hkex-application-tracker` 共用）
5. 导出 JSON 三件套到 `data/`

### 3. 读取结果（agent 主接口）

agent 永远读 JSON，不查数据库。

**全量概览** — 读 [`data/manifest.json`](../../../data/manifest.json):

```json
{
  "by_stage": {"遞表": 525, "聆訊": 16, "招股": 16, "已上市": 0},
  "companies": [{"stock_code": "06951", "listing_stage": "招股", ...}]
}
```

**单公司详情** — 读 `data/companies/<code>_<name>/company.json`，含 `listing_stage` + `state_history` + `documents` 清单，以及预留的 `listing_type` / `listing_method` / `confirmed_name` 字段（`listing_method` 已用公司名后缀启发式填充，如 `-B`→机制B、`-P`→18C特专科、`-W`→WVR、GEM 代码→创业板；`listing_type` 默认 `待确认`，待 PDF 读取工具识别；`confirmed_name` 初始值=公司名）。

**文件路径本身编码状态** — `data/companies/<code>_<name>/docs/<doc_type>_<state>_<YYYYMMDD_HHMMSS>.pdf`，例如 `全球發售_招股_20260630_065700.pdf`，ls 一眼看出。

**三库架构（v2.2）** — 公司库分三层存储，详见 [state-machine.md](references/state-machine.md)：

| 库 | 位置 | 内容 | 由本工具维护？ |
|---|---|---|---|
| Raw 素材库 | `companies/<code>_<name>/docs/` | HKEX 原始 PDF | ✅ **本工具写入** |
| Derived 信息库 | `companies/<code>_<name>/info/` | PDF 提取的结构化数据 | 否（PDF 读取工具） |
| Analysis 报告库 | `companies/<code>_<name>/reports/` | 人工/AI 分析报告 | 否（你/AI 写） |

每公司的 `company.json` 同时索引三库（含 DB 注册条目 + 文件系统扫描容错），Agent 单点拿到全貌。跨公司聚合视图在 `data/views/by_stage.json`、`by_method.json`、`by_type.json`。

## Reading the Output

| Agent 想做的事 | 读什么 |
|---------------|--------|
| "现在哪些公司在招股?" | `manifest.json` → 过滤 `listing_stage == "招股"` |
| "状态分布统计" | `manifest.json.by_stage` |
| "XX 公司全部历史" | `companies/<code>_<name>/company.json` |
| "XX 公司招股书路径" | 该公司 `company.json.documents[].local_path` |
| "新增了什么?" | 对比本次 `generated_at` 与上次；或查 SQLite `state_history` 表 |
| "公司是 AH 股吗？哪个机制?" | `company.json` 的 `listing_type` / `listing_method`（`listing_method` 已启发式填充，`listing_type` 仍为 `待确认`，待 PDF 工具识别） |

## Pitfalls

- **繁体匹配**：HKEX 页面是繁体中文，过滤关键词必须用繁体（「全球發售」非「全球发售」）。脚本已对两者都匹配。
- **大文件慢**：部分招股书超 20MB，流式下载，不要中断脚本。
- **公司名非法字符**：繁体公司名可能含 `/`、`:` 等，脚本已替换为 `_`，但 agent 引用路径时仍需注意。
- **GEM 板结构不同**：`predefineddocuments=6` 也含 GEM 板招股，其 URL 路径含 `/gem/` 而非 `/sehk/`，脚本会自动识别。
- **重組/介紹不算招股**：同页面会混入「重組方案」「介紹」「股份發售」等，这些 doc_type 不在白名单，会被过滤。
- **状态字段命名**：早期版本使用 `current_state`（单字段）。新版 schema 扩展为 4 维：`listing_stage` / `listing_type` / `listing_method` / `confirmed_name`。`current_state` 仍保留以兼容旧 agent，值同 `listing_stage`。其中 `listing_method` 由公司名后缀启发式填充（`-B`/`-P`/`-W`/GEM 代码），`confirmed_name` 初始值=公司名，`listing_type` 保持 `待确认` 待 PDF 工具识别。

## Verification

成功运行后：

1. `data/manifest.json` 存在，`by_stage.招股 > 0`
2. `data/companies/` 下有对应公司子目录
3. 每个公司子目录有 `company.json` 和 `docs/*.pdf`
4. 终端输出 `Summary: X new, Y skipped, Z companies total`

## References

- [State machine definition](references/state-machine.md)
- [HKEX page anatomy](references/page-anatomy.md)
- [Troubleshooting](references/troubleshooting.md)
- 姐妹工具：[hkex-application-tracker](../hkex-application-tracker/SKILL.md)（递表聆讯抓取）
