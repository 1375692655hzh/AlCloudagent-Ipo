---
name: hkex-ipo-tracker
description: "Tracks HKEX IPO prospectuses. Use when fetching 招股 filings."
disable-model-invocation: true
metadata:
  hermes:
    tags: [finance, scraping, ipo, hkex]
    category: research
platforms: [linux, macos, windows]
---

# HKEX IPO Tracker

抓取港交所招股文件页面,识别处于「招股」状态的公司,下载全球发售招股书 PDF,并以 JSON 三件套形式向 agent 暴露全量状态。

## When to Use

用户提到以下任一场景时触发:

- "抓港交所新股" / "下载招股书" / "更新 IPO 列表"
- "看看现在哪些公司在招股"
- "查 XX 公司的招股文件"
- 处理 HKEX 链接 `hkexnews.hk/search/predefineddoc.xhtml`

## State Machine

本 skill 跟踪 IPO 生命周期 4 个状态,当前数据源只填充「招股」:

| 状态 | 数据源 | 第一版抓取 |
|------|--------|-----------|
| 遞表 | 上市申请人页面 | 预留,不抓 |
| 聆訊 | PHIP 反推 | 预留,不抓 |
| **招股** | `predefineddocuments=6` | **抓** |
| 已上市 | 新上市股份配發結果 | 预留,不抓 |

状态定义和推断规则见 [references/state-machine.md](references/state-machine.md)。

## Procedure

### 1. 安装依赖(首次或环境更新时)

```bash
pip install -r skills/hkex-ipo-tracker/scripts/requirements.txt
```

### 2. 运行抓取

```bash
python skills/hkex-ipo-tracker/scripts/fetch_ipos.py
```

脚本会:

1. 抓取 HKEX 招股文件页 HTML
2. 解析表格行,推断每行状态
3. 只下载状态为「招股」且 `doc_type` 在白名单(全球發售)的 PDF
4. 写入 SQLite 状态库
5. 导出 JSON 三件套到 `data/`

### 3. 读取结果(agent 主接口)

agent 永远读 JSON,不查数据库。

**全量概览** — 读 [`data/manifest.json`](../../../data/manifest.json):

```json
{
  "by_state": {"招股": 16, "遞表": 0, "聆訊": 0, "已上市": 0},
  "companies": [{"stock_code": "06951", "current_state": "招股", ...}]
}
```

**单公司详情** — 读 `data/companies/<code>_<name>/company.json`,含 `current_state` + `state_history` + `documents` 清单。

**文件路径本身编码状态** — `data/companies/<code>_<name>/docs/<doc_type>_<state>_<YYYYMMDD_HHMMSS>.pdf`,例如 `全球發售_招股_20260630_065700.pdf`,ls 一眼看出。

## Reading the Output

| Agent 想做的事 | 读什么 |
|---------------|--------|
| "现在哪些公司在招股?" | `manifest.json` → 过滤 `current_state == "招股"` |
| "状态分布统计" | `manifest.json.by_state` |
| "XX 公司全部历史" | `companies/<code>_<name>/company.json` |
| "XX 公司招股书路径" | 该公司 `company.json.documents[].local_path` |
| "新增了什么?" | 对比本次 `generated_at` 与上次;或查 SQLite `state_history` 表 |

## Pitfalls

- **繁体匹配**:HKEX 页面是繁体中文,过滤关键词必须用繁体(「全球發售」非「全球发售」)。脚本已对两者都匹配。
- **大文件慢**:部分招股书超 20MB,流式下载,不要中断脚本。
- **公司名非法字符**:繁体公司名可能含 `/`、`:` 等,脚本已替换为 `_`,但 agent 引用路径时仍需注意。
- **GEM 板结构不同**:`predefineddocuments=6` 也含 GEM 板招股,其 URL 路径含 `/gem/` 而非 `/sehk/`,脚本会自动识别。
- **重組/介紹不算招股**:同页面会混入「重組方案」「介紹」「股份發售」等,这些 doc_type 不在白名单,会被过滤。

## Verification

成功运行后:

1. `data/manifest.json` 存在,`by_state.招股 > 0`
2. `data/companies/` 下有对应公司子目录
3. 每个公司子目录有 `company.json` 和 `docs/*.pdf`
4. 终端输出 `Summary: X new, Y skipped, Z companies total`

## References

- [State machine definition](references/state-machine.md)
- [HKEX page anatomy](references/page-anatomy.md)
- [Troubleshooting](references/troubleshooting.md)
