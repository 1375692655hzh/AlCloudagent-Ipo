# Troubleshooting

## 解析返回 0 行

**症状**:`Parsed 0 rows from page`

**原因**:
- HKEX 改版了 HTML 结构(罕见,JSF 页面稳定多年)
- 网络代理拦截了响应
- 编码错误导致 `lxml` 解析失败

**排查**:

```bash
# 1. 直接看原始 HTML
curl -A "Mozilla/5.0" "https://www1.hkexnews.hk/search/predefineddoc.xhtml?lang=zh&predefineddocuments=6" -o page.html
# 2. 在 page.html 中搜 "/listedco/listconews/" 看是否有 PDF 锚点
grep -c "/listedco/listconews/" page.html
```

- 若 grep 返回 0 → 页面结构变了，需更新 [`scripts/fetch_offerings.py`](../scripts/fetch_offerings.py) 的 `parse_listing_html()`
- 若 grep 返回 N > 0 但解析为 0 → 容器爬升逻辑失效,调整向上爬升层数(当前 6 层)

## PDF 下载失败(HTTP 4xx/5xx)

**症状**:`FAIL 06951 三環集團: HTTP 403 for ...`

**原因**:
- HKEX 临时风控(罕见)
- UA 被识别为 bot

**修复**:
- 默认 UA 已是浏览器风格,如仍被拒,在 `USER_AGENT` 改为更真实的浏览器串
- 等几分钟重试(脚本内置 tenacity 3 次指数退避)

## PDF 下载超时

**症状**:大文件(>20MB)超时

**修复**:
- `DOWNLOAD_TIMEOUT` 已设为 180 秒
- 如仍超时，在 [`scripts/common.py`](../scripts/common.py) 顶部调整 `DOWNLOAD_TIMEOUT = httpx.Timeout(300.0, ...)`
- 或降低 `CONCURRENCY = 2`

## 公司名含非法字符

**症状**:Windows 上路径错误

**已处理**:`sanitize_filename()` 替换 `\/:*?"<>|` 为 `_`

**注意**:agent 引用 `local_path` 时若平台不支持 UTF-8 文件名(Linux 老内核),需配置 `LANG=zh_CN.UTF-8`。

## SQLite 锁定

**症状**:`database is locked`

**原因**:上一次运行未正常退出。

**修复**:

```bash
rm data/state.db
# 重新运行会重建 schema 并重抓
python skills/hkex-offering-tracker/scripts/fetch_offerings.py
```

## manifest.json 与实际文件不一致

**症状**:JSON 里有路径但磁盘没文件(或反之)

**原因**:JSON 是从 SQLite 重算的,如果手动删过文件,DB 仍有记录。

**修复**:

```bash
# 方式 A:从 DB 删除孤儿记录后重新 export
sqlite3 data/state.db "DELETE FROM ipo_documents WHERE local_path NOT IN ($(find data/companies -name '*.pdf' | sed ...))"

# 方式 B:清库重抓(简单粗暴)
rm -rf data/
python skills/hkex-offering-tracker/scripts/fetch_offerings.py
```

## 繁体/简体匹配失败

**症状**:`doc_type` 是简体「全球发售」但页面是繁体「全球發售」,或反之

**已处理**:`state.py` 的 `STATE_INFER_RULES` 同时收录了简繁两种写法。如未来出现新的变体,加进字典即可。

## 重复抓取

**症状**:每次运行都重新下载所有 PDF

**原因**:SQLite 的 `url_hash` 没匹配上(可能 PDF URL 改了)。

**排查**:

```bash
sqlite3 data/state.db "SELECT url_hash, pdf_url FROM ipo_documents LIMIT 5"
```

- 若 URL 与页面上的不一致 → HKEX 改了 URL 格式,需重新计算 hash(自动,重跑即可)
- 若 hash 一致但仍重抓 → 检查 `_handle_row()` 中的查询逻辑

## 状态推断错误

**症状**:某 doc_type 没被识别,或被错误归类

**修复**:在 [`scripts/state.py`](../scripts/state.py) 的 `STATE_INFER_RULES` 加映射，然后重跑 `export_json` 即可，无需重抓 PDF：

```bash
# 重跑任一 fetcher 即可触发 export_json，例如：
python skills/hkex-offering-tracker/scripts/fetch_offerings.py --dry-run
# 注：第一版未拆分 export_json 为独立脚本，由 fetch_offerings.py / fetch_applications.py 末尾调用 common.export_json
```

## 依赖安装失败

```bash
# 推荐 Python 3.10+
python --version

# 在仓库根创建虚拟环境
python -m venv .venv
.venv\Scripts\activate    # Windows
source .venv/bin/activate  # macOS/Linux

pip install -r skills/hkex-offering-tracker/scripts/requirements.txt
```

如 `lxml` 在 Windows 编译失败,改用预编译 wheel:

```bash
pip install --only-binary :all: lxml
```
