import { create } from "zustand";
import type {
  ActionKind,
  ActionStatus,
  AgentNode,
  Approval,
  PromptTrace,
  RecoveryRecord,
  RunAction,
  RunEvent,
  RunStatus,
  TodoItem,
  TokenUsage,
} from "@/types/api";
import { asNumber, asRecord, asString } from "@/lib/utils";

export type ConnectionState = "idle" | "connecting" | "live" | "reconnecting" | "closed";

export type RunViewState = {
  runId: string;
  status: RunStatus;
  queuePosition?: number | null;
  connection: ConnectionState;
  lastSeq: number;
  events: RunEvent[];
  eventKeys: Record<string, true>;
  streamingText: string;
  actions: Record<string, RunAction>;
  actionOrder: string[];
  todos: TodoItem[];
  agents: Record<string, AgentNode>;
  recovery: RecoveryRecord[];
  approvals: Record<string, Approval>;
  usage: TokenUsage;
  usageByCall: TokenUsage[];
  usageCallIds: Record<string, true>;
  promptTrace: PromptTrace;
  modifiedFiles: string[];
  error?: string;
};

type RunStore = {
  runs: Record<string, RunViewState>;
  ensureRun: (runId: string, status?: RunStatus, queuePosition?: number | null) => void;
  setConnection: (runId: string, connection: ConnectionState) => void;
  mergeEvent: (event: RunEvent) => void;
  setRunStatus: (runId: string, status: RunStatus, error?: string) => void;
  resolveApproval: (runId: string, approvalId: string, status: Approval["status"]) => void;
  clearRun: (runId: string) => void;
};

function blankRun(runId: string, status: RunStatus = "queued", queuePosition?: number | null): RunViewState {
  return {
    runId,
    status,
    queuePosition,
    connection: "idle",
    lastSeq: 0,
    events: [],
    eventKeys: {},
    streamingText: "",
    actions: {},
    actionOrder: [],
    todos: [],
    agents: {},
    recovery: [],
    approvals: {},
    usage: {},
    usageByCall: [],
    usageCallIds: {},
    promptTrace: { fragments: [] },
    modifiedFiles: [],
  };
}

function canonicalType(type: string) {
  return type.trim().toLowerCase().replace(/[.\-:/]+/g, "_");
}

function stringFrom(payload: Record<string, unknown>, ...keys: string[]) {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string") return value;
  }
  return undefined;
}

function arrayFrom(payload: Record<string, unknown>, ...keys: string[]) {
  for (const key of keys) if (Array.isArray(payload[key])) return payload[key] as unknown[];
  return undefined;
}

function statusFrom(value: unknown, fallback: ActionStatus): ActionStatus {
  if (value === "queued" || value === "waiting" || value === "running" || value === "completed" || value === "failed" || value === "blocked" || value === "cancelled") return value;
  if (value === "success" || value === "finished") return "completed";
  if (value === "error") return "failed";
  return fallback;
}

function runStatusFromType(type: string, payload: Record<string, unknown>, current: RunStatus): RunStatus {
  const explicit = payload.status;
  const valid: RunStatus[] = ["queued", "running", "waiting_approval", "cancelling", "completed", "failed", "cancelled", "interrupted"];
  if (typeof explicit === "string" && valid.includes(explicit as RunStatus)) return explicit as RunStatus;
  if (type === "run_queued") return "queued";
  if (type === "run_started" || type === "run_running") return "running";
  if (type === "approval_requested" || type === "tool_waiting_approval") return "waiting_approval";
  if (type === "run_cancelling" || type === "cancel_requested") return "cancelling";
  if (type === "run_completed" || type === "run_succeeded") return "completed";
  if (type === "run_failed") return "failed";
  if (type === "run_cancelled") return "cancelled";
  if (type === "run_interrupted") return "interrupted";
  return current;
}

function actionIdentity(event: RunEvent, payload: Record<string, unknown>, kind: ActionKind) {
  return stringFrom(payload, "action_id", "tool_call_id", "tool_use_id", "model_call_id", "call_id", "subagent_id") ?? `${kind}:${event.agent_id}:${event.iteration ?? 0}:${event.seq}`;
}

function actionKind(type: string): ActionKind | undefined {
  if (type.includes("tool")) return "tool";
  if (type.includes("model")) return "model";
  if (type.includes("subagent") || type === "agent_spawned" || type === "agent_completed") return "subagent";
  if (type.includes("recovery") || type.includes("retry")) return "recovery";
  if (type.includes("compact") || type.includes("context")) return "context";
  return undefined;
}

function actionStatus(type: string, payload: Record<string, unknown>): ActionStatus {
  if (type.includes("waiting") || type.includes("approval_requested")) return "waiting";
  if (type.includes("request") || type.includes("queued")) return "queued";
  if (type.includes("start") || type.includes("executing") || type.includes("retrying")) return "running";
  if (type.includes("complete") || type.includes("success") || type.includes("finish")) return "completed";
  if (type.includes("fail") || type.includes("error")) return "failed";
  if (type.includes("block") || type.includes("denied")) return "blocked";
  if (type.includes("cancel")) return "cancelled";
  return statusFrom(payload.status, "running");
}

function mergeUsage(current: TokenUsage, incoming: Record<string, unknown>): TokenUsage {
  const usage = asRecord(incoming.usage ?? incoming.token_usage ?? incoming);
  const take = (key: keyof TokenUsage) => {
    const value = usage[key];
    return typeof value === "number" ? value : current[key];
  };
  return {
    model: typeof usage.model === "string" ? usage.model : current.model,
    call_kind: typeof usage.call_kind === "string" ? usage.call_kind : current.call_kind,
    input_tokens: take("input_tokens") as number | null | undefined,
    output_tokens: take("output_tokens") as number | null | undefined,
    cache_creation_input_tokens: take("cache_creation_input_tokens") as number | null | undefined,
    cache_read_input_tokens: take("cache_read_input_tokens") as number | null | undefined,
    total_tokens: take("total_tokens") as number | null | undefined,
    estimated: typeof usage.estimated === "boolean" ? usage.estimated : current.estimated,
    available: typeof usage.available === "boolean" ? usage.available : current.available,
  };
}

function addUsage(current: TokenUsage, incoming: TokenUsage): TokenUsage {
  const add = (left?: number | null, right?: number | null) =>
    left == null && right == null ? undefined : (left ?? 0) + (right ?? 0);
  return {
    model: incoming.model ?? current.model,
    call_kind: incoming.call_kind ?? current.call_kind,
    input_tokens: add(current.input_tokens, incoming.input_tokens),
    output_tokens: add(current.output_tokens, incoming.output_tokens),
    cache_creation_input_tokens: add(current.cache_creation_input_tokens, incoming.cache_creation_input_tokens),
    cache_read_input_tokens: add(current.cache_read_input_tokens, incoming.cache_read_input_tokens),
    total_tokens: add(current.total_tokens, incoming.total_tokens),
    estimated: Boolean(current.estimated || incoming.estimated),
    available: current.available === false || incoming.available === false ? false : current.available ?? incoming.available,
  };
}

function normalizeTodos(value: unknown): TodoItem[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item, index) => {
    const row = asRecord(item);
    const content = stringFrom(row, "content", "text", "task");
    if (!content) return [];
    const rawStatus = stringFrom(row, "status") ?? "pending";
    const status: TodoItem["status"] = rawStatus === "completed" || rawStatus === "in_progress" ? rawStatus : "pending";
    return [{ id: stringFrom(row, "id") ?? `todo-${index}`, content, status }];
  });
}

function appendUnique(list: string[], values: unknown[]) {
  const next = new Set(list);
  for (const value of values) if (typeof value === "string" && value) next.add(value);
  return [...next];
}

function reduceEvent(state: RunViewState, event: RunEvent): RunViewState {
  const key = event.id || String(event.seq);
  if (state.eventKeys[key] || (event.seq > 0 && event.seq <= state.lastSeq && state.events.some((item) => item.seq === event.seq))) return state;

  const type = canonicalType(event.type);
  const payload = asRecord(event.payload);
  let next: RunViewState = {
    ...state,
    status: runStatusFromType(type, payload, state.status),
    queuePosition: asNumber(payload.queue_position) ?? state.queuePosition,
    lastSeq: Math.max(state.lastSeq, event.seq),
    events: [...state.events, event].sort((a, b) => a.seq - b.seq),
    eventKeys: { ...state.eventKeys, [key]: true },
  };

  if (type.includes("text_delta") || type === "assistant_delta" || type === "content_delta") {
    next.streamingText += stringFrom(payload, "delta", "text", "content") ?? "";
  } else if (type === "assistant_message" || type === "message_completed") {
    next.streamingText = stringFrom(payload, "content", "text") ?? next.streamingText;
  }

  if ((type === "run_completed" || type === "run_failed") && (payload.usage || payload.token_usage)) {
    // The terminal run event is the authoritative aggregate from the shared
    // UsageTracker. It intentionally replaces locally accumulated deltas.
    next.usage = mergeUsage({}, payload);
  } else if (type === "usage_updated" || type === "model_usage" || type === "call_usage_updated") {
    // usage.updated represents one model call, not a running total. The same
    // call's usage is also embedded in model.completed, so aggregate only this
    // dedicated event and deduplicate reconnect/replay by call_id.
    const callId = stringFrom(payload, "call_id", "model_call_id") ?? `usage:${event.id || event.seq}`;
    if (!next.usageCallIds[callId]) {
      const callUsage = mergeUsage({}, payload);
      next.usage = addUsage(next.usage, callUsage);
      next.usageByCall = [...next.usageByCall, callUsage];
      next.usageCallIds = { ...next.usageCallIds, [callId]: true };
    }
  }

  if (type.includes("todo")) {
    const todos = normalizeTodos(payload.todos ?? payload.items ?? payload.plan);
    if (todos.length || Array.isArray(payload.todos) || Array.isArray(payload.items)) next.todos = todos;
  }

  if (type.includes("prompt") && (payload.hash || payload.fragments)) {
    const rawFragments = arrayFrom(payload, "fragments") ?? [];
    next.promptTrace = {
      hash: stringFrom(payload, "hash", "prompt_hash") ?? next.promptTrace.hash,
      characters: asNumber(payload.characters ?? payload.char_count) ?? next.promptTrace.characters,
      fragments: rawFragments.map((item, index) => {
        const row = asRecord(item);
        return {
          name: stringFrom(row, "name", "id") ?? `fragment-${index + 1}`,
          source: stringFrom(row, "source"),
          characters: asNumber(row.characters ?? row.char_count),
          truncated: typeof row.truncated === "boolean" ? row.truncated : undefined,
        };
      }),
    };
  }

  const files = arrayFrom(payload, "modified_files", "files", "changed_files");
  if (files) next.modifiedFiles = appendUnique(next.modifiedFiles, files);
  const singleFile = stringFrom(payload, "file", "path", "file_path");
  if (singleFile && (type.includes("file") || type.includes("write") || type.includes("edit"))) next.modifiedFiles = appendUnique(next.modifiedFiles, [singleFile]);

  if (type === "approval_requested" || type === "tool_waiting_approval") {
    const id = stringFrom(payload, "approval_id", "id") ?? `approval:${event.seq}`;
    next.approvals = {
      ...next.approvals,
      [id]: {
        id,
        run_id: event.run_id,
        tool_name: stringFrom(payload, "tool_name", "name") ?? "危险操作",
        summary: stringFrom(payload, "summary", "description", "command") ?? "该操作需要你的确认",
        reason: stringFrom(payload, "reason"),
        input: payload.input ?? payload.arguments,
        status: "pending",
        requested_at: event.occurred_at,
      },
    };
  }

  if (type.includes("approval_") && !type.endsWith("requested")) {
    const id = stringFrom(payload, "approval_id", "id");
    if (id && next.approvals[id]) {
      const status: Approval["status"] = type.includes("allow") || type.includes("approve") ? "allowed" : type.includes("expire") || type.includes("timeout") ? "expired" : "denied";
      next.approvals = { ...next.approvals, [id]: { ...next.approvals[id], status } as Approval };
    }
  }

  if (type.includes("subagent") || type === "agent_spawned" || type === "agent_completed") {
    const id = stringFrom(payload, "agent_id", "subagent_id") ?? event.agent_id;
    const existing = next.agents[id];
    const agentStatus = actionStatus(type, payload);
    next.agents = {
      ...next.agents,
      [id]: {
        id,
        parent_id: stringFrom(payload, "parent_agent_id") ?? event.parent_agent_id,
        label: stringFrom(payload, "label", "name") ?? (event.parent_agent_id ? "子 Agent" : "主 Agent"),
        task: stringFrom(payload, "task", "description") ?? existing?.task,
        status: agentStatus,
      },
    };
  }

  if (type.includes("recovery") || type.includes("retry")) {
    const recovery: RecoveryRecord = {
      id: stringFrom(payload, "recovery_id") ?? `recovery:${event.seq}`,
      reason: stringFrom(payload, "reason", "error", "classification") ?? "正在恢复",
      decision: stringFrom(payload, "decision", "action"),
      attempt: asNumber(payload.attempt),
      delay_ms: asNumber(payload.delay_ms),
      occurred_at: event.occurred_at,
      status: type.includes("fail") ? "failed" : type.includes("complete") || type.includes("recover") && !type.includes("recovering") ? "recovered" : "retrying",
    };
    next.recovery = [...next.recovery.filter((item) => item.id !== recovery.id), recovery];
  }

  const kind = actionKind(type);
  if (kind && !type.includes("delta") && !type.includes("usage") && !type.includes("prompt")) {
    const id = actionIdentity(event, payload, kind);
    const existing = next.actions[id];
    const status = actionStatus(type, payload);
    const name = stringFrom(payload, "tool_name", "name", "label");
    const defaultTitle: Record<ActionKind, string> = { model: "模型思考", tool: "执行工具", subagent: "子 Agent", recovery: "错误恢复", context: "整理上下文" };
    const startedAt = existing?.started_at ?? (status === "running" || status === "queued" || status === "waiting" ? event.occurred_at : undefined);
    const completedAt = status === "completed" || status === "failed" || status === "blocked" || status === "cancelled" ? event.occurred_at : existing?.completed_at;
    const calculatedDuration = startedAt && completedAt ? new Date(completedAt).getTime() - new Date(startedAt).getTime() : undefined;
    next.actions = {
      ...next.actions,
      [id]: {
        id,
        kind,
        title: name || existing?.title || defaultTitle[kind],
        subtitle: stringFrom(payload, "summary", "description", "task") ?? existing?.subtitle,
        status,
        started_at: startedAt,
        completed_at: completedAt,
        input: payload.input ?? payload.arguments ?? existing?.input,
        output: payload.output ?? payload.result ?? existing?.output,
        error: stringFrom(payload, "error", "message") ?? existing?.error,
        duration_ms: asNumber(payload.duration_ms) ?? calculatedDuration ?? existing?.duration_ms,
        agent_id: event.agent_id,
        parent_agent_id: event.parent_agent_id,
        iteration: event.iteration,
      },
    };
    if (!next.actionOrder.includes(id)) next.actionOrder = [...next.actionOrder, id];
  }

  if (type === "run_failed") {
    const errorRecord = asRecord(payload.error);
    next.error = stringFrom(payload, "message") ?? stringFrom(errorRecord, "message", "detail", "error") ?? "运行失败";
  }
  return next;
}

export const useRunStore = create<RunStore>((set) => ({
  runs: {},
  ensureRun: (runId, status = "queued", queuePosition) =>
    set((store) => ({ runs: store.runs[runId] ? store.runs : { ...store.runs, [runId]: blankRun(runId, status, queuePosition) } })),
  setConnection: (runId, connection) =>
    set((store) => {
      const run = store.runs[runId] ?? blankRun(runId);
      return { runs: { ...store.runs, [runId]: { ...run, connection } } };
    }),
  mergeEvent: (event) =>
    set((store) => {
      const run = store.runs[event.run_id] ?? blankRun(event.run_id);
      return { runs: { ...store.runs, [event.run_id]: reduceEvent(run, event) } };
    }),
  setRunStatus: (runId, status, error) =>
    set((store) => {
      const run = store.runs[runId] ?? blankRun(runId);
      return { runs: { ...store.runs, [runId]: { ...run, status, error: error ?? run.error } } };
    }),
  resolveApproval: (runId, approvalId, status) =>
    set((store) => {
      const run = store.runs[runId];
      const approval = run?.approvals[approvalId];
      if (!run || !approval) return store;
      return {
        runs: {
          ...store.runs,
          [runId]: { ...run, approvals: { ...run.approvals, [approvalId]: { ...approval, status } } },
        },
      };
    }),
  clearRun: (runId) =>
    set((store) => {
      const runs = { ...store.runs };
      delete runs[runId];
      return { runs };
    }),
}));

export function parseRunEvent(raw: string, fallbackType?: string): RunEvent | null {
  try {
    const value = JSON.parse(raw) as Record<string, unknown>;
    const payload = asRecord(value.payload);
    const runId = asString(value.run_id ?? payload.run_id);
    if (!runId) return null;
    return {
      id: asString(value.id, `${runId}:${String(value.seq ?? Date.now())}`),
      seq: asNumber(value.seq) ?? 0,
      type: asString(value.type, fallbackType || "unknown"),
      occurred_at: asString(value.occurred_at, new Date().toISOString()),
      conversation_id: asString(value.conversation_id ?? payload.conversation_id),
      run_id: runId,
      agent_id: asString(value.agent_id ?? payload.agent_id, "root"),
      parent_agent_id: typeof value.parent_agent_id === "string" ? value.parent_agent_id : null,
      iteration: asNumber(value.iteration),
      payload,
    };
  } catch {
    return null;
  }
}
