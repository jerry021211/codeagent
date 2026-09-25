import { useEffect, useId, useRef, useState } from "react";
import { Check, ChevronUp, Code2, MessageSquare } from "lucide-react";
import type { ExecutionMode } from "@/types/api";
import { cx } from "@/lib/utils";

const modes = [
  { value: "normal", label: "Code · 编码", description: "修改代码，执行任务", icon: Code2 },
  { value: "discuss", label: "Discuss · 只读", description: "阅读代码，讨论架构与方案", icon: MessageSquare },
] as const;

export function ModeSelector({ mode, disabled, onChange }: {
  mode: ExecutionMode;
  disabled?: boolean;
  onChange: (mode: ExecutionMode) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const menuId = useId();
  const selectedIndex = mode === "discuss" ? 1 : 0;

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  useEffect(() => {
    if (!open) return;
    optionRefs.current[selectedIndex]?.focus();
    const onPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && !rootRef.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open, selectedIndex]);

  const choose = (value: ExecutionMode) => {
    if (disabled) return;
    onChange(value);
    setOpen(false);
    triggerRef.current?.focus();
  };

  return (
    <div ref={rootRef} className="relative shrink-0" onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
    }}>
      <button
        ref={triggerRef}
        type="button"
        aria-label={`选择模式，当前为 ${modes[selectedIndex].label}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        onKeyDown={(event) => {
          if (event.key === "ArrowUp" || event.key === "ArrowDown") {
            event.preventDefault();
            setOpen(true);
          }
        }}
        className="inline-flex h-8 items-center gap-2 rounded-lg border border-line px-2.5 text-[10px] font-medium text-ink-muted transition hover:bg-surface-muted hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-line-strong disabled:cursor-not-allowed disabled:opacity-50"
      >
        {modes[selectedIndex].label}
        <ChevronUp aria-hidden className={cx("size-3 transition-transform", open && "rotate-180")} />
      </button>
      {open && !disabled && (
        <div id={menuId} role="listbox" aria-label="运行模式" className="absolute bottom-full left-0 z-50 mb-2 w-64 rounded-xl border border-line bg-surface p-1.5 shadow-panel">
          {modes.map((item, index) => {
            const Icon = item.icon;
            const selected = item.value === mode;
            return (
              <button
                key={item.value}
                ref={(element) => { optionRefs.current[index] = element; }}
                type="button"
                role="option"
                aria-selected={selected}
                tabIndex={selected ? 0 : -1}
                onClick={() => choose(item.value)}
                onKeyDown={(event) => {
                  if (event.key === "Escape") {
                    event.preventDefault();
                    event.stopPropagation();
                    setOpen(false);
                    triggerRef.current?.focus();
                  } else if (event.key === "Tab") {
                    setOpen(false);
                    triggerRef.current?.focus();
                  } else if (["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) {
                    event.preventDefault();
                    const next = event.key === "Home" ? 0 : event.key === "End" ? modes.length - 1 : (index + (event.key === "ArrowUp" ? -1 : 1) + modes.length) % modes.length;
                    optionRefs.current[next]?.focus();
                  }
                }}
                className={cx("flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left transition hover:bg-surface-muted focus:bg-surface-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/40", selected && "bg-accent/5")}
              >
                <Icon aria-hidden className={cx("size-4 shrink-0", selected ? "text-accent" : "text-ink-muted")} />
                <span className="min-w-0 flex-1">
                  <span className={cx("block text-xs font-medium", selected ? "text-accent" : "text-ink")}>{item.label}</span>
                  <span className="mt-0.5 block text-[10px] text-ink-muted">{item.description}</span>
                </span>
                {selected && <Check aria-hidden className="size-3.5 shrink-0 text-accent" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
