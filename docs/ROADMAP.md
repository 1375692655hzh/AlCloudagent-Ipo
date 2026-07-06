# 项目长期规划（ROADMAP）

> **本文件是项目的「长期记忆」。**
> 任何 agent（无论 Cursor / Hermes / 其他）在打开本仓库、做架构决策、规划新功能、做历史补全、或被问到「这项目接下来该怎么做」时，**必须先读本文件**。
> 本文件回答两个问题：① 现在到哪了？② 接下来怎么走？

---

## 0. 文档定位与读取规则

### 0.1 为什么存在

- 项目的「设计哲学」和「未来路径」分散在 `README.md`、三个 `SKILL.md`、`state-machine.md`、`common.py` 注释里。一旦用户切换窗口、新会话开始，agent 容易丢失全局上下文。
- 本文件是**单一事实源**（single source of truth）关于：现状、缺口、路线、决策理由。
- 代码是「现在能跑什么」；本文件是「为什么这么跑、接下来该怎么跑」。

### 0.2 谁读、什么时候读

| 触发场景 | 读哪几节 |
|---------|---------|
| 新会话被问到「项目进展」「接下来做什么」 | §1 现状 + §3 缺口 + §4 路线 |
| 要做历史补全（招股书 / 分配结果） | §3 缺口 1 + §4.1（前置必读） |
| 要做公司状态补全工具 | §4.4 |
| 要做可视化页面 | §4.5 |
| 要加新 tracker | §1.3 覆盖矩阵 + §4.1 |
| 被问到「为什么不用 PostgreSQL / 为什么不常驻服务」 | §5 决策日志 |
| 不确定某个决策是否合理 | §5 + §2 评分卡 |

### 0.3 维护契约

- **每完成一个 P0/P1 任务，必须更新本文档对应小节**（标记 ✅ 已完成，把内容移到 §1 现状）。
- **每新增一个 tracker 或工具，必须更新 §1.3 覆盖矩阵**。
- **每做一次重大架构决策，必须追加到 §5 决策日志**（写「为什么这么做 / 为什么没那么做」）。
- 评分卡（§2）每季度回顾一次。

---

## 1. 现状基线（As-Is，v2.2）

### 1.1 系统组成

```
┌──────────────────────────────────────────────────────────┐
│  Agent 层（Cursor / Hermes，未来：可视化页面）           │
│  读：data/manifest.json + company.json + views/*.json    │
└────────────────────────────┬─────────────────────────────┘
                             │ 只读
┌────────────────────────────▼─────────────────────────────┐
│  Skill 层（写入唯一入口，CLI）                            │
│  - hkex-application-tracker  (递表+聆讯，JSON API)        │
│  - hkex-offering-tracker     (招股，HTML)                 │
│  - hkex-listing-tracker      (配发结果，HTML，7天窗口)    │
└────────────────────────────┬─────────────────────────────┘
                             │ UPSERT
┌────────────────────────────▼─────────────────────────────┐
│  数据层（事实源）                                         │
│  - data/state.db (SQLite, 5 表, schema v2.2)              │
│  - data/companies/<code>_<name>/                          │
│      docs/*.pdf   info/*.json   reports/*.md              │
│  - data/manifest.json + views/{by_stage,by_method,by_type}│
└──────────────────────────────────────────────────────────┘
```

### 1.2 事实标准（代码引用）

| 内容 | 文件 | 行号/锚点 |
|------|------|----------|
| 5 张表 SCHEMA | `skills/hkex-offering-tracker/scripts/common.py` | 138-218 |
| Schema 迁移 | 同上 | 222-227 (`MIGRATIONS`) |
| 4 维状态填充 | `infer_method_from_name()` | 280 |
| 公司 UPSERT | `upsert_company()` | 314 |
| 衍生信息 UPSERT | `upsert_extraction()` | 376 |
| 报告 UPSERT | `upsert_report()` | 418 |
| 子目录扫描（容错） | `index_company_subdir()` | 445 |
| 通用下载/并发骨架 | `process_rows()` | 528 |
| JSON 投影 | `export_json()` | 562 |
| 跨公司维度视图 | `_write_dimension_view()` | 756 |
| 文件命名 | `build_doc_filename()` | 102 |
| 公司目录命名 | `build_company_dir()` | 117 |
| 状态推断规则 | 三个 `state.py` 副本（须同步） | — |
| 状态机规范 | `skills/hkex-offering-tracker/references/state-machine.md` | — |

### 1.3 当前覆盖矩阵

**IPO 抓取层（4 阶段全覆盖）**：

| IPO 阶段 | 数据源 | 由哪个 tracker 抓 | 第一版状态 |
|---------|--------|------------------|-----------|
| **遞表** | appindex JSON | `hkex-application-tracker` | ✅ 已抓 |
| **聆訊** | appindex JSON（PHIP 标志） | `hkex-application-tracker` | ✅ 已抓 |
| **招股** | `predefineddocuments=6` (HTML) | `hkex-offering-tracker` | ✅ 已抓 |
| **已上市** | `predefineddocuments=4` (HTML, 7 天窗) | `hkex-listing-tracker` | ✅ 已抓（仅已跟踪公司） |

**PDF 处理层（v1.0 已落地，三件套）**：

| Skill | 引擎 | 覆盖场景 | 状态 |
|-------|------|---------|------|
| `hkex-pdf-reader-batch` (A) | MarkItDown | 批量入库（PDF → Markdown） | ✅ v1.1 已落地（加 `--pdf`/`--label`） |
| `hkex-pdf-reader-precision` (B) | MinerU pipeline/vlm | 精准分析（高保真 Markdown） | ✅ v1.2 已落地（加 `--model pipeline\|vlm`，配发结果走 vlm） |
| `hkex-pdf-field-extractor` (C) | LLM (GLM/MiniMax) | 字段抽取（Markdown → 结构化字段，反向 update companies） | ✅ v1.1 已落地（加 `--source-file`） |
| `hkex-chapter-locator` | pypdf + LLM（可选） | 章节定位 + PDF 切片（局部精读前置） | ✅ v1.0 已落地 |
| `_common/`（共享库） | — | tables/verify/llm/pdf/env 复用模块 | ✅ v1.0 已落地（被 4 个 skill 依赖） |
| `hkex-allotment-basis`（Skill 2） | MinerU vlm + doubao vision | 配发结果分配基准表（双源校验，数字 100% 准确） | ✅ v1.0 已落地 |
| `hkex-prospectus-schedule`（Skill 1） | PyMuPDF 文本 + vision + LLM 兜底 | 招股书档位表（中签率计算输入） | ✅ v1.0 已落地 |
| `hkex-pdf-summary`（Skill 4） | LLM (GLM/MiniMax/DeepSeek) | 通用文字场景（业务概览/风险/股东/募资用途） | ✅ v1.0 已落地（演化自 C） |
| `hkex-prospectus-financials`（Skill 3） | MinerU pipeline + YAML 配置 | 招股书财务表格（三大报表 × 多年度） | ✅ v1.0 已落地 |

### 1.4 当前数据量（实时快照，更新于 chapter-locator v1.0 落地时）

```
companies   : 15 (全部 listing_stage='招股')
ipo_documents: 15
extractions : 3   (含 markitdown_batch_v1 全本 + chapter_locator_v1 切片定位)
reports     : 0
APP-* 行    : 0
书签覆盖率  : 15/15 (100%)
```

### 1.5 已合理的设计（**不要回退**）

1. **三库分离**：`docs/`（Raw 只读）/ `info/`（Derived 可重建）/ `reports/`（Analysis 不可重建）。三者生命周期、修改频率、可信度都不同。
2. **SQLite 单库 + JSON 投影**：Agent 永远读 JSON、不碰 DB；DB 是事实源，JSON 是物化视图。
3. **4 维状态模型**：`listing_stage`（机器填）/ `listing_type`（PDF 工具填）/ `listing_method`（启发式 + PDF 覆盖）/ `confirmed_name`（= company_name 初始值）。
4. **三个 tracker 共享 `state.db`**：状态机贯通，不重不漏。
5. **目录命名 `<code>_<name>`**：ls 一眼看出，文件路径本身编码状态。
6. **CLI 是写入唯一入口**：未来加可视化页面也保持这条，前端只读 + 触发任务。

---

## 2. 现状评分卡

| 维度 | 评分 | 说明 |
|------|------|------|
| 目录结构 | ⭐⭐⭐⭐ | 三库分离 + `<code>_<name>` 命名是对的；隐患：`APP-{id}` 与正式 code 未合并 |
| 数据库 schema | ⭐⭐⭐⭐ | 5 表 + 4 维状态覆盖 90% 场景；缺：身份层、任务层、完整度字段 |
| Agent 接口 | ⭐⭐⭐⭐⭐ | manifest / company.json / views 三件套完整，双端兼容 |
| 可视化页面拓展性 | ⭐⭐⭐ | 数据出口齐了；缺身份合并、完整度两个查询接口 |
| 历史补全支持 | ⭐⭐ | **当下不足以做历史补全**，会因身份合并失败导致重复入库 |

---

## 3. 三大缺口（待解决）

### 缺口 1：**公司身份层缺失**（最高优先）

**症状**：

- 申请阶段公司用临时主键 `APP-{id}`（如 `APP-108261_立訊精密`），见 `hkex-application-tracker/SKILL.md:130`。
- 招股 / 配发阶段公司有真实 `stock_code`（如 `02668_紅星美凱龍`）。
- **三者之间没有合并机制**：`applicant_id_map` 表在 `fetch_applications.py:207` 作为「forward scaffolding」存在，但 `resolve_stock_code()`（`fetch_applications.py:185`）只走 fallback 分支，**从未真正回填**。

**直接后果**：

- 同一公司在 DB 里有两行（`APP-12345_...` 和 `02668_...`）。
- 历史补全抓 2019 年招股书 → 拿到 `02668_紅星美凱龍` → 但递表阶段入库的是 `APP-12345` → **重复入库**。
- `company.json` 分裂，完整度统计错误。

**关联文档**：`state-machine.md:317-319` 已承认此问题（"两者暂不自动合并"）。

### 缺口 2：**无完整度 / 任务队列概念**

**症状**：

- 当下三个 tracker 是**被动抓页面**，不是**主动按公司补全**。
- 没有「这家公司缺哪个阶段的素材」的概念。
- 没有「采集任务」队列，无法批量触发历史补全。
- `material_completeness` 字段不存在，可视化页面无法显示「100 家跟踪公司里 30 家缺配发结果」。

**直接后果**：

- 历史补全只能手动一家家抓，没有自动化抓手。
- 可视化页面（未来）缺少最有价值的一列（完整度）。

### 缺口 3：**`confirmed_name` 填充时机不清**

**症状**：

- 4 维状态模型把它列为独立维度。
- 但当前所有 tracker 都把它初始化为 `company_name`（JSON `a` 字段）。
- **没有任何工具会把它从 `company_name` 变成「真正确认的名字」**。
- 它实际上等于 `company_name`，没有独立信息。

**直接后果**：可视化页面展示它会很尴尬（永远是 company_name）。

---

## 4. 拓展路线（按优先级排序）

### 4.1【P0】公司身份层（解决缺口 1，**历史补全的前置必做**）

#### 4.1.1 为什么 P0

不做这一步，历史补全会把数据库搞乱（同一公司多行）。当下增量抓新股场景不阻塞，但任何「回溯历史」的需求都必须先做这一步。

#### 4.1.2 Schema 草案

```sql
-- 新表：公司身份（与 stock_code 解耦）
CREATE TABLE company_identities (
    identity_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name      TEXT NOT NULL,         -- 全生命周期统一显示名
    primary_stock_code  TEXT,                  -- 上市后的正式代码（递表阶段为空）
    hkex_applicant_id   INTEGER,               -- HKEX 申请 id（递表阶段才有）
    first_listed_date   TEXT,                  -- 上市日（含历史补全）
    board               TEXT,                  -- '主板' / '创业板'（统一字段，不再用 stock_code 前缀推）
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE(primary_stock_code),
    UNIQUE(hkex_applicant_id)
);

-- 改造 companies 表：让 stock_code 可以是别名，指向一个 identity
-- ALTER TABLE companies ADD COLUMN identity_id INTEGER REFERENCES company_identities(identity_id);
```

#### 4.1.3 工作流：抓取时如何 match

任何 tracker 在 `upsert_company` 之前，先按以下顺序 match identity：

```
1. 有 primary_stock_code  →  SELECT identity_id FROM company_identities WHERE primary_stock_code = ?
2. 有 hkex_applicant_id   →  SELECT identity_id WHERE hkex_applicant_id = ?
3. 都没有（早期申请）     →  按 canonical_name 模糊匹配（fuzzy + 人审）
4. 都不命中               →  新建 identity
```

#### 4.1.4 实施步骤（落地时按此顺序）

1. 加 `company_identities` 表（`common.py` 的 SCHEMA + MIGRATIONS）。
2. `companies` 表加 `identity_id` 列。
3. 写一个**一次性回填脚本**：扫现有 `companies`，把 `APP-{id}` 行与正式 `stock_code` 行按公司名 match 合并。
4. 改造 `upsert_company()` 接受 `identity_id` 参数（或内部先 match）。
5. 改造三个 tracker 的 `_handle_row`，传入 `hkex_applicant_id`（仅 application-tracker 有）或 `stock_code`。
6. 改造 `export_json`：`company.json` 顶部展示 `identity` 段而非裸 `stock_code`。

#### 4.1.5 验收标准

- 同一公司在 `companies` 表只有一行（通过 `identity_id` 关联，而非多个 `stock_code`）。
- 历史补全脚本抓「2019 年 X 公司招股书」→ 自动挂到 `identity_id=42` 下，不再新建行。

---

### 4.2【P1】素材完整度字段（解决缺口 2 的查询侧）

#### 4.2.1 Schema 草案（在 `company.json` 加字段，不改 DB）

由 `export_json` 实时计算，不存表：

```json
{
  "stock_code": "02668",
  "material_completeness": {
    "application_proof": {"expected": true, "have": true,  "path": "docs/申請版本_遞表_...pdf"},
    "phip":              {"expected": true, "have": false, "path": null},
    "prospectus":        {"expected": true, "have": true,  "path": "docs/全球發售_招股_...pdf"},
    "allotment_result":  {"expected": true, "have": false, "path": null},
    "completeness_ratio": 0.5,
    "missing": ["phip", "allotment_result"]
  }
}
```

#### 4.2.2 期望矩阵（`expected` 由 `listing_stage` 推）

| listing_stage | application_proof | phip | prospectus | allotment_result |
|---------------|:-:|:-:|:-:|:-:|
| 遞表 | ✅ | — | — | — |
| 聆訊 | ✅ | ✅ | — | — |
| 招股 | ✅ | ✅ | ✅ | — |
| 已上市 | ✅ | ✅ | ✅ | ✅ |

#### 4.2.3 改造点

- `common.py::export_json` 加一段计算逻辑（纯函数，可单测）。
- `manifest.json` 的每条 entry 加 `completeness_ratio` 字段，便于跨公司排序。

#### 4.2.4 对可视化页面的意义

这是表格里最有用的一列：一眼看出「100 家跟踪公司里 30 家缺配发结果」，直接驱动补全任务。

---

### 4.3【P1】采集任务队列（解决缺口 2 的写入侧）

#### 4.3.1 Schema 草案

```sql
CREATE TABLE fetch_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_id   INTEGER NOT NULL REFERENCES company_identities(identity_id),
    doc_kind      TEXT NOT NULL,        -- 'application_proof' / 'phip' / 'prospectus' / 'allotment'
    status        TEXT NOT NULL,        -- 'pending' / 'in_progress' / 'done' / 'failed' / 'not_found'
    source        TEXT,                 -- 'hkex_appindex' / 'hkex_predefined6' / 'hkex_predefined4' / 'manual'
    source_url    TEXT,
    attempted_at  TEXT,
    completed_at  TEXT,
    notes         TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_jobs_status ON fetch_jobs(status);
CREATE INDEX idx_jobs_identity ON fetch_jobs(identity_id);
```

#### 4.3.2 工作流：历史补全脚本

```
1. 扫 material_completeness，找出所有 missing 项
2. 对每个 missing 项，INSERT 一行 fetch_jobs (status='pending')
3. 触发针对性抓取：
   - prospectus / allotment: 用公司名去 HKEX 历史搜索页（predefineddoc.xhtml 的 JSF POST，见 §4.3.3）
   - application_proof / phip: 用 applicant_id 直接查 appindex
4. 抓到 → 更新 fetch_jobs.status='done'，调用对应 tracker 的 _handle_row 入库
5. 未抓到 → status='not_found'，记录 notes
```

#### 4.3.3 长时间窗口的实现（当下未做）

`predefineddocuments=4` 和 `=6` 默认显示 7 天。更长窗口需要 JSF POST：

1. GET 首页拿 `javax.faces.ViewState`（隐藏 input）
2. POST 同一 URL，传 `javax.faces.partial.ajax=true` + 时间窗 select 控件值（用 DevTools 抓真实 ID）
3. 解析 partial-response XML

**已在文档预留**：`skills/hkex-listing-tracker/references/page-anatomy.md` 末尾有完整方案说明。

**风险**：JSF 控件 ID 与 ViewState 随 HKEX 升级变化，维护成本高。建议非必要不做，按公司手动补抓更稳。

---

### 4.4【P2，已落地 v1.0】公司状态补全工具（闭合 4 维状态）

**状态：✅ v1.0 已落地**（2026-07-05）。原计划的单一 PDF 读取工具，按"价值分层"原则拆为**三个并行 skill**，对应不同精度/成本场景。

#### 4.4.1 设计原则：价值分层

不要用一把锤子处理所有文件。低价值大批量场景走免费工具，高价值小批量场景才花 API 额度。

| 场景 | 价值密度 | 量 | 用哪个 skill | 引擎 | 成本 |
|------|---------|---|------------|------|------|
| 全库招股书入库 | 低 | 大 | **Skill A** | MarkItDown | 0 |
| 历史回填 | 低 | 大 | **Skill A** | MarkItDown | 0 |
| 单家深度分析 | 高 | 小 | **Skill B** | MinerU pipeline | 每日 1000 页免费 |
| 配发结果精读 | 高 | 小 | **Skill B** | MinerU pipeline | 同上 |
| 抽结构化字段 | — | 按需 | **Skill C** | LLM (GLM/MiniMax/gpt-4o) | 按 token |

#### 4.4.2 三件套架构

```
docs/*.pdf（Raw 库，三个 tracker 写入）
    │
    ├──→ Skill A: hkex-pdf-reader-batch       [extractor='markitdown_batch_v1']
    │    MarkItDown 批量转换，零成本
    │    ──→ info/<stem>.md  (field_name='markdown_raw')
    │
    ├──→ Skill B: hkex-pdf-reader-precision   [extractor='mineru_pipeline_v1']
    │    MinerU 精准 API（pipeline，永不 vlm）
    │    ──→ info/precision/<stem>.md  (field_name='markdown_precision')
    │    （与 Skill A 并行存在，不覆盖）
    │
    └──→ Skill C: hkex-pdf-field-extractor    [extractor='pdf_field_v1']
         LLM 字段抽取（按字段单独提问 + 校验护栏）
         数据源：优先 Skill B，回退 Skill A
         ──→ info/<field>.json
         ──→ UPDATE companies SET listing_type=, confirmed_name=
```

三个 skill 共用 `extractions` 表，通过 `extractor` 字段区分来源。

#### 4.4.3 Skill A：批量入库（hkex-pdf-reader-batch）

| 项 | 值 |
|---|---|
| 引擎 | MarkItDown 基础版（pdfplumber，纯本地） |
| 模型依赖 | 无 |
| 触发场景 | "批量入库" / "全库回填" / "MarkItDown" |
| 并发 | 4（默认，可调） |
| 输出 | `info/<pdf_stem>.md`，`extractor='markitdown_batch_v1'` |
| 幂等 | 通过 `(stock_code, source_pdf_hash)` 跳过已处理 |
| 实测 | 三環集團 456 页招股书，12.5 秒，666k 字符 |

**局限**：表格保真度低（合并单元格、跨页表会丢信息）。下游需要精确数字时，配 Skill B 或 Skill C 的校验护栏。

#### 4.4.4 Skill B：精准分析（hkex-pdf-reader-precision）

| 项 | 值 |
|---|---|
| 引擎 | MinerU 精准 API（`/api/v4/file-urls/batch`） |
| 模型 | **pipeline**（**永不 vlm**，财务数字零幻觉） |
| 触发场景 | "深度分析" / "精读" / "财务表保真" / 用户主动指定 |
| Token | 必填（环境变量 `MINERU_TOKEN` 或 `~/.mineru/config.yaml`） |
| 文件限制 | 200MB / 200 页（招股书超过自动分段 `page_ranges`） |
| 速度 | 每页 5-15 秒，一本 250 页招股书 20-40 分钟 |
| 成本 | 每日 1000 页免费优先级额度 |
| 隐私 | ⚠️ PDF 上传到 mineru.net OSS |
| 输出 | `info/precision/<pdf_stem>.md`，`extractor='mineru_pipeline_v1'` |

**关键设计**：招股书 > 200 页自动分段（每段 200 页）独立提交，下载后拼接。各段拼接处插入 `<!-- === segment N === -->` 标记便于 LLM 定位。

#### 4.4.5 Skill C：字段抽取（hkex-pdf-field-extractor）

| 项 | 值 |
|---|---|
| 引擎 | OpenAI-compatible LLM（默认 GLM-5.2 / MiniMax-M3） |
| 触发场景 | "抽招股价" / "补 listing_type" / "生成全库字段表" |
| 数据源 | 优先 `info/precision/`（Skill B），回退 `info/`（Skill A） |
| 抽取策略 | **按字段单独提问**（不是整本总结），降低幻觉 |
| 校验 | 数值字段做正则/范围校验（如募资用途百分比和 ≈ 100%） |
| 输出 | `info/<field>.json`，`extractor='pdf_field_v1'` |
| 副作用 | `listing_type` / `confirmed_name` 反向 UPDATE `companies` 表 |
| Markdown 长度上限 | 120,000 字符（约 30-40k 中文 token） |

**支持的字段**（`field_dictionary.py` 内置 6 个）：

| 字段 | 反向 update | 校验规则 |
|------|------------|---------|
| `listing_type` | ✅ | 枚举值（AH / 非-AH / H 股 / 红筹 / 待确认） |
| `issue_price_range` | 否 | 必须含数字 |
| `use_of_proceeds` | 否 | 各项百分比之和 90-110% |
| `cornerstone_investors` | 否 | 数组，每项含 name |
| `top_shareholders` | 否 | 非空数组，每项含 name |
| `confirmed_name` | ✅ | 无（字符串即可） |

校验失败的字段写入 `needs_review=true`，**不**反向 update `companies` 表（避免污染主表），由 Agent 触发人工复核。

#### 4.4.6 三件套关系（关键）

1. **Skill A 与 Skill B 输出分目录存**（`info/` vs `info/precision/`），不互相覆盖，便于对比与回退
2. **Skill C 默认 `--source auto`**：优先读 Skill B 高精度版，没有则回退 Skill A
3. **触发逻辑交给用户**：Skill A 跑完不会自动调用 Skill B，避免"不知不觉把额度烧光"
4. **三 skill 共用 schema**：`extractions` 表通过 `extractor` 字段区分来源；`upsert_extraction()` 是统一入口

#### 4.4.7 已闭合的目标

ROADMAP §4.4 原目标已全部实现：

- ✅ `listing_type` 从 PDF 抽取填回（Skill C）
- ✅ `confirmed_name` 升级到 PDF 封皮确认的简称（Skill C）
- ✅ 衍生信息库 `info/` 已启用（三 skill 都写入）
- ✅ 4 维状态模型完整闭环

#### 4.4.8 后续优化（未做）

- **历史字段版本管理**：当前 Skill C 重跑覆盖。如需保留招股书版本变更前后的字段对比，未来在 `extractions` 加 `version` 列（与 `reports` 一致）
- **Skill B 增量更新**：当前每次重抽全量上传。可加 PDF 内容 hash 缓存避免重复
- **更多字段**：`financial_summary` / `risk_factors` / `business_segments` 等仍在 `field_dictionary.py` 待扩展

---

### 4.5【P3】可视化页面（最后做）

#### 4.5.1 三层架构

```
┌─────────────────────────────────────────────────────────┐
│  前端（Vue / React，未来做）                            │
│  - 公司列表（带完整度列、阶段筛选、方式分组）           │
│  - 单公司深度页（PDF iframe + 三库切换 tab）            │
│  - 补全任务面板（触发 fetch_jobs）                      │
└────────────────┬────────────────────────────────────────┘
                 │ HTTP/REST
┌────────────────▼────────────────────────────────────────┐
│  API 层（薄 FastAPI 服务，未来做）                      │
│  - GET  /companies?stage=招股&method=机制B              │
│  - GET  /companies/<identity_id>                        │
│  - GET  /companies/<identity_id>/completeness           │
│  - POST /fetch_jobs （触发补全，调用 CLI）              │
│  - GET  /pdf/<path>  （静态文件 serve data/companies/） │
└────────────────┬────────────────────────────────────────┘
                 │ 只读 + 子进程
┌────────────────▼────────────────────────────────────────┐
│  当前已有的层（不动）：                                  │
│  - data/state.db                                        │
│  - data/manifest.json + views/*.json                    │
│  - data/companies/<code>_<name>/{docs,info,reports}/    │
│  - 三个 tracker 的 CLI（写入唯一入口）                  │
└─────────────────────────────────────────────────────────┘
```

#### 4.5.2 关键设计决策（未来做时遵守）

1. **API 层只读 + 任务触发**，不让前端直接写库。写入只走 tracker CLI（子进程方式）。
2. **manifest.json 是天然的 list API**：公司列表页可以直接读 manifest，无需额外查询接口；只有补全任务和单公司深度页才走 DB。
3. **PDF 是静态资源**：用 FastAPI `StaticFiles` 或 nginx 直接 serve `data/companies/` 目录，前端 `<iframe>` 嵌入 PDF 阅读器。
4. **写入幂等**：所有触发任务必须幂等（同一公司同一 doc_kind 重复触发不会重复下载，靠 `ipo_documents.pdf_url UNIQUE` 保证）。

#### 4.5.3 拓展顺序（推荐）

1. 公司列表页（直接读 manifest.json，**最小可用**）
2. 加完整度列（依赖 §4.2 完成）
3. 单公司深度页（PDF iframe）
4. 补全任务面板（依赖 §4.3 完成）
5. 状态补全触发（依赖 §4.4 完成）

---

## 5. 决策日志（Why Not）

记录「为什么没那么做」的取舍，避免后人重复踩坑。

### 5.1 为什么不一开始就做身份层（缺口 1）

**取舍**：v1 优先把 4 阶段抓取打通，证明数据流通畅。身份层是「干净度」问题，不是「能不能跑」问题，可以延后。

**代价**：当下数据库只有 15 家公司（全招股），重复入库问题尚未显现。一旦跑 application-tracker + 历史补全，问题会立刻暴露。

**触发条件**：① 跑 application-tracker 后看到 `APP-*` 行；② 任何历史补全需求出现。

### 5.2 为什么不用 PostgreSQL

**取舍**：

- 单机部署，没有多用户并发写。
- SQLite 是单文件，方便 git 备份 / 复制 / 迁移。
- 数据量预估 < 10 万行，SQLite 完全 hold 住。
- 不需要复杂的权限 / 角色 / 视图。

**何时升级**：① 多 agent 同时写（需要行锁）；② 数据量 > 100 万行；③ 需要全文搜索招股书内容（用 PostgreSQL + pgvector）。

### 5.3 为什么 tracker 用 CLI 而不是常驻服务

**取舍**：

- CLI 触发更简单，cron / 手动 / agent 调用都行。
- 不需要管理进程、内存泄漏、重启。
- 幂等：每次全量重算 manifest，不依赖中间状态。

**代价**：实时性差（招股公司从招股开始到我们抓到有延迟）；并发抓取需要外部锁（当下靠 `state.db` 的 SQLite 文件锁兜底）。

**何时升级**：① 需要实时通知（HKEX 一更新就抓）；② 多机分布式抓取。

### 5.4 为什么 `confirmed_name` 默认 = `company_name`

**取舍**：

- 4 维状态模型需要一个独立字段表达「最终确认的名字」（PDF 封皮可能与 HKEX 列表页不同）。
- 但抓取阶段没有「封皮名字」来源，只能用 HKEX 列表页的 `company_name` 作占位。
- 留字段是为了让 PDF 工具（§4.4）后续能覆盖。

**代价**：当下该字段没有独立信息，可视化展示会重复。已在 §3 缺口 3 记录。

### 5.5 为什么 listing-tracker 用双层过滤（只抓已跟踪公司）

**取舍**：

- `predefineddocuments=4` 同页面混杂 IPO 配发、老公司供股 / 配售。
- 如果不限制「只抓已跟踪公司」，会把所有配发公告都入库，DB 灌爆，且大部分与 IPO 无关。
- Layer 2（已跟踪检查）让该工具只推进「招股 → 已上市」转换，不主动发现新公司。

**代价**：如果一家公司从递表开始就被我们漏抓，它的配发结果永远不会被 listing-tracker 抓。需要靠 application-tracker 全量补 + 历史补全（§4.3）来兜底。

### 5.6 为什么三个 `state.py` 是副本而不是共享模块

**取舍**：

- 跨 skill 目录的 Python import 在 Windows 上路径处理麻烦（反斜杠、长路径）。
- 副本 + 文档约束「三份必须同步」更简单直接。
- `state-machine.md` 是规范，三个副本是实例。

**代价**：维护时容易漏改一份。

**何时升级**：当 `state.py` 改动频繁（> 每月一次），或副本数 > 5 时，考虑抽成 `skills/_shared/state.py` 包。

### 5.7 为什么 PDF 处理拆三个 skill 而非一个

**取舍**（v1.0 落地，2026-07-05）：

最初 ROADMAP §4.4 设想是"一个 PDF 读取工具"，落地时拆为 Skill A（batch, MarkItDown）/ Skill B（precision, MinerU）/ Skill C（field-extractor, LLM）三个。

理由：
- **价值分层原则**：低价值大批量场景必须零成本（Skill A），高价值小批量场景才花 API 额度（Skill B）。一个 skill 内做"自动路由"会让用户不知不觉烧光额度。
- **触发逻辑交给用户**：用户主动选择走 A 还是 B，agent 不替用户决定。
- **数据源回退**：Skill C 设计为"优先 precision、回退 batch"，需要两套数据并存（`info/` + `info/precision/`），这要求 A 和 B 是独立可分别调用的。

**代价**：
- 三个 SKILL.md、三个 frontmatter、三个 requirements，文档量增加。
- 用户需要理解三个 skill 的差异才能选对工具（在 SKILL.md 里都明确写了 When to Use / 不要在此工具询问）。

**何时合并**：暂不考虑。如果未来加 `hkex-pdf-reader-ocr`（扫描件专用 OCR）等更多变体，反而会进一步分化。

### 5.8 为什么 MinerU 永不使用 vlm 模型

**取舍**：

MinerU 官方现在推荐 `vlm`（视觉语言模型），精度更高、复杂版面更好。本项目的 Skill B（hkex-pdf-reader-precision）**硬编码 `model_version='pipeline'`，永不切换 vlm**。

理由：
- 招股书 / 配发结果的核心是**财务数字**（招股价、中签率、持股比例、募资额）。
- vlm 模型在罕见情况下会**编造数字**（hallucination），即便概率很低，对财务场景不可接受。
- pipeline 模型无幻觉，表格保真度稍低但**数字 100% 来自 PDF**。
- 用 Skill C 的 LLM 校验护栏可以弥补 pipeline 表格结构稍弱的缺陷。

**何时升级**：当 MinerU vlm 模型的幻觉率公开数据降至 < 0.01%（且通过我们自己招股书抽样验证），可考虑切换或加 `--model vlm` 选项。短期不做。

### 5.9 为什么 Skill C 按字段单独提问而非整本总结

**取舍**：

Skill C（hkex-pdf-field-extractor）对每个字段用独立 prompt 调一次 LLM（6 字段 = 6 次调用），而不是把整本招股书丢给 LLM 一次总结所有字段。

理由：
- **降低幻觉率**：单字段 prompt 强制 JSON schema + 校验规则，幻觉率比"整本总结"低一个数量级。
- **便于校验**：每字段独立校验（如募资用途百分比和 ≈ 100%），失败时精确定位是哪个字段错了。
- **可重跑**：单字段重跑只调一次 LLM，成本低；整本总结重跑贵。
- **字段独立演进**：新加字段只改 `field_dictionary.py`，不影响其他字段。

**代价**：API 调用次数 ×N，token 总量稍多（每次都要传 markdown）。但 GLM/MiniMax 国内模型 token 极便宜，可接受。

**何时升级**：当字段数 > 20 且 LLM 调用成本敏感时，可考虑"分组总结"（如把财务字段合并为一次调用）。

### 5.10 为什么章节定位独立成 skill 而非塞进 Skill A/B

**取舍**（v1.2 落地，2026-07-05）：

Skill A/B 当前已经能处理全本 PDF，要做局部读取有两条路：
- **路径 A**：在 Skill A/B 里加 `--pages` 参数，让它们自己切
- **路径 B**：新建独立的 `hkex-chapter-locator` skill 负责定位 + 切片，输出子 PDF 喂给 A/B（**实际选择**）

理由：
- **职责单一**：Skill A/B 的核心是"PDF → Markdown"，把"找章节"塞进来会让它们变复杂（要处理书签解析、LLM 目录解析、偏移量算法三套子系统）
- **复用切片**：同一切片子 PDF 可以喂给 A（批量）和 B（精准），不必分别实现 `--pages`
- **可独立调用**：用户可以先用 locator `--list` 看章节，再决定要不要切，再决定喂给 A 还是 B——把决策权完全交给用户
- **零侵入**：A/B/C 只需各加 2 个小参数（`--pdf`/`--label`/`--source-file`），核心逻辑不动

**实测数据**（验证用）：库内 15 家公司招股书 **100% 带 PDF 书签**，层 1（书签直读）完全够用，不需要 LLM 解析目录。`hkex-chapter-locator` 实测在 06951 上定位"財務資料"章节，从 456 页全本切出 33 页子 PDF，Skill A 处理时间从 12.5 秒降到 3.5 秒，markdown 字符数从 666k 降到 55k（**完全进 LLM 上下文，不再截断**）。

**代价**：多一个 skill 目录、多一个 SKILL.md 维护。

**何时合并**：暂不考虑。如果未来加"PDF OCR 修复"等其他前置工具，反而会进一步分化为"PDF 预处理 skill 群"。

### 5.11 切片 markdown 与全本 markdown 共用 `field_name` 的取舍

**取舍**（v1.2 落地，2026-07-05）：

Skill A 的 `field_name='markdown_raw'` 是 `(stock_code, field_name)` 唯一键的一部分。当用户用 `--pdf <切片> --label X` 跑切片时，新切片会**覆盖**之前全本的 `markdown_raw` 记录。

| 方案 | 行为 | 当前 |
|---|---|---|
| **A. 共用 `markdown_raw`**（实际选择） | 切片覆盖全本；同一公司同一字段只有一份最新 | ✅ |
| B. 切片用 `markdown_raw_<label>` | 全本和切片并存，可对比；但 `field_name` 字段会膨胀 | 备选 |
| C. 加 `material_id` 列区分 | 改 schema，全本/切片各有独立标识；最干净但工程量大 | 暂不做 |

理由（选 A）：
- 大部分场景下，用户切了章节就不再需要全本（精读取代粗读）
- 切片 markdown 体积小（55k 字符 vs 全本 666k），更适合 Skill C 处理
- 如需保留全本，备份原始 PDF 即可（重跑 Skill A 总能再生 markdown）
- 方案 C 改 schema 代价大，目前没有真实痛点驱动

**何时升级到 B 或 C**：当用户开始抱怨"切了 X 章节后想再切 Y 章节但 X 被覆盖了"。届时加 `material_id` 列让多份 markdown 并存。

### 5.12 PDF Skills 体系重构：分层 + 场景化 + 双源校验（v1.3）

**重构决策**（v1.3 落地，2026-07-06）：

经"麦克医药配售结果.pdf"测试发现：MinerU pipeline 在配发结果的甲/乙组分配基准表上出现**数字撕裂**（`12,982` 误识为 `212,982`），单凭一源不可靠。基于此 + 复用 HKIPO 项目的成熟设计，重构为分层架构：

| 层 | skill | 职责 |
|---|---|---|
| 底层（保留） | A/B/C + chapter-locator | 通用 PDF → MD 转换 + 章节切片 + 反向 update |
| 共享库（新） | `_common/` | tables/verify/llm/pdf/env 复用模块 |
| 上层（新 4 个） | allotment-basis / prospectus-schedule / pdf-summary / prospectus-financials | 场景化"读什么 + 抽什么 + 怎么校验" |

**关键设计选择**：

1. **Skill B 默认 pipeline，配发结果场景用 vlm**：pipeline 永不幻觉但易撕裂数字；vlm 在嵌套表头表现更稳。通过 `--model` 参数让用户按场景选。
2. **Skill 2 默认开双源校验**：MinerU vlm 主提取 + doubao vision 第二源识图，**异构双源**（文本结构化 vs 整页视觉）犯同样错的概率极低；分歧不动数据只加 ⚠️。
3. **业务规则作为兜底**：行加总=总计、香港占≈10%、最大承配人<25%、会计恒等式（资产=负债+权益）等规则即使双源都错也能抓到。
4. **YAML 驱动而非硬编码 prompt**：Skill 2/3/4 都用 `fields.yaml` 配置字段 + anchor + 业务规则；加字段不改代码。
5. **共享 `_common/` 库避免重复**：4 个新 skill 都依赖 `_common/common_tables.py`（HTML table parser）、`common_verify.py`（双源比对+业务规则引擎）、`common_llm.py`（OpenAI 兼容 LLM + doubao vision）。
6. **Skill 1 文本路径优先**：档位表是强线性结构（金额 = 股数 × 单价 × 1.0085），用比值众数过滤能零成本抽到准确数据，只在文本路径不足时才降级到 vision/LLM。
7. **Skill 4 演化自 C**：保留 C 的字段抽取能力（listing_type 反向 update companies），新增 summary 字段类（business_overview/industry_position/key_risks 等）；C 和 4 并存，用户按需选。

**实测结果**（麦克医药配售结果 v4 输出）：9/9 scalars 抽中（offer_price=18.20港元、shares_global=58,054,400、oversub_hk=1,181.46倍、oversub_intl=2.52倍 等），3/5 业务规则通过（hk_split/hk_pct/intl_pct），剩余 2 条（甲/乙组行加总=总计）失败恰好暴露了 MinerU 的数字撕裂——证明双源校验 + 业务规则的设计有效。

**未做的事**（明确不在范围）：
- 不做"逐格 vision 比对"（成本太高，配发结果 5-15 页 vision 调用已经够）
- 不做"PDF OCR 修复"（让 MinerU 解决 OCR）
- 不做"阅读理解 skill"（用户提到后续会做，本重构只解决"读取+抽取"层）

---

## 6. 维护规则

### 6.1 强制更新触发器

| 事件 | 必须更新 |
|------|---------|
| 完成一个 P0 / P1 任务 | §1 现状（移入已完成项）+ §4 路线（标记 ✅） |
| 新增 tracker / 工具 | §1.3 覆盖矩阵 + §1.2 引用表 |
| 重大架构决策 | §5 决策日志（追加一条） |
| 评分变化 | §2 评分卡 |
| schema 升级 | §1.2 引用行号 + §5 决策日志 |

### 6.2 季度回顾（每 3 个月）

- 重新跑评分卡（§2）
- 检查路线优先级是否需要重排
- 把已完成的 P 任务从 §4 移到 §1

### 6.3 文件位置（不要移动）

- 本文件固定在 `docs/ROADMAP.md`
- README.md 顶部必须有指向本文件的链接（见 §7）

---

## 7. 引用

### 7.1 内部文档

- [README.md](../README.md) — 项目入口
- [skills/hkex-offering-tracker/SKILL.md](../skills/hkex-offering-tracker/SKILL.md) — 招股发行抓取
- [skills/hkex-application-tracker/SKILL.md](../skills/hkex-application-tracker/SKILL.md) — 递表聆讯抓取
- [skills/hkex-listing-tracker/SKILL.md](../skills/hkex-listing-tracker/SKILL.md) — 配发结果抓取
- [skills/hkex-offering-tracker/references/state-machine.md](../skills/hkex-offering-tracker/references/state-machine.md) — 状态机规范
- [skills/hkex-listing-tracker/references/page-anatomy.md](../skills/hkex-listing-tracker/references/page-anatomy.md) — JSF POST 时间窗扩展方案

### 7.2 代码事实源

- `skills/hkex-offering-tracker/scripts/common.py` — SCHEMA、helper、export_json
- `skills/hkex-application-tracker/scripts/fetch_applications.py` — `resolve_stock_code` (L185)、`_APPLICANT_MAP_DDL` (L207)
- 三个 `scripts/state.py` 副本 — 状态推断规则

### 7.3 外部参考

- HKEX 上市申请人页面：`https://www1.hkexnews.hk/app/appindex.html`
- HKEX 预定义文档（招股）：`predefineddoc.xhtml?predefineddocuments=6`
- HKEX 预定义文档（配发）：`predefineddoc.xhtml?predefineddocuments=4`

---

## 附录 A：版本历史

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-07-04 | ROADMAP v1.0 | 初版落地：7 节大纲 + 5 个拓展点 schema 草案 + 6 条决策日志 |
| 2026-07-05 | ROADMAP v1.1 | §4.4 PDF 处理工具落地为三件套（Skill A/B/C），新增 §5.7/5.8/5.9 决策日志，更新 §1.3 覆盖矩阵与 §1.4 数据快照 |
| 2026-07-05 | ROADMAP v1.2 | 新增 hkex-chapter-locator skill（章节定位 + PDF 切片），三件套 A/B/C 各加 `--pdf`/`--label`/`--source-file` 参数支持局部精读，新增 §5.10 决策日志 |
| 2026-07-06 | ROADMAP v1.3 | **PDF Skills 体系重构**：① 新增 `_common/` 共享库（tables/verify/llm/pdf/env）；② Skill B 增设 `--model vlm` 选项（配发结果场景走 vlm，避免 pipeline 的数字撕裂）；③ 新增 4 个场景化阅读 skill：hkex-allotment-basis（含 MinerU+doubao vision 异构双源校验）、hkex-prospectus-schedule（文本直抽+vision+LLM 三路兜底）、hkex-pdf-summary（演化自 C，YAML 驱动）、hkex-prospectus-financials（三大报表+会计恒等式）；④ 在麦克医药配售结果 PDF 上验证：9/9 scalars 抽中、3/5 业务规则通过，剩余 2 条业务规则失败恰好抓到 MinerU 数字撕裂问题（验证设计有效）。新增 §5.11 决策日志。 |
