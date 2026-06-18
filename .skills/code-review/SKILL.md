---
name: code-review
description: Review code changes for bugs, regressions, security risks, and missing tests.
when_to_use: Use for review, audit, risk analysis, finding bugs, or assessing test coverage.
---

# Code Review Skill

## Workflow

1. Identify the files, entry points, and behavior under review.
2. Read tests or examples that define the expected behavior.
3. Prioritize findings by severity and user impact.
4. Cite concrete files and lines when possible.
5. Keep summaries secondary to findings.

## Output Rules

- Lead with findings, ordered by severity.
- Include missing tests or residual risks.
- If no issues are found, say that clearly and name remaining uncertainty.
- Do not rewrite unrelated code during a review unless the user asks.
