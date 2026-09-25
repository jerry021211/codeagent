from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from starlette.responses import StreamingResponse
except ImportError:  # pragma: no cover - optional dependency in core-only installs
    TestClient = None  # type: ignore[assignment,misc]
    StreamingResponse = None  # type: ignore[assignment,misc]

from codeagent.events import RunEvent
from codeagent.web.api import create_app
from codeagent.web.storage import SQLiteRepository


class FakeScheduler:
    def __init__(self, repository: SQLiteRepository) -> None:
        self.repository = repository
        self.started = False
        self.stopped = False
        self.submissions: list[tuple[str, str, bool]] = []
        self.mcp_reloads: list[str] = []
        self.modes: list[str] = []

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def submit(self, conversation_id: str, content: str, *, use_team: bool = False, mode: str = "normal"):
        self.modes.append(mode)
        self.submissions.append((conversation_id, content, use_team))
        run = self.repository.create_run(conversation_id)
        self.repository.create_message(
            conversation_id,
            role="user",
            content=content,
            run_id=run.id,
        )
        return run

    def cancel(self, run_id: str):
        return self.repository.request_run_cancel(run_id)

    def resolve_approval(self, run_id: str, approval_id: str, decision: str):
        approval = self.repository.get_approval(approval_id)
        if approval is None or approval.run_id != run_id:
            raise LookupError(approval_id)
        return self.repository.resolve_approval(approval_id, decision)

    def reload_mcp(self, workspace: str) -> bool:
        self.mcp_reloads.append(workspace)
        return True


@unittest.skipIf(TestClient is None, "FastAPI test dependencies are not installed")
class WebApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.repository = SQLiteRepository(
            self.workspace / "state.db",
            recover_incomplete=False,
        )
        self.scheduler = FakeScheduler(self.repository)
        self.env = SimpleNamespace(
            model_id="test-model",
            max_tokens=4096,
            max_iterations=12,
        )
        self.app = create_app(
            repository=self.repository,
            scheduler=self.scheduler,
            workspace=self.workspace,
            env=self.env,
            static_dir=self.workspace / "missing-dist",
        )
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        # Seed the process-bound CSRF cookie before mutation requests.
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.repository.close()
        self.temp_dir.cleanup()

    def test_user_question_answer_contract(self) -> None:
        conversation = self.repository.create_conversation(workspace=self.workspace)
        run = self.repository.create_run(conversation.id)
        self.repository.start_run(run.id)
        question = self.repository.create_user_question(run.id, "格式？", ["CSV", "JSON"])
        url = f"/api/runs/{run.id}/questions/{question['id']}/answer"
        self.assertEqual(self.client.get(f"/api/runs/{run.id}/questions").json(), [question])
        self.assertEqual(self.client.post(url, json={"answer": " "}).status_code, 422)
        self.assertEqual(self.client.post(url, json={"answer": "x" * 20001}).status_code, 422)
        self.assertEqual(self.client.post(url, json={"answer": "JSON"}).status_code, 200)
        self.assertEqual(self.client.post(url, json={"answer": "JSON"}).status_code, 200)
        self.assertEqual(self.client.post(url, json={"answer": "CSV"}).status_code, 409)
        self.assertEqual(self.client.post(f"/api/runs/wrong/questions/{question['id']}/answer", json={"answer": "JSON"}).status_code, 404)
        pending = self.repository.create_user_question(run.id, "文件名？", [])
        self.repository.request_run_cancel(run.id)
        self.assertEqual(self.client.post(f"/api/runs/{run.id}/questions/{pending['id']}/answer", json={"answer": "out.json"}).status_code, 409)

    def test_activity_history_is_paginated_without_text_delta_replay(self):
        conversation = self.repository.create_conversation(workspace=self.workspace)
        run = self.repository.create_run(conversation.id)
        other_conversation = self.repository.create_conversation(workspace=self.workspace)
        other = self.repository.create_run(other_conversation.id)
        for kind in ["tool.requested", "model.text_delta", "tool.completed", "model.text_delta", "tool.interrupted"]:
            self.repository.append_event(RunEvent(type=kind, run_id=run.id, conversation_id=conversation.id, payload={"tool_use_id": "one"}))
        self.repository.append_event(RunEvent(type="tool.started", run_id=other.id))
        self.repository.update_run_status(run.id, "cancelled")
        first = self.client.get(f"/api/runs/{run.id}/activity?limit=2")
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(body["status"], "cancelled")
        self.assertEqual([event["type"] for event in body["events"]], ["tool.requested", "tool.completed"])
        second = self.client.get(f"/api/runs/{run.id}/activity?after={body['next_after']}&limit=2").json()
        self.assertEqual([event["type"] for event in second["events"]], ["tool.interrupted"])
        self.assertIsNone(second["next_after"])
        self.assertEqual(self.client.get("/api/runs/missing/activity").status_code, 404)

    def test_lifespan_health_and_runtime_config(self) -> None:
        self.assertTrue(self.scheduler.started)
        health = self.client.get("/healthz")
        self.assertEqual(health.json(), {"status": "ok", "database": "ok"})

        response = self.client.get("/api/runtime-config")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["workspace"], str(self.workspace.resolve()))
        self.assertEqual(body["max_tokens"], 4096)
        self.assertTrue(body["features"]["sse"])
        self.assertFalse(body["features"]["agent_team"])
        disabled = self.client.get("/api/teams")
        self.assertEqual(disabled.status_code, 503)

    def test_discuss_mode_api_and_team_conflict(self) -> None:
        conversation = self.repository.create_conversation(title="Discuss", workspace=str(self.workspace))
        url = f"/api/conversations/{conversation.id}/runs"
        response = self.client.post(url, json={"content": "inspect", "mode": "discuss"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.scheduler.modes, ["discuss"])
        conflict = self.client.post(url, json={"content": "inspect", "mode": "discuss", "useTeam": True})
        self.assertEqual(conflict.status_code, 409)
        invalid = self.client.post(url, json={"content": "inspect", "mode": "unsafe"})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(len(self.scheduler.submissions), 1)

    def test_task_event_endpoint_returns_sse_stream(self) -> None:
        created = self.client.post(
            "/api/conversations",
            json={"title": "Task stream", "workspace": str(self.workspace)},
        ).json()
        route = next(
            route
            for route in self.app.routes
            if getattr(route, "path", "") == "/api/task-lists/{task_list_id}/events"
        )

        response = asyncio.run(
            route.endpoint(
                request=SimpleNamespace(),
                task_list_id=created["active_task_list_id"],
                after=0,
                last_event_id=None,
            )
        )

        self.assertIsInstance(response, StreamingResponse)
        self.assertEqual(response.media_type, "text/event-stream")

    def test_conversation_crud_and_message_contract(self) -> None:
        created = self.client.post(
            "/api/conversations",
            json={"title": "Transport test", "workspace": str(self.workspace)},
        )
        self.assertEqual(created.status_code, 201)
        conversation_id = created.json()["id"]
        self.assertEqual(created.json()["workspace"], str(self.workspace.resolve()))

        workspaces = self.client.get(
            "/api/workspaces", params={"path": str(self.workspace)}
        )
        self.assertEqual(workspaces.status_code, 200)
        self.assertEqual(workspaces.json()["current"], str(self.workspace.resolve()))

        rejected = self.client.post(
            "/api/conversations",
            json={"title": "Missing", "workspace": str(self.workspace / "missing")},
        )
        self.assertEqual(rejected.status_code, 422)

        renamed = self.client.patch(
            f"/api/conversations/{conversation_id}",
            json={"title": "Renamed"},
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["title"], "Renamed")

        run = self.client.post(
            f"/api/conversations/{conversation_id}/runs",
            json={"content": "Implement the feature"},
        )
        self.assertEqual(run.status_code, 202)
        self.assertEqual(run.json()["status"], "queued")
        self.assertEqual(
            self.scheduler.submissions[-1],
            (conversation_id, "Implement the feature", False),
        )

        team_disabled = self.client.post(
            f"/api/conversations/{conversation_id}/runs",
            json={"content": "Use a team", "useTeam": True},
        )
        self.assertEqual(team_disabled.status_code, 409)

        messages = self.client.get(
            f"/api/conversations/{conversation_id}/messages"
        )
        self.assertEqual(messages.json()[0]["content"], "Implement the feature")

        listing = self.client.get("/api/conversations")
        item = listing.json()[0]
        self.assertEqual(item["last_message"], "Implement the feature")
        self.assertEqual(item["active_run_id"], run.json()["run_id"])
        self.assertEqual(item["run_status"], "queued")

        archived = self.client.patch(
            f"/api/conversations/{conversation_id}", json={"archived": True}
        )
        self.assertIsNotNone(archived.json()["archived_at"])
        self.assertEqual(self.client.get("/api/conversations").json(), [])
        self.assertEqual(
            len(self.client.get("/api/conversations?archived=true").json()), 1
        )

    def test_delete_conversation_cleans_history_and_preserves_other_conversations(self) -> None:
        conversation = self.repository.create_conversation(workspace=str(self.workspace))
        other = self.repository.create_conversation(workspace=str(self.workspace))
        run = self.repository.create_run(conversation.id)
        message = self.repository.create_message(conversation.id, role="user", content="delete me", run_id=run.id)
        self.repository.append_event(RunEvent(type="run.started", run_id=run.id))
        self.repository.update_run_status(run.id, "completed")
        self.repository.create_task(conversation.active_task_list_id, subject="Private", description="Delete with chat")
        self.repository.update_conversation(conversation.id, archived=True)
        project_file = self.workspace / "keep.txt"
        project_file.write_text("keep", encoding="utf-8")

        url = f"/api/conversations/{conversation.id}"
        deleted = self.client.delete(url)

        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(deleted.content, b"")
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)
        self.assertIsNone(self.repository.get_message(message.id))
        self.assertIsNone(self.repository.get_run(run.id))
        self.assertIsNone(self.repository.get_task_list(conversation.active_task_list_id))
        self.assertIsNotNone(self.repository.get_conversation(other.id))
        self.assertIsNotNone(self.repository.get_task_list(other.active_task_list_id))
        self.assertEqual(project_file.read_text(encoding="utf-8"), "keep")
        remaining = self.repository._connection.execute(
            "SELECT COUNT(*) FROM database_changes WHERE scope_id IN (?, ?, ?)",
            (conversation.id, run.id, conversation.active_task_list_id),
        ).fetchone()[0]
        self.assertEqual(remaining, 0)
        self.assertEqual(self.repository._connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_delete_conversation_rejects_active_runs(self) -> None:
        for run_status in ("queued", "running"):
            with self.subTest(status=run_status):
                conversation = self.repository.create_conversation(workspace=str(self.workspace))
                run = self.repository.create_run(conversation.id, status=run_status)
                response = self.client.delete(f"/api/conversations/{conversation.id}")
                self.assertEqual(response.status_code, 409)
                self.assertIn("先停止", response.json()["detail"])
                self.assertIsNotNone(self.repository.get_conversation(conversation.id))
                self.assertEqual(self.repository.get_run(run.id).status, run_status)

    def test_delete_conversation_rejects_active_team_after_root_finishes(self) -> None:
        conversation = self.repository.create_conversation(workspace=str(self.workspace))
        run = self.repository.create_run(conversation.id)
        self.repository.create_team_run(
            conversation_id=conversation.id, root_run_id=run.id,
            task_list_id=conversation.active_task_list_id, base_commit="a" * 40,
        )
        self.repository.update_run_status(run.id, "completed")
        response = self.client.delete(f"/api/conversations/{conversation.id}")
        self.assertEqual(response.status_code, 409)
        self.assertIn("团队", response.json()["detail"])
        self.assertIsNotNone(self.repository.get_conversation(conversation.id))

    def test_disabled_team_cannot_resume_through_normal_chat(self) -> None:
        conversation = self.client.post(
            "/api/conversations",
            json={"title": "Existing team", "workspace": str(self.workspace)},
        ).json()
        root_run = self.repository.create_run(conversation["id"])
        team = self.repository.create_team_run(
            conversation_id=conversation["id"],
            root_run_id=root_run.id,
            task_list_id=conversation["active_task_list_id"],
            base_commit="a" * 40,
            lead_agent_id="disabled_team_lead",
        )
        sessions_before = self.repository.list_agent_sessions(team.id)

        response = self.client.post(
            f"/api/conversations/{conversation['id']}/runs",
            json={"content": "继续", "useTeam": False},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("Team 功能已关闭", response.json()["detail"])
        self.assertEqual(self.scheduler.submissions, [])
        self.assertEqual(self.repository.get_team_run(team.id), team)
        self.assertEqual(self.repository.list_agent_sessions(team.id), sessions_before)

        ordinary = self.client.post(
            "/api/conversations",
            json={"title": "Ordinary", "workspace": str(self.workspace)},
        ).json()
        response = self.client.post(
            f"/api/conversations/{ordinary['id']}/runs",
            json={"content": "普通任务"},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.scheduler.submissions, [(ordinary["id"], "普通任务", False)])

    def test_run_usage_cancel_and_approval(self) -> None:
        conversation = self.repository.create_conversation(title="Run test")
        run = self.scheduler.submit(conversation.id, "hello")
        self.repository.record_model_call(
            run.id,
            model="test-model",
            call_kind="main",
            usage={
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 2,
                "available": True,
            },
        )
        approval = self.repository.create_approval(
            run.id,
            tool_name="bash",
            tool_input={"command": "dangerous"},
            reason="Command requires confirmation",
        )

        fetched = self.client.get(f"/api/runs/{run.id}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["token_usage"]["total_tokens"], 23)
        self.assertEqual(fetched.json()["token_usage"]["model"], "test-model")

        decision = self.client.post(
            f"/api/runs/{run.id}/approvals/{approval.id}",
            json={"decision": "allow"},
        )
        self.assertEqual(decision.status_code, 200)
        self.assertEqual(decision.json()["status"], "allowed")
        self.assertEqual(decision.json()["input"], {"command": "dangerous"})

        cancelled = self.client.post(f"/api/runs/{run.id}/cancel")
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "cancelled")

    def test_workspace_search_returns_fuzzy_candidates(self) -> None:
        project = self.workspace / "ii-project-00"
        project.mkdir()
        response = self.client.get(
            "/api/workspaces", params={"query": str(self.workspace / "II00")}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["current"], str(self.workspace.resolve()))
        self.assertEqual([entry["path"] for entry in response.json()["entries"]], [str(project.resolve())])
        rejected = self.client.post(
            "/api/conversations", json={"workspace": str(self.workspace / "II00")}
        )
        self.assertEqual(rejected.status_code, 422)

    def test_mcp_config_can_be_managed_for_workspace(self) -> None:
        empty = self.client.get(
            "/api/mcp/servers", params={"workspace": str(self.workspace)}
        )
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json()["servers"], [])

        created = self.client.post(
            "/api/mcp/servers",
            json={
                "workspace": str(self.workspace),
                "name": "github",
                "transport": "http",
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "Bearer ${GITHUB_PAT}"},
            },
        )
        self.assertEqual(created.status_code, 201)
        self.assertFalse(created.json()["restart_required"])
        self.assertEqual(self.scheduler.mcp_reloads, [str(self.workspace)])
        self.assertEqual(created.json()["servers"][0]["name"], "github")
        self.assertEqual(
            created.json()["servers"][0]["header_keys"], ["Authorization"]
        )
        self.assertNotIn("Bearer", json.dumps(created.json()))

        payload = json.loads((self.workspace / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(
            payload["mcpServers"]["github"]["headers"]["Authorization"],
            "Bearer ${GITHUB_PAT}",
        )

        deleted = self.client.delete(
            "/api/mcp/servers/github", params={"workspace": str(self.workspace)}
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["servers"], [])

    def test_task_list_and_task_crud(self) -> None:
        created = self.client.post(
            "/api/conversations",
            json={"title": "Task API", "workspace": str(self.workspace)},
        )
        conversation = created.json()
        task_list_id = conversation["active_task_list_id"]
        self.assertTrue(task_list_id)

        task = self.client.post(
            f"/api/task-lists/{task_list_id}/tasks",
            json={"subject": "Build API", "description": "Implement and test it"},
        )
        self.assertEqual(task.status_code, 201)
        self.assertEqual(task.json()["task"]["id"], "1")
        self.assertEqual(
            set(task.json()["task"]),
            {"id", "subject", "description", "activeForm", "owner", "status", "blocks", "blockedBy", "metadata"},
        )

        dependent = self.client.post(
            f"/api/task-lists/{task_list_id}/tasks",
            json={
                "subject": "Integrate API",
                "description": "Integrate after the API is ready",
                "blockedBy": ["1"],
            },
        )
        self.assertEqual(dependent.status_code, 201)
        self.assertEqual(dependent.json()["task"]["blockedBy"], ["1"])

        updated = self.client.patch(
            f"/api/task-lists/{task_list_id}/tasks/1",
            json={"expectedRevision": 1, "status": "in_progress", "owner": "human"},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["task"]["owner"], "human")

        stale = self.client.patch(
            f"/api/task-lists/{task_list_id}/tasks/1",
            json={"expectedRevision": 1, "subject": "Stale write"},
        )
        self.assertEqual(stale.status_code, 409)

        listing = self.client.get(f"/api/task-lists/{task_list_id}/tasks")
        self.assertEqual(len(listing.json()), 2)
        activity = self.client.get(
            f"/api/task-lists/{task_list_id}/tasks/1/activity"
        )
        self.assertEqual([item["eventType"] for item in activity.json()], ["created", "updated"])

    def test_sse_replays_after_latest_cursor_and_closes_on_terminal(self) -> None:
        conversation = self.repository.create_conversation(title="SSE test")
        run = self.repository.create_run(conversation.id)
        first = self.repository.append_event(
            RunEvent(
                type="run.queued",
                conversation_id=conversation.id,
                run_id=run.id,
                payload={"status": "queued"},
            )
        )
        second = self.repository.append_event(
            RunEvent(
                type="run.cancelled",
                conversation_id=conversation.id,
                run_id=run.id,
                payload={"status": "cancelled"},
            )
        )
        self.repository.update_run_status(run.id, "cancelled")

        response = self.client.get(
            f"/api/runs/{run.id}/events?after=0",
            headers={"Last-Event-ID": str(first.seq)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/event-stream")
        self.assertNotIn(f"id: {first.seq}\n", response.text)
        self.assertIn(f"id: {second.seq}\n", response.text)
        self.assertIn("event: run.cancelled", response.text)
        data_line = next(
            line for line in response.text.splitlines() if line.startswith("data: ")
        )
        payload = json.loads(data_line.removeprefix("data: "))
        self.assertEqual(payload["run_id"], run.id)
        self.assertEqual(payload["seq"], second.seq)

        drained = self.client.get(
            f"/api/runs/{run.id}/events?after={second.seq}"
        )
        self.assertEqual(drained.status_code, 200)
        self.assertIn("event: stream.end", drained.text)
        self.assertNotIn("event: run.", drained.text)

    def test_conversation_summary_exposes_waiting_question_and_latest_run(self) -> None:
        conversation = self.repository.create_conversation(workspace=str(self.workspace))
        run = self.repository.create_run(conversation.id)
        self.repository.start_run(run.id)
        question = self.repository.create_user_question(run.id, "format?", ["CSV"])
        response = self.client.get(f"/api/conversations/{conversation.id}").json()
        self.assertEqual(response["latest_run_id"], run.id)
        self.assertTrue(response["waiting_for_answer"])
        self.repository.answer_user_question(run.id, question["id"], "CSV")
        self.repository.update_run_status(run.id, "completed")
        response = self.client.get(f"/api/conversations/{conversation.id}").json()
        self.assertEqual(response["latest_run_id"], run.id)
        self.assertIsNone(response["active_run_id"])
        self.assertFalse(response["waiting_for_answer"])

    def test_sse_drains_multiple_pages_before_end_marker(self) -> None:
        conversation = self.repository.create_conversation()
        run = self.repository.create_run(conversation.id)
        for index in range(505):
            self.repository.append_event(RunEvent(
                type="model.text_delta", run_id=run.id, conversation_id=conversation.id,
                payload={"text": str(index)},
            ))
        self.repository.update_run_status(run.id, "cancelled")
        response = self.client.get(f"/api/runs/{run.id}/events")
        self.assertEqual(response.text.count("event: model.text_delta"), 505)
        self.assertEqual(response.text.count("event: stream.end"), 1)
        self.assertLess(response.text.index("id: 505\n"), response.text.index("event: stream.end"))

    def test_sse_waits_for_final_event_after_terminal_status_was_committed(self) -> None:
        conversation = self.repository.create_conversation()
        run = self.repository.create_run(conversation.id)
        self.repository.start_run(run.id)
        self.repository.update_run_status(run.id, "completed")
        checked, released = threading.Event(), threading.Event()

        def pending(_run_id):
            checked.set()
            return not released.is_set()

        def finish_event():
            if checked.wait(3):
                self.repository.append_event(RunEvent(
                    type="run.completed", run_id=run.id, conversation_id=conversation.id,
                    payload={"status": "completed"},
                ))
            released.set()

        worker = threading.Thread(target=finish_event)
        with patch.object(self.scheduler, "is_run_pending", pending, create=True):
            worker.start()
            try:
                response = self.client.get(f"/api/runs/{run.id}/events")
            finally:
                worker.join(timeout=3)
        self.assertIn("event: run.completed", response.text)
        self.assertIn("event: stream.end", response.text)
        self.assertLess(response.text.index("event: run.completed"), response.text.index("event: stream.end"))

    def test_local_security_and_static_fallback(self) -> None:
        no_cookie_app = create_app(
            repository=self.repository,
            scheduler=self.scheduler,
            workspace=self.workspace,
            static_dir=self.workspace / "missing-dist",
        )
        with TestClient(no_cookie_app) as fresh:
            fresh.cookies.set("codeagent_session", "forged")
            blocked = fresh.post(
                "/api/conversations", json={}
            )
            self.assertEqual(blocked.status_code, 403)

            fresh.get("/api/health")
            bad_origin = fresh.post(
                "/api/conversations",
                json={},
                headers={"Origin": "https://attacker.example"},
            )
            self.assertEqual(bad_origin.status_code, 403)

            bad_host = fresh.get(
                "/api/health", headers={"Host": "attacker.example"}
            )
            self.assertEqual(bad_host.status_code, 400)

        missing = self.client.get("/some/client/route")
        self.assertEqual(missing.status_code, 503)
        self.assertIn("Frontend build not found", missing.json()["detail"])

    def test_spa_serves_assets_and_history_fallback(self) -> None:
        static = self.workspace / "dist"
        assets = static / "assets"
        assets.mkdir(parents=True)
        (static / "index.html").write_text("<main>Cockpit</main>", encoding="utf-8")
        (assets / "app.js").write_text("console.log('ok')", encoding="utf-8")

        app = create_app(
            repository=self.repository,
            scheduler=self.scheduler,
            workspace=self.workspace,
            static_dir=static,
        )
        with TestClient(app) as client:
            index = client.get("/conversation/deep-link")
            self.assertEqual(index.status_code, 200)
            self.assertIn("Cockpit", index.text)
            asset = client.get("/assets/app.js")
            self.assertEqual(asset.status_code, 200)
            self.assertIn("immutable", asset.headers["cache-control"])


if __name__ == "__main__":
    unittest.main()
