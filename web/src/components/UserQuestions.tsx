import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { UserQuestion } from "@/types/api";

export function UserQuestions({ runId, active }: { runId: string; active: boolean }) {
  const questions = useQuery({
    queryKey: ["user-questions", runId],
    queryFn: () => api.listQuestions(runId),
    refetchInterval: active ? 1000 : false,
  });
  const { refetch } = questions;
  useEffect(() => { void refetch(); }, [active, refetch]);

  return (
    <div className="max-h-80 space-y-2 overflow-y-auto" aria-live="polite">
      {questions.isError && <p role="alert" className="text-xs text-danger">无法读取 Agent 提问，请检查连接。<button type="button" onClick={() => void refetch()} className="ml-2 underline">重试</button></p>}
      {questions.data?.map((question) => question.status === "pending" && active ? (
        <QuestionForm key={question.id} question={question} />
      ) : (
        <details key={question.id} className="rounded-xl border border-line bg-surface px-3 py-2 text-xs text-ink-muted">
          <summary className="cursor-pointer">{question.status === "answered" ? "已回答" : "提问已取消"}：{question.question}</summary>
          {question.answer && <p className="mt-2 whitespace-pre-wrap break-words text-ink">{question.answer}</p>}
        </details>
      ))}
    </div>
  );
}

function QuestionForm({ question }: { question: UserQuestion }) {
  const [answer, setAnswer] = useState("");
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => api.answerQuestion(question.run_id, question.id, answer),
    onSuccess: (resolved) => {
      queryClient.setQueryData<UserQuestion[]>(["user-questions", question.run_id], (current) =>
        current?.map((item) => item.id === resolved.id ? resolved : item));
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["user-questions", question.run_id] }),
  });
  return (
    <form onSubmit={(event) => { event.preventDefault(); if (answer.trim() && !mutation.isPending) mutation.mutate(); }} className="space-y-3 rounded-xl border border-warning/30 bg-surface p-4">
      <div className="text-xs font-semibold text-warning">等待你的回答 · Agent 已暂停</div>
      <label htmlFor={`answer-${question.id}`} className="block whitespace-pre-wrap break-words text-sm text-ink">{question.question}</label>
      {question.options.length > 0 && <div className="flex flex-wrap gap-2">{question.options.map((option, index) => (
        <button key={index} type="button" disabled={mutation.isPending} onClick={() => setAnswer(option)} aria-pressed={answer === option} className={`rounded-lg border px-3 py-2 text-xs ${answer === option ? "border-accent bg-accent/10 text-accent" : "border-line text-ink-muted"}`}>{option}</button>
      ))}</div>}
      <textarea id={`answer-${question.id}`} value={answer} onChange={(event) => setAnswer(event.target.value)} disabled={mutation.isPending} maxLength={20000} rows={2} placeholder="输入回答，或选择上方选项后提交…" className="w-full rounded-lg border border-line bg-canvas p-2 text-sm text-ink outline-none focus:border-accent" />
      {mutation.isError && <p role="alert" className="text-xs text-danger">{mutation.error.message}</p>}
      <button type="submit" disabled={!answer.trim() || mutation.isPending} className="rounded-lg bg-accent px-3 py-2 text-xs text-white disabled:opacity-50">{mutation.isPending ? "提交中…" : "提交回答并继续"}</button>
    </form>
  );
}
