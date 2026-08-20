import { useMemo, useState } from "react";
import { Check, Circle, CircleDashed, ListChecks, Play, Plus, Share2, X } from "lucide-react";
import type { TaskList, TaskResource } from "@/types/api";
import { cx } from "@/lib/utils";
import { Spinner } from "@/components/ui";

type Props = {
  tasks: TaskResource[];
  loading?: boolean;
  busy?: boolean;
  taskList?: TaskList;
  taskLists?: TaskList[];
  onContinue: (task: TaskResource) => void;
  onCreate: (input: { subject: string; description: string; activeForm?: string }) => void;
  onSelectList?: (taskListId: string) => void;
  onPromote?: () => void;
};

export function TaskPlan(props: Props) {
  const [creating, setCreating] = useState(false);
  const [subject, setSubject] = useState("");
  const [description, setDescription] = useState("");
  const groups = useMemo(() => {
    const completedIds = new Set(props.tasks.filter((item) => item.task.status === "completed").map((item) => item.task.id));
    const blocked = (item: TaskResource) => item.task.status === "pending" && item.task.blockedBy.some((id) => !completedIds.has(id));
    return [
      { key: "active", label: "进行中", items: props.tasks.filter((item) => item.task.status === "in_progress") },
      { key: "ready", label: "Ready", items: props.tasks.filter((item) => item.task.status === "pending" && !blocked(item)) },
      { key: "blocked", label: "Blocked", items: props.tasks.filter(blocked) },
      { key: "completed", label: "已完成", items: props.tasks.filter((item) => item.task.status === "completed") },
    ];
  }, [props.tasks]);

  const submit = () => {
    const cleanSubject = subject.trim();
    const cleanDescription = description.trim();
    if (!cleanSubject || !cleanDescription) return;
    props.onCreate({ subject: cleanSubject, description: cleanDescription });
    setSubject("");
    setDescription("");
    setCreating(false);
  };

  if (props.loading) return <div className="flex items-center justify-center gap-2 py-10 text-xs text-ink-muted"><Spinner /> 加载任务…</div>;

  return (
    <div className="space-y-4 px-4 py-5">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0 flex-1"><select value={props.taskList?.id ?? ""} onChange={(event) => props.onSelectList?.(event.target.value)} className="h-8 w-full rounded-lg border border-line bg-surface px-2 text-xs font-semibold text-ink outline-none"><option value={props.taskList?.id ?? ""}>{props.taskList?.name || "当前任务列表"}</option>{props.taskLists?.filter((item) => item.id !== props.taskList?.id && item.scope === "workspace_shared").map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select><div className="mt-1 text-[10px] text-ink-faint">{props.tasks.length} 个任务 · 当前会话直接执行</div></div>
        {props.taskList?.scope === "conversation_private" && <button type="button" onClick={props.onPromote} title="提升为工作区共享列表" className="grid size-8 shrink-0 place-items-center rounded-xl border border-line text-ink-muted hover:bg-surface-muted hover:text-ink"><Share2 className="size-3.5" /></button>}
        <button type="button" onClick={() => setCreating((value) => !value)} className="grid size-8 shrink-0 place-items-center rounded-xl border border-line text-ink-muted hover:bg-surface-muted hover:text-ink">{creating ? <X className="size-3.5" /> : <Plus className="size-3.5" />}</button>
      </div>
      {creating && <div className="space-y-2 rounded-2xl border border-line bg-surface-muted p-3"><input value={subject} onChange={(event) => setSubject(event.target.value)} placeholder="任务标题" className="h-9 w-full rounded-xl border border-line bg-surface px-3 text-xs text-ink outline-none focus:border-accent/40" /><textarea value={description} onChange={(event) => setDescription(event.target.value)} placeholder="背景、要求和完成条件" rows={4} className="w-full resize-none rounded-xl border border-line bg-surface px-3 py-2 text-xs leading-5 text-ink outline-none focus:border-accent/40" /><button type="button" disabled={!subject.trim() || !description.trim() || props.busy} onClick={submit} className="h-8 w-full rounded-xl bg-accent text-xs font-medium text-white disabled:opacity-40">创建任务</button></div>}
      {!props.tasks.length && !creating && <div className="rounded-2xl border border-dashed border-line px-4 py-10 text-center text-xs text-ink-faint"><ListChecks className="mx-auto mb-2 size-5" />Agent 尚未创建任务</div>}
      {groups.map((group) => group.items.length > 0 && <section key={group.key}><div className="mb-2 flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.12em] text-ink-muted"><span>{group.label}</span><span className="ml-auto rounded-full bg-surface-strong px-1.5 py-0.5 font-mono text-[8px] text-ink-faint">{group.items.length}</span></div><div className="space-y-1.5">{group.items.map((resource) => <TaskCard key={resource.task.id} resource={resource} blocked={group.key === "blocked"} disabled={Boolean(props.busy)} onContinue={() => props.onContinue(resource)} />)}</div></section>)}
    </div>
  );
}

function TaskCard({ resource, blocked, disabled, onContinue }: { resource: TaskResource; blocked: boolean; disabled: boolean; onContinue: () => void }) {
  const task = resource.task;
  const done = task.status === "completed";
  const active = task.status === "in_progress";
  return <article className="rounded-xl border border-line px-3 py-2.5"><div className="flex items-start gap-2"><span className={cx("mt-0.5 grid size-4 shrink-0 place-items-center rounded-full border", done ? "border-success/30 bg-success/10 text-success" : active ? "border-accent/30 bg-accent/10 text-accent" : blocked ? "border-warning/30 bg-warning/10 text-warning" : "border-line text-ink-faint")}>{done ? <Check className="size-2.5" /> : blocked ? <CircleDashed className="size-2.5" /> : <Circle className="size-2.5" />}</span><div className="min-w-0 flex-1"><div className={cx("text-[11px] font-medium leading-4", done ? "text-ink-faint line-through" : "text-ink")}><span className="mr-1 font-mono text-ink-faint">#{task.id}</span>{task.subject}</div><p className="mt-1 line-clamp-2 text-[10px] leading-4 text-ink-muted">{task.description}</p>{task.blockedBy.length > 0 && <div className="mt-1 text-[9px] text-ink-faint">依赖：{task.blockedBy.map((id) => `#${id}`).join("、")}</div>}{task.owner && <div className="mt-1 truncate text-[9px] text-ink-faint" title={task.owner}>owner: {task.owner}</div>}</div>{!done && !blocked && <button type="button" disabled={disabled} onClick={onContinue} title="在当前会话继续处理" className="grid size-7 shrink-0 place-items-center rounded-lg text-accent hover:bg-accent/10 disabled:opacity-30"><Play className="size-3 fill-current" /></button>}</div></article>;
}
