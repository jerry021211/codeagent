# 上下文处理评测材料包

第一次执行请先读 [一步一步操作指南](context-evaluation-walkthrough.md)。新版提供 `prepare-kit` 一次展开所有固定题、恢复规则和人工多轮材料；`steps` 列出九个步骤，`step 01-check` 起逐步执行。每次只执行一项，前四个 step 不调用模型，后续 live 步骤需要真实用量。

报告现已直接列出主模型与摘要 token、全部试验总量/平均及 A/D 配对差异；旧实验可用 `regrade` 在新目录补表，不调用模型。下面保留原始命令和实现细节；首次操作优先采用上面的逐步指南及其统一预算。

费用也已接入：按用户提供的截图价格，分别展示全部按空闲/高峰单价的人民币估算。配置为 `evals/context_suite/pricing.json`，主模型、摘要可独立设价；缺失用量或非零缓存创建但无单价时不计算完整费用。`result.json.cost_estimates` 保留单价、来源哈希、各组及配对估算；原 `cost` 仍为 null，避免将未经账单核实的估算写成实际扣费。

本材料包对应单 Agent 的上下文处理链路，入口是 `python -m evals.context_suite`。它已经包含固定历史、用户问题、独立答案、人工摘要评分表、四组配置、执行器和报告，不需要自己编长对话。

实验分三层：规则是否正确、压缩后能否续答、真实编码任务是否整体受益。当前包覆盖前两层；既有 C01 可以补充短编码任务冒烟。六个合成题的成功率不能替代长编码任务成功率，离线脚本也不能证明真实 LLM 摘要质量。

## 1. 对照组与公平条件

| 组 | 自动摘要 mode | 工具投影 tool_projection_enabled | 回答的问题 |
|---|---|---|---|
| A | off | false | 保留原历史的基线 |
| B | off | true | 单靠工具结果清理能省多少 |
| C | model | false | 单靠 LLM 滚动摘要的收益和代价 |
| D | model | true | 当前组合策略的整体效果 |

四组都有相同的单结果/批次入站限制、请求硬预算、文件工具、load_tool_output 和 load_context_history。`off` 不等于关闭全部保护。

每次 suite 先冻结当前工作树的 codeagent、evals 和 pyproject，包括未提交代码，再从同一快照执行全部 trial。每个 trial 独立工作区、数据库、消息、归档和模型客户端。固定随机种子打散执行顺序，避免总是先跑 A；相同案例各组原始历史 SHA-256 相同，S04 的实际归档路径按 trial 隔离。

`production` 使用 ContextConfig 的默认配置（软阈值 300,000 字符、硬上限 600,000、摘要输入 120,000、摘要输出 12,000、保留最近 12 条消息等）。`stress` 只把软阈值设为 40,000，并使用较短背景材料。两种规模分开出报告，不能把 stress 节省比例当成生产收益。

为保证可比性，评测不会自动继承 `.env` 中的 CONTEXT_* 参数。实际完整配置在每个 trial 的 effective-context.json；模型窗口用 `--context-window-tokens` 和 `--summary-context-window-tokens` 显式设置，默认 0，与项目默认未知窗口行为一致。主模型、摘要模型和凭据从环境或 `.env` 读取。支持独立 SUMMARIZATION_API_KEY，主模型与摘要模型使用同一 BASE_URL；两套 SDK 共用请求编号、证据文件和调用次数上限，凭据不会写入实验配置。摘要凭据由评测客户端分流，因此 effective-context.json 的 summarization_api_key 保持空值，manifest 中 separate_summary_credentials 记录是否启用独立凭据。

## 2. 六份材料及验收

| 案例 | 材料 | 关键验收 | 观察重点 |
|---|---|---|---|
| S01 短历史 | 简短接口约定 | zh-CN、/v1/report 精确保留 | 不应无故摘要或投影 |
| S02 长搜索 | 18 轮大 grep 结果，工作区有对应资料 | 找到 exporter.py、emit_report、JSON | B/D 是否清理旧调查结果；信息是否可回读 |
| S03 需求修正 | 早期 CSV 被后续 JSON、中文字段、禁止覆盖替代，后接长背景 | 最终格式、字段、编码、禁止覆盖均正确 | 摘要会不会保留废案、遗失否定约束 |
| S04 日志中段 | 2,400 行原文；第 1,201 行有异常；经过真实上下文入站归档 | 故障码、记录 ID、重试决策正确，且工具回读确实取得证据 | 原文隐藏在预览以外时能否按页找回 |
| S05 执行状态 | 已完成回执、单测通过、集成失败、全量未运行 | 不重复发布、不宣称全量通过、下一步修复集成 | 摘要中的完成/失败/未验证是否混淆 |
| S06 阶段交接 | 解析器完成，样例未确认，后续渲染器待改 | 路径、符号、阻塞 ID、未解决状态及下一步 | 精确标识与不确定性是否保留 |

每题最终交付 `answer.json`。独立 grader 校验字段、类型和值，拒绝多余字段、缺失字段、重复 JSON 键和把 0 当 false。评分版本 context-v2 仅对 S02/S03 的 format 接受 `JSON`/`json` 等大小写等价写法；字段名、路径、标识、字段顺序和布尔值仍严格比较，原始答案保留不改写。S04 额外要求 canonical 历史中的 load_tool_output 成功结果含目标证据，猜中答案也不算通过。

S03/S05/S06 的关键事实放在旧前缀，不写入最新问题、近 12 条消息或初始 runtime state。模型可以主动回读原始历史，因此“最终答对”证明续答有效，不能单独证明摘要正文完整。fact_exposure_audit 分别列出首个摘要块、保留消息、初始运行态和回读结果中的字面命中，供人工排查信息从哪里进入。

S02 有意允许重新读取工作区原资料，测投影后的恢复能力。S04 是直接送入上下文层的合成输出；真实 bash 工具会先截短超长输出，本题不能证明 bash 已丢掉的中段可恢复。S05 是状态判断题，没有真实发布或外部副作用；真正的重复执行保护由规则测试及后续隔离任务验证。

## 3. 已准备文件的结构

小型原始配方在 `evals/context_suite/cases.json`，生成器在 materials.py。展开材料在交付目录的 materials-production 和 materials-stress，每题包括：

```text
S03/
  prompt.txt              # 可以直接阅读的当前用户问题
  seed.json               # 合法原始历史、初始文件、入站输出；没有预注入摘要
  gold.json               # 验收答案，留在工作区之外
  review-template.json    # 人工逐字段审阅摘要，未评分字段为 null
  workspace/              # Agent 真正能读取的题目文件
```

新增或修改材料后，用 prepare 生成一个新目录；同名目录不会覆盖。run 从快照中的配方重新生成同样的 seed，不读取你手改的展开目录。要永久修改题目，应修改 cases.json/materials.py 并重新 validate。

## 4. 推荐运行顺序

以下命令都在项目根目录执行，使用当前项目 Python 环境，无需新装服务。

先验证规则和验收器，不访问模型服务：

```powershell
python -m evals.context_suite validate
python -m evals.context_suite rules
python -m unittest discover -s tests -p test_evals.py
```

重新展开完整材料（目录需不存在）：

```powershell
python -m evals.context_suite prepare --scale production --output eval-results/my-materials-production
python -m evals.context_suite prepare --scale stress --output eval-results/my-materials-stress
```

跑四组的完整离线链路，检验触发、调用、归档、评分和报告：

```powershell
python -m evals.context_suite run --mode offline --scale production --variants A B C D --max-trials 24 --max-total-api-calls 288
```

offline SDK 的摘要和答案是脚本构造的。它会经过真实 WebAgentFactory、Agent、ContextManager、文件工具和 SQLite，但所有结果 `quality_measurement=false`，usage 缺失，cost 为 null。

准备开始真实模型时，先用 4 个 trial 检查摘要和回读入口：

```powershell
python -m evals.context_suite run --mode live --scale stress --cases S03 S04 --variants A D --repeats 1 --timeout 120 --max-iterations 6 --max-tokens 1024 --max-api-calls 8 --max-trials 4 --max-total-api-calls 32
```

此命令会调用供应商。上限为 4 个 trial × 8 次 SDK 请求；主调用、摘要和恢复重试共享上限。每次主输出最多 1,024 token；摘要沿用真实策略的 4,000 token 上限。每个 trial 最多 120 秒，父进程到时终止 worker。本包未执行这一步。

真实评测会读取环境或项目 `.env` 中的 LangSmith 开关、API key、项目及 endpoint；进程环境优先，兼容 LANGCHAIN_* 旧名称。离线模式始终关闭上传。你当前配置的项目是 `CodeAgent`；运行时终端会显示 `LangSmith tracing: enabled; project=CodeAgent`。在 LangSmith 的 Tracing 页面搜索 `eval.context.S03.A.r1` 或 `eval.context.S03.D.r1`，展开后能看到 Agent、模型和工具调用；模型记录的 metadata.call_kind 区分 main 与 context_summary。

每个 trial 保存 `tracing.json`，包含启用状态、项目名、根记录名称/ID和发送等待状态，不含 key。正常结束或任务异常时，worker 在关闭根记录后最多等待 5 秒发送队列；被父进程强制终止的 trial 可能来不及发送完整记录。flush 返回不等于服务端确认接收，连接/认证问题还需查看该 trial 的 stderr.txt。LangSmith 里的长输入沿用项目追踪截短规则，完整公开证据仍看本地 model-requests.jsonl。启用上传后，worker 总耗时也包含发送等待时间。

联通后，用生产阈值跑 16 个配对试验：

```powershell
python -m evals.context_suite run --mode live --scale production --cases S01 S02 S03 S04 --variants A D --repeats 2 --timeout 180 --max-api-calls 12 --max-trials 16 --max-total-api-calls 192
```

需要区分投影与摘要的贡献时，再跑全部四组、六题、三次重复：

```powershell
python -m evals.context_suite run --mode live --scale production --variants A B C D --repeats 3 --max-trials 72 --max-total-api-calls 864
```

次数和时间是调用预算，不是人民币/美元上限。请求长度和供应商计费方式会影响金额；先看小批实际 usage，再决定是否跑大矩阵。调用上限按计划的最坏次数预检，超过上限会在执行前报错。网络错误、超时和摘要失败均保留，不得挑掉失败 trial 后只比较成功部分。

达到单 trial 的 SDK 调用上限后直接停止，原因是 `budget_exceeded:evaluation_api_calls`，不再按网络错误恢复重试。报告分别显示“模型调用次数达到上限”“没有生成 answer.json”等失败原因，以及摘要尝试/成功次数、本地拦截和回读次数。摘要返回 max_tokens 属于不完整输出，即使用量齐全也不表示摘要成功。

如果旧结果仅因 format 的 JSON 大小写被判错，可以免费重评已有证据：

```powershell
python -m evals.context_suite regrade eval-results/<原实验目录>
```

regrade 先校验原实验的 checksums，在新目录生成评分，不调用模型、不修改原始记录。新目录的 regrade-source.json 保存原证据哈希、新旧分数和评分代码快照。这是事后评分修正，后续正式运行应预先采用新规则。

对“D 已保留正确要求，但回读后用完 8 次额度”的情况，下一步只做一次增加预算的诊断：

```powershell
python -m evals.context_suite run --mode live --scale stress --cases S03 --variants A D --repeats 1 --timeout 180 --max-iterations 12 --max-api-calls 16 --max-total-api-calls 32
```

两组都给 16 次机会；新结果用于判断 D 能否完成及是否仍有多余回读。增加额度不是效率修复，也不能覆盖旧实验在 8 次额度下未完成的事实。若仍反复回读或摘要反复截断，应针对这些行为排查，而不是继续增加次数或直接扩大题目范围。

## 5. 规则测试逐项看什么

rules 把详细 unittest 名称和输出保留在独立目录；用 result.json 检查是否全部通过及有无跳过。

| 机制 | 已有测试文件 | 要核实的性质 |
|---|---|---|
| 入站单条/批次限制 | test_context.py、test_tool_output_paging.py | 预览在预算内；归档保留原文；相同工具 ID 不覆盖不同输出 |
| 水位与完整请求预算 | test_context_budget.py、test_context_side_budget.py | system、tools、新输入、输出预留均计入；边界内通过、超限阻止 |
| 投影清理 | test_context_projection.py | 原历史不变；最近轮数保留；完整读、失败证据和未知命令保守保留 |
| 切点与工具配对 | test_context_runtime.py、test_context_recovery_protocol.py | user 文本与 tool_result 混合消息不能错切；并行 tool_use/result 不拆散 |
| 增量摘要 | test_context_runtime.py | 第二次只用旧摘要和新增区间；当前用户修正仍保留 |
| 摘要失败 | test_context_limits.py、test_context_runtime.py | 超长、未完成、归档失败不推进切点；冷却恢复；硬超限阻止发送 |
| 源材料预览 | test_context_summary_source.py、test_context_limits.py | 保留可见身份和协议；不携带隐藏推理和媒体原载荷 |
| 恢复与切点定位 | test_context_runtime.py、test_context_nonteam_regressions.py | 序列化重建视图一致；45 轮滚动后恢复；历史改变则废弃旧摘要 |
| 两种归档回读 | test_context_history_tool.py、test_tool_output_paging.py | 按消息/行/字符分页；长 Unicode 行不丢字；范围与路径检查 |
| 评测器自身 | test_context_evals.py、test_evals.py | 长证据不截断、SDK clone 计数共享、金标准/错误答案对照、超时留证 |

规则恢复测试主要覆盖检查点序列化/重建及续跑，不等于完整 Web 服务进程 kill/restart 的端到端验收。要测试真实服务重启，可按第 8 节人工脚本补充。

## 6. 报告怎样判断“效果好”

先看各题成功及约束，再看成本。不要用摘要更短替代效果结论。

1. **正确性**：task_success 要求运行完成、answer.json 严格通过、只修改允许文件、canonical 前缀未改、证据可解析、checkpoint 未报错；S04 还要回读证据。
2. **触发**：S01 应无摘要；S02 B/D 应出现工具投影；S03/S05/S06 C/D 应出现摘要。D 已经靠投影降到水位以下时，不做摘要是正常结果。
3. **摘要本身**：打开模型响应和 final-state.json 的 summary_text，对照 source 的相关区间填 review-template；记录有效约束漏失、旧需求复活、精确标识损坏、捏造完成/授权。发现 gold 事实从未进入摘要请求时，归因为选源/预览，不能直接算摘要器漏写。
4. **续答能力**：比较同 case/repeat 的 A/D，观察一方通过一方失败；再看 B/C 解释贡献。材料少且重复少时只报告观察值，不宣称统计显著。
5. **总开销**：metrics.usage_by_kind 与组汇总包含 main/context_summary 等调用的 input/output/cache 字段；api_requests、recall_calls、worker 耗时一起看。usage_complete 按录制器实际转交 SDK 的请求逐条核查用量；本地达到上限而未转交 SDK 的尝试单列 locally_blocked_calls。真实请求缺少响应、usage、输入/输出计数或存在重复记录时仍标记不完整。logical_usage_complete 保留旧的逻辑调用口径供排查。calls_per_success_including_failures 把失败试验耗费也计入，它不是货币成本。
6. **真实费用**：原始 provider usage 在 model-responses.jsonl；确认单价、cache 与 input 是否重复计费后再换算。当前没有价格表，cost 保持 null，不用字符估算冒充账单。

建议试运行验收门槛：所有确定性检查通过；S03 禁止覆盖、S05 不虚报测试状态、S06 不虚报阻塞已解决这类约束不退化；关键标识完全正确；D 的成功率没有观察到低于 A。成本是否改善要等真实调用；若摘要费/回读费抵消主调用节省，应照实记录为没有收益。

## 7. 每次试验留下的证据

```text
experiment/
  engine-snapshot/          # 所有 trial 共用的代码快照
  source-manifest.json      # 当前 Git 状态、依赖版本、快照哈希
  suite.json               # 随机执行顺序、重复、预算
  partial-results.json     # 已结束的 trial；中途停止时可定位进度
  trial-.../
    manifest.json          # 组别、模型、限制、原始文件与历史哈希
    seed.json / gold.json  # 输入与独立金标准；不在模型工作区
    initial-history.jsonl  # 经过真实入站归档后的 canonical 起点
    seed-state.json        # 起点状态，证明没有预填摘要或答案
    effective-context.json # Agent 实际上下文配置
    tool-schemas.json
    model-requests.jsonl   # 完整公开请求，不再截为前 200 条
    model-responses.jsonl  # 响应/错误和原始 usage
    model-blocked.jsonl    # 存在本地调用额度拦截时生成；未转交 SDK
    events.jsonl
    messages.jsonl
    final-state.json       # 摘要、切点、哈希与归档位置
    tracing.json           # LangSmith 开关、项目、根记录及发送等待状态
    runtime/state.db       # 真实持久化检查点
    workspace/answer.json
    result.json            # 字段评分、暴露审计、触发、回读、用量
  result.json / report.md
  checksums.json
```

```powershell
python -m evals.context_suite verify eval-results/<experiment-directory>
```

公开 trace 去除 thinking/signature 并脱敏常见凭据；完整 SQLite 检查点按项目原有行为保存。封存后不要修改目录；人工评分写到目录外，或另建审阅目录。哈希用于本地变更检测，不是防篡改公证。执行器限制了工具和路径，未提供操作系统级沙箱。

## 8. 后续真实长任务与服务重启材料

完成固定历史 pilot 后，可从本包复制 S03/S05/S06 的验收约束，替换为你真实项目的一次长任务；保留同样的配对与独立验收，不能靠 Agent 自述“已完成”。C01 当前只是一道短工具修复题，不足以承担这个结论。

服务重启使用单独测试数据目录，准备以下记录表（不要拿正在工作的会话试）：

| 检查时点 | 记录 | 通过标准 |
|---|---|---|
| 第一次压缩并正常结束回合 | conversation/run ID、消息数、summary_revision、compacted_message_count、prefix hash、归档路径 | checkpoint 含同一组消息与摘要状态 |
| 停止并重新启动测试 Web 服务 | 同一测试数据目录与 conversation ID | 恢复的消息顺序、切点和 hash 一致 |
| 提交“继续” | 实际请求、原始历史分页返回 | 使用原摘要和后缀；回读的消息范围正确 |
| 继续到第二次摘要 | 上次/本次切点、新摘要请求 | 旧摘要 + 新区间，无重复覆盖旧消息 |
| 仅在测试副本中修改历史前缀 | 恢复后的摘要状态 | hash 不匹配时拒绝旧摘要，不误用旧切点 |
| 运行中中断且存在工具副作用 | 已完成工具回执与恢复动作 | 依据项目运行态处理，不从旧 checkpoint 盲目重复执行 |

此表是后续人工验收脚本，本次材料包未启动 Web 服务，也未执行真实外部副作用。
