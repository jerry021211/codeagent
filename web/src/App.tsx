import { useEffect, useMemo, useState } from "react";
import { useMutation, useMutationState, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, X } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { cx, isRunActive } from "@/lib/utils";
import type { ApprovalDecision, Conversation, ExecutionMode, McpConfig, Message, SaveMcpServer, TaskResource } from "@/types/api";
import { ConversationSidebar } from "@/components/ConversationSidebar";
import { ChatWorkspace } from "@/components/ChatWorkspace";
import { InspectorPanel } from "@/components/InspectorPanel";
import { WorkspacePicker } from "@/components/WorkspacePicker";
import { McpConfigModal } from "@/components/McpConfigModal";
import { useRunEvents } from "@/hooks/useRunEvents";
import { useConversationActivity } from "@/hooks/useConversationActivity";
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
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const draft = selectedId ? drafts[selectedId] ?? "" : "";
  const setDraft = (value: string) => {
    if (selectedId) setDrafts((current) => ({ ...current, [selectedId]: value }));
  };
  const sendingConversations = useMutationState({
    filters: { mutationKey: ["send-run"], status: "pending" },
    select: (mutation) => (mutation.state.variables as { conversationId: string }).conversationId,
  });
  const cancellingRuns = useMutationState({
    filters: { mutationKey: ["cancel-run"], status: "pending" },
    select: (mutation) => mutation.state.variables as string,
  });
  const decidingRuns = useMutationState({
    filters: { mutationKey: ["decide-approval"], status: "pending" },
    select: (mutation) => (mutation.state.variables as { targetRunId: string }).targetRunId,
  });
  const sending = Boolean(selectedId && sendingConversations.includes(selectedId));
  const [modes, setModes] = useState<Record<string, ExecutionMode>>({});
  const [leftOpen, setLeftOpen] = useState(false);
  const [rightOpen, setRightOpen] = useState(false);
  const [runIds, setRunIds] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string>();
  const [workspacePickerOpen, setWorkspacePickerOpen] = useState(false);
  const [workspacePath, setWorkspacePath] = useState<string>();
  const [workspaceSearch, setWorkspaceSearch] = useState("");
  const [mcpOpen, setMcpOpen] = useState(false);
  const [mcpMessage, setMcpMessage] = useState<string>();
  const { theme, cycleTheme } = useTheme();

  const conversationsQuery = useQuery({
    queryKey: conversationsKey,
    queryFn: () => api.listConversations({ archived: false }),
    refetchInterval: (query) => query.state.data?.some((item) => isRunActive(item.run_status)) ? 2_000 : 15_000,
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
  const selectedConversation = conversations.find((item) => item.id === selectedId) ?? conversationQuery.data;
  const taskListId = selectedConversation?.active_task_list_id ?? undefined;
  const messagesQuery = useQuery({
    queryKey: messagesKey(selectedId ?? ""),
    queryFn: () => api.listMessages(selectedId!),
    enabled: Boolean(selectedId),
  });
  const runtimeQuery = useQuery({ queryKey: ["runtime-config"], queryFn: api.getRuntimeConfig, staleTime: 60_000 });
  const lastUserMessage = [...(messagesQuery.data ?? [])].reverse().find((message) => message.role === "user");
  const mode: ExecutionMode = modes[selectedId ?? ""] ?? lastUserMessage?.metadata?.mode ?? "normal";
  const selectMode = (value: ExecutionMode) => {
    if (selectedId) setModes((current) => ({ ...current, [selectedId]: value }));
  };
  const toggleDiscuss = () => {
    if (selectedId) setModes((current) => ({ ...current, [selectedId]: mode === "discuss" ? "normal" : "discuss" }));
  };
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
    queryKey: ["workspaces", workspacePath ?? "default", workspaceSearch],
    queryFn: () => api.listWorkspaces(workspacePath, workspaceSearch),
    enabled: workspacePickerOpen,
    retry: false,
  });
  const mcpWorkspace = selectedConversation?.workspace ?? runtimeQuery.data?.workspace;
  const mcpQuery = useQuery({
    queryKey: ["mcp-servers", mcpWorkspace],
    queryFn: () => api.getMcpConfig(mcpWorkspace!),
    enabled: mcpOpen && Boolean(mcpWorkspace),
  });

  const runId = selectedId ? selectedConversation?.active_run_id ?? runIds[selectedId] ?? selectedConversation?.latest_run_id ?? undefined : undefined;
  const liveRun = useRunEvents(runId);
  const activityQuery = useConversationActivity(selectedId ?? undefined, messagesQuery.data ?? [], liveRun);
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
    let disposed = false;
    const initialSeq = useRunStore.getState().runs[runId]?.lastSeq ?? 0;
    void api.getRun(runId).then((run) => {
      if (disposed) return;
      ensureRun(run.id, run.status, run.queue_position);
      const current = useRunStore.getState().runs[run.id];
      // An HTTP snapshot must not overwrite newer progress/terminal SSE events.
      if (current && current.lastSeq === initialSeq && current.connection !== "closed") {
        setRunStatus(run.id, run.status, run.error ?? undefined);
      }
    }).catch(() => undefined);
    return () => { disposed = true; };
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
      setWorkspacePickerOpen(false);
      setWorkspacePath(undefined);
      setWorkspaceSearch("");
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

  const deleteConversation = useMutation({
    mutationFn: (conversation: Conversation) => api.deleteConversation(conversation.id),
    onSuccess: async (_, conversation) => {
      await queryClient.cancelQueries({ queryKey: conversationsKey });
      await queryClient.cancelQueries({ queryKey: teamsKey(conversation.id) });
      queryClient.setQueryData<Conversation[]>(conversationsKey, (current = []) => current.filter((item) => item.id !== conversation.id));
      setSelectedId((current) => current === conversation.id ? undefined : current);
      queryClient.removeQueries({ queryKey: ["conversations", conversation.id] });
      queryClient.removeQueries({ queryKey: teamsKey(conversation.id) });
      for (const setter of [setDrafts, setRunIds]) {
        setter((current) => {
          const next = { ...current };
          delete next[conversation.id];
          return next;
        });
      }
      setModes((current) => {
        const next = { ...current };
        delete next[conversation.id];
        return next;
      });
    },
    onError: (error) => showError(error, setNotice),
  });
  const confirmDeleteConversation = (conversation: Conversation) => {
    if (sendingConversations.includes(conversation.id)) {
      setNotice("消息正在发送，请等待任务结束后再删除会话。");
      return;
    }
    if (window.confirm(`确定永久删除会话「${conversation.title || "新会话"}」？\n聊天记录和运行记录将被删除，无法恢复。工作区文件会保留。`)) {
      deleteConversation.mutate(conversation);
    }
  };

  const sendRun = useMutation({
    mutationKey: ["send-run"],
    mutationFn: async ({ conversationId, content, mode: submittedMode }: { conversationId: string; content: string; mode: ExecutionMode }) => {
      const result = await api.createRun(conversationId, content, false, submittedMode);
      return { ...result, content, conversationId };
    },
    onSuccess: (result) => {
      ensureRun(result.run_id, result.status, result.queue_position);
      setRunIds((current) => ({ ...current, [result.conversationId]: result.run_id }));
      setDrafts((current) => current[result.conversationId]?.trim() === result.content
        ? { ...current, [result.conversationId]: "" } : current);
      void queryClient.invalidateQueries({ queryKey: conversationsKey });
    },
    onError: (error) => showError(error, setNotice),
    onSettled: (_, __, variables) => {
      void queryClient.invalidateQueries({ queryKey: messagesKey(variables.conversationId) });
    },
  });

  const cancelRun = useMutation({
    mutationKey: ["cancel-run"],
    mutationFn: (targetRunId: string) => api.cancelRun(targetRunId),
    onMutate: (targetRunId) => setRunStatus(targetRunId, "cancelling"),
    onError: (error, targetRunId) => {
      void api.getRun(targetRunId).then((run) => setRunStatus(targetRunId, run.status, run.error ?? undefined));
      showError(error, setNotice);
    },
  });

  const decideApproval = useMutation({
    mutationKey: ["decide-approval"],
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
      setMcpMessage(config.restart_required ? "配置已保存。请重启 CodeAgent 后使用。" : "配置已保存，后续任务会加载新工具；当前任务继续使用原连接。");
    },
  });

  const deleteMcpServer = useMutation({
    mutationFn: ({ workspace, name }: { workspace: string; name: string }) => api.deleteMcpServer(workspace, name),
    onSuccess: (config) => {
      queryClient.setQueryData<McpConfig>(["mcp-servers", config.workspace], config);
      setMcpMessage(config.restart_required ? "配置已删除。请重启 CodeAgent。" : "配置已删除，后续任务使用新配置；当前任务继续使用原连接。");
    },
  });

  const continueTask = (task: TaskResource) => {
    if (mode === "discuss") {
      setNotice("请先切回 Code · 编码，再执行任务。");
      return;
    }
    if (!selectedId || sending || liveRun && isRunActive(liveRun.status)) return;
    sendRun.mutate({
      conversationId: selectedId,
      content: `继续处理 Task #${task.task.id}：${task.task.subject}。先读取 TaskGet，按 description 的完成条件执行，并及时用 TaskUpdate 更新状态。`,
      mode,
    });
  };

  const send = () => {
    const content = draft.trim();
    if (sending || messagesQuery.isLoading) return;
    if (!selectedId || !content || liveRun && isRunActive(liveRun.status)) return;
    if (content.toLowerCase() === "/discuss" && !teamLeadActive) {
      toggleDiscuss();
      setDraft("");
      return;
    }
    const optimistic: Message = {
      id: `optimistic:${Date.now()}`,
      conversation_id: selectedId,
      role: "user",
      content,
      created_at: new Date().toISOString(),
      status: "complete",
      metadata: { mode },
    };
    queryClient.setQueryData<Message[]>(messagesKey(selectedId), (current = []) => [...current, optimistic]);
    sendRun.mutate({ conversationId: selectedId, content, mode });
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
          <ConversationSidebar conversations={filteredConversations} selectedId={selectedId} search={search} loading={conversationsQuery.isLoading} creating={createConversation.isPending} onSearch={setSearch} onSelect={setSelectedId} onCreate={() => setWorkspacePickerOpen(true)} onArchive={(conversation) => archiveConversation.mutate(conversation)} onDelete={confirmDeleteConversation} deletingId={deleteConversation.isPending ? deleteConversation.variables.id : undefined} />
        </div>

        <div className="relative flex min-h-0 min-w-0 flex-col">
          <ChatWorkspace historyRuns={activityQuery.data} historyLoading={activityQuery.isFetching} historyError={activityQuery.isError} discussMode={mode === "discuss"} onModeChange={selectMode} title={selectedConversation?.title} messages={messagesQuery.data ?? []} loading={Boolean(selectedId && messagesQuery.isLoading)} run={liveRun} draft={draft} sending={sending} cancelling={Boolean(runId && cancellingRuns.includes(runId))} approval={pendingApproval} approvalBusy={Boolean(runId && decidingRuns.includes(runId))} runtimeModel={runtimeQuery.data?.model} teamLeadActive={teamLeadActive} workspace={selectedConversation?.workspace ?? runtimeQuery.data?.workspace} theme={theme} onDraft={setDraft} onSend={send} onCancel={() => runId && cancelRun.mutate(runId)} onApprovalDecision={(decision) => runId && pendingApproval && decideApproval.mutate({ targetRunId: runId, approvalId: pendingApproval.id, decision })} onOpenLeft={() => setLeftOpen(true)} onOpenRight={() => setRightOpen(true)} onOpenMcp={() => { setMcpMessage(undefined); setMcpOpen(true); }} onToggleTheme={cycleTheme} />
        </div>

        <div className="hidden min-h-0 xl:block"><InspectorPanel run={liveRun} runtime={activeRuntime} tasks={tasksQuery.data} tasksLoading={tasksQuery.isLoading} taskBusy={createTask.isPending || Boolean(liveRun && isRunActive(liveRun.status))} taskList={taskListQuery.data} onContinueTask={continueTask} onCreateTask={(input) => createTask.mutate(input)} {...teamPanelProps} /></div>
      </div>

      <Drawer open={leftOpen} side="left" onClose={() => setLeftOpen(false)}>
        <ConversationSidebar mobile conversations={filteredConversations} selectedId={selectedId} search={search} loading={conversationsQuery.isLoading} creating={createConversation.isPending} onSearch={setSearch} onSelect={(id) => { setSelectedId(id); setLeftOpen(false); }} onCreate={() => { setLeftOpen(false); setWorkspacePickerOpen(true); }} onArchive={(conversation) => archiveConversation.mutate(conversation)} onDelete={confirmDeleteConversation} deletingId={deleteConversation.isPending ? deleteConversation.variables.id : undefined} onClose={() => setLeftOpen(false)} />
      </Drawer>
      <Drawer open={rightOpen} side="right" onClose={() => setRightOpen(false)} width="min(90vw, 360px)"><InspectorPanel mobile run={liveRun} runtime={activeRuntime} tasks={tasksQuery.data} tasksLoading={tasksQuery.isLoading} taskBusy={createTask.isPending || Boolean(liveRun && isRunActive(liveRun.status))} taskList={taskListQuery.data} onContinueTask={continueTask} onCreateTask={(input) => createTask.mutate(input)} onClose={() => setRightOpen(false)} {...teamPanelProps} /></Drawer>

      <WorkspacePicker
        open={workspacePickerOpen}
        initialPath={runtimeQuery.data?.workspace}
        loading={workspacesQuery.isFetching || createConversation.isPending}
        listing={workspacesQuery.data}
        error={workspacesQuery.error ? errorMessage(workspacesQuery.error) : undefined}
        onBrowse={(path) => { setWorkspacePath(path); setWorkspaceSearch(""); }}
        onSearch={setWorkspaceSearch}
        onConfirm={(path) => createConversation.mutate(path)}
        onClose={() => { if (!createConversation.isPending) { setWorkspacePickerOpen(false); setWorkspacePath(undefined); setWorkspaceSearch(""); } }}
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
