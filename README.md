# AI Cloud Agent — Skills Repo

一个跨端共用的 AI Agent Skill 仓库。同一份 `SKILL.md` 同时在 [Cursor](https://cursor.sh/) 本地 IDE 和云端 [Hermes Agent](https://hermes-agent.nousresearch.com/) 上运行,遵循 [agentskills.io](https://agentskills.io/specification) 开放标准。

## 目录结构

```
.
├── skills/                       # Skill 集合(Hermes external_dirs 指向这里)
│   └── hello-world/              # 示例 skill
│       ├── SKILL.md
│       └── references/
├── docs/
│   ├── skill-authoring-guide.md  # 编写规范
│   ├── deployment.md             # 部署步骤
│   └── skill-template/           # 复制即用模板
└── scripts/
    ├── new-skill.sh              # Linux/macOS:新建 skill 骨架
    ├── new-skill.ps1             # Windows:同上
    └── validate-skills.py        # 校验所有 SKILL.md 的 frontmatter
```

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
