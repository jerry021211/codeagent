# 中文提示词优化实施记录

日期：2026-09-15。代码已修改并完成本地回归；未提交 Git，未执行真实模型 A/B 评测。

## 已完成

| 批次 | 结果 |
| --- | --- |
| A：基础行为 | 新增中文公共 core；普通执行与 Discuss、计划、技能、记忆指导中文化；技能目录去重、稳定排序并检查加载工具是否存在 |
| B：证据与预算 | ToolOutput 保留字符串兼容性并携带 success/error/blocked 与退出码；主循环、回执 is_error、事件及验证记录识别失败；必须片段先预留预算，最终 trace 记录包含、省略、裁剪及 hash |
| C：记忆与连续性 | 记忆辅助提示和摘要提示中文化；选择数量与配置一致；记忆按完整记录纳入预算；摘要超长保留原历史并报错；计划提醒按状态去重，重复失败第二次给一次纠偏，schema 变化提醒随版本更新 |
| D：验证 | 新增 15 项机制回归并更新受中文文案影响的原测试；全量 346 项，344 通过、2 跳过、0 失败 |

现有可选 subagent 只翻译文案，没有新增编排能力。Team 专用模板、调度及权限流程没有开发改动；共享基础组件的兼容性包含在全量回归中。

## 主要入口

- [公共输入、证据和权限规则](D:/new_codeAgent/codeagent/prompts/templates/core.md)
- [普通执行提示](D:/new_codeAgent/codeagent/prompts/templates/execution.md)
- [只读讨论提示](D:/new_codeAgent/codeagent/prompts/templates/discuss.md)
- [预算与最终组装](D:/new_codeAgent/codeagent/prompts/runtime.py:238)
- [兼容旧字符串接口的工具状态](D:/new_codeAgent/codeagent/tools/base.py:32)
- [摘要器](D:/new_codeAgent/codeagent/context/manager.py:16)
- [记忆辅助模型](D:/new_codeAgent/codeagent/memory/manager.py:18)
- [新增回归测试](D:/new_codeAgent/tests/test_prompt_optimization.py)
- [完整策略](D:/new_codeAgent/docs/prompt-optimization-strategy.md)

## 兼容性与可见变化

1. Code、Discuss 的公共核心、身份/模式和必要项目规范以及当前环境事实不能静默截断。必须项超预算会抛出明确错误；大目录按完整行裁剪，其他可选片段按整体保留或省略。
2. 普通文件工具继续返回原有文本，ToolOutput 是 str 子类。Shell 的真实退出码优先于输出文字；旧第三方纯文本工具仍使用有限前缀适配，并非所有外部协议都已结构化。
3. 错误或拒绝的模型可见 tool_result 带 is_error；失败写入不会被运行状态记成已完成修改。
4. TODO 不再因计划为空、计划已完成或单纯调用数而要求新建工作；已有活跃计划仍可按配置间隔提醒。
5. 选中记忆按完整记录保留，过大记录可能被省略。摘要超过 summary_max_chars 时失败并保留原历史；没有自动偷偷丢掉末尾章节。
6. 工具名称、参数键、JSON 字段和机器状态保留兼容写法；用户项目规范、外部 Skill/MCP 的原文不会自动翻译。
7. 已经运行中的服务需要重启才会加载改动后的 Python 代码；历史消息不会被重写成中文。自定义 template_dir 中的同名模板仍优先于内置模板。
8. 提示版本在 prompt.assembled 事件中记为 single-agent-zh-v1，实际复现仍需结合模板、schema、代码与片段 hash，不能仅依据该版本字符串。

## 验证与复现

使用本地假模型和工具隔离测试，没有消费真实模型评测额度。覆盖预算边界、模式隔离、Shell 非零退出和超时、拒绝状态、失败写入、重复失败提醒、计划去重、完整记忆记录和超长摘要回退。

本轮先执行 114 项相关测试，全部通过；再执行全量 346 项测试，344 通过、2 跳过、0 失败。全量耗时约 111 秒。两项跳过不计为通过。

结果：[本次测试日志](D:/new_codeAgent/.task_outputs/prompt-opt/test-results.txt)。

在项目根目录可复现：

```powershell
$env:TEMP = 'D:\new_codeAgent\.task_outputs\prompt-opt\tmp'
$env:TMP = $env:TEMP
$env:PYTHONUTF8 = '1'
$env:LANGSMITH_TRACING = 'false'
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
python -m unittest discover -s tests -p 'test_*.py'
```

测试临时目录设在 D 盘，是因为本次检查发现 C 盘剩余空间为 0。没有删除用户文件或清理系统目录。

## 尚未执行的后续工作

- 真实模型的配对任务评测，以及任务通过率、无效重试、无必要提问、耗时和用量比较。当前测试只能证明机制行为，不能证明提示词提升了真实任务成功率。
- 摘要加最近原文窗口、超长摘要的二次模型收敛等后续上下文架构；目前保留完整 transcript，发送窗口仍采用原有摘要替换方式。
- 全请求 token 精确预算和缓存计费归因。当前实现的是字符预算及片段观测，工具 schemas 和历史仍由各自的机制管理。
- 新的统一 consult、动态工具发现或 Team 功能，均不属于本轮实现。
