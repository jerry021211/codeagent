# Team 协作：简单流程，严格执行边界

这次改动不增加第二套任务系统，不增加通用契约管理或运行中多版本重规划。
Root 仍然是 Lead；模型决定如何拆分、阅读、实现和沟通，Runtime 管认领、权限、持久化与恢复。

## 1. 任务写清楚，但不规定每一步怎么做

- Lead 只探查到足以明确目标、约束和验收；成员自己决定具体实现步骤。
- 人数不是目标，一个 Teammate 也有效。紧密关联的代码、测试、使用说明可放在同一 Task。
- 已有充分设计时不再加一轮重复分析；analysis 应解决一个具体未知问题。
- 每条依赖要有明确交接产物；不自动复制或集成上游 Worktree 的文件。
- `analysis`：只读分析，最终提交文字报告；`write_scopes` 必须为空。
- `code`：任何仓库文件修改，包括 `docs/DESIGN.md`、README 和配置，不只指源代码。
- `plan_required` 必须是真正的 JSON boolean，不能使用字符串、数字或 null。
  analysis 只能设为 false 或省略。Attempt Plan 是 code 的写入前方案，不是分析报告审批。
  高风险只读分析仍只交报告；高风险 code 无论该字段是否为 false，都必须先审批 Attempt Plan。
- Team 规划期间 TaskCreate/TaskUpdate 会先校验完整 metadata，错误直接作为工具结果交给模型纠正，
  不先写入无效 Task。普通 Task 的 metadata 仍可自由使用。
- 认领事务按 Task 配置计算审批要求，调用方不能强开或绕过审批；新 Team Plan 保存
  `plan_required`，提交、批准和认领时核对。旧错误记录不会被自动修改或重新批准。
- 普通单 Agent 的 Task 不受这项 Team 专属校验影响。
- Team Plan 可用普通 `shared_context` 文本写一次公共约定（最多 8000 字符），不必复制到每个 Task。
- 每个 Teammate 的 `TASK_ASSIGNED` 包含自己的完整目标、执行要求、公共约定，以及直接前置分析任务的结果和消息 ID。
- 数据库仍保存完整 JSON 消息；新投递给模型的 TASK_ASSIGNED 显示为中文分节任务单，
  只展示任务、执行边界、公共约定、非空验收/验证要求、前置报告和产物引用。
  消息 ID、Attempt ID/序号、内部版本号和空字段不再占用任务说明。
  目标与公共约定保留原文，不使用额外模型摘要或长度截断；相同整段文本只展示一次。
  Lead 应只写本任务职责，公共约定自动附带，不在 description 中重复整份需求。
  其他消息（特别是 QUESTION/ANSWER 和恢复消息）的关联 ID、ACK 及 Checkpoint 流程不变；
  已保存的旧上下文不重写。配置冲突明确显示，不能通过精简提示词隐藏或自动解锁。
- 不附带 Root 的完整聊天、不附带其他成员的完整上下文。`TaskList` 保持摘要，模型可以按需查询。
- 提交、批准、认领都会核对任务类型与实际 Task 配置；错误不自动改成另一种任务。

依赖 DAG 只说明先后顺序，不等于代码已经集成。分析报告可以随分配消息传给后续任务；
文件修改仍在独立 Worktree 中，不会自动出现在别人的目录里。
第一阶段不能承诺“全部从同一个 base 创建，又自动拥有前置任务代码”。
紧密依赖的文件修改优先放在同一个 code Task；候选代码仍由用户人工集成。

## 2. 提问、等待、回答

1. Teammate 调用 `team_ask_lead`；QUESTION 记录 Task、Attempt、发送方 Session ID 和 generation。
2. `blocking=true` 时，成员在安全停顿点保存上下文，Session 的等待原因包含具体 QUESTION ID。
3. Lead 调用 `team_answer_question`。数据库事务检查问题归属、当前 Attempt/Session、批准方案及持久化绑定/租约。
4. ANSWER 通过 `correlation_id` 指回 QUESTION，`dedupe_key=answer:<question_id>`。
5. 只唤醒正在等待这条问题的成员，不新建 Task、Attempt 或 Worktree，也不打开原来关闭的写权限。
6. 如果回答比等待存档先到，存档事务会检查已落库的回答，避免错过唤醒。
7. Supervisor 再次启动同一个 Attempt 的 worker，从原 Checkpoint 恢复上下文并投递未确认的 ANSWER。

重复提交相同回答只读取原记录，不二次唤醒；相同问题的不同回答会报冲突。
旧 generation、结束/取消的 Attempt、未知写结果或安全冻结不能通过回答恢复。
真正执行工具时，仍由现有 Worktree 和工具权限闸门检查文件系统现场。

如果回答表明必须改变批准范围：Attempt/Session 保持等待，写权限关闭，记录
`team_plan_change_required`，现场与租约不清理。用户应进一步决定如何调整；
这次没有实现热切换新计划，也不会自动取消或重建整个 Team。
“检查并恢复”不能替代修改范围所需的方案审批。

## 3. 交付与 Memory

- 普通分析调用 `team_submit_analysis_result` 完成并通知 Lead，不额外套 Candidate 审批。
- 分析报告不代表文件已落盘，也不能把等待/未知状态伪装成完成。
- 一次协议纠正必须匹配任务类型、Attempt 状态和写权限；矛盾的历史 Attempt 不会被要求
  调用另一个角色的提交工具，也不会通过纠正消息获得权限。
- 文件修改提交 Candidate，Lead 语义审查后 Runtime 独立验证并生成候选提交。
- 不自动 merge、cherry-pick、rebase，不改源工作区，不自动清理现场。
- Team 期间 Memory 仍只读；没有 `remember` 的角色提示词不再要求使用它。

## 4. 在前端看什么

- 批准 Team Plan 前：展开各项任务，查看类型、完整目标、写入范围、风险、验证命令及依赖；公共约定也可展开。
- “协调与阻塞”：显示成员具体问了什么，是等 Lead 回答，还是需用户决定方案调整。
- “需要人工恢复”：仍专门展示执行异常，不把普通提问显示为安全冻结。
- 执行观察面板：查 `team_messages` 的 QUESTION/ANSWER，按 `correlation_id` 对应；
  查 `agent_sessions.waiting_reason`、`state` 和 `agent_session_checkpoints`，确认上下文与等待/恢复顺序。

## 5. 验证与升级注意

```powershell
python -m unittest discover -s tests -p 'test_team*.py' -v
python -m unittest discover -s tests -p test_prompts.py -v
python -m unittest discover -s tests -p test_runtime_activity.py -v
python -m unittest discover -s tests -v
cd web
npm run typecheck
npm run build
```

测试使用临时数据库、临时仓库和假模型，不运行用户的现存 Team。
生产数据库不迁移、不清空；旧矛盾计划不会自动修正或重新审批。
旧 QUESTION 若没有 Session 身份信息，会拒绝自动恢复，不能猜测它属于哪个新 worker。
重启新版服务、刷新前端后建议用新的测试会话验证。不要在真实写操作尚未确认完成时强行重启或恢复。

实现位置：`teams/tasks.py`（任务校验）、`teams/lead.py`（方案入口）、
`teams/tools.py`（提问/回答工具）、`web/storage.py`（事务、分配、Checkpoint）、
`prompts/runtime.py` 与 Team 模板、`web/src/components/TeamPanel.tsx`。
现有 `runtime/activity.py`、Supervisor 的模型超时与心跳区分继续复用，不重新实现。

2026-09-09 规划与任务校验改动验证：全量 310 项测试通过，较改动前新增 15 项。
新增用例覆盖错误配置不落库、同一 Agent 循环收到错误后纠正且工具列表不变、
认领事务不能绕过高风险代码审批、方案审批字段一致性以及协议纠正不串用角色工具。
测试使用假模型，不能据此断言真实模型的任务拆分质量已经提升；未重跑用户历史任务。
本轮未改前端，未重新执行前端构建或生产页面操作。

2026-09-10 任务单展示改动验证：全量 317 项测试通过，新增 7 项测试。
覆盖模型收到分节文本、数据库原消息不变、精确约束与报告尾部保留、代码范围和审批提示、
旧配置冲突可见、非分配消息关联信息不变，以及旧 Checkpoint 不被重写。
只改变新投递任务说明的格式与 Lead 的任务描述指导，不引入摘要模型或修改已有方案。
