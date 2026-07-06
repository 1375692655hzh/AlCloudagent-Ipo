#!/usr/bin/env python3
"""独立的财务合理性检查（会计恒等式 + 同比/环比合理性）。

复用 _common.common_verify + 本 skill 的 fields.yaml.sanity_checks 配置。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "_common"))

from common_verify import check_approx_equal, RuleResult  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path, required=True,
                    help="extract_financials.py 的输出 JSON")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    data = json.loads(args.input.read_text(encoding="utf-8"))
    # Reuse the sanity_checks that were already run during extraction,
    # but render them as a focused report.
    checks = data.get("sanity_checks") or []
    s = data.get("sanity_summary") or {
        "total": len(checks),
        "passed": sum(1 for c in checks if c.get("passed")),
        "failed": sum(1 for c in checks if not c.get("passed")),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "source": str(args.input),
        "summary": s,
        "checks": checks,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"OK: {args.out}")
    print(f"  {s['passed']}/{s['total']} passed")
    for c in checks:
        flag = "✓" if c.get("passed") else "✗"
        print(f"  {flag} {c.get('name','')}")
        if not c.get("passed") and c.get("note"):
            print(f"      {c['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
