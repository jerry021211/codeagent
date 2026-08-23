# codeagent

一个面向 coding agent 的最小 harness 骨架。

设计参考 `shareAI-lab/learn-claude-code` 的核心思想：

- agent loop 保持简单稳定：模型响应、执行工具、追加 `tool_result`、继续循环。
- 工具、权限、hooks、memory、subagent、skills、MCP 等能力放在 loop 外侧扩展。
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
  context/          # 上下文预算、压缩与 checkpoint 状态
  prompts/          # system prompt 动态组装
  skills/           # skill catalog 与按需加载
  memory/           # Markdown 长期记忆与模型选择
  tasks/            # 持久化 Task 领域模型
  runtime/          # 后台任务/运行时占位
  teams/            # 多 agent 通讯占位
  worktrees/        # worktree 隔离占位
  mcp/              # MCP 路由占位
  recovery/         # 分类、退避、fallback 与续写恢复
  events/           # 结构化运行事件与 Token 计量
  web/              # SQLite、FIFO 调度器与 FastAPI/SSE transport
```

说明：仓库中的 Web 运行时已是实际实现；`mcp/`、`teams/`、`worktrees/` 和旧的
`runtime/background.py` 仍是后续扩展点，不参与当前页面执行链路。

## Web 工作台

项目现在包含一个本机单用户 Coding Cockpit：左侧管理会话，中间显示对话、
流式回复和 Agent 动作，右侧展示 Token、持久化任务、子 Agent、恢复记录、文件改动与
脱敏后的调试事件。消息、Run、审批、模型调用用量、事件流和 checkpoint 持久化在
启动目录的 `.codeagent/state.db`。新建对话时可以从页面选择任意已有的本机项目
目录；每个对话永久绑定自己的工作区，后续执行使用该目录专属的 Agent 和工具状态。

安装并构建：

```powershell
python -m pip install -e ".[web]"
Set-Location web
npm.cmd install
npm.cmd run build
Set-Location ..
```

启动（只监听本机）：

```powershell
codeagent-web --workspace . --port 8765
```

然后访问 `http://127.0.0.1:8765`。开发前端时，可另开终端运行
`npm.cmd run dev`；Vite 会把 `/api` 代理到 8765 端口。

Web 运行时有以下边界：

- `--workspace` 是数据存储位置和新对话的默认目录，不再是唯一可打开的项目。
- 所有文件和搜索工具限制在当前对话绑定的工作区内，并防止符号链接逃逸。
- 根任务使用 FIFO 串行队列；不同工作区不共享可变的 Agent/Tool/CWD 状态。
- 工作区浏览 API 只列出本机目录名，不读取文件内容，并拒绝 UNC/网络路径。
- 危险操作通过页面审批；取消在模型调用、工具调用和退避等待之间的安全边界生效。
- SSE 事件带持久化序号，断线后可以继续回放；未完成 Run 在进程重启后标记为
  `interrupted`，不会盲目重放可能产生副作用的操作。
- Token 使用量来自 provider 返回的真实 usage，并按主模型、memory、context、
  子 Agent 等 `call_kind` 汇总；界面同时展示缓存读取量和缓存命中率，provider
  不返回 usage 时明确标记为不可用。
- 调试面板不会展示完整 system prompt 或隐藏推理内容，事件 payload 会截断和脱敏。

## 环境配置

复制 `.env.example` 为 `.env`，按你的模型服务填写：

```bash
MODEL_ID=claude-3-5-sonnet-latest
API_KEY=your_api_key_here
BASE_URL=
MAX_TOKENS=8000
MAX_ITERATIONS=50
STREAMING=false
CODEAGENT_PLANNING_MODE=auto
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

规划后端由 `CODEAGENT_PLANNING_MODE=auto|tasks|todo` 控制。`auto` 下 Web 和
交互式 CLI 使用持久化 Task System，单次 CLI、SDK 默认注册表和普通子 Agent
继续使用 TodoWrite；同一个 Agent 不会同时获得两套规划工具。

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

## Task System：当前会话直接执行

交互式会话注册 `TaskCreate`、`TaskGet`、`TaskList`、`TaskUpdate`。Task 持久化在
工作区 `.codeagent/state.db`，支持 TaskList、依赖、owner、原子认领和 Activity。
任务业务对象保持九字段：`id`、`subject`、`description`、`activeForm`、`owner`、
`status`、`blocks`、`blockedBy`、`metadata`；TaskList、revision 和时间戳位于
独立持久化外壳中。

Task 默认在当前 Conversation 中直接执行。开始 Ready 任务时用 `TaskUpdate` 设置
`in_progress`，完成代码和验证后设置 `completed`，不会为每个任务创建独立对话。
Web 右侧“任务”页可查看 Ready、Blocked、进行中和已完成任务，也可以创建任务，
或把指定任务作为普通 Run 继续交给当前会话。

## 子 Agent：subagent

`subagent` 是一个委派工具，由 `SubagentTool(spawn_fn=...)` 实现。工具层只保存 schema
和被注入的 `spawn_fn`，不 import `Agent`，因此不会形成循环依赖。`Agent` 默认
会给自身注入 `SubagentTool(spawn_fn=self._spawn_subagent)`。模型调用 `subagent` 时，父
Agent 会创建一个新的子 Agent：

- 子 Agent 使用全新的 `messages` 列表，只包含父 Agent 传入的子任务描述。
- 子 Agent 跑自己的 agent loop，可承担独立且边界清晰的调查、实现、修复、重构或验证，
  并继续调用读文件、搜索、bash、写入、编辑、`todo_write` 等工具。
- 子 Agent 的工具表会移除 `subagent`，避免递归生成子 Agent。
- 父 Agent 的上下文只收到子 Agent 的最终文本结论，不接收其中间消息和工具历史。
- 子任务描述只有明确要求修改代码时，子 Agent 才会编辑文件。
- 父 Agent 负责持久化 Task 状态、最终集成和验证，不把整个模糊目标交给子 Agent。
- 子 Agent 内部使用流式模型请求；失败会作为失败事件和 `Error:` 工具结果返回，
  父 Agent 不应原样重复提交同一个失败任务。

CLI 默认会给子 Agent 创建独立的 `TodoStore`、默认工具池和默认 hooks；权限检查
仍通过 hooks 执行，因此子 Agent 不会绕过权限策略。代码中如需自定义子 Agent
环境，可在构造 `Agent` 时传入 `subagent_environment_factory`，固定返回子 Agent 的
`ToolRegistry`、`HookManager` 和 `ContextManager`。

CLI 会在进入和退出子 Agent 时输出显式标志：

```text
[subagent enter] ...
[subagent exit] returned to parent agent
```

可以用下面的命令测试一次子 Agent 调用：

```bash
python -m codeagent --no-stream "请必须调用 subagent 工具，让子 Agent 读取 README.md 并总结这个项目的用途；拿到子 Agent 结果后，再用一句话告诉我结论。"
```

## 运行平台与命令 Shell

进程首次创建 Agent 或命令工具时会检测宿主操作系统，并缓存检测结果：

- Windows 优先使用 PowerShell（`pwsh` 或 Windows PowerShell），不可用时回退到
  `cmd.exe`。
- Linux 和 macOS 优先使用 Bash，不可用时回退到 POSIX `sh`。
- 命令工具会显式调用检测到的 shell，不再依赖 `subprocess` 的隐式平台默认值。
- 当前操作系统、实际 shell 和对应命令风格会作为运行时提醒发送给模型，避免在
  Windows 生成 POSIX-only 命令，或在 Linux/macOS 生成 PowerShell、cmd 命令。

为了兼容现有工具协议，工具名仍为 `bash`，但其描述会标明当前实际使用的 shell。

## 运行时 System Prompt 组装

Agent 不再在 `agent.py` 里硬编码 todo、subagent、skill、memory 等 prompt 文案。
每次模型调用前会通过 `PromptRuntime` 运行时组装 system prompt：

```text
Agent 收集真实运行状态
-> PromptRuntime 按固定顺序选择当前能力需要的模板
-> 按 static/dynamic 分区和 budget 组装
-> 返回 system prompt + trace/hash
```

System prompt 的顺序是：

- `static`：稳定身份和执行规则。
- `dynamic`：工具、todo、subagent、skill、memory 指引等能力信息。
- system 尾层：当前日期、工作区、操作系统和 shell 等运行时事实。

这些 system 内容保持稳定顺序；工具循环只在历史消息尾部追加 assistant/tool result，
不再临时插入并删除 `<system-reminder>` 用户消息。LLM memory 每个外部用户回合只选择
一次，选中内容随该回合用户消息持久化。DeepSeek 的上下文缓存自动生效，不发送会被
忽略的 `cache_control`；命中率按 provider 返回的缓存读取 token / prompt 输入 token
计算。

内置模板在：

```text
codeagent/prompts/templates/
```

普通 Agent 的身份提示只维护在 `templates/identity.md`；子 Agent 使用
`templates/subagent.md`。不再通过 `SYSTEM_PROMPT` 环境变量重复配置身份提示。

项目可以用 `.prompts/*.md` 覆盖内置模板，例如：

```text
.prompts/todo.md
```

可配置项：

```bash
PROMPT_TEMPLATE_DIR=.prompts
SYSTEM_PROMPT_BUDGET_CHARS=120000
SYSTEM_PROMPT_STATIC_BUDGET_CHARS=50000
SYSTEM_PROMPT_DYNAMIC_BUDGET_CHARS=70000
SKILL_CATALOG_BUDGET_CHARS=12000
PROMPT_TRACE=false
```

打开 `PROMPT_TRACE=true` 后，CLI 会打印每次组装的 prompt hash、字符数和包含的
fragment，方便调试和复现。

## 错误恢复：Error Recovery

Agent 使用独立的 `RecoveryRuntime` 保护模型调用。它不是简单 `try/except`，
而是把异常或特殊 `stop_reason` 分类成 `RecoveryReason`，再根据当前
`RecoveryState` 做恢复决策。

覆盖的主要路径：

- `429 rate limit`：指数退避 + jitter 后重试，尊重 `Retry-After`。
- `529 overloaded`：指数退避；连续多次 overloaded 后可切换 `FALLBACK_MODEL_ID`。
- `timeout/network/5xx`：有限重试。
- `prompt too long/context length/413`：触发 `ContextManager.reactive_compact()` 后重试。
- `max_tokens`：第一次提升输出 token 上限；仍截断时追加 continuation prompt 续写。
- `401/403/invalid request/invalid model/schema error`：不可恢复，快速失败。

主模型调用使用完整 recovery；memory selection side-query 使用轻量 recovery，失败时返回空
memory context，不影响主任务。工具执行错误不进入 recovery，而是作为 `tool_result`
返回给模型自我修正。

常用配置：

```bash
RECOVERY_ENABLED=true
FALLBACK_MODEL_ID=
RECOVERY_TRACE=false
```

高级配置：

```bash
RECOVERY_MAX_RETRIES=10
RECOVERY_BASE_DELAY_MS=500
RECOVERY_MAX_DELAY_MS=32000
RECOVERY_JITTER_RATIO=0.25
RECOVERY_MAX_CONTINUATIONS=3
RECOVERY_ESCALATED_MAX_TOKENS=64000
RECOVERY_OVERLOAD_FALLBACK_AFTER=3
RECOVERY_SIDE_QUERY_MAX_RETRIES=2
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

默认启用轻量级长期记忆。每次主模型调用前，Agent 会先发起一次轻量 side-query：

1. 后端列出 memory 的 `filename + name + description` 清单。
2. 使用当前配置的模型，让它从清单里选择真正有用的记忆文件，最多 5 个。
3. 模型必须返回严格 JSON，例如 `{"selected_memories":["project-style.md"]}`。
4. 后端只读取被选中的真实 markdown 文件，把完整内容注入本轮 system prompt。
5. 单轮注入总预算默认 60KB，避免 memory 把上下文撑爆。

你的项目使用 `deepseek-v4-pro` 时，这个 side-query 也会走同一个 Anthropic-compatible
客户端和同一个 `MODEL_ID`，不会硬编码 Sonnet。

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
MEMORY_SESSION_BUDGET_CHARS=60000
MEMORY_MAX_MEMORY_BYTES=50000
MEMORY_SELECTION_MODE=llm
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

默认启用 `CONTEXT_COMPACT_MODE=model`。同一代历史只允许在尾部追加，旧消息
不会因为工具结果变旧而再次改写，因此更利于模型的前缀缓存。

工具结果会在第一次加入 history 前定型：单个结果超过 80k 时保存完整文件并留下
路径和预览；同一批结果超过 200k 时从最大的结果开始进一步外置，直到整批回到
预算内。已发送的工具结果之后不再修改。

history 超过 300k 时，专用摘要模型生成结构化 checkpoint，完整旧 history 保存到
`.transcripts/`，摘要成为下一代的起点。自动压缩、手动 `compact()` 和上下文过长
恢复共用这个入口。换代后的第一次调用会冷启动缓存，之后继续追加并重新预热。

`RuntimeState` 会持续记录 generation、读取过的文件范围及次数、各工具调用次数、
任务进度、改动文件、命令、测试结果和工具 artifact 路径。这些状态会随 checkpoint
保存，并在摘要时提供给专用模型。

模型也可以主动调用：

```text
compact()
```

触发一次手动历史压缩。

可配置项：

```bash
CONTEXT_COMPACT_MODE=model   # off | model
SUMMARIZATION_MODEL_ID=your-summary-model
SUMMARIZATION_API_KEY=your-summary-api-key  # 留空时与主模型共用 API Key
CONTEXT_TOOL_RESULT_BUDGET_CHARS=200000
CONTEXT_SINGLE_TOOL_OUTPUT_MAX_CHARS=80000
CONTEXT_COMPACT_THRESHOLD_CHARS=300000
CONTEXT_SUMMARY_MAX_CHARS=12000
CONTEXT_TRANSCRIPT_DIR=.transcripts
CONTEXT_TOOL_OUTPUT_DIR=.task_outputs/tool-results
CONTEXT_REACTIVE_RETRIES=1
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
