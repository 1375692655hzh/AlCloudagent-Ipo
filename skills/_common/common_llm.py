"""common_llm — OpenAI 兼容客户端 + 多模型路由 + vision 调用。

支持的 LLM 后端（环境变量）：
  LLM_API_KEY / LLM_BASE_URL / LLM_MODEL             默认文本模型（GLM/MiniMax/DeepSeek 任一）
  ARK_API_KEY / ARK_BASE_URL / ARK_VISION_MODEL      doubao vision（双源校验的第二源）

所有调用都遵循 OpenAI chat/completions 协议。
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


# ---------------------------------------------------------------------------
# 文本 LLM (OpenAI-compatible)
# ---------------------------------------------------------------------------


@dataclass
class LLMConfig:
    api_key: str
    model: str
    base_url: str | None = None
    timeout: float = 120.0

    @classmethod
    def from_env(
        cls,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> "LLMConfig":
        return cls(
            api_key=api_key or os.environ.get("LLM_API_KEY", ""),
            model=model or os.environ.get("LLM_MODEL", "glm-5.2"),
            base_url=base_url or os.environ.get("LLM_BASE_URL"),
        )


def chat_json(
    cfg: LLMConfig,
    system: str,
    user: str,
    *,
    temperature: float = 0.0,
    timeout: float | None = None,
) -> tuple[Any, str]:
    """调一次 chat.completions，返回 (parsed_json, raw_text)。

    若返回内容不是合法 JSON，parsed_json 为 None，raw_text 仍返回。
    自动剥离 ```json 包装和 reasoning 模型的 <think>...</think>。
    """
    headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
    if cfg.base_url:
        url = f"{cfg.base_url.rstrip('/')}/chat/completions"
    else:
        url = "https://api.openai.com/v1/chat/completions"

    with httpx.Client(timeout=timeout or cfg.timeout) as cli:
        r = cli.post(url, headers=headers, json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:400]}")
        data = r.json()
    raw = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    parsed = _parse_json_lenient(raw)
    return parsed, raw


def _parse_json_lenient(raw: str) -> Any:
    """宽松 JSON 解析：剥离 <think>...</think>、```json 包装。"""
    if not raw:
        return None
    s = raw.strip()
    # 剥离 reasoning think 块
    m = re.search(r"</think>", s, re.DOTALL)
    if m:
        s = s[m.end():].strip()
    # 剥离 ```json ... ```
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s)
    # 找最外层 { ... }
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# ---------------------------------------------------------------------------
# Vision LLM (Ark doubao 或任意 OpenAI-compatible vision API)
# ---------------------------------------------------------------------------


@dataclass
class VisionConfig:
    api_key: str
    model: str
    base_url: str
    timeout: float = 90.0

    @classmethod
    def from_env(cls) -> "VisionConfig | None":
        key = os.environ.get("ARK_API_KEY", "").strip()
        if not key:
            return None
        return cls(
            api_key=key,
            model=os.environ.get("ARK_VISION_MODEL", "doubao-seed-1-6-vision-250815"),
            base_url=(os.environ.get("ARK_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3").rstrip("/"),
            timeout=float(os.environ.get("ARK_TIMEOUT", "90")),
        )


def vision_chat(
    cfg: VisionConfig,
    image_path: Path,
    prompt: str,
    *,
    timeout: float | None = None,
) -> str:
    """对图片提问，返回文本内容。"""
    img_b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
    headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
    payload = {
        "model": cfg.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    with httpx.Client(timeout=timeout or cfg.timeout) as cli:
        r = cli.post(f"{cfg.base_url}/chat/completions", headers=headers, json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"vision HTTP {r.status_code}: {r.text[:400]}")
        data = r.json()
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()


def vision_chat_json(
    cfg: VisionConfig,
    image_path: Path,
    prompt: str,
    *,
    timeout: float | None = None,
) -> tuple[Any, str]:
    """对图片提问并解析 JSON。"""
    raw = vision_chat(cfg, image_path, prompt, timeout=timeout)
    return _parse_json_lenient(raw), raw


def call_with_retry(
    fn,
    *,
    retries: int = 2,
    base_delay: float = 2.0,
    label: str = "call",
    log_to_stderr: bool = True,
):
    """指数退避重试包装器。fn 应返回结果或抛异常。"""
    import time
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if log_to_stderr:
                print(f"  [{label}] attempt {attempt+1} failed: {str(e)[:160]}",
                      file=sys.stderr)
            if attempt < retries:
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
    raise last_err  # type: ignore[misc]
