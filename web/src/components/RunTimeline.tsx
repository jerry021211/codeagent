import { Bot, Check, CircleDashed, GitBranch, Hammer, RefreshCcw, Sparkles } from "lucide-react";
import type { RunViewState } from "@/store/runStore";
import { cx, isRunActive, statusLabel } from "@/lib/utils";

type Stage = { id: string; label: string; state: "pending" | "active" | "done" | "failed"; icon: typeof Bot };

export function RunTimeline({ run }: { run?: RunViewState }) {
  const actions = run ? run.actionOrder.map((id) => run.actions[id]).filter(Boolean) : [];
  const has = (kind: string) => actions.some((action) => action?.kind === kind);
  const activeKind = [...actions].reverse().find((action) => action?.status === "running" || action?.status === "waiting")?.kind;
  const terminal = run && !isRunActive(run.status);
  const failed = run?.status === "failed" || run?.status === "interrupted";
  const stages: Stage[] = [
    { id: "queue", label: "排队", icon: CircleDashed, state: !run ? "pending" : run.status === "queued" ? "active" : "done" },
    { id: "model", label: "模型", icon: Sparkles, state: activeKind === "model" ? "active" : has("model") ? "done" : "pending" },
    { id: "tool", label: "工具", icon: Hammer, state: activeKind === "tool" ? "active" : has("tool") ? "done" : "pending" },
    { id: "subagent", label: "子 Agent", icon: GitBranch, state: activeKind === "subagent" ? "active" : has("subagent") ? "done" : "pending" },
    { id: "recovery", label: "恢复", icon: RefreshCcw, state: activeKind === "recovery" ? "active" : has("recovery") ? "done" : "pending" },
    { id: "done", label: failed ? "异常" : "完成", icon: failed ? Bot : Check, state: terminal ? (failed ? "failed" : "done") : "pending" },
  ];

  return (
    <div className="border-b border-line bg-surface/90 px-4 py-2 backdrop-blur-xl sm:px-6">
      <div className="mx-auto flex max-w-4xl items-center">
        {stages.map((stage, index) => {
          const Icon = stage.icon;
          return (
            <div key={stage.id} className={cx("flex items-center", index < stages.length - 1 && "flex-1")}>
              <div className="group flex shrink-0 items-center gap-1.5" title={stage.label}>
                <div
                  className={cx(
                    "grid size-6 place-items-center rounded-full border transition-all duration-300 motion-reduce:transition-none",
                    stage.state === "active" && "border-accent/50 bg-accent/10 text-accent shadow-[0_0_0_4px_rgba(72,113,247,0.08)]",
                    stage.state === "done" && "border-success/35 bg-success/10 text-success",
                    stage.state === "failed" && "border-danger/35 bg-danger/10 text-danger",
                    stage.state === "pending" && "border-line bg-surface text-ink-faint",
                  )}
                >
                  <Icon className={cx("size-3", stage.state === "active" && "animate-soft-pulse motion-reduce:animate-none")} />
                </div>
                <span className={cx("hidden text-[10px] font-medium sm:block", stage.state === "pending" ? "text-ink-faint" : "text-ink-muted")}>{stage.label}</span>
              </div>
              {index < stages.length - 1 && (
                <div className="mx-2 h-px min-w-2 flex-1 overflow-hidden bg-line sm:mx-3">
                  <div className={cx("h-full bg-success transition-all duration-500 motion-reduce:transition-none", stage.state === "done" || stage.state === "failed" ? "w-full" : "w-0")} />
                </div>
              )}
            </div>
          );
        })}
      </div>
      {run?.status === "queued" && run.queuePosition != null && <div className="mt-1 text-center text-[10px] text-ink-muted">队列位置 {run.queuePosition}</div>}
      {run?.status && <span className="sr-only">运行状态：{statusLabel[run.status]}</span>}
    </div>
  );
}
