#!/usr/bin/env bash
# Create a new skill skeleton under skills/<name>/
# Usage: ./scripts/new-skill.sh --name <skill-name> [--category <category>]
set -euo pipefail

NAME=""
CATEGORY="misc"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name) NAME="$2"; shift 2 ;;
        --category) CATEGORY="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 --name <skill-name> [--category <category>]"
            echo "  --name      Skill slug (lowercase, digits, hyphens; 2-64 chars)"
            echo "  --category  Hermes category (devops|coding|research|misc). Default: misc"
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$NAME" ]]; then
    echo "Error: --name is required" >&2
    exit 1
fi

# Validate name format
if ! [[ "$NAME" =~ ^[a-z0-9][a-z0-9-]{1,63}$ ]]; then
    echo "Error: name '$NAME' must match ^[a-z0-9][a-z0-9-]{1,63}\$" >&2
    echo "  - lowercase letters, digits, hyphens only" >&2
    echo "  - must start with alphanumeric" >&2
    echo "  - length 2-64 characters" >&2
    exit 1
fi

# Resolve repo root (parent of scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE_DIR="$REPO_ROOT/docs/skill-template"
TARGET_DIR="$REPO_ROOT/skills/$NAME"

if [[ -d "$TARGET_DIR" ]]; then
    echo "Error: target already exists: $TARGET_DIR" >&2
    exit 1
fi

if [[ ! -d "$TEMPLATE_DIR" ]]; then
    echo "Error: template not found: $TEMPLATE_DIR" >&2
    exit 1
fi

# Copy template
mkdir -p "$TARGET_DIR"
cp -r "$TEMPLATE_DIR"/. "$TARGET_DIR/"

# Replace placeholders in SKILL.md
SKILL_FILE="$TARGET_DIR/SKILL.md"
if [[ -f "$SKILL_FILE" ]]; then
    # Derive human-readable title from slug
    TITLE=$(echo "$NAME" | sed 's/-/ /g' | awk '{for(i=1;i<=NF;i++)$i=toupper(substr($i,1,1))substr($i,2)}1')
    # Generate a minimal placeholder description (<60 chars) so validation passes
    DESC="TODO: Describe what '$NAME' does and when to use it."
    # Replace placeholders (handle CRLF and LF line endings)
    sed -i.bak \
        -e "s/^name: TODO_FILL_NAME\r\?$/name: $NAME/" \
        -e "s|^description: .*\r\?$|description: \"$DESC\"|" \
        -e "s/^    category: TODO_FILL_CATEGORY\r\?$/    category: $CATEGORY/" \
        -e "s/^# TODO: Skill Title\r\?$/# $TITLE/" \
        "$SKILL_FILE"
    rm -f "$SKILL_FILE.bak"
fi

echo "Created skill: $TARGET_DIR"
echo "Next steps:"
echo "  1. Edit $SKILL_FILE (fill in description, procedure, etc.)"
echo "  2. Run: python $REPO_ROOT/scripts/validate-skills.py"
