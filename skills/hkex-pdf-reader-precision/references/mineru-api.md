# MinerU 精准 API 接口规范

本文件是 [`mineru_client.py`](../scripts/mineru_client.py) 实现所依据的接口契约，
基于 [https://mineru.net/apiManage/docs](https://mineru.net/apiManage/docs)（2026-07 抓取）。
若 MinerU 升级接口，**先更新本文件**，再改 `mineru_client.py`。

## Base URL

```
https://mineru.net/api/v4
```

## 鉴权

每个请求必须带：

```
Authorization: Bearer <token>
Content-Type: application/json
```

Token 在 https://mineru.net/apiManage/token 创建。

## 模型选择

```
model_version: pipeline | vlm | MinerU-HTML
```

**本 skill 硬编码 `pipeline`**：
- `pipeline`：无幻觉，财务数字 100% 安全
- `vlm`：官方推荐，精度更高，但**可能在罕见情况下编造数字**——招股书/配发结果场景下不可接受
- `MinerU-HTML`：仅用于 HTML 文件（本 skill 不处理 HTML）

## 文件限制

| 限制项 | 值 | 超限错误码 |
|--------|---|----------|
| 单文件大小 | 200 MB | -60005 |
| 单次页数 | 200 页 | -60006 |
| 单 batch 文件数 | 50 | -500 |
| 每日免费优先级额度 | 1000 页/账号 | -60018 |

## 接口流程

### 1. 申请上传 URL（批量）

```
POST /api/v4/file-urls/batch
```

请求体：

```json
{
  "files": [
    {"name": "招股书.pdf", "page_ranges": "0-199"},
    {"name": "招股书.pdf", "page_ranges": "200-399"}
  ],
  "model_version": "pipeline",
  "language": "ch",
  "enable_table": true,
  "enable_formula": false
}
```

- `files[].name`：必填，文件名（含扩展名）
- `files[].page_ranges`：可选，分段页码（如 `"0-199"`、`"200-399"`、`"2--2"` 表第 2 页到倒数第 2 页）
- `language`：默认 `ch`，可选 `en`/`ch_server`/`chinese_cht` 等
- `enable_table`：默认 true（招股书表格多，必须开）
- `enable_formula`：默认 true，**本 skill 关闭**（招股书无公式，省时间）
- `enable_formula` 对 vlm 模型只影响行内公式（无关）

响应：

```json
{
  "code": 0,
  "data": {
    "batch_id": "2bb2f0ec-...",
    "file_urls": ["https://oss-mineru.../...", "https://oss-mineru.../..."]
  }
}
```

`file_urls[i]` 与请求 `files[i]` 一一对应，是 OSS 签名上传 URL（24 小时有效）。

### 2. PUT 上传文件

```
PUT <file_url>
Content: <binary>
```

- **不要**设 `Content-Type`（官方文档明确："上传文件时，无须设置 Content-Type 请求头"）
- 同一文件分 N 段处理时，会 N 次完整上传该文件到不同的 OSS URL（幂等无副作用）
- 200MB 文件建议流式上传（`requests.put(url, data=f)` 或 httpx 等价）

上传完成后**无需**调用提交接口，server 自动开始解析。

### 3. 轮询结果（批量）

```
GET /api/v4/extract-results/batch/{batch_id}
```

响应：

```json
{
  "code": 0,
  "data": {
    "batch_id": "2bb2f0ec-...",
    "extract_result": [
      {
        "file_name": "招股书.pdf",
        "state": "done",
        "full_zip_url": "https://cdn-mineru.../xxx.zip",
        "err_msg": ""
      }
    ]
  }
}
```

`state` 枚举：

| state | 含义 |
|-------|------|
| `waiting-file` | 等待 PUT 上传完成 |
| `pending` | 排队中 |
| `running` | 正在解析 |
| `converting` | 格式转换中（如转 docx/html） |
| `done` | 完成 |
| `failed` | 失败（看 `err_msg`） |

轮询策略（`mineru_client.poll_until_done`）：

- 间隔 5 秒（默认）
- 超时 1800 秒（30 分钟，足够 200 页 × 几段）
- 当所有 result 的 state 都不在 `{waiting-file, pending, running, converting}` 时停止
- 进度回调每 15 秒打印一次状态汇总

### 4. 下载 zip 并提取 Markdown

`full_zip_url` 是一个 zip 包，内含：

```
full.md              ← Markdown 结果（本 skill 主要用这个）
layout.json          ← 版面分析中间结果（middle.json）
*_model.json         ← 模型推理结果
*_content_list.json  ← 内容列表
images/              ← 提取的图片
```

`mineru_client.download_markdown` 会下载 zip、解压到指定目录、返回 `full.md` 路径。

## 错误码处理

`mineru_client.py` 的 `ERROR_CODE_MAP` 把这些错误码映射到具体异常类：

| code | 异常类 | 含义 | 调用方应对 |
|------|-------|------|----------|
| `A0202` | `MinerUAuthError` | token 错误 | 检查 token / Bearer 前缀 |
| `A0211` | `MinerUAuthError` | token 过期 | 换新 token |
| `-60005` | `MinerULimitError` | 文件太大 | 应已被分段逻辑避免；出现说明单段仍 >200MB |
| `-60006` | `MinerULimitError` | 文件页数超限 | 同上；调用方应再分小段 |
| `-60009` | `MinerURateLimit` | 任务队列已满 | 退避重试 |
| `-60018` | `MinerUQuotaError` | 每日额度耗尽 | **停止后续 PDF，明天再来** |
| HTTP 429 | `MinerURateLimit` | IP 限频 | 退避重试 |
| 其他 | `MinerUError` | 通用 | 记录日志，标记失败 |

完整错误码见官方文档底部表格。

## 招股书分段策略

`compute_page_ranges(total_pages, chunk=200)` 把整本 PDF 切成 ≤ 200 页的段：

```python
>>> compute_page_ranges(450, 200)
['0-199', '200-399', '400-449']

>>> compute_page_ranges(150, 200)
['0-149']
```

注意：MinerU 的 `page_ranges` 是闭区间，且页码从 0 开始（与 pypdf 一致）。

## 缓存策略

本 skill **不做客户端缓存**（每次都重新上传 + 解析）。原因：

- MinerU server 端已对 URL 做 15 分钟缓存（参数 `no_cache=false`、`cache_tolerance=900`）
- 同一 PDF 重抽的需求罕见（招股书版本变了才会重抽）
- 客户端缓存会与 Skill C 的字段版本管理冲突

如果需要强制重抽，调 `precision_extract.py` 时它默认会重新走一遍流程；DB 里的 `extractions` 行会被 `upsert_extraction` 覆盖（key = stock_code + field_name）。

## v4 vs v1（Agent 轻量 API）对比

本 skill **不使用 v1 Agent 轻量 API**，原因：

| 维度 | v4 精准 | v1 Agent 轻量 |
|------|---------|--------------|
| 文件大小 | 200 MB | **10 MB** |
| 页数 | 200 | **20** |
| Token | 需要 | 不需要 |
| 批量 | ≤ 50 | 单文件 |
| 输出 | zip（多格式） | 仅 md CDN |
| 模型 | pipeline/vlm | 固定 pipeline 轻量 |
| 限频 | 按账号额度 | **按 IP** |

招股书都 >10MB / >20 页，v1 装不下。配发结果公告虽小，但为简化代码统一走 v4（耗额度可忽略）。
