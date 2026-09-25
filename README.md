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
  runtime/          # 取消、活动监测、运行数据目录与 Team Supervisor
  teams/            # 团队规划、消息、Session、候选交付与执行权限
  worktrees/        # Git worktree 隔离、候选快照与现场校验
  mcp/              # 外部 MCP Server 配置、连接与工具适配
  recovery/         # 分类、退避、fallback 与续写恢复
  events/           # 结构化运行事件与 Token 计量
  web/              # SQLite、FIFO 调度器与 FastAPI/SSE transport
```

说明：Web 运行时、MCP、Team 和 Worktree 均已有实际实现。显式启用 Team 后，
`runtime/background.py` 中的 `TeamSupervisor` 负责调度团队执行，`teams/` 负责团队协作，
`worktrees/` 为代码任务提供独立工作区。

## Web 工作台

项目现在包含一个本机单用户 Coding Cockpit：左侧管理会话，中间显示对话、
流式回复和 Agent 动作，右侧展示 Token、持久化任务、子 Agent、恢复记录、文件改动与
脱敏后的调试事件。消息、Run、审批、模型调用用量、事件流和 checkpoint 持久化在
CodeAgent 外部数据目录的 `state/state.db`。新建对话时可以从页面选择任意已有的本机项目
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
- 根任务使用 FIFO 队列，默认最多 4 个会话并行；同一会话只能有一个排队或运行中的
  任务。`CODEAGENT_WEB_MAX_CONCURRENT_RUNS` 可设置正整数并发上限，设为 `1` 恢复
  串行执行。此上限不包含 Team 内部 worker；不同 Run 不共享可变的 Agent/Tool/CWD 状态。
- 等待回答、审批或模型重试仍占一个并发名额。多个会话可操作同一项目目录，但项目文件
  仍然共享，涉及重叠修改时应使用独立工作区/worktree。
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

普通 Agent、Discuss 和普通子 Agent 默认启用防循环与任务预算，Team 不在本次范围内。
保护状态独立于上下文压缩；Web checkpoint 保存状态，循环或预算停止会标记为失败，
CLI 单次执行返回非零退出码。规则、可调阈值、恢复语义和输入识别限制见
[防循环与执行预算](docs/loop-guard.md)。

## Discuss：只读讨论模式

用于代码阅读、审查和架构讨论，不要求先建计划。通过 `PreToolUse` hook 强制限制
工具执行；即使模型要求写入，也会返回 `tool.blocked`，不会进入审批或执行写工具。

- **Web**：点击输入框下方的模式按钮，在向上展开的菜单中选择 `Code · 编码` 或 `Discuss · 只读`；也可输入
  `/discuss` 切换。运行和排队期间不可切换，停止或完成后可切回编码。
  每条消息的模式随 Run 保存；重新打开会话时按最近一条用户消息恢复选择。
  切换模式后的下一条请求会在模型历史尾部追加运行时模式更新，明确旧回复中的模式
  已经过时；原有对话保持完整。旧版会话首次恢复时同样补充当前模式，之后同模式不重复追加。
- **CLI**：`python -m codeagent --discuss "解释这个项目的架构"`；交互模式支持
  `/discuss`、`/discuss on`、`/discuss off`，提示符显示 `[discuss] >`。
- **SDK**：构造 `Agent(..., prompt_mode=PromptMode.DISCUSS)`，或在普通 Agent
  空闲时调用 `agent.set_discuss_mode(True/False)`。
- **API**：`POST /api/conversations/{id}/runs` 的 JSON 支持
  `{"content":"解释架构","mode":"discuss"}`；省略 `mode` 时仍为 `normal`。

可使用读取、搜索、技能/记忆加载、`TaskGet`、`TaskList` 和上下文压缩。
文件写入、编辑、记忆保存、任务/TODO 更新、子 Agent、Team 和所有外部 MCP 工具
均被阻止，回合后的自动记忆维护也暂停。退出后继续遵守原来的权限策略。

Shell 只接收单条字面量命令和明确允许的选项，例如 `Get-Content -Raw README.md`、
`Get-ChildItem -Name`、`rg -n pattern codeagent`、`git status --short`。
`git diff/log/show` 必须加 `--no-ext-diff --no-textconv`，防止执行外部差异转换器。
复合命令、管道、重定向、脚本、网络命令和未知工具默认拒绝；复杂搜索优先使用原生工具。

这是 Agent 工具执行策略，不是操作系统沙箱；仍假定本机命令程序及配置可信。
会话、事件、checkpoint 和工具输出仍按原机制持久化到 Runtime 数据目录。
Web 的模式是每次请求的快照，不会终止其他会话或已存在的后台程序。

源码研究、原项目 hook 调用链、命令策略局限和本项目接入设计见
[Discuss 模式实现说明](docs/discuss-mode.md)。

## 用户提问：ask_user

CLI 和 Web 的主 Agent 可在需求、偏好或关键决策不明确时调用：

```json
{"question": "导出文件需要哪种格式？", "options": ["CSV", "JSON"]}
```

`options` 可省略，用户始终可以自由回答。调用会阻塞当前 Agent，直到收到非空回答；
等待没有自动超时，不会替用户选择答案或继续模型循环。当前 Run 占用一个并发名额，
其他名额可以继续执行其他会话；所有名额都在等待时，新任务排队。HTTP 服务仍可处理
回答和取消请求。多会话调度设计与验证见 [多会话并发说明](docs/concurrent-sessions.md)。

- CLI 在终端显示问题，接受选项编号或自由文本；空输入继续等待，Ctrl+C 中止。
- Web 在聊天输入区上方显示问题和回答框，点击选项后仍需提交；可用“停止”取消等待。
  问题和回答保存在 SQLite，刷新页面可恢复；服务重启会中断 Run 并取消未回答问题。
- Discuss 模式也可提问；Team 和独立子 Agent 工具池不新增交互入口。
- 回答作为本次 `ask_user` 的工具结果交回模型，随后继续同一个 Run。

SDK 通过 `create_default_registry(ask_user_fn=handler)` 注入同步回调，签名为
`handler(question: str, options: list[str]) -> str`；未注入时不暴露提问工具。
终端处理器可从 `codeagent.tools` 导入 `terminal_ask_user`。

Web 接口：`GET /api/runs/{run_id}/questions` 查看问题，
`POST /api/runs/{run_id}/questions/{question_id}/answer` 提交 `{"answer":"JSON"}`。
空回答返回 422；重复提交相同回答幂等，冲突回答或已取消问题返回 409。

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
CodeAgent 外部数据目录的 `state/state.db`，支持 TaskList、依赖、owner、原子认领和 Activity。
任务业务对象保持九字段：`id`、`subject`、`description`、`activeForm`、`owner`、
`status`、`blocks`、`blockedBy`、`metadata`；TaskList、revision 和时间戳位于
独立持久化外壳中。

`TaskList` 只返回 `id`、`subject`、`status`、`owner`、`blocks`、`blockedBy` 六个摘要字段，
用于浏览和选择任务；执行选中的任务前，用 `TaskGet(taskId)` 获取完整描述、验收条件和
metadata。`TaskGet` 返回完整九字段，`TaskList` 不携带描述、进度文案或 metadata。

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

项目只能通过一个追加文件提供项目级说明，不能覆盖 Root、Lead 或 Teammate 的
核心身份和安全规则：

```text
.prompts/project.md
```

`PROMPT_TEMPLATE_DIR` 是部署者显式配置的完整模板目录，只应指向受信任位置。

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

- 所有项目、工作区及 Team Worktree 共用同一份全局技能库，不扫描项目内的 `.skills`。
- 启动时扫描 `SKILLS_DIR` 指定的目录，默认是 `CODEAGENT_DATA_DIR/skills`。
- 相对路径统一相对于 CodeAgent 数据目录解析；也支持指定全局技能库的绝对路径。
- 每个 skill 放在独立目录中，并提供 `SKILL.md`。
- Agent 的 system prompt 只注入 skill catalog：名称、描述和适用场景。
- 完整 `SKILL.md` 不会常驻 system prompt；模型需要时调用 `load_skill(name)` 按需加载。
- `load_skill` 只能按已注册 skill 名称加载，不能传任意路径。

示例目录：

```text
<CODEAGENT_DATA_DIR>/skills/
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

全局技能库可存放以下 skill：

- `code-review`：代码审查、风险、测试缺口。
- `python-refactor`：Python 重构、类型标注、docstring、main guard。
- `agent-harness`：修改或解释本项目的 agent loop、tools、hooks、todo、subagent、skill loading。

可通过环境变量关闭或改目录：

```bash
ENABLE_SKILLS=true
SKILLS_DIR=skills
```

Windows 默认技能目录为 `%LOCALAPPDATA%\CodeAgent\data\skills`。
CLI 和 Web 使用相同的加载规则，切换项目不会切换技能目录。
升级旧配置时，将 `SKILLS_DIR=.skills` 改为 `SKILLS_DIR=skills`，并把原有技能目录
复制到全局技能库；程序不会自动导入新打开项目中的技能。

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

记忆按项目保存在 `CODEAGENT_DATA_DIR/workspaces/<workspace-id>/memory/`，不会写入
用户 Git 工作区或 Team Worktree。每条记忆是一个 markdown 文件；只有存在记忆记录时
才生成 `MEMORY.md` 索引。

推荐记忆内容：

- 用户长期偏好，例如“回答时先给结论，再给关键理由”。
- 项目约定，例如“子 Agent 默认不能再委托子 Agent”。
- 重要决策，例如“memory 使用 markdown store，暂不引入向量库”。
- 可复用参考，例如“某类任务应优先加载某个 skill”。

不推荐保存：

- API key、token、密码等秘密。
- 当前任务的临时状态。
- 大段工具输出或大段代码。

普通单 Agent 默认拥有读写 memory 的工具；同步子 Agent 默认只读，确实需要时可通过
`MEMORY_ALLOW_SUBAGENT_WRITE=true` 开放。任一非终态 Agent Team 存在期间，同一项目的
Root、Lead 和 Teammate 全部只读；Team 进入终态后 Root 自动恢复写权限。Team 执行结果
不会在结束后被自动回填进 Memory。

可配置项：

```bash
CODEAGENT_DATA_DIR=                 # 留空时使用系统用户数据目录
ENABLE_MEMORY=true
MEMORY_DIR=.memory                 # 旧工作区 Memory 的一次性只读导入位置
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

上下文管理面向普通主 Agent 和同步子 Agent，涵盖 CLI、普通 Web 与 SDK。
本轮不开发或验收 Team。`Agent.messages` 和 checkpoint 保存已接收历史，
每次发送给模型时另建视图；摘要和清理不会覆盖原记录。

默认规则是“够用就保留，接近上限再整理”：

- 完整请求超过 300k 字符，或本次估算输入加输出预留达到已配置窗口的 80%，
  才尝试清理旧的大工具内容；清理后仍有压力才调用摘要模型。
- 消息数、执行轮数不再独立触发摘要；上一轮很大也不会让已经缩小的当前请求反复压缩。
- 摘要只折叠合法的旧区间，保留当前用户原文、近期至少 2 个执行轮；跨回合至少留 12 条消息。
  候选摘要须让完整请求至少省下 256 字符及约 5%，估算输入 token 也要下降，才归档并提交。
- 跨回合继续保留未摘要的工具证据与附件，不再另走首尾裁剪。命令输出没有可靠归档就不清理。
- 旧工具结果和成功写入正文的默认清理门槛提高到 8k 字符；写参保留最近 2 条 assistant。
  清理只给身份、状态和读取说明，不再生成多语言结构摘录。失败、完整文件读取等仍受保护。
- 单个结果超过 80k 或整批超过 200k 字符时才按预算归档；取消 2k bash 提前归档。
- 每次摘要归档只写新覆盖的消息，并引用上一段。`load_context_history` 自动串联，
  消息编号连续；旧 checkpoint 不会看到之后新增的归档内容。

主请求、摘要和辅助模型调用都检查完整预算，token 仍是估算。摘要优先使用完整可见材料，
最小合法批次仍放不下才使用有损字段预览。失败、取消或没有足够缩减时保留旧摘要和水位；
失败及无收益尝试默认冷却 90 秒。45 秒摘要超时是 SDK I/O 超时，不是严格总墙钟期限。

摘要使用四节滚动记忆 prompt，保留目标、有效约束与授权边界、完成结果和验证范围、
有效决策、未决事项及文件标识符。本批结构化执行记录中的路径由代码逐字提取。
`summary_char_budget` 来自 `CONTEXT_SUMMARY_MAX_CHARS`，默认 **4000 字符**，不是 token 数；
按去除首尾空白后的 Python `len()` 检查，标题、换行、空格和标点都计入预算。
摘要请求不再发送 `max_tokens`，由兼容服务端采用自己的默认输出限制；主模型的
`MAX_TOKENS` 不变。Anthropic SDK 的 `messages.create` 强制要求该参数，因此省略输出 token 上限的
摘要请求使用同一 SDK 的 `post` 发送 `/v1/messages`；不接受省略参数的服务端会报错并保留旧摘要。
完整输出超长时附上原始材料和草稿，要求模型重新压缩一次；重试请求同样检查输入预算。
若仍超长、为空、未完整结束、超时或异常，保留旧摘要、水位及完整历史，不机械截断。
只有有效且有压缩收益的摘要才与水位一起提交；后续请求复用该摘要。

`load_tool_output` 和 `load_context_history` 都支持有界分页，整个响应最多 16k 字符，
并限制到本执行者目录。子 Agent 独立管理归档、压缩回调与内置待办状态。
`compact()` 可以主动申请压缩，但同样遵守保护窗口、冷却和收益检查。

无压力时只测量一次完整请求；请求内容变化才重新测量。使用量、失败原因和历史视图变化
继续记录，上一份视图的哈希复用已计算结果。完整设计与验证见
[上下文适配报告](docs/context-management-porting.md)。

可配置项：

```bash
CONTEXT_COMPACT_MODE=model   # off | model
SUMMARIZATION_MODEL_ID=your-summary-model
SUMMARIZATION_API_KEY=your-summary-api-key  # 留空时与主模型共用 API Key
CONTEXT_TOOL_RESULT_BUDGET_CHARS=200000
CONTEXT_SINGLE_TOOL_OUTPUT_MAX_CHARS=80000
CONTEXT_COMPACT_THRESHOLD_CHARS=300000
CONTEXT_SUMMARY_MAX_CHARS=4000              # 字符数，不是 token 数；超长只重压缩一次
CONTEXT_TRANSCRIPT_DIR=.transcripts              # 旧目录导入位置
CONTEXT_TOOL_OUTPUT_DIR=.task_outputs/tool-results  # 旧目录导入位置
CONTEXT_REACTIVE_RETRIES=1
CONTEXT_RECENCY_MESSAGES=12
CONTEXT_RECENCY_ROUNDS=2
CONTEXT_MAX_FOLD_ROUNDS=12
CONTEXT_MAX_REQUEST_CHARS=600000
CONTEXT_SUMMARY_INPUT_MAX_CHARS=120000
CONTEXT_WINDOW_TOKENS=0                     # 未知窗口；不猜厂商值
CONTEXT_MODEL_WINDOWS_JSON={}                # 按实际模型名覆盖窗口，包含 fallback
CONTEXT_NEAR_CONTEXT_RATIO=0.8
CONTEXT_SUMMARY_WINDOW_TOKENS=0
CONTEXT_SUMMARY_TIMEOUT_SECONDS=45            # SDK I/O timeout，非严格总期限
CONTEXT_FAILURE_COOLDOWN_SECONDS=90
CONTEXT_SUMMARY_TEXT_PREVIEW_CHARS=4000
CONTEXT_SUMMARY_ARGUMENT_PREVIEW_CHARS=2000
CONTEXT_TOOL_PROJECTION_ENABLED=true       # 有压力时启用，独立于语义摘要开关
CONTEXT_TOOL_CLEAR_MIN_CHARS=8000
CONTEXT_WRITE_CLEAR_MIN_CHARS=8000
CONTEXT_WRITE_KEEP_ROUNDS=2
```

权限策略参考 `s03_permission` 的三道闸门：

- 硬拒绝：`sudo`、`rm -rf /`、`shutdown` 等直接拒绝
- 需确认：`rm `、写入 `/etc/`、`chmod 777`、写工作区外文件
- 默认允许：普通读文件、搜索、工作区内写入和非危险命令

## 接入外部 MCP 工具

项目现在是一个轻量 MCP Host：现有 `bash`、文件读写等工具保持不变，外部 MCP
Server 提供的工具会额外注册到 Agent。首版只接入 MCP Tools，不处理 Resources、
Prompts 和 Sampling，保持边界简单。

1. 复制示例配置：

```powershell
Copy-Item mcp.json.example mcp.json
```

2. 把 `command` 和 `args` 改成你的 MCP Server 启动命令。配置格式与 Claude Code
常用的 `mcpServers` 格式一致：

```json
{
  "mcpServers": {
    "my-plugin": {
      "command": "python",
      "args": ["path/to/mcp_server.py"],
      "env": {
        "PLUGIN_API_KEY": "${PLUGIN_API_KEY}"
      }
    }
  }
}
```

远程 Streamable HTTP 服务也可以直接配置：

```json
{
  "mcpServers": {
    "remote-plugin": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {
        "Authorization": "Bearer ${PLUGIN_TOKEN}"
      }
    }
  }
}
```

3. 正常启动 CLI 或 Web。外部工具名称会显示为
`mcp__服务名__工具名`，例如 `mcp__github__search_repositories`。每次执行外部 MCP
工具都会走现有的用户审批流程；没有 `mcp.json` 时 MCP 自动关闭，不影响任何内置功能。

Web 工作台标题栏提供插头形状的“MCP 插件配置”按钮，可以直接添加本地命令或远程
HTTP Server、套用常用模板并删除已有配置。保存或删除后会自动刷新对应工作区的 MCP
缓存，下一条消息直接生效；如果当时有任务正在运行，页面会提示重启 CodeAgent。
对话 checkpoint 会持久化当前 `tool_schema_hash`。恢复旧对话时如果发现工具定义已经
变化，系统提示会明确要求模型以本轮注册工具为准，忽略历史消息中过时的工具可用性判断。

如需把配置放到其他位置，可在 `.env` 中设置：

```bash
MCP_CONFIG=config/mcp.json
```

## Agent Team（第一阶段）

当前 Web 已隐藏 `Agent Team` 开关，聊天提交固定使用普通 Agent 模式，不根据用户文字
自动组队。本机关闭 Team 时使用以下配置，修改后需要重启后端：

```bash
TEAM_RUNTIME_ENABLED=false
TEAM_WRITE_ENABLED=false
```

关闭后不启动 Team Supervisor，并拒绝显式 Team 请求及旧活跃 Team 会话的继续执行。
已有 Team 数据和 Worktree 不会删除或自动取消；请在新会话使用普通 Agent。同一项目
仍有未结束 Team 时，Memory 的只读保护继续保留。以下为保留的 Team 实现说明。

显式 Team 请求首先进入只读 `team_planner`：Root 创建或复用普通 Task DAG，并必须调用
`TeamPlanSubmit`。如果模型只输出文字方案却没有提交工具调用，Runtime 会把本次 Run
标为失败，不会伪装成已创建 Team。Team Plan 获得用户批准后，Runtime 才创建 Teammate、
Attempt 和代码 Worktree。

执行期间 Root 就是 Lead：Lead 负责 Attempt Plan 和 Candidate 的语义审查；Runtime 负责
原子认领、权限、写入范围、验证和候选提交。低/中风险审查无需用户代替 Lead 点击；
高风险 Candidate 仍需用户确认。第一阶段不会自动 merge、cherry-pick、rebase、push，
也不会修改用户源工作区或主分支。候选提交必须由用户人工集成，之后才能发起只读联合验证。

Team 心跳由 Runtime 观察实际执行阶段，不要求模型定期调用工具报平安。等待模型、接收
文本/思考/工具参数、网络重试等待、工具执行和权限审批分别显示；模型的 keepalive/ping
不算有效内容。单次逻辑模型调用默认连续 300 秒没有有效内容，或累计达到 600 秒时暂停。
累计时间包含本次调用的网络重试和输出截断后的重新生成，不会通过重试重置计时。

```bash
TEAM_MODEL_RESPONSE_TIMEOUT=300
TEAM_MODEL_CALL_TIMEOUT=600
```

模型超时不会直接把 Teammate 判为失联：Runtime 撤销写权限，等待 worker 真正退出，保留
Task、Attempt、Worktree 和最后安全上下文，进入“需要人工恢复”。确认并通过原有现场
校验后可以继续；迟到的模型回复不会执行工具，未知写操作不会自动重放。Lead 模型超时
同样停止本次调用，用户可发送新的团队指令继续。旧版本已经产生的 `orphaned` 记录不会
因此自动恢复。工具仍使用自己的执行超时，等待审批仍使用原审批超时；普通单 Agent
和同步 Subagent 不启用这套 Team 模型期限。

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
