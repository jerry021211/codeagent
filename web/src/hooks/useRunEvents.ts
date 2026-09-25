import { useEffect } from "react";
import { api } from "@/lib/api";
import { isRunActive } from "@/lib/utils";
import { createRunEventBuffer } from "@/lib/runEventBuffer";
import { parseRunEvent, useRunStore } from "@/store/runStore";
import type { RunStatus } from "@/types/api";

const EVENT_NAMES = [
  "run_queued", "run_started", "run_completed", "run_failed", "run_cancelled", "run_interrupted",
  "message_completed", "agent_profile_selected",
  "model_started", "model_text_delta", "model_completed", "model_failed", "model_usage",
  "usage_updated",
  "tool_requested", "tool_waiting_approval", "tool_started", "tool_completed", "tool_failed", "tool_blocked", "tool_cancelled", "tool_interrupted",
  "approval_requested", "approval_allowed", "approval_denied", "approval_expired",
  "question_requested", "question_answered",
  "todo_updated", "subagent_started", "subagent_completed", "subagent_failed",
  "recovery_retrying", "recovery_completed", "recovery_failed", "context_compacted", "history_rewritten", "prompt_assembled",
].flatMap((name) => [name, name.replace("_", "."), name.replaceAll("_", ".")]);

export function useRunEvents(runId?: string | null) {
  const ensureRun = useRunStore((state) => state.ensureRun);
  const mergeEvents = useRunStore((state) => state.mergeEvents);
  const setConnection = useRunStore((state) => state.setConnection);
  const setRunStatus = useRunStore((state) => state.setRunStatus);
  const run = useRunStore((state) => (runId ? state.runs[runId] : undefined));

  useEffect(() => {
    if (!runId) return;
    ensureRun(runId);
    const source = new EventSource(api.eventStreamUrl(runId, useRunStore.getState().runs[runId]?.lastSeq), { withCredentials: true });
    const buffer = createRunEventBuffer(mergeEvents);
    let disposed = false;
    setConnection(runId, "connecting");

    const receive = (message: MessageEvent<string>) => {
      if (disposed) return;
      const event = parseRunEvent(message.data, message.type === "message" ? undefined : message.type);
      if (event) buffer.push(event);
    };
    source.onmessage = receive;
    for (const name of EVENT_NAMES) source.addEventListener(name, receive as EventListener);
    source.addEventListener("stream.end", (message) => {
      if (disposed) return;
      const end = JSON.parse((message as MessageEvent<string>).data) as { run_id: string; status: RunStatus };
      if (end.run_id !== runId || isRunActive(end.status)) return;
      buffer.flush();
      setRunStatus(runId, end.status);
      source.close();
      setConnection(runId, "closed");
    });
    source.onopen = () => { if (!disposed) setConnection(runId, "live"); };
    source.onerror = () => {
      if (disposed) return;
      buffer.flush();
      if (source.readyState === EventSource.CLOSED) setConnection(runId, "closed");
      else setConnection(runId, "reconnecting");
    };

    return () => {
      disposed = true;
      source.close();
      // Persist queued events before switching so the reconnect cursor is accurate.
      buffer.flush();
      setConnection(runId, "closed");
    };
  }, [runId, ensureRun, mergeEvents, setConnection, setRunStatus]);

  return run;
}
