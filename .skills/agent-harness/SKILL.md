---
name: agent-harness
description: Work on this coding-agent harness, including agent loop, tools, hooks, todo, subagents, and skills.
when_to_use: Use when changing or explaining codeagent internals such as Agent, ToolRegistry, hooks, todo_write, subagent, prompts, or skill loading.
---

# Agent Harness Skill

## Workflow

1. Trace the request from CLI or tests into Agent.run.
2. Separate model prompt guidance from harness-enforced behavior.
3. Keep tool implementations independent from Agent unless a callback is injected.
4. Preserve hook ordering and permission checks.
5. Add focused tests for loop behavior, tool schemas, and edge cases.

## Design Rules

- Reuse ToolRegistry and HookManager instead of special-case dispatch.
- Keep subagent and skill loading context isolated from parent history unless returned as a tool result.
- Prefer small runtime extension points over large changes inside the main loop.
- Document user-visible behavior in README when adding a harness feature.
