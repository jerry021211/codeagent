import type { ButtonHTMLAttributes, ReactNode } from "react";
import { LoaderCircle } from "lucide-react";
import { cx } from "@/lib/utils";

export function IconButton({
  label,
  className,
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={cx(
        "inline-flex size-9 shrink-0 items-center justify-center rounded-xl border border-transparent text-ink-muted transition hover:border-line hover:bg-surface-strong hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-40",
        className,
      )}
      {...props}
    >
      {children}
    </button>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <LoaderCircle aria-hidden className={cx("size-4 animate-spin text-accent motion-reduce:animate-none", className)} />;
}

export function EmptyPanel({ icon, title, body }: { icon: ReactNode; title: string; body: string }) {
  return (
    <div className="mx-auto flex max-w-sm flex-col items-center px-6 py-16 text-center">
      <div className="mb-4 grid size-12 place-items-center rounded-2xl border border-line bg-surface-strong text-ink-muted shadow-sm">{icon}</div>
      <h2 className="text-sm font-semibold text-ink">{title}</h2>
      <p className="mt-2 text-sm leading-6 text-ink-muted">{body}</p>
    </div>
  );
}

export function StatusDot({ status, pulse = false }: { status: "idle" | "running" | "success" | "warning" | "error"; pulse?: boolean }) {
  const color = {
    idle: "bg-ink-faint",
    running: "bg-accent",
    success: "bg-success",
    warning: "bg-warning",
    error: "bg-danger",
  }[status];
  return (
    <span className="relative inline-flex size-2 shrink-0" aria-hidden>
      {pulse && <span className={cx("absolute inset-0 rounded-full opacity-40 animate-ping motion-reduce:animate-none", color)} />}
      <span className={cx("relative size-2 rounded-full", color)} />
    </span>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cx("animate-pulse rounded-lg bg-surface-strong motion-reduce:animate-none", className)} />;
}
