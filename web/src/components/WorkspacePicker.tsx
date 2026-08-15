import { useEffect, useMemo, useState } from "react";
import { ChevronRight, Folder, FolderGit2, HardDrive, LoaderCircle, X } from "lucide-react";
import type { WorkspaceListing } from "@/types/api";
import { IconButton } from "@/components/ui";

type Props = {
  open: boolean;
  initialPath?: string;
  loading?: boolean;
  listing?: WorkspaceListing;
  error?: string;
  onBrowse: (path?: string) => void;
  onConfirm: (path: string) => void;
  onClose: () => void;
};

export function WorkspacePicker(props: Props) {
  const [path, setPath] = useState(props.initialPath ?? "");

  useEffect(() => {
    if (props.open) setPath((current) => current || props.initialPath || "");
    else setPath("");
  }, [props.open, props.initialPath]);

  useEffect(() => {
    if (props.open && props.listing?.current) setPath(props.listing.current);
  }, [props.open, props.listing?.current]);

  const crumbs = useMemo(() => pathCrumbs(props.listing?.current), [props.listing?.current]);
  if (!props.open) return null;

  const browse = (next?: string) => {
    if (next) setPath(next);
    props.onBrowse(next);
  };

  return (
    <div className="fixed inset-0 z-[80] grid place-items-center bg-black/55 p-4 backdrop-blur-sm" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && props.onClose()}>
      <section role="dialog" aria-modal="true" aria-labelledby="workspace-picker-title" className="flex max-h-[min(760px,92vh)] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-2xl">
        <header className="flex items-start gap-3 border-b border-line px-5 py-4">
          <div className="grid size-10 shrink-0 place-items-center rounded-xl bg-accent/10 text-accent"><FolderGit2 className="size-5" /></div>
          <div className="min-w-0 flex-1">
            <h2 id="workspace-picker-title" className="text-sm font-semibold text-ink">打开项目工作区</h2>
            <p className="mt-1 text-[11px] leading-5 text-ink-muted">为新对话选择一个已有目录。Agent 的读取、写入和命令执行都会限制在该目录中。</p>
          </div>
          <IconButton label="关闭工作区选择" onClick={props.onClose}><X className="size-4" /></IconButton>
        </header>

        <div className="border-b border-line px-5 py-3">
          <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); browse(path.trim()); }}>
            <input value={path} onChange={(event) => setPath(event.target.value)} aria-label="工作区绝对路径" spellCheck={false} className="h-10 min-w-0 flex-1 rounded-xl border border-line bg-surface-muted px-3 font-mono text-[11px] text-ink outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/10" placeholder="输入项目的绝对路径" />
            <button type="submit" disabled={!path.trim() || props.loading} className="rounded-xl border border-line-strong px-4 text-xs font-medium text-ink transition hover:bg-surface-muted disabled:opacity-50">前往</button>
          </form>
          {props.error && <p role="alert" className="mt-2 text-[11px] text-danger">{props.error}</p>}
        </div>

        <div className="scrollbar-thin min-h-0 flex-1 overflow-y-auto p-3">
          {props.listing && props.listing.roots.length > 1 && <div className="mb-2 flex flex-wrap gap-1 px-1">{props.listing.roots.map((root) => <button key={root} type="button" onClick={() => browse(root)} className="flex items-center gap-1.5 rounded-lg border border-line px-2 py-1.5 font-mono text-[9px] text-ink-muted transition hover:border-line-strong hover:text-ink"><HardDrive className="size-3" />{root}</button>)}</div>}
          <div className="mb-2 flex flex-wrap items-center gap-1 px-1 text-[10px] text-ink-muted">
            {crumbs.map((crumb, index) => <span key={crumb.path} className="flex items-center"><button type="button" onClick={() => browse(crumb.path)} className="max-w-40 truncate rounded px-1.5 py-1 hover:bg-surface-muted hover:text-ink">{crumb.label}</button>{index < crumbs.length - 1 && <ChevronRight className="size-3 text-ink-faint" />}</span>)}
          </div>

          <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
            {props.listing?.parent && <DirectoryButton label=".." path={props.listing.parent} onClick={browse} />}
            {props.listing?.entries.map((entry) => <DirectoryButton key={entry.path} label={entry.name} path={entry.path} project={entry.is_project} onClick={browse} />)}
          </div>
          {props.loading && <div className="grid min-h-40 place-items-center text-ink-muted"><LoaderCircle className="size-5 animate-spin" /></div>}
          {!props.loading && props.listing && props.listing.entries.length === 0 && <div className="grid min-h-40 place-items-center text-xs text-ink-faint">这个目录下没有子目录</div>}
        </div>

        <footer className="flex flex-col gap-3 border-t border-line bg-surface-muted/40 px-5 py-4 sm:flex-row sm:items-center">
          <div className="min-w-0 flex-1">
            <div className="text-[10px] font-medium text-ink-muted">将要打开</div>
            <div className="mt-0.5 truncate font-mono text-[10px] text-ink" title={props.listing?.current}>{props.listing?.current ?? "尚未选择目录"}</div>
          </div>
          <button type="button" onClick={() => props.listing?.current && props.onConfirm(props.listing.current)} disabled={!props.listing?.current || props.loading} className="h-10 rounded-xl bg-accent px-5 text-xs font-semibold text-white shadow-md shadow-accent/20 transition hover:bg-accent-strong disabled:opacity-50">在此目录新建对话</button>
        </footer>
      </section>
    </div>
  );
}

function DirectoryButton({ label, path, project, onClick }: { label: string; path: string; project?: boolean; onClick: (path: string) => void }) {
  const Icon = project ? FolderGit2 : Folder;
  return <button type="button" onClick={() => onClick(path)} title={path} className="group flex min-w-0 items-center gap-3 rounded-xl border border-transparent px-3 py-2.5 text-left transition hover:border-line hover:bg-surface-muted"><div className={project ? "grid size-8 shrink-0 place-items-center rounded-lg bg-accent/10 text-accent" : "grid size-8 shrink-0 place-items-center rounded-lg bg-surface-strong text-ink-muted"}>{label === ".." ? <HardDrive className="size-4" /> : <Icon className="size-4" />}</div><div className="min-w-0"><div className="truncate text-xs font-medium text-ink">{label}</div><div className="mt-0.5 truncate font-mono text-[9px] text-ink-faint">{project ? "检测到项目" : path}</div></div></button>;
}

function pathCrumbs(value?: string) {
  if (!value) return [];
  const separator = value.includes("\\") ? "\\" : "/";
  const pieces = value.split(/[\\/]+/).filter(Boolean);
  const drive = /^[A-Za-z]:/.test(pieces[0] ?? "") ? pieces.shift()! : "";
  const result: Array<{ label: string; path: string }> = [];
  let current = drive ? `${drive}${separator}` : separator;
  result.push({ label: drive || "/", path: current });
  for (const piece of pieces) {
    current = `${current}${current.endsWith(separator) ? "" : separator}${piece}`;
    result.push({ label: piece, path: current });
  }
  return result;
}
