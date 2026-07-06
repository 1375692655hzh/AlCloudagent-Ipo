"""MinerU 精准 API 客户端封装（Skill B 内部使用）。

实现 https://mineru.net/apiManage/docs 的「精准解析 API」：
  - POST /api/v4/file-urls/batch    申请 OSS 签名上传 URL（批量）
  - PUT  <oss_url>                  客户端直传本地文件
  - GET  /api/v4/extract-results/batch/{batch_id}  轮询批量结果
  - 下载 full_zip_url 里的 zip 包，提取 full.md

设计要点：
  - 全程使用 `model_version='pipeline'`（**永不 vlm**，避免财务数字幻觉）
  - 单文件 200MB / 200 页限制。招股书超过 200 页时由调用方分段
    (page_ranges)，本 client 不自动分段（保持职责单一）
  - Token 优先级：参数 > 环境变量 MINERU_TOKEN > ~/.mineru/config.yaml
  - 错误码处理：返回结构化错误，由调用方决定 retry / fallback
"""
from __future__ import annotations

import io
import os
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import httpx
import yaml


MINERU_BASE = "https://mineru.net/api/v4"
DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
UPLOAD_TIMEOUT = httpx.Timeout(600.0, connect=30.0)  # 200MB 文件上传
DOWNLOAD_TIMEOUT = httpx.Timeout(300.0, connect=15.0)

# Pipeline 模型无幻觉，是财务数字场景的安全选择。
# vlm 模型可能在罕见情况下编造数字，本 skill 永不使用。
DEFAULT_MODEL_VERSION = "pipeline"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MinerUError(Exception):
    """Base."""


class MinerUAuthError(MinerUError):
    """Token invalid / expired (codes A0202 / A0211)."""


class MinerUQuotaError(MinerUError):
    """Daily quota exhausted (code -60018)."""


class MinerULimitError(MinerUError):
    """File too big or too many pages (codes -60005 / -60006)."""


class MinerURateLimit(MinerUError):
    """HTTP 429 (IP rate-limit) or task queue full (-60009)."""


class MinerUFailedTask(MinerUError):
    """Server returned state=failed."""


# Error code -> exception class mapping (from official docs).
ERROR_CODE_MAP = {
    "A0202": MinerUAuthError,
    "A0211": MinerUAuthError,
    -60005: MinerULimitError,
    -60006: MinerULimitError,
    -60009: MinerURateLimit,
    -60018: MinerUQuotaError,
}


def _raise_for_code(code, msg):
    cls = ERROR_CODE_MAP.get(code)
    if cls:
        raise cls(f"[{code}] {msg}")
    raise MinerUError(f"[{code}] {msg}")


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------


def resolve_token(explicit: str | None = None) -> str:
    """Resolve MinerU API token.

    Order: explicit arg > $MINERU_TOKEN > ~/.mineru/config.yaml
    """
    if explicit:
        return explicit.strip()
    env = os.environ.get("MINERU_TOKEN")
    if env:
        return env.strip()
    cfg = Path.home() / ".mineru" / "config.yaml"
    if cfg.is_file():
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            tok = data.get("token")
            if tok:
                return str(tok).strip()
        except Exception:
            pass
    raise MinerUAuthError(
        "MinerU token not found. Set $MINERU_TOKEN, write ~/.mineru/config.yaml "
        "with `token: <...>`, or pass --token. Get one at "
        "https://mineru.net/apiManage/token"
    )


# ---------------------------------------------------------------------------
# Data records
# ---------------------------------------------------------------------------


@dataclass
class SubmissionResult:
    """One file's submission outcome."""

    file_name: str
    batch_id: str
    upload_url: str
    page_range: str | None  # the page_range used (None = whole file)


@dataclass
class TaskStatus:
    """Polled status of one file in a batch."""

    file_name: str
    state: str  # waiting-file | pending | running | converting | done | failed
    full_zip_url: str | None = None
    err_msg: str | None = None
    extract_progress: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class MinerUClient:
    """Synchronous client. All network ops are blocking.

    Lifecycle:
        c = MinerUClient(token)
        subs = c.request_upload_urls([("file1.pdf", None), ("file2.pdf", "0-199")])
        for s in subs:
            c.upload_file(s, local_path=...)
        results = c.poll_until_done(subs[0].batch_id)
        for r in results:
            if r.state == "done":
                md = c.download_markdown(r.full_zip_url, dest_dir)
    """

    def __init__(self, token: str | None = None):
        self.token = resolve_token(token)

    # ----- public API -----

    def request_upload_urls(
        self,
        files: list[tuple[str, str | None]],
        *,
        model_version: str = DEFAULT_MODEL_VERSION,
        language: str = "ch",
        enable_table: bool = True,
        enable_formula: bool = False,  # 招股书无公式，关闭省时间
    ) -> list[SubmissionResult]:
        """Apply for OSS upload URLs.

        Args:
            files: list of (file_name, page_range_or_None). page_range examples:
                   "0-199", "200-399", or None for whole file.
        """
        url = f"{MINERU_BASE}/file-urls/batch"
        payload = {
            "files": [
                {"name": fname, **({"page_ranges": pr} if pr else {})}
                for fname, pr in files
            ],
            "model_version": model_version,
            "language": language,
            "enable_table": enable_table,
            "enable_formula": enable_formula,
        }
        resp = self._post(url, payload)
        data = resp["data"]
        return [
            SubmissionResult(
                file_name=files[i][0],
                batch_id=data["batch_id"],
                upload_url=data["file_urls"][i],
                page_range=files[i][1],
            )
            for i in range(len(files))
        ]

    def upload_file(self, sub: SubmissionResult, local_path: Path) -> None:
        """PUT local file to OSS signed URL. Streams to avoid loading 200MB."""
        with local_path.open("rb") as f:
            with httpx.Client(timeout=UPLOAD_TIMEOUT) as cli:
                r = cli.put(sub.upload_url, content=f)
                if r.status_code not in (200, 201):
                    raise MinerUError(
                        f"OSS upload failed: HTTP {r.status_code} "
                        f"for {local_path.name}"
                    )

    def poll_until_done(
        self,
        batch_id: str,
        *,
        timeout_s: float = 1800.0,  # 30 min default per batch
        interval_s: float = 5.0,
        progress_cb=None,
    ) -> list[TaskStatus]:
        """Block-poll until all tasks in batch reach done/failed."""
        url = f"{MINERU_BASE}/extract-results/batch/{batch_id}"
        start = time.time()
        last_log = 0.0
        while True:
            resp = self._get(url)
            results = self._parse_batch_results(resp)
            elapsed = time.time() - start

            # Status summary for logging
            states = {}
            for r in results:
                states[r.state] = states.get(r.state, 0) + 1
            if progress_cb:
                progress_cb(results, elapsed)
            elif elapsed - last_log >= 15.0:
                last_log = elapsed
                print(f"  [{int(elapsed)}s] {states}", file=sys.stderr)

            # Done when no task is in an in-progress state
            in_progress = {"waiting-file", "pending", "running", "converting"}
            if all(r.state not in in_progress for r in results):
                return results

            if elapsed > timeout_s:
                raise MinerUError(
                    f"Polling timed out after {int(elapsed)}s; batch_id={batch_id}"
                )
            time.sleep(interval_s)

    def download_markdown(self, full_zip_url: str, dest_dir: Path) -> Path:
        """Download the result zip and extract full.md.

        Returns the path to the extracted .md file. Other artifacts
        (layout.json, content_list.json, images/) are also extracted
        to dest_dir for downstream use.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=DOWNLOAD_TIMEOUT) as cli:
            r = cli.get(full_zip_url)
            if r.status_code != 200:
                raise MinerUError(
                    f"Download failed: HTTP {r.status_code} for {full_zip_url}"
                )
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            zf.extractall(dest_dir)
        # Find the markdown (file name varies: full.md, <stem>.md, etc.)
        candidates = list(dest_dir.rglob("*.md"))
        if not candidates:
            raise MinerUError(f"No .md found in extracted zip at {dest_dir}")
        # Prefer full.md
        for c in candidates:
            if c.name == "full.md":
                return c
        return candidates[0]

    # ----- internals -----

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _post(self, url: str, payload: dict) -> dict:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as cli:
            r = cli.post(url, json=payload, headers=self._headers())
        return self._parse_response(r)

    def _get(self, url: str) -> dict:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as cli:
            r = cli.get(url, headers=self._headers())
        return self._parse_response(r)

    @staticmethod
    def _parse_response(r: httpx.Response) -> dict:
        try:
            body = r.json()
        except Exception:
            raise MinerUError(f"Non-JSON response: HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code == 429:
            raise MinerURateLimit(f"HTTP 429 rate-limited")
        if body.get("code") != 0:
            _raise_for_code(body.get("code"), body.get("msg", ""))
        return body

    @staticmethod
    def _parse_batch_results(resp: dict) -> list[TaskStatus]:
        out: list[TaskStatus] = []
        for item in resp.get("data", {}).get("extract_result", []):
            out.append(TaskStatus(
                file_name=item.get("file_name", ""),
                state=item.get("state", "pending"),
                full_zip_url=item.get("full_zip_url"),
                err_msg=item.get("err_msg"),
                extract_progress=item.get("extract_progress", {}) or {},
            ))
        return out


# ---------------------------------------------------------------------------
# Pagination helper: split a PDF into 200-page chunks
# ---------------------------------------------------------------------------


def compute_page_ranges(total_pages: int, chunk: int = 200) -> list[str]:
    """Split total_pages into MinerU page_ranges strings.

    >>> compute_page_ranges(150, 200)
    ['0-199']  # whole doc fits in one chunk (we still cap at total)
    >>> compute_page_ranges(450, 200)
    ['0-199', '200-399', '400-449']
    """
    if total_pages <= 0:
        return [None]  # type: ignore[list-item]
    ranges = []
    i = 0
    while i < total_pages:
        end = min(i + chunk - 1, total_pages - 1)
        ranges.append(f"{i}-{end}")
        i = end + 1
    return ranges
