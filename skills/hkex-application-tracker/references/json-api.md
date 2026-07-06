# HKEX appindex JSON API

`https://www1.hkexnews.hk/app/appindex.html` 是客户端渲染的页面，所有数据来自 `https://www1.hkexnews.hk/ncms/json/eds/` 下的静态 JSON 文件。无需 JavaScript 执行、无 session、无 CSRF。

## 端点矩阵

文件名模式：`{status}_{doctype}_{board}_{lang}.json`

| 状态 status | 文档 doctype | 板块 board | 语言 lang | 文件名 |
|---|---|---|---|---|
| `appactive` | `appphip` | `sehk` | `c` | `appactive_appphip_sehk_c.json` |
| `appactive` | `appphip` | `gem`  | `c` | `appactive_appphip_gem_c.json` |
| `appactive` | `app`     | `sehk` | `c` | `appactive_app_sehk_c.json` |
| `appactive` | `app`     | `gem`  | `c` | `appactive_app_gem_c.json` |
| `applisted` | —         | `sehk` | `c` | `applisted_sehk_c.json`（已上市公司全集） |
| `applisted` | —         | `gem`  | `c` | `applisted_gem_c.json` |
| `appreturned`/`appinactive` | — | `sehk`/`gem` | `c` | 退回 / 不活跃申请人 |

字段含义：

- **status**：`appactive`（在审）/ `applisted`（已上市）/ `appreturned`（被退回）/ `appinactive`（不活跃）
- **doctype**：`app`（仅申请版本 Application Proof）/ `appphip`（申请版本 + 聆讯后资料集 PHIP）
- **board**：`sehk`（主板）/ `gem`（创业板 Growth Enterprise Market）
- **lang**：`c`（中文繁体）/ `e`（英文）

> 本工具默认抓 `appactive_appphip_*_c.json`（递表 + 聆讯全集，中文版）。

## 顶层 schema

```json
{
  "genDate": "1783098010001",   // 生成时间戳（毫秒）
  "uDate": "03/07/2026",        // 更新日期（DD/MM/YYYY）
  "app": [ <applicant>, ... ]   // 申请人列表
}
```

## 申请人对象 schema

```json
{
  "id": 108261,                          // 内部申请人 ID（主键，全局唯一）
  "d": "23/06/2026",                     // 最新文件日期（DD/MM/YYYY）
  "a": "立訊精密工業股份有限公司",          // 申请人名称（语言随 _lang）
  "s": "A",                              // 状态码：A=Active, LT=Listed, IR=Inactive
  "w": "sehk/2026/108261/documents/warn26062301879_c.pdf",  // 警告声明 PDF 相对路径
  "sD": 5,                               // sequence day
  "sA": 5,                               // sequence applicant number
  "st": "01461",                         // 股份代号（仅在 applisted 出现，appactive 无此字段）
  "ls": [ <submission>, ... ],           // 子文档列表（AP/PHIP/OC 公告等）
  "ps": [ <submission>, ... ],           // prospectus section（通常 appactive 为空）
  "hasPhip": true,                       // 是否含 PHIP（粗略标志聆讯）
  "postingDate": "Jun 23, 2026"          // 发布日期（人类可读）
}
```

## submission 对象 schema（`ls[]` 与 `ps[]` 内的元素）

```json
{
  "d": "23/06/2026",                                       // 文件日期
  "nF": "聆訊後資料集（第一次呈交）",                          // 主文档类型（核心字段）
  "nS1": "全文檔案",                                       // 子类型（全文/多檔案/OC 公告子类）
  "nS2": "多檔案",                                         // 进一步子类型（可选）
  "u1": "sehk/2026/108261/documents/sehk26062301881_c.pdf", // 主 PDF 相对路径
  "u2": "sehk/2026/108261/2026062301878_c.htm"             // 多檔案 HTML 索引（可选）
}
```

`u1` 完整 URL：`https://www1.hkexnews.hk/app/` + 相对路径。

## `nF` 字段值与状态推断

| `nF` 值 | 推断的 listing_stage | 是否下载 |
|---|---|---|
| `申請版本` / `申請版本（第一次呈交）` / `申請版本（第二次呈交）` | 遞表 | 是 |
| `聆訊後資料集` / `聆訊後資料集（第一次呈交）` | 聆訊 | 是 |
| `整體協調人公告－委任` / `整體協調人公告－委任（經修訂）` / `整體協調人公告－退任` | — | 否（辅助文档） |
| （无 `nF`，仅有 `nS1`） | — | 否（OC 公告或警告声明） |

## PDF URL 模式

| 类型 | 路径模式 | 示例 |
|---|---|---|
| 申请版本 / PHIP | `/app/{board}/{YYYY}/{appId}/documents/sehk{YYMMDD}{NNNNN}[_c].pdf` | `/app/sehk/2026/108261/documents/sehk26062301881_c.pdf` |
| 多档 HTML 索引 | `/app/{board}/{YYYY}/{appId}/{YYYYMMDD}{NNNNN}[_c].htm` | `/app/sehk/2026/108261/2026062301878_c.htm` |
| 警告声明 | `/app/{board}/{YYYY}/{appId}/documents/warn{YYMMDD}{NNNNN}[_c].pdf` | `/app/sehk/2026/108261/documents/warn26062301879_c.pdf` |

文件名末尾的 `_c` 表示中文版，`_e` 表示英文版。同一文档可能同时有 `_c.pdf` 与 `_e.pdf` 两个语言版本。

## 关键观察

1. **`appactive` 与 `applisted` 是两套端点**：申请阶段无 `st`（股份代号），上市后才有。本工具用 `APP-{id}` 作临时主键。
2. **`appphip` 是 `app` 的超集**：`appphip` 同时含申请版本和 PHIP；`app` 仅含申请版本。默认抓 `appphip`。
3. **多档提交**：同一申请人可能有「第一次呈交」「第二次呈交」等多个版本，全部出现在 `ls[]` 中。
4. **OC 公告（整體協調人公告）**：是整体协调人任命/退任公告，不算 IPO 主证据，本工具过滤掉。
5. **JSON 每日约 16:00 HKT 更新**：日内多次拉取意义不大，社区惯例是日级轮询。
6. **`sA`（sequence applicant）**：全局递增的申请人序号，可用于排序；但稳定主键用 `id`。

## 反爬措施

实测无：

- 无 CSRF token
- 无 session cookie 要求（页面 disclaimer 弹窗只写 localStorage）
- 无 User-Agent 验证
- 无 X-RateLimit 头
- 无 Cloudflare/Akamai 防护

建议的礼貌策略：

- `User-Agent: Mozilla/5.0 (compatible; hkex-tracker/2.0; +URL)`
- `Referer: https://www1.hkexnews.hk/app/appindex.html`（推荐但非强制）
- 并发下载 ≤ 4（本工具默认 `CONCURRENCY=4`）
- PDF 间隔约 1 秒（社区惯例，本工具靠信号量限速）

## 实测样本（2026-07-03）

`appactive_appphip_sehk_c.json`：17 条 PHIP 已交记录（聆讯阶段），如：

- `id=108261 立訊精密工業股份有限公司` — `聆訊後資料集（第一次呈交）` 2026-06-23
- `id=108670 MOMENTA GLOBAL LIMITED - W` — `聆訊後資料集（第一次呈交）` 2026-06-23

`appactive_app_sehk_c.json`（未在本工具默认抓取）：525 条主板在审申请人。
