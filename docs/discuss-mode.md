# Discuss 模式：仓库研究与本项目实现

## 结论

`zwrong/pi-discuss-mode` 是 Pi Coding Agent 的 TypeScript 扩展，核心确实依赖
事件 hook。它把「告诉模型只读讨论」和「在工具执行前阻断副作用」分成两层。
不要求生成计划，也没有计划提交、审批或自动转入执行的工作流。

本次研究版本：`main`，提交 `dde6e5d36d3e5663b12511f7c6495e6e013cbcf9`。
主要依据是实际克隆的源码，而非仅根据 README 判断。

## 原仓库怎么实现

推荐阅读顺序：

1. [index.ts](https://github.com/zwrong/pi-discuss-mode/blob/dde6e5d36d3e5663b12511f7c6495e6e013cbcf9/index.ts)：扩展入口、切换逻辑、三个事件订阅。
2. [utils.ts](https://github.com/zwrong/pi-discuss-mode/blob/dde6e5d36d3e5663b12511f7c6495e6e013cbcf9/utils.ts)：命令允许/禁止正则及 `isSafeCommand()`。
3. [test/utils.test.ts](https://github.com/zwrong/pi-discuss-mode/blob/dde6e5d36d3e5663b12511f7c6495e6e013cbcf9/test/utils.test.ts)：16 个命令判断测试。

### 1. 扩展入口和状态

默认导出的 `discussModeExtension(pi)` 在闭包内保存三个布尔值：

- `discussMode`：当前实际模式。
- `pendingEnterMessage`：下次模型请求前要添加进入提示。
- `pendingExitMessage`：下次模型请求前要添加退出提示。

`registerCommand("discuss")`、`registerShortcut(Key.ctrlAlt("d"))` 都调用
`toggleDiscussMode(ctx)`；`registerFlag("discuss")` 接受启动参数 `--discuss`。
切换同时更新状态栏和通知，并清除反方向的 pending 标记。连续切换后只保留最终状态。

### 2. before_agent_start：软约束

`pi.on("before_agent_start", ...)` 检查 pending 标记，在下一个 agent request 返回
一个 `display: false` 的自定义上下文消息，内容说明进入或退出讨论模式。
标记随后清零，因此不会在每次请求重复注入。

这里没有替换整个 system prompt，而是在对话尾部加消息。这样做的意图是尽量
保留既有提示前缀缓存；退出提示用于纠正模型仍认为自己只读的状态。
这层只是模型指引，不能单独阻止模型发起写工具调用。

### 3. tool_call：实际拦截

`pi.on("tool_call", ...)` 在每次工具执行前检查：

- 普通模式立即返回，不改变执行行为。
- 工具名为 `edit` 或 `write`：返回 `{block: true, reason: ...}`。
- 工具名为 `bash`：取 `event.input.command`，调用 `isSafeCommand()`；不通过则阻断。
- 其他工具：此扩展没有进一步检查。

所以它是「扩展 hook + 命令策略」，没有给文件写工具改实现，也没有修改 Pi 的模型
推理循环，更没有建立容器或操作系统级只读文件系统。

### 4. session_start：启动参数

`session_start` 读取 `pi.getFlag("discuss")`，为 true 时设置只读状态和 pending
进入消息。虽然源码注释提到恢复状态，但该提交没有会话存储读写逻辑；不能据此认为
用户手动切换的模式会跨进程持久化。

### 5. 命令判断的局限

`isSafeCommand()` 的规则是：整条字符串没有命中任何禁止正则，且至少命中一个
允许正则。它没有真正解析 shell 或子命令参数。以下是源码规则直接推导出的缺口：

| 示例 | 为什么可能被原实现放行 |
| --- | --- |
| `git branch topic` | 允许 `git branch` 前缀，禁止规则只覆盖删除分支 |
| `git remote add origin URL` | 允许 `git remote`，没有限制它的写入子命令 |
| `curl -o output URL` | 允许 `curl` 前缀，没有限制输出文件参数 |
| `curl -X POST URL` | 未限制 HTTP 方法，因此不等价于只读请求 |
| `rg --pre=program pattern .` | 允许 `rg`，未限制执行预处理程序的选项 |
| `echo ok; python script.py` | 安全匹配只看开头，后续命令不必再次通过允许列表 |

相反，`echo rm` 会因参数中的 `rm` 命中禁止规则而误拦。
原仓库的 16 个测试主要验证常见命令和空白输入，没有覆盖上述绕过路径，也没有
覆盖扩展切换、hook 排序、第三方工具或状态持久化。其代码值得学习的是分层和接入点，
不能把命令正则视为完整安全边界。

## 本项目怎么接入

### 调用链

```text
CLI --discuss / /discuss 或 Web 请求 mode=discuss
  → Agent.prompt_mode = PromptMode.DISCUSS
  → PromptRuntime 选择 discuss.md，跳过执行、任务规划和写记忆模板
  → 模型输出 tool_use
  → PreToolUse 最前面的 discuss guard
      → 拒绝：tool.blocked + tool_result，模型可继续解释
      → 允许：原权限 hook → 原工具执行 → 原 PostToolUse
  → 回合结束：跳过自动记忆维护
```

核心文件：

- `codeagent/permissions/discuss.py`：工具允许列表和严格命令检查。
- `codeagent/hooks/manager.py`：支持前置 hook，以及复制注册表以隔离每个 Agent。
- `codeagent/agent.py`：安装 guard、提供模式切换、讨论时跳过模型调用前的 reminder
  hooks 和回合结束的自动记忆维护。
- `codeagent/prompts/templates/discuss.md`：讨论指引。
- `codeagent/prompts/runtime.py`：排除讨论时不适用的模板。
- `codeagent/cli.py`：启动参数、交互命令和状态提示符。
- `codeagent/web/scheduler.py`：将模式固化到队列 job、Run metadata 和用户消息 metadata。
- `codeagent/web/factory.py`：创建对应 profile 的 Agent，并为讨论 Run 设置只读 memory store。
- `web/src/App.tsx`、`web/src/components/ChatWorkspace.tsx`：按钮、斜杠命令及会话模式显示。

### 与原仓库有意不同的选择

1. **复用现有 PromptRuntime。** 当前模式每次组装时都有效，不依赖一条旧提示是否仍
   留在历史里。上下文压缩、Web 新建 Agent、checkpoint 恢复后都由本次模式决定权限。
   模式切换会改变 system prompt，因此可能损失一次前缀缓存；同模式的工具循环保持稳定。
2. **保留工具 schema，由 hook 执行约束。** 避免切换时重建工具注册表，也确保可以测试
   模型无视提示时是否仍无法写入。退出讨论后原有能力恢复，不额外授予权限。
3. **未知工具默认拒绝。** 除文件写工具外，也阻止任务更新、TODO、remember、subagent、
   Team 和 MCP，避免通过这些间接产生写入。保留读取、搜索、加载与上下文管理。
4. **Shell 采用保守语法子集。** 每次只接受一条字面量命令，限制命令名和选项，拒绝
   管道、控制符、重定向、表达式、动态变量、脚本和网络访问。Git diff/show/log 要求
   显式关闭外部 diff 和 textconv。复杂读取应优先使用原生工具。
5. **Web 按请求固定模式。** 后端没有全局开关；不同会话互不污染，排队时不受后续
   选择影响。界面刷新会按最近一条用户消息恢复已提交的模式。尚未发送的切换仅存在于
   页面内存；API 未传 mode 始终使用 normal，SDK 重建时需明确传入所需模式。
6. **避免 Team 间接写入。** Discuss 与显式 Team 请求冲突，活跃 Team 会话也不能
   进入讨论；调度执行前再检查一次，防止排队期间新增 Team 后转入 Lead 执行。

### 边界

讨论模式限制的是 Agent 的工具执行，不承诺整台机器无写入。UI 的人工操作、已有后台
程序、MCP 服务启动，以及运行时保存会话、事件、checkpoint、工具输出仍按原机制运行。
只读命令仍信任本机二进制、shell 环境和 Git 配置；例如 Git 的 fsmonitor 等配置会影响
命令行为。需要对不可信本机程序提供强隔离时，还需另加真正的只读沙箱。

自定义部署模板可以改变模型指引，但不能取消 Agent 安装的工具 guard。新增只读工具
必须明确加入允许列表；不要根据工具名中出现 read/get 或 MCP 自报描述自动放行。

## 验证入口

`tests/test_discuss.py` 覆盖真实文件写入拦截与恢复、读取、模型强行请求写入、审批
顺序、共享 hooks 隔离、自动记忆维护、提示模板，以及多种命令绕过。
`tests/test_web_scheduler.py` 验证模式传递和持久化；`tests/test_web_api.py`
验证 API 参数、非法模式和 Team 冲突。前端使用 TypeScript 检查及 Vite 构建验证。

```powershell
python -m unittest discover -s tests
Set-Location web
npm.cmd run build
```
