"""Durable web questions; only the calling Agent worker waits."""

from codeagent.events import EventEmitter
from codeagent.runtime import CancellationToken, CancelledError
from codeagent.web.storage import SQLiteRepository


class WebUserQuestions:
    def __init__(self, repository: SQLiteRepository, emitter: EventEmitter, cancellation: CancellationToken) -> None:
        self.repository = repository
        self.emitter = emitter
        self.cancellation = cancellation

    def ask(self, question: str, options: list[str]) -> str:
        self.cancellation.raise_if_cancelled()
        run_id = self.emitter.context.run_id
        pending = self.repository.create_user_question(run_id, question, options)
        question_id = pending["id"]
        try:
            self.emitter.emit("question.requested", {"question_id": question_id})
            while True:
                self.cancellation.raise_if_cancelled()
                current = self.repository.get_user_question(run_id, question_id)
                if current["status"] == "answered":
                    self.cancellation.raise_if_cancelled()
                    self.emitter.emit("question.answered", {"question_id": question_id})
                    return current["answer"]
                if current["status"] != "pending":
                    self.cancellation.cancel("User question was cancelled")
                    raise CancelledError("User question was cancelled")
                # No automatic timeout: wait for an actual answer or cancellation.
                # The repository lock is never held while waiting.
                self.cancellation.wait(0.1)
        finally:
            self.repository.cancel_user_question(run_id, question_id)
