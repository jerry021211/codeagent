import { useState } from "react";
import {
  Activity,
  Bot,
  Braces,
  Check,
  ChevronDown,
  CircleAlert,
  Clock3,
  Coins,
  Copy,
  FileCode2,
  GitBranch,
  Hash,
  RefreshCcw,
  X,
} from "lucide-react";
import type { RuntimeConfig } from "@/types/api";
import type { TaskList, TaskResource } from "@/types/api";
import type { RunViewState } from "@/store/runStore";
import { cx, formatDuration, formatNumber, formatTime, isRunActive, prettyJson, statusLabel, tokenTotal } from "@/lib/utils";
import { EmptyPanel, IconButton, StatusDot } from "@/components/ui";
import { TaskPlan } from "@/components/TaskPlan";

type Props = {
  run?: RunViewState;
  runtime?: RuntimeConfig;
  mobile?: boolean;
  onClose?: () => void;
  tasks?: TaskResource[];
  tasksLoading?: boolean;
  taskBusy?: boolean;
  taskList?: TaskList;
  taskLists?: TaskList[];
  onContinueTask?: (task: TaskResource) => void;
  onCreateTask?: (input: { subject: string; description: string; activeForm?: string }) => void;
  onSelectTaskList?: (taskListId: string) => void;
  onPromoteTaskList?: () => void;
};

export function InspectorPanel({ run, runtime, mobile, onClose, tasks = [], tasksLoading, taskBusy, taskList, taskLists, onContinueTask, onCreateTask, onSelectTaskList, onPromoteTaskList }: Props) {
  const [tab, setTab] = useState<"run" | "tasks" | "debug">("tasks");
  return (
    <aside className="flex h-full min-h-0 w-full flex-col border-l border-line bg-surface">
      <header className="flex h-16 shrink-0 items-center justify-between border-b border-line px-4">
        <div className="flex h-full items-end gap-5">
          <Tab active={tab === "run"} onClick={() => setTab("run")}>运行</Tab>
          <Tab active={tab === "tasks"} onClick={() => setTab("tasks")}>任务</Tab>
          <Tab active={tab === "debug"} onClick={() => setTab("debug")}>调试</Tab>
        </div>
        {mobile && onClose && <IconButton label="关闭运行面板" onClick={onClose}><X className="size-4" /></IconButton>}
      </header>
      <div className="scrollbar-thin min-h-0 flex-1 overflow-y-auto">
        {tab === "run" ? <RunInspector run={run} runtime={runtime} /> : tab === "tasks" ? <TaskPlan tasks={tasks} loading={tasksLoading} busy={taskBusy} taskList={taskList} taskLists={taskLists} onContinue={onContinueTask ?? (() => undefined)} onCreate={onCreateTask ?? (() => undefined)} onSelectList={onSelectTaskList} onPromote={onPromoteTaskList} /> : <DebugInspector run={run} runtime={runtime} />}
      </div>
    </aside>
  );
}

function Tab({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return <button type="button" onClick={onClick} className={cx("relative h-11 text-xs font-medium transition", active ? "text-ink" : "text-ink-muted hover:text-ink")}><span>{children}</span>{active && <span className="absolute inset-x-0 bottom-0 h-0.5 rounded-full bg-accent" />}</button>;
}

function RunInspector({ run, runtime }: { run?: RunViewState; runtime?: RuntimeConfig }) {
  if (!run) return <EmptyPanel icon={<Activity className="size-5" />} title="没有正在观察的运行" body="发送消息后，这里会汇总进度、Token 和 Agent 状态。" />;
  const activeAction = [...run.actionOrder].reverse().map((id) => run.actions[id]).find((action) => action?.status === "running" || action?.status === "waiting");
  const agents = Object.values(run.agents);
  return (
    <div className="space-y-5 px-4 py-5">
      <Section title="运行状态" icon={<Activity className="size-3.5" />}>
        <div className="rounded-2xl border border-line bg-surface-muted p-3.5">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-xs font-semibold text-ink"><StatusDot status={isRunActive(run.status) ? run.status === "waiting_approval" ? "warning" : "running" : run.status === "failed" ? "error" : "success"} pulse={isRunActive(run.status)} />{statusLabel[run.status]}</div>
            <span className="font-mono text-[9px] text-ink-faint" title={run.runId}>{run.runId.slice(0, 8)}</span>
          </div>
          {activeAction && <div className="mt-3 border-t border-line pt-3"><div className="text-[9px] uppercase tracking-wider text-ink-faint">当前动作</div><div className="mt-1 truncate text-xs text-ink">{activeAction.title}</div></div>}
          {run.queuePosition != null && run.status === "queued" && <div className="mt-2 text-[10px] text-ink-muted">队列位置：{run.queuePosition}</div>}
        </div>
      </Section>

      <Section title="Token" icon={<Coins className="size-3.5" />}>
        <TokenPanel run={run} model={runtime?.model} />
      </Section>

      <Section title="Agent" icon={<GitBranch className="size-3.5" />} count={agents.length}>
        {agents.length ? <div className="space-y-1.5">{agents.map((agent) => <div key={agent.id} className={cx("rounded-xl border border-line px-3 py-2.5", agent.parent_id && "ml-3 border-violet-500/20")}><div className="flex items-center gap-2"><Bot className={cx("size-3.5", agent.parent_id ? "text-violet-500" : "text-accent")} /><span className="min-w-0 flex-1 truncate text-[11px] font-medium text-ink">{agent.label}</span><StatusDot status={agent.status === "running" ? "running" : agent.status === "failed" ? "error" : agent.status === "completed" ? "success" : "idle"} pulse={agent.status === "running"} /></div>{agent.task && <p className="mt-1.5 line-clamp-2 text-[10px] leading-4 text-ink-muted">{agent.task}</p>}</div>)}</div> : <MutedEmpty text="没有子 Agent 活动" />}
      </Section>

      <Section title="修改文件" icon={<FileCode2 className="size-3.5" />} count={run.modifiedFiles.length}>
        {run.modifiedFiles.length ? <div className="space-y-1">{run.modifiedFiles.map((file) => <div key={file} className="truncate rounded-lg bg-surface-muted px-2.5 py-2 font-mono text-[9px] text-ink-muted" title={file}>{file}</div>)}</div> : <MutedEmpty text="尚未记录文件改动" />}
      </Section>

      <Section title="错误恢复" icon={<RefreshCcw className="size-3.5" />} count={run.recovery.length}>
        {run.recovery.length ? <div className="space-y-2">{run.recovery.map((item) => <div key={item.id} className="rounded-xl border border-warning/20 bg-warning/5 p-3"><div className="flex items-center gap-2"><RefreshCcw className={cx("size-3 text-warning", item.status === "retrying" && "animate-spin motion-reduce:animate-none")} /><span className="min-w-0 flex-1 truncate text-[11px] font-medium text-ink">{item.reason}</span><span className="text-[9px] text-ink-faint">{formatTime(item.occurred_at)}</span></div><div className="mt-1.5 flex gap-2 text-[9px] text-ink-muted">{item.attempt != null && <span>第 {item.attempt} 次</span>}{item.delay_ms != null && <span>等待 {formatDuration(item.delay_ms)}</span>}{item.decision && <span>{item.decision}</span>}</div></div>)}</div> : <MutedEmpty text="未触发恢复流程" />}
      </Section>
    </div>
  );
}

function TokenPanel({ run, model }: { run: RunViewState; model?: string | null }) {
  const total = tokenTotal(run.usage);
  const hitRatio = run.usage.cache_hit_ratio;
  return <div className="overflow-hidden rounded-2xl border border-line"><div className="bg-surface-muted px-3.5 py-3"><div className="flex items-end justify-between"><div><div className="text-[9px] uppercase tracking-wider text-ink-faint">Run total</div><div className="mt-1 text-xl font-semibold tracking-tight text-ink">{formatNumber(total)}</div></div><span className="max-w-28 truncate font-mono text-[9px] text-ink-muted" title={run.usage.model || model || ""}>{run.usage.model || model || "—"}</span></div></div><div className="grid grid-cols-2 divide-x divide-y divide-line border-t border-line"><Metric label="未缓存输入" value={run.usage.input_tokens} /><Metric label="缓存读取" value={run.usage.cache_read_input_tokens} /><Metric label="缓存命中率" value={hitRatio == null ? null : `${(hitRatio * 100).toFixed(1)}%`} /><Metric label="输出" value={run.usage.output_tokens} /></div>{run.usageByCall.length > 0 && <div className="border-t border-line px-3 py-2 text-[9px] text-ink-muted">已记录 {run.usageByCall.length} 次模型调用{run.usage.estimated ? " · 含估算值" : ""}</div>}</div>;
}

function Metric({ label, value }: { label: string; value?: number | string | null }) {
  return <div className="px-3 py-2.5"><div className="text-[9px] text-ink-faint">{label}</div><div className="mt-0.5 font-mono text-[11px] text-ink">{typeof value === "number" ? formatNumber(value) : value ?? "不可用"}</div></div>;
}

function DebugInspector({ run, runtime }: { run?: RunViewState; runtime?: RuntimeConfig }) {
  const [expandedEvent, setExpandedEvent] = useState<string>();
  const [copied, setCopied] = useState<string>();
  const copy = async (id: string, value: unknown) => { await navigator.clipboard.writeText(prettyJson(value)); setCopied(id); window.setTimeout(() => setCopied(undefined), 1200); };
  return <div className="space-y-5 px-4 py-5"><Section title="Runtime" icon={<Braces className="size-3.5" />}><DebugRows rows={[{ label: "模型", value: runtime?.model || "不可用" }, { label: "工作区", value: runtime?.workspace || "不可用" }, { label: "Max tokens", value: runtime?.max_tokens?.toString() || "不可用" }, { label: "Max iterations", value: runtime?.max_iterations?.toString() || "不可用" }]} /></Section><Section title="Prompt trace" icon={<Hash className="size-3.5" />}>{run?.promptTrace.hash || run?.promptTrace.fragments.length ? <div className="rounded-2xl border border-line"><div className="flex items-center gap-2 border-b border-line px-3 py-2.5"><Hash className="size-3 text-ink-faint" /><code className="min-w-0 flex-1 truncate text-[9px] text-ink-muted">{run.promptTrace.hash || "hash 不可用"}</code>{run.promptTrace.characters != null && <span className="text-[9px] text-ink-faint">{formatNumber(run.promptTrace.characters)} chars</span>}</div><div className="divide-y divide-line">{run.promptTrace.fragments.map((fragment) => <div key={fragment.name} className="px-3 py-2"><div className="flex items-center gap-2"><span className="min-w-0 flex-1 truncate text-[10px] font-medium text-ink">{fragment.name}</span><span className="font-mono text-[9px] text-ink-faint">{fragment.characters ?? "?"}</span></div><div className="mt-0.5 flex gap-2 text-[9px] text-ink-muted"><span>{fragment.source || "unknown"}</span>{fragment.truncated && <span className="text-warning">已裁剪</span>}</div></div>)}</div></div> : <MutedEmpty text="尚无 Prompt 元数据（正文不会展示）" />}</Section><Section title="原始事件" icon={<Clock3 className="size-3.5" />} count={run?.events.length ?? 0}>{run?.events.length ? <div className="space-y-1.5">{[...run.events].reverse().map((event) => <div key={`${event.id}:${event.seq}`} className="overflow-hidden rounded-xl border border-line"><button type="button" onClick={() => setExpandedEvent(expandedEvent === event.id ? undefined : event.id)} className="flex w-full items-center gap-2 px-2.5 py-2 text-left"><span className="font-mono text-[9px] text-ink-faint">#{event.seq}</span><span className="min-w-0 flex-1 truncate font-mono text-[9px] text-ink">{event.type}</span><span className="text-[8px] text-ink-faint">{formatTime(event.occurred_at)}</span><ChevronDown className={cx("size-3 text-ink-faint transition", expandedEvent === event.id && "rotate-180")} /></button>{expandedEvent === event.id && <div className="relative border-t border-line bg-code p-2.5"><button type="button" onClick={() => void copy(event.id, event)} className="absolute right-2 top-2 grid size-6 place-items-center rounded bg-white/10 text-code-ink/60 hover:text-code-ink">{copied === event.id ? <Check className="size-3" /> : <Copy className="size-3" />}</button><pre className="scrollbar-thin max-h-72 overflow-auto whitespace-pre-wrap break-all pr-7 font-mono text-[9px] leading-4 text-code-ink">{prettyJson(event)}</pre></div>}</div>)}</div> : <MutedEmpty text="SSE 事件将在这里按序显示" />}</Section>{run?.error && <div className="flex gap-2 rounded-xl border border-danger/20 bg-danger/5 p-3 text-[10px] text-danger"><CircleAlert className="size-3.5 shrink-0" />{run.error}</div>}</div>;
}

function DebugRows({ rows }: { rows: Array<{ label: string; value: string }> }) { return <div className="divide-y divide-line overflow-hidden rounded-xl border border-line">{rows.map((row) => <div key={row.label} className="grid grid-cols-[88px_1fr] gap-2 px-3 py-2 text-[9px]"><span className="text-ink-faint">{row.label}</span><span className="truncate font-mono text-ink-muted" title={row.value}>{row.value}</span></div>)}</div>; }
function Section({ title, icon, count, children }: { title: string; icon: React.ReactNode; count?: number; children: React.ReactNode }) { return <section><div className="mb-2.5 flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.13em] text-ink-muted">{icon}<span>{title}</span>{count != null && <span className="ml-auto rounded-full bg-surface-strong px-1.5 py-0.5 font-mono text-[8px] text-ink-faint">{count}</span>}</div>{children}</section>; }
function MutedEmpty({ text }: { text: string }) { return <div className="rounded-xl border border-dashed border-line px-3 py-4 text-center text-[10px] text-ink-faint">{text}</div>; }
