import { Archive, Bot, FolderGit2, MessageSquare, Plus, Search, Trash2, X } from "lucide-react";
import type { Conversation } from "@/types/api";
import { cx, formatRelativeTime, isRunActive, statusLabel } from "@/lib/utils";
import { IconButton, Skeleton, StatusDot } from "@/components/ui";

type Props = {
  conversations: Conversation[];
  selectedId?: string;
  search: string;
  loading?: boolean;
  creating?: boolean;
  mobile?: boolean;
  onSearch: (value: string) => void;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onArchive: (conversation: Conversation) => void;
  onDelete: (conversation: Conversation) => void;
  deletingId?: string;
  onClose?: () => void;
};

export function ConversationSidebar(props: Props) {
  return (
    <aside className="flex h-full min-h-0 w-full flex-col bg-sidebar text-sidebar-ink">
      <header className="flex h-16 shrink-0 items-center justify-between px-4">
        <div className="flex min-w-0 items-center gap-2.5">
          <div className="grid size-8 shrink-0 place-items-center rounded-xl bg-accent text-white shadow-lg shadow-accent/20">
            <Bot className="size-4.5" />
          </div>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold tracking-tight">CodeAgent</div>
            <div className="text-[10px] uppercase tracking-[0.18em] text-sidebar-muted">Local Cockpit</div>
          </div>
        </div>
        {props.mobile && props.onClose && (
          <IconButton label="关闭会话栏" onClick={props.onClose} className="text-sidebar-muted hover:bg-white/5 hover:text-white">
            <X className="size-4" />
          </IconButton>
        )}
      </header>

      <div className="space-y-3 px-3 pb-3">
        <button
          type="button"
          onClick={props.onCreate}
          disabled={props.creating}
          className="flex h-10 w-full items-center justify-center gap-2 rounded-xl bg-accent px-3 text-sm font-medium text-white shadow-md shadow-accent/15 transition hover:bg-accent-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/40 disabled:opacity-60"
        >
          <Plus className="size-4" /> 新建会话
        </button>
        <label className="flex h-9 items-center gap-2 rounded-xl border border-white/[0.07] bg-white/[0.04] px-3 text-sidebar-muted transition focus-within:border-white/15 focus-within:bg-white/[0.06]">
          <Search className="size-3.5 shrink-0" />
          <input
            value={props.search}
            onChange={(event) => props.onSearch(event.target.value)}
            placeholder="搜索会话"
            aria-label="搜索会话"
            className="min-w-0 flex-1 bg-transparent text-xs text-sidebar-ink outline-none placeholder:text-sidebar-muted/70"
          />
        </label>
      </div>

      <div className="flex min-h-0 flex-1 flex-col">
        <div className="px-4 pb-2 pt-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-sidebar-muted">最近会话</div>
        <nav className="scrollbar-thin flex-1 space-y-0.5 overflow-y-auto px-2 pb-4" aria-label="会话列表">
          {props.loading && Array.from({ length: 5 }, (_, index) => <Skeleton key={index} className="mb-1 h-14 bg-white/[0.05]" />)}
          {!props.loading && props.conversations.length === 0 && (
            <div className="mx-2 mt-5 rounded-xl border border-dashed border-white/10 px-4 py-6 text-center text-xs leading-5 text-sidebar-muted">
              <MessageSquare className="mx-auto mb-2 size-5 opacity-70" />
              {props.search ? "没有匹配的会话" : "还没有会话，创建一个开始工作"}
            </div>
          )}
          {props.conversations.map((conversation) => {
            const active = props.selectedId === conversation.id;
            const running = isRunActive(conversation.run_status);
            return (
              <div
                key={conversation.id}
                className={cx(
                  "group relative flex rounded-xl transition",
                  active ? "bg-white/[0.09] text-white" : "text-sidebar-ink hover:bg-white/[0.05]",
                )}
              >
                <button
                  type="button"
                  onClick={() => props.onSelect(conversation.id)}
                  className="min-w-0 flex-1 px-3 py-2.5 text-left focus-visible:rounded-xl focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                >
                  <span className="flex items-center gap-2 pr-14">
                    <span className="truncate text-xs font-medium">{conversation.title || "新会话"}</span>
                    {conversation.run_status && (
                      <StatusDot
                        status={running ? (conversation.waiting_for_answer || conversation.run_status === "waiting_approval" ? "warning" : "running") : conversation.run_status === "failed" ? "error" : "success"}
                        pulse={running}
                      />
                    )}
                    {conversation.waiting_for_answer && <span className="shrink-0 text-[10px] text-amber-400">待回答</span>}
                  </span>
                  <span className="mt-1 flex items-center justify-between gap-2 text-[10px] text-sidebar-muted">
                    <span className="truncate">{conversation.last_message || (conversation.run_status ? statusLabel[conversation.run_status] : "等待消息")}</span>
                    <span className="shrink-0">{formatRelativeTime(conversation.updated_at)}</span>
                  </span>
                  <span className="mt-1 flex min-w-0 items-center gap-1 text-[9px] text-sidebar-muted/70" title={conversation.workspace}>
                    <FolderGit2 className="size-2.5 shrink-0" /><span className="truncate">{workspaceName(conversation.workspace)}</span>
                  </span>
                </button>
                <div className={cx("absolute right-2 top-2 flex gap-1 rounded-lg bg-sidebar transition group-hover:opacity-100 group-focus-within:opacity-100", props.mobile ? "opacity-100" : "opacity-0")}>
                  <button
                    type="button"
                    aria-label={`归档 ${conversation.title}`}
                    title="归档"
                    onClick={() => props.onArchive(conversation)}
                    disabled={props.deletingId === conversation.id}
                    className="grid size-7 place-items-center rounded-lg text-sidebar-muted transition hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-40"
                  >
                    <Archive className="size-3.5" />
                  </button>
                  <button
                    type="button"
                    aria-label={`删除 ${conversation.title}`}
                    title={running ? "请先停止任务再删除" : "删除会话"}
                    disabled={running || Boolean(props.deletingId)}
                    onClick={() => props.onDelete(conversation)}
                    className="grid size-7 place-items-center rounded-lg text-sidebar-muted transition hover:text-danger focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    <Trash2 className="size-3.5" />
                  </button>
                </div>
              </div>
            );
          })}
        </nav>
      </div>
      <footer className="border-t border-white/[0.06] px-4 py-3 text-[10px] text-sidebar-muted">数据仅保存在本机</footer>
    </aside>
  );
}

function workspaceName(path: string) {
  const parts = path.split(/[\\/]+/).filter(Boolean);
  return parts.at(-1) || path || "未绑定工作区";
}
