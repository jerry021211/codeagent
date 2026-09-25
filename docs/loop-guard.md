# 单 Agent 防循环与执行预算

普通编码 Agent、Discuss 和普通子 Agent 默认启用此保护。Team planner、lead、
teammate 不在本次范围内。实现复用 Agent 的 Hook 调度和现有执行路径；不会新建
监督模型、事件总线或数据库。

## 判断与处理

检测分别比较动作、相关输入状态、结果签名。相同工具名称或 shell 命令重复，
本身不会触发硬停止。JSON 参数稳定排序；shell 引号、大小写和参数顺序保留。
Shell 结果在截断前计算签名，仅过滤 pytest/unittest 标准汇总行的耗时，保留失败
用例、错误类型和断言数值；其他输出差异仍可能使重复诊断无法归并。

- 最近 12 次已完成调用构成默认观察窗口。相同动作、已确认相同输入、相同确定性
  失败达到 3 次后提示；模型实际收到提示后再次原样请求，`PreToolUse` 阻断该具体
  操作，保留对应 `tool_use_id` 的错误结果及修正建议。不是禁用整个工具。
- 相同无效参数调用达到 2 次时强化提示，之后原样调用被阻断。缺少替换原文、
  原文与新文相同等明确的编辑参数错误可直接判断；修正参数后仍可执行。
- 重复提交被阻断操作有单独的恢复次数上限，默认 3 次，避免形成请求与拒绝循环。
- 连续 2 次没有可见正文也没有工具调用的响应有界结束。仅推理内容不算有效正文；
  有效正文或真实工具调用重置计数。
- 重复成功读取、不同区间或分页、修改后回读、`edit → test → edit → test`、
  单纯 `A → B → A → B` 都不单独触发硬停止。
- 相关输入变化允许重新验证，但不等于修复成功，也不会清除其他问题的阻断记录。
  输入状态未知、依赖环境或外部服务的变化不可判断时，只提醒，不按确定性失败阻断。

**普通 shell 的输入依赖默认未知。** 仅凭相同命令、退出码、Git HEAD 或工作区修改
版本，无法确认测试依赖没有变化。没有可信输入状态提供者时，重复 shell 诊断只提醒，
最终由调用次数和时间预算兜底；不会承诺识别所有测试的依赖图。测试断言失败和编译
错误是有效诊断，不是 shell 工具故障，不会自动重试到通过。

已有权限或 Discuss Hook 拒绝的请求不计为真实执行失败。被拦截的请求仍写入合法
工具结果，不静默丢弃；反馈不是新的工具执行，不递归计数。执行层同步等待控制
Hook，工具只有通过检查后才启动。

## 配置

CLI 和 Web 通过 `EnvironmentConfig.loop_guard_config` 读取以下环境变量，
`to_agent_config()` 将同一配置传给 `AgentConfig.loop_guard`。SDK 可直接传入
`codeagent.hooks.loop_guard.LoopGuardConfig`。

| 环境变量 | 字段 | 默认值 |
| --- | --- | ---: |
| `CODEAGENT_LOOP_WINDOW` | `window_size` | 12 |
| `CODEAGENT_LOOP_REPEAT_FAILURE_LIMIT` | `repeat_failure_limit` | 3 |
| `CODEAGENT_LOOP_PARAMETER_ERROR_LIMIT` | `parameter_error_limit` | 2 |
| `CODEAGENT_LOOP_BLOCKED_ATTEMPT_LIMIT` | `blocked_attempt_limit` | 3 |
| `CODEAGENT_LOOP_EMPTY_RESPONSE_LIMIT` | `empty_response_limit` | 2 |
| `CODEAGENT_LOOP_TOOL_MAX_RETRIES` | `tool_max_retries` | 2 |
| `CODEAGENT_LOOP_RETRY_DELAY_SECONDS` | `retry_delay_seconds` | 0.25 |
| `CODEAGENT_RUN_MAX_MODEL_CALLS` | `max_model_calls` | 80 |
| `CODEAGENT_RUN_MAX_TOOL_CALLS` | `max_tool_calls` | 200 |
| `CODEAGENT_RUN_MAX_TOTAL_TOKENS` | `max_total_tokens` | 300000 |
| `CODEAGENT_RUN_MAX_ACTIVE_SECONDS` | `max_active_seconds` | 1800 |

次数与执行时间为正值；`tool_max_retries` 和 `retry_delay_seconds` 可为 0，后者适合
测试或离线运行时关闭退避等待；`max_total_tokens=0` 仅关闭 Token
上限，其余兜底继续生效。这些是初始调优参数：12 条限制局部检测成本，3 次失败与
2 次参数错误给模型纠正机会；80 次模型请求、200 次工具请求和 30 分钟限制单次
本地开发任务的消耗。它们不是经过本项目任务集验证的最佳值。

现有 `MAX_ITERATIONS=50` 仍有效。轮数与模型请求次数口径不同：基础设施重试、
续写等请求也需要预算，不能把 50 轮理解为最多 50 次请求。

Token 口径是 provider 报告的普通输入、缓存创建输入、缓存读取输入与输出之和，
不是仅新增 Token，也不是费用。缺少 usage 的调用保持未知，不按真实消耗为零解释；
已知部分仍累计，模型请求次数和时间限制仍有效。Token 检查在响应返回后生效，
可能超出最多一个在途响应的消耗；不能作为预付费硬额度。

工具仅在结果明确标记为临时故障且安全重放时有限重试，默认额外 2 次、共 3 次，
使用有界退避并计入预算。没有通用 shell 自动重试。模型请求沿用 recovery 层，
不在 Agent 外层重复套用相同重试。超时后仍运行的任务进程不得原样再次启动；
进程查询和必要清理不因普通重复失败记录而被拦截。时间限制在执行边界检查，
在途操作还受现有请求或命令超时、取消能力约束。
进程树清理结果不确定时，在同一执行中保留该命令的阻断记录；目前没有自动核验
并解除该记录的接口。常驻服务管理、完整后台进程恢复不在这一版中。

默认模型响应静默超时为 300 秒、单次模型调用上限为 600 秒，复用
`ExecutionActivity`；SDK 可显式传入其他活动配置。Shell 默认单次超时 120 秒，
请求值受工具的 600 秒上限约束，任务剩余时间更少时还会提前停止。

## 状态、压缩与恢复

执行状态与模型消息、压缩摘要分离。运行内持续维护有界观察记录、提醒、阻断和
共享预算；参数与结果通过摘要标识，不额外复制完整源码或大段输出。上下文压缩
不会清空计数。父 Agent 与普通子 Agent 共享预算，局部重复检测各自隔离。

`agent.export_execution_state()` 导出 version 1 的执行作用域、局部状态和预算，
`agent.restore_execution_state(payload)` 恢复。Web 在现有 checkpoint 的
`metadata['execution_guard']` 中保存这一数据；旧 checkpoint 缺少该字段时保持兼容。
Web 默认使用 `event_emitter.context.run_id` 标识执行作用域，同一个 Run 恢复会
保留保护状态，新的 Run 不继承旧执行的封禁。该接入不新增自动恢复或重放机制。

SDK 在同一逻辑执行中传同一个 `execution_id`，或用 `agent.run(None)` 继续已有
执行。提供新 prompt 且没有显式 ID、也没有 Web run ID 时，创建新的执行作用域。
不同任务不共享封禁记录；暂停恢复时应连同原有消息和 context 一起恢复执行状态。

## 停止与界面结果

循环停止使用 `loop_detected:*`，预算停止使用 `budget_exceeded:*`。停止保留已有
文件修改，不回滚，也不标记成功。程序根据已记录事实生成摘要，报告验证结果、
停止原因与未完成内容，**收尾额外调用模型 0 次**。记录不完整时不能推断已验证成功。
用户取消沿用取消语义，不额外发起模型收尾。

CLI 即使开启流式输出，也打印最终程序化失败摘要；单次执行失败返回退出码 1，
交互模式允许继续提交新任务。普通 Web Run 保存失败消息、`run.failed` 事件和
checkpoint；兼容现有 `recovery_failed`、`max_iterations`、`runtime_contract` 失败原因。

验收测试使用假模型、假工具及真实 Agent/Hook/Web 调度路径，无需真实模型 API。
项目检查命令：`python -m unittest discover -s tests`。
