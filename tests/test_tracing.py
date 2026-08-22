from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from codeagent.tools.base import ToolDefinition
from codeagent.tools.registry import ToolRegistry
from codeagent.tracing import TraceHandle


class TraceHandleTests(unittest.TestCase):
    def test_end_forwards_outputs_and_error(self) -> None:
        run = Mock()

        TraceHandle(run).end(outputs={"status": "error"}, error="failed")

        run.end.assert_called_once_with(
            outputs={"status": "error"},
            error="failed",
        )

    def test_registry_marks_caught_tool_exception_as_trace_error(self) -> None:
        handle = Mock()

        @contextmanager
        def fake_trace(*args, **kwargs):
            yield handle

        registry = ToolRegistry()
        registry.register_handler(
            ToolDefinition(
                name="broken",
                description="Always fails.",
                input_schema={"type": "object", "properties": {}},
            ),
            lambda: (_ for _ in ()).throw(ValueError("boom")),
        )

        with patch("codeagent.tools.registry.trace_run", fake_trace):
            output = registry.execute("broken")

        self.assertEqual(output, "Error: ValueError: boom")
        handle.end.assert_called_once_with(
            outputs={"output": output, "status": "error"},
            error=output,
        )


if __name__ == "__main__":
    unittest.main()
