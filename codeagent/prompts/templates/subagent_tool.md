Use the subagent tool for an independent, bounded coding work unit that needs several tool calls. Good uses include focused investigation, implementation, bug fixing, a contained refactor, or targeted validation.

Do not delegate trivial one-step work, an ambiguous whole-project goal, final integration, or work that overlaps files you are actively changing. Keep tightly dependent steps in the parent conversation. Use no more than three subagents for one parent task.

Write a self-contained description containing:

- the requested action and goal;
- the exact scope or allowed files;
- important constraints and interfaces;
- clear completion conditions;
- the validation to run.

Explicitly ask for implementation, modification, or fixing when the child should edit files. Otherwise it will inspect and report only.

The child starts with fresh conversation history and returns only its final report. Use that report for integration and verification; do not repeat the same investigation without a concrete reason. If a subagent returns an error, inspect the cause and either fix it or take over the work; do not submit the same failed assignment unchanged. The parent owns persistent task updates and the final integrated answer.
