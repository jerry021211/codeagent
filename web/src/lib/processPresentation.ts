import type { ActionStatus, RunAction, RunEvent } from "@/types/api";
import type { RunViewState } from "@/store/runStore";

export type ProcessEntry = {
  id: string;
  kind: "narration" | "exploration" | "tool" | "model" | "subagent" | "context" | "recovery";
  status: ActionStatus;
  label: string;
  text?: string;
  target?: string;
  agentId?: string;
  agentLabel?: string;
  durationMs?: number;
  error?: string;
  inputText?: string;
  outputText?: string;
  outputTruncated?: boolean;
  actions: RunAction[];
  count: number;
  targets: string[];
  readCount: number;
  searchCount: number;
  listCount: number;
};

export type ProcessSummary = {
  toolCount: number;
  modelCount: number;
  readCount: number;
  searchCount: number;
  listCount: number;
  editCount: number;
  commandCount: number;
  agentCount: number;
  failureCount: number;
  unknownCount: number;
  fileCount: number;
  durationMs?: number;
  latestLabel?: string;
};

export type ProcessPresentation = { entries: ProcessEntry[]; summary: ProcessSummary };

type Category = "read" | "search" | "list" | "edit" | "command" | "other";
type EventInfo = { seq: number; type: string; reason?: string; callKind?: string; stopReason?: string; output?: unknown };
type Narration = { key: string; callId: string; agentId: string; parentAgentId?: string | null; seq: number; chunks: string[]; callKind?: string };

const toolLabels: Record<string, string> = {
  read_file: "读取文件", read: "读取文件", write_file: "写入文件", write: "写入文件",
  edit_file: "修改文件", edit: "修改文件", apply_patch: "应用修改", bash: "运行命令",
  shell: "运行命令", exec_command: "运行命令", glob: "查找文件", grep: "搜索内容",
  search: "搜索内容", list_files: "浏览目录", list_directory: "浏览目录", ls: "浏览目录",
  tasklist: "查看任务列表", taskget: "查看任务", taskcreate: "创建任务", taskupdate: "更新任务",
  ask_user: "等待你的回答", skill: "读取技能", task: "委派子任务", subagent: "委派子任务",
  todo: "更新计划", todowrite: "更新计划", compact: "整理上下文",
  team_get_status: "查看团队进度", team_ask_lead: "向主 Agent 提问", team_answer_question: "答复子任务",
  team_wait: "等待团队进展", team_report_progress: "汇报任务进度", team_submit_attempt_plan: "提交执行计划",
  team_submit_analysis_result: "提交分析结果", team_submit_candidate: "提交代码成果",
  team_decide_attempt_plan: "审核执行计划", team_get_candidate_context: "检查候选修改",
};

function record(value: unknown): Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function stringFrom(value: Record<string, unknown>, ...keys: string[]) {
  for (const key of keys) if (typeof value[key] === "string") return value[key] as string;
  return undefined;
}

function typeOf(event: RunEvent) { return event.type.toLowerCase().replace(/[.\-:/]+/g, "_"); }
function normalized(text: string) { return text.trim().replace(/\s+/g, " "); }
function callKey(agentId: string, callId: string) { return JSON.stringify([agentId, callId]); }
function identity(payload: Record<string, unknown>) {
  return stringFrom(payload, "action_id", "tool_call_id", "tool_use_id", "model_call_id", "call_id", "subagent_id");
}
function eventActionId(event: RunEvent) {
  const id = identity(event.payload);
  if (id) return id;
  const type = typeOf(event);
  const kind = type.includes("tool") ? "tool" : type.includes("model") ? "model"
    : type.includes("subagent") || type === "agent_spawned" || type === "agent_completed" ? "subagent"
    : type.includes("recovery") || type.includes("retry") ? "recovery"
    : type.includes("compact") || type.includes("context") ? "context" : undefined;
  return kind ? `${kind}:${event.agent_id}:${event.iteration ?? 0}:${event.seq}` : undefined;
}
function toolCategory(action: RunAction): Category {
  if (action.kind !== "tool") return "other";
  const name = action.title.toLowerCase();
  if (["read", "read_file"].includes(name)) return "read";
  if (["grep", "search"].includes(name)) return "search";
  if (["glob", "list_files", "list_directory", "ls"].includes(name)) return "list";
  if (["write", "write_file", "edit", "edit_file", "apply_patch"].includes(name)) return "edit";
  if (["bash", "shell", "exec_command"].includes(name)) return "command";
  return "other";
}

/** Show the already-public result, not its transport envelope or hidden model fields. */
export function processOutputText(value: unknown): string | undefined {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    const text = value.flatMap((part) => {
      const item = record(part);
      return item.type === "text" && typeof item.text === "string" ? [item.text] : [];
    }).join("\n");
    return text || undefined;
  }
  const output = record(value);
  return stringFrom(output, "preview", "text", "stdout", "message", "summary", "content");
}

function targetFor(action: RunAction) {
  const input = record(action.input);
  if (action.kind !== "tool") return action.subtitle;
  const category = toolCategory(action);
  const keys = category === "command" ? ["command", "cmd"]
    : category === "search" || category === "list" ? ["pattern", "query", "path", "directory"]
    : ["file_path", "path", "subject", "summary", "question", "answer", "taskId", "task_id", "description", "prompt", "name"];
  const target = stringFrom(input, ...keys) ?? action.subtitle;
  return target ? normalized(target).slice(0, 240) : undefined;
}

function inputFor(action: RunAction) {
  const input = record(action.input);
  if (typeof action.input === "string") return action.input;
  if (toolCategory(action) === "command") return stringFrom(input, "command", "cmd");
  // Paths, queries and descriptions make useful details; raw argument JSON belongs in Debug.
  return stringFrom(input, "file_path", "path", "pattern", "query", "question", "answer", "summary", "description", "prompt", "subject", "name");
}

function labelFor(action: RunAction) {
  if (action.kind === "model") return "模型调用";
  if (action.kind === "context") return "整理上下文";
  if (action.kind === "recovery") return "恢复执行";
  if (action.kind === "subagent") return "委派子任务";
  return toolLabels[action.title.toLowerCase()] ?? action.title;
}

function agentLabel(run: RunViewState, id: string | undefined, child: boolean) {
  if (!id || !child) return undefined;
  const agent = run.agents[id];
  return agent?.label ?? "子 Agent";
}

function stamp(value?: string) {
  const time = value ? Date.parse(value) : NaN;
  return Number.isFinite(time) ? time : undefined;
}

/**
 * A read-only view of the emitted public activity. One pass over token events joins
 * chunks per (agent, call), then only the much smaller step list is sorted/grouped.
 * Successful empty model calls and root end_turn answers never become fake thoughts.
 */
export function buildProcessPresentation(run: RunViewState, options: { finalAnswer?: string } = {}): ProcessPresentation {
  const infos = new Map<string, EventInfo>();
  const narrations = new Map<string, Narration>();
  const fallbackCalls = new Map<string, string>();
  let finalAnswer = options.finalAnswer;
  let start: number | undefined;
  let end: number | undefined;
  for (const event of run.events) {
    const type = typeOf(event);
    const payload = event.payload;
    const id = eventActionId(event);
    if (id) {
      const key = callKey(event.agent_id, id);
      const previous = infos.get(key);
      infos.set(key, {
        seq: Math.min(previous?.seq ?? event.seq, event.seq),
        type,
        reason: stringFrom(payload, "reason", "error", "message") ?? previous?.reason,
        callKind: stringFrom(payload, "call_kind") ?? previous?.callKind,
        stopReason: stringFrom(payload, "stop_reason") ?? previous?.stopReason,
        output: payload.output ?? payload.result ?? previous?.output,
      });
      if (type === "model_started") fallbackCalls.set(event.agent_id, id);
    }
    if (type === "run_started") start = stamp(event.occurred_at) ?? start;
    if (["run_completed", "run_failed", "run_cancelled", "run_interrupted"].includes(type)) end = stamp(event.occurred_at) ?? end;
    if (type === "message_completed" && !event.parent_agent_id && payload.role === "assistant" && options.finalAnswer === undefined) {
      finalAnswer = stringFrom(payload, "content", "text") ?? finalAnswer;
    }
    // Explicit allowlist: do not render provider thinking/reasoning events or side queries.
    if (type !== "model_text_delta") continue;
    const text = stringFrom(payload, "text", "delta", "content");
    if (!text) continue;
    const callId = stringFrom(payload, "call_id", "model_call_id") ?? fallbackCalls.get(event.agent_id) ?? `iteration:${event.iteration ?? 0}`;
    const key = callKey(event.agent_id, callId);
    const existing = narrations.get(key);
    if (existing) existing.chunks.push(text);
    else narrations.set(key, { key, callId, agentId: event.agent_id, parentAgentId: event.parent_agent_id, seq: event.seq, chunks: [text], callKind: stringFrom(payload, "call_kind") });
  }

  const summary: ProcessSummary = { toolCount: 0, modelCount: 0, readCount: 0, searchCount: 0, listCount: 0, editCount: 0, commandCount: 0, agentCount: 0, failureCount: 0, unknownCount: 0, fileCount: 0 };
  const files = new Set<string>();
  const agents = new Set<string>();
  const ordered: Array<{ seq: number; entry: ProcessEntry }> = [];
  const actionIndexes = new Map<string, number>();
  const finalNormalized = finalAnswer ? normalized(finalAnswer) : undefined;
  let firstActionStart: number | undefined;
  let lastActionEnd: number | undefined;

  // Lifecycle events can have distinct synthetic action IDs. Show one child
  // lifecycle at its original position, and never call the root a subagent.
  const activities: Array<{ action: RunAction; seq: number; info?: EventInfo }> = [];
  const children = new Map<string, typeof activities[number]>();
  for (const [index, id] of run.actionOrder.entries()) {
    const action = run.actions[id];
    if (!action) continue;
    const key = callKey(action.agent_id ?? "root", id);
    const info = infos.get(key);
    const seq = info?.seq ?? run.events.length + index;
    if (action.kind === "subagent") {
      if (!action.parent_agent_id || !action.agent_id) continue;
      const previous = children.get(action.agent_id);
      if (previous) {
        previous.action = { ...previous.action, ...action, id: previous.action.id,
          started_at: previous.action.started_at ?? action.started_at,
          subtitle: action.subtitle ?? previous.action.subtitle,
          input: action.input ?? previous.action.input, output: action.output ?? previous.action.output };
        previous.info = info ?? previous.info;
        const from = stamp(previous.action.started_at);
        const to = stamp(previous.action.completed_at);
        if (from !== undefined && to !== undefined) previous.action.duration_ms = Math.max(0, to - from);
        continue;
      }
    }
    const activity = { action, seq, info };
    activities.push(activity);
    if (action.kind === "subagent" && action.agent_id) children.set(action.agent_id, activity);
  }

  for (const { action, seq, info } of activities) {
    const id = action.id;
    const key = callKey(action.agent_id ?? "root", id);
    actionIndexes.set(key, seq);
    const actionStart = stamp(action.started_at);
    const actionEnd = stamp(action.completed_at);
    if (actionStart !== undefined) firstActionStart = Math.min(firstActionStart ?? actionStart, actionStart);
    if (actionEnd !== undefined) lastActionEnd = Math.max(lastActionEnd ?? actionEnd, actionEnd);
    const category = toolCategory(action);
    const outputText = processOutputText(info?.type === "context_compacted" ? info.output : action.output);
    const explicitBlocked = /^\s*(?:blocked|permission denied)\s*:/i.test(outputText ?? "");
    const status = explicitBlocked ? "blocked" : info?.type === "context_compacted" ? "completed" : action.status;
    if (status === "failed" || status === "blocked") summary.failureCount++;
    if (status === "unknown") summary.unknownCount++;
    if (action.kind === "model") summary.modelCount++;
    if (action.kind === "tool") summary.toolCount++;
    if (category === "read") summary.readCount++;
    if (category === "search") summary.searchCount++;
    if (category === "list") summary.listCount++;
    if (category === "edit") summary.editCount++;
    if (category === "command") summary.commandCount++;
    const file = stringFrom(record(action.input), "file_path", "path");
    if (file && (category === "read" || category === "edit")) files.add(file);
    if (action.parent_agent_id && action.agent_id) agents.add(action.agent_id);
    // Empty successful model iterations are timing data, not user-facing reasoning.
    if (action.kind === "model" && !["failed", "blocked", "waiting", "unknown", "cancelled"].includes(status)) continue;
    const target = targetFor(action);
    const exploration = ["read", "search", "list"].includes(category) && status === "completed";
    ordered.push({ seq, entry: {
      id, kind: exploration ? "exploration" : action.kind, status, label: labelFor(action),
      target, agentId: action.agent_id, agentLabel: agentLabel(run, action.agent_id, Boolean(action.parent_agent_id)),
      durationMs: action.duration_ms, error: action.error ?? ((status === "failed" || status === "blocked" || status === "waiting" || status === "unknown") ? info?.reason ?? outputText : undefined),
      inputText: inputFor(action), outputText, outputTruncated: record(action.output).truncated === true,
      actions: [action], count: 1, targets: target ? [target] : [],
      readCount: category === "read" ? 1 : 0, searchCount: category === "search" ? 1 : 0, listCount: category === "list" ? 1 : 0,
    } });
  }

  for (const narration of narrations.values()) {
    const info = infos.get(narration.key);
    const callKind = narration.callKind ?? info?.callKind;
    if (callKind && callKind !== "main" && callKind !== "subagent") continue;
    const text = narration.chunks.join("").trim();
    if (!text) continue;
    const child = Boolean(narration.parentAgentId) || callKind === "subagent";
    if (child) agents.add(narration.agentId);
    // The current root text belongs in the streaming answer until tool_use confirms
    // it was working commentary. End-turn replies stay exclusively in the answer.
    if (!child && (info?.stopReason !== "tool_use" || (finalNormalized && normalized(text) === finalNormalized))) continue;
    const action = run.actions[narration.callId];
    const status = action?.status ?? (info?.stopReason ? "completed" : run.status === "running" ? "running" : "completed");
    ordered.push({ seq: actionIndexes.get(narration.key) ?? narration.seq, entry: {
      id: `narration:${narration.key}`, kind: "narration", status, label: "工作说明", text,
      agentId: narration.agentId, agentLabel: agentLabel(run, narration.agentId, child),
      actions: [], count: 1, targets: [], readCount: 0, searchCount: 0, listCount: 0,
    } });
  }

  ordered.sort((left, right) => left.seq - right.seq);
  const entries: ProcessEntry[] = [];
  let groupTargets = new Set<string>();
  for (const { entry } of ordered) {
    const previous = entries.at(-1);
    if (entry.kind === "exploration" && previous?.kind === "exploration" && previous.agentId === entry.agentId) {
      previous.actions.push(...entry.actions);
      previous.count += entry.count;
      previous.readCount += entry.readCount;
      previous.searchCount += entry.searchCount;
      previous.listCount += entry.listCount;
      for (const target of entry.targets) if (!groupTargets.has(target)) {
        groupTargets.add(target);
        previous.targets.push(target);
      }
      previous.durationMs = (previous.durationMs ?? 0) + (entry.durationMs ?? 0);
      previous.label = "探索代码";
      previous.target = previous.targets.slice(0, 3).join("、");
      // The original actions remain available for individual result disclosure.
      previous.inputText = undefined;
      previous.outputText = undefined;
      previous.outputTruncated = undefined;
    } else {
      entries.push(entry);
      groupTargets = new Set(entry.targets);
    }
  }
  summary.fileCount = files.size;
  summary.agentCount = agents.size;
  const durationStart = start ?? firstActionStart;
  const durationEnd = end ?? lastActionEnd;
  if (durationStart !== undefined && durationEnd !== undefined && durationEnd >= durationStart) summary.durationMs = durationEnd - durationStart;
  const latest = entries.at(-1);
  if (latest) summary.latestLabel = latest.kind === "narration" ? "正在生成回复" : [latest.label, latest.target].filter(Boolean).join(" · ");
  return { entries, summary };
}
