**CodeAgent 最小评测基线实施方案**

本文根据 2026-09-18 当前工作区代码制定。第一步 C01 已落地：`python -m evals validate/run/verify`、离线接线测试、独立验收及一轮真实模型试跑已完成。运行方式见 [evals README](D:/new_codeAgent/evals/README.md)，存证见 [证据索引](D:/new_codeAgent/eval-results/README.md)。其余任务、report 子命令、记忆/压缩观测扩展仍是设计方案，尚未开展批量模型评测。

建议第一版交付：三个独立评测集、十二个场景、每场景三次试验，以及一份可以定位到原始轨迹的报告。先完成三个冒烟场景，再扩到十二个；十二个场景用于工程基线，不能代表通用编程能力。

**1. 先明确评测对象和结论边界**

| 评测集 | 对象 | 输入 | 主判定 |
|---|---|---|---|
| coding | 模型与 CodeAgent 执行框架共同完成代码任务 | 固定代码副本＋需求 | 独立验收测试通过且已有相关功能不回退 |
| memory | 当前自动记忆选择策略 | 固定记忆库＋查询＋相关记忆标注 | 候选召回、最终选择、实际注入是否符合标注 |
| context | 压缩后继续执行任务的能力 | 固定合法历史＋工作区状态＋续做需求 | 压缩确实发生，后续产物满足任务与保留约束 |

三个评测集分表报告，不把选择一条记忆和修复一段代码混成一个成功率。已有单元测试继续验证产品确定性行为；此前的 SQLite、前端 reducer 微基准单独保留为性能评测，不加入任务成功率。

**2. 直接复用哪些代码**

- [Agent.run](D:/new_codeAgent/codeagent/agent.py:170)：真实执行入口，返回 iterations、stop_reason 和本回合 usage。
- [WebAgentFactory.create](D:/new_codeAgent/codeagent/web/factory.py:83)：复用 Web 的 Task 规划、hooks、权限、context、memory 和工具装配；评测可以直接调用，不必启动 HTTP 或浏览器。
- [EnvironmentConfig](D:/new_codeAgent/codeagent/config.py:59)：固定模型、摘要模型、迭代上限、压缩阈值等配置；输出配置时排除密钥。
- [EventEmitter / RecordingEventSink](D:/new_codeAgent/codeagent/events/sink.py:36)：记录模型、工具和压缩事件。
- [UsageTracker](D:/new_codeAgent/codeagent/events/models.py:154)：累计主模型、记忆选择、摘要和子调用的已报告 usage。
- [SQLiteRepository](D:/new_codeAgent/codeagent/web/storage.py:186)：为每个 trial 提供独立 Task/Run/事件数据库。
- [CancellationToken](D:/new_codeAgent/codeagent/runtime/cancellation.py:27)：协作式取消。它不是任意阻塞操作的硬超时，runner 还需进程级截止时间和清理。

建议评测适配器用 WebAgentFactory 创建 Agent，然后直接运行 Agent。这样可以保持 Web 的 Task 规划行为。若选择更简单的 SDK/Todo 装配，报告必须标记为另一个 profile，不能与 Web 结果直接混合。

首次创建 trial 时先创建 Conversation、Run，给 EventEmitter 完整的 ExecutionContext，之后调用 factory。runner 在结束、异常与超时路径保存产物及明确状态；直接调用 factory 不会自动执行 RunScheduler 的终态保存逻辑。

**3. 首批十二个场景**

代码修复任务是在固定副本中人为引入缺陷，并不是宣称当前代码存在下列 bug。每个 seed 都应有机器可验证的失效行为；不向 Agent 展示 seed patch 或参考修复。

| ID | 场景 | 准备方式 | 验收要点 |
|---|---|---|---|
| C01 | 修复文件读取起始行偏移 | 对读取工具副本引入 offset 偏移 | offset=1、3、末尾、越界；行号和内容正确 |
| C02 | 恢复精确编辑的唯一匹配要求 | 在编辑工具副本去掉多处匹配保护 | 0/1/多处匹配；拒绝时文件未变化 |
| C03 | 修复重复事件入库 | 在存储副本破坏 event.id 幂等分支 | 重复 ID 不增行、不推进序号；不同 ID 正常入库 |
| C04 | 恢复工具失败状态传播 | 在工具结果副本丢弃失败状态或 exit_code | 非零退出码判失败；普通输出中的 Error 文本不误判 |
| C05 | 修复前端重连后的重复累加 | 在 runStore 副本破坏去重 | 重复 id/seq 不重复增加文本和 usage；混合新事件正确 |
| C06 | 修复主答案混入旁路模型输出 | 在 runAnswer 副本破坏调用身份过滤 | memory_select、子 Agent 文本不进入主答案，主答案正常展示 |
| M01 | 少量记忆下找到相关约定 | 10 条记忆，1～2 条相关 | 相关文件被选择且实际注入 |
| M02 | 排序末尾的相关记忆 | 100 条记忆，相关项位于排序后半段 | 记录候选缺失与最终遗漏，区分两种原因 |
| M03 | 没有相关记忆 | 同样规模的记忆库，无关查询 | 不注入无关记忆；单独报误注入率 |
| X01 | 压缩后保留用户修正 | 固定历史先约定 CSV，后改为 JSON | 确实发生压缩，产物为 JSON，旧要求未被恢复 |
| X02 | 压缩后不重复已完成步骤 | 历史与磁盘一起预置第一阶段已完成状态 | 完成下一阶段，已完成文件/一次性标记不被重复修改 |
| X03 | 压缩后保留测试失败事实 | 历史明确记录非零退出码及待修复问题 | 最终完成对应修复并通过外部验收；失败历史未被当作通过 |

第一轮只做 C01、M02、X01，每个一次，检查端到端链路。然后十二个场景各跑三次，共 36 个 trial。一个 trial 可能包含多次模型请求，36 不是 API 请求数。编码、记忆和上下文分开报告，绝不把不同类型的分数简单平均。

后续扩充到 30～60 个场景并划分开发集、保留集。上面多数任务属于自建回归/故障注入题，应标明来源；简历若要主张跨项目能力，需要补充其他项目或真实需求任务。

**4. 每个 coding task 的最小定义**

下面是建议的 JSON 合同，字段和对应 assets 都需要后续实现。grader_id、seed_id、gold_id 由评测控制器解析；不会把参考补丁或隐藏验收代码放入 Agent 工作区。

```json
{
  "id": "C01-read-offset",
  "suite": "coding",
  "fixture_id": "codeagent-tools-v1",
  "seed_id": "read-offset-off-by-one-v1",
  "gold_id": "read-offset-reference-fix-v1",
  "prompt": "read_file 在指定 offset 时返回的内容与行号不对应。请修复，使 offset 按 1 开始计数，并保持 limit 与越界行为正确。",
  "profile": "web-task-single-v1",
  "grader_id": "read-offset-behavior-v1",
  "limits": {
    "max_iterations": 20,
    "wall_timeout_seconds": 300
  }
}
```

20 轮、300 秒是试跑起点，不是项目当前默认值，也不是天然合适的最终预算。先根据冒烟结果调整一次，然后在 A/B 前冻结。固定 retry、输出上限和累计预算；不能让某个方案无限续跑直到成功。

grader 至少包含：故障用例由失败变通过（FAIL_TO_PASS）、原有相关用例继续通过（PASS_TO_PASS）、验收资产未被修改。采用行为断言，允许不同正确实现，不要求补丁与参考修复逐字一致。

任务入库前必须验证：seed 后目标测试失败、相关回归仍通过；应用参考修复后两类测试均通过；空补丁不能被判成功。代码修复基准通常用类似测试判定最终补丁结果，可参考 [SWE-bench evaluation](https://www.swebench.com/SWE-bench/guides/evaluation/) 和 [Anthropic Agent 评测说明](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)。

**5. 一次 trial 的生命周期**

1. 从冻结的 fixture 快照创建新工作目录，应用 seed；确认输入文件哈希。
2. 创建独立 runtime 数据目录：SQLite、Memory、transcripts、tool-results 均按 trial 隔离。
3. 根据 profile 装配 Agent，安装事件采集器与总截止时间。依赖应预先安装，环境准备耗时单列。
4. 传入任务需求，或 memory/context 场景输入。进程从 task 工作目录启动，不在多线程间改变全局 CWD。
5. 无论成功、异常还是超时，保留已写出的事件、工作区改动、usage 完整性和运行原因。
6. Agent 结束后，独立 verifier 在干净副本中应用允许的源码改动，使用原始验收资产测试。确认导入模块来自目标副本，避免 editable install 把测试导向当前开发仓库。
7. 保存 trial 结果，再生成评测集报告。grader 时间不计入 Agent 执行时间；另报实验总耗时。

Agent 执行框架代码和待修复的目标代码必须是两份独立快照。Agent 修改 C01 目标工具文件时，不能改变本次正在运行的工具执行器。

目前工作区有大量未提交修改，仅记录 git SHA 不够。snapshot 应包含白名单源码和需要的未跟踪源码，并记录文件清单哈希；不包含 `.env`、真实 memory、历史会话、参考补丁和 grader。以快照而非当前目录作为反复运行的起点。

隐藏验收需要进程/文件权限隔离保证；仅放到目录外并不能限制可执行 shell 的访问。最小可信方案可让执行环境只看到任务源码，在执行结束后才将改动交给独立 verifier。正常开发者测试可见，隐藏边界测试由 verifier 持有。

**6. profile 要固定到足以复跑**

建议第一版 profile 名称 `web-task-single-v1`：使用 Web 的 Task 规划装配、普通 Code 模式、单 worker 顺序跑。固定主模型和摘要模型 ID、服务提供方、prompt/工具 schema 哈希、产品源码快照、依赖版本、OS/shell、重试参数和试验预算。只保存非敏感配置。

- Team 关闭；子 Agent 若关闭，应同时移除 subagent 工具和禁用委派能力，仅改一个 flag 不够。将这一受控配置与日常产品默认配置明确区分。
- Skills/MCP 第一版不启用；后续作为单独 profile 比较。不要意外加载用户本机 mcp.json 或技能目录。
- memory 默认空库；M 场景按题目 seed；其他场景若需要记忆必须显式声明。关闭自动记忆提取，避免前题污染后题。
- context 保留当前实现；X 场景使用固定历史和测试阈值，A/B 对同一题保持一致。至少另留一条生产阈值下的代表性长任务，不能把低阈值应力测试当成真实长任务结果。
- 不需要交互的任务直接把需求写完整；ask_user/审批通过固定脚本和明确规则处理，未预设的交互应记录为 blocked，而不是无限等待或随意自动同意。
- 真实计费密钥只进入调用模型的一侧，验收报告不保存密钥；准备依赖后固定环境，避免每题安装造成时间噪声。

本项目 Memory/Context 会导入 legacy runtime 数据。评测输入副本应没有这些目录，且 RuntimeDataPaths 指向 trial 独立根目录；只修改工作区路径但继续用默认用户数据目录会污染实验。

**7. 先实现少量、口径明确的指标**

| 指标 | 来源/定义 | 需要避免的误读 |
|---|---|---|
| coding success | 外部验收全部通过且无回归 | `agent.completed` 或“已修复”文本不是成功证据 |
| agent duration | runner 使用 monotonic 时间包围实际执行 | 不包含安装、复制目录和 grader 时间 |
| model calls | model.started，按 call_id 去重 | 是逻辑调用；SDK 内部 HTTP 重试需要另记或固定禁用 |
| tool requested/executed/failed | tool.requested、started、failed/blocked 等分类统计 | 被拒绝的调用不能记成已执行 |
| token usage | usage.updated，按 call_id 去重，并按 call_kind 分组 | model.completed 和 usage.updated 含同一次 usage，不可重复求和 |
| memory Recall@K | 标注相关集合与候选/选中/注入集合比较 | 无相关项的题单独算误注入率，别用 0/0 |
| context continuation success | 压缩事件存在且后续产物通过验收 | 未触发压缩的 trial 不算压缩质量样本 |

每个 coding trial 同时保存 execution_status 和 task_success。超时、预算耗尽、模型服务错误保留在主结果中并单列原因；环境准备或验收器自身故障属于 invalid_trial，不当作 Agent 质量失败，但必须公开计数并原样保留，修复后重跑。

成本第一版先存 input/output/cache read/cache creation 明细，再按 provider 已确认的互斥计费口径换算。价格没有核实时 cost=null；失败请求没有 usage 时记录 usage_complete=false，不能算成零成本。已有 UsageTracker 不会凭空知道未返回 usage 的失败请求开销，runner 应结合 model.failed 明确标记不完整。

`单成功任务成本 = 全部有效尝试成本（含失败） / 成功数`。分母为 0 时成本未定义；缺失计费数据时标记为不完整估算，不输出精确降本百分比。

完整任务基线首先报告成功次数/尝试次数、逐题结果、Token 明细、中位耗时、失败原因。每题三次不足以可靠估计 P95；三次全过比例和三次至少一次通过比例可以作稳定性描述，但不能替代单次成功率。

**8. 只补两类关键观测缺口**

记忆方面，[select_context](D:/new_codeAgent/codeagent/memory/manager.py:44) 返回拼接后的文本，当前没有可直接用于评测的完整候选/选择/注入 ID 轨迹。建议增加可选 trace 或事件，记录 candidate_ids、selected_ids、injected_ids、selection_duration_ms。三种集合必须分开：模型选中了，但内容超预算未注入，是不同的问题。

上下文方面，已有 context.compacted 事件，但需要在评测适配层补充压缩前后字符数、压缩耗时、场景约束判定，并把所有摘要调用 Token 计入本 trial。字符缩短比例不是 Token 节省比例；最终以 provider usage 和任务质量为准。

这些属于观测补充，不应同时改变检索或压缩策略，否则拿不到当前方案的基线。

**9. 建议新增的文件结构**

以下是完整计划目录；当前已实现 C01 对应的 runner、agent_adapter、metrics 和独立 verifier，具体布局以 evals README 为准：

```text
D:\new_codeAgent\evals\
  __main__.py          # validate / run / report
  runner.py            # 子进程运行、截止时间、产物保存
  agent_adapter.py     # 复用现有 factory，冻结配置
  grading.py           # 调用独立 verifier
  metrics.py           # 事件去重、usage、时延、统计
  tasks\              # controller 使用的题目定义
  profiles\           # 非敏感配置
  fixtures\           # 冻结任务快照或构建定义
  verifier_assets\    # seed、gold、隐藏测试，仅 controller/verifier 可见
```

结果按 experiment/task/trial 存放在独立的 `eval-results` 根目录：manifest.json、events.jsonl、patch.diff、grader.json、result.json、report.md。task 工作区不能看到上述参考材料。机器可读结果建议 JSON/JSONL，第一版报告用 Markdown 即可。

建议 result 至少包含 task_id、trial_id、suite、profile_hash、engine_hash、fixture_hash、execution_status、task_success、invalid_reason、stop_reason、iterations、duration_ms、model_calls_by_kind、tool_counts、usage_by_kind、usage_complete、cost、grader_result 和 artifact 路径。

**10. 验收顺序**

第一步只实现 C01：validate seed/gold、创建副本、调用 Agent、外部验收、落盘结果。离线用固定响应客户端验证 runner 和 grader 接线；该结果不能填入真实模型成绩。

第二步增加 M02 和 X01，验证模型调用分类、记忆候选轨迹、压缩实际触发与续做判定。

第三步扩充十二个场景，真实模型顺序各跑三次，冻结为 baseline-v1。成本与耗时预算先用三个冒烟场景估算；不因失败而无限追加重跑。

第四步只优化记忆候选生成，得到 candidate-v1。编码与 context 集合作为回归检查；比较 memory Recall、实际注入与开销，随后增加端到端记忆复用任务，证明组件收益能传递到任务结果。

候选和基线使用相同题目快照、模型条件与预算，随机交替执行 A/B，记录运行时段与缓存状态。provider 缓存不能强制清空时，不应把某次试验直接标记为冷缓存。若模型/服务版本不同，单列实验，不把差异全部归因于代码。

十二场景阶段保留全部失败轨迹，人工复查评分器误判；扩到更大任务集后，保留一组不参与调参的题目，按任务统计配对差值和区间。

完成最小基线的标准是：同一份任务输入可复跑，每条成功有独立验收证据，每笔用量能追到调用，失败也有完整结果；然后才能可靠比较优化前后。
