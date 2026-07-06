"""共享库 _common

被 4 个场景化阅读 skill 复用，避免代码重复。

模块：
  - common_env    .env 加载、API key 解析
  - common_llm    OpenAI 兼容客户端 + 多模型路由 (GLM / MiniMax / DeepSeek / doubao vision)
  - common_pdf    PyMuPDF 关键词定位、PNG 渲染
  - common_tables HTML table parser（rowspan/colspan 展开）
  - common_verify 双源比对 + 业务规则引擎
"""
