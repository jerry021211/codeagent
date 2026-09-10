from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codeagent.web.storage import SQLiteRepository
from codeagent.web.team_observation import TABLE_SCOPES, install_observation


class TeamObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.db"
        self.repo = SQLiteRepository(self.path, recover_incomplete=False)
        self.conversation = self.repo.create_conversation(title="Observe", workspace=self.temp.name)
        self.run = self.repo.create_run(self.conversation.id, status="running")
        self.task_list = self.conversation.active_task_list_id
        self.task = self.repo.create_task(self.task_list, subject="计算器", description="四则运算")
        self.team = self.repo.create_team_run(
            conversation_id=self.conversation.id, root_run_id=self.run.id,
            task_list_id=self.task_list, base_commit="a" * 40,
        )

    def tearDown(self) -> None:
        self.repo.close()
        self.temp.cleanup()

    def history(self, table: str | None = None) -> list[dict]:
        items, cursor = [], 0
        while True:
            page = self.repo.list_team_changes(self.team.id, after=cursor, limit=2, table=table)
            items.extend(page["items"])
            cursor = page["next_cursor"]
            if not page["has_more"]:
                return items

    def detail(self, change: dict) -> dict:
        return self.repo.list_team_changes(self.team.id, seq=change["seq"])["items"][0]

    def test_all_business_tables_and_columns_are_covered(self) -> None:
        connection = self.repo._connection
        tables = {r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )} - {"database_changes"}
        self.assertEqual(tables, set(TABLE_SCOPES))
        for table in tables:
            for operation in ("insert", "update", "delete"):
                self.assertIsNotNone(connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                    (f"observe_{table}_{operation}",),
                ).fetchone())
        row = self.detail(self.history("tasks")[0])
        columns = {r[1] for r in connection.execute("PRAGMA table_info(tasks)")}
        self.assertEqual(set(row["after"]), columns)
        self.assertEqual(row["after"]["subject"], "计算器")

    def test_update_null_and_delete_keep_exact_previous_values(self) -> None:
        with self.repo._transaction() as c:
            c.execute("UPDATE tasks SET owner='成员A', active_form='计算中' WHERE task_list_id=?",
                      (self.task_list,))
            c.execute("UPDATE tasks SET owner=NULL WHERE task_list_id=?", (self.task_list,))
            c.execute("DELETE FROM tasks WHERE task_list_id=?", (self.task_list,))
        history = self.history("tasks")
        self.assertEqual([r["operation"] for r in history], ["insert", "update", "update", "delete"])
        update = self.detail(history[1])
        self.assertIsNone(update["before"]["owner"])
        self.assertEqual(update["after"]["owner"], "成员A")
        self.assertEqual(set(update["changed_fields"]), {"owner", "active_form"})
        self.assertIsNone(self.detail(history[2])["after"]["owner"])
        self.assertIsNone(self.detail(history[3])["after"])
        self.assertEqual(self.detail(history[3])["before"]["subject"], "计算器")

    def test_rollback_and_noop_do_not_create_history(self) -> None:
        before = self.history()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.repo._transaction() as c:
                c.execute("UPDATE tasks SET owner='not committed' WHERE task_list_id=?", (self.task_list,))
                raise RuntimeError("rollback")
        with self.repo._transaction() as c:
            c.execute("UPDATE tasks SET subject=subject WHERE task_list_id=?", (self.task_list,))
        self.assertEqual(before, self.history())

    def test_history_is_not_visible_until_commit(self) -> None:
        reader = sqlite3.connect(self.path)
        try:
            previous = reader.execute("SELECT count(*) FROM database_changes").fetchone()[0]
            with self.repo._transaction() as c:
                c.execute("UPDATE tasks SET owner='A' WHERE task_list_id=?", (self.task_list,))
                self.assertEqual(reader.execute("SELECT count(*) FROM database_changes").fetchone()[0], previous)
            self.assertEqual(reader.execute("SELECT count(*) FROM database_changes").fetchone()[0], previous + 1)
        finally:
            reader.close()

    def test_restart_and_pagination_preserve_history(self) -> None:
        before = self.history()
        self.assertEqual(len({r["seq"] for r in before}), len(before))
        self.repo.close()
        self.repo = SQLiteRepository(self.path, recover_incomplete=False)
        self.assertEqual(before, self.history())
        tail = self.repo.list_team_changes(self.team.id, after=before[-1]["seq"])
        self.assertEqual(tail["items"], [])
        self.assertEqual(tail["next_cursor"], before[-1]["seq"])

    def test_existing_database_gets_snapshots_once(self) -> None:
        c = self.repo._connection
        for table in TABLE_SCOPES:
            for operation in ("insert", "update", "delete"):
                c.execute(f"DROP TRIGGER observe_{table}_{operation}")
        c.execute("DROP TABLE database_changes")
        install_observation(c)
        first = self.history()
        self.assertTrue(first)
        self.assertEqual({r["operation"] for r in first}, {"snapshot"})
        install_observation(c)
        self.assertEqual(first, self.history())

    def test_other_team_cannot_see_dedicated_rows_or_detail(self) -> None:
        other_conversation = self.repo.create_conversation(title="Other", workspace=self.temp.name)
        other_run = self.repo.create_run(other_conversation.id)
        other = self.repo.create_team_run(
            conversation_id=other_conversation.id, root_run_id=other_run.id,
            task_list_id=other_conversation.active_task_list_id, base_commit="b" * 40,
        )
        sequence = self.history("team_runs")[0]["seq"]
        self.assertEqual(self.repo.list_team_changes(other.id, seq=sequence)["items"], [])
        self.assertEqual({self.detail(row)["after"]["id"] for row in self.history("team_runs")}, {self.team.id})

    def test_followup_run_visible_before_final_team_tag(self) -> None:
        self.repo.update_run_status(self.run.id, "completed")
        cursor = self.history()[-1]["seq"]
        followup = self.repo.create_run(self.conversation.id, status="running")
        rows = self.repo.list_team_changes(self.team.id, after=cursor)["items"]
        self.assertTrue(any(r["table_name"] == "runs" and r["record_key"]["id"] == followup.id for r in rows))

    def test_message_delivery_ack_and_checkpoint_survive_session_deletion(self) -> None:
        session = self.repo.list_agent_sessions(self.team.id)[0]
        message = self.repo.send_team_message(
            self.team.id, sender_type="runtime", recipient_type="lead",
            recipient_agent_id=session.agent_id, recipient_generation=session.generation,
            message_type="USER_INSTRUCTION", payload={"content": "请检查", "run_id": self.run.id},
            dedupe_key="observation-instruction",
        )
        self.repo.fetch_unacked_team_messages(session.id)
        self.repo.save_agent_session_checkpoint(
            session.id, messages=[], context={}, safe_boundary="test",
            acknowledged_message_ids=[message.id],
        )
        changes = [r for r in self.history("team_messages") if r["record_key"]["id"] == message.id]
        self.assertTrue(any("delivered_at" in r["changed_fields"] for r in changes))
        self.assertTrue(any("acked_at" in r["changed_fields"] for r in changes))
        with self.repo._transaction() as c:
            c.execute("DELETE FROM agent_sessions WHERE id=?", (session.id,))
        checkpoints = self.history("agent_session_checkpoints")
        self.assertEqual([r["operation"] for r in checkpoints], ["insert", "delete"])
        self.assertTrue(self.history("team_message_consumptions"))

    def test_json_redaction_keeps_raw_database_values(self) -> None:
        secret = "observation-secret-value"
        with self.repo._transaction() as c:
            c.execute("UPDATE tasks SET metadata_json=? WHERE task_list_id=?",
                      (json.dumps({"password": secret, "description": "可见"}), self.task_list))
        change = self.history("tasks")[-1]
        detail = self.detail(change)
        self.assertEqual(detail["after"]["metadata_json"]["password"], "[REDACTED]")
        self.assertEqual(detail["after"]["metadata_json"]["description"], "可见")
        raw = self.repo._connection.execute("SELECT after_json FROM database_changes WHERE seq=?", (change["seq"],)).fetchone()[0]
        self.assertIn(secret, raw)

    def test_long_json_arrays_are_not_silently_truncated(self) -> None:
        with self.repo._transaction() as c:
            c.execute("UPDATE tasks SET metadata_json=? WHERE task_list_id=?",
                      (json.dumps({"items": list(range(401)), "nested": {"api_key": "secret"}}), self.task_list))
        data = self.detail(self.history("tasks")[-1])["after"]["metadata_json"]
        self.assertEqual(data["items"], list(range(401)))
        self.assertEqual(data["nested"]["api_key"], "[REDACTED]")

    def test_ignored_constraint_update_does_not_create_phantom_change(self) -> None:
        before = self.history()
        with self.repo._transaction() as c:
            c.execute("UPDATE OR IGNORE tasks SET subject=NULL WHERE task_list_id=?", (self.task_list,))
        self.assertEqual(before, self.history())

    def test_repeated_approval_command_does_not_duplicate_transitions(self) -> None:
        plan = self.repo.create_team_plan_revision(self.team.id, plan={"tasks": ["calculator"]},
            created_by=self.team.lead_agent_id, command_id="plan")
        self.repo.submit_team_plan_revision(self.team.id, plan.revision, command_id="submit")
        decision = dict(decision="approve", decided_by="user", reason="同意", command_id="approve")
        self.repo.decide_team_plan_revision(self.team.id, plan.revision, **decision)
        before = self.history()
        self.repo.decide_team_plan_revision(self.team.id, plan.revision, **decision)
        self.assertEqual(before, self.history())


if __name__ == "__main__":
    unittest.main()
