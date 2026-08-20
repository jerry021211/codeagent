## Persistent task planning

Use TaskCreate, TaskGet, TaskList, and TaskUpdate for meaningful multi-step work.
Do not create tasks for tiny execution steps such as reading one file or running
one command.

- Call TaskList before creating tasks so you do not duplicate existing work.
- Put context, requirements, and verifiable completion conditions in description.
- Express real prerequisites with addBlocks or addBlockedBy.
- Before starting a ready task, set it to in_progress; the harness assigns you as owner.
- Work on at most one in_progress task at a time.
- Perform the work directly in this conversation, then immediately mark the task completed.
- A blocked task cannot be started or completed.
- A subagent may help with execution, but the parent agent owns and updates the task.
