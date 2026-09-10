import { useEffect, useRef, useState } from "react";
import { Bot, CircleStop, Menu, Monitor, Moon, PanelRight, Plug, Send, Sparkles, Square, Sun, Users, Wifi, WifiOff } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Approval, ApprovalDecision, Message, RunStatus } from "@/types/api";
import type { RunViewState } from "@/store/runStore";
import { cx, formatTime, isRunActive, statusLabel } from "@/lib/utils";
import { ActionCard } from "@/components/ActionCard";
import { ApprovalBanner } from "@/components/ApprovalBanner";
import { EmptyPanel, IconButton, Spinner, StatusDot } from "@/components/ui";
import { RunTimeline } from "@/components/RunTimeline";

type Props = {
  title?: string;
  messages: Message[];
  loading?: boolean;
  run?: RunViewState;
  draft: string;
  sending?: boolean;
  cancelling?: boolean;
  approval?: Approval;
  approvalBusy?: boolean;
  runtimeModel?: string | null;
  teamAvailable?: boolean;
  useTeam?: boolean;
  teamLeadActive?: boolean;
  workspace?: string;
  theme: "system" | "light" | "dark";
  onDraft: (value: string) => void;
  onUseTeam: (value: boolean) => void;
  onSend: () => void;
  onCancel: () => void;
  onApprovalDecision: (decision: ApprovalDecision) => void;
  onOpenLeft: () => void;
  onOpenRight: () => void;
  onOpenMcp: () => void;
  onToggleTheme: () => void;
};

export function ChatWorkspace(props: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const [following, setFollowing] = useState(true);
  const actions = props.run ? props.run.actionOrder.map((id) => props.run?.actions[id]).filter((item): item is NonNullable<typeof item> => Boolean(item)) : [];
  const active = isRunActive(props.run?.status);
  const showStreaming = Boolean(props.run?.streamingText) && !props.messages.some((message) => message.run_id === props.run?.runId && message.role === "assistant" && message.content === props.run?.streamingText);

  useEffect(() => {
    if (!following) return;
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [props.messages, props.run?.streamingText, props.run?.actionOrder.length, following]);

  useEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  }, [props.draft]);

  const onKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      props.onSend();
    }
  };

  return (
    <main className="flex min-h-0 min-w-0 flex-1 flex-col bg-canvas">
      <header className="flex h-16 shrink-0 items-center gap-3 border-b border-line bg-surface/85 px-3 backdrop-blur-xl sm:px-5">
        <IconButton label="打开会话列表" onClick={props.onOpenLeft} className="lg:hidden"><Menu className="size-4" /></IconButton>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-sm font-semibold text-ink">{props.title || "CodeAgent"}</h1>
          <div className="mt-0.5 flex min-w-0 items-center gap-2 text-[10px] text-ink-muted">
            {props.run ? (
              <>
                <StatusDot status={active ? props.run.status === "waiting_approval" ? "warning" : "running" : props.run.status === "failed" ? "error" : "success"} pulse={active} />
                <span>{statusLabel[props.run.status]}</span>
                <span aria-hidden>·</span>
                {props.run.connection === "live" ? <Wifi className="size-3 text-success" /> : props.run.connection === "reconnecting" ? <><Spinner className="size-3" /><span>重连中</span></> : <WifiOff className="size-3" />}
              </>
            ) : (
              <><span className="size-1.5 rounded-full bg-success" /><span className="truncate">{props.runtimeModel || "本地 Agent"}</span></>
            )}
          </div>
        </div>
        {props.workspace && <div className="hidden max-w-52 truncate rounded-lg border border-line bg-surface-muted px-2.5 py-1 font-mono text-[9px] text-ink-muted md:block" title={props.workspace}>{props.workspace}</div>}
        <IconButton label="配置 MCP 插件" onClick={props.onOpenMcp}><Plug className="size-4" /></IconButton>
        <IconButton label={`当前主题：${props.theme === "system" ? "跟随系统" : props.theme === "light" ? "浅色" : "深色"}，点击切换`} onClick={props.onToggleTheme}>
          {props.theme === "system" ? <Monitor className="size-4" /> : props.theme === "light" ? <Sun className="size-4" /> : <Moon className="size-4" />}
        </IconButton>
        <IconButton label="打开运行面板" onClick={props.onOpenRight} className="xl:hidden"><PanelRight className="size-4" /></IconButton>
      </header>

      {props.run && <RunTimeline run={props.run} />}

      <div
        ref={scrollRef}
        onScroll={(event) => {
          const target = event.currentTarget;
          setFollowing(target.scrollHeight - target.scrollTop - target.clientHeight < 120);
        }}
        className="scrollbar-thin min-h-0 flex-1 overflow-y-auto"
      >
        {!props.loading && props.messages.length === 0 && !props.run && (
          <div className="grid min-h-full place-items-center">
            <EmptyPanel icon={<Sparkles className="size-5" />} title="准备开始编码" body="描述你希望完成的工作。Agent 的模型调用、工具、TODO、子 Agent 和恢复过程都会在这里实时呈现。" />
          </div>
        )}
        {props.loading && <div className="flex min-h-full items-center justify-center gap-2 text-xs text-ink-muted"><Spinner /> 加载会话…</div>}
        {!props.loading && (props.messages.length > 0 || props.run) && (
          <div className="mx-auto w-full max-w-4xl px-4 py-6 sm:px-7 sm:py-8">
            <div className="space-y-6">
              {props.messages.map((message) => <ChatMessage key={message.id} message={message} />)}
              {actions.length > 0 && (
                <section className="ml-0 space-y-2 sm:ml-11" aria-label="Agent 动作">
                  <div className="mb-2 flex items-center gap-2 px-1 text-[10px] font-bold uppercase tracking-[0.16em] text-ink-faint"><Bot className="size-3.5" /> Agent actions</div>
                  {actions.map((action) => <ActionCard key={action.id} action={action} />)}
                </section>
              )}
              {showStreaming && (
                <ChatMessage
                  message={{ id: `${props.run?.runId}:stream`, conversation_id: "", role: "assistant", content: props.run?.streamingText ?? "", created_at: new Date().toISOString(), status: "streaming" }}
                />
              )}
              {active && !showStreaming && actions.length === 0 && (
                <div className="flex items-center gap-3 text-xs text-ink-muted"><span className="grid size-8 place-items-center rounded-xl bg-accent/10 text-accent"><Spinner /></span> Agent 正在准备…</div>
              )}
              {props.run?.error && <div className="rounded-xl border border-danger/25 bg-danger/5 px-4 py-3 text-xs text-danger">{props.run.error}</div>}
            </div>
          </div>
        )}
      </div>

      <div className="shrink-0 bg-gradient-to-t from-canvas via-canvas to-transparent px-3 pb-3 pt-2 sm:px-5 sm:pb-5">
        {!following && (
          <button type="button" onClick={() => scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" })} className="mx-auto mb-2 block rounded-full border border-line bg-surface px-3 py-1 text-[10px] text-ink-muted shadow-sm hover:text-ink">回到最新消息</button>
        )}
        <div className="mx-auto max-w-4xl space-y-2">
          {props.approval && (
            <ApprovalBanner
              approval={props.approval}
              busy={props.approvalBusy}
              onDecision={props.onApprovalDecision}
            />
          )}
          <div className="rounded-2xl border border-line-strong bg-surface p-2 shadow-panel transition focus-within:border-accent/35 focus-within:ring-4 focus-within:ring-accent/[0.06]">
          <textarea
            ref={inputRef}
            value={props.draft}
            onChange={(event) => props.onDraft(event.target.value)}
            onKeyDown={onKeyDown}
            rows={1}
            disabled={active}
            placeholder={active ? "Agent 正在工作…" : props.teamLeadActive ? "向 Root / Lead 发送团队指令…" : "告诉 CodeAgent 你想实现什么…"}
            aria-label="发送消息"
            className="scrollbar-thin min-h-11 w-full resize-none bg-transparent px-2.5 py-2 text-sm leading-6 text-ink outline-none placeholder:text-ink-faint disabled:cursor-not-allowed disabled:opacity-60"
          />
          <div className="flex items-center justify-between gap-3 px-1 pt-1">
            <div className="flex items-center gap-2">
              <button
                type="button"
                disabled={active || props.teamLeadActive || !props.teamAvailable}
                onClick={() => props.onUseTeam(!props.useTeam)}
                aria-pressed={Boolean(props.useTeam)}
                className={cx(
                  "inline-flex h-8 items-center gap-1.5 rounded-lg border px-2.5 text-[10px] font-medium transition disabled:cursor-not-allowed disabled:opacity-50",
                  props.teamLeadActive || props.useTeam
                    ? "border-accent/30 bg-accent/10 text-accent"
                    : "border-line bg-surface-muted text-ink-muted hover:text-ink",
                )}
              >
                <Users className="size-3.5" />
                {props.teamLeadActive ? "Team 运行中" : props.useTeam ? "Agent Team 已开启" : "Agent Team"}
              </button>
              <div className="text-[9px] text-ink-faint"><kbd className="rounded border border-line bg-surface-muted px-1 py-0.5 font-sans">Enter</kbd> 发送 · <kbd className="rounded border border-line bg-surface-muted px-1 py-0.5 font-sans">Shift Enter</kbd> 换行</div>
            </div>
            {active ? (
              <button type="button" onClick={props.onCancel} disabled={props.cancelling || props.run?.status === "cancelling"} className="inline-flex h-9 items-center gap-2 rounded-xl border border-danger/20 bg-danger/5 px-3 text-xs font-medium text-danger transition hover:bg-danger/10 disabled:opacity-50">
                {props.cancelling || props.run?.status === "cancelling" ? <><Spinner className="size-3.5 text-danger" /> 等待当前步骤结束</> : <><Square className="size-3.5 fill-current" /> 停止</>}
              </button>
            ) : (
              <button type="button" onClick={props.onSend} disabled={!props.draft.trim() || props.sending} className="grid size-9 place-items-center rounded-xl bg-accent text-white shadow-md shadow-accent/20 transition hover:bg-accent-strong disabled:cursor-not-allowed disabled:opacity-40">
                {props.sending ? <Spinner className="text-white" /> : <Send className="size-4" />}
                <span className="sr-only">发送</span>
              </button>
            )}
          </div>
          </div>
        </div>
        {props.run?.status === "cancelling" && <div className="mt-2 flex items-center justify-center gap-1.5 text-[10px] text-ink-muted"><CircleStop className="size-3" /> 取消将在当前安全边界生效</div>}
      </div>
    </main>
  );
}

function ChatMessage({ message }: { message: Message }) {
  const user = message.role === "user";
  if (message.role === "system") return <div className="mx-auto max-w-lg rounded-full border border-line bg-surface-muted px-3 py-1 text-center text-[10px] text-ink-muted">{message.content}</div>;
  return (
    <article className={cx("flex gap-3", user && "justify-end")}>
      {!user && <div className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-xl border border-accent/20 bg-accent/10 text-accent"><Bot className="size-4" /></div>}
      <div className={cx("min-w-0 max-w-[88%] sm:max-w-[82%]", user && "rounded-2xl rounded-br-md bg-user-bubble px-4 py-2.5 text-user-bubble-ink shadow-sm")}>
        {user ? (
          <p className="whitespace-pre-wrap break-words text-sm leading-6">{message.content}</p>
        ) : (
          <div className="markdown text-sm leading-7 text-ink"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>{message.status === "streaming" && <span className="ml-1 inline-block h-4 w-0.5 animate-pulse bg-accent align-middle motion-reduce:animate-none" />}</div>
        )}
        <div className={cx("mt-1 text-[9px]", user ? "text-user-bubble-ink/55" : "text-ink-faint")}>{formatTime(message.created_at)}</div>
      </div>
    </article>
  );
}
