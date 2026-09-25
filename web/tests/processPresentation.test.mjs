import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, rmdirSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-process-presentation-"));
const bundle = join(directory, "presentation.cjs");
await build({ entryPoints: [resolve("src/lib/processPresentation.ts")], tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle });
const { buildProcessPresentation, processOutputText } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });

function action(id, title, input = {}, extra = {}) {
  return { id, kind: "tool", title, input, status: "completed", agent_id: "root", ...extra };
}
function event(seq, type, payload, extra = {}) {
  return { id: `event-${seq}`, seq, type, payload, agent_id: "root", occurred_at: `2026-09-17T00:00:${String(seq % 60).padStart(2, "0")}Z`, ...extra };
}
function run(actions = [], events = [], extra = {}) {
  return { runId: "run-1", status: "completed", actions: Object.fromEntries(actions.map(item => [item.id, item])), actionOrder: actions.map(item => item.id), events, agents: {}, ...extra };
}
function call(seq, id, text, stopReason = "tool_use", extra = {}) {
  return [event(seq, "model.started", { call_id: id, call_kind: "main" }, extra), event(seq + 1, "model.text_delta", { call_id: id, call_kind: "main", text }, extra), event(seq + 2, "model.completed", { call_id: id, call_kind: "main", stop_reason: stopReason }, extra)];
}

test("groups successful exploration and counts calls separately from unique files", () => {
  const actions = [action("read1", "read_file", { path: "src/main.py" }), action("read2", "read_file", { path: "src/main.py" }), action("search", "grep", { pattern: "TODO", path: "src" }), action("list", "glob", { pattern: "tests/**" }), action("edit", "edit_file", { path: "src/main.py" }), action("read3", "read_file", { path: "src/other.py" })];
  const result = buildProcessPresentation(run(actions));
  assert.deepEqual(result.entries.map(entry => [entry.kind, entry.count]), [["exploration", 4], ["tool", 1], ["exploration", 1]]);
  assert.deepEqual(result.entries[0].targets, ["src/main.py", "TODO", "tests/**"]);
  assert.equal(result.entries[0].readCount, 2);
  assert.equal(result.entries[0].searchCount, 1);
  assert.equal(result.entries[0].listCount, 1);
  assert.equal(result.summary.toolCount, 6);
  assert.equal(result.summary.fileCount, 2);
  assert.equal(result.summary.editCount, 1);
  assert.equal(actions[0].count, undefined, "does not mutate run actions");
});

test("public narration stays chronological, joins call chunks, and separates exploration groups", () => {
  const actions = [action("read1", "read_file", { path: "one.py" }), action("read2", "read_file", { path: "two.py" }), action("call1", "模型思考", {}, { kind: "model" })];
  const events = [event(1, "tool.started", { tool_use_id: "read1" }), ...call(2, "call1", "我会先检查"), event(5, "model.text_delta", { call_id: "call1", text: "调用方。" }), event(6, "tool.started", { tool_use_id: "read2" })];
  const result = buildProcessPresentation(run(actions, events));
  assert.deepEqual(result.entries.map(entry => entry.kind), ["exploration", "narration", "exploration"]);
  assert.equal(result.entries[1].text, "我会先检查调用方。");
  assert.equal(result.entries.filter(entry => entry.kind === "model").length, 0);
});

test("final root answers and internal side queries are not presented as thinking", () => {
  const events = [...call(1, "comment", "先检查现有实现。"), ...call(4, "final", "最终答案。", "end_turn"), ...call(7, "old-final", "之前的答案。", "end_turn"), ...call(10, "legacy-final", "最终答案。", undefined), event(14, "model.text_delta", { call_id: "memory", call_kind: "memory_select", text: "PRIVATE SIDE QUERY" }), event(15, "model.reasoning_delta", { call_id: "comment", text: "PRIVATE REASONING" }), event(16, "message.completed", { role: "assistant", content: "最终答案。" })];
  const result = buildProcessPresentation(run([], events));
  assert.deepEqual(result.entries.map(entry => entry.text), ["先检查现有实现。"]);
});

test("child narration is attributed per agent and call, including a reused call id", () => {
  const events = [...call(1, "same", "主线程检查。"), ...call(4, "same", "子任务结果。", "end_turn", { agent_id: "worker-1", parent_agent_id: "root" }), ...call(7, "lead-final", "最终答案。", "end_turn", { agent_id: "lead-custom-id" })];
  const result = buildProcessPresentation(run([], events, { agents: { "worker-1": { id: "worker-1", label: "测试助手" } } }));
  assert.deepEqual(result.entries.map(entry => entry.text), ["主线程检查。", "子任务结果。"]);
  assert.equal(result.entries[1].agentLabel, "测试助手");
  assert.notEqual(result.entries[0].id, result.entries[1].id);
  assert.equal(result.summary.agentCount, 1);
});

test("current root output stays in the live answer until tool use confirms commentary", () => {
  const events = [event(1, "model.started", { call_id: "current", call_kind: "main" }, { agent_id: "agent_root" }), event(2, "model.text_delta", { call_id: "current", text: "正在说明下一步。" }, { agent_id: "agent_root" })];
  const activeRun = run([], events, { status: "running" });
  assert.equal(buildProcessPresentation(activeRun).entries.length, 0);
  activeRun.events = [...events, event(3, "model.completed", { call_id: "current", stop_reason: "tool_use" }, { agent_id: "agent_root" })];
  const result = buildProcessPresentation(activeRun);
  assert.equal(result.entries[0].text, "正在说明下一步。");
  assert.equal(result.entries[0].agentLabel, undefined, "root internal identifiers must not leak into labels");
});

test("failures, blocked results and waiting operations stay visible between exploration groups", () => {
  const actions = [action("r1", "read_file", { path: "a.py" }), action("blocked", "read_file", { path: "secret.py" }, { output: { preview: "Blocked: outside workspace", chars: 26, truncated: false } }), action("r2", "read_file", { path: "b.py" }), action("failed", "模型思考", {}, { kind: "model", status: "failed", error: "upstream timeout" }), action("waiting", "bash", { command: "npm test" }, { status: "waiting" })];
  const result = buildProcessPresentation(run(actions));
  assert.deepEqual(result.entries.map(entry => entry.status), ["completed", "blocked", "completed", "failed", "waiting"]);
  assert.equal(result.summary.failureCount, 2);
  assert.equal(result.entries[1].error, "Blocked: outside workspace");
  assert.equal(result.entries[3].error, "upstream timeout");
  assert.equal(result.entries[4].inputText, "npm test");
});

test("blocked event reasons survive even when the store action has no error", () => {
  const result = buildProcessPresentation(run([action("t1", "write_file", { path: "a.py" }, { status: "blocked" })], [event(1, "tool.blocked", { tool_use_id: "t1", reason: "当前是只读模式" })]));
  assert.equal(result.entries[0].error, "当前是只读模式");
});

test("tool details unwrap public preview and never serialize the transport envelope", () => {
  const result = buildProcessPresentation(run([action("shell", "bash", { command: "python -m pytest\npython -m compileall src" }, { output: { preview: "7 passed\n", chars: 3000, truncated: true } })]));
  assert.equal(result.entries[0].label, "运行命令");
  assert.equal(result.entries[0].inputText, "python -m pytest\npython -m compileall src");
  assert.equal(result.entries[0].outputText, "7 passed\n");
  assert.equal(result.entries[0].outputTruncated, true);
  assert.equal(processOutputText({ unknown: "opaque transport data" }), undefined);
  assert.equal(processOutputText([{ type: "text", text: "public" }, { type: "thinking", thinking: "hidden" }]), "public");
});

test("run elapsed time uses start/end boundaries rather than summed overlapping actions", () => {
  const actions = [action("a", "read_file", {}, { started_at: "2026-09-17T00:00:02Z", completed_at: "2026-09-17T00:00:07Z", duration_ms: 5000 }), action("b", "read_file", {}, { started_at: "2026-09-17T00:00:04Z", completed_at: "2026-09-17T00:00:09Z", duration_ms: 5000 })];
  const result = buildProcessPresentation(run(actions, [event(1, "run.started", {}), event(10, "run.completed", {})]));
  assert.equal(result.summary.durationMs, 9000);
});

test("root lifecycle is not a child, and split child lifecycle rows merge once", () => {
  const root = action("subagent:root:0:1", "子 Agent", {}, { kind: "subagent" });
  assert.equal(buildProcessPresentation(run([root])).summary.agentCount, 0);
  assert.equal(buildProcessPresentation(run([root])).entries.length, 0);
  const child = { kind: "subagent", agent_id: "child", parent_agent_id: "root" };
  const actions = [root,
    action("subagent:child:0:2", "子 Agent", {}, { ...child, status: "unknown", started_at: "2026-09-17T00:00:02Z" }),
    action("subagent:child:0:4", "子 Agent", {}, { ...child, completed_at: "2026-09-17T00:00:04Z" }),
    action("subagent:child:0:5", "子 Agent", {}, { ...child, completed_at: "2026-09-17T00:00:05Z" }),
  ];
  const result = buildProcessPresentation(run(actions));
  assert.equal(result.summary.agentCount, 1);
  assert.equal(result.entries.length, 1);
  assert.equal(result.entries[0].status, "completed");
  assert.equal(result.entries[0].durationMs, 3000);
  assert.equal(actions[1].status, "unknown", "does not mutate replay state");
});

test("synthetic context event identity preserves chronology and explicit completion", () => {
  const actions = [action("read", "read_file"), action("context:root:0:2", "整理上下文", {}, { kind: "context", status: "unknown", output: "Synthetic missing ending warning" }), action("edit", "edit_file")];
  const events = [event(1, "tool.started", { tool_call_id: "read" }), event(2, "context.compacted", {}), event(3, "tool.started", { tool_call_id: "edit" })];
  const result = buildProcessPresentation(run(actions, events));
  assert.deepEqual(result.entries.map(entry => entry.kind), ["exploration", "context", "tool"]);
  assert.equal(result.entries[1].status, "completed");
  assert.equal(result.entries[1].outputText, undefined);
  assert.equal(result.summary.unknownCount, 0);
});

test("unknown and cancelled model outcomes remain visible", () => {
  const result = buildProcessPresentation(run([action("unknown", "模型思考", {}, { kind: "model", status: "unknown", output: "实际结果未知" }), action("cancelled", "模型思考", {}, { kind: "model", status: "cancelled" })]));
  assert.deepEqual(result.entries.map(entry => entry.status), ["unknown", "cancelled"]);
  assert.equal(result.summary.unknownCount, 1);
  assert.equal(result.entries[0].error, "实际结果未知");
});
