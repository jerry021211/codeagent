import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, X } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { cx, isRunActive } from "@/lib/utils";
import type { ApprovalDecision, Conversation, McpConfig, Message, SaveMcpServer, TaskResource } from "@/types/api";
import { ConversationSidebar } from "@/components/ConversationSidebar";
import { ChatWorkspace } from "@/components/ChatWorkspace";
import { InspectorPanel } from "@/components/InspectorPanel";
import { WorkspacePicker } from "@/components/WorkspacePicker";
import { McpConfigModal } from "@/components/McpConfigModal";
import { useRunEvents } from "@/hooks/useRunEvents";
import { useRunStore } from "@/store/runStore";

type ThemeMode = "system" | "light" | "dark";

const conversationsKey = ["conversations"] as const;
const messagesKey = (conversationId: string) => ["conversations", conversationId, "messages"] as const;
const tasksKey = (taskListId: string) => ["task-lists", taskListId, "tasks"] as const;
const teamsKey = (conversationId: string) => ["teams", conversationId] as const;

type TeamCommand =
  | { kind: "team-plan"; revision: number; decision: "approve" | "reject"; reason: string }
  | { kind: "candidate-approval"; candidateId: string; decision: "approve" | "reject"; reason: string }
  | { kind: "resume-attempt"; attemptId: string; reason: string; acknowledgeUnknownResult: boolean }
  | { kind: "cancel"; reason: string }
  | { kind: "integration"; targetRef: string }
  | { kind: "cleanup"; worktreeId: string };

export default function App() {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | undefined>(() => localStorage.getItem("codeagent.conversation") || undefined);
  const [search, setSearch] = useState("");
  const [draft, setDraft] = useState("");
  const [useTeam, setUseTeam] = useState(false);
  const [leftOpen, setLeftOpen] = useState(false);
  const [rightOpen, setRightOpen] = useState(false);
  const [runIds, setRunIds] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string>();
  const [workspacePickerOpen, setWorkspacePickerOpen] = useState(false);
  const [workspacePath, setWorkspacePath] = useState<string>();
  const [mcpOpen, setMcpOpen] = useState(false);
  const [mcpMessage, setMcpMessage] = useState<string>();
  const { theme, cycleTheme } = useTheme();

  const conversationsQuery = useQuery({
    queryKey: conversationsKey,
    queryFn: () => api.listConversations({ archived: false }),
    refetchInterval: 15_000,
  });
  const conversations = conversationsQuery.data ?? [];
  const filteredConversations = useMemo(() => {
    const term = search.trim().toLocaleLowerCase("zh-CN");
    if (!term) return conversations;
    return conversations.filter((item) => `${item.title} ${item.last_message ?? ""}`.toLocaleLowerCase("zh-CN").includes(term));
  }, [conversations, search]);

  useEffect(() => {
    if (selectedId && conversations.some((item) => item.id === selectedId)) return;
    const first = conversations[0];
    if (first) setSelectedId(first.id);
    else if (!conversationsQuery.isLoading) setSelectedId(undefined);
  }, [conversations, conversationsQuery.isLoading, selectedId]);

  useEffect(() => {
    if (selectedId) localStorage.setItem("codeagent.conversation", selectedId);
    else localStorage.removeItem("codeagent.conversation");
  }, [selectedId]);

  const conversationQuery = useQuery({
    queryKey: ["conversations", selectedId],
    queryFn: () => api.getConversation(selectedId!),
    enabled: Boolean(selectedId),
  });
  const selectedConversation = conversationQuery.data ?? conversations.find((item) => item.id === selectedId);
  const taskListId = selectedConversation?.active_task_list_id ?? undefined;
  const messagesQuery = useQuery({
    queryKey: messagesKey(selectedId ?? ""),
    queryFn: () => api.listMessages(selectedId!),
    enabled: Boolean(selectedId),
  });
  const runtimeQuery = useQuery({ queryKey: ["runtime-config"], queryFn: api.getRuntimeConfig, staleTime: 60_000 });
  const teamEnabled = Boolean(runtimeQuery.data?.features?.agent_team);
  const teamsQuery = useQuery({
    queryKey: teamsKey(selectedId ?? ""),
    queryFn: () => api.listTeams(selectedId!),
    enabled: teamEnabled && Boolean(selectedId),
    refetchInterval: teamEnabled ? 5_000 : false,
  });
  const team = useMemo(() => {
    const teams = teamsQuery.data ?? [];
    const terminal = new Set(["completed", "failed", "cancelled", "closed_with_unmerged_candidates"]);
    return teams.find((item) => !terminal.has(item.team.state))
      ?? [...teams].sort((left, right) => right.team.updated_at.localeCompare(left.team.updated_at))[0];
  }, [teamsQuery.data]);
  const teamLeadActive = Boolean(team && !["completed", "failed", "cancelled", "closed_with_unmerged_candidates"].includes(team.team.state));

  useEffect(() => {
    setUseTeam(false);
  }, [selectedId]);
  const taskListQuery = useQuery({
    queryKey: ["task-lists", taskListId],
    queryFn: () => api.getTaskList(taskListId!),
    enabled: Boolean(taskListId),
  });
  const tasksQuery = useQuery({
    queryKey: tasksKey(taskListId ?? ""),
    queryFn: () => api.listTasks(taskListId!),
    enabled: Boolean(taskListId),
  });
  const activeRuntime = runtimeQuery.data
    ? { ...runtimeQuery.data, workspace: selectedConversation?.workspace ?? runtimeQuery.data.workspace }
    : runtimeQuery.data;
  const workspacesQuery = useQuery({
    queryKey: ["workspaces", workspacePath ?? "default"],
    queryFn: () => api.listWorkspaces(workspacePath),
    enabled: workspacePickerOpen,
    retry: false,
  });
  const mcpWorkspace = selectedConversation?.workspace ?? runtimeQuery.data?.workspace;
  const mcpQuery = useQuery({
    queryKey: ["mcp-servers", mcpWorkspace],
    queryFn: () => api.getMcpConfig(mcpWorkspace!),
    enabled: mcpOpen && Boolean(mcpWorkspace),
  });

  const runId = selectedId ? runIds[selectedId] ?? selectedConversation?.active_run_id ?? undefined : undefined;
  const liveRun = useRunEvents(runId);
  const ensureRun = useRunStore((state) => state.ensureRun);
  const setRunStatus = useRunStore((state) => state.setRunStatus);
  const resolveApproval = useRunStore((state) => state.resolveApproval);
  const pendingApproval = liveRun ? Object.values(liveRun.approvals).find((item) => item.status === "pending") : undefined;

  useEffect(() => {
    if (!taskListId) return;
    const source = new EventSource(api.taskEventStreamUrl(taskListId), { withCredentials: true });
    const refresh = () => {
      void queryClient.invalidateQueries({ queryKey: tasksKey(taskListId) });
      void queryClient.invalidateQueries({ queryKey: ["task-lists", taskListId] });
    };
    source.addEventListener("task.created", refresh);
    source.addEventListener("task.updated", refresh);
    source.addEventListener("task.completed", refresh);
    return () => source.close();
  }, [queryClient, taskListId]);

  useEffect(() => {
    if (!team?.team.id || !selectedId) return;
    const source = new EventSource(api.teamEventStreamUrl(team.team.id), { withCredentials: true });
    const refresh = () => void queryClient.invalidateQueries({ queryKey: teamsKey(selectedId) });
    const eventTypes = [
      "team.plan.created", "team.plan.submitted", "team.plan.approved", "team.plan.rejected",
      "team.attempt.assigned", "team.attempt.cancel_requested",
      "team.attempt_plan.submitted", "team.attempt_plan.approved", "team.attempt_plan.rejected",
      "team.candidate.submitted", "team.candidate.accepted", "team.candidate.rework",
      "team.candidate.user_approved", "team.candidate.user_rejected",
      "team.candidate.committed", "team.candidate.validation_failed", "team.analysis.completed", "team.integration.verified",
      "team.session.state_changed", "team.session.suspect", "team.scope_violation",
      "team.worktree.bound", "team.worktree.cleaned", "team.cancelled",
    ];
    eventTypes.forEach((type) => source.addEventListener(type, refresh));
    return () => source.close();
  }, [queryClient, selectedId, team?.team.id]);

  useEffect(() => {
    if (!runId) return;
    void api.getRun(runId).then((run) => {
      ensureRun(run.id, run.status, run.queue_position);
      setRunStatus(run.id, run.status, run.error ?? undefined);
    }).catch(() => undefined);
  }, [ensureRun, runId, setRunStatus]);

  useEffect(() => {
    if (!liveRun || isRunActive(liveRun.status)) return;
    void queryClient.invalidateQueries({ queryKey: conversationsKey });
    if (selectedId) void queryClient.invalidateQueries({ queryKey: messagesKey(selectedId) });
    if (taskListId) void queryClient.invalidateQueries({ queryKey: tasksKey(taskListId) });
  }, [liveRun?.status, liveRun?.runId, queryClient, selectedId, taskListId]);

  const createConversation = useMutation({
    mutationFn: (workspace: string) => api.createConversation({ workspace }),
    onSuccess: (conversation) => {
      queryClient.setQueryData<Conversation[]>(conversationsKey, (current = []) => [conversation, ...current.filter((item) => item.id !== conversation.id)]);
      setSelectedId(conversation.id);
      setLeftOpen(false);
      setDraft("");
      setWorkspacePickerOpen(false);
      setWorkspacePath(undefined);
    },
    onError: (error) => showError(error, setNotice),
  });

  const archiveConversation = useMutation({
    mutationFn: (conversation: Conversation) => api.updateConversation(conversation.id, { archived: true }),
    onSuccess: (_, conversation) => {
      queryClient.setQueryData<Conversation[]>(conversationsKey, (current = []) => current.filter((item) => item.id !== conversation.id));
      if (selectedId === conversation.id) setSelectedId(undefined);
    },
    onError: (error) => showError(error, setNotice),
  });

  const sendRun = useMutation({
    mutationFn: async ({ conversationId, content, useTeam }: { conversationId: string; content: string; useTeam: boolean }) => {
      const result = await api.createRun(conversationId, content, useTeam);
      return { ...result, content, conversationId };
    },
    onSuccess: (result) => {
      ensureRun(result.run_id, result.status, result.queue_position);
      setRunIds((current) => ({ ...current, [result.conversationId]: result.run_id }));
      setDraft("");
      setUseTeam(false);
      void queryClient.invalidateQueries({ queryKey: conversationsKey });
      window.setTimeout(() => void queryClient.invalidateQueries({ queryKey: messagesKey(result.conversationId) }), 150);
    },
    onError: (error) => showError(error, setNotice),
  });

  const cancelRun = useMutation({
    mutationFn: (targetRunId: string) => api.cancelRun(targetRunId),
    onMutate: (targetRunId) => setRunStatus(targetRunId, "cancelling"),
    onError: (error, targetRunId) => {
      void api.getRun(targetRunId).then((run) => setRunStatus(targetRunId, run.status, run.error ?? undefined));
      showError(error, setNotice);
    },
  });

  const decideApproval = useMutation({
    mutationFn: ({ targetRunId, approvalId, decision }: { targetRunId: string; approvalId: string; decision: ApprovalDecision }) => api.decideApproval(targetRunId, approvalId, decision),
    onMutate: ({ targetRunId, approvalId, decision }) => resolveApproval(targetRunId, approvalId, decision === "allow" ? "allowed" : "denied"),
    onError: (error, variables) => {
      resolveApproval(variables.targetRunId, variables.approvalId, "pending");
      showError(error, setNotice);
    },
  });

  const createTask = useMutation({
    mutationFn: (input: { subject: string; description: string; activeForm?: string }) => {
      if (!taskListId) throw new Error("当前会话没有任务列表");
      return api.createTask(taskListId, input);
    },
    onSuccess: (resource) => {
      queryClient.setQueryData<TaskResource[]>(tasksKey(resource.taskListId), (current = []) => [...current, resource]);
    },
    onError: (error) => showError(error, setNotice),
  });

  const teamCommand = useMutation({
    mutationFn: async (command: TeamCommand) => {
      if (!team) throw new Error("当前会话没有 TeamRun");
      const teamId = team.team.id;
      switch (command.kind) {
        case "team-plan":
          await api.decideTeamPlan(teamId, command.revision, command.decision, command.reason);
          return;
        case "candidate-approval":
          await api.approveCandidate(teamId, command.candidateId, command.decision, command.reason);
          return;
        case "resume-attempt":
          await api.resumeAttempt(teamId, command.attemptId, command.reason, command.acknowledgeUnknownResult);
          return;
        case "cancel":
          await api.cancelTeam(teamId, command.reason);
          return;
        case "integration":
          await api.verifyManualIntegration(teamId, command.targetRef);
          return;
        case "cleanup":
          await api.disposeWorktree(teamId, command.worktreeId, "cleanup");
          return;
      }
    },
    onSuccess: () => {
      if (selectedId) void queryClient.invalidateQueries({ queryKey: teamsKey(selectedId) });
      if (taskListId) void queryClient.invalidateQueries({ queryKey: tasksKey(taskListId) });
    },
    onError: (error) => showError(error, setNotice),
  });

  const saveMcpServer = useMutation({
    mutationFn: (server: SaveMcpServer) => api.saveMcpServer(server),
    onSuccess: (config) => {
      queryClient.setQueryData<McpConfig>(["mcp-servers", config.workspace], config);
      setMcpMessage(config.restart_required ? "配置已保存。当前有任务占用运行时，请重启 CodeAgent 后使用。" : "配置已保存，下一条消息会自动加载新工具。");
    },
  });

  const deleteMcpServer = useMutation({
    mutationFn: ({ workspace, name }: { workspace: string; name: string }) => api.deleteMcpServer(workspace, name),
    onSuccess: (config) => {
      queryClient.setQueryData<McpConfig>(["mcp-servers", config.workspace], config);
      setMcpMessage(config.restart_required ? "配置已删除。当前有任务占用运行时，请重启 CodeAgent。" : "配置已删除，运行时缓存已刷新。");
    },
  });

  const continueTask = (task: TaskResource) => {
    if (!selectedId || liveRun && isRunActive(liveRun.status)) return;
    sendRun.mutate({
      conversationId: selectedId,
      content: `继续处理 Task #${task.task.id}：${task.task.subject}。先读取 TaskGet，按 description 的完成条件执行，并及时用 TaskUpdate 更新状态。`,
      useTeam: false,
    });
  };

  const send = () => {
    const content = draft.trim();
    if (!selectedId || !content || liveRun && isRunActive(liveRun.status)) return;
    const optimistic: Message = {
      id: `optimistic:${Date.now()}`,
      conversation_id: selectedId,
      role: "user",
      content,
      created_at: new Date().toISOString(),
      status: "complete",
    };
    queryClient.setQueryData<Message[]>(messagesKey(selectedId), (current = []) => [...current, optimistic]);
    sendRun.mutate({ conversationId: selectedId, content, useTeam });
  };

  const teamPanelProps = {
    teamEnabled,
    team,
    teamLoading: teamsQuery.isLoading,
    teamBusy: teamCommand.isPending,
    teamError: teamsQuery.error ? errorMessage(teamsQuery.error) : teamCommand.error ? errorMessage(teamCommand.error) : undefined,
    onTeamPlan: (revision: number, decision: "approve" | "reject", reason: string) => teamCommand.mutate({ kind: "team-plan", revision, decision, reason }),
    onCandidateApproval: (candidateId: string, decision: "approve" | "reject", reason: string) => teamCommand.mutate({ kind: "candidate-approval", candidateId, decision, reason }),
    onResumeAttempt: (attemptId: string, reason: string, acknowledgeUnknownResult: boolean) => teamCommand.mutate({ kind: "resume-attempt", attemptId, reason, acknowledgeUnknownResult }),
    onCancelTeam: (reason: string) => teamCommand.mutate({ kind: "cancel", reason }),
    onVerifyIntegration: (targetRef: string) => teamCommand.mutate({ kind: "integration", targetRef }),
    onCleanupWorktree: (worktreeId: string) => teamCommand.mutate({ kind: "cleanup", worktreeId }),
  };

  return (
    <div className="h-dvh min-h-[520px] overflow-hidden bg-canvas text-ink">
      <div className="grid h-full min-h-0 grid-cols-1 lg:grid-cols-[264px_minmax(0,1fr)] xl:grid-cols-[264px_minmax(0,1fr)_320px]">
        <div className="hidden min-h-0 lg:block">
          <ConversationSidebar conversations={filteredConversations} selectedId={selectedId} search={search} loading={conversationsQuery.isLoading} creating={createConversation.isPending} onSearch={setSearch} onSelect={setSelectedId} onCreate={() => setWorkspacePickerOpen(true)} onArchive={(conversation) => archiveConversation.mutate(conversation)} />
        </div>

        <div className="relative flex min-h-0 min-w-0 flex-col">
          <ChatWorkspace title={selectedConversation?.title} messages={messagesQuery.data ?? []} loading={Boolean(selectedId && messagesQuery.isLoading)} run={liveRun} draft={draft} sending={sendRun.isPending} cancelling={cancelRun.isPending} approval={pendingApproval} approvalBusy={decideApproval.isPending} runtimeModel={runtimeQuery.data?.model} teamAvailable={teamEnabled} useTeam={useTeam} teamLeadActive={teamLeadActive} workspace={selectedConversation?.workspace ?? runtimeQuery.data?.workspace} theme={theme} onDraft={setDraft} onUseTeam={setUseTeam} onSend={send} onCancel={() => runId && cancelRun.mutate(runId)} onApprovalDecision={(decision) => runId && pendingApproval && decideApproval.mutate({ targetRunId: runId, approvalId: pendingApproval.id, decision })} onOpenLeft={() => setLeftOpen(true)} onOpenRight={() => setRightOpen(true)} onOpenMcp={() => { setMcpMessage(undefined); setMcpOpen(true); }} onToggleTheme={cycleTheme} />
        </div>

        <div className="hidden min-h-0 xl:block"><InspectorPanel run={liveRun} runtime={activeRuntime} tasks={tasksQuery.data} tasksLoading={tasksQuery.isLoading} taskBusy={createTask.isPending || Boolean(liveRun && isRunActive(liveRun.status))} taskList={taskListQuery.data} onContinueTask={continueTask} onCreateTask={(input) => createTask.mutate(input)} {...teamPanelProps} /></div>
      </div>

      <Drawer open={leftOpen} side="left" onClose={() => setLeftOpen(false)}>
        <ConversationSidebar mobile conversations={filteredConversations} selectedId={selectedId} search={search} loading={conversationsQuery.isLoading} creating={createConversation.isPending} onSearch={setSearch} onSelect={(id) => { setSelectedId(id); setLeftOpen(false); }} onCreate={() => { setLeftOpen(false); setWorkspacePickerOpen(true); }} onArchive={(conversation) => archiveConversation.mutate(conversation)} onClose={() => setLeftOpen(false)} />
      </Drawer>
      <Drawer open={rightOpen} side="right" onClose={() => setRightOpen(false)} width="min(90vw, 360px)"><InspectorPanel mobile run={liveRun} runtime={activeRuntime} tasks={tasksQuery.data} tasksLoading={tasksQuery.isLoading} taskBusy={createTask.isPending || Boolean(liveRun && isRunActive(liveRun.status))} taskList={taskListQuery.data} onContinueTask={continueTask} onCreateTask={(input) => createTask.mutate(input)} onClose={() => setRightOpen(false)} {...teamPanelProps} /></Drawer>

      <WorkspacePicker
        open={workspacePickerOpen}
        initialPath={runtimeQuery.data?.workspace}
        loading={workspacesQuery.isFetching || createConversation.isPending}
        listing={workspacesQuery.data}
        error={workspacesQuery.error ? errorMessage(workspacesQuery.error) : undefined}
        onBrowse={(path) => setWorkspacePath(path)}
        onConfirm={(path) => createConversation.mutate(path)}
        onClose={() => { if (!createConversation.isPending) { setWorkspacePickerOpen(false); setWorkspacePath(undefined); } }}
      />

      <McpConfigModal
        open={mcpOpen}
        workspace={mcpWorkspace}
        config={mcpQuery.data}
        loading={mcpQuery.isLoading}
        saving={saveMcpServer.isPending}
        deleting={deleteMcpServer.isPending ? deleteMcpServer.variables?.name : undefined}
        error={mcpQuery.error ? errorMessage(mcpQuery.error) : saveMcpServer.error ? errorMessage(saveMcpServer.error) : deleteMcpServer.error ? errorMessage(deleteMcpServer.error) : undefined}
        message={mcpMessage}
        onSave={(server) => { setMcpMessage(undefined); saveMcpServer.mutate(server); }}
        onDelete={(name) => mcpWorkspace && deleteMcpServer.mutate({ workspace: mcpWorkspace, name })}
        onClose={() => { if (!saveMcpServer.isPending && !deleteMcpServer.isPending) setMcpOpen(false); }}
      />

      {notice && <div role="alert" className="fixed bottom-4 left-1/2 z-[70] flex max-w-[calc(100vw-2rem)] -translate-x-1/2 items-center gap-2 rounded-xl border border-danger/20 bg-surface px-3 py-2.5 text-xs text-danger shadow-panel"><AlertCircle className="size-4 shrink-0" /><span className="min-w-0">{notice}</span><button type="button" aria-label="关闭提示" onClick={() => setNotice(undefined)}><X className="size-3.5" /></button></div>}
    </div>
  );
}

function Drawer({ open, side, width = "min(88vw, 300px)", onClose, children }: { open: boolean; side: "left" | "right"; width?: string; onClose: () => void; children: React.ReactNode }) {
  return <div className={cx("fixed inset-0 z-50 transition lg:hidden", open ? "pointer-events-auto" : "pointer-events-none")} aria-hidden={!open}><button type="button" aria-label="关闭面板" onClick={onClose} className={cx("absolute inset-0 bg-black/45 backdrop-blur-[1px] transition-opacity", open ? "opacity-100" : "opacity-0")} /><div style={{ width }} className={cx("absolute inset-y-0 shadow-2xl transition-transform duration-200 motion-reduce:transition-none", side === "left" ? "left-0" : "right-0", open ? "translate-x-0" : side === "left" ? "-translate-x-full" : "translate-x-full")}>{children}</div></div>;
}

function useTheme() {
  const [theme, setTheme] = useState<ThemeMode>(() => {
    const stored = localStorage.getItem("codeagent.theme");
    return stored === "light" || stored === "dark" || stored === "system" ? stored : "system";
  });
  useEffect(() => {
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => document.documentElement.classList.toggle("dark", theme === "dark" || theme === "system" && query.matches);
    apply();
    query.addEventListener("change", apply);
    localStorage.setItem("codeagent.theme", theme);
    return () => query.removeEventListener("change", apply);
  }, [theme]);
  const cycleTheme = () => setTheme((value) => value === "system" ? "light" : value === "light" ? "dark" : "system");
  return { theme, cycleTheme };
}

function showError(error: unknown, setNotice: (message: string) => void) {
  setNotice(errorMessage(error));
}

function errorMessage(error: unknown) {
  return error instanceof ApiError ? error.message : error instanceof Error ? error.message : "操作失败，请稍后重试";
}
