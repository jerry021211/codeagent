from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - optional dependency in core-only installs
    TestClient = None  # type: ignore[assignment,misc]

from codeagent.events import RunEvent
from codeagent.web.api import create_app
from codeagent.web.storage import SQLiteRepository


class FakeScheduler:
    def __init__(self, repository: SQLiteRepository) -> None:
        self.repository = repository
        self.started = False
        self.stopped = False
        self.submissions: list[tuple[str, str]] = []

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def submit(self, conversation_id: str, content: str):
        self.submissions.append((conversation_id, content))
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
        self.assertEqual(len(listing.json()), 1)
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
        self.assertEqual(drained.text, "")

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
