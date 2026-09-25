"""Blocking user questions with an injected terminal or web transport."""

from __future__ import annotations

from collections.abc import Callable

from codeagent.tools.base import ToolDefinition, ToolOutput

ASK_USER_TOOL_NAME = "ask_user"
AskUserHandler = Callable[[str, list[str]], str]


def terminal_ask_user(question: str, options: list[str]) -> str:
    """Wait on stdin; suggested choices never replace an explicit answer."""
    print(f"\n[ask_user] {question}")
    for index, option in enumerate(options, 1):
        print(f"  {index}. {option}")
    while True:
        answer = input("你的回答（可输入选项编号或自由文本）: ")
        if not answer.strip():
            print("请输入回答后继续。")
            continue
        for index, option in enumerate(options, 1):
            if answer.strip() == str(index):
                return option
        return answer


class AskUserTool:
    definition = ToolDefinition(
        name=ASK_USER_TOOL_NAME,
        description=(
            "在需求、偏好或关键决策不明确且无法从现有证据确定时向用户提问。"
            "提出一个简短、具体的问题，可附建议选项，用户始终可以自由回答。"
            "调用会阻塞当前 Agent，直到用户回答或运行被取消；不要猜测回答。"
            "已有明确授权或能自行核实的常规事项无需重复询问。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "minLength": 1, "maxLength": 4000},
                "options": {
                    "type": "array", "maxItems": 8,
                    "items": {"type": "string", "minLength": 1, "maxLength": 500},
                    "description": "可选的建议回答；不提供时使用自由文本。",
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    )

    def __init__(self, ask_fn: AskUserHandler) -> None:
        self.ask_fn = ask_fn

    def run(self, question: str, options: list[str] | None = None) -> str:
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise ValueError("question must be non-blank text of at most 4000 characters")
        choices = [] if options is None else options
        if not isinstance(choices, list) or len(choices) > 8 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 500
            for item in choices
        ):
            raise ValueError("options must contain at most 8 non-blank strings of at most 500 characters")
        try:
            answer = self.ask_fn(question.strip(), list(choices))
        except EOFError:
            return ToolOutput("Blocked: User input is unavailable; no answer was received. Do not guess the user's decision.", status="blocked")
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("No user answer was received")
        # Answers such as 'Error: ...' are still successful user responses.
        return ToolOutput(answer, status="success")
