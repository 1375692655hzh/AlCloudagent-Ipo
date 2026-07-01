#!/usr/bin/env python3
"""Validate all SKILL.md files under skills/*.

Checks:
- YAML frontmatter is parseable
- name and description fields exist
- name matches ^[a-z0-9][a-z0-9-]{1,63}$
- description length <= 60 chars (warning, non-blocking)
- body contains no Windows-style backslash paths (warning, non-blocking)

Usage: python scripts/validate-skills.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
DESCRIPTION_MAX = 60
BACKSLASH_PATH_PATTERN = re.compile(r"\(`?[\w./-]+\\\[\w./-]+`?\)")


def parse_frontmatter(text: str) -> tuple[dict | None, str | None, str]:
    """Return (frontmatter_dict_or_None, error_msg_or_None, body)."""
    if not text.startswith("---"):
        return None, "missing leading '---' frontmatter delimiter", text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, "malformed frontmatter (missing closing '---')", text
    fm_raw = parts[1]
    body = parts[2]
    try:
        import yaml

        fm = yaml.safe_load(fm_raw) or {}
        if not isinstance(fm, dict):
            return None, "frontmatter is not a mapping", body
        return fm, None, body
    except ImportError:
        # Fallback: minimal hand-parse for name/description only
        fm: dict[str, str] = {}
        for line in fm_raw.splitlines():
            m = re.match(r"^(\w+):\s*(.+)$", line.strip())
            if m and m.group(1) in ("name", "description"):
                fm[m.group(1)] = m.group(2).strip().strip("\"'")
        return fm or None, None if fm else "could not parse frontmatter (install pyyaml for full support)", body
    except Exception as e:
        return None, f"YAML parse error: {e}", body


def validate_skill(skill_md: Path) -> list[str]:
    """Return list of issues (empty = OK). Warnings prefixed with 'WARN:', errors with 'ERR:'."""
    issues: list[str] = []
    rel = skill_md.relative_to(skill_md.parents[2])

    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception as e:
        return [f"ERR: {rel}: cannot read file: {e}"]

    fm, err, body = parse_frontmatter(text)
    if err:
        return [f"ERR: {rel}: {err}"]
    assert fm is not None

    name = fm.get("name")
    if not name:
        issues.append(f"ERR: {rel}: missing 'name' field")
    elif not isinstance(name, str):
        issues.append(f"ERR: {rel}: 'name' must be a string")
    elif not NAME_PATTERN.match(name):
        issues.append(
            f"ERR: {rel}: 'name'='{name}' must match {NAME_PATTERN.pattern}"
        )

    desc = fm.get("description")
    if not desc:
        issues.append(f"ERR: {rel}: missing 'description' field")
    elif isinstance(desc, str):
        if len(desc) > DESCRIPTION_MAX:
            issues.append(
                f"WARN: {rel}: 'description' is {len(desc)} chars (> {DESCRIPTION_MAX} recommended for Hermes)"
            )
    else:
        issues.append(f"ERR: {rel}: 'description' must be a string")

    # Check for Windows-style backslash paths in body links
    for m in BACKSLASH_PATH_PATTERN.finditer(body):
        issues.append(
            f"WARN: {rel}: backslash path detected '{m.group(0)}' — use forward slashes for cross-platform"
        )

    # Name should match directory name
    dir_name = skill_md.parent.name
    if name and name != dir_name:
        issues.append(
            f"ERR: {rel}: 'name'='{name}' does not match directory name '{dir_name}'"
        )

    return issues


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    skills_dir = repo_root / "skills"

    if not skills_dir.is_dir():
        print(f"ERR: skills directory not found: {skills_dir}", file=sys.stderr)
        return 2

    skill_files = sorted(skills_dir.glob("*/SKILL.md"))
    if not skill_files:
        print(f"WARN: no SKILL.md files under {skills_dir}", file=sys.stderr)
        return 0

    all_issues: list[str] = []
    ok_count = 0
    for sf in skill_files:
        issues = validate_skill(sf)
        if not issues:
            ok_count += 1
            print(f"OK   {sf.relative_to(repo_root)}")
        else:
            all_issues.extend(issues)

    for issue in all_issues:
        print(issue)

    total = len(skill_files)
    print()
    print(f"Summary: {ok_count}/{total} OK, {len([i for i in all_issues if i.startswith('ERR')])} errors, {len([i for i in all_issues if i.startswith('WARN')])} warnings")

    # Non-zero exit if any errors
    return 1 if any(i.startswith("ERR") for i in all_issues) else 0


if __name__ == "__main__":
    sys.exit(main())
