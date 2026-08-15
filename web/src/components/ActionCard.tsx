import { useState } from "react";
import { Bot, ChevronDown, CircleAlert, FileCode2, GitBranch, Hammer, RefreshCcw, Sparkles } from "lucide-react";
import type { RunAction } from "@/types/api";
import { cx, formatDuration, prettyJson } from "@/lib/utils";
import { Spinner, StatusDot } from "@/components/ui";

const kindMeta = {
  model: { icon: Sparkles, className: "text-accent bg-accent/10 border-accent/20", label: "MODEL" },
  tool: { icon: Hammer, className: "text-cyan-600 dark:text-cyan-400 bg-cyan-500/10 border-cyan-500/20", label: "TOOL" },
  subagent: { icon: GitBranch, className: "text-violet-600 dark:text-violet-400 bg-violet-500/10 border-violet-500/20", label: "AGENT" },
  recovery: { icon: RefreshCcw, className: "text-warning bg-warning/10 border-warning/20", label: "RECOVERY" },
  context: { icon: FileCode2, className: "text-ink-muted bg-surface-strong border-line", label: "CONTEXT" },
};

const statusText: Record<RunAction["status"], string> = {
  queued: "等待执行",
  waiting: "等待确认",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  blocked: "已阻止",
  cancelled: "已取消",
};

export function ActionCard({ action }: { action: RunAction }) {
  const [expanded, setExpanded] = useState(action.status === "failed");
  const meta = kindMeta[action.kind];
  const Icon = meta.icon;
  const hasDetails = action.input != null || action.output != null || action.error;
  const live = action.status === "running" || action.status === "waiting";
  return (
    <div className={cx("overflow-hidden rounded-2xl border bg-surface shadow-sm transition", live ? "border-accent/25 shadow-accent/5" : "border-line")}>
      <button
        type="button"
        disabled={!hasDetails}
        onClick={() => setExpanded((value) => !value)}
        className="flex w-full items-center gap-3 px-3.5 py-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/50 disabled:cursor-default"
      >
        <span className={cx("grid size-8 shrink-0 place-items-center rounded-xl border", meta.className)}>
          {live ? <Spinner className="size-3.5" /> : <Icon className="size-3.5" />}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span className="text-[9px] font-bold tracking-[0.16em] text-ink-faint">{meta.label}</span>
            {action.iteration != null && <span className="rounded bg-surface-strong px-1.5 py-0.5 font-mono text-[9px] text-ink-faint">#{action.iteration}</span>}
          </span>
          <span className="mt-0.5 block truncate text-xs font-semibold text-ink">{action.title}</span>
          {action.subtitle && <span className="mt-0.5 block truncate text-[11px] text-ink-muted">{action.subtitle}</span>}
        </span>
        <span className="flex shrink-0 items-center gap-2 text-[10px] text-ink-muted">
          <StatusDot status={action.status === "failed" ? "error" : action.status === "blocked" || action.status === "waiting" ? "warning" : action.status === "completed" ? "success" : live ? "running" : "idle"} pulse={live} />
          <span className="hidden sm:inline">{statusText[action.status]}</span>
          {action.duration_ms != null && <span className="font-mono">{formatDuration(action.duration_ms)}</span>}
          {hasDetails && <ChevronDown className={cx("size-3.5 transition-transform motion-reduce:transition-none", expanded && "rotate-180")} />}
        </span>
      </button>
      {expanded && hasDetails && (
        <div className="space-y-3 border-t border-line bg-surface-muted/60 px-3.5 py-3">
          {action.input != null && <Detail label="输入" value={action.input} />}
          {action.output != null && <Detail label="结果" value={action.output} />}
          {action.error && (
            <div className="flex gap-2 rounded-xl border border-danger/20 bg-danger/5 p-3 text-xs text-danger">
              <CircleAlert className="mt-0.5 size-3.5 shrink-0" />
              <span className="break-words">{action.error}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Detail({ label, value }: { label: string; value: unknown }) {
  return (
    <div>
      <div className="mb-1.5 text-[9px] font-bold uppercase tracking-[0.16em] text-ink-faint">{label}</div>
      <pre className="scrollbar-thin max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-xl border border-line bg-code p-3 font-mono text-[10px] leading-5 text-code-ink">{prettyJson(value)}</pre>
    </div>
  );
}
