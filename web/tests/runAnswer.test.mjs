import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, rmdirSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-run-answer-"));
const bundle = join(directory, "answer.cjs");
await build({
  entryPoints: [resolve("src/lib/runAnswer.ts")],
  tsconfig: "tsconfig.app.json",
  bundle: true,
  platform: "node",
  format: "cjs",
  outfile: bundle,
});
const { getRunAnswer } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });

function event(type, payload = {}, identity = {}) {
  return { type, payload, run_id: "run-a", agent_id: "agent_root", ...identity };
}
function model(id, type, payload = {}, identity = {}) {
  return event(`model.${type}`, { call_id: id, ...payload }, identity);
}
function run(events, status = "running") {
  return { runId: "run-a", status, events, streamingText: "unsafe combined legacy text" };
}

test("live answer isolates the latest public call from tool commentary, memory and teammates", () => {
  const events = [
    model("first", "started", { call_kind: "main" }),
    model("first", "text_delta", { text: "I will inspect the file." }),
    model("first", "completed", { stop_reason: "tool_use" }),
    model("answer", "started", { call_kind: "main" }),
    model("answer", "text_delta", { text: "Fixed " }),
    model("memory", "started", { call_kind: "memory_select" }),
    model("memory", "text_delta", { text: "memory data" }),
    model("child", "text_delta", { call_kind: "subagent", text: "child output" }, { agent_id: "child", parent_agent_id: "agent_root" }),
    model("worker", "text_delta", { call_kind: "main", text: "worker output" }, { agent_id: "worker", parent_agent_id: "lead" }),
    model("answer", "text_delta", { text: "the bug." }),
  ];
  assert.deepEqual(getRunAnswer(run(events), []), { text: "Fixed the bug.", streaming: true });
});

test("team lead output does not require a hardcoded root agent id", () => {
  const events = [model("lead", "text_delta", { call_kind: "main", text: "Lead response" }, { agent_id: "lead-42" })];
  assert.deepEqual(getRunAnswer(run(events), []), { text: "Lead response", streaming: true });
});

test("a saved assistant answer for this run suppresses output even if runtime changed its text", () => {
  const state = run([model("answer", "text_delta", { text: "Original" })]);
  assert.equal(getRunAnswer(state, [{ role: "assistant", run_id: "run-a", content: "Original plus Runtime notice" }]), undefined);
  assert.deepEqual(getRunAnswer(state, [{ role: "assistant", run_id: "older-run", content: "Original" }]), { text: "Original", streaming: true });
});

test("the scheduler completion is authoritative and never has an active cursor", () => {
  const events = [
    model("answer", "text_delta", { text: "Original" }),
    event("message.completed", { role: "user", content: "Not an answer" }),
    event("message.completed", { role: "assistant", content: "Final with runtime notice" }),
    model("after", "text_delta", { call_kind: "memory_write", text: "postprocessing" }),
    event("message.completed", { role: "assistant", content: "Child completion" }, { agent_id: "child", parent_agent_id: "agent_root" }),
  ];
  assert.deepEqual(getRunAnswer(run(events), []), { text: "Final with runtime notice", streaming: false });
  assert.deepEqual(getRunAnswer(run([event("message.completed", { role: "assistant", content: "Nonstreaming result" })], "completed"), []), { text: "Nonstreaming result", streaming: false });
});

test("only a completed end_turn is a terminal model fallback", () => {
  for (const stopReason of ["tool_use", "max_tokens", undefined]) {
    const events = [model("a", "text_delta", { text: "Partial" }), model("a", "completed", { stop_reason: stopReason })];
    assert.equal(getRunAnswer(run(events, "completed"), []), undefined);
  }
  const events = [model("a", "text_delta", { text: "Done" }), model("a", "completed", { stop_reason: "end_turn" })];
  for (const status of ["running", "completed", "failed", "cancelled"]) {
    assert.deepEqual(getRunAnswer(run(events, status), []), { text: "Done", streaming: false });
  }
});

test("interrupted calls are not presented as completed answers and no old provisional answer survives a newer call", () => {
  const events = [model("a", "text_delta", { text: "Partial" })];
  for (const status of ["completed", "failed", "cancelled", "interrupted"]) assert.equal(getRunAnswer(run(events, status), []), undefined);
  assert.equal(getRunAnswer(run([...events, model("a", "failed")]), []), undefined);
  assert.equal(getRunAnswer(run([...events, model("a", "completed", { stop_reason: "end_turn" }), model("b", "started")]), []), undefined);
});

test("legacy call boundaries preserve only the current main answer without mixing actor output", () => {
  const events = [
    event("model_started"),
    event("model.text.delta", { delta: "Looking at files" }),
    event("model_completed", { stop_reason: "tool_use" }),
    event("model_started"),
    event("model_text_delta", { delta: "Answer " }),
    event("model_text_delta", { delta: "child" }, { agent_id: "child", parent_agent_id: "agent_root" }),
    event("model_text_delta", { delta: "text" }),
  ];
  assert.deepEqual(getRunAnswer(run(events), []), { text: "Answer text", streaming: true });
  assert.equal(getRunAnswer(run([]), []), undefined);
});

test("switching runs and empty authoritative answers cannot resurrect stale text", () => {
  assert.equal(getRunAnswer(run([model("a", "text_delta", { text: "Other conversation" }, { run_id: "run-b" })]), []), undefined);
  assert.equal(getRunAnswer(run([model("a", "text_delta", { text: "Old" }), event("message.completed", { role: "assistant", content: "" })]), []), undefined);
});

test("actors that reuse a call id cannot mix or suppress the main answer", () => {
  const events = [model("same", "text_delta", { text: "Main " }), model("same", "text_delta", { text: "Worker" }, { agent_id: "child", parent_agent_id: "agent_root" }), model("same", "text_delta", { text: "answer" })];
  assert.deepEqual(getRunAnswer(run(events), []), { text: "Main answer", streaming: true });
});
