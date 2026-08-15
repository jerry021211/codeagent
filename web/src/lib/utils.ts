import { clsx, type ClassValue } from "clsx";
import type { ApiList, RunStatus, TokenUsage } from "@/types/api";

export function cx(...values: ClassValue[]) {
  return clsx(values);
}

export function unwrapList<T>(value: ApiList<T>): T[] {
  if (Array.isArray(value)) return value;
  if ("items" in value) return value.items;
  return value.data;
}

export function formatTime(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
}

export function formatRelativeTime(value?: string | null) {
  if (!value) return "";
  const delta = Date.now() - new Date(value).getTime();
  const minutes = Math.floor(delta / 60_000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} 天前`;
  return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric" }).format(new Date(value));
}

export function formatDuration(durationMs?: number) {
  if (durationMs == null) return "";
  if (durationMs < 1_000) return `${Math.max(0, Math.round(durationMs))}ms`;
  if (durationMs < 60_000) return `${(durationMs / 1_000).toFixed(1)}s`;
  return `${Math.floor(durationMs / 60_000)}m ${Math.round((durationMs % 60_000) / 1_000)}s`;
}

export function formatNumber(value?: number | null) {
  if (value == null) return "不可用";
  return new Intl.NumberFormat("zh-CN", { notation: value > 99_999 ? "compact" : "standard" }).format(value);
}

export function tokenTotal(usage?: TokenUsage | null) {
  if (!usage || usage.available === false) return null;
  if (usage.total_tokens != null) return usage.total_tokens;
  const known = [usage.input_tokens, usage.output_tokens].filter((item): item is number => item != null);
  return known.length ? known.reduce((sum, value) => sum + value, 0) : null;
}

export const statusLabel: Record<RunStatus, string> = {
  queued: "排队中",
  running: "运行中",
  waiting_approval: "等待审批",
  cancelling: "正在停止",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

export function isRunActive(status?: RunStatus | null) {
  return status === "queued" || status === "running" || status === "waiting_approval" || status === "cancelling";
}

export function asRecord(value: unknown): Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

export function asString(value: unknown, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

export function asNumber(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

export function prettyJson(value: unknown) {
  if (value == null || value === "") return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
