import { useId, useMemo, useState } from "react";
import { GitFork } from "lucide-react";
import type { TaskResource } from "@/types/api";
import { cx } from "@/lib/utils";
import { isTaskBlocked } from "@/lib/tasks";

const NODE_WIDTH = 148;
const NODE_HEIGHT = 72;
const COLUMN_GAP = 52;
const ROW_GAP = 14;
const PADDING = 12;

type PositionedTask = {
  resource: TaskResource;
  x: number;
  y: number;
};

type GraphEdge = {
  sourceId: string;
  targetId: string;
  path: string;
};

export function TaskDependencyGraph({ tasks }: { tasks: TaskResource[] }) {
  const markerId = `task-arrow-${useId().replaceAll(":", "")}`;
  const completedIds = useMemo(
    () => new Set(tasks.filter((item) => item.task.status === "completed").map((item) => item.task.id)),
    [tasks],
  );
  const layout = useMemo(() => buildLayout(tasks), [tasks]);
  const [selectedId, setSelectedId] = useState<string>();
  const selected = tasks.find((item) => item.task.id === selectedId)
    ?? tasks.find((item) => item.task.status === "in_progress")
    ?? tasks[0];

  return (
    <section className="overflow-hidden rounded-2xl border border-line bg-surface-muted">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2.5">
        <GitFork className="size-3.5 text-accent" />
        <span className="text-[10px] font-bold uppercase tracking-[0.12em] text-ink-muted">依赖关系</span>
        <span className="ml-auto text-[9px] text-ink-faint">从左到右执行</span>
      </div>

      <div className="scrollbar-thin overflow-x-auto bg-surface px-1 py-2">
        <div className="relative" style={{ width: layout.width, height: layout.height }}>
          <svg className="pointer-events-none absolute inset-0" width={layout.width} height={layout.height} aria-hidden>
            <defs>
              <marker id={markerId} markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">
                <path d="M0,0 L7,3.5 L0,7 Z" fill="rgb(var(--line-strong))" />
              </marker>
            </defs>
            {layout.edges.map((edge) => {
              const highlighted = selected?.task.id === edge.sourceId || selected?.task.id === edge.targetId;
              return (
                <path
                  key={`${edge.sourceId}:${edge.targetId}`}
                  d={edge.path}
                  fill="none"
                  markerEnd={`url(#${markerId})`}
                  stroke={highlighted ? "rgb(var(--accent))" : "rgb(var(--line-strong))"}
                  strokeWidth={highlighted ? 1.8 : 1.2}
                />
              );
            })}
          </svg>

          {layout.nodes.map(({ resource, x, y }) => {
            const task = resource.task;
            const visual = taskVisual(resource, completedIds);
            const active = selected?.task.id === task.id;
            return (
              <button
                key={task.id}
                type="button"
                aria-pressed={active}
                onClick={() => setSelectedId(task.id)}
                className={cx(
                  "absolute rounded-xl border px-2.5 py-2 text-left shadow-sm transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40",
                  visual.card,
                  active && "ring-2 ring-accent/25",
                )}
                style={{ left: x, top: y, width: NODE_WIDTH, height: NODE_HEIGHT }}
              >
                <div className="flex items-center gap-1.5">
                  <span className={cx("size-1.5 shrink-0 rounded-full", visual.dot)} />
                  <span className="font-mono text-[9px] text-ink-faint">#{task.id}</span>
                  <span className={cx("ml-auto text-[8px] font-medium", visual.labelColor)}>{visual.label}</span>
                </div>
                <div className="mt-1.5 line-clamp-2 text-[10px] font-medium leading-4 text-ink">{task.subject}</div>
              </button>
            );
          })}
        </div>
      </div>

      {selected && <TaskDetails resource={selected} completedIds={completedIds} />}
    </section>
  );
}

function TaskDetails({ resource, completedIds }: { resource: TaskResource; completedIds: Set<string> }) {
  const task = resource.task;
  const visual = taskVisual(resource, completedIds);
  return (
    <div className="border-t border-line px-3 py-3">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className="font-mono text-[9px] text-ink-faint">#{task.id}</span>
            <span className="truncate text-[11px] font-semibold text-ink">{task.subject}</span>
          </div>
          <p className="mt-1.5 text-[10px] leading-4 text-ink-muted">{task.description}</p>
        </div>
        <span className={cx("shrink-0 rounded-full px-2 py-1 text-[8px] font-medium", visual.badge)}>{visual.label}</span>
      </div>
      <div className="mt-2.5 grid grid-cols-2 gap-2 text-[9px] text-ink-muted">
        <Relation label="依赖" ids={task.blockedBy} />
        <Relation label="阻塞" ids={task.blocks} />
      </div>
      {task.owner && <div className="mt-2 truncate font-mono text-[8px] text-ink-faint" title={task.owner}>owner: {task.owner}</div>}
    </div>
  );
}

function Relation({ label, ids }: { label: string; ids: string[] }) {
  return (
    <div className="rounded-lg bg-surface px-2 py-1.5">
      <span className="text-ink-faint">{label}：</span>
      <span>{ids.length ? ids.map((id) => `#${id}`).join("、") : "无"}</span>
    </div>
  );
}

function taskVisual(resource: TaskResource, completedIds: Set<string>) {
  if (resource.task.status === "completed") {
    return {
      label: "已完成",
      card: "border-success/25 bg-success/5 hover:border-success/40",
      dot: "bg-success",
      labelColor: "text-success",
      badge: "bg-success/10 text-success",
    };
  }
  if (resource.task.status === "in_progress") {
    return {
      label: "进行中",
      card: "border-accent/30 bg-accent/5 hover:border-accent/50",
      dot: "bg-accent",
      labelColor: "text-accent",
      badge: "bg-accent/10 text-accent",
    };
  }
  if (isTaskBlocked(resource, completedIds)) {
    return {
      label: "Blocked",
      card: "border-warning/30 bg-warning/5 hover:border-warning/50",
      dot: "bg-warning",
      labelColor: "text-warning",
      badge: "bg-warning/10 text-warning",
    };
  }
  return {
    label: "Ready",
    card: "border-line bg-surface hover:border-line-strong",
    dot: "bg-ink-faint",
    labelColor: "text-ink-muted",
    badge: "bg-surface-strong text-ink-muted",
  };
}

function buildLayout(tasks: TaskResource[]) {
  const byId = new Map(tasks.map((item) => [item.task.id, item]));
  const depthById = new Map<string, number>();

  const depthOf = (resource: TaskResource): number => {
    const known = depthById.get(resource.task.id);
    if (known !== undefined) return known;
    const blockers = resource.task.blockedBy
      .map((id) => byId.get(id))
      .filter((item): item is TaskResource => item !== undefined);
    const depth = blockers.length ? Math.max(...blockers.map(depthOf)) + 1 : 0;
    depthById.set(resource.task.id, depth);
    return depth;
  };

  tasks.forEach(depthOf);
  const columnCount = Math.max(0, ...depthById.values()) + 1;
  const columns = Array.from({ length: columnCount }, () => [] as TaskResource[]);
  tasks.forEach((item) => columns[depthById.get(item.task.id) ?? 0]!.push(item));
  columns.forEach((column) => column.sort((left, right) => left.task.id.localeCompare(right.task.id, undefined, { numeric: true })));

  const maxRows = Math.max(1, ...columns.map((column) => column.length));
  const contentHeight = maxRows * NODE_HEIGHT + (maxRows - 1) * ROW_GAP;
  const nodes: PositionedTask[] = [];
  columns.forEach((column, columnIndex) => {
    const columnHeight = column.length * NODE_HEIGHT + Math.max(0, column.length - 1) * ROW_GAP;
    const top = PADDING + (contentHeight - columnHeight) / 2;
    column.forEach((resource, rowIndex) => {
      nodes.push({
        resource,
        x: PADDING + columnIndex * (NODE_WIDTH + COLUMN_GAP),
        y: top + rowIndex * (NODE_HEIGHT + ROW_GAP),
      });
    });
  });

  const positionById = new Map(nodes.map((node) => [node.resource.task.id, node]));
  const edges: GraphEdge[] = [];
  nodes.forEach((target) => {
    target.resource.task.blockedBy.forEach((sourceId) => {
      const source = positionById.get(sourceId);
      if (!source) return;
      const startX = source.x + NODE_WIDTH;
      const startY = source.y + NODE_HEIGHT / 2;
      const endX = target.x;
      const endY = target.y + NODE_HEIGHT / 2;
      const bend = (endX - startX) / 2;
      edges.push({
        sourceId,
        targetId: target.resource.task.id,
        path: `M ${startX} ${startY} C ${startX + bend} ${startY}, ${endX - bend} ${endY}, ${endX} ${endY}`,
      });
    });
  });

  return {
    nodes,
    edges,
    width: PADDING * 2 + columnCount * NODE_WIDTH + (columnCount - 1) * COLUMN_GAP,
    height: PADDING * 2 + contentHeight,
  };
}
