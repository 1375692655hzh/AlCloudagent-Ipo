# Skill 编写规范

本规范同时满足 [Cursor Agent Skills](https://docs.cursor.com/) 和 [Hermes Agent](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/skills.md) 的要求,基于 [agentskills.io](https://agentskills.io/specification) 开放标准。

## 目录布局

每个 skill 是 `skills/` 下的一个独立子目录:

```
skills/
└── my-skill/
    ├── SKILL.md              # 必须,主入口
    └── references/           # 可选,深度细节
        ├── detail.md
        └── examples.md
```

- `references/` 只深一级。Hermes 对深层嵌套可能只读部分文件,不要嵌套到 `references/foo/bar/baz.md`。
- 路径分隔符**永远用正斜杠** `/`。Windows 上编辑时如果写成 `references\foo.md`,Linux 上的 Hermes 会找不到。`validate-skills.py` 会扫描这种错误并报警。

## SKILL.md 结构

```markdown
---
name: my-skill
description: <≤60 字符,第三人称,WHAT + WHEN>
disable-model-invocation: true
metadata:
  hermes:
    tags: [python, automation]
    category: coding
platforms: [linux, macos, windows]
---

# Skill Title

## When to Use
<触发条件>

## Procedure
1. <步骤>

## Pitfalls
- <已知坑>

## Verification
<如何验证成功>

## References
- [Detail](references/detail.md)
```

## Frontmatter 字段

### 必填

| 字段 | 约束 | 说明 |
|---|---|---|
| `name` | 小写字母 + 数字 + 连字符,`^[a-z0-9][a-z0-9-]{1,63}$`,≤64 字符 | 全仓唯一,避免 `helper`/`utils`/`tools` 等模糊名 |
| `description` | ≤60 字符(Hermes 硬约束,Cursor 允许 ≤1024 但取小值) | 第三人称("Processes X"),含 **WHAT + WHEN** |

### 可选(Cursor 私有,Hermes 忽略)

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `disable-model-invocation` | bool | `true` | `true` = 仅显式 `/skill-name` 才加载;省略 = 上下文匹配时自动加载 |

### 可选(Hermes 专用,Cursor 忽略)

```yaml
metadata:
  hermes:
    tags: [python, automation]                    # 任意标签
    category: devops                              # 分类
    fallback_for_toolsets: [web]                  # 仅当某 toolset 不可用时显示
    requires_toolsets: [terminal]                 # 仅当某 toolset 可用时显示
    fallback_for_tools: [web_search]              # 同上,粒度更细
    requires_tools: [terminal]
    config:                                       # 非敏感配置项,存入 config.yaml
      - key: myplugin.path
        description: 数据目录路径
        default: "~/myplugin-data"
        prompt: "Plugin data directory path"

required_environment_variables:                   # 敏感变量,缺失时本地提示
  - name: API_KEY
    prompt: 服务 API Key
    help: 从 https://example.com/developers 获取
    required_for: full functionality

platforms: [linux, macos, windows]                # OS 限制
```

## 关键原则

### 1. Progressive Disclosure(进度披露)

agent 不是一次性加载整个 skill,而是分三层:

| 层级 | 触发 | 加载内容 | 成本 |
|---|---|---|---|
| 0 | session 开始 | 仅 name + description(~3k tokens 全部 skill) | 低 |
| 1 | agent 判断相关 | 完整 SKILL.md | 中 |
| 2 | 需要细节 | references/xxx.md | 高 |

**写法**:`SKILL.md` 放"看到 description 就能决定要不要加载"的最小信息,细节用 `## References` 链接到 `references/*.md`。

### 2. description 写法

60 字符很紧,要把 **WHAT + WHEN** 都塞进去。

| 差 | 好 |
|---|---|
| `Helps with PDFs` | `Extracts text from PDF files. Use when user uploads a PDF.` |
| `I can analyze code` | `Reviews Python code for security issues. Use on .py files.` |
| `A useful tool` | `Generates release notes from git log since last tag.` |

第三人称,不要写 "I can" / "You can"。

### 3. 正文长度

- `SKILL.md` ≤ 500 行
- 超过的内容拆到 `references/`
- 每个 token 都在和上下文窗口竞争,只写 agent 不知道的信息

### 4. 术语一致

全 skill 内用同一个词指代同一个概念。不要一会儿 "script"、一会儿 "program"、一会儿 "snippet"。

### 5. 脆弱 vs 自由

| 任务类型 | 写法 |
|---|---|
| 文本生成、回答问题 | 给原则和示例,留自由度 |
| 文件操作、命令执行、API 调用 | 给精确步骤,甚至预写脚本 |

对脆弱操作,优先在 `scripts/` 下放预写好的脚本,skill 正文只说"运行 scripts/foo.py",而不是让 agent 现场生成代码。

## 校验

写完后跑:

```bash
python scripts/validate-skills.py
```

检查项:

- YAML frontmatter 可解析
- `name` 和 `description` 都存在
- `name` 符合格式约束
- `description` ≤60 字符(警告,不阻断)
- 正文未出现 Windows 反斜杠路径(警告)

## 不可写进 skill 的内容

- 密钥、token、密码(用 `required_environment_variables` 声明,运行时由 Hermes/Cursor 注入)
- 会过期的信息(版本号、临时 URL、"截至 2026 年 X 月"等)
- 与其他 skill 重复的内容(如果有共性,考虑拆成独立 skill 或共享 references)
