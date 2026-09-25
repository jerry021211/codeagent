export type Identifier = string;
export type ExecutionMode = "normal" | "discuss";

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
  latest_run_id?: Identifier | null;
  run_status?: RunStatus | null;
  waiting_for_answer?: boolean;
  active_task_list_id?: Identifier | null;
};

export type TaskStatus =
  | "pending"
  | "in_progress"
  | "completed"
  | "failed"
  | "cancelled";

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
  metadata?: { mode?: ExecutionMode; [key: string]: unknown };
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
  model_calls?: number | null;
  available_calls?: number | null;
  unavailable_calls?: number | null;
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

export type TeamRun = {
  id: Identifier;
  conversation_id: Identifier;
  root_run_id: Identifier;
  task_list_id: Identifier;
  lead_agent_id: Identifier;
  base_commit: string;
  state: string;
  active_plan_revision?: number | null;
  max_teammates: number;
  token_budget?: number | null;
  model_call_budget?: number | null;
  deadline_at?: string | null;
  created_at: string;
  updated_at: string;
};

export type TeamPlanRevision = {
  team_run_id: Identifier;
  revision: number;
  status: string;
  plan: Record<string, unknown>;
  created_by: Identifier;
  created_at: string;
  decision_reason?: string | null;
};

export type TeamSession = {
  id: Identifier;
  agent_id: Identifier;
  generation: number;
  state: string;
  heartbeat_at: string;
  current_attempt_id?: Identifier | null;
  waiting_reason?: string | null;
  failure?: Record<string, unknown> | null;
};

export type TeamAttempt = {
  id: Identifier;
  task_id: string;
  agent_id: Identifier;
  state: string;
  ordinal: number;
  write_enabled: boolean;
  result_unknown: boolean;
  error?: Record<string, unknown> | null;
};

export type AttemptPlan = {
  id: Identifier;
  attempt_id: Identifier;
  revision: number;
  status: string;
  summary: string;
  risk_level: string;
  planned_files: string[];
  planned_commands: string[];
  write_scopes: string[];
  decision_reason?: string | null;
};

export type TeamCandidate = {
  id: Identifier;
  task_id: string;
  attempt_id: Identifier;
  revision: number;
  status: string;
  summary: string;
  changed_files: string[];
  untracked_files: string[];
  tests_reported: string[];
  known_risks: string[];
  review_reason?: string | null;
  user_approval_required: boolean;
  user_decision?: string | null;
  user_decided_by?: string | null;
  user_decided_at?: string | null;
  user_decision_reason?: string | null;
  commit_hash?: string | null;
  integrated_commit?: string | null;
  integrated_at?: string | null;
};

export type TeamWorktree = {
  id: Identifier;
  attempt_id: Identifier;
  path: string;
  branch: string;
  state: string;
  write_scopes: string[];
  write_enabled: boolean;
  frozen_reason?: string | null;
};

export type TeamScheduling = {
  task_id: string;
  dependency_ready: boolean;
  schedulable: boolean;
  reasons: string[];
};

export type TeamRecovery = {
  attempt_id: Identifier;
  task_id: string;
  agent_id: Identifier;
  reason_code: string;
  summary: string;
  recoverable: boolean;
  result_unknown: boolean;
  tool_name?: string | null;
  tool_call_id?: string | null;
  tool_executed: boolean;
  allowed_scopes: string[];
  outside_paths: string[];
  worktree_path?: string | null;
  blocking_checks: string[];
};

export type TeamSnapshot = {
  team: TeamRun;
  base_confirmation?: Record<string, unknown> | null;
  plans: TeamPlanRevision[];
  agents: Array<{ id: Identifier; role: string; name: string; model?: string }>;
  sessions: TeamSession[];
  tasks: TaskResource[];
  scheduling: TeamScheduling[];
  attempts: TeamAttempt[];
  attempt_plans: AttemptPlan[];
  worktrees: TeamWorktree[];
  recoveries: TeamRecovery[];
  candidates: TeamCandidate[];
  validation_runs: Array<{
    id: Identifier;
    candidate_id: Identifier;
    command: string;
    status: string;
    exit_code?: number | null;
    output_ref?: string | null;
  }>;
  messages: Array<{
    id: Identifier;
    type: string;
    task_id?: string | null;
    attempt_id?: Identifier | null;
    correlation_id?: Identifier | null;
    payload?: Record<string, unknown>;
    delivered_at?: string | null;
    acked_at?: string | null;
    last_delivery_error?: string | null;
  }>;
  integration_checks: Array<Record<string, unknown>>;
  usage: TokenUsage;
  manual_integration: {
    required: boolean;
    commands: string[];
    automatic_merge: false;
  };
};

export type DatabaseChange = {
  seq: number;
  occurred_at: string;
  table_name: string;
  operation: "insert" | "update" | "delete" | "snapshot";
  record_key: Record<string, unknown>;
  changed_fields: string[];
  identity: Record<string, unknown>;
  transitions: Record<string, { before: unknown; after: unknown }>;
  before?: Record<string, unknown> | null;
  after?: Record<string, unknown> | null;
};

export type TeamChangesPage = {
  items: DatabaseChange[];
  next_cursor: number;
  has_more: boolean;
  tables: string[];
  coverage: string;
};

export type McpTransport = "stdio" | "http";

export type McpServer = {
  name: string;
  transport: McpTransport;
  command: string;
  args: string[];
  cwd?: string | null;
  url: string;
  env_keys: string[];
  header_keys: string[];
};

export type McpConfig = {
  workspace: string;
  config_path: string;
  restart_required: boolean;
  servers: McpServer[];
};

export type SaveMcpServer = {
  workspace: string;
  name: string;
  transport: McpTransport;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string;
  url?: string;
  headers?: Record<string, string>;
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
export type ActionStatus = "queued" | "waiting" | "running" | "completed" | "failed" | "blocked" | "cancelled" | "unknown";

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
export interface UserQuestion {
  id: string;
  run_id: string;
  question: string;
  options: string[];
  answer: string | null;
  status: "pending" | "answered" | "cancelled";
  created_at: string;
  resolved_at: string | null;
}
