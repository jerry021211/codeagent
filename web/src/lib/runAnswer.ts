import type { Message } from "@/types/api";
import type { RunViewState } from "@/store/runStore";

type RunAnswer = { text: string; streaming: boolean };

type ModelCall = {
  text: string;
  kind: string;
  eligible: boolean;
  state: "running" | "completed" | "failed";
  stopReason?: string;
};

function stringField(payload: Record<string, unknown>, ...keys: string[]) {
  for (const key of keys) {
    if (typeof payload[key] === "string") return payload[key] as string;
  }
  return undefined;
}

/** Select the public answer without joining commentary, teammates or side queries. */
export function getRunAnswer(run: RunViewState, messages: Message[]): RunAnswer | undefined {
  if (messages.some((message) => message.run_id === run.runId && message.role === "assistant")) return undefined;

  const calls = new Map<string, ModelCall>();
  const latestByActor = new Map<string, ModelCall>();
  const orderedCalls: ModelCall[] = [];
  let authoritativeText: string | undefined;

  for (const event of run.events) {
    if (event.run_id !== run.runId) continue;
    const type = event.type.trim().toLowerCase().replace(/[.\-:/]+/g, "_");
    const payload = event.payload;
    const kind = stringField(payload, "call_kind");
    const publicMain = !event.parent_agent_id && (!kind || kind === "main");

    if (type === "message_completed" || type === "assistant_message") {
      if (publicMain && (payload.role === undefined || payload.role === "assistant")) {
        authoritativeText = stringField(payload, "content", "text") ?? authoritativeText;
      }
      continue;
    }
    if (!["model_started", "model_text_delta", "model_completed", "model_failed"].includes(type)) continue;

    const callId = stringField(payload, "call_id", "model_call_id");
    const actor = `${event.agent_id}\0${event.parent_agent_id ?? ""}`;
    const key = `${actor}\0${callId}`;
    let call = callId ? calls.get(key) : latestByActor.get(actor);
    // Legacy events have no call_id. A start (or a delta after a terminal call)
    // opens a new call instead of appending the next round to the old answer.
    if (!callId && (type === "model_started" || (kind && call?.kind !== kind) || (type === "model_text_delta" && call?.state !== "running"))) call = undefined;
    if (!call) {
      call = { text: "", kind: kind ?? "main", eligible: publicMain, state: "running" };
      orderedCalls.push(call);
      if (callId) calls.set(key, call);
    }
    if (kind) call.kind = kind;
    // Missing fields in subsequent deltas must never promote a side query or
    // child call whose identity was already established by model.started.
    call.eligible &&= publicMain;
    latestByActor.set(actor, call);

    if (type === "model_text_delta") call.text += stringField(payload, "text", "delta", "content") ?? "";
    if (type === "model_completed") {
      call.state = "completed";
      call.stopReason = stringField(payload, "stop_reason");
    } else if (type === "model_failed") call.state = "failed";
  }

  // A scheduler completion includes the actual persisted answer, including
  // any runtime additions. It also covers models configured without streaming.
  if (authoritativeText !== undefined) return authoritativeText ? { text: authoritativeText, streaming: false } : undefined;

  let latest: ModelCall | undefined;
  for (const call of orderedCalls) if (call.eligible) latest = call;
  if (!latest?.text) return undefined;
  if (latest.state === "completed") {
    return latest.stopReason === "end_turn" ? { text: latest.text, streaming: false } : undefined;
  }
  const active = ["queued", "running", "waiting_approval", "cancelling"].includes(run.status);
  return active && latest.state === "running" ? { text: latest.text, streaming: true } : undefined;
}
