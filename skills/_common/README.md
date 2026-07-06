# _common — 共享库

被 4 个场景化阅读 skill 复用，避免代码重复。

## 模块

| 模块 | 用途 |
|---|---|
| [`common_env.py`](common_env.py) | `.env` 加载、API key 解析 |
| [`common_llm.py`](common_llm.py) | OpenAI 兼容文本 LLM + doubao vision + 重试 |
| [`common_pdf.py`](common_pdf.py) | PyMuPDF 关键词定位、PNG 渲染、页文本 |
| [`common_tables.py`](common_tables.py) | HTML `<table>` 解析（rowspan/colspan 展开）|
| [`common_verify.py`](common_verify.py) | 双源比对 + 业务规则引擎 + 档位表校验 |

## 用法

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_common"))

from common_env import load_env
from common_llm import LLMConfig, chat_json, VisionConfig, vision_chat_json
from common_pdf import find_pages_by_keywords, render_page
from common_tables import parse_html_table, extract_all_tables, find_table_by_anchor
from common_verify import compare_scalars, run_allotment_business_checks, validate_schedule_table

repo_root = load_env()
```

## 依赖

```
httpx
PyMuPDF
```

不需要 openai SDK（我们直接用 httpx 调 OpenAI 兼容协议）。
