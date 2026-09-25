import assert from "node:assert/strict";
import { after, beforeEach, test } from "node:test";
import { mkdtempSync, rmdirSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-run-events-"));
const bundle = join(directory, "store.cjs");
await build({
  stdin: {
    contents: 'export { useRunStore } from "./src/store/runStore"; export { createRunEventBuffer } from "./src/lib/runEventBuffer";',
    resolveDir: resolve("."),
    loader: "ts",
  },
  tsconfig: "tsconfig.app.json",
  bundle: true,
  platform: "node",
  format: "cjs",
  outfile: bundle,
});
const { useRunStore, createRunEventBuffer } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });
beforeEach(() => useRunStore.setState({ runs: {} }));

function event(seq, type = "model.text.delta", payload = { delta: "x" }, runId = "run-a") {
  return { id: `${runId}:${seq}`, seq, type, payload, run_id: runId, conversation_id: `conversation-${runId}`, agent_id: "root", occurred_at: "2026-09-17T00:00:00Z" };
}

test("large replay publishes once while retaining text, actions, usage and final status", () => {
  let updates = 0;
  const unsubscribe = useRunStore.subscribe(() => updates++);
  const events = [event(1, "run.started", {}), event(2, "tool.started", { tool_call_id: "tool-1", tool_name: "read_file" })];
  for (let seq = 3; seq <= 3502; seq++) events.push(event(seq));
  events.push(event(3503, "tool.completed", { tool_call_id: "tool-1", output: "done" }));
  events.push(event(3504, "usage.updated", { call_id: "call-1", input_tokens: 8, output_tokens: 9 }));
  events.push(event(3505, "run.completed", { usage: { input_tokens: 8, output_tokens: 9 } }));
  useRunStore.getState().mergeEvents(events);
  unsubscribe();
  const run = useRunStore.getState().runs["run-a"];
  assert.equal(updates, 1);
  assert.equal(run.events.length, events.length);
  assert.equal(run.lastSeq, 3505);
  assert.equal(run.streamingText, "x".repeat(3500));
  assert.equal(run.status, "completed");
  assert.equal(run.actions[run.actionOrder[0]].output, "done");
  assert.equal(run.actions[run.actionOrder[0]].status, "completed");
  assert.equal(run.usage.input_tokens, 8);
  assert.equal(run.usage.output_tokens, 9);
});

test("reconnect duplicates by id or sequence are ignored without notifying subscribers", () => {
  useRunStore.getState().mergeEvents([event(1), event(2)]);
  const before = useRunStore.getState();
  let updates = 0;
  const unsubscribe = useRunStore.subscribe(() => updates++);
  useRunStore.getState().mergeEvents([event(1), { ...event(2), id: "different-id" }]);
  unsubscribe();
  assert.equal(useRunStore.getState(), before);
  assert.equal(updates, 0);
});

test("batches preserve snapshots and sort out-of-order history", () => {
  useRunStore.getState().mergeEvent(event(2));
  const before = useRunStore.getState().runs["run-a"];
  useRunStore.getState().mergeEvents([event(1), event(3), event(3)]);
  assert.deepEqual(before.events.map(item => item.seq), [2]);
  assert.equal(before.streamingText, "x");
  const after = useRunStore.getState().runs["run-a"];
  assert.deepEqual(after.events.map(item => item.seq), [1, 2, 3]);
  assert.equal(after.streamingText, "xxx");
});

test("events for different conversations stay isolated", () => {
  useRunStore.getState().mergeEvents([event(1), event(1, "model.text.delta", { delta: "other" }, "run-b"), event(2)]);
  assert.equal(useRunStore.getState().runs["run-a"].streamingText, "xx");
  assert.equal(useRunStore.getState().runs["run-b"].streamingText, "other");
});

test("live events flush on a short timer; completion and switching flush immediately", (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const batches = [];
  const buffer = createRunEventBuffer(events => batches.push(events));
  buffer.push(event(1));
  buffer.push(event(2));
  context.mock.timers.tick(31);
  assert.equal(batches.length, 0);
  context.mock.timers.tick(1);
  assert.deepEqual(batches[0].map(item => item.seq), [1, 2]);
  buffer.push(event(3));
  buffer.flush();
  assert.deepEqual(batches[1].map(item => item.seq), [3]);
  context.mock.timers.tick(32);
  buffer.flush();
  assert.equal(batches.length, 2);
});

test("flushing before stream end preserves the authoritative terminal status", (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const buffer = createRunEventBuffer(events => useRunStore.getState().mergeEvents(events));
  buffer.push(event(1, "run.started", {}));
  buffer.push(event(2));
  buffer.flush();
  useRunStore.getState().setRunStatus("run-a", "completed");
  context.mock.timers.tick(32);
  assert.equal(useRunStore.getState().runs["run-a"].status, "completed");
  assert.equal(useRunStore.getState().runs["run-a"].lastSeq, 2);
});
