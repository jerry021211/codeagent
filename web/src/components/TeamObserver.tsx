import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { api } from "@/lib/api";
import { cx, prettyJson } from "@/lib/utils";
import type { DatabaseChange, TeamSnapshot } from "@/types/api";

const tableNames: Record<string, string> = {
  team_runs: "团队整体状态", team_plan_revisions: "团队方案与审批", team_agents: "成员身份",
  agent_sessions: "成员运行状态", task_attempts: "任务执行", tasks: "任务", task_lists: "任务列表",
  task_dependencies: "任务依赖", task_activity: "任务活动", resource_leases: "资源占用",
  team_messages: "团队通信", team_message_consumptions: "消息消费确认", team_commands: "幂等命令",
  team_base_confirmations: "源仓库确认", worktree_bindings: "工作目录与写权限",
  tool_executions: "工具执行", attempt_plans: "执行方案审批", candidates: "候选与审查",
  validation_runs: "Runtime 测试", manual_integration_checks: "人工集成验证",
  agent_session_checkpoints: "成员上下文存档", conversations: "会话", runs: "主循环",
  messages: "聊天消息", events: "运行事件", approvals: "工具审批", model_calls: "模型调用",
  checkpoints: "主循环上下文存档",
};

const fieldNames: Record<string, string> = {
  state: "状态", status: "状态", owner: "负责人", revision: "修订版本",
  active_plan_revision: "当前批准方案", write_enabled: "是否允许写入", result_unknown: "写入结果是否未知",
  waiting_reason: "等待原因", frozen_reason: "冻结原因", generation: "会话代次",
  current_attempt_id: "正在执行的 Attempt", session_id: "运行会话", agent_id: "成员",
  task_id: "任务编号", attempt_id: "执行编号", tool_call_id: "工具调用编号", trace_id: "追踪编号",
  sender_agent_id: "发送者", recipient_agent_id: "接收者", id: "记录编号", title: "标题",
  subject: "任务名称", description: "说明", workspace: "项目目录", created_at: "创建时间",
  updated_at: "更新时间", archived_at: "归档时间", active_task_list_id: "当前任务列表",
  heartbeat_at: "最近心跳", delivered_at: "首次投递时间", acked_at: "确认消费时间",
  delivery_attempts: "投递次数", dedupe_key: "去重键", payload_json: "消息内容",
  input_json: "工具参数", output_ref: "输出位置", commit_hash: "候选提交",
  exit_code: "测试退出码", scope_hash: "写入范围指纹", fingerprint: "工作目录绑定指纹",
  write_scopes_json: "允许写入范围", decision_reason: "审批意见", review_reason: "审查意见",
  error_json: "异常详情", error: "错误原因", plan_json: "方案内容", lease_token: "租约凭据",
};

const states: Record<string, string> = {
  planning: "规划中", waiting_approval: "等待批准", pending_user_approval: "等待用户批准",
  draft: "草稿", approved: "已批准", rejected: "已拒绝", pending: "待执行", in_progress: "执行中",
  running: "执行中", work: "工作中", idle: "空闲", waiting: "等待中", lost: "已失联",
  plan_required: "需要执行方案", plan_submitted: "等待执行方案审批", candidate_submitted: "等待候选审查",
  submitted: "已提交", accepted: "已接受", validating: "验证中", committed: "已形成提交",
  succeeded: "执行成功", completed: "已完成", failed: "失败", cancelled: "已取消",
  active: "有效", frozen: "已冻结", retained: "已保留", released: "已释放", blocked: "执行前已拦截",
  ready_for_manual_integration: "等待人工集成", closed_with_unmerged_candidates: "已关闭，候选未集成",
  processed: "已消费", shutdown: "已停止", suspect: "疑似失联", scope_violation: "越界",
};

const operationNames = { insert: "新增", update: "更新", delete: "删除", snapshot: "初始快照" };
const noiseFields = new Set(["updated_at", "heartbeat_at", "next_event_seq"]);
const supportingTables = new Set(["events", "task_activity", "agent_session_checkpoints", "checkpoints"]);
const pageSize = 100;

function valueText(value: unknown): string {
  if (value === undefined) return "（字段不存在）";
  if (value === null) return "NULL（空值）";
  if (typeof value === "string") return states[value] ? `${states[value]} · ${value}` : value;
  return prettyJson(value);
}

function describe(change: DatabaseChange): string {
  const transitions = Object.entries(change.transitions);
  const context = [change.identity.type ? `消息/事件：${String(change.identity.type)}` : "",
    change.identity.tool_name ? `工具：${String(change.identity.tool_name)}` : ""].filter(Boolean).join("；");
  if (transitions.length) return [context, ...transitions.map(([key, pair]) =>
    `${fieldNames[key] ?? key}：${change.operation === "insert" || change.operation === "snapshot" ? valueText(pair.after) : `${valueText(pair.before)} → ${change.operation === "delete" ? "记录已删除" : valueText(pair.after)}`}`,
  )].filter(Boolean).join("；");
  if (context) return context;
  return change.changed_fields.map((key) => fieldNames[key] ?? key).join("、");
}

export function TeamObserver({ team, onClose }: { team: TeamSnapshot; onClose: () => void }) {
  const [changes, setChanges] = useState<DatabaseChange[]>([]);
  const [table, setTable] = useState("");
  const [search, setSearch] = useState("");
  const [allChanges, setAllChanges] = useState(false);
  const [live, setLive] = useState(true);
  const [selected, setSelected] = useState<number>();
  const [page, setPage] = useState(0);
  const [tables, setTables] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [caughtUp, setCaughtUp] = useState(false);
  const [lastRead, setLastRead] = useState("");
  const cursor = useRef(0);
  const dialog = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.focus();
    return () => previous?.focus();
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function read() {
      try {
        const result = await api.getTeamChanges(team.team.id, cursor.current);
        if (cancelled) return;
        setChanges((current) => [...current, ...result.items]);
        cursor.current = result.next_cursor;
        setTables(result.tables);
        setCaughtUp(!result.has_more);
        setLastRead(new Date().toLocaleTimeString());
        setError("");
        // Drain durable history in pages; polling never samples away intermediate writes.
        if (live) timer = setTimeout(read, result.has_more ? 50 : 2000);
      } catch (cause) {
        if (cancelled) return;
        setError(cause instanceof Error ? cause.message : "读取失败");
        if (live) timer = setTimeout(read, 3000);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void read();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [team.team.id, live]);

  const names = useMemo(() => new Map([
    ...team.agents.map((agent) => [agent.id, agent.name] as const),
    ...team.tasks.map((task) => [task.task.id, task.task.subject] as const),
  ]), [team.agents, team.tasks]);
  const filtered = useMemo(() => changes.filter((change) => {
    if (table && change.table_name !== table) return false;
    if (!allChanges && !table && (supportingTables.has(change.table_name)
      || change.changed_fields.every((field) => noiseFields.has(field)))) return false;
    const haystack = [change.table_name, ...Object.values(change.record_key),
      ...Object.values(change.identity), ...Object.values(change.identity).map((id) => names.get(String(id))),
      ...change.changed_fields, describe(change)].join(" ").toLowerCase();
    return haystack.includes(search.trim().toLowerCase());
  }), [changes, table, allChanges, search, names]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, pageCount - 1);
  const visible = filtered.slice(currentPage * pageSize, (currentPage + 1) * pageSize);
  const selectedId = selected ?? visible[0]?.seq;
  const detail = useQuery({
    queryKey: ["team-change", team.team.id, selectedId],
    queryFn: () => api.getTeamChange(team.team.id, selectedId!),
    enabled: selectedId !== undefined,
    staleTime: Infinity,
  });
  const selectedIndex = filtered.findIndex((change) => change.seq === selectedId);
  const move = (offset: number) => {
    const next = filtered[selectedIndex + offset];
    if (next) { setSelected(next.seq); setPage(Math.floor((selectedIndex + offset) / pageSize)); }
  };
  const resetSelection = () => { setSelected(undefined); setPage(0); };

  return createPortal(
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/40 p-2 sm:p-5">
      <div ref={dialog} role="dialog" aria-modal="true" aria-labelledby="team-observer-title" tabIndex={-1}
        onKeyDown={(event) => {
          if (event.key === "Escape") onClose();
          if (event.key === "Tab") {
            const elements = dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input, select, [tabindex="0"]');
            const first = elements?.[0], last = elements?.[elements.length - 1];
            if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) {
              event.preventDefault(); last?.focus();
            } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
          }
        }}
        className="flex h-[94vh] w-full max-w-[1500px] flex-col overflow-hidden rounded-2xl border border-line bg-surface text-ink shadow-2xl outline-none">
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-line p-4">
          <div className="min-w-0">
            <h2 id="team-observer-title" className="text-lg font-semibold">Team 执行观察 · 数据库变化时间线</h2>
            <p className="mt-1 break-all font-mono text-xs text-ink-muted">{team.team.id}</p>
            <p className="mt-2 text-xs text-ink-muted">只读观察，不批准、不恢复、不触发 Agent。当前状态：{valueText(team.team.state)}</p>
          </div>
          <button onClick={onClose} aria-label="关闭执行观察" className="rounded-lg p-2 hover:bg-surface-muted"><X className="size-5" /></button>
        </header>
        <div className="shrink-0 space-y-2 border-b border-line p-3 text-xs">
          <p className="rounded-lg bg-surface-muted p-2 leading-5">阅读顺序：方案审批 → 任务认领 → Worktree 与权限 → 工具执行 / 团队通信 → Lead 审查 → Runtime 验证 → 候选提交。点击一条记录，右侧查看字段旧值和新值。</p>
          <div className="flex flex-wrap items-center gap-3">
            <select aria-label="按数据库表筛选" value={table} onChange={(event) => { setTable(event.target.value); resetSelection(); }} className="max-w-full rounded-lg border border-line bg-surface p-2">
              <option value="">全部流程</option>
              {tables.map((name) => <option key={name} value={name}>{tableNames[name] ?? name} · {name}</option>)}
            </select>
            <input aria-label="搜索任务成员或字段" value={search} onChange={(event) => { setSearch(event.target.value); resetSelection(); }} placeholder="搜索任务 / Agent / ID / 字段 / 状态" className="min-w-56 flex-1 rounded-lg border border-line bg-surface p-2" />
            <label className="flex items-center gap-1"><input type="checkbox" checked={allChanges} onChange={(event) => { setAllChanges(event.target.checked); resetSelection(); }} />包括心跳、存档及底层事件</label>
            <label className="flex items-center gap-1"><input type="checkbox" checked={live} onChange={(event) => setLive(event.target.checked)} />实时读取</label>
          </div>
          <div className="flex flex-wrap items-center gap-3 text-ink-muted">
            <span>已读取 {changes.length} 条 · 筛选后 {filtered.length} 条 · {loading ? "读取中" : caughtUp ? "已追上最新记录" : "正在读取历史分页"} · {lastRead}</span>
            <span>暂停读取不影响后台记录；初始快照不是操作历史。</span>
          </div>
          {error && <p role="alert" className="text-danger">读取失败：{error}。已读取的数据仍保留，实时模式会重连。</p>}
        </div>
        <div className="grid min-h-0 flex-1 grid-cols-1 overflow-auto md:grid-cols-[minmax(300px,38%)_1fr] md:overflow-hidden">
          <section aria-label="变化时间线" className="flex min-h-64 flex-col border-r border-line md:min-h-0">
            <div className="flex items-center justify-between border-b border-line p-2 text-xs">
              <button disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)} className="p-1 disabled:opacity-30">上一页</button>
              <span>{currentPage + 1} / {pageCount}</span>
              <button disabled={currentPage >= pageCount - 1} onClick={() => setPage(currentPage + 1)} className="p-1 disabled:opacity-30">下一页</button>
              <button onClick={() => { setPage(pageCount - 1); setSelected(filtered.at(-1)?.seq); }} className="p-1 text-accent">到最新</button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-2">
              {!visible.length && <p className="p-4 text-sm text-ink-muted">{loading ? "正在读取持久化记录…" : "没有符合筛选条件的变化。可选择其他表或显示全部底层记录。"}</p>}
              {visible.map((change) => <button key={change.seq} onClick={() => setSelected(change.seq)}
                className={cx("mb-2 w-full rounded-xl border p-3 text-left", selectedId === change.seq ? "border-accent bg-accent/5" : "border-line hover:bg-surface-muted")}>
                <div className="flex justify-between gap-2 text-[11px] text-ink-muted"><span>#{change.seq} · {operationNames[change.operation]}</span><time>{new Date(change.occurred_at).toLocaleTimeString(undefined, { hour12: false })}</time></div>
                <div className="mt-1 text-sm font-medium">{tableNames[change.table_name] ?? change.table_name}</div>
                <p className="mt-1 break-all text-[11px] text-ink-muted">{change.table_name} · {Object.values(change.record_key).join(" / ")}</p>
                <p className="mt-2 line-clamp-3 break-all text-xs leading-5">{describe(change)}</p>
                {Object.entries(change.identity).filter(([key]) => ["agent_id", "task_id", "sender_agent_id", "recipient_agent_id"].includes(key)).map(([key, id]) => <span key={key} className="mr-2 text-[10px] text-ink-muted">{fieldNames[key] ?? key}: {names.get(String(id)) ?? String(id)}</span>)}
              </button>)}
            </div>
          </section>
          <section aria-label="数据库字段变化详情" className="min-w-0 overflow-y-auto p-4">
            <div className="mb-3 flex gap-3 text-xs">
              <button disabled={selectedIndex <= 0} onClick={() => move(-1)} className="rounded-lg border border-line px-3 py-2 disabled:opacity-30">上一步</button>
              <button disabled={selectedIndex < 0 || selectedIndex >= filtered.length - 1} onClick={() => move(1)} className="rounded-lg border border-line px-3 py-2 disabled:opacity-30">下一步</button>
            </div>
            {detail.isLoading && <p className="text-sm text-ink-muted">正在读取字段详情…</p>}
            {detail.error && <p role="alert" className="text-sm text-danger">{detail.error.message}</p>}
            {detail.data && <ChangeDetail change={detail.data} />}
            {selectedId === undefined && <p className="text-sm text-ink-muted">选择左侧记录查看详情。</p>}
          </section>
        </div>
        <footer className="shrink-0 border-t border-line px-4 py-2 text-[10px] leading-4 text-ink-muted">
          按数据库写入序号排列；同一事务可产生多条记录，只有提交后才可见。不是 LLM 内部思考或文件系统录像。任务列表及会话运行记录包含该列表 / 会话的其他活动。原始字段完整保存在本地 database_changes；页面敏感字段会脱敏，超长文本标明截断。
        </footer>
      </div>
    </div>, document.body,
  );
}

function ChangeDetail({ change }: { change: DatabaseChange }) {
  const [showUnchanged, setShowUnchanged] = useState(false);
  const fields = showUnchanged
    ? [...new Set([...Object.keys(change.before ?? {}), ...Object.keys(change.after ?? {})])]
    : change.changed_fields;
  return <div className="space-y-3">
    <div>
      <h3 className="text-base font-semibold">#{change.seq} · {tableNames[change.table_name]} · {operationNames[change.operation]}</h3>
      <p className="mt-1 break-all font-mono text-xs text-ink-muted">{change.table_name} · {JSON.stringify(change.record_key)}</p>
      <p className="mt-1 text-xs text-ink-muted">{change.occurred_at}</p>
    </div>
    <p className="rounded-lg bg-surface-muted p-3 text-sm leading-6">{change.operation === "snapshot" ? "这是开启监控时已有记录的初始值。此前如何变成这个值，没有历史记录，不能推断。" : describe(change)}</p>
    <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={showUnchanged} onChange={(event) => setShowUnchanged(event.target.checked)} />显示未变化字段（默认只看变化）</label>
    <div className="overflow-x-auto rounded-xl border border-line">
      <table className="w-full table-fixed text-left text-xs">
        <thead className="bg-surface-muted"><tr><th className="w-1/4 p-2">数据库字段</th><th className="p-2">变化前</th><th className="p-2">变化后</th></tr></thead>
        <tbody>{fields.map((field) => <tr key={field} className="border-t border-line align-top">
          <th className="break-all p-2 font-normal"><div>{fieldNames[field] ?? field}</div><code className="text-[10px] text-ink-muted">{field}</code></th>
          <td className="p-2"><FieldValue value={change.before?.[field]} /></td>
          <td className="p-2"><FieldValue value={change.after?.[field]} /></td>
        </tr>)}</tbody>
      </table>
    </div>
    <details className="rounded-lg border border-line p-3 text-xs"><summary className="cursor-pointer">记录关联 ID（查找任务、Agent、工具和消息）</summary><pre className="mt-2 whitespace-pre-wrap break-all">{prettyJson(change.identity)}</pre></details>
  </div>;
}

function FieldValue({ value }: { value: unknown }) {
  const text = valueText(value);
  if (text.length > 240) return <details><summary className="cursor-pointer text-accent">展开内容（{text.length} 字符）</summary><pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-all">{text}</pre></details>;
  return <pre className="whitespace-pre-wrap break-all font-mono leading-5">{text}</pre>;
}
