from __future__ import annotations

import unittest

from codeagent.teams.tasks import requires_attempt_plan, validate_task_execution


class TeamTaskExecutionTests(unittest.TestCase):
    def test_attempt_plan_is_a_code_write_requirement(self) -> None:
        cases = [
            ({}, False),
            ({"kind": "analysis", "plan_required": False}, False),
            ({"kind": "analysis", "risk_level": "high"}, False),
            ({"kind": "code", "plan_required": False}, False),
            ({"kind": "code", "plan_required": True}, True),
            ({"kind": "code", "risk_level": "high", "plan_required": False}, True),
        ]
        for metadata, expected in cases:
            with self.subTest(metadata=metadata):
                self.assertIs(requires_attempt_plan(metadata), expected)

    def test_plan_required_never_coerces_strings_numbers_or_null(self) -> None:
        for value in ("true", "false", "", 0, 1, None, [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "JSON boolean"):
                    validate_task_execution({"kind": "code", "plan_required": value})

    def test_analysis_cannot_request_a_write_plan_or_write_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "analysis Tasks cannot require"):
            validate_task_execution({"kind": "analysis", "plan_required": True})
        with self.assertRaisesRegex(ValueError, "analysis Tasks cannot declare"):
            validate_task_execution({"kind": "analysis", "write_scopes": ["docs/"]})

    def test_unknown_risk_is_rejected_not_treated_as_low(self) -> None:
        for risk in ("hign", "", None, [], 1):
            with self.subTest(risk=risk):
                with self.assertRaisesRegex(ValueError, "risk_level"):
                    validate_task_execution({"kind": "code", "risk_level": risk})

    def test_kind_must_match_runtime_role_names_without_coercion(self) -> None:
        for kind in (" code ", "CODE", "", None, [], 1):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "Task kind"):
                    validate_task_execution({"kind": kind})


if __name__ == "__main__":
    unittest.main()
