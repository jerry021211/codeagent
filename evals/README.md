**C01 最小 Agent 评测**

首次做上下文评测，请先打开 [通俗逐步操作指南](../docs/context-evaluation-walkthrough.md)。`python -m evals.context_suite prepare-kit` 生成完整材料，`steps` 查看步骤，`step 01-check` 开始免费检查；后续每步独立启动，live 步骤会调用真实模型。新报告含完整 token 分项与配对比较，旧记录可免费 regrade。

上下文报告同时展示人民币费用估算：`context_suite/pricing.json` 保存用户提供的峰谷价格，主模型与摘要分别计价，报告保留价格版本和哈希。尚未核实模型及计费时段，因此两种价格场景均为条件估算，实际账单字段保持 null；缺失用量不算零成本。旧记录执行 regrade 即可新增费用表，无需再调模型。

上下文专用的固定历史、A/B/C/D 批量评测和规则测试已经提供，入口为 `python -m evals.context_suite`。详细材料、运行命令和验收标准见 [上下文评测材料包](../docs/context-evaluation-kit.md)。以下仍是原有 C01 入口，两者互不替代。

上下文评分 context-v2 修正 format 的 JSON 大小写判定，报告给出失败原因并单独统计本地额度拦截。已有封存实验可以用 `python -m evals.context_suite regrade <原实验目录>` 免费重新评分，结果另存，原证据不变。

从 `D:\new_codeAgent` 运行，使用项目已安装的 Python 依赖。无需新增服务。

```powershell
python -m evals validate
python -m evals run --mode offline
python -m evals run --mode live --timeout 300 --max-iterations 20 --max-tokens 4096
python -m evals verify eval-results/<experiment-directory>
```

`validate` 检查故障版本失败、参考修复通过、空补丁失败，并保留 5 个 FAIL_TO_PASS 和 6 个 PASS_TO_PASS 的逐项结果。

`offline` 用固定响应客户端走真实 Agent 循环，只验证执行器接线，不能计入模型成绩。`live` 从进程环境或根目录 `.env` 读取 MODEL_ID、API_KEY、BASE_URL 和摘要模型配置；每次命令只执行一个 trial，真实模型会产生供应商调用费用。

`live` 同时沿用环境/`.env` 的 LangSmith 配置，`offline` 关闭追踪。C01 记录名为 `eval.C01-read-offset`，上下文评测记录名形如 `eval.context.S03.D.r1`。每次试验的 tracing.json 保存项目名、记录 ID 和发送等待状态；worker 正常退出前最多等待 5 秒发送队列。凭据不会写入实验配置。

每次命令创建独立时间戳＋随机 ID 目录，不覆盖历史结果，不自动删除失败数据。默认存放在 `D:\new_codeAgent\eval-results`，可用 `--output` 指定其他位置。CLI 成功返回 0，任务或校验失败返回 1。

**证据布局**

```text
experiment/
  source-manifest.json        # Git 状态、依赖版本、逐文件源码哈希
  engine-snapshot/            # 执行框架、评测代码和题目资产快照
  validation/                 # seed/gold 副本与独立验收
  trial-001/
    manifest.json             # prompt、配置、输入文件哈希
    worker-runtime.json       # 实际加载的执行框架路径
    workspace/                # 模型完成后的代码
    model-requests.jsonl      # 请求参数（不含认证头）
    model-responses.jsonl     # 公开内容和 provider 原始 usage
    events.jsonl              # 运行事件
    messages.jsonl            # 公开对话轨迹
    runtime/state.db          # 本次独立 SQLite / checkpoint
    execution.json            # 执行状态和耗时
    patch.diff                # 相对带缺陷起点的改动
    verification-workspace/   # 原始依赖＋候选目标文件的独立验收副本
    grader.json               # 11 项断言的 expected/actual
    stdout.txt / stderr.txt   # 子进程日志
    result.json               # 结构化指标
  result.json
  report.md
  checksums.json              # SHA-256 清单
```

public trace 会去除 provider thinking/signature 字段并脱敏；SQLite checkpoint 保持运行时原有结构。校验结果封存后不要直接编辑实验目录，补充说明或重新验收写到另一个目录。`checksums.json` 检查已有证据改动或新增文件，但不构成外部公证/时间戳；Python 缓存目录不参与校验。

**第一步的测量边界**

被测代码是当前项目读取工具的最小提取副本。只人为注入 `offset` 的偏移缺陷；没有修改产品中的读取工具。运行 Agent 的源码与待修复代码分别存储，worker 使用 Python `-P` 避免目标包遮蔽执行框架；grader 独立进程显式加载验收副本。

profile 为 `web-task-file-only-c01-v1`：复用 WebAgentFactory 的 Task 规划和 hooks，提供工作区文件工具及 Task 工具，关闭 shell、交互、子 Agent、Team、Memory、Skills 和 MCP。这个 profile 用于读取/编辑能力冒烟，不代表完整产品的命令执行、测试或多 Agent 能力。

文件工具使用 WorkspaceGuard，写入限制为唯一目标文件；验证器另外持有行为断言。它是工具级边界＋独立验证进程，不是操作系统沙箱，也不是对抗性补丁安全评测。参考修复和独立验收数据不通过 Agent 工具提供。

耗时分别记录 Agent 执行与整个 worker 时长；超时采用父进程截止时间强制退出，本 profile 不暴露能生成后台命令的工具。正常失败、超时、无 usage 等情况均保留记录。SDK 重试为 0，项目恢复重试上限为 1。`usage.updated` 按 call_id 去重；模型失败或缺失 usage 时标记不完整，未核实单价时成本为 null。

**验证代码**

```powershell
python -m unittest discover -s tests -p test_evals.py
```

覆盖 seed/gold、候选语法失败、用量去重、用量缺失、校验和、离线执行/验收导入隔离、超时留证。
