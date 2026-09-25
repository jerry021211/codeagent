import { memo, useId, useMemo, useState } from "react";
import { Check, ChevronDown, ChevronRight, CircleAlert, CircleMinus, FileCode2, FilePenLine, FolderSearch, RotateCcw, Search, Terminal, Users } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { RunViewState } from "@/store/runStore";
import type { RunAction } from "@/types/api";
import { buildProcessPresentation, processOutputText, type ProcessEntry } from "@/lib/processPresentation";
import { asRecord, cx, formatDuration, isRunActive, statusLabel } from "@/lib/utils";
import { Spinner } from "@/components/ui";

const RECENT_STEPS = 12;
const focus = "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50";

export const ThinkingProcess = memo(function ThinkingProcess({ run }: { run: RunViewState }) {
  const [expanded, setExpanded] = useState(false);
  const [showEarlier, setShowEarlier] = useState(false);
  const contentId = useId();
  const { entries, summary } = useMemo(() => buildProcessPresentation(run), [run.events, run.actions, run.actionOrder, run.agents, run.status]);
  const active = isRunActive(run.status);
  const failed = run.status === "failed" || run.status === "interrupted";
  const waiting = run.status === "waiting_approval";
  const visible = showEarlier ? entries : entries.slice(-RECENT_STEPS);
  const earlierCount = entries.length - visible.length;
  const activity = [...entries].reverse().find((entry) => entry.status === "running" || entry.status === "waiting");
  const metrics = [
    summary.readCount && `读取 ${summary.readCount} 次`,
    summary.searchCount + summary.listCount && `搜索 ${summary.searchCount + summary.listCount} 次`,
    summary.editCount && `编辑 ${summary.editCount} 次`,
    summary.commandCount && `命令 ${summary.commandCount} 次`,
    summary.agentCount && `${summary.agentCount} 个子任务`,
  ].filter(Boolean);

  return (
    <section className="min-w-0 sm:ml-11" aria-label="执行过程">
      <button type="button" aria-expanded={expanded} aria-controls={contentId} onClick={() => setExpanded((value) => !value)}
        className={cx("group flex max-w-full items-center gap-2 rounded-md py-1.5 text-xs text-ink-muted transition hover:text-ink", focus)}>
        {active ? <Spinner className={cx("size-3.5", waiting && "text-warning")} /> : failed ? <CircleAlert className="size-3.5 text-danger" /> : run.status === "cancelled" ? <CircleMinus className="size-3.5" /> : <Check className="size-3.5 text-ink-muted" />}
        <span className="font-medium">Thinking</span>
        <span className="truncate text-[11px] text-ink-muted/75">
          {summary.toolCount > 0 && `· ${summary.toolCount} 次工具调用 `}
          {!active && summary.durationMs != null && `· ${formatDuration(summary.durationMs)}`}
        </span>
        <ChevronDown aria-hidden className={cx("ml-0.5 size-3.5 shrink-0 text-ink-faint transition-transform motion-reduce:transition-none", !expanded && "-rotate-90")} />
      </button>
      {(failed || waiting || run.status === "cancelled" || run.status === "cancelling" || summary.failureCount > 0 || summary.unknownCount > 0) && (
        <p className={cx("mt-0.5 pl-[22px] text-[11px]", failed ? "text-danger" : waiting || summary.failureCount || summary.unknownCount ? "text-warning" : "text-ink-muted")}>
          {failed || waiting || run.status === "cancelled" || run.status === "cancelling" ? statusLabel[run.status] : [summary.failureCount && `${summary.failureCount} 项操作未成功`, summary.unknownCount && `${summary.unknownCount} 项结果未知`].filter(Boolean).join(" · ") + (expanded ? "" : "，展开查看")}
        </p>
      )}
      {active && !expanded && !waiting && run.status !== "cancelling" && (
        <p className="mt-0.5 truncate pl-[22px] text-xs text-ink-muted" aria-live="polite">{run.status === "queued" ? "等待开始…" : activity ? `${activity.label}${activity.target ? ` · ${activity.target}` : ""}` : "正在组织回复…"}</p>
      )}
      {expanded && (
        <div id={contentId} className="mt-2 pb-2">
          {metrics.length > 0 && <div className="mb-4 flex flex-wrap gap-x-3 gap-y-1 pl-[22px] text-[10px] text-ink-muted">{metrics.map((metric) => <span key={String(metric)}>{metric}</span>)}</div>}
          {earlierCount > 0 && <button type="button" onClick={() => setShowEarlier(true)} className={cx("mb-3 ml-[22px] rounded py-1 text-[11px] text-ink-muted transition hover:text-accent", focus)}>显示更早的 {earlierCount} 项活动</button>}
          {entries.length > 0 ? (
            <ol className="process-trace space-y-3">
              {visible.map((entry) => <ProcessStep key={entry.id} entry={entry} active={active} />)}
            </ol>
          ) : <p className="border-l border-line py-1 pl-4 text-xs leading-6 text-ink-muted">{active ? "正在处理你的请求，执行活动会显示在这里。" : summary.toolCount === 0 && run.status === "completed" ? "本次直接生成了回复，没有调用工具。" : "暂无可展示的执行活动。"}</p>}
          {showEarlier && entries.length > RECENT_STEPS && <button type="button" onClick={() => setShowEarlier(false)} className={cx("ml-[22px] mt-3 rounded py-1 text-[11px] text-ink-muted hover:text-accent", focus)}>仅显示最近 {RECENT_STEPS} 项活动</button>}
        </div>
      )}
    </section>
  );
});

function StepIcon({ entry, active }: { entry: ProcessEntry; active: boolean }) {
  if (active && entry.status === "running") return <Spinner className="size-3.5" />;
  if (entry.status === "failed" || entry.status === "blocked" || entry.status === "waiting" || entry.status === "unknown") return <CircleAlert className={cx("size-3.5", entry.status === "failed" ? "text-danger" : "text-warning")} />;
  const Icon = entry.kind === "exploration" ? FolderSearch : entry.kind === "subagent" ? Users : entry.kind === "recovery" ? RotateCcw
    : isCommand(entry) ? Terminal : /修改|写入|应用修改/.test(entry.label) ? FilePenLine : /搜索|查找/.test(entry.label) ? Search : FileCode2;
  return <Icon className="size-3.5" />;
}

function isCommand(entry: ProcessEntry) { return entry.label === "运行命令"; }
function explorationLabel(entry: ProcessEntry) {
  return [entry.readCount && `读取 ${entry.readCount} 次`, entry.searchCount && `${entry.searchCount} 次内容搜索`, entry.listCount && `${entry.listCount} 次文件查找`].filter(Boolean).join("、");
}

const ProcessStep = memo(function ProcessStep({ entry, active }: { entry: ProcessEntry; active: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const detailId = useId();
  const narration = entry.kind === "narration";
  const longText = Boolean(entry.text && entry.text.length > 360);
  const error = entry.status === "failed" || entry.status === "blocked";
  const hasDetails = entry.kind === "exploration" || Boolean(entry.outputText || entry.inputText && entry.inputText !== entry.target);
  const label = entry.kind === "exploration" ? explorationLabel(entry) : entry.label;

  return (
    <li className="process-step relative min-w-0 pl-[22px] text-xs leading-5">
      <span className="absolute left-0 top-0.5 z-[1] grid size-3.5 place-items-center bg-canvas text-ink-muted" aria-hidden>
        {narration ? <span className="size-1 rounded-full bg-ink-faint" /> : <StepIcon entry={entry} active={active} />}
      </span>
      {entry.agentLabel && <div className="mb-1 inline-flex items-center gap-1 text-[10px] text-accent"><Users className="size-3" />{entry.agentLabel}</div>}
      {narration ? (
        <div>
          <div className={cx("markdown process-narration break-words text-ink-muted", longText && !expanded && "line-clamp-3")}><ReactMarkdown remarkPlugins={[remarkGfm]}>{entry.text}</ReactMarkdown></div>
          {longText && <button type="button" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded} className={cx("mt-1 rounded text-[11px] text-ink-muted hover:text-accent", focus)}>{expanded ? "收起说明" : "展开说明"}</button>}
        </div>
      ) : (
        <>
          <div className="flex items-start gap-2">
            <button type="button" disabled={!hasDetails} aria-expanded={hasDetails ? expanded : undefined} aria-controls={hasDetails ? detailId : undefined} onClick={() => setExpanded((value) => !value)}
              className={cx("group min-w-0 flex-1 rounded text-left disabled:cursor-default", hasDetails && "hover:text-ink", focus)}>
              <span className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
                <span className={cx("font-medium", error ? "text-danger" : "text-ink-muted")}>{label}</span>
                {hasDetails && <ChevronRight aria-hidden className={cx("size-3 text-ink-faint transition-transform motion-reduce:transition-none", expanded && "rotate-90")} />}
                {entry.status === "blocked" && <span className="text-[10px] text-warning">已阻止</span>}
                {entry.status === "failed" && <span className="text-[10px] text-danger">失败</span>}
                {entry.status === "waiting" && <span className="text-[10px] text-warning">等待确认</span>}
                {entry.status === "cancelled" && <span className="text-[10px] text-ink-muted">已取消</span>}
                {entry.status === "unknown" && <span className="text-[10px] text-warning">结果未知</span>}
                {entry.status === "queued" && <span className="text-[10px] text-ink-muted">等待执行</span>}
                {active && entry.status === "running" && <span className="text-[10px] text-accent">进行中</span>}
              </span>
              {entry.target && <span title={entry.target} className={cx("mt-0.5 block truncate font-mono text-[11px]", isCommand(entry) ? "text-ink" : "text-ink-muted/80")}>{isCommand(entry) && <span className="mr-1.5 text-ink-faint">$</span>}{entry.target}</span>}
            </button>
            {entry.durationMs != null && <span className="shrink-0 text-[10px] tabular-nums text-ink-faint">{formatDuration(entry.durationMs)}</span>}
          </div>
          {entry.error && <div className={cx("mt-2 rounded-md border-l-2 px-3 py-2 text-[11px]", entry.status === "unknown" || entry.status === "waiting" ? "border-warning/50 bg-warning/5 text-warning" : "border-danger/50 bg-danger/5 text-danger")}><ExpandableText text={entry.error} /></div>}
          {expanded && hasDetails && (
            <div id={detailId} className="mt-2">
              {entry.kind === "exploration" && entry.actions.length > 1 ? <div className="space-y-2 border-l border-line pl-3">{entry.actions.map((action) => <ExplorationResult key={action.id} action={action} />)}</div> : (
                <>
                  {entry.inputText && entry.inputText !== entry.target && <pre className="mb-2 whitespace-pre-wrap break-words font-mono text-[11px] text-ink-muted">{isCommand(entry) ? "$ " : ""}{entry.inputText}</pre>}
                  {entry.outputText && entry.outputText !== entry.error && <Output text={entry.outputText} truncated={entry.outputTruncated} />}
                  {!entry.outputText && <p className="text-[11px] text-ink-muted">{entry.status === "running" ? "等待结果…" : "该操作没有文本结果。"}</p>}
                </>
              )}
            </div>
          )}
        </>
      )}
    </li>
  );
});

function ExplorationResult({ action }: { action: RunAction }) {
  const [open, setOpen] = useState(false);
  const input = asRecord(action.input);
  const target = input.file_path ?? input.path ?? input.pattern ?? input.query ?? action.subtitle ?? action.title;
  const output = open ? processOutputText(action.output) : undefined;
  return <div className="min-w-0">
    <button type="button" onClick={() => setOpen((value) => !value)} aria-expanded={open} className={cx("flex max-w-full items-center gap-1.5 rounded text-left text-[11px] text-ink-muted hover:text-ink", focus)}>
      <ChevronRight className={cx("size-3 shrink-0", open && "rotate-90")} /><span className="truncate font-mono">{String(target)}</span>
    </button>
    {open && <div className="mt-2">{output ? <Output text={output} truncated={asRecord(action.output).truncated === true} /> : <p className="text-[11px] text-ink-muted">该操作没有文本结果。</p>}</div>}
  </div>;
}

function ExpandableText({ text }: { text: string }) {
  const [full, setFull] = useState(false);
  const lines = text.split("\n");
  const preview = lines.slice(0, 14).join("\n").slice(0, 1800);
  const clipped = preview.length < text.length;
  return <>
    <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-5">{full ? text : preview}</pre>
    {clipped && <button type="button" onClick={() => setFull((value) => !value)} aria-expanded={full} className={cx("mt-2 rounded text-[10px] font-medium text-accent", focus)}>{full ? "收起输出" : `展开输出${lines.length > 14 ? `（${lines.length} 行）` : ""}`}</button>}
  </>;
}

function Output({ text, truncated }: { text: string; truncated?: boolean }) {
  return <div className="overflow-hidden rounded-lg border border-line/80 bg-surface/70 px-3 py-2 text-ink-muted"><ExpandableText text={text} />{truncated && <p className="mt-2 text-[10px] text-ink-faint">工具返回的内容已截断</p>}</div>;
}
