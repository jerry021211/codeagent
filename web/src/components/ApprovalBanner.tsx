import { AlertTriangle, Check, ShieldAlert, X } from "lucide-react";
import type { Approval, ApprovalDecision } from "@/types/api";
import { prettyJson } from "@/lib/utils";

export function ApprovalBanner({
  approval,
  busy,
  onDecision,
}: {
  approval: Approval;
  busy?: boolean;
  onDecision: (decision: ApprovalDecision) => void;
}) {
  return (
    <section className="mx-3 mb-2 overflow-hidden rounded-2xl border border-warning/30 bg-warning/10 shadow-lg shadow-warning/5 sm:mx-5" aria-live="assertive">
      <div className="flex gap-3 px-4 py-3">
        <div className="grid size-9 shrink-0 place-items-center rounded-xl border border-warning/30 bg-warning/15 text-warning">
          <ShieldAlert className="size-4.5" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-xs font-semibold text-ink">
            <AlertTriangle className="size-3.5 text-warning" /> 操作需要确认
          </div>
          <p className="mt-1 text-xs leading-5 text-ink-muted"><strong className="font-semibold text-ink">{approval.tool_name}</strong> · {approval.summary}</p>
          {approval.reason && <p className="mt-1 text-[11px] text-ink-muted">原因：{approval.reason}</p>}
          {approval.input != null && <pre className="scrollbar-thin mt-2 max-h-24 overflow-auto rounded-lg border border-warning/15 bg-surface/60 px-2.5 py-2 font-mono text-[10px] text-ink-muted">{prettyJson(approval.input)}</pre>}
        </div>
        <div className="flex shrink-0 flex-col justify-center gap-1.5 sm:flex-row sm:items-center">
          <button
            type="button"
            disabled={busy}
            onClick={() => onDecision("deny")}
            className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg border border-line bg-surface px-3 text-[11px] font-medium text-ink transition hover:border-danger/30 hover:text-danger disabled:opacity-50"
          >
            <X className="size-3.5" /> 拒绝
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onDecision("allow")}
            className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg bg-warning px-3 text-[11px] font-semibold text-white transition hover:brightness-95 disabled:opacity-50"
          >
            <Check className="size-3.5" /> 允许一次
          </button>
        </div>
      </div>
    </section>
  );
}
