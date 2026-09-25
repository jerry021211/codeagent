import type {
  UserQuestion,
  ApiList,
  ExecutionMode,
  ApprovalDecision,
  Conversation,
  CreateRunResponse,
  Message,
  McpConfig,
  SaveMcpServer,
  Run,
  RunEvent,
  RunStatus,
  RuntimeConfig,
  TaskList,
  TaskResource,
  TeamSnapshot,
  TeamChangesPage,
  DatabaseChange,
  WorkspaceListing,
} from "@/types/api";
import { unwrapList } from "@/lib/utils";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly details?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const API_ROOT = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  headers.set("Accept", "application/json");

  const response = await fetch(`${API_ROOT}${path}`, {
    credentials: "same-origin",
    ...init,
    headers,
  });
  const contentType = response.headers.get("content-type") ?? "";
  const body = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof body === "object" && body && "detail" in body ? String(body.detail) : undefined;
    throw new ApiError(detail || `请求失败（${response.status}）`, response.status, body);
  }
  return body as T;
}

export const api = {
  async getRunActivity(runId: string) {
    const events: RunEvent[] = [];
    let after = 0;
    for (;;) {
      const page = await request<{ run_id: string; status: RunStatus; events: RunEvent[]; next_after: number | null }>(`/runs/${encodeURIComponent(runId)}/activity?after=${after}`);
      events.push(...page.events);
      if (page.next_after == null) return { runId, status: page.status, events };
      after = page.next_after;
    }
  },
  listQuestions(runId: string) {
    return request<UserQuestion[]>(`/runs/${encodeURIComponent(runId)}/questions`);
  },

  answerQuestion(runId: string, questionId: string, answer: string) {
    return request<UserQuestion>(`/runs/${encodeURIComponent(runId)}/questions/${encodeURIComponent(questionId)}/answer`, {
      method: "POST", body: JSON.stringify({ answer }),
    });
  },

  async listConversations(options?: { archived?: boolean; search?: string }) {
    const params = new URLSearchParams();
    if (options?.archived != null) params.set("archived", String(options.archived));
    if (options?.search) params.set("search", options.search);
    const suffix = params.size ? `?${params}` : "";
    return unwrapList(await request<ApiList<Conversation>>(`/conversations${suffix}`));
  },

  createConversation(options?: { title?: string; workspace?: string }) {
    return request<Conversation>("/conversations", {
      method: "POST",
      body: JSON.stringify(options ?? {}),
    });
  },

  listWorkspaces(path?: string, query?: string) {
    const params = new URLSearchParams();
    if (path) params.set("path", path);
    if (query) params.set("query", query);
    return request<WorkspaceListing>(`/workspaces${params.size ? `?${params}` : ""}`);
  },

  getConversation(id: string) {
    return request<Conversation>(`/conversations/${encodeURIComponent(id)}`);
  },

  updateConversation(id: string, patch: { title?: string; archived?: boolean }) {
    return request<Conversation>(`/conversations/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
  },

  deleteConversation(id: string) {
    return request<void>(`/conversations/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  async listMessages(conversationId: string) {
    return unwrapList(
      await request<ApiList<Message>>(`/conversations/${encodeURIComponent(conversationId)}/messages`),
    );
  },

  getTaskList(taskListId: string) {
    return request<TaskList>(`/task-lists/${encodeURIComponent(taskListId)}`);
  },

  async listTasks(taskListId: string) {
    return unwrapList(
      await request<ApiList<TaskResource>>(`/task-lists/${encodeURIComponent(taskListId)}/tasks`),
    );
  },

  createTask(taskListId: string, task: { subject: string; description: string; activeForm?: string }) {
    return request<TaskResource>(`/task-lists/${encodeURIComponent(taskListId)}/tasks`, {
      method: "POST",
      body: JSON.stringify(task),
    });
  },

  createRun(conversationId: string, content: string, useTeam = false, mode: ExecutionMode = "normal") {
    return request<CreateRunResponse>(`/conversations/${encodeURIComponent(conversationId)}/runs`, {
      method: "POST",
      body: JSON.stringify({ content, useTeam, mode }),
    });
  },

  getRun(runId: string) {
    return request<Run>(`/runs/${encodeURIComponent(runId)}`);
  },

  cancelRun(runId: string) {
    return request<Run | { status: string }>(`/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
  },

  decideApproval(runId: string, approvalId: string, decision: ApprovalDecision) {
    return request<{ status: string }>(
      `/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}`,
      { method: "POST", body: JSON.stringify({ decision }) },
    );
  },

  getRuntimeConfig() {
    return request<RuntimeConfig>("/runtime-config");
  },

  getMcpConfig(workspace: string) {
    return request<McpConfig>(`/mcp/servers?workspace=${encodeURIComponent(workspace)}`);
  },

  saveMcpServer(server: SaveMcpServer) {
    return request<McpConfig>("/mcp/servers", {
      method: "POST",
      body: JSON.stringify(server),
    });
  },

  deleteMcpServer(workspace: string, name: string) {
    return request<McpConfig>(`/mcp/servers/${encodeURIComponent(name)}?workspace=${encodeURIComponent(workspace)}`, {
      method: "DELETE",
    });
  },

  eventStreamUrl(runId: string, after?: number) {
    const query = after != null && after > 0 ? `?after=${after}` : "";
    return `${API_ROOT}/runs/${encodeURIComponent(runId)}/events${query}`;
  },

  taskEventStreamUrl(taskListId: string) {
    return `${API_ROOT}/task-lists/${encodeURIComponent(taskListId)}/events`;
  },

  listTeams(conversationId: string) {
    return request<TeamSnapshot[]>(`/teams?conversation_id=${encodeURIComponent(conversationId)}`);
  },

  getTeam(teamRunId: string) {
    return request<TeamSnapshot>(`/teams/${encodeURIComponent(teamRunId)}`);
  },

  decideTeamPlan(teamRunId: string, revision: number, decision: "approve" | "reject", reason: string) {
    return request<TeamSnapshot>(`/teams/${encodeURIComponent(teamRunId)}/plans/${revision}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision, reason, commandId: crypto.randomUUID() }),
    });
  },

  approveCandidate(teamRunId: string, candidateId: string, decision: "approve" | "reject", reason: string) {
    return request<{ candidate: unknown; team: TeamSnapshot }>(`/teams/${encodeURIComponent(teamRunId)}/candidates/${encodeURIComponent(candidateId)}/approval`, {
      method: "POST",
      body: JSON.stringify({ decision, reason, commandId: crypto.randomUUID() }),
    });
  },

  cancelTeam(teamRunId: string, reason: string) {
    return request<TeamSnapshot>(`/teams/${encodeURIComponent(teamRunId)}/cancel`, {
      method: "POST",
      body: JSON.stringify({ reason, commandId: crypto.randomUUID() }),
    });
  },

  resumeAttempt(teamRunId: string, attemptId: string, reason: string, acknowledgeUnknownResult: boolean) {
    return request<TeamSnapshot>(`/teams/${encodeURIComponent(teamRunId)}/attempts/${encodeURIComponent(attemptId)}/resume`, {
      method: "POST",
      body: JSON.stringify({ reason, acknowledgeUnknownResult, commandId: crypto.randomUUID() }),
    });
  },

  verifyManualIntegration(teamRunId: string, targetRef: string) {
    return request<{ check: Record<string, unknown>; team: TeamSnapshot }>(`/teams/${encodeURIComponent(teamRunId)}/manual-integration`, {
      method: "POST",
      body: JSON.stringify({ targetRef, commandId: crypto.randomUUID() }),
    });
  },

  disposeWorktree(teamRunId: string, worktreeId: string, action: "retain" | "cleanup") {
    return request<TeamSnapshot["worktrees"][number]>(`/teams/${encodeURIComponent(teamRunId)}/worktrees/${encodeURIComponent(worktreeId)}/disposition`, {
      method: "POST",
      body: JSON.stringify({ action, commandId: crypto.randomUUID() }),
    });
  },

  teamEventStreamUrl(teamRunId: string) {
    return `${API_ROOT}/teams/${encodeURIComponent(teamRunId)}/events`;
  },
  getTeamChanges(teamRunId: string, after = 0, table = "") {
    const query = new URLSearchParams({ after: String(after), limit: "100" });
    if (table) query.set("table", table);
    return request<TeamChangesPage>(`/teams/${encodeURIComponent(teamRunId)}/changes?${query}`);
  },

  getTeamChange(teamRunId: string, sequence: number) {
    return request<DatabaseChange>(`/teams/${encodeURIComponent(teamRunId)}/changes/${sequence}`);
  },
};
