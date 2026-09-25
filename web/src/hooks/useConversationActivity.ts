import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { buildRunHistory, type RunViewState } from "@/store/runStore";
import type { Message } from "@/types/api";

export function useConversationActivity(conversationId: string | undefined, messages: Message[], current?: RunViewState) {
  const ids = useMemo(() => [...new Set(messages.map(message => message.run_id).filter((id): id is string => Boolean(id)))].filter(id => id !== current?.runId), [messages, current?.runId]);
  const query = useQuery({
    queryKey: ["conversation-activity", conversationId, ids],
    enabled: Boolean(conversationId && ids.length),
    queryFn: async () => {
      const result: Record<string, RunViewState> = {};
      let index = 0;
      // Finite history reads, with bounded concurrency; only the current run uses SSE.
      await Promise.all(Array.from({ length: Math.min(4, ids.length) }, async () => {
        while (index < ids.length) {
          const id = ids[index++]!;
          const snapshot = await api.getRunActivity(id);
          result[id] = buildRunHistory(id, snapshot.status, snapshot.events);
        }
      }));
      return result;
    },
    staleTime: 30_000,
  });
  return query;
}
