"""Context budget and compaction management."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codeagent.context.models import ContextConfig, RuntimeState
from codeagent.messages import Message, ToolUse
from codeagent.tools.todo import TodoStore

SUMMARY_SYSTEM_PROMPT = (
    "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools. "
    "Summarize the conversation for a coding agent that must continue work."
)

SUMMARY_USER_PROMPT = (
    "Create a concise but complete continuation summary. Preserve user goals, "
    "constraints, files read or changed, commands and tests, loaded skills, "
    "subagent results, important findings, risks, and remaining work. "
    "Do not include irrelevant chatter."
)


class ContextManager:
    """Apply cheap-first context compaction while preserving agent state."""

    def __init__(
        self,
        *,
        config: ContextConfig | None = None,
        state: RuntimeState | None = None,
        todo_store: TodoStore | None = None,
    ) -> None:
        self.config = config or ContextConfig()
        self.state = state or RuntimeState()
        self.todo_store = todo_store
        self._compact_failures = 0
        self._reactive_retries = 0

    def prepare_before_model_call(
        self,
        messages: list[Message],
        *,
        client: Any | None = None,
        model: str = "",
        max_tokens: int = 8000,
    ) -> list[Message]:
        if self.config.mode == "off":
            return messages

        prepared = deepcopy(messages)
        prepared = self.tool_result_budget(prepared) #压缩过大的 tool 输出
        prepared = self.snip_compact(prepared) #消息数量太多 → 掐头去尾，中间砍掉
        prepared = self.micro_compact(prepared) # 旧的 tool_result 替换成简短占位符

        if self._estimate_chars(prepared) > self.config.compact_threshold_chars:
            prepared = self.compact_history(
                prepared,
                reason="auto compact", #不参与业务逻辑只是为了观测用，看文件中的reason就知道发生了哪个压缩
                client=client,
                model=model,
                max_tokens=max_tokens,
            )
        return prepared

    def compact_tool_output(self, tool_use: ToolUse, output: str) -> str:
        if self.config.mode == "off":
            return output

        if len(output) <= self.config.single_tool_output_max_chars:
            return output

        return "\n".join(
            [
                "[tool output truncated]",
                f"tool: {tool_use.name}",
                f"original_chars: {len(output)}",
                "",
                "--- head ---",
                output[: self.config.single_tool_output_max_chars],
                "",
                f"... ({len(output) - self.config.single_tool_output_max_chars} more chars)",
            ]
        )

    def record_user_prompt(self, prompt: str) -> None:
        self.state.set_user_goal(prompt)

    def record_tool_result(self, tool_use: ToolUse, output: str) -> None:
        self.state.record_tool_result(tool_use, output)

    def force_compact(
        self,
        messages: list[Message],
        *,
        client: Any | None = None,
        model: str = "",
        max_tokens: int = 8000,
        reason: str = "manual compact",
    ) -> list[Message]:
        return self.compact_history(
            messages,
            reason=reason,
            client=client,
            model=model,
            max_tokens=max_tokens,
        )

    def reactive_compact(
        self,
        messages: list[Message],
        *,
        client: Any | None = None,
        model: str = "",
        max_tokens: int = 8000,
    ) -> list[Message] | None:
        if self._reactive_retries >= self.config.reactive_retries:
            return None
        self._reactive_retries += 1

        compacted = self.compact_history(
            messages,
            reason="reactive compact", 
            client=client,
            model=model,
            max_tokens=max_tokens,
        )
        tail = self._safe_tail(messages, self.config.keep_reactive_tail_messages)
        return compacted + tail

    def reset_reactive_retries(self) -> None:
        self._reactive_retries = 0

    def tool_result_budget(self, messages: list[Message]) -> list[Message]:  #检查后台工具结构，太长了会保存和剪裁
        if not messages:
            return messages

        last = messages[-1]
        content = last.get("content")
        if not isinstance(content, list):
            return messages

        blocks = [
            (index, block)
            for index, block in enumerate(content)
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ] #提取编号和tool result
        total = sum(len(str(block.get("content", ""))) for _, block in blocks)
        if total <= self.config.tool_result_budget_chars:
            return messages

        ranked = sorted(
            blocks,
            key=lambda pair: len(str(pair[1].get("content", ""))),
            reverse=True,
        ) #从大到小排序

        for index, block in ranked:
            if total <= self.config.tool_result_budget_chars:
                break
            original = str(block.get("content", ""))
            replacement = self._persist_tool_output(
                str(block.get("tool_use_id", f"tool_result_{index}")),
                original,
            ) #持久化并在message中替换
            content[index] = {**block, "content": replacement} #展开block，只替换content内容
            total = sum(
                len(str(item.get("content", "")))
                for item in content
                if isinstance(item, dict) and item.get("type") == "tool_result"
            ) #重新计算大小
        return messages

    def snip_compact(self, messages: list[Message]) -> list[Message]:
        if len(messages) <= self.config.max_messages:
            return messages

        head_end = min(self.config.keep_head_messages, len(messages))
        tail_count = max(1, self.config.keep_tail_messages)
        tail_start = max(head_end, len(messages) - tail_count)

        while head_end < len(messages) and _message_has_tool_use(messages[head_end - 1]):
            if not _is_tool_result_message(messages[head_end]):
                break
            head_end += 1

        while tail_start > head_end and _is_tool_result_message(messages[tail_start]):
            if not _message_has_tool_use(messages[tail_start - 1]):
                break
            tail_start -= 1

        snipped = tail_start - head_end
        if snipped <= 0:
            return messages

        placeholder: Message = {
            "role": "user",
            "content": f"[snipped {snipped} messages from conversation middle]",
        }
        return messages[:head_end] + [placeholder] + messages[tail_start:]

    def micro_compact(self, messages: list[Message]) -> list[Message]:
        tool_results = _collect_tool_result_blocks(messages)
        keep = self.config.keep_recent_tool_results
        if len(tool_results) <= keep:
            return messages

        for _, _, block in tool_results[:-keep]:
            content = str(block.get("content", ""))
            if len(content) <= 120:
                continue
            block["content"] = self._micro_placeholder(content)
        return messages

    def compact_history(
        self,
        messages: list[Message],
        *,
        reason: str,
        client: Any | None = None,
        model: str = "",
        max_tokens: int = 8000,
    ) -> list[Message]:
        if self.config.mode == "off":
            return messages

        transcript = self.write_transcript(messages, reason=reason)
        runtime_summary = self.state.render_summary(self._todo_summary())
        model_summary = ""

        if self.config.mode == "model":
            model_summary = self._model_summary(
                messages,
                client=client,
                model=model,
                max_tokens=max_tokens,
            )

        parts = [
            f"[{reason}]",
            f"Transcript saved: {transcript}",
            "",
            runtime_summary,
        ]
        if model_summary:
            parts.extend(["", "<history_summary>", model_summary, "</history_summary>"])

        summary = _limit_text("\n".join(parts), self.config.summary_max_chars)
        return [{"role": "user", "content": summary}]

    def write_transcript(self, messages: list[Message], *, reason: str) -> Path:
        self.config.transcript_dir.mkdir(parents=True, exist_ok=True)
        safe_reason = "".join(ch if ch.isalnum() else "-" for ch in reason).strip("-")
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        path = self.config.transcript_dir / f"{stamp}-{safe_reason or 'compact'}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for message in messages:
                handle.write(json.dumps(message, ensure_ascii=False, default=str))
                handle.write("\n")
        self.state.record_transcript(path)
        return path

    def _persist_tool_output(self, tool_use_id: str, output: str) -> str:
        self.config.tool_output_dir.mkdir(parents=True, exist_ok=True)
        safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tool_use_id)
        path = self.config.tool_output_dir / f"{safe_id}.txt"
        path.write_text(output, encoding="utf-8")
        preview = output[: self.config.persisted_preview_chars]
        return "\n".join(
            [
                (
                    f'<persisted-output path="{path}" '
                    f'original_chars="{len(output)}">'
                ),
                "Preview:",
                preview,
                "</persisted-output>",
            ]
        )

    def _model_summary(
        self,
        messages: list[Message],
        *,
        client: Any | None,
        model: str,
        max_tokens: int,
    ) -> str:
        if client is None or not model:
            return ""
        if self._compact_failures >= self.config.max_compact_failures:
            return ""

        fork = getattr(client, "fork", None)
        summary_client = fork(stream=False, on_text=None) if callable(fork) else client
        try:
            response = summary_client.create_message(
                model=model,
                system=SUMMARY_SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"{SUMMARY_USER_PROMPT}\n\n"
                            f"{json.dumps(messages, ensure_ascii=False, default=str)}"
                        ),
                    }
                ],
                tools=[],
                max_tokens=min(max_tokens, 4000),
            )
        except Exception:
            self._compact_failures += 1
            return ""

        self._compact_failures = 0
        return _extract_text(response.content)

    def _safe_tail(self, messages: list[Message], count: int) -> list[Message]:
        if not messages:
            return []
        tail_start = max(0, len(messages) - count)
        while tail_start > 0 and _is_tool_result_message(messages[tail_start]):
            if not _message_has_tool_use(messages[tail_start - 1]):
                break
            tail_start -= 1
        return deepcopy(messages[tail_start:])

    def _todo_summary(self) -> str | None:
        if self.todo_store is None:
            return None
        return self.todo_store.format()

    def _micro_placeholder(self, content: str) -> str:
        return (
            "[Earlier tool result compacted. Re-run the tool if full output is needed. "
            f"Original length: {len(content)} chars.]"
        )

    @staticmethod
    def _estimate_chars(messages: list[Message]) -> int:
        return len(json.dumps(messages, ensure_ascii=False, default=str))


def _collect_tool_result_blocks(
    messages: list[Message],
) -> list[tuple[int, int, dict[str, Any]]]:
    blocks: list[tuple[int, int, dict[str, Any]]] = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                blocks.append((message_index, block_index, block))
    return blocks


def _message_has_tool_use(message: Message) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "tool_use"
        for block in content
    )


def _is_tool_result_message(message: Message) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def _limit_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n... ({len(value) - limit} more chars compacted)"


def _extract_text(content: Any) -> str:
    blocks = content if isinstance(content, list) else [content]
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        else:
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
    return "\n".join(part for part in parts if part)
