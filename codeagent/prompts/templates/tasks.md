Use persistent Tasks only for meaningful multi-step work, not single reads or commands.

- Call TaskList first. For broad work, create 2 to 5 finishable phase tasks.
- TaskList returns summaries only. Before executing a listed Task, use TaskGet with its taskId to read the full description, completion conditions, and metadata.
- Every TaskCreate needs both subject and description; include verifiable completion conditions.
- Create prerequisites first and express dependencies with TaskCreate blockedBy.
- Before working on a ready Task, set it to in_progress. Keep at most one Task in progress.
- Perform the work directly in this conversation, then mark the phase completed before moving on.
- A blocked Task cannot start or complete. The Root Agent owns Task updates even when a Subagent helps.
