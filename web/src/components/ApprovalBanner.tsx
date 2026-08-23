import { useState } from "react";
import { Check, ChevronDown, ShieldAlert, X } from "lucide-react";
import type { Approval, ApprovalDecision } from "@/types/api";
import { cx, prettyJson } from "@/lib/utils";

export function ApprovalBanner({
  approval,
  busy,
  onDecision,
}: {
  approval: Approval;
  busy?: boolean;
  onDecision: (decision: ApprovalDecision) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const hasDetails = Boolean(approval.reason || approval.input != null);

  return (
    <section
      className="group overflow-hidden rounded-xl border border-warning/30 bg-warning/[0.07] shadow-sm transition-colors hover:border-warning/45 focus-within:border-warning/45"
      aria-live="assertive"
    >
      <div className="flex min-h-12 items-center gap-2 px-2.5 sm:px-3">
        <div className="grid size-7 shrink-0 place-items-center rounded-lg bg-warning/15 text-warning">
          <ShieldAlert className="size-3.5" />
        </div>

        <button
          type="button"
          className="flex min-w-0 flex-1 items-center gap-2 self-stretch text-left outline-none"
          aria-expanded={expanded}
          aria-controls={`approval-details-${approval.id}`}
          onClick={() => hasDetails && setExpanded((value) => !value)}
        >
          <span className="shrink-0 text-[11px] font-semibold text-ink">需要确认</span>
          <span className="max-w-24 shrink-0 truncate rounded-md border border-warning/20 bg-surface/70 px-1.5 py-0.5 font-mono text-[9px] font-semibold text-warning" title={approval.tool_name}>
            {approval.tool_name}
          </span>
          <span className="min-w-0 flex-1 truncate text-[11px] text-ink-muted" title={approval.summary}>
            {approval.summary}
          </span>
          {hasDetails && (
            <span className="hidden shrink-0 items-center gap-1 text-[9px] text-ink-faint md:flex">
              <span className="group-hover:hidden group-focus-within:hidden">悬停查看详情</span>
              <ChevronDown className={cx("size-3 transition-transform duration-200 group-hover:rotate-180 group-focus-within:rotate-180", expanded && "rotate-180")} />
            </span>
          )}
        </button>

        <div className="flex shrink-0 items-center gap-1.5 border-l border-warning/15 pl-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => onDecision("deny")}
            className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg border border-line bg-surface px-2.5 text-[10px] font-medium text-ink-muted transition hover:border-danger/30 hover:bg-danger/5 hover:text-danger disabled:opacity-50"
          >
            <X className="size-3.5" /> <span className="hidden sm:inline">拒绝</span>
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onDecision("allow")}
            className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg bg-warning px-2.5 text-[10px] font-semibold text-white shadow-sm shadow-warning/15 transition hover:brightness-95 disabled:opacity-50"
          >
            <Check className="size-3.5" /> <span className="hidden sm:inline">允许一次</span>
          </button>
        </div>
      </div>

      {hasDetails && (
        <div
          id={`approval-details-${approval.id}`}
          className={cx(
            "grid transition-[grid-template-rows,opacity] duration-200 ease-out group-hover:grid-rows-[1fr] group-hover:opacity-100 group-focus-within:grid-rows-[1fr] group-focus-within:opacity-100",
            expanded ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0",
          )}
        >
          <div className="min-h-0 overflow-hidden">
            <div className="border-t border-warning/15 px-3 pb-3 pt-2.5 sm:px-4">
              {approval.reason && (
                <div className="flex gap-2 text-[10px] leading-5">
                  <span className="shrink-0 font-medium text-ink-faint">触发原因</span>
                  <span className="text-ink-muted">{approval.reason}</span>
                </div>
              )}
              {approval.input != null && (
                <pre className="scrollbar-thin mt-2 max-h-28 overflow-auto rounded-lg border border-line bg-code px-3 py-2 font-mono text-[10px] leading-5 text-code-ink">
                  {prettyJson(approval.input)}
                </pre>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
