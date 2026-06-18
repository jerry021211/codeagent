---
name: python-refactor
description: Refactor Python code with type hints, docstrings, compatibility, and focused tests.
when_to_use: Use for Python refactors, type hints, docstrings, main guards, API cleanup, or behavior-preserving edits.
---

# Python Refactor Skill

## Workflow

1. Read the target file and nearby tests before editing.
2. Preserve public behavior unless the user asks for a behavior change.
3. Keep edits scoped to the requested refactor.
4. Add type hints and docstrings where they clarify the public contract.
5. Run focused tests or explain why validation was not possible.

## Rules

- Prefer existing project style over new abstractions.
- Avoid broad formatting churn.
- Do not change unrelated files.
- Mention behavior compatibility and validation in the final response.
