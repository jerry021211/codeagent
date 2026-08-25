from __future__ import annotations

import tempfile
import sqlite3
import threading
import time
import unittest
from pathlib import Path

from codeagent.events import RunEvent, TokenUsage
from codeagent.web.storage import (
    InvalidStateTransitionError,
    RecordNotFoundError,
    SQLiteRepository,
    StorageConflictError,
)


class SQLiteRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "state.db"
        self.repository = SQLiteRepository(
            self.database,
            recover_incomplete=False,
        )

    def tearDown(self) -> None:
        self.repository.close()
        self.temp_dir.cleanup()

    def create_run(self, *, status: str = "queued"):
        conversation = self.repository.create_conversation(title="Storage test")
        run = self.repository.create_run(conversation.id, status=status)
        return conversation, run

    def test_initializes_wal_schema_and_survives_reopen(self) -> None:
        conversation = self.repository.create_conversation(
            title="Persistent conversation",
            conversation_id="conv_persisted",
        )
        self.assertTrue(self.repository.health_check())
        self.repository.close()

        reopened = SQLiteRepository(self.database, recover_incomplete=False)
        try:
            loaded = reopened.get_conversation(conversation.id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.title, "Persistent conversation")

            with reopened._lock:
                mode = reopened._connection.execute("PRAGMA journal_mode").fetchone()[0]
                tables = {
                    row[0]
                    for row in reopened._connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertEqual(mode.casefold(), "wal")
            self.assertTrue(
                {
                    "conversations",
                    "messages",
                    "runs",
                    "events",
                    "approvals",
                    "model_calls",
                    "checkpoints",
                }.issubset(tables)
            )
        finally:
            reopened.close()

    def test_migrates_existing_conversations_and_binds_default_workspace(self) -> None:
        legacy_database = Path(self.temp_dir.name) / "legacy.db"
        connection = sqlite3.connect(legacy_database)
        connection.execute(
            "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT)"
        )
        connection.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?, ?)",
            ("legacy", "Legacy", "2026-01-01", "2026-01-01", None),
        )
        connection.commit()
        connection.close()

        legacy = SQLiteRepository(legacy_database, recover_incomplete=False)
        try:
            self.assertEqual(legacy.get_conversation("legacy").workspace, "")
            self.assertEqual(legacy.bind_unassigned_workspaces("D:\\legacy"), 1)
            self.assertEqual(
                legacy.get_conversation("legacy").workspace,
                "D:\\legacy",
            )
        finally:
            legacy.close()

    def test_conversation_and_message_crud_round_trips_json(self) -> None:
        conversation = self.repository.create_conversation(title="Initial")
        updated = self.repository.update_conversation(
            conversation.id, title="Renamed", archived=True
        )
        self.assertEqual(updated.title, "Renamed")
        self.assertIsNotNone(updated.archived_at)
        self.assertEqual(self.repository.list_conversations(), [])
        self.assertEqual(
            [item.id for item in self.repository.list_conversations(include_archived=True)],
            [conversation.id],
        )

        run = self.repository.create_run(conversation.id)
        message = self.repository.create_message(
            conversation.id,
            role="user",
            content=[{"type": "text", "text": "你好"}],
            run_id=run.id,
            metadata={"source": "web"},
        )
        loaded = self.repository.get_message(message.id)
        self.assertEqual(loaded.content[0]["text"], "你好")
        self.assertEqual(loaded.metadata, {"source": "web"})

        edited = self.repository.update_message(
            message.id,
            content="updated",
            metadata={"edited": True},
        )
        self.assertEqual(edited.content, "updated")
        self.assertTrue(edited.metadata["edited"])
        self.assertTrue(self.repository.delete_message(message.id))
        self.assertIsNone(self.repository.get_message(message.id))

    def test_foreign_keys_and_one_active_run_per_conversation(self) -> None:
        conversation, first = self.create_run()

        with self.assertRaises(StorageConflictError):
            self.repository.create_run(conversation.id)
        with self.assertRaises(RecordNotFoundError):
            self.repository.create_message(
                "missing", role="user", content="no conversation"
            )

        self.repository.update_run_status(first.id, "completed")
        second = self.repository.create_run(conversation.id)
        self.assertEqual(second.status, "queued")

    def test_queue_positions_close_after_start_and_cancel(self) -> None:
        conversations = [
            self.repository.create_conversation(title=f"Conversation {index}")
            for index in range(3)
        ]
        runs = [self.repository.create_run(item.id) for item in conversations]
        self.assertEqual([run.queue_position for run in runs], [1, 2, 3])

        started = self.repository.start_run(runs[0].id)
        self.assertEqual(started.status, "running")
        self.assertIsNotNone(started.started_at)
        self.assertEqual(self.repository.get_run(runs[1].id).queue_position, 1)
        self.assertEqual(self.repository.get_run(runs[2].id).queue_position, 2)

        cancelled = self.repository.request_run_cancel(runs[1].id)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertIsNotNone(cancelled.cancel_requested_at)
        self.assertEqual(self.repository.get_run(runs[2].id).queue_position, 1)

        cancellation_requested = self.repository.request_run_cancel(runs[0].id)
        self.assertEqual(cancellation_requested.status, "running")
        self.assertTrue(self.repository.is_cancel_requested(runs[0].id))

    def test_terminal_runs_reject_lifecycle_regression(self) -> None:
        _, run = self.create_run()
        completed = self.repository.update_run_status(run.id, "completed")
        self.assertIsNotNone(completed.finished_at)

        with self.assertRaises(InvalidStateTransitionError):
            self.repository.update_run_status(run.id, "running")

    def test_append_event_assigns_atomic_sequences_and_replays(self) -> None:
        conversation, run = self.create_run(status="running")
        count = 30
        barrier = threading.Barrier(count)
        persisted: list[RunEvent] = []
        errors: list[BaseException] = []
        append_lock = threading.Lock()

        def append(index: int) -> None:
            try:
                barrier.wait()
                event = self.repository.append_event(
                    RunEvent(
                        id=f"evt_{index}",
                        seq=999,
                        type="tool.started",
                        conversation_id=conversation.id,
                        run_id=run.id,
                        payload={"index": index},
                    )
                )
                with append_lock:
                    persisted.append(event)
            except BaseException as exc:  # pragma: no cover - assertion reports it
                with append_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=append, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(sorted(event.seq for event in persisted), list(range(1, count + 1)))
        replay = self.repository.list_events(run.id, after_seq=20)
        self.assertEqual([event.seq for event in replay], list(range(21, count + 1)))
        self.assertEqual(self.repository.get_run(run.id).next_event_seq, count)

    def test_append_event_is_idempotent_and_redacts_payload(self) -> None:
        conversation, run = self.create_run(status="running")
        event = RunEvent(
            id="evt_stable",
            type="tool.requested",
            conversation_id=conversation.id,
            run_id=run.id,
            payload={"api_key": "secret", "safe": "value"},
        )

        first = self.repository.append_event(event)
        second = self.repository.append_event(event)

        self.assertEqual(first, second)
        self.assertEqual(first.seq, 1)
        self.assertEqual(first.payload["api_key"], "[REDACTED]")
        self.assertEqual(len(self.repository.list_events(run.id)), 1)

    def test_event_conversation_must_match_run(self) -> None:
        _, run = self.create_run(status="running")
        other = self.repository.create_conversation(title="Other")

        with self.assertRaises(StorageConflictError):
            self.repository.append_event(
                RunEvent(
                    type="invalid",
                    conversation_id=other.id,
                    run_id=run.id,
                )
            )

    def test_wait_for_events_wakes_after_append(self) -> None:
        conversation, run = self.create_run(status="running")
        result: list[list[RunEvent]] = []
        started = threading.Event()

        def wait() -> None:
            started.set()
            result.append(self.repository.wait_for_events(run.id, 0, timeout=2.0))

        waiter = threading.Thread(target=wait)
        waiter.start()
        started.wait(timeout=1)
        time.sleep(0.02)
        self.repository.append_event(
            RunEvent(
                type="run.started",
                conversation_id=conversation.id,
                run_id=run.id,
            )
        )
        waiter.join(timeout=2)

        self.assertFalse(waiter.is_alive())
        self.assertEqual(result[0][0].type, "run.started")

    def test_approval_resolution_is_one_way_and_idempotent(self) -> None:
        _, run = self.create_run(status="running")
        approval = self.repository.create_approval(
            run.id,
            tool_name="bash",
            tool_input={"command": "Remove-Item file.txt"},
            reason="destructive command",
        )
        self.assertEqual(approval.status, "pending")

        allowed = self.repository.resolve_approval(approval.id, "allow")
        repeated = self.repository.resolve_approval(approval.id, "deny")

        self.assertEqual(allowed.status, "allowed")
        self.assertEqual(repeated.status, "allowed")
        self.assertEqual(repeated.decision, "allow")
        self.assertEqual(repeated.resolved_at, allowed.resolved_at)

    def test_expired_approvals_are_denied(self) -> None:
        _, run = self.create_run(status="running")
        approval = self.repository.create_approval(
            run.id,
            tool_name="bash",
            tool_input={"command": "remove file"},
            reason="destructive command",
            expires_at="2025-01-01T00:00:00+00:00",
        )

        expired = self.repository.expire_pending_approvals(
            now="2025-01-01T00:00:01+00:00"
        )

        self.assertEqual([item.id for item in expired], [approval.id])
        self.assertEqual(expired[0].status, "expired")

    def test_model_calls_aggregate_available_and_missing_usage(self) -> None:
        _, run = self.create_run(status="running")
        self.repository.record_model_call(
            run.id,
            model="model-a",
            call_kind="main",
            agent_id="root",
            usage=TokenUsage(
                model="model-a",
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=2,
                cache_read_input_tokens=3,
            ),
        )
        self.repository.record_model_call(
            run.id,
            model="model-a",
            call_kind="subagent",
            agent_id="child",
            usage={"input_tokens": 4, "output_tokens": 1, "estimated": True},
        )
        self.repository.record_model_call(
            run.id,
            model="model-b",
            call_kind="memory",
            usage=None,
        )

        totals = self.repository.aggregate_usage(run_id=run.id)

        self.assertEqual(totals["input_tokens"], 14)
        self.assertEqual(totals["output_tokens"], 6)
        self.assertEqual(totals["prompt_input_tokens"], 19)
        self.assertAlmostEqual(totals["cache_hit_ratio"], 3 / 19)
        self.assertEqual(totals["total_tokens"], 25)
        self.assertEqual(totals["model_calls"], 3)
        self.assertEqual(totals["available_calls"], 2)
        self.assertEqual(totals["unavailable_calls"], 1)
        self.assertTrue(totals["estimated"])
        self.assertEqual(totals["by_call_kind"]["subagent"]["total_tokens"], 5)
        self.assertEqual(totals["by_agent"]["child"]["model_calls"], 1)

    def test_finish_run_and_checkpoint_commit_together(self) -> None:
        conversation, run = self.create_run(status="running")

        completed, checkpoint = self.repository.finish_run_with_checkpoint(
            run.id,
            status="completed",
            messages=[{"role": "assistant", "content": "done"}],
            todos=[{"content": "test", "status": "completed"}],
            context={"user_goal": "ship", "tool_schema_hash": "abc123"},
        )

        self.assertEqual(completed.status, "completed")
        self.assertEqual(checkpoint.run_status, "completed")
        self.assertEqual(checkpoint.messages[0]["content"], "done")
        self.assertEqual(
            self.repository.get_latest_checkpoint(conversation.id).id,
            checkpoint.id,
        )
        self.repository.close()
        self.repository = SQLiteRepository(self.database, recover_incomplete=False)
        restored = self.repository.get_latest_checkpoint(conversation.id)
        self.assertEqual(restored.context["tool_schema_hash"], "abc123")

    def test_checkpoint_requires_terminal_run(self) -> None:
        _, run = self.create_run(status="running")

        with self.assertRaises(InvalidStateTransitionError):
            self.repository.save_checkpoint(
                run.id, messages=[], todos=[], context={}
            )

    def test_startup_marks_queued_and_running_runs_interrupted(self) -> None:
        first_conversation = self.repository.create_conversation(title="Queued")
        second_conversation = self.repository.create_conversation(title="Running")
        queued = self.repository.create_run(first_conversation.id)
        running = self.repository.create_run(second_conversation.id, status="running")
        self.repository.close()

        reopened = SQLiteRepository(self.database)
        try:
            self.assertEqual(reopened.get_run(queued.id).status, "interrupted")
            interrupted = reopened.get_run(running.id)
            self.assertEqual(interrupted.status, "interrupted")
            self.assertEqual(interrupted.error["type"], "service_restart")
            self.assertEqual(reopened.list_events(queued.id)[-1].type, "run.interrupted")
            self.assertEqual(reopened.list_events(running.id)[-1].type, "run.interrupted")
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
