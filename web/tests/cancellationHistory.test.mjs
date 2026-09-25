import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, rmdirSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-cancel-history-"));
const bundle = join(directory, "history.cjs");
await build({ stdin: { contents: `
export { useRunStore, buildRunHistory } from "./src/store/runStore";
export { processAnchors } from "./src/lib/conversationProcess";
export { api } from "./src/lib/api";
export { ChatWorkspace } from "./src/components/ChatWorkspace";
export { buildProcessPresentation } from "./src/lib/processPresentation";
export { createElement } from "react";
export { renderToStaticMarkup } from "react-dom/server";
export { QueryClient, QueryClientProvider } from "@tanstack/react-query";
`, resolveDir: resolve("."), loader: "tsx" }, tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle });
const { useRunStore, buildRunHistory, processAnchors, api, ChatWorkspace, createElement, renderToStaticMarkup, buildProcessPresentation, QueryClient, QueryClientProvider } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });
const event = (seq, type, payload, run_id = "first") => ({ id: `${run_id}:${seq}`, seq, type, payload, run_id, conversation_id: "chat", agent_id: "root", occurred_at: "2026-09-17T00:00:00Z" });

test("new cancellation states preserve success, unknown and not executed separately", () => {
  const events = [event(1, "tool.completed", { tool_use_id: "a", name: "write_file", output: "saved" }), event(2, "tool.interrupted", { tool_use_id: "b", name: "bash", status: "unknown", output: "possible side effects" }), event(3, "tool.cancelled", { tool_use_id: "c", name: "probe", output: "未执行" })];
  const run = buildRunHistory("first", "cancelled", events);
  assert.equal(run.actions.a.status, "completed");
  assert.equal(run.actions.a.output, "saved");
  assert.equal(run.actions.b.status, "unknown");
  assert.equal(run.actions.c.status, "cancelled");
  const entries = buildProcessPresentation(run).entries;
  assert.ok(entries.some(entry => entry.status === "unknown"));
});

test("legacy unfinished cards become unknown after full replay without inventing success", () => {
  const events = [event(1, "tool.started", { tool_use_id: "a", name: "write_file" })];
  const run = buildRunHistory("first", "cancelled", events);
  assert.equal(run.actions.a.status, "unknown");
  assert.match(run.actions.a.output, /实际结果未知/);
  useRunStore.setState({ runs: {} });
  useRunStore.getState().mergeEvents(events);
  useRunStore.getState().setRunStatus("first", "cancelled");
  assert.equal(useRunStore.getState().runs.first.actions.a.status, "running");
  useRunStore.getState().setConnection("first", "closed");
  assert.equal(useRunStore.getState().runs.first.actions.a.status, "unknown");
});

test("cancelled run without assistant reply stays next to its own prompt", () => {
  const messages = [{ id: "u1", role: "user", run_id: "first", content: "first question", conversation_id: "chat", created_at: "2026-09-17T00:00:00Z" }, { id: "u2", role: "user", run_id: "second", content: "second question", conversation_id: "chat", created_at: "2026-09-17T00:01:00Z" }];
  assert.deepEqual(processAnchors(messages, ["first", "second"]), { before: {}, after: { u1: ["first"], u2: ["second"] }, trailing: [] });
  const first = buildRunHistory("first", "cancelled", [event(1, "tool.completed", { tool_use_id: "a", output: "saved" })]);
  const second = buildRunHistory("second", "completed", []);
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: new QueryClient() }, createElement(ChatWorkspace, { messages, run: second, historyRuns: { first }, draft: "", theme: "light", onDraft() {}, onSend() {}, onCancel() {}, onModeChange() {} })));
  assert.equal((html.match(/aria-label="执行过程"/g) ?? []).length, 2);
  assert.match(html, /first question[\s\S]*Thinking[\s\S]*second question[\s\S]*Thinking/);
});

test("history fetch follows pagination and does not require an SSE connection", async () => {
  const original = globalThis.fetch;
  const urls = [];
  globalThis.fetch = async url => {
    urls.push(String(url));
    const second = String(url).includes("after=7");
    return new Response(JSON.stringify({ run_id: "first", status: "cancelled", events: [event(second ? 8 : 7, "tool.completed", { tool_use_id: second ? "b" : "a" })], next_after: second ? null : 7 }), { headers: { "Content-Type": "application/json" } });
  };
  try {
    const result = await api.getRunActivity("first");
    assert.deepEqual(result.events.map(item => item.seq), [7, 8]);
    assert.equal(urls.length, 2);
    assert.match(urls[1], /after=7/);
  } finally { globalThis.fetch = original; }
});
