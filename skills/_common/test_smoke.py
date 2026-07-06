"""Smoke test for _common modules."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "_common"))

from common_env import load_env, find_repo_root
from common_llm import (
    LLMConfig, chat_json, VisionConfig,
    vision_chat, vision_chat_json, call_with_retry,
)
from common_pdf import (
    find_pages_by_keywords, render_page, page_count, page_text,
)
from common_tables import (
    parse_html_table, extract_all_tables, find_table_by_anchor,
    flatten_md_for_scalar, match_anchor, normalize_md_for_matching,
)
from common_verify import (
    compare_scalars, run_allotment_business_checks, validate_schedule_table,
    to_num, normalize_for_compare, check_approx_equal, check_row_sum_vs_total,
)
print("all imports OK")

# Test parse_html_table with nested header (colspan + rowspan)
html = (
    '<table>'
    '<tr><td colspan="3"></td><td rowspan="2">獲配發H股 佔所申請H股 總數的概約</td></tr>'
    '<tr><td>申請H股</td><td>有效申請</td><td></td></tr>'
    '<tr><td>數目</td><td></td><td>數目分配/抽籤基準</td><td>百分比</td></tr>'
    '<tr><td>200</td><td>44,060</td><td>44,060名申請人中有1,322名獲配發200股H股</td><td>3.00%</td></tr>'
    '</table>'
)
rows = parse_html_table(html)
print(f"parsed {len(rows)} rows")
for i, r in enumerate(rows):
    print(f"  row {i}: {r}")

# Test compare
res = compare_scalars(
    {"offer_price": {"value": "7.20港元", "name": "發售價"},
     "shares_hk_final": {"value": "8,516,500", "name": "香港公開發售"}},
    {"offer_price": "7.20港元", "shares_hk_final": "8,516,501"},
)
print("\ncompare result:", res["summary"])

# Test business checks
biz = run_allotment_business_checks(
    scalars={
        "shares_global": {"value": "85,162,500"},
        "shares_hk_final": {"value": "8,516,500"},
        "shares_intl_final": {"value": "76,646,000"},
    },
    table_a_rows=[
        ["申請H股", "有效申請", "基準", "百分比"],
        ["200", "44,060", "...", "3.00%"],
        ["總計", "44,060", "", ""],
    ],
    table_b_rows=[],
)
print("\nbusiness checks:", biz["summary"])

# Test schedule validate
sched = validate_schedule_table(
    [["甲组", "500", "5,131.24", "", ""], ["甲组", "1000", "10,262.48", "", ""]],
    offer_price=10.0,
    min_rows=5,
)
print("\nschedule validation:", sched)
