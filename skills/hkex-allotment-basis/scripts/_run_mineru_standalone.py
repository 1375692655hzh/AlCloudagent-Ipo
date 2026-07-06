#!/usr/bin/env python3
"""独立运行 MinerU vlm/pipeline 模式（不走 Skill B 公司目录）。

为 hkex-allotment-basis 的 --pdf-path 模式服务。

用法：
    python _run_mineru_standalone.py --pdf <path> --out <md_path> [--model vlm|pipeline]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "hkex-pdf-reader-precision" / "scripts"))

from mineru_client import MinerUClient  # type: ignore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", choices=["pipeline", "vlm"], default="vlm")
    ap.add_argument("--token", type=str, default=None)
    args = ap.parse_args()

    if not args.pdf.is_file():
        print(f"ERROR: {args.pdf} not found", file=sys.stderr)
        return 2

    import os
    token = args.token or os.environ.get("MINERU_TOKEN")
    if not token:
        print("ERROR: MINERU_TOKEN not set", file=sys.stderr)
        return 2

    client = MinerUClient(token=token)
    print(f"MinerU client ready (model={args.model})", file=sys.stderr)

    files = [(args.pdf.name, None)]
    subs = client.request_upload_urls(files, language="ch", model_version=args.model)
    print(f"Got upload URL, batch_id={subs[0].batch_id}", file=sys.stderr)
    client.upload_file(subs[0], args.pdf)
    print("Uploaded", file=sys.stderr)

    def cb(results, elapsed):
        states = {}
        for r in results:
            states[r.state] = states.get(r.state, 0) + 1
        print(f"  [{int(elapsed)}s] {states}", file=sys.stderr)

    results = client.poll_until_done(subs[0].batch_id, progress_cb=cb)
    for r in results:
        if r.state == "failed":
            print(f"ERROR: MinerU failed: {r.err_msg}", file=sys.stderr)
            return 1

    out_dir = args.out.parent / f"_work_{args.pdf.stem}"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = client.download_markdown(results[0].full_zip_url, out_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"OK: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
