import type { RunEvent } from "@/types/api";

/** Keep live output responsive without rerendering for every replayed token. */
export function createRunEventBuffer(deliver: (events: RunEvent[]) => void, delayMs = 32) {
  let pending: RunEvent[] = [];
  let timer: ReturnType<typeof setTimeout> | undefined;
  const flush = () => {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
    if (!pending.length) return;
    const batch = pending;
    pending = [];
    deliver(batch);
  };
  return {
    push(event: RunEvent) {
      pending.push(event);
      if (timer === undefined) timer = setTimeout(flush, delayMs);
    },
    flush,
  };
}
