#!/usr/bin/env python3
"""业务规则校验 — 调用 _common.common_verify.run_allotment_business_checks。

输入：extract_fields.py 输出的 fields.json（含 scalars + tables）
输出：{ "checks": [...], "summary": {...} }
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "_common"))

from common_verify import run_allotment_business_checks  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fields", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    data = json.loads(args.fields.read_text(encoding="utf-8"))
    scalars = data.get("scalars", {})
    tables = data.get("tables", {})

    table_a_rows = (tables.get("allotment_basis_a") or {}).get("rows", [])
    table_b_rows = (tables.get("allotment_basis_b") or {}).get("rows", [])
    placee_rows = (tables.get("placee_concentration") or {}).get("rows", [])

    result = run_allotment_business_checks(
        scalars=scalars,
        table_a_rows=table_a_rows,
        table_b_rows=table_b_rows,
        table_placee_rows=placee_rows or None,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: {args.out}")
    s = result["summary"]
    print(f"  total={s['total']} passed={s['passed']} failed={s['failed']}")
    for c in result["checks"]:
        flag = "✓" if c.get("passed") else "✗"
        print(f"  {flag} {c['id']:14s} {c['name']}")
        if not c.get("passed") and c.get("note"):
            print(f"      {c['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
