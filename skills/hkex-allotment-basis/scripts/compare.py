#!/usr/bin/env python3
"""交叉比对 MinerU 主提取 与 doubao vision 校验来源，输出字段级一致性报告。

输入：
  --primary  extract_fields.py 输出的 fields.json（含 scalars/tables）
  --verify   verify_vision.py 输出的 verify.json（含 scalars/tables）
  --out      compare.json

输出 JSON：
  {
    "scalars": { "fields": [...], "summary": {...} },
    "tables":  { "<id>": {"row_count_match": bool, "primary_rows": N, "verify_rows": M}, ... }
  }
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "_common"))

from common_verify import compare_scalars  # noqa: E402


def compare_tables(primary: dict, verify: dict) -> dict:
    """表格比对：行数 + 关键列加总。"""
    out: dict[str, dict] = {}
    p_tables = primary.get("tables", {}) or {}
    v_tables = verify.get("tables", {}) or {}
    for tid in set(p_tables.keys()) | set(v_tables.keys()):
        p_rows = (p_tables.get(tid) or {}).get("rows", [])
        v_rows = (v_tables.get(tid) or {}).get("rows", [])
        out[tid] = {
            "primary_rows": len(p_rows),
            "verify_rows": len(v_rows),
            "row_count_match": len(p_rows) == len(v_rows),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--primary", type=Path, required=True)
    ap.add_argument("--verify", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    p = json.loads(args.primary.read_text(encoding="utf-8"))
    v = json.loads(args.verify.read_text(encoding="utf-8"))

    p_scalars = p.get("scalars", {})
    v_scalars = v.get("scalars", {})

    scalars_cmp = compare_scalars(p_scalars, v_scalars)
    tables_cmp = compare_tables(p, v)

    out = {"scalars": scalars_cmp, "tables": tables_cmp}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    s = scalars_cmp["summary"]
    print(f"OK: {args.out}")
    print(f"  scalars: total={s['total']} matched={s['matched']} "
          f"mismatch={s['mismatch']} verify_missing={s['verify_missing']} "
          f"primary_missing={s['primary_missing']}")
    for tid, info in tables_cmp.items():
        if info["row_count_match"]:
            print(f"  table {tid}: {info['primary_rows']} rows (matched)")
        else:
            print(f"  table {tid}: primary={info['primary_rows']} vs verify={info['verify_rows']} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
