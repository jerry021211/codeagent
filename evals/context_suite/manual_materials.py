"""Generate manual live scenarios without starting a server or calling a model."""

from __future__ import annotations

import json
from pathlib import Path
import textwrap


def _write(path: Path, value: str | dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, dict) else value
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _background(batch: str) -> str:
    return "\n".join(
        f"模拟背景 {batch}-{index:03d}：这是演示环境的日常巡检笔记，显示按钮、列表滚动、"
        "帮助说明和页面布局已讨论。本条不是导出要求，不修改任何已确认约定，不需要执行操作。"
        for index in range(1, 101)
    )


_RULES = """这是合成的上下文测试。只在聊天中回答，不写代码、不修改文件、不写长期记忆，
不创建团队、不运行发布或删除。不要读取工作区外的材料、评分答案、测试脚本。
每轮只做本轮要求，未经要求不要复述整份约定；信息不足可以读已有聊天归档，但不要猜。
"""

_INITIAL = """项目：蓝鹭报表练习。先记录这些约定，不执行导出：
导出格式 CSV；字段顺序为 姓名、金额；编码 UTF-8；禁止覆盖已有文件；金额保留两位小数；
时区 Asia/Shanghai；重复记录保留首次出现的一条；脱敏开关开启。
固定验收编号 LHX-8241，不允许改变大小写、连字符或数字。
导出路径 exports/blue-heron.csv；目前没有真实发布，也没有真实删除。
记住即可，本轮只回复“已记录第一版约定”，不要把约定另存到文件。
"""

_FIELDS = "format、fields、encoding、overwrite、decimal_places、timezone、dedup、masking、acceptance_id、output_path、published"


def _compact_prompt(number: int) -> str:
    return (
        f"现在是第 {number} 次压缩检查。请实际调用 compact 工具一次，再等待本轮完成。"
        "不要只口头说已经压缩，也不要连续重试。只简短说明工具返回了什么；"
        "最终是否成功由评测者检查 context.compacted 事件。不要复述全部业务约定。\n"
    )


def _gold(*, corrected: bool) -> dict:
    return {
        "format": "JSON" if corrected else "CSV",
        "fields": ["姓名", "金额", "批次"] if corrected else ["姓名", "金额"],
        "encoding": "UTF-8", "overwrite": False, "decimal_places": 2,
        "timezone": "Asia/Shanghai", "dedup": "keep_first", "masking": True,
        "acceptance_id": "LHX-8241",
        "output_path": "exports/final-v3.json" if corrected else "exports/blue-heron.csv",
        "published": False,
    }


def _m01_prompts() -> list[str]:
    prompts: list[str] = []
    updates = [
        _INITIAL,
        "需求修改：第一版的 CSV 已作废，改为 JSON；导出路径改为 exports/revised-v2.json。"
        "其他已确认约定保持有效。只回复“已记录第二版修改”，不要复述未修改的约定。\n",
        "最终修改：字段列表末尾追加 批次，原有字段顺序保持；路径改为 exports/final-v3.json。"
        "不要回到旧版本，其他约定仍有效。只回复“已记录最终修改”，不要复述未修改的约定。\n",
    ]
    for number, update in enumerate(updates, 1):
        prompts.append((_RULES if number == 1 else "") + update + "\n以下只是背景：\n" + _background(f"P{number}-A"))
        for suffix in ("B", "C"):
            prompts.append(
                "继续接收背景材料；这些内容不修改已有约定。只回复“背景已收到”，不要复述业务要求。\n"
                + _background(f"P{number}-{suffix}")
            )
        prompts.append(_compact_prompt(number))
    prompts.append(
        "现在请根据这段对话截至目前仍有效的约定，仅返回一个 JSON 对象，不做文件操作。"
        f"字段名为：{_FIELDS}。fields 用数组，overwrite、masking、published 用布尔值，"
        "decimal_places 用整数；dedup 用 keep_first 或 keep_last。"
        "不要把已被替代的旧要求当作当前要求；信息确实无法恢复时说明缺失，不要猜。\n"
    )
    return prompts


_CAPTURE_SCRIPT = '''"""Read-only SQLite capture. Does not send messages or call a model."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

parser = argparse.ArgumentParser()
parser.add_argument("--run-dir", type=Path, required=True)
parser.add_argument("--label", required=True)
args = parser.parse_args()
if not args.label.replace("-", "").replace("_", "").isalnum():
    parser.error("label must contain only letters, numbers, hyphens or underscores")
root = args.run_dir.resolve()
database = root / "runtime-data" / "state" / "state.db"
if not database.is_file():
    parser.error("No state.db: verify --run-dir and start the isolated server first")
out = root / "evidence" / (args.label + ".json")
if out.exists():
    parser.error("Evidence label already exists; use a new label, do not overwrite evidence")
connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
try:
    connection.execute("BEGIN")
    checkpoints = []
    for row in connection.execute("SELECT * FROM checkpoints ORDER BY created_at, rowid"):
        item = dict(row)
        messages = json.loads(item.pop("messages_json"))
        context = json.loads(item.pop("context_json"))
        item["canonical_messages"] = messages
        item["context"] = context
        canonical = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        item["canonical_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        item["message_count"] = len(messages)
        item["summary_revision"] = context.get("summary_revision", 0)
        item["compacted_message_count"] = context.get("compacted_message_count", 0)
        item["compacted_prefix_hash"] = context.get("compacted_prefix_hash", "")
        item["summary_transcript"] = context.get("summary_transcript", "")
        checkpoints.append(item)
    events = []
    for row in connection.execute("SELECT run_id, conversation_id, seq, type, payload_json FROM events ORDER BY occurred_at, rowid"):
        event = dict(row)
        event["payload"] = json.loads(event.pop("payload_json"))
        events.append(event)
    calls = [dict(row) for row in connection.execute(
        "SELECT run_id, model, call_kind, status, input_tokens, output_tokens, "
        "cache_creation_input_tokens, cache_read_input_tokens, usage_available "
        "FROM model_calls ORDER BY started_at, rowid")]
    conversations = [dict(row) for row in connection.execute(
        "SELECT id, title, workspace FROM conversations ORDER BY created_at")]
finally:
    connection.close()
archives = []
for path in sorted((root / "runtime-data" / "workspaces").rglob("*")):
    if path.is_file() and "contexts" in path.parts:
        archives.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
payload = {"status": "evidence_only_not_a_pass", "conversations": conversations,
           "checkpoints": checkpoints, "events": events, "model_calls": calls, "archives": archives}
out.parent.mkdir(parents=True, exist_ok=True)
with out.open("x", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
print(out)
for cp in checkpoints:
    print(cp["run_id"], "revision=", cp["summary_revision"], "cursor=", cp["compacted_message_count"])
'''


def _launch_script(case_id: str, repository: Path, profile: dict) -> str:
    repo_literal = str(repository).replace("'", "''")
    assignments = "\n".join(f"    $env:{key} = '{value}'" for key, value in profile.items())
    names = ", ".join(f"'{name}'" for name in ("CODEAGENT_DATA_DIR", "MCP_CONFIG", *profile))
    return f'''param(
    [Parameter(Mandatory=$true)][string]$RunDir,
    [string]$RepoPath = '{repo_literal}',
    [int]$Port = 8876,
    [switch]$Resume
)
$ErrorActionPreference = 'Stop'
$manualRunRoot = [IO.Path]::GetFullPath($RunDir)
$manualMaterialsRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\\..'))
$manualPrefix = $manualMaterialsRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if ($manualRunRoot -eq $manualMaterialsRoot -or $manualRunRoot.StartsWith($manualPrefix, [StringComparison]::OrdinalIgnoreCase)) {{
    throw 'RunDir must be outside the sealed materials package.'
}}
$manualManifest = Join-Path $manualRunRoot 'manual-run.json'
if ($Resume) {{
    if (!(Test-Path -LiteralPath $manualManifest)) {{ throw 'Resume needs an existing manual-run.json.' }}
    $manualExisting = Get-Content -LiteralPath $manualManifest -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manualExisting.case_id -ne '{case_id}') {{ throw 'Case id does not match this run directory.' }}
    if (!(Test-Path -LiteralPath (Join-Path $manualRunRoot 'workspace'))) {{ throw 'Original workspace is missing.' }}
}} else {{
    if (Test-Path -LiteralPath $manualRunRoot) {{ throw 'RunDir already exists. Use another directory for a new trial, or -Resume for restart.' }}
    New-Item -ItemType Directory -Path $manualRunRoot | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'workspace') -Destination (Join-Path $manualRunRoot 'workspace') -Recurse
    New-Item -ItemType Directory -Path (Join-Path $manualRunRoot 'evidence') | Out-Null
    @{{case_id='{case_id}'; source=$PSScriptRoot; profile='manual-explicit-compact'; status='not_evaluated'}} | ConvertTo-Json | Set-Content -LiteralPath $manualManifest -Encoding UTF8
}}
$manualEnvironmentNames = @({names})
$manualOriginalEnvironment = @{{}}
foreach ($manualName in $manualEnvironmentNames) {{
    $manualOriginalEnvironment[$manualName] = [Environment]::GetEnvironmentVariable($manualName, 'Process')
}}
Push-Location -LiteralPath $RepoPath
try {{
    $env:CODEAGENT_DATA_DIR = Join-Path $manualRunRoot 'runtime-data'
    $env:MCP_CONFIG = Join-Path $PSScriptRoot 'empty-mcp.json'
{assignments}
    Write-Host "Workspace: $(Join-Path $manualRunRoot 'workspace')"
    Write-Host "Runtime data: $env:CODEAGENT_DATA_DIR"
    Write-Host "Open http://127.0.0.1:$Port ; ordinary single-agent conversation only."
    python -m codeagent.web.cli --workspace (Join-Path $manualRunRoot 'workspace') --port $Port
    if ($LASTEXITCODE -ne 0) {{ throw "Server exited with code $LASTEXITCODE" }}
}} finally {{
    foreach ($manualName in $manualEnvironmentNames) {{
        [Environment]::SetEnvironmentVariable($manualName, $manualOriginalEnvironment[$manualName], 'Process')
    }}
    Pop-Location
}}
'''


def prepare_manual(output: Path) -> dict:
    """Create a fresh, inert material directory; refusing reuse protects old results."""
    root = output.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    repository = Path(__file__).resolve().parents[2]
    profile = {
        "ENABLE_MEMORY": "false", "ENABLE_SKILLS": "false", "TEAM_RUNTIME_ENABLED": "false",
        "TEAM_WRITE_ENABLED": "false", "MAX_ITERATIONS": "12", "CONTEXT_COMPACT_MODE": "model",
        "CONTEXT_RECENCY_MESSAGES": "2", "CONTEXT_RECENCY_ROUNDS": "2",
        "CONTEXT_MIN_FOLD_MESSAGES": "4", "CONTEXT_COMPACT_THRESHOLD_CHARS": "300000",
        "CONTEXT_MAX_REQUEST_CHARS": "600000", "CONTEXT_SUMMARY_MAX_CHARS": "4000",
        "CONTEXT_SUMMARY_INPUT_MAX_CHARS": "120000", "CONTEXT_TOOL_PROJECTION_ENABLED": "false",
        "CONTEXT_TOOL_RESULT_BUDGET_CHARS": "200000", "CONTEXT_SINGLE_TOOL_OUTPUT_MAX_CHARS": "80000",
    }
    prompts = _m01_prompts()
    cases = {
        "M01": {"title": "三次摘要后保留最早约定和最新修改", "prompts": prompts, "expected": _gold(corrected=True)},
        "M02": {
            "title": "正常结束后重启服务并继续原会话",
            "prompts": prompts[:4] + [
                "服务刚刚重启。请继续本会话，读取已恢复的上下文，实际调用 load_context_history 一次，"
                "file_path 必须使用已有摘要提供的真实归档路径；读取第一条消息的开头确认归档可访问，"
                "不要猜路径。然后根据重启前仍有效的约定，仅返回一个 JSON 对象。"
                f"字段名为：{_FIELDS}。类型同字段含义；dedup 用 keep_first 或 keep_last。不要写文件。\n",
                "下面继续补充无关背景，只回复收到，不改变现有业务约定。\n" + _background("RESTART-A"),
                "再补充一批无关背景，只回复收到，不改变现有业务约定。\n" + _background("RESTART-B"),
                _compact_prompt(2),
                "请再次根据当前约定返回 JSON，字段名仍与上次核对一致；不要重新读取外部材料，不做文件操作。\n",
            ],
            "expected": _gold(corrected=False),
        },
        "M03": {
            "title": "大型 read_file 结果归档后精确回读",
            "prompts": [
                _RULES + "请使用 read_file 一次读取 synthetic-log.txt，offset=1，limit=400。"
                "不要改用 bash、grep 或分段读取，不要寻找关键记录。本轮只回复“已读取背景日志”。\n",
                "现在要核对之前日志第 271 行。请从上一次工具返回的真实归档路径，"
                "使用 load_tool_output，offset=271、limit=1。不要再次 read_file 原文件，"
                "不要用 bash 或 grep 绕过归档。仅输出 JSON：incident_id、batch、retry_allowed。"
                "如果上轮没有保存完整输出，明确说明无法完成，不要猜。\n",
            ],
            "expected": {"incident_id": "INC-R7-4829", "batch": "B-2037", "retry_allowed": False},
        },
    }
    _write(root / "capture-state.py", _CAPTURE_SCRIPT)
    catalog = []
    for case_id, case in cases.items():
        folder = root / case_id
        workspace = folder / "workspace"
        workspace.mkdir(parents=True)
        _write(workspace / "README.md", "这是隔离的合成测试工作区。仅按聊天中的要求操作。不要读取工作区外的评分材料。")
        if case_id == "M03":
            lines = [
                f"line-{index:03d} routine-check screen=dashboard state=normal synthetic-only " + "background-" * 6
                for index in range(1, 401)
            ]
            lines[270] = "INC-R7-4829 batch=B-2037 retry_allowed=false status=review_required"
            _write(workspace / "synthetic-log.txt", "\n".join(lines))
        selected_profile = dict(profile)
        if case_id == "M03":
            selected_profile.update(CONTEXT_COMPACT_MODE="off", CONTEXT_SINGLE_TOOL_OUTPUT_MAX_CHARS="4000")
        _write(folder / "profile.json", selected_profile)
        _write(folder / "empty-mcp.json", {"mcpServers": {}})
        _write(folder / "start.ps1", _launch_script(case_id, repository, selected_profile))
        for number, prompt in enumerate(case["prompts"], 1):
            _write(folder / "prompts" / f"{number:02d}-prompt.txt", prompt)
        _write(folder / "evaluator-only" / "expected.json", {
            "case_id": case_id, "do_not_send_to_agent": True, "expected": case["expected"],
            "comparison": "字段必须齐全，无多余字段。仅 format 的 JSON/CSV 接受大小写等价；其余值和数组顺序严格比较。",
        })
        _write(folder / "evaluator-only" / "result-template.json", {
            "case_id": case_id, "status": "not_run", "live_executed": False,
            "model": None, "summarization_model": None, "run_directory": None, "conversation_id": None,
            "answer_passed": None, "mechanism_passed": None, "summary_success_count": None,
            "summary_revisions": [], "compacted_message_counts": [], "history_read_count": None,
            "tool_output_read_count": None, "restart_same_checkpoint": None,
            "api_usage": {"input_tokens": None, "cache_read_input_tokens": None, "output_tokens": None},
            "evidence_files": [], "invalid_reason": None, "notes": "生成材料不等于已执行；不要把未执行项填成通过。",
        })
        _write(folder / "操作.md", _case_instructions(case_id, root, len(case["prompts"])))
        catalog.append({"case_id": case_id, "title": case["title"], "prompt_count": len(case["prompts"]), "status": "not_run"})
    _write(root / "操作.md", _overview(root, repository))
    _write(root / "README.md", "# 人工会话测试材料\n\n从 [操作.md](操作.md) 开始。三个案例的逐步说明：\n\n"
           "- [M01：连续三次摘要](M01/操作.md)\n- [M02：正常停服重启](M02/操作.md)\n"
           "- [M03：工具大输出回读](M03/操作.md)\n\n当前仅生成材料，三个案例均未运行；生成过程不调用模型。")
    manifest = {"schema_version": "context-manual-v1", "status": "materials_only_not_run", "model_calls": 0, "cases": catalog}
    _write(root / "manifest.json", manifest)
    return manifest


def _overview(root: Path, repository: Path) -> str:
    return textwrap.dedent(f"""\
        # 真人逐轮操作的上下文测试

        这些材料已经准备好，尚未调用模型，也没有替你运行 Web 服务。它们补充自动 S 系列测试。
        打开对应目录的 操作.md，依次做 M01、M02、M03。一次只启动一个测试服务。

        - M01：发 13 条消息，检查三次摘要后旧约定和新修改是否同时保留。
        - M02：发 9 条消息，中间正常停服并重启，检查同一会话恢复和后续切点推进。
        - M03：发 2 条消息，检查真实大工具输出能否存档、再按行读取。

        **先准备一次**

        1. 在 VS Code 打开 {repository}，打开 PowerShell 终端。使用你已经能跑 live 评测的 Python 环境。
        2. 项目 .env 继续使用已有 MODEL_ID、API_KEY、SUMMARIZATION_MODEL_ID 等配置。不要把密钥贴进聊天或测试结果。
        3. 若没有 Web 依赖，在项目目录执行 `python -m pip install -e ".[web]"`。这是安装，不会调用模型。
        4. 若 web/dist/index.html 不存在，在项目目录依次执行 `npm.cmd --prefix web install`、
           `npm.cmd --prefix web run build`。后端会提供构建后的页面；已有构建可以跳过。
        5. 每个案例的 start.ps1 会把**仅 workspace 子目录**复制到包外的新执行目录。
           提示词和 evaluator-only 不会复制进去。不要把整个材料包选成 Agent 工作区。
        6. 每轮打开 prompts 下对应编号的 txt，复制全文到普通单 Agent 会话，点击发送，等本轮结束再发下一条。
           不要启用 Team，也不要用 Discuss 模式。不能把多条问题一次性全贴进去。

        **测试配置说明**

        M01/M02 显式调用 compact，并把最近消息保护量临时设为 2，便于少量回合形成可折叠历史；
        关闭长期记忆、技能、MCP 和工具投影，避免从别处找答案。其他测试参数见各目录 profile.json。
        这是检查手动滚动压缩链路，不证明生产默认水位会自动触发。自动触发用 S 系列评测。
        M03 关闭语义摘要，把单条工具结果上限设为 4000 字符，检查工具结果入口的归档和回读。
        本套件全是合成材料；不执行真实发布、付款或删除。M02 只验证正常结束后的恢复，不代表强杀进程容错。
        启动脚本退出时会恢复它临时设置的环境变量，不改项目 .env。脚本使用当前项目代码；人工测试期间不要修改代码，
        不是自动套件的冻结代码快照。可运行 `git rev-parse HEAD`、`git status --short` 记录版本和未提交修改。

        **怎么记结果**

        原始材料包保持不变。把 evaluator-only/result-template.json 复制到运行目录 evidence/result.json 再填写。
        gold 只供你自己核对，不要发送给 Agent、不要放进它的 workspace。回答正确和机制成功分别填写。
        模型只说“压缩成功”不算证据，必须看导出结果里的 context.compacted 事件、summary_revision 和切点。
        运行 capture-state.py 只读 SQLite，不调用模型。它保留检查点、调用用量、事件和归档文件哈希；
        数据保存在运行目录 evidence，不会修改封存材料。它只导出证据，不自动宣判通过。
        Web 各轮会真实调用模型，次数由模型行动决定；MAX_ITERATIONS=12 是每轮上限，不是总账单上限。
        先各做一次排错，固定代码和配置后，每案例使用新的 -RunDir 重复 3 次，保留全部成功和失败记录。
        记录使用的主模型与摘要模型、时间和代码版本。不要把人工案例用量混入 S 系列 A/D 汇总。

        **常见卡点**

        - 页面打不开：检查终端服务是否正在运行；地址是 http://127.0.0.1:8876。
          若 8876 被占用，用 `-Port 8877`，重启时也保持同端口；不要停止不属于本测试的进程。
        - PowerShell 拒绝脚本：可以在项目终端用 `powershell -NoProfile -ExecutionPolicy Bypass -File "完整的start.ps1路径" -RunDir "运行目录"`。
          这只影响这次脚本进程，不修改机器全局执行策略。
        - 摘要没生成：查是否实际调用 compact，是否有 context.compaction_failed，是否历史不足或处于失败冷却。
          记录失败原因后停止该次评测；不要不断重试直到成功再把失败删掉。
        - 重启后出现新会话列表：优先检查 -RunDir 是否变了。原来的 workspace 路径和 runtime-data 必须一起保持。
        - 重启后页面提示旧 cookie：刷新页面再试；不要因此创建新会话。
        """)


def _case_instructions(case_id: str, root: Path, count: int) -> str:
    launch = root / case_id / "start.ps1"
    capture = root / "capture-state.py"
    run_dir = Path(__file__).resolve().parents[2] / "eval-results" / "manual-runs" / f"{case_id}-r1"
    common = f"""# {case_id} 操作步骤

先阅读上一级 操作.md。本案例有 {count} 条问题，一条一条发送。材料生成不代表通过。

1. 在项目 PowerShell 终端复制下面一整行启动测试服务。第一次执行不要加 -Resume。

```powershell
& '{launch}' -RunDir '{run_dir}'
```

2. 浏览器打开 http://127.0.0.1:8876，新建一个普通单 Agent 会话。
   确认工作区是 `{run_dir / 'workspace'}`，不要选择项目仓库或材料包。
3. 提示词在本目录 prompts 下；按编号发送，必须等每轮结束。这个终端保持运行。
   如果需要检查数据，在 VS Code 另开第二个 PowerShell 终端执行导出命令。
4. 请在网页保存/抄下会话 id 或会话地址；不要靠会话标题判断是不是同一个会话。
   也可以从导出 JSON 的 conversations 数组读取 id。

"""
    if case_id == "M01":
        steps = f"""5. 发完 01～04 后，导出第一份证据：

```powershell
python '{capture}' --run-dir '{run_dir}' --label after-compact-1
```

6. 用 VS Code 打开运行目录 evidence/after-compact-1.json，搜索 `context.compacted`。
   找到真正的成功事件，并看最后一个检查点的 summary_revision 至少是 1、compacted_message_count 大于 0。
   compact 工具的“已申请”不是成功事件。没有成功就记为机制失败，先别发后续轮次。
7. 依次发 05～08，导出 `--label after-compact-2`；再发 09～12，导出 `--label after-compact-3`。
   三份证据最后的 revision 应逐次增加，compacted_message_count 也应逐次增加。
   这表示每次又折叠了新历史，而不是只重复使用第一份摘要。
8. 最后发送 13，把原样回答保存在运行目录 evidence/final-answer.txt，再对照 evaluator-only/expected.json。
   模型应该采用最后版本，早期未被修改的精确约定仍正确。仅 format 可忽略大小写，编号、路径、字段顺序不能放宽。
9. 导出 `--label final`。查看三次 context.compacted 的路径，确认归档文件存在，后续归档通过 previous 字段链接旧分段。
   归档跨段完整性更深入的程序化检查属于规则测试；不要仅凭文件存在就声称全部恢复路径正确。
"""
    elif case_id == "M02":
        steps = f"""5. 依次发送 01～04，等第 04 轮正常完成。必须确认没有正在运行的回合、工具、审批。
   在第二个终端导出：

```powershell
python '{capture}' --run-dir '{run_dir}' --label before-restart
```

6. 打开 before-restart.json。最后的 summary_revision 必须至少为 1，有 context.compacted 成功事件。
   记录最后检查点的 id、canonical_sha256、summary_revision、compacted_message_count、compacted_prefix_hash 和 summary_transcript。
   如果摘要没成功，本次不算“压缩后重启”测试，记录失败后停止，不跳过这一条件。
7. 回到运行服务的第一个终端按 Ctrl+C，等它退出并重新显示命令提示符。
   不删除、不移动运行目录。runtime-data/state/state.db 和 runtime-data/workspaces 下的归档都要保留。
8. 用**同一个 RunDir**加 -Resume 启动；即使换了终端也必须用这条完整命令：

```powershell
& '{launch}' -RunDir '{run_dir}' -Resume
```

9. 刷新网页，打开原来的会话，此时先不要发消息。在第二个终端导出：

```powershell
python '{capture}' --run-dir '{run_dir}' --label after-restart-before-message
```

10. 对比两份证据第 6 步记录的字段：重启后发新消息前应完全相同；archives 内旧文件的哈希也应相同。
    这一步验证持久化记录没有丢失，接下来才验证实际恢复使用了这些记录。
11. 向原会话发送 05。检查实际发生 load_context_history 调用且成功读到旧归档，然后用 expected.json 核对回答。
    仅网页还能显示旧消息，不足以证明模型恢复了摘要和切点。
12. 依次发送 06、07、08。再导出 `--label after-restart-compact`。
    新的 summary_revision 和 compacted_message_count 应比重启前更大；切点不应从零重新开始。
    再发送 09 核对最终回答，导出 `--label final`。如果调用失败、切点回退或答案错误，如实记录。
"""
    else:
        steps = f"""5. 发送 01，等回合结束；检查 read_file 的返回是否包含 `[tool output stored]`、真实 path 和头部预览。
   源文件有 400 行，单次读取会超过本测试的 4000 字符限制；第 271 行故意放在预览之外。
   不要让 Agent 先分段读取或 grep 关键行；发生这种情况就记录为执行偏离，不能算归档回读通过。
6. 发送 02，必须观察实际发生 load_tool_output 调用，参数 offset=271、limit=1，路径来自上轮输出。
   查看回读结果是否有目标行，再与 evaluator-only/expected.json 对照答案。
   如果模型用 read_file、grep 或 bash 重新读取源文件，即使答对，机制项也不能记为通过。
7. 导出最终证据：

```powershell
python '{capture}' --run-dir '{run_dir}' --label final
```

8. 打开 final.json 搜索 load_tool_output，结合工具结果核对路径和行号。
   本测试证明 read_file 结果进入上下文管理后可归档并回读，不证明 bash 自带截断前的原始输出都已保存。
"""
    return common + steps + f"""
完成后，把本目录 evaluator-only/result-template.json 复制到 `{run_dir / 'evidence' / 'result.json'}`，
填写实际结果。所有答案和导出证据都放运行目录，保持材料包不变。停止测试服务用它所在终端的 Ctrl+C。
重测时把命令中的 {case_id}-r1 改为 {case_id}-r2，重新开始新会话，避免读到上一轮答案。
"""
