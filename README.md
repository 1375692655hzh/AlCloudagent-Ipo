# AI Cloud Agent — Skills Repo

一个跨端共用的 AI Agent Skill 仓库。同一份 `SKILL.md` 同时在 [Cursor](https://cursor.sh/) 本地 IDE 和云端 [Hermes Agent](https://hermes-agent.nousresearch.com/) 上运行,遵循 [agentskills.io](https://agentskills.io/specification) 开放标准。

> 📌 **长期规划在 [docs/ROADMAP.md](docs/ROADMAP.md)** —— 现状、缺口、路线、决策日志。任何新会话做架构决策前必读。

## 目录结构

```
.
├── skills/                            # Skill 集合（Hermes external_dirs 指向这里）
│   ├── hello-world/                   # 示例 skill
│   │   ├── SKILL.md
│   │   └── references/
│   ├── hkex-offering-tracker/         # 招股发行抓取工具
│   │   ├── SKILL.md                   # 抓 predefineddocuments=6，下载全球发售招股书 PDF
│   │   ├── scripts/
│   │   │   ├── fetch_offerings.py
│   │   │   ├── common.py              # 与 application-tracker / listing-tracker 共享的 DB/下载/JSON 助手
│   │   │   ├── state.py               # doc_type → listing_stage 推断规则（三 skill 副本同步）
│   │   │   └── requirements.txt
│   │   └── references/
│   ├── hkex-application-tracker/      # 递表聆讯抓取工具
│   │   ├── SKILL.md                   # 抓 appindex JSON API，下载申请版本 / 聆讯后资料集 PDF
│   │   ├── scripts/
│   │   │   ├── fetch_applications.py
│   │   │   ├── state.py               # 副本（须与其他副本同步）
│   │   │   └── requirements.txt
│   │   └── references/
│   │       └── json-api.md            # appindex JSON 端点与字段文档
│   └── hkex-listing-tracker/          # 配发结果抓取工具
│       ├── SKILL.md                   # 抓 predefineddocuments=4，双层过滤后下载配发结果 PDF
│       ├── scripts/
│       │   ├── fetch_listings.py      # 复用 common.py + 加 Layer 1（白名单+负向）+ Layer 2（已跟踪）过滤
│       │   ├── state.py               # 副本（须与其他副本同步，含 LISTING_SOURCE_WHITELIST）
│       │   └── requirements.txt
│       └── references/
│           └── page-anatomy.md        # predefineddocuments=4 页面解析 + 双层过滤说明
│
│   # —— PDF 处理三件套（价值分层）——
│   ├── hkex-pdf-reader-batch/         # Skill A：批量入库（MarkItDown，零成本）
│   │   ├── SKILL.md
│   │   ├── scripts/batch_extract.py   # 支持 --pdf 任意路径 + --label 改文件名
│   │   └── requirements.txt
│   ├── hkex-pdf-reader-precision/     # Skill B：精准分析（MinerU，默认 pipeline，配发结果可用 --model vlm）
│   │   ├── SKILL.md
│   │   ├── scripts/
│   │   │   ├── precision_extract.py   # 支持 --pdf 任意路径 + --label 改文件名 + --model pipeline|vlm
│   │   │   └── mineru_client.py       # MinerU 精准 API 客户端封装
│   │   ├── references/mineru-api.md   # 接口规范
│   │   └── requirements.txt
│   └── hkex-pdf-field-extractor/      # Skill C：字段抽取（LLM，反向 update 4 维状态）
│       ├── SKILL.md
│       ├── scripts/
│       │   ├── extract_fields.py      # 支持 --source-file 读章节切片 markdown
│       │   ├── field_dictionary.py    # 6 个字段的 prompt + 校验规则
│       │   └── .env.example
│       ├── references/field-dictionary.md
│       └── requirements.txt
│
│   # —— 共享库（4 个场景化 skill 复用）——
│   └── _common/                       # HTML table parser + 双源校验 + LLM/PDF/env
│       ├── common_tables.py           # rowspan/colspan 展开 + anchor 匹配
│       ├── common_verify.py           # 异构双源比对 + 业务规则引擎 + 档位表校验
│       ├── common_llm.py              # OpenAI 兼容 LLM + doubao vision + 重试
│       ├── common_pdf.py              # PyMuPDF 关键词定位 + PNG 渲染
│       ├── common_env.py              # .env 加载
│       ├── test_smoke.py              # 冒烟测试
│       └── requirements.txt
│
│   # —— 4 个场景化阅读 skill（基于 _common 构建）——
│   ├── hkex-allotment-basis/          # Skill 2: 配发结果分配基准表（含双源校验）⭐
│   │   ├── SKILL.md
│   │   ├── fields.yaml                # 字段 + 业务规则配置
│   │   └── scripts/
│   │       ├── parse_allotment.py     # 主入口/编排
│   │       ├── extract_fields.py      # MinerU 后处理（HTML table parser）
│   │       ├── verify_vision.py       # doubao vision 第二源识图
│   │       ├── compare.py             # 双源比对
│   │       ├── business_checks.py     # 业务规则
│   │       ├── render_output.py       # 渲染最终 MD + 校验报告
│   │       └── _run_mineru_standalone.py  # 任意 PDF 模式的 MinerU 直跑
│   ├── hkex-prospectus-schedule/      # Skill 1: 招股书档位表（中签率计算输入）
│   │   ├── SKILL.md
│   │   ├── fields.yaml                # 定位关键词 + 校验规则
│   │   └── scripts/
│   │       ├── extract_schedule.py    # 文本直抽 + vision/LLM 兜底
│   │       └── verify_schedule.py     # 单调性 + 线性关系校验
│   ├── hkex-pdf-summary/              # Skill 4: 通用文字场景（演化自 C）
│   │   ├── SKILL.md
│   │   ├── fields.yaml                # 业务概览/风险/股东/募资用途 等
│   │   └── scripts/
│   │       └── extract_summary.py     # YAML 驱动 LLM 字段抽取
│   └── hkex-prospectus-financials/    # Skill 3: 招股书财务表格（三大报表）
│       ├── SKILL.md
│       ├── fields.yaml                # 字段定义 + 会计恒等式
│       └── scripts/
│           ├── extract_financials.py  # row_anchor 定位 + 多年度对齐
│           └── sanity_checks.py       # 会计恒等式 + 利润率区间
│
│   # —— 章节定位（局部精读前置）——
│   └── hkex-chapter-locator/          # 章节定位 + PDF 切片
│       ├── SKILL.md
│       ├── scripts/
│       │   ├── locate_chapter.py      # 主入口（含切片功能）
│       │   ├── bookmark_reader.py     # 层 1：PDF 书签直读
│       │   ├── offset_calculator.py   # 层 2 辅助：页脚投票算偏移量
│       │   └── toc_parser.py          # 层 2 主路径：LLM 解析目录
│       ├── references/page-number-systems.md  # 招股书三套页码体系
│       └── requirements.txt
├── data/                              # 运行时数据（gitignored）—— 所有 HKEX 工具共用
│   ├── state.db                       # 共享 SQLite 索引库（5 张表）
│   ├── manifest.json                  # agent 主接口（schema v2.2）
│   ├── views/                         # 跨公司聚合视图
│   │   ├── by_stage.json              # 按上市阶段切片
│   │   ├── by_method.json             # 按上市方式切片
│   │   └── by_type.json               # 按 AH 类型切片
│   └── companies/<code>_<name>/       # 每公司一个目录（三库架构）
│       ├── company.json               # 三库索引 + 状态 + 历史
│       ├── docs/*.pdf                 # Raw 素材库（HKEX PDF）
│       ├── info/*.json                # Derived 信息库（Skill A/B/C/1/2/3/4 产出）
│       ├── info/precision/*.md        # Skill B 高精度 Markdown（与 batch 并存）
│       ├── info/allotment_full/       # Skill 2 配发结果全套（含 校验报告.md）
│       ├── info/summary/              # Skill 4 业务概览/风险/股东 等
│       ├── info/financials/           # Skill 3 三大报表
│       └── reports/*.md               # Analysis 报告库（人工/AI 写）
├── docs/
│   ├── skill-authoring-guide.md       # 编写规范
│   ├── deployment.md                  # 部署步骤
│   └── skill-template/                # 复制即用模板
└── scripts/
    ├── new-skill.sh                   # Linux/macOS：新建 skill 骨架
    ├── new-skill.ps1                  # Windows：同上
    └── validate-skills.py             # 校验所有 SKILL.md 的 frontmatter
```

### HKEX IPO 抓取工具（三件套）

三个 sibling skill 共用 `data/state.db`，覆盖 IPO 生命周期四个阶段：

| Skill | 中文名 | 数据源 | 覆盖阶段 |
|---|---|---|---|
| `hkex-application-tracker` | 递表聆讯抓取工具 | `appindex.html` 背后的 `ncms/json/eds/*.json` | 递表、聆讯 |
| `hkex-offering-tracker` | 招股发行抓取工具 | `predefineddoc.xhtml?predefineddocuments=6`（HTML） | 招股 |
| `hkex-listing-tracker` | 配发结果抓取工具 | `predefineddoc.xhtml?predefineddocuments=4`（HTML） | 已上市（仅已跟踪公司，7 天窗口） |

状态机贯通：遞表 → 聆訊 → 招股 → 已上市。公司状态模型为 4 维：`listing_stage`（上市阶段，由抓取工具填充）/ `listing_type`（AH 类型，待 PDF 工具识别）/ `listing_method`（上市方式，启发式填充 `-B/-P/-W`/GEM）/ `confirmed_name`（确认股票名称，初始=公司名）。

`hkex-listing-tracker` 用**双层过滤**避免污染数据库：Layer 1 用 `doc_type` 白名单 + 负向过滤排除供股/配售；Layer 2 检查 `stock_code` 必须已在 DB 中（即之前被其他 tracker 跟踪过），所以只推进已跟踪公司从「招股 → 已上市」。详见各 skill 的 `SKILL.md`。

### PDF 处理工具（三件套，价值分层）

三个 sibling skill 共用 `data/state.db` 的 `extractions` 表，把 Raw 库 PDF 转成 Derived 信息库。**核心设计原则：价值分层**——低价值大批量走免费工具，高价值小批量才花 API 额度。

| Skill | 中文名 | 引擎 | 触发场景 | 成本 |
|---|---|---|---|---|
| `hkex-pdf-reader-batch` (A) | 批量读取工具 | MarkItDown（本地） | 全库入库、历史回填、快速预览 | **0** |
| `hkex-pdf-reader-precision` (B) | 精准读取工具 | MinerU 精准 API（pipeline，**永不 vlm**） | 单家深度分析、财务表精读、配发结果精读 | 每日 1000 页免费 |
| `hkex-pdf-field-extractor` (C) | 字段抽取工具 | OpenAI-compatible LLM（GLM-5.2/MiniMax-M3 等） | 抽招股价/募资用途/基石/主要股东/listing_type | 按 token |

三件套数据流：

```
docs/*.pdf（三个 tracker 写入的 Raw 库）
    │
    │   ┌─── hkex-chapter-locator ──→ docs/_slices/<stem>_p<N>-<M>_<chapter>.pdf
    │   │   （章节定位 + PDF 切片，专为局部精读）
    │   │   三层定位：书签直读 / 目录LLM+页脚偏移 / 手动 --pages
    │   │
    ├──→ Skill A (MarkItDown)     ──→ info/<stem>.md            [extractor=markitdown_batch_v1]
    │   │   支持全本 PDF 或切片 PDF（--pdf 接任意路径）
    ├──→ Skill B (MinerU)         ──→ info/precision/<stem>.md  [extractor=mineru_pipeline_v1]
    │   │   （与 Skill A 并行存在，不覆盖）
    │   │   同样支持切片 PDF
    └──→ Skill C (LLM 抽字段)     ──→ info/<field>.json
                                     + UPDATE companies SET listing_type=, confirmed_name=
                                     数据源选择：
                                       默认 auto（precision 优先，回退 batch）
                                       --source-file 指定章节切片 markdown（精读场景）
```

Skill C 抽到 `listing_type` / `confirmed_name` 后**反向更新 `companies` 表**，闭合 4 维状态模型。详见 [docs/ROADMAP.md](docs/ROADMAP.md) §4.4 与各 skill 的 `SKILL.md`。

### 章节定位工具（局部精读前置）

`hkex-chapter-locator` 把"招股书第 X 章节"转成"PDF 第 N-M 页"并切出子 PDF，让 Skill A/B/C 不必处理全本：

| 场景 | 命令 |
|---|---|
| 探测库内 PDF 书签覆盖率 | `locate_chapter.py --probe-bookmarks` |
| 列出某公司招股书所有章节 | `locate_chapter.py --company 06951 --list` |
| 定位"財務資料"并切片 | `locate_chapter.py --company 06951 --chapter "財務資料" --slice` |
| 手动指定页范围切片 | `locate_chapter.py --company 06951 --pages 204-236 --slice` |

实测库内 15 家公司 PDF **100% 带书签**（30+ 章节），层 1 直接覆盖，不需要 LLM。详见 [hkex-chapter-locator/SKILL.md](skills/hkex-chapter-locator/SKILL.md)。

### 公司库三库架构（v2.2）

`data/companies/<code>_<name>/` 下每公司一个目录，分三层存储：

| 层 | 子目录 | 内容 | 可重建？ |
|---|---|---|---|
| Raw 素材库 | `docs/` | HKEX 原始 PDF（递表/聆讯/招股/分配） | ✅ 重跑 fetcher |
| Derived 信息库 | `info/` | PDF 提取的结构化 JSON（财务/股东/募资用途等） | ✅ 重跑 PDF 读取工具 |
| Analysis 报告库 | `reports/` | 人工/AI 写的分析报告（估值/风险/质量） | ❌ 含主观判断 |

`data/state.db` 是统一索引库，含 5 张表：`companies` / `ipo_documents` / `state_history` / `extractions` / `reports`。Agent 主入口读 `data/manifest.json`，跨公司聚合读 `data/views/by_{stage,method,type}.json`。详见 [skills/hkex-offering-tracker/references/state-machine.md](skills/hkex-offering-tracker/references/state-machine.md)。

## Quick Start

### 1. Clone 仓库

```bash
# 云端 VM(Ubuntu)
git clone <your-remote-url> ~/projects/ai-skills

# 或本地 Windows
git clone <your-remote-url> D:\path\to\ai-skills
```

### 2. 接入 Hermes(云端)

编辑 `~/.hermes/config.yaml`,加入 external_dirs:

```yaml
skills:
  external_dirs:
    - ~/projects/ai-skills/skills
```

重载:

```bash
hermes skills reload
```

然后在 Hermes 里运行 `/hello-world` 验证。

### 3. 接入 Cursor(本地)

**方案 A:项目级**(推荐,跟着仓库走)

仓库根已有 `.cursor/skills/` 软链或在 `.cursor/settings.json` 里指向 `skills/`。Cursor 打开本仓库时会自动发现。

**方案 B:用户级**(管理员 PowerShell)

```powershell
New-Item -ItemType Junction -Path "$env:USERPROFILE\.cursor\skills" -Target "D:\path\to\ai-skills\skills"
```

详细步骤见 [docs/deployment.md](docs/deployment.md)。

## 编写新 Skill(3 步)

```bash
# 1. 用脚本生成骨架
./scripts/new-skill.sh --name my-awesome-skill --category coding

# 2. 编辑 skills/my-awesome-skill/SKILL.md 的 frontmatter 和正文
# 3. 校验
python ./scripts/validate-skills.py
```

详见 [docs/skill-authoring-guide.md](docs/skill-authoring-guide.md)。

## 双端兼容性

| 字段 | Cursor | Hermes |
|---|---|---|
| `name` | 必须 | 必须 |
| `description` | 必须(≤1024 字符) | 必须(建议 ≤60 字符) |
| `disable-model-invocation` | 可选,私有 | 忽略 |
| `metadata.hermes.*` | 忽略 | 专用(tags / category / 条件激活) |
| `platforms` | 忽略 | 按 OS 过滤 |

**结论**:一个 SKILL.md 同时写两套字段,互不冲突。description 控制在 60 字符内两端都通过。

## License

MIT,见 [LICENSE](LICENSE)。
