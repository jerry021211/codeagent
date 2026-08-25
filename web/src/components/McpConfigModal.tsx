import { useEffect, useState } from "react";
import { CheckCircle2, CircleAlert, Plug, Server, Trash2, X } from "lucide-react";
import type { McpConfig, McpTransport, SaveMcpServer } from "@/types/api";
import { IconButton, Spinner } from "@/components/ui";

type Props = {
  open: boolean;
  workspace?: string;
  config?: McpConfig;
  loading?: boolean;
  saving?: boolean;
  deleting?: string;
  error?: string;
  message?: string;
  onSave: (server: SaveMcpServer) => void;
  onDelete: (name: string) => void;
  onClose: () => void;
};

type FormState = {
  name: string;
  transport: McpTransport;
  command: string;
  args: string;
  cwd: string;
  url: string;
  env: string;
  headers: string;
};

const emptyForm: FormState = {
  name: "",
  transport: "http",
  command: "",
  args: "",
  cwd: "",
  url: "",
  env: "",
  headers: "",
};

export function McpConfigModal(props: Props) {
  const [form, setForm] = useState<FormState>(emptyForm);
  const [formError, setFormError] = useState<string>();

  useEffect(() => {
    if (!props.open) {
      setForm(emptyForm);
      setFormError(undefined);
    }
  }, [props.open]);

  if (!props.open) return null;

  const update = (patch: Partial<FormState>) => setForm((current) => ({ ...current, ...patch }));
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!props.workspace) return;
    try {
      setFormError(undefined);
      props.onSave({
        workspace: props.workspace,
        name: form.name.trim(),
        transport: form.transport,
        command: form.command.trim(),
        args: lines(form.args),
        cwd: form.cwd.trim() || undefined,
        url: form.url.trim(),
        env: keyValues(form.env),
        headers: keyValues(form.headers),
      });
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "配置格式不正确");
    }
  };

  return (
    <div className="fixed inset-0 z-[80] grid place-items-center bg-black/55 p-4 backdrop-blur-sm" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && props.onClose()}>
      <section role="dialog" aria-modal="true" aria-labelledby="mcp-config-title" className="flex max-h-[min(860px,94vh)] w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-2xl">
        <header className="flex items-start gap-3 border-b border-line px-5 py-4">
          <div className="grid size-10 shrink-0 place-items-center rounded-xl bg-accent/10 text-accent"><Plug className="size-5" /></div>
          <div className="min-w-0 flex-1">
            <h2 id="mcp-config-title" className="text-sm font-semibold text-ink">MCP 插件配置</h2>
            <p className="mt-1 text-[11px] leading-5 text-ink-muted">为当前项目添加本地或远程 MCP Server。保存后，下一条消息会自动加载新工具。</p>
          </div>
          <IconButton label="关闭 MCP 配置" onClick={props.onClose}><X className="size-4" /></IconButton>
        </header>

        <div className="scrollbar-thin min-h-0 flex-1 overflow-y-auto">
          <section className="border-b border-line px-5 py-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h3 className="text-xs font-semibold text-ink">快速模板</h3>
                <p className="mt-1 text-[10px] text-ink-muted">先套用模板，再按需要修改。</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <PresetButton label="Context7" onClick={() => setForm({ ...emptyForm, name: "context7", transport: "http", url: "https://mcp.context7.com/mcp" })} />
                <PresetButton label="GitHub" onClick={() => setForm({ ...emptyForm, name: "github", transport: "http", url: "https://api.githubcopilot.com/mcp/", headers: "Authorization=Bearer ${GITHUB_PAT}" })} />
                <PresetButton label="Playwright" onClick={() => setForm({ ...emptyForm, name: "playwright", transport: "stdio", command: "cmd", args: "/c\nnpx\n-y\n@playwright/mcp@latest\n--headless" })} />
              </div>
            </div>
          </section>

          <form onSubmit={submit} className="space-y-4 border-b border-line px-5 py-5">
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="配置名称" hint="只能使用字母、数字、- 和 _">
                <input required pattern="[A-Za-z0-9_-]+" value={form.name} onChange={(event) => update({ name: event.target.value })} className={inputClass} placeholder="例如 github" />
              </Field>
              <Field label="连接方式">
                <div className="grid grid-cols-2 rounded-xl border border-line bg-surface-muted p-1">
                  {(["http", "stdio"] as McpTransport[]).map((transport) => <button key={transport} type="button" onClick={() => update({ transport })} className={form.transport === transport ? "rounded-lg bg-surface px-3 py-2 text-xs font-medium text-accent shadow-sm" : "rounded-lg px-3 py-2 text-xs text-ink-muted hover:text-ink"}>{transport === "http" ? "远程 HTTP" : "本地命令"}</button>)}
                </div>
              </Field>
            </div>

            {form.transport === "http" ? (
              <>
                <Field label="MCP 地址"><input required type="url" value={form.url} onChange={(event) => update({ url: event.target.value })} className={inputClass} placeholder="https://example.com/mcp" /></Field>
                <Field label="Headers" hint="每行一个 KEY=value；密钥推荐使用 ${TOKEN_NAME}"><textarea value={form.headers} onChange={(event) => update({ headers: event.target.value })} className={textareaClass} placeholder="Authorization=Bearer ${API_TOKEN}" /></Field>
              </>
            ) : (
              <>
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="启动命令"><input required value={form.command} onChange={(event) => update({ command: event.target.value })} className={inputClass} placeholder="python、cmd、docker…" /></Field>
                  <Field label="工作目录" hint="可选"><input value={form.cwd} onChange={(event) => update({ cwd: event.target.value })} className={inputClass} placeholder="例如 D:\project" /></Field>
                </div>
                <Field label="启动参数" hint="每行一个参数"><textarea value={form.args} onChange={(event) => update({ args: event.target.value })} className={textareaClass} placeholder={"/c\nnpx\n-y\n@company/server"} /></Field>
                <Field label="环境变量" hint="每行一个 KEY=value；密钥推荐使用 ${TOKEN_NAME}"><textarea value={form.env} onChange={(event) => update({ env: event.target.value })} className={textareaClass} placeholder="GITHUB_TOKEN=${GITHUB_TOKEN}" /></Field>
              </>
            )}

            {(formError || props.error) && <p role="alert" className="flex items-center gap-2 rounded-xl border border-danger/20 bg-danger/5 px-3 py-2 text-[11px] text-danger"><CircleAlert className="size-4 shrink-0" />{formError || props.error}</p>}
            {props.message && <p className="flex items-center gap-2 rounded-xl border border-success/20 bg-success/5 px-3 py-2 text-[11px] text-success"><CheckCircle2 className="size-4 shrink-0" />{props.message}</p>}

            <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
              <p className="min-w-0 flex-1 truncate font-mono text-[9px] text-ink-faint" title={props.config?.config_path}>{props.config?.config_path ?? `${props.workspace ?? "当前工作区"}/mcp.json`}</p>
              <button type="submit" disabled={!props.workspace || props.saving} className="inline-flex h-10 items-center justify-center gap-2 rounded-xl bg-accent px-5 text-xs font-semibold text-white shadow-md shadow-accent/20 transition hover:bg-accent-strong disabled:opacity-50">{props.saving && <Spinner className="size-3.5 text-white" />}保存配置</button>
            </div>
          </form>

          <section className="px-5 py-5">
            <h3 className="text-xs font-semibold text-ink">已添加的 MCP Server</h3>
            <div className="mt-3 space-y-2">
              {props.loading && <div className="flex items-center justify-center gap-2 py-8 text-xs text-ink-muted"><Spinner />读取配置…</div>}
              {!props.loading && props.config?.servers.length === 0 && <div className="rounded-xl border border-dashed border-line px-4 py-8 text-center text-xs text-ink-faint">当前项目还没有 MCP 配置</div>}
              {props.config?.servers.map((server) => (
                <div key={server.name} className="flex items-center gap-3 rounded-xl border border-line bg-surface-muted/50 px-3 py-3">
                  <div className="grid size-9 shrink-0 place-items-center rounded-lg bg-surface-strong text-ink-muted"><Server className="size-4" /></div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2"><span className="truncate text-xs font-semibold text-ink">{server.name}</span><span className="rounded-md border border-line px-1.5 py-0.5 text-[8px] uppercase text-ink-faint">{server.transport}</span></div>
                    <div className="mt-1 truncate font-mono text-[9px] text-ink-muted" title={server.url || [server.command, ...server.args].join(" ")}>{server.url || [server.command, ...server.args].join(" ")}</div>
                    {(server.env_keys.length > 0 || server.header_keys.length > 0) && <div className="mt-1 text-[9px] text-ink-faint">变量：{[...server.env_keys, ...server.header_keys].join("、")}</div>}
                  </div>
                  <IconButton label={`删除 ${server.name}`} disabled={props.deleting === server.name} onClick={() => window.confirm(`确定删除 MCP 配置 ${server.name}？`) && props.onDelete(server.name)} className="hover:text-danger">{props.deleting === server.name ? <Spinner className="size-3.5" /> : <Trash2 className="size-4" />}</IconButton>
                </div>
              ))}
            </div>
          </section>
        </div>
      </section>
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return <label className="block"><span className="mb-1.5 flex items-center gap-2 text-[11px] font-medium text-ink">{label}{hint && <span className="font-normal text-ink-faint">{hint}</span>}</span>{children}</label>;
}

function PresetButton({ label, onClick }: { label: string; onClick: () => void }) {
  return <button type="button" onClick={onClick} className="rounded-lg border border-line px-2.5 py-1.5 text-[10px] font-medium text-ink-muted transition hover:border-accent/30 hover:bg-accent/5 hover:text-accent">{label}</button>;
}

function lines(value: string) {
  return value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
}

function keyValues(value: string) {
  const result: Record<string, string> = {};
  for (const line of lines(value)) {
    const separator = line.indexOf("=");
    if (separator <= 0) throw new Error(`请使用 KEY=value 格式：${line}`);
    result[line.slice(0, separator).trim()] = line.slice(separator + 1).trim();
  }
  return result;
}

const inputClass = "h-10 w-full rounded-xl border border-line bg-surface-muted px-3 font-mono text-[11px] text-ink outline-none transition placeholder:font-sans placeholder:text-ink-faint focus:border-accent focus:ring-2 focus:ring-accent/10";
const textareaClass = "scrollbar-thin min-h-24 w-full resize-y rounded-xl border border-line bg-surface-muted px-3 py-2 font-mono text-[11px] leading-5 text-ink outline-none transition placeholder:font-sans placeholder:text-ink-faint focus:border-accent focus:ring-2 focus:ring-accent/10";
