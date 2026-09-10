import { useMemo, useState } from "react";
import { Bot, Check, CircleAlert, GitBranch, ShieldCheck, Users, X } from "lucide-react";
import type { TaskRecord, TeamRecovery, TeamSnapshot } from "@/types/api";
import { cx, formatNumber, formatTime, prettyJson, tokenTotal } from "@/lib/utils";
import { EmptyPanel, StatusDot } from "@/components/ui";
import { TeamObserver } from "@/components/TeamObserver";

type Props = {
  enabled: boolean;
  team?: TeamSnapshot;
  loading?: boolean;
  busy?: boolean;
  error?: string;
  onTeamPlan: (revision: number, decision: "approve" | "reject", reason: string) => void;
  onCandidateApproval: (candidateId: string, decision: "approve" | "reject", reason: string) => void;
  onResumeAttempt: (attemptId: string, reason: string, acknowledgeUnknownResult: boolean) => void;
  onCancel: (reason: string) => void;
  onVerifyIntegration: (targetRef: string) => void;
  onCleanupWorktree: (worktreeId: string) => void;
};

export function TeamPanel({
  enabled,
  team,
  loading,
  busy,
  error,
  onTeamPlan,
  onCandidateApproval,
  onResumeAttempt,
  onCancel,
  onVerifyIntegration,
  onCleanupWorktree,
}: Props) {
  const [reason, setReason] = useState("");
  const [observing, setObserving] = useState(false);
  const [targetRef, setTargetRef] = useState("HEAD");
  const pendingPlan = [...(team?.plans ?? [])].reverse().find((item) => item.status === "pending_user_approval");
  const questionWaits = (team?.sessions ?? []).filter((session) => session.state === "waiting" && (
    session.waiting_reason?.startsWith("waiting_for_lead_answer:") || session.waiting_reason === "team_plan_change_required"
  ));
  const submittedAttemptPlans = team?.attempt_plans.filter((item) => item.status === "submitted") ?? [];
  const reviewCandidates = team?.candidates.filter((item) => item.status === "submitted") ?? [];
  const highRiskApprovals = team?.candidates.filter((item) => item.status === "accepted" && item.user_approval_required && !item.user_decision) ?? [];
  const taskNames = useMemo(
    () => new Map((team?.tasks ?? []).map((item) => [item.task.id, item.task.subject])),
    [team?.tasks],
  );
  const agentNames = useMemo(
    () => new Map((team?.agents ?? []).map((item) => [item.id, item.name])),
    [team?.agents],
  );
  const worktreeByAttempt = useMemo(
    () => new Map((team?.worktrees ?? []).map((item) => [item.attempt_id, item])),
    [team?.worktrees],
  );
  const validationsByCandidate = useMemo(() => {
    const grouped = new Map<string, TeamSnapshot["validation_runs"]>();
    for (const validation of team?.validation_runs ?? []) {
      const items = grouped.get(validation.candidate_id) ?? [];
      items.push(validation);
      grouped.set(validation.candidate_id, items);
    }
    return grouped;
  }, [team?.validation_runs]);

  if (!enabled) {
    return <EmptyPanel icon={<Users className="size-5" />} title="Agent Team 未启用" body="设置 TEAM_RUNTIME_ENABLED 后，团队运行会在这里独立展示；现有单 Agent 不受影响。" />;
  }
  if (loading) return <div className="p-5 text-xs text-ink-muted">正在读取团队状态…</div>;
  if (!team) {
    return <EmptyPanel icon={<Users className="size-5" />} title="当前会话没有 TeamRun" body="第一阶段 TeamRun 由 Lead 提交方案后创建，不会自动修改或合并主分支。" />;
  }

  const requireReason = (action: () => void) => {
    if (!reason.trim()) return;
    action();
  };
  const hasFailure = team.sessions.some((item) => item.failure || ["failed", "lost"].includes(item.state))
    || team.attempts.some((item) => item.error || item.result_unknown);

  return (
    <div className="space-y-5 px-4 py-5">
      <button onClick={() => setObserving(true)} className="w-full rounded-xl border border-accent/30 bg-accent/5 px-3 py-3 text-left text-accent">
        <span className="block text-sm font-semibold">打开执行观察面板</span>
        <span className="mt-1 block text-[11px]">按时间查看流程、通信与数据库字段旧值 → 新值</span>
      </button>
      {observing && <TeamObserver key={team.team.id} team={team} onClose={() => setObserving(false)} />}
      <section className="rounded-2xl border border-line bg-surface-muted p-3.5">
        <div className="flex items-center gap-2">
          <StatusDot status={teamStatus(team.team.state)} pulse={["running", "waiting_approval"].includes(team.team.state)} />
          <span className="min-w-0 flex-1 truncate text-xs font-semibold text-ink">{team.team.state}</span>
          <span className="font-mono text-[9px] text-ink-faint">{team.team.id.slice(0, 10)}</span>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2 text-[9px] text-ink-muted">
          <Metric label="Base" value={team.team.base_commit.slice(0, 10)} />
          <Metric label="Plan" value={team.team.active_plan_revision ? `r${team.team.active_plan_revision}` : "未批准"} />
          <Metric label="Token" value={`${formatNumber(tokenTotal(team.usage))}${team.team.token_budget ? ` / ${formatNumber(team.team.token_budget)}` : ""}`} />
          <Metric label="Model calls" value={`${formatNumber(team.usage.model_calls ?? 0)}${team.team.model_call_budget ? ` / ${formatNumber(team.team.model_call_budget)}` : ""}`} />
        </div>
        <p className="mt-3 border-t border-line pt-3 text-[9px] leading-4 text-ink-muted">第一阶段仅生成候选提交；自动合并与自动冲突处理始终关闭。</p>
      </section>

      {(pendingPlan || highRiskApprovals.length > 0) && (
        <section>
          <Heading icon={<ShieldCheck className="size-3.5" />} title="需要用户决策" count={(pendingPlan ? 1 : 0) + highRiskApprovals.length} />
          <textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="填写审批、拒绝或返工理由（必填）"
            className="mb-2 min-h-16 w-full resize-y rounded-xl border border-line bg-surface px-3 py-2 text-[10px] text-ink outline-none focus:border-accent"
          />
          <div className="space-y-2">
            {pendingPlan && (
              <DecisionCard title={`Team Plan r${pendingPlan.revision}`} detail="用户审批后 Runtime 才能创建 Teammate、Attempt 与代码 Worktree。">
                {typeof pendingPlan.plan.shared_context === "string" && pendingPlan.plan.shared_context && (
                  <details className="mb-2 text-[10px] text-ink-muted"><summary className="cursor-pointer">公共约定（审批内容）</summary><p className="mt-1 whitespace-pre-wrap break-words">{pendingPlan.plan.shared_context}</p></details>
                )}
                <div className="mb-3 space-y-2">
                  {team.tasks.filter(({ task }) => !Array.isArray(pendingPlan.plan.tasks) || pendingPlan.plan.tasks.some((item) => item && typeof item === "object" && String(item.task_id) === task.id)).map(({ task }) => <TaskDetails key={task.id} task={task} />)}
                </div>
                <DecisionButtons busy={busy} disabled={!reason.trim()} positive="批准" negative="拒绝" onPositive={() => requireReason(() => onTeamPlan(pendingPlan.revision, "approve", reason.trim()))} onNegative={() => requireReason(() => onTeamPlan(pendingPlan.revision, "reject", reason.trim()))} />
              </DecisionCard>
            )}
            {highRiskApprovals.map((candidate) => (
              <DecisionCard key={candidate.id} title={`高风险 Candidate · Task #${candidate.task_id}`} detail={candidate.summary}>
                <div className="mb-2 text-[9px] text-ink-faint">{candidate.changed_files.length} 个变更文件{candidate.known_risks.length ? ` · ${candidate.known_risks.length} 项已知风险` : ""}</div>
                <DecisionButtons busy={busy} disabled={!reason.trim()} positive="批准 Runtime 验证" negative="拒绝并返工" onPositive={() => requireReason(() => onCandidateApproval(candidate.id, "approve", reason.trim()))} onNegative={() => requireReason(() => onCandidateApproval(candidate.id, "reject", reason.trim()))} />
              </DecisionCard>
            ))}
          </div>
        </section>
      )}

      {questionWaits.length > 0 && (
        <section>
          <Heading icon={<Bot className="size-3.5" />} title="协调与阻塞" count={questionWaits.length} />
          <div className="space-y-2">
            {questionWaits.map((session) => {
              const attempt = team.attempts.find((item) => item.id === session.current_attempt_id);
              const planChange = session.waiting_reason === "team_plan_change_required";
              const questionId = planChange ? String(attempt?.error?.question_id ?? "") : session.waiting_reason?.split(":")[1];
              const question = team.messages.find((item) => item.id === questionId);
              const answer = team.messages.find((item) => item.type === "ANSWER" && item.correlation_id === questionId);
              return <div key={session.id} className="rounded-xl border border-warning/30 bg-warning/5 p-3 text-[10px]">
                <div className="font-semibold text-ink">{agentNames.get(session.agent_id)} · Task #{attempt?.task_id}</div>
                <div className="mt-1 text-warning">{planChange ? "需要用户决定如何调整方案" : "等待 Lead 回答（不是等待用户审批）"}</div>
                <p className="mt-2 whitespace-pre-wrap break-words text-ink-muted">{String(question?.payload?.question ?? "问题正文见执行观察面板中的 QUESTION 记录。")}</p>
                {answer && <p className="mt-2 whitespace-pre-wrap break-words text-ink-muted">Lead：{String(answer.payload?.answer ?? "")}</p>}
                <p className="mt-2 text-ink-faint">{planChange ? "现场与租约保留、写权限关闭。请向 Root/Lead 说明下一步；普通回答或恢复按钮不能改变批准范围。不会自动重建或取消整个 Team。" : "回答持久化后，Runtime 在安全停顿点继续同一任务，保留原上下文。安全冻结不会被普通回答解除。"}</p>
              </div>;
            })}
          </div>
        </section>
      )}

      {(team.recoveries ?? []).length > 0 && (
        <section>
          <Heading icon={<CircleAlert className="size-3.5" />} title="需要人工恢复" count={team.recoveries.length} />
          <div className="space-y-2">
            {team.recoveries.map((recovery) => (
              <RecoveryCard
                key={recovery.attempt_id}
                recovery={recovery}
                taskName={taskNames.get(recovery.task_id)}
                teammateName={agentNames.get(recovery.agent_id)}
                busy={busy}
                onResume={onResumeAttempt}
              />
            ))}
          </div>
        </section>
      )}

      <section>
        <Heading icon={<ShieldCheck className="size-3.5" />} title="Team Plan 历史" count={team.plans.length} />
        <div className="space-y-1.5">
          {[...team.plans].reverse().map((plan) => (
            <div key={plan.revision} className="rounded-xl border border-line px-3 py-2.5 text-[9px]">
              <div className="flex items-center gap-2"><span className="font-semibold text-ink">r{plan.revision}</span><span className="text-ink-muted">{plan.status}</span></div>
              {plan.decision_reason && <div className="mt-1 leading-4 text-ink-muted">{plan.decision_reason}</div>}
            </div>
          ))}
        </div>
      </section>

      {(submittedAttemptPlans.length > 0 || reviewCandidates.length > 0) && (
        <section>
          <Heading icon={<Bot className="size-3.5" />} title="Root / Lead 审查队列" count={submittedAttemptPlans.length + reviewCandidates.length} />
          <div className="space-y-2 text-[9px] text-ink-muted">
            {submittedAttemptPlans.map((plan) => <div key={plan.id} className="rounded-xl border border-line p-3">Attempt Plan p{plan.revision} · {plan.summary}<div className="mt-1 text-ink-faint">等待 Root/Lead 自动审查</div></div>)}
            {reviewCandidates.map((candidate) => <div key={candidate.id} className="rounded-xl border border-line p-3">Candidate · Task #{candidate.task_id} · {candidate.summary}<div className="mt-1 text-ink-faint">等待 Root/Lead 语义审查</div></div>)}
          </div>
        </section>
      )}

      <section>
        <Heading icon={<Bot className="size-3.5" />} title="Agent Sessions" count={team.sessions.length} />
        <div className="space-y-1.5">
          {team.sessions.map((session) => (
            <div key={session.id} className="rounded-xl border border-line px-3 py-2.5">
              <div className="flex items-center gap-2 text-[10px]">
                <StatusDot status={sessionStatus(session.state)} pulse={session.state === "work"} />
                <span className="min-w-0 flex-1 truncate font-medium text-ink">{team.agents.find((item) => item.id === session.agent_id)?.name ?? session.agent_id}</span>
                <span className="font-mono text-ink-faint">g{session.generation}</span>
              </div>
              <div className="mt-1 text-[9px] text-ink-muted">{session.state} · heartbeat {formatTime(session.heartbeat_at)}</div>
              {session.waiting_reason && <div className="mt-1 text-[9px] text-warning">{activityLabel(session.waiting_reason)}</div>}
            </div>
          ))}
        </div>
      </section>

      <section>
        <Heading icon={<GitBranch className="size-3.5" />} title="任务与调度" count={team.scheduling.length} />
        <div className="space-y-1.5">
          {team.scheduling.map((item) => (
            <div key={item.task_id} className="rounded-xl border border-line px-3 py-2.5 text-[10px]">
              <div className="flex items-center gap-2"><StatusDot status={item.schedulable ? "success" : "idle"} /><span className="min-w-0 flex-1 truncate text-ink">#{item.task_id} {taskNames.get(item.task_id)}</span></div>
              {team.tasks.filter(({ task }) => task.id === item.task_id).map(({ task }) => <TaskDetails key={task.id} task={task} />)}
              {!item.schedulable && <div className="mt-1.5 text-[9px] leading-4 text-ink-muted">{item.reasons.map(schedulingLabel).join("；") || "等待调度条件"}</div>}
            </div>
          ))}
        </div>
      </section>

      <section>
        <Heading icon={<GitBranch className="size-3.5" />} title="候选提交与 Worktree" count={team.candidates.length} />
        <div className="space-y-2">
          {team.candidates.map((candidate) => (
            <div key={candidate.id} className="rounded-xl border border-line p-3 text-[9px]">
              <div className="flex items-center gap-2"><StatusDot status={candidate.integrated_at ? "success" : candidate.status === "validation_failed" ? "error" : "idle"} /><span className="min-w-0 flex-1 truncate text-[10px] font-medium text-ink">Task #{candidate.task_id} · {candidate.status}</span></div>
              {worktreeByAttempt.get(candidate.attempt_id)?.branch && <code className="mt-1.5 block truncate text-ink-muted">{worktreeByAttempt.get(candidate.attempt_id)?.branch}</code>}
              {candidate.commit_hash && <code className="mt-1.5 block truncate text-ink-muted">{candidate.commit_hash}</code>}
              <div className="mt-1 leading-4 text-ink-muted">文件：{candidate.changed_files.join(", ") || "无"}</div>
              <div className="mt-1 leading-4 text-ink-muted">Teammate 测试：{candidate.tests_reported.join("；") || "未报告"}</div>
              {(validationsByCandidate.get(candidate.id) ?? []).map((validation) => <div key={validation.id} className={cx("mt-1 leading-4", validation.status === "succeeded" ? "text-success" : validation.status === "failed" ? "text-danger" : "text-ink-muted")}>Runtime 验证：{validation.command} · {validation.status}{validation.exit_code != null ? ` (${validation.exit_code})` : ""}</div>)}
              {candidate.integrated_at && <div className="mt-1 text-success">已人工集成并核验</div>}
            </div>
          ))}
          {team.worktrees.map((worktree) => (
            <div key={worktree.id} className="rounded-xl border border-line p-3 text-[9px]">
              <div className="flex items-center gap-2"><span className="min-w-0 flex-1 truncate font-mono text-ink">{worktree.branch}</span><span className="text-ink-muted">{worktree.state}</span></div>
              <div className="mt-1 truncate text-ink-faint" title={worktree.path}>{worktree.path}</div>
              <div className="mt-1 text-ink-muted">写权限：{worktree.write_enabled ? "限定开放" : "关闭"} · {worktree.write_scopes.join(", ") || "仓库级租约"}</div>
              {worktree.frozen_reason && <div className="mt-1 text-warning">{worktree.frozen_reason}</div>}
              {worktree.state === "retained" && <button type="button" disabled={busy} onClick={() => { if (window.confirm("确认清理已保留的 Worktree 目录？候选分支和提交会保留。")) onCleanupWorktree(worktree.id); }} className="mt-2 rounded-lg border border-danger/30 px-2 py-1 text-danger disabled:opacity-50">用户确认清理目录</button>}
            </div>
          ))}
        </div>
      </section>

      {team.manual_integration.commands.length > 0 && (
        <section>
          <Heading icon={<Check className="size-3.5" />} title="人工集成" count={team.manual_integration.commands.length} />
          <div className="space-y-1.5">{team.manual_integration.commands.map((command) => <code key={command} className="block overflow-x-auto rounded-lg bg-code px-2.5 py-2 text-[9px] text-code-ink">{command}</code>)}</div>
          <div className="mt-2 flex gap-2"><input value={targetRef} onChange={(event) => setTargetRef(event.target.value)} className="min-w-0 flex-1 rounded-lg border border-line bg-surface px-2 py-1.5 font-mono text-[9px] text-ink outline-none" placeholder="已集成目标 ref" /><button type="button" disabled={busy || !targetRef.trim()} onClick={() => onVerifyIntegration(targetRef.trim())} className="rounded-lg bg-accent px-2.5 py-1.5 text-[9px] font-semibold text-white disabled:opacity-50">只读核验</button></div>
        </section>
      )}

      {(error || hasFailure) && <div className="rounded-xl border border-danger/20 bg-danger/5 p-3 text-[9px] text-danger"><div className="mb-1 flex items-center gap-2 font-semibold"><CircleAlert className="size-3.5" />异常与未知结果</div>{error || prettyJson({ sessions: team.sessions.filter((item) => item.failure), attempts: team.attempts.filter((item) => item.error || item.result_unknown) })}</div>}

      {! ["completed", "failed", "cancelled", "closed_with_unmerged_candidates"].includes(team.team.state) && (
        <div className="space-y-2">
          <input aria-label="取消 TeamRun 的理由" placeholder="需要取消时，填写取消理由" value={reason} onChange={(event) => setReason(event.target.value)} className="w-full rounded-lg border border-line bg-surface px-3 py-2 text-[10px]" />
          <button type="button" disabled={busy || !reason.trim()} onClick={() => { if (window.confirm("确认取消整个 TeamRun？Runtime 会先撤销权限并等待 worker 停止。")) onCancel(reason.trim()); }} className="flex w-full items-center justify-center gap-2 rounded-xl border border-danger/30 px-3 py-2 text-[10px] text-danger disabled:opacity-40"><X className="size-3.5" />取消 TeamRun</button>
        </div>
      )}
    </div>
  );
}

function Heading({ icon, title, count }: { icon: React.ReactNode; title: string; count?: number }) {
  return <div className="mb-2.5 flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.13em] text-ink-muted">{icon}<span>{title}</span>{count != null && <span className="ml-auto rounded-full bg-surface-strong px-1.5 py-0.5 font-mono text-[8px] text-ink-faint">{count}</span>}</div>;
}

function TaskDetails({ task }: { task: TaskRecord }) {
  const kind = String(task.metadata.kind ?? "analysis");
  return <details className="mt-2 text-[10px] leading-5">
    <summary className="cursor-pointer text-accent">#{task.id} · {kind === "analysis" ? "只读分析报告" : kind === "code" ? "文件修改（含文档）" : `未知类型：${kind}`} · 查看目标与要求</summary>
    <p className="mt-1 font-medium text-ink">{task.subject}</p>
    <p className="whitespace-pre-wrap break-words text-ink-muted">{task.description}</p>
    <div className="mt-2 text-ink-muted">写入范围：{textList(task.metadata.write_scopes) || (kind === "analysis" ? "无，禁止写入" : "仓库级独占写租约")}</div>
    <div className="text-ink-muted">风险：{String(task.metadata.risk_level ?? "low")} · 另需执行方案审批：{task.metadata.plan_required || task.metadata.risk_level === "high" ? "是" : "否"}</div>
    {textList(task.metadata.acceptance_criteria) && <div className="whitespace-pre-wrap text-ink-muted">验收要求：{textList(task.metadata.acceptance_criteria)}</div>}
    <div className="break-words text-ink-muted">验证命令：{textList(task.metadata.validation_commands) || "未声明"}</div>
    <div className="text-ink-muted">前置任务：{task.blockedBy.map((id) => `#${id}`).join("、") || "无"}</div>
  </details>;
}

function textList(value: unknown): string {
  return Array.isArray(value) ? value.map(String).join("；") : "";
}

function schedulingLabel(value: string): string {
  const labels: Record<string, string> = {
    task_configuration_conflict: "任务类型与执行配置冲突，需要修订方案",
    team_plan_not_approved: "等待用户批准 Team Plan",
    dependency_not_completed: "等待前置任务完成",
    candidate_not_integrated: "等待用户人工集成前置候选提交",
    no_idle_teammate: "暂无空闲成员",
    resource_conflict: "写入范围或资源租约已被占用",
    team_concurrency_exhausted: "已达到并行任务上限",
    token_budget_exhausted: "Token 预算已用尽",
    model_call_budget_exhausted: "模型调用预算已用尽",
    "task_status:in_progress": "该任务已有执行中的 Attempt",
    "team_status:waiting_approval": "团队等待审批",
  };
  return labels[value] ?? value;
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div><div className="text-ink-faint">{label}</div><div className="mt-0.5 truncate font-mono text-ink">{value}</div></div>;
}

function DecisionCard({ title, detail, children }: { title: string; detail: string; children: React.ReactNode }) {
  return <div className="rounded-xl border border-warning/25 bg-warning/5 p-3"><div className="text-[10px] font-semibold text-ink">{title}</div><p className="my-1.5 text-[9px] leading-4 text-ink-muted">{detail}</p>{children}</div>;
}

function DecisionButtons({ busy, disabled, positive, negative, onPositive, onNegative }: { busy?: boolean; disabled?: boolean; positive: string; negative: string; onPositive: () => void; onNegative: () => void }) {
  return <div className="flex gap-2"><button type="button" disabled={busy || disabled} onClick={onPositive} className="rounded-lg bg-accent px-2.5 py-1.5 text-[9px] font-semibold text-white disabled:opacity-40">{positive}</button><button type="button" disabled={busy || disabled} onClick={onNegative} className="rounded-lg border border-line px-2.5 py-1.5 text-[9px] text-ink-muted disabled:opacity-40">{negative}</button></div>;
}

function RecoveryCard({ recovery, taskName, teammateName, busy, onResume }: { recovery: TeamRecovery; taskName?: string; teammateName?: string; busy?: boolean; onResume: (attemptId: string, reason: string, acknowledgeUnknownResult: boolean) => void }) {
  const [reason, setReason] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const ready = recovery.recoverable && reason.trim() && (!recovery.result_unknown || acknowledged);
  return (
    <div className="rounded-xl border border-warning/30 bg-warning/5 p-3 text-[9px]">
      <div className="flex items-start gap-2">
        <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-warning" />
        <div className="min-w-0 flex-1">
          <div className="text-[10px] font-semibold text-ink">Task #{recovery.task_id}{taskName ? ` · ${taskName}` : ""}</div>
          <div className="mt-1 text-ink-faint">Teammate：{teammateName || recovery.agent_id}</div>
          <div className="mt-1 leading-4 text-ink-muted">{recovery.summary}</div>
        </div>
      </div>
      <div className="mt-2 space-y-1 leading-4 text-ink-muted">
        <div>工具：{recovery.tool_name || "未记录"} · {recovery.tool_executed ? "可能已执行" : "未执行或未确认执行"}</div>
        {recovery.allowed_scopes.length > 0 && <div>允许范围：{recovery.allowed_scopes.join(", ")}</div>}
        {recovery.outside_paths.length > 0 && <div className="text-warning">越界文件：{recovery.outside_paths.join(", ")}</div>}
        {recovery.worktree_path && <div className="break-all font-mono text-ink-faint">Worktree：{recovery.worktree_path}</div>}
        {recovery.blocking_checks.length > 0 && <div>待确认：{recovery.blocking_checks.map(recoveryCheckLabel).join("；")}</div>}
        <div>恢复时Runtime会重新检查Plan、租约、Worktree绑定和实际Diff。</div>
      </div>
      <textarea value={reason} onChange={(event) => setReason(event.target.value)} placeholder="填写已完成的人工检查或处理说明（必填）" className="mt-2 min-h-14 w-full resize-y rounded-lg border border-line bg-surface px-2.5 py-2 text-[9px] text-ink outline-none focus:border-accent" />
      {recovery.result_unknown && (
        <label className="mt-2 flex items-start gap-2 leading-4 text-warning">
          <input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} className="mt-0.5" />
          <span>我已检查Worktree现状，并理解Runtime不会自动重放上一次写操作。</span>
        </label>
      )}
      {!recovery.recoverable && <div className="mt-2 text-danger">该遗留记录当前不能直接恢复，请保留现场并取消或等待兼容恢复。</div>}
      <button type="button" disabled={busy || !ready} onClick={() => onResume(recovery.attempt_id, reason.trim(), acknowledged)} className="mt-2 rounded-lg bg-accent px-2.5 py-1.5 text-[9px] font-semibold text-white disabled:opacity-40">重新检查并继续</button>
    </div>
  );
}

function activityLabel(value: string): string {
  if (value.startsWith("waiting_for_lead_answer:")) return "等待 Lead 回答指定问题";
  return ({
    team_plan_change_required: "需要调整批准方案，等待用户指示",
    model_waiting: "等待模型响应（思考期间可能没有正文）",
    model_receiving: "正在接收模型响应",
    model_retry_wait: "等待有限重试",
    tool_executing: "正在执行工具",
    permission_waiting: "等待工具权限审批",
    executing: "正在处理本轮结果",
    model_response_timeout: "模型长时间没有有效响应，已请求停止",
    model_call_timeout: "本轮模型调用达到总时限，已请求停止",
    worker_heartbeat_timeout: "执行进度异常，正在确认 worker 是否停止",
  } as Record<string, string>)[value] ?? value;
}

function recoveryCheckLabel(value: string): string {
  const labels: Record<string, string> = {
    unknown_result_acknowledgement_required: "确认未知写结果",
    runtime_recheck_required: "等待Runtime重新校验现场",
  };
  return labels[value] ?? value;
}

function teamStatus(state: string): "running" | "warning" | "success" | "error" | "idle" {
  if (state === "running") return "running";
  if (["planning", "waiting_approval", "ready_for_manual_integration"].includes(state)) return "warning";
  if (state === "completed") return "success";
  if (["failed", "cancelled"].includes(state)) return "error";
  return "idle";
}

function sessionStatus(state: string): "running" | "warning" | "success" | "error" | "idle" {
  if (state === "work") return "running";
  if (["waiting", "suspect"].includes(state)) return "warning";
  if (state === "shutdown") return "success";
  if (["failed", "lost"].includes(state)) return "error";
  return "idle";
}
