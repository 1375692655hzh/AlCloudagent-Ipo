"""common_env — 统一 .env 加载。

读取项目根的 .env，把变量写入 os.environ（不覆盖已有的）。
支持 = 两端空格、单/双引号包裹、注释、空行。
"""
from __future__ import annotations

import os
from pathlib import Path


def find_repo_root(start: Path | None = None) -> Path:
    """从 start（默认本文件所在路径）向上找包含 .env 或 skills/ 的目录。"""
    p = (start or Path(__file__).resolve()).resolve()
    for cand in [p, *p.parents]:
        if (cand / ".env").is_file() or (cand / "skills").is_dir():
            return cand
        if cand.parent == cand:  # reached filesystem root
            break
    return p.parent  # fallback


def load_env(repo_root: Path | None = None, *, env_file: str = ".env") -> Path:
    """加载 <repo_root>/.env 到 os.environ（不覆盖已有值）。返回 repo_root。"""
    root = repo_root or find_repo_root()
    env_path = root / env_file
    if not env_path.is_file():
        return root
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        idx = line.index("=")
        k = line[:idx].strip()
        v = line[idx + 1:].strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v
    return root


def require(*keys: str) -> None:
    """断言环境变量已设置，缺则抛 RuntimeError。"""
    missing = [k for k in keys if not os.environ.get(k, "").strip()]
    if missing:
        raise RuntimeError(f"missing env vars: {missing}")
