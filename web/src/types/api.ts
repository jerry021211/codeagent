export type Identifier = string;

export type RunStatus =
  | "queued"
  | "running"
  | "waiting_approval"
  | "cancelling"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export type Conversation = {
  id: Identifier;
  title: string;
  workspace: string;
  created_at: string;
  updated_at: string;
  archived_at?: string | null;
  last_message?: string | null;
  active_run_id?: Identifier | null;
  run_status?: RunStatus | null;
  active_task_list_id?: Identifier | null;
};

export type TaskStatus = "pending" | "in_progress" | "completed";

export type TaskRecord = {
  id: string;
  subject: string;
  description: string;
  activeForm?: string | null;
  owner?: string | null;
  status: TaskStatus;
  blocks: string[];
  blockedBy: string[];
  metadata: Record<string, unknown>;
};

export type TaskResource = {
  taskListId: string;
  task: TaskRecord;
  revision: number;
  createdAt: string;
  updatedAt: string;
};

export type TaskList = {
  id: string;
  workspace: string;
  name: string;
  scope: "conversation_private" | "workspace_shared";
  originConversationId?: string | null;
  revision: number;
  createdAt: string;
  updatedAt: string;
  archivedAt?: string | null;
};

export type MessageRole = "user" | "assistant" | "system";

export type Message = {
  id: Identifier;
  conversation_id: Identifier;
  role: MessageRole;
  content: string;
  created_at: string;
  run_id?: Identifier | null;
  status?: "streaming" | "complete" | "failed";
};

export type TokenUsage = {
  model?: string | null;
  call_kind?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_creation_input_tokens?: number | null;
  cache_read_input_tokens?: number | null;
  prompt_input_tokens?: number | null;
  cache_hit_ratio?: number | null;
  total_tokens?: number | null;
  estimated?: boolean;
  available?: boolean;
};

export type Run = {
  id: Identifier;
  conversation_id: Identifier;
  status: RunStatus;
  queue_position?: number | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  error?: string | null;
  token_usage?: TokenUsage | null;
};

export type RunEvent = {
  id: Identifier;
  seq: number;
  type: string;
  occurred_at: string;
  conversation_id: Identifier;
  run_id: Identifier;
  agent_id: Identifier;
  parent_agent_id?: Identifier | null;
  iteration?: number | null;
  payload: Record<string, unknown>;
};

export type RuntimeConfig = {
  model?: string | null;
  workspace: string;
  max_tokens?: number | null;
  max_iterations?: number | null;
  planning_backend?: "tasks" | "todo";
  features?: Record<string, boolean>;
};

export type WorkspaceEntry = {
  name: string;
  path: string;
  is_project: boolean;
};

export type WorkspaceListing = {
  current: string;
  parent?: string | null;
  roots: string[];
  entries: WorkspaceEntry[];
};

export type CreateRunResponse = {
  run_id: Identifier;
  status: RunStatus;
  queue_position?: number | null;
};

export type ApprovalDecision = "allow" | "deny";

export type Approval = {
  id: Identifier;
  run_id: Identifier;
  tool_name: string;
  summary: string;
  reason?: string | null;
  input?: unknown;
  status: "pending" | "allowed" | "denied" | "expired";
  requested_at?: string;
};

export type TodoStatus = "pending" | "in_progress" | "completed";

export type TodoItem = {
  id: string;
  content: string;
  status: TodoStatus;
};

export type PromptFragment = {
  name: string;
  source?: string;
  characters?: number;
  truncated?: boolean;
};

export type PromptTrace = {
  hash?: string;
  characters?: number;
  fragments: PromptFragment[];
};

export type ActionKind = "model" | "tool" | "subagent" | "recovery" | "context";
export type ActionStatus = "queued" | "waiting" | "running" | "completed" | "failed" | "blocked" | "cancelled";

export type RunAction = {
  id: string;
  kind: ActionKind;
  title: string;
  subtitle?: string;
  status: ActionStatus;
  started_at?: string;
  completed_at?: string;
  input?: unknown;
  output?: unknown;
  error?: string;
  duration_ms?: number;
  agent_id?: string;
  parent_agent_id?: string | null;
  iteration?: number | null;
};

export type AgentNode = {
  id: string;
  parent_id?: string | null;
  label: string;
  status: ActionStatus;
  task?: string;
};

export type RecoveryRecord = {
  id: string;
  reason: string;
  decision?: string;
  attempt?: number;
  delay_ms?: number;
  occurred_at: string;
  status: "retrying" | "recovered" | "failed";
};

export type ApiList<T> = T[] | { items: T[] } | { data: T[] };
