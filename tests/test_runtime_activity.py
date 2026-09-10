from __future__ import annotations

import unittest

from codeagent import ModelResponse
from codeagent.recovery import RecoveryRuntime
from codeagent.recovery.models import RecoveryConfig
from codeagent.runtime.activity import ExecutionActivity
from codeagent.runtime.cancellation import (
    CancellationToken,
    CancelledError,
    ModelCallTimeout,
)
from codeagent.permissions.broker import WaitingPermissionBroker


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.token = CancellationToken()
        self.activity = ExecutionActivity(self.token, clock=self.clock)

    def test_long_known_model_wait_is_not_a_stale_worker(self):
        with self.activity.operation("model", 600):
            self.clock.now = 161
            self.assertIsNone(self.activity.timeout_reason(120))
            self.activity.touch(response=True)
        self.clock.now += 121
        self.assertEqual(self.activity.timeout_reason(120), "worker_heartbeat_timeout")

    def test_content_resets_idle_but_never_total_deadline(self):
        with self.assertRaisesRegex(ModelCallTimeout, "model_call_timeout"):
            with self.activity.operation("model", 600):
                for now in (200, 400, 599):
                    self.clock.now = now
                    self.activity.touch(response=True)
                    self.assertIsNone(self.activity.timeout_reason(120))
                self.clock.now = 600

    def test_no_response_times_out_and_does_not_need_supervisor_tick(self):
        with self.assertRaisesRegex(ModelCallTimeout, "model_response_timeout"):
            with self.activity.model_request():
                self.clock.now = 301
                self.activity.check()

    def test_user_cancellation_reason_is_not_overwritten(self):
        self.token.cancel("user cancelled")
        self.token.cancel("model_call_timeout", reason_code="model_call_timeout")
        with self.assertRaises(CancelledError) as caught:
            self.token.raise_if_cancelled()
        self.assertNotIsInstance(caught.exception, ModelCallTimeout)
        self.assertEqual(str(caught.exception), "user cancelled")

    def test_human_approval_is_not_a_model_or_worker_timeout(self):
        with self.activity.operation("approval", float("inf")):
            self.clock.now = 1000
            self.assertIsNone(self.activity.timeout_reason(120))

    def test_invalid_deadlines_are_rejected(self):
        for invalid in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                ExecutionActivity(self.token, response_timeout=invalid)

    def test_permission_broker_reports_approval_and_keeps_its_own_decision(self):
        labels = []
        self.activity.on_activity = labels.append

        def approve(request):
            self.clock.now = 500
            self.assertIsNone(self.activity.timeout_reason(120))
            broker.resolve(request.id, True)

        broker = WaitingPermissionBroker(on_request=approve)
        broker.execution_activity = self.activity
        self.assertTrue(
            broker.request("bash", {}, "needs approval", cancellation=self.token)
        )
        self.assertEqual(labels, ["permission_waiting", "executing"])

    def test_tool_has_its_own_deadline_and_nested_call_cannot_extend_it(self):
        with self.activity.operation("tool", 400):
            self.clock.now = 150
            self.assertIsNone(self.activity.timeout_reason(120))
        with self.assertRaises(ModelCallTimeout):
            with self.activity.operation("model", 600):
                self.clock.now = 590
                with self.activity.operation("model", 900):
                    self.clock.now = 601

    def test_reports_are_throttled_and_do_not_include_thinking(self):
        labels = []
        self.activity.on_activity = labels.append
        with self.activity.model_request():
            for now in range(21):
                self.clock.now = now
                self.activity.touch(response=True)
        self.assertEqual(
            labels,
            [
                "model_waiting",
                "model_receiving",
                "model_receiving",
                "model_receiving",
                "executing",
            ],
        )

    def test_interrupt_closes_only_active_request_once(self):
        closed = []
        with self.activity.model_request():
            self.activity.set_request_closer(lambda: closed.append(1))
            self.activity.interrupt_request()
            self.activity.interrupt_request()
        self.assertEqual(closed, [1])

    def test_response_limit_regeneration_keeps_logical_deadline(self):
        recovery = RecoveryRuntime(activity=self.activity)
        state = recovery.create_state(model="fake", max_tokens=8000)
        calls = []

        def call(*_args):
            calls.append(1)
            self.clock.now += 250
            return ModelResponse("max_tokens", [])

        result = recovery.call_model(call, state=state, messages=[])
        decision = recovery.handle_response(result.response, state=state, messages=[])
        self.assertTrue(decision.retry)
        self.assertEqual(state.model_deadline, 600)
        self.clock.now = 601
        with self.assertRaises(ModelCallTimeout):
            recovery.call_model(call, state=state, messages=[])
        self.assertEqual(len(calls), 1)

    def test_runtime_deadline_never_enters_network_retry(self):
        recovery = RecoveryRuntime(
            RecoveryConfig(sleep_enabled=False), activity=self.activity
        )
        state = recovery.create_state(model="fake", max_tokens=8000)
        calls = []

        def call(*_args):
            calls.append(1)
            self.clock.now = 301
            return ModelResponse("end_turn", [])

        with self.assertRaises(ModelCallTimeout):
            recovery.call_model(call, state=state, messages=[])
        self.assertEqual(calls, [1])
        self.assertEqual(state.retry_count, 0)

    def test_network_retries_cannot_reset_total_deadline(self):
        recovery = RecoveryRuntime(
            RecoveryConfig(sleep_enabled=False), activity=self.activity
        )
        state = recovery.create_state(model="fake", max_tokens=8000)
        calls = []

        def call(*_args):
            calls.append(1)
            self.clock.now += 250
            self.activity.touch(response=True)
            self.activity.check()
            raise ConnectionError("connection interrupted")

        with self.assertRaisesRegex(ModelCallTimeout, "model_call_timeout"):
            recovery.call_model(call, state=state, messages=[])
        self.assertEqual(len(calls), 3)
        self.assertEqual(state.model_deadline, 600)

    def test_success_allows_next_logical_call_a_new_deadline(self):
        recovery = RecoveryRuntime(activity=self.activity)
        state = recovery.create_state(model="fake", max_tokens=8000)
        result = recovery.call_model(
            lambda *_: ModelResponse("tool_use", []), state=state, messages=[]
        )
        recovery.handle_response(result.response, state=state, messages=[])
        self.assertIsNone(state.model_deadline)


if __name__ == "__main__":
    unittest.main()
