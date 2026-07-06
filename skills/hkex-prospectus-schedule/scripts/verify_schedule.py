#!/usr/bin/env python3
"""对档位表做单调性 + 线性关系的二次校验，独立输出。

复用 _common.common_verify.validate_schedule_table。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "_common"))

from common_verify import validate_schedule_table  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path, required=True,
                    help="extract_schedule.py 的输出 JSON")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    data = json.loads(args.input.read_text(encoding="utf-8"))
    rows = data.get("rows", [])
    # 转成 [[shares, amount, ...], ...]
    table_rows = [[r.get("股数"), r.get("金额")] for r in rows]
    result = validate_schedule_table(
        table_rows, shares_col=0, amount_col=1,
        offer_price=data.get("offer_price"),
        min_rows=10,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"OK: {args.out}")
    print(f"  confidence: {result['confidence']}")
    for i in result["issues"]:
        print(f"  - {i}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
