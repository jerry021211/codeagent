from __future__ import annotations

import threading
import time
import unittest

from codeagent.permissions import CliPermissionBroker, WaitingPermissionBroker
from codeagent.runtime import CancellationToken, CancelledError


class CancellationTokenTests(unittest.TestCase):
    def test_cancel_is_idempotent_and_preserves_first_reason(self) -> None:
        token = CancellationToken()

        token.cancel("first reason")
        token.cancel("second reason")

        self.assertTrue(token.is_cancelled)
        self.assertEqual(token.reason, "first reason")
        with self.assertRaisesRegex(CancelledError, "first reason") as raised:
            token.raise_if_cancelled()
        self.assertEqual(raised.exception.reason, "first reason")

    def test_wait_wakes_when_cancelled(self) -> None:
        token = CancellationToken()

        timer = threading.Timer(0.02, token.cancel)
        timer.start()
        try:
            self.assertTrue(token.wait(1.0))
        finally:
            timer.join()

    def test_wait_returns_false_on_timeout(self) -> None:
        self.assertFalse(CancellationToken().wait(0.001))


class PermissionBrokerTests(unittest.TestCase):
    def test_cli_broker_delegates_to_prompt(self) -> None:
        calls: list[tuple[str, dict[str, object], str]] = []
        broker = CliPermissionBroker(
            prompt=lambda tool, args, reason: calls.append((tool, args, reason)) or True
        )

        allowed = broker.request("bash", {"command": "rm file"}, "destructive")

        self.assertTrue(allowed)
        self.assertEqual(
            calls,
            [("bash", {"command": "rm file"}, "destructive")],
        )

    def test_waiting_broker_publishes_and_resolves_request(self) -> None:
        published = threading.Event()
        broker = WaitingPermissionBroker(
            default_timeout=1.0,
            on_request=lambda request: published.set(),
        )
        result: list[bool] = []
        worker = threading.Thread(
            target=lambda: result.append(
                broker.request("write_file", {"file_path": "a.txt"}, "confirm")
            )
        )

        worker.start()
        self.assertTrue(published.wait(1.0))
        pending = broker.pending
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].tool_name, "write_file")
        self.assertEqual(pending[0].tool_input, {"file_path": "a.txt"})
        self.assertTrue(broker.resolve(pending[0].id, True))
        worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [True])
        self.assertEqual(broker.pending, ())
        self.assertFalse(broker.resolve(pending[0].id, False))

    def test_waiting_broker_denies_on_timeout(self) -> None:
        broker = WaitingPermissionBroker(default_timeout=0.01, poll_interval=0.002)

        self.assertFalse(broker.request("bash", {}, "confirm"))
        self.assertEqual(broker.pending, ())

    def test_waiting_broker_honors_cancellation_and_cleans_up(self) -> None:
        token = CancellationToken()
        published = threading.Event()
        broker = WaitingPermissionBroker(
            default_timeout=None,
            on_request=lambda request: published.set(),
            poll_interval=0.002,
        )
        errors: list[BaseException] = []

        def request() -> None:
            try:
                broker.request("bash", {}, "confirm", cancellation=token)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=request)
        worker.start()
        self.assertTrue(published.wait(1.0))
        token.cancel("run stopped")
        worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], CancelledError)
        self.assertEqual(str(errors[0]), "run stopped")
        self.assertEqual(broker.pending, ())

    def test_request_callback_failure_does_not_leave_pending_state(self) -> None:
        def fail(_request: object) -> None:
            raise RuntimeError("publish failed")

        broker = WaitingPermissionBroker(on_request=fail)

        with self.assertRaisesRegex(RuntimeError, "publish failed"):
            broker.request("bash", {}, "confirm")
        self.assertEqual(broker.pending, ())


if __name__ == "__main__":
    unittest.main()
