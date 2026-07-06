# HKEX 配发结果页面解析（predefineddocuments=4）

解析 `https://www1.hkexnews.hk/search/predefineddoc.xhtml?lang=zh&predefineddocuments=4` 的页面结构。

## 页面性质

- **JSF（JavaServer Faces）渲染的静态 HTML**，无需 JavaScript 执行。与招股页面 `predefineddocuments=6` **结构完全一致**，共用同一套解析逻辑。
- `httpx.get()` + BeautifulSoup（lxml 解析器）即可拿到完整数据。
- 编码 UTF-8（繁体中文字符正常显示）。
- **时间窗口**：页面右侧「显示筛选」默认控制为最近 **7 天**。当前实现仅覆盖这 7 天。

## 表格行结构

每条记录是一个表格行，包含 4 个标签字段 + 1 个 PDF 链接。HTML 大致结构：

```html
<tr>
  <td>
    <span>發放時間: 30/06/2026 08:30</span>
    <span>股份代號: 02668</span>
    <span>股份簡稱: 紅星美凱龍</span>
    <a href="/listedco/listconews/sehk/2026/0630/2026063000123_c.pdf">
      最終發售價及配發結果公告 (357KB)
    </a>
  </td>
</tr>
```

注意：锚点文本可能是「配發結果」、「配發結果公告」、「最終發售價及配發結果公告」、「新上市股份配發結果」等多种变体（详见下方白名单），也可能**不是** IPO 配发（供股、配售等，详见下方过滤）。

## 字段抽取

| 字段 | 标签 | 抽取正则 | 示例值 |
|------|------|---------|--------|
| 发放时间 | `發放時間:` | `(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})` | `30/06/2026 08:30` |
| 股份代号 | `股份代號:` | `: ([^\s,;]+)` | `02668` |
| 股份简称 | `股份簡稱:` | `: ([^\s,;]+)` | `紅星美凱龍` |
| 文件标题 | 锚点文本（去尾部大小） | — | `最終發售價及配發結果公告` |
| 文件大小 | 锚点文本尾 `\((\d+(\.\d+)?[KMG]B)\)` | — | `357KB` |
| PDF 链接 | `href` 属性（拼前缀） | `/listedco/listconews/.*\.pdf` | 完整 URL |

抽取逻辑与招股页面完全一致，由 `fetch_listings.py::parse_listing_html` 实现，复用 `_extract_field` 辅助函数。

## PDF 链接 URL 模式

| 板块 | 路径模式 | 示例 |
|------|---------|------|
| 主板 | `/listedco/listconews/sehk/YYYY/MMDD/YYYYMMDDNNNNN_c.pdf` | `sehk/2026/0630/2026063000123_c.pdf` |
| GEM | `/listedco/listconews/gem/YYYY/MMDD/YYYYMMDDNNNNN_c.pdf` | `gem/2026/0629/2026062900044_c.pdf` |

URL 拼接规则：`https://www1.hkexnews.hk` + 相对路径（脚本中 `HKEX_BASE` 常量）。

## Layer 1 过滤：白名单 + 负向过滤

只接受以下 `doc_type`（state.py 的 `LISTING_SOURCE_WHITELIST`）：

- 配發結果 / 配发结果
- 配發結果公告 / 配发结果公告
- 分配結果公告 / 分配结果公告
- 最終發售價及配發結果公告 / 最终发售价及配发结果公告
- 新上市股份配發結果 / 新上市股份配发结果

并且**负向过滤**：只要 `doc_type` 含以下任一关键词就拒绝（state.py 的 `LISTING_EXCLUDED_TITLE_PATTERNS`）：

- **供股**（rights issue）—— 老公司配股，非 IPO
- **配售**（private placement）—— 老公司配售，非 IPO
- **公開發售增發**（open offer top-up）—— 老公司增发

这条负向过滤是关键防线：HKEX 同页面会混入这些「配发类」公告，标题甚至可能同时含「配發結果」与「供股」字样（如「供股結果（包括補償安排）」），单纯白名单匹配不够，必须负向拒绝。

## Layer 2 过滤：已跟踪公司检查

对 Layer 1 通过的每一行，再查 SQLite `companies` 表：

```sql
SELECT 1 FROM companies WHERE stock_code = ?;
```

只有该 `stock_code` 之前已经在本系统中（经递表/聆讯/招股阶段被跟踪过）才放行。

调试时可用 `--include-unknown` 关掉这层（**仅 debug，正常不要用**），让所有 IPO 配发结果都进来，不限于已跟踪公司。

## 时间解析

- 输入：`30/06/2026 08:30`（DD/MM/YYYY HH:MM）
- 输出 ISO8601：`2026-06-30T08:30:00+08:00`
- 时区：HKEX 时间均为香港时间（+08:00）
- 解析失败回退：字符串原样保留在 `release_time` 字段，`release_time_iso` 留 NULL

## 解析鲁棒性

脚本对以下情况做了容错（与 offering-tracker 一致）：

1. **字段顺序变化**：不依赖固定顺序，通过标签字符串定位
2. **重复锚点**：页面有时同一 PDF 出现多次，用 `pdf_url` 去重
3. **行容器嵌套**：向上爬 6 层寻找同时含 `股份代號` 和 `發放時間` 的容器
4. **空字段**：任一关键字段为空则跳过整行

## 解析输出示例

```python
[HkexRow(
    stock_code='02668',
    company_name='紅星美凱龍',
    release_time_raw='30/06/2026 08:30',
    doc_type='最終發售價及配發結果公告',
    pdf_url='https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0630/2026063000123_c.pdf',
    file_size_label='(357KB)'
), ...]
```

## 如何扩展时间窗口（暂未实现）

当前固定 7 天。若将来需要更长历史，技术方案如下（未实现，预留）：

`predefineddocuments=4` 是 JSF 表单，时间窗由隐藏字段控制。可通过以下步骤扩展：

1. **GET 首页**拿到 `javax.faces.ViewState`（隐藏 input）
2. **POST 同一 URL**，表单参数：
   - `javax.faces.ViewState`: 上一步拿到的 token
   - `javax.faces.partial.ajax=true`
   - 与时间窗相关的 `select` 控件值（如 `fromDate`、`toDate`，需用浏览器 DevTools 抓真实控件 ID）
3. 解析返回的 partial-response XML，更新表格

**风险**：JSF 控件 ID 与 ViewState 会随 HKEX 升级而变化，维护成本高。**建议**：如非必要，保持 7 天；若需补全历史公司，配合 PDF 工具按公司手动补抓，更稳。

## 验证

不带下载的干跑：

```bash
python skills/hkex-listing-tracker/scripts/fetch_listings.py --dry-run
```

输出会列出：
- `Filter stats`：`{total: N, dropped_rights_issue: X, dropped_untracked: Y, kept: K}`
- 每条通过双层过滤的记录（股份代号、公司名、doc_type、PDF URL）

便于在改解析或过滤逻辑后快速验证。

注：递表聆讯数据请见姐妹工具 `skills/hkex-application-tracker/`（`appindex.html` 背后的静态 JSON）；招股数据见 `skills/hkex-offering-tracker/`（`predefineddocuments=6`）。
