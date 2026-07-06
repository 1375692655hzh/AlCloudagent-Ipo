# HKEX Predefined Documents Page Anatomy

解析 `https://www1.hkexnews.hk/search/predefineddoc.xhtml?lang=zh&predefineddocuments=6` 的页面结构。

## 页面性质

- JSF(JavaServer Faces)渲染的**静态 HTML**,无需 JavaScript 执行
- `httpx.get()` + BeautifulSoup 即可拿到完整数据
- 编码 UTF-8(传统中文字符正常显示)

## 表格行结构

每条记录是一个表格行,包含 4 个标签字段 + 1 个 PDF 链接。HTML 大致结构:

```html
<tr>
  <td>
    <span>發放時間: 30/06/2026 06:57</span>
    <span>股份代號: 06951</span>
    <span>股份簡稱: 三環集團</span>
    <a href="/listedco/listconews/sehk/2026/0630/2026063000324_c.pdf">
      全球發售 (11MB)
    </a>
  </td>
</tr>
```

## 字段抽取

| 字段 | 标签 | 抽取正则 | 示例值 |
|------|------|---------|--------|
| 发放时间 | `發放時間:` | `(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})` | `30/06/2026 06:57` |
| 股份代号 | `股份代號:` | `: ([^\s,;]+)` | `06951` |
| 股份简称 | `股份簡稱:` | `: ([^\s,;]+)` | `三環集團` |
| 文件标题 | 锚点文本(去尾部大小) | — | `全球發售` |
| 文件大小 | 锚点文本尾 `\((\d+(\.\d+)?[KMG]B)\)` | — | `11MB` |
| PDF 链接 | `href` 属性(拼前缀) | `/listedco/listconews/.*\.pdf` | 完整 URL |

## PDF 链接 URL 模式

| 板块 | 路径模式 | 示例 |
|------|---------|------|
| 主板 | `/listedco/listconews/sehk/YYYY/MMDD/YYYYMMDDNNNNN_c.pdf` | `sehk/2026/0630/2026063000324_c.pdf` |
| GEM | `/listedco/listconews/gem/YYYY/MMDD/YYYYMMDDNNNNN_c.pdf` | `gem/2026/0629/2026062900044_c.pdf` |

URL 拼接规则:`https://www1.hkexnews.hk` + 相对路径(脚本中 `HKEX_BASE` 常量)。

## 时间解析

- 输入:`30/06/2026 06:57`(DD/MM/YYYY HH:MM)
- 输出 ISO8601:`2026-06-30T06:57:00+08:00`
- 时区:HKEX 时间均为香港时间(+08:00),与 UTC 一致
- 解析失败回退:字符串原样保留在 `release_time` 字段,`release_time_iso` 留 NULL

## 解析鲁棒性

脚本对以下情况做了容错:

1. **字段顺序变化**:不依赖固定顺序,通过标签字符串定位
2. **重复锚点**:页面有时同一 PDF 出现多次,用 `pdf_url` 去重
3. **行容器嵌套**:向上爬 6 层寻找同时含 `股份代號` 和 `發放時間` 的容器
4. **空字段**:任一关键字段为空则跳过整行

## 解析输出示例

```python
[HkexRow(
    stock_code='06951',
    company_name='三環集團',
    release_time_raw='30/06/2026 06:57',
    doc_type='全球發售',
    pdf_url='https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0630/2026063000324_c.pdf',
    file_size_label='(11MB)'
), ...]
```

## 验证

不带下载的干跑：

```bash
python skills/hkex-offering-tracker/scripts/fetch_offerings.py --dry-run
```

输出会列出每条过滤后的招股记录，便于在改解析逻辑后快速验证。

注：递表聆讯数据请见姐妹工具 `skills/hkex-application-tracker/`，其抓取的是 `appindex.html` 背后的静态 JSON，详见该 skill 的 `references/json-api.md`。
