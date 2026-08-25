import type {
  ApiList,
  ApprovalDecision,
  Conversation,
  CreateRunResponse,
  Message,
  McpConfig,
  SaveMcpServer,
  Run,
  RuntimeConfig,
  TaskList,
  TaskResource,
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

  listWorkspaces(path?: string) {
    const query = path ? `?path=${encodeURIComponent(path)}` : "";
    return request<WorkspaceListing>(`/workspaces${query}`);
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

  updateTask(taskListId: string, taskId: string, patch: Record<string, unknown> & { expectedRevision: number }) {
    return request<TaskResource>(`/task-lists/${encodeURIComponent(taskListId)}/tasks/${encodeURIComponent(taskId)}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
  },

  createRun(conversationId: string, content: string) {
    return request<CreateRunResponse>(`/conversations/${encodeURIComponent(conversationId)}/runs`, {
      method: "POST",
      body: JSON.stringify({ content }),
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
};
