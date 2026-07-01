# Hello World — Usage Examples

## Example 1: Basic greeting

**User says:** `/hello-world`

**Agent responds:**

> Hello, friend! This skill is loaded from the shared repo.

## Example 2: With a name

**User says:** `/hello-world I'm Alice`

**Agent responds:**

> Hello, Alice! This skill is loaded from the shared repo.

## How this file is loaded

Hermes and Cursor both use **progressive disclosure**:

1. The skill index (name + description) is loaded into the system prompt at session start.
2. When the agent decides this skill is relevant, it loads the full `SKILL.md`.
3. Only if the agent needs more detail does it load this `references/usage.md` file.

That keeps token usage low when the skill isn't needed.
