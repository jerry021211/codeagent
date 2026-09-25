import type { Message } from "@/types/api";

/** Place each process before its reply, or after its prompt if it has no reply. */
export function processAnchors(messages: Message[], runIds: string[]) {
  const before: Record<string, string[]> = {};
  const after: Record<string, string[]> = {};
  const trailing: string[] = [];
  for (const id of runIds) {
    const reply = messages.find(message => message.run_id === id && message.role === "assistant");
    const prompt = [...messages].reverse().find(message => message.run_id === id);
    if (reply) (before[reply.id] ??= []).push(id);
    else if (prompt) (after[prompt.id] ??= []).push(id);
    else trailing.push(id);
  }
  return { before, after, trailing };
}
