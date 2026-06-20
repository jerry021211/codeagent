# codeagent

一个面向 coding agent 的最小 harness 骨架。

设计参考 `shareAI-lab/learn-claude-code` 的核心思想：

- agent loop 保持简单稳定：模型响应、执行工具、追加 `tool_result`、继续循环。
- 工具、权限、hooks、memory、task、skills、MCP 等能力放在 loop 外侧扩展。
- `Agent` 直接使用 Anthropic SDK client，保持 Anthropic 的 messages/tools 格式。

## 当前结构

```text
codeagent/
  agent.py          # 核心 Agent 类和 loop
  config.py         # 从环境变量读取模型和运行配置
  anthropic_client.py  # Anthropic SDK 调用与 streaming
  models.py         # 模型响应结构
  messages.py       # message/tool_use 规范化
  tools/            # 工具定义与注册表
  permissions/      # 工具执行权限策略
  hooks/            # agent lifecycle hooks
  context/          # context 管理占位
  prompts/          # system prompt 组装占位
  skills/           # skill 加载占位
  memory/           # 记忆系统占位
  tasks/            # 任务系统占位
  runtime/          # 后台任务/运行时占位
  teams/            # 多 agent 通讯占位
  worktrees/        # worktree 隔离占位
  mcp/              # MCP 路由占位
  recovery/         # 错误恢复占位
```

## 环境配置

复制 `.env.example` 为 `.env`，按你的模型服务填写：

```bash
MODEL_ID=claude-3-5-sonnet-latest
API_KEY=your_api_key_here
BASE_URL=
MAX_TOKENS=8000
MAX_ITERATIONS=50
STREAMING=false
SYSTEM_PROMPT=You are a coding agent. Use tools to solve tasks.
```

代码里可通过 `EnvironmentConfig.from_env()` 构建运行配置：

```python
from codeagent import (
    Agent,
    EnvironmentConfig,
    TodoStore,
    create_default_hooks,
    create_default_registry,
)

env = EnvironmentConfig.from_env()
agent_config = env.to_agent_config()
client = env.create_anthropic_client(stream=True, on_text=print)
todo_store = TodoStore()
tools = create_default_registry(todo_store=todo_store)
hooks = create_default_hooks(todo_store=todo_store)
agent = Agent(client=client, tools=tools, config=agent_config, hooks=hooks)
```

CLI 默认启用基础 hooks：

- `UserPromptSubmit`：记录工作目录
- `BeforeModelCall`：TODO 计划过久未更新时注入 reminder
- `PreToolUse`：权限检查和工具调用日志
- `PostToolUse`：大输出提醒
- `Stop`：工具调用次数统计

## 规划能力：todo_write

默认工具池包含 `todo_write`。它只维护当前进程内的一份 TODO
计划，不读文件、不运行命令、不写工作区。它的作用是让模型在多步骤任务前
先拆清楚步骤，并在执行过程中持续更新状态。

TODO 项只有三个状态：

- `pending`：还没开始
- `in_progress`：正在做，最多只能有一个
- `completed`：已经完成

当默认工具池里存在 `todo_write` 时，`Agent` 会自动在 system prompt 后追加
规划规则：多步骤任务、代码修改任务、或需要多次工具调用的任务，应先调用
`todo_write`，再使用 `read_file`、`bash`、`write_file`、`edit_file` 等执行类
工具。

默认 hooks 还会注册 reminder：如果模型连续 3 轮没有更新 TODO，就会在下一次
模型调用前注入一条 `<reminder>...</reminder>` 消息，提醒它更新计划或确认
下一步。这个机制只影响对话上下文，不会替 Agent 执行任何实际动作。

CLI 会在 `todo_write` 更新计划时打印用户可见的任务表：

- 第一次创建计划时打印 `[todo created]` 和完整任务表。
- 任务状态或内容发生变化时打印 `[todo updated]`、变化项和当前任务表。
- 所有任务完成时打印 `[todo completed]`。
- Agent 停止时如果仍有未完成任务，打印一次 `[todo final]`。
- 如果模型提交的 TODO 和当前任务表完全相同，不重复打印。

任务表使用固定状态标记：

```text
[ ] pending
[>] in_progress
[x] completed
```

如果一个进程里创建多个 Agent 或 subagent，应为每个 Agent 创建独立的
`TodoStore`，并把同一个 store 同时传给 `create_default_registry()` 和
`create_default_hooks()`。这样每个 Agent 的 TODO 计划互不污染。

## 子 Agent：task

`task` 是一个普通工具，由 `TaskTool(spawn_fn=...)` 实现。工具层只保存 schema
和被注入的 `spawn_fn`，不 import `Agent`，因此不会形成循环依赖。`Agent` 默认
会给自身注入 `TaskTool(spawn_fn=self._spawn_subagent)`。模型调用 `task` 时，父
Agent 会创建一个新的子 Agent：

- 子 Agent 使用全新的 `messages` 列表，只包含父 Agent 传入的子任务描述。
- 子 Agent 跑自己的 agent loop，可继续调用读文件、搜索、bash、写入、编辑、
  `todo_write` 等工具。
- 子 Agent 的工具表会移除 `task`，避免递归生成子 Agent。
- 父 Agent 的上下文只收到子 Agent 的最终文本结论，不接收其中间消息和工具历史。

CLI 默认会给子 Agent 创建独立的 `TodoStore`、默认工具池和默认 hooks；权限检查
仍通过 hooks 执行，因此子 Agent 不会绕过权限策略。代码中如需自定义子 Agent
环境，可在构造 `Agent` 时传入 `subagent_environment_factory`。如需手动组装工具
池，也可以调用 `create_default_registry(task_spawn_fn=...)` 显式加入 `TaskTool`。

CLI 会在进入和退出子 Agent 时输出显式标志：

```text
[subagent enter] ...
[subagent exit] returned to parent agent
```

可以用下面的命令测试一次子 Agent 调用：

```bash
python -m codeagent --no-stream "请必须调用 task 工具，让子 Agent 读取 README.md 并总结这个项目的用途；拿到子 Agent 结果后，再用一句话告诉我结论。"
```

## 按需能力：Skill Loading

默认启用两级 Skill Loading：

- 启动时扫描 `SKILLS_DIR` 指定的目录，默认是项目根目录 `.skills`。
- 每个 skill 放在独立目录中，并提供 `SKILL.md`。
- Agent 的 system prompt 只注入 skill catalog：名称、描述和适用场景。
- 完整 `SKILL.md` 不会常驻 system prompt；模型需要时调用 `load_skill(name)` 按需加载。
- `load_skill` 只能按已注册 skill 名称加载，不能传任意路径。

示例目录：

```text
.skills/
  code-review/
    SKILL.md
  python-refactor/
    SKILL.md
  agent-harness/
    SKILL.md
```

`SKILL.md` 使用简单 frontmatter：

```markdown
---
name: python-refactor
description: Refactor Python code with type hints, docstrings, compatibility, and focused tests.
when_to_use: Use for Python refactors, type hints, docstrings, main guards, API cleanup, or behavior-preserving edits.
---

# Python Refactor Skill

...
```

当前项目默认提供三个项目级 skill：

- `code-review`：代码审查、风险、测试缺口。
- `python-refactor`：Python 重构、类型标注、docstring、main guard。
- `agent-harness`：修改或解释本项目的 agent loop、tools、hooks、todo、subagent、skill loading。

可通过环境变量关闭或改目录：

```bash
ENABLE_SKILLS=true
SKILLS_DIR=.skills
```

## 长期记忆：Memory

默认启用轻量级长期记忆。启动时 Agent 只会收到 memory catalog，也就是记忆名称、
类型和一句话描述；完整内容不会常驻上下文，需要模型主动调用工具按需读取。

内置三个 memory 工具：

- `search_memory(query)`：按关键词搜索记忆摘要。
- `load_memory(name)`：按精确名称加载完整记忆。
- `remember(name, type, description, content)`：保存稳定、可复用的长期记忆。

记忆保存在 `.memory/` 目录中，每条记忆是一个 markdown 文件，`MEMORY.md` 是自动
生成的索引。默认 `.memory/` 已加入 `.gitignore`，避免把个人偏好或项目外信息误提交。

推荐记忆内容：

- 用户长期偏好，例如“回答时先给结论，再给关键理由”。
- 项目约定，例如“子 Agent 默认不能再委托子 Agent”。
- 重要决策，例如“memory 使用 markdown store，暂不引入向量库”。
- 可复用参考，例如“某类任务应优先加载某个 skill”。

不推荐保存：

- API key、token、密码等秘密。
- 当前任务的临时状态。
- 大段工具输出或大段代码。

父 Agent 默认拥有读写 memory 的工具；子 Agent 默认只拥有读 memory 的工具。这样子
Agent 可以利用长期记忆完成任务，但不会随手污染长期记忆。确实需要让子 Agent 写入时，
再打开 `MEMORY_ALLOW_SUBAGENT_WRITE=true`。

可配置项：

```bash
ENABLE_MEMORY=true
MEMORY_DIR=.memory
MEMORY_MAX_ITEMS_IN_PROMPT=50
MEMORY_MAX_LOADED_ITEMS=5
MEMORY_MAX_MEMORY_BYTES=50000
MEMORY_AUTO_EXTRACT=false
MEMORY_EXTRACT_RECENT_MESSAGES=12
MEMORY_CONSOLIDATE_THRESHOLD=30
MEMORY_CONSOLIDATE_MODE=simple   # simple | model
MEMORY_ALLOW_SUBAGENT_WRITE=false
```

`MEMORY_AUTO_EXTRACT=true` 时，Agent 会在每轮结束后让模型从最近对话中抽取稳定记忆。
默认关闭，是为了避免把临时对话误写成长期状态。`MEMORY_CONSOLIDATE_MODE=model`
会在记忆数量超过阈值后让模型合并重复记忆；默认 `simple` 只重建索引。

可以用下面的 query 测试手动记忆：

```bash
python -m codeagent --no-stream "请记住：这个项目里解释代码时先讲调用链，再讲关键函数。保存成长期记忆，然后告诉我保存的 memory 名称。"
```

也可以测试按需读取：

```bash
python -m codeagent --no-stream "按照我之前记录过的项目讲解偏好，解释 codeagent/agent.py 的主循环。"
```

## 上下文压缩：Context Compact

默认启用 `CONTEXT_COMPACT_MODE=simple`。Agent 会在每次模型调用前运行一组
cheap-first 压缩器，避免长会话、大文件读取和大量工具输出撑爆上下文。

压缩顺序：

1. `tool_result_budget`：最后一条 user message 里的工具结果总量超过预算时，
   把最大输出落盘到 `.task_outputs/tool-results/`，上下文只保留路径和预览。
2. `snip_compact`：消息数量超过阈值时裁掉中间历史，保留开头和最近上下文，并
   保证 `assistant(tool_use)` 和后续 `user(tool_result)` 不被拆开。
3. `micro_compact`：只保留最近几个完整 `tool_result`，旧的大结果替换为占位符。
4. `compact_history`：仍超过阈值时保存完整 transcript 到 `.transcripts/`，再用
   `RuntimeState` 生成结构化 `<context_summary>` 替换旧历史。
5. `reactive_compact`：如果 API 返回 prompt/context too long，会做一次应急压缩
   并重试，避免无限循环。

`RuntimeState` 会持续记录用户目标、当前 TODO、已加载 skill、子 Agent 结论、
改动文件、命令和测试结果。压缩后的摘要不是泛泛总结，而是保留继续工作需要的
结构化状态。

模型也可以主动调用：

```text
compact()
```

触发一次手动历史压缩。

可配置项：

```bash
CONTEXT_COMPACT_MODE=simple   # off | simple | model
CONTEXT_MAX_MESSAGES=50
CONTEXT_KEEP_HEAD_MESSAGES=3
CONTEXT_KEEP_TAIL_MESSAGES=47
CONTEXT_KEEP_RECENT_TOOL_RESULTS=3
CONTEXT_TOOL_RESULT_BUDGET_CHARS=200000
CONTEXT_SINGLE_TOOL_OUTPUT_MAX_CHARS=80000
CONTEXT_COMPACT_THRESHOLD_CHARS=300000
CONTEXT_SUMMARY_MAX_CHARS=12000
CONTEXT_TRANSCRIPT_DIR=.transcripts
CONTEXT_TOOL_OUTPUT_DIR=.task_outputs/tool-results
CONTEXT_REACTIVE_RETRIES=1
CONTEXT_MAX_COMPACT_FAILURES=3
```

权限策略参考 `s03_permission` 的三道闸门：

- 硬拒绝：`sudo`、`rm -rf /`、`shutdown` 等直接拒绝
- 需确认：`rm `、写入 `/etc/`、`chmod 777`、写工作区外文件
- 默认允许：普通读文件、搜索、工作区内写入和非危险命令

## 运行测试

单次运行：

```bash
python -m codeagent "读取 README.md，并用一句话总结这个项目"
```

不传 query 会进入交互模式：

```bash
python -m codeagent
```

```bash
python -m unittest discover -s tests
```
