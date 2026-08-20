import { useEffect, useRef } from "react";
import { api } from "@/lib/api";
import { isRunActive } from "@/lib/utils";
import { parseRunEvent, useRunStore } from "@/store/runStore";

const EVENT_NAMES = [
  "run_queued", "run_started", "run_completed", "run_failed", "run_cancelled", "run_interrupted",
  "message_completed",
  "model_started", "model_text_delta", "model_completed", "model_failed", "model_usage",
  "usage_updated",
  "tool_requested", "tool_waiting_approval", "tool_started", "tool_completed", "tool_failed", "tool_blocked",
  "approval_requested", "approval_allowed", "approval_denied", "approval_expired",
  "todo_updated", "subagent_started", "subagent_completed", "subagent_failed",
  "recovery_retrying", "recovery_completed", "recovery_failed", "context_compacted", "history_rewritten", "prompt_assembled",
].flatMap((name) => [name, name.replaceAll("_", ".")]);

export function useRunEvents(runId?: string | null) {
  const sourceRef = useRef<EventSource>();
  const ensureRun = useRunStore((state) => state.ensureRun);
  const mergeEvent = useRunStore((state) => state.mergeEvent);
  const setConnection = useRunStore((state) => state.setConnection);
  const run = useRunStore((state) => (runId ? state.runs[runId] : undefined));

  useEffect(() => {
    if (!runId) return;
    ensureRun(runId);
    const source = new EventSource(api.eventStreamUrl(runId, run?.lastSeq), { withCredentials: true });
    sourceRef.current = source;
    setConnection(runId, "connecting");

    const receive = (message: MessageEvent<string>) => {
      const event = parseRunEvent(message.data, message.type === "message" ? undefined : message.type);
      if (event) mergeEvent(event);
    };
    source.onmessage = receive;
    for (const name of EVENT_NAMES) source.addEventListener(name, receive as EventListener);
    source.onopen = () => setConnection(runId, "live");
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) setConnection(runId, "closed");
      else setConnection(runId, "reconnecting");
    };

    return () => {
      source.close();
      if (sourceRef.current === source) sourceRef.current = undefined;
      setConnection(runId, "closed");
    };
    // The browser maintains the connection; lastSeq is only used when a new stream is created.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  useEffect(() => {
    if (!runId || !run || isRunActive(run.status)) return;
    sourceRef.current?.close();
    sourceRef.current = undefined;
    if (run.connection !== "closed") setConnection(runId, "closed");
  }, [runId, run, setConnection]);

  return run;
}
