---
name: hello-world
description: Greets the user. Use to verify skill loading or say hello.
disable-model-invocation: true
metadata:
  hermes:
    tags: [demo, test]
    category: misc
platforms: [linux, macos, windows]
---

# Hello World

## When to Use

User asks for a greeting, or wants to verify that skills are loading correctly from the shared repo.

## Procedure

1. Read the user's name from the request (or use "friend" if none given).
2. Respond with: `Hello, {name}! This skill is loaded from the shared repo.`

## Verification

The response includes a personalized greeting and the literal phrase "loaded from the shared repo".

## References

- [Usage examples](references/usage.md)
