from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codeagent import EnvironmentConfig
from codeagent.cli import create_default_subagent_environment
from codeagent.context import ContextConfig
from codeagent.events import EventEmitter, ExecutionContext
from codeagent.memory import MemoryConfig
from codeagent.messages import ToolUse
from codeagent.permissions import WaitingPermissionBroker
from codeagent.permissions.discuss import discuss_tool_guard
from codeagent.runtime import CancellationToken
from codeagent.tools import LoadContextHistoryTool
from codeagent.tools.runtime_data import _history_record
from codeagent.web.factory import WebAgentFactory
from codeagent.web.storage import SQLiteRepository


class ContextHistoryToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.own = self.root / "own"
        self.own.mkdir()
        self.tool = LoadContextHistoryTool(self.own)

    def write_history(self, messages, name="history.jsonl") -> Path:
        target = self.own / name
        target.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in messages) + "\n", encoding="utf-8")
        return target

    def test_message_pages_preserve_raw_facts_and_do_not_change_archive(self) -> None:
        path = self.write_history([
            {"role": "user", "content": "目标"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "write-1", "name": "write_file", "input": {"content": "original"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "write-1", "is_error": True, "content": "失败"}]},
        ])
        original = path.read_bytes()
        middle = self.tool.run(path.name, message_offset=2, message_limit=1)
        self.assertIn("message=2", middle)
        self.assertIn('"id": "write-1"', middle)
        self.assertIn("next_message_offset=3", middle)
        final = self.tool.run(str(path), message_offset=3, message_limit=1)
        self.assertIn('"is_error": true', final)
        self.assertIn("失败", final)
        self.assertNotIn("more_messages=true", final)
        self.assertEqual(path.read_bytes(), original)
        self.assertIn("no messages", self.tool.run(str(path), message_offset=4))

    def test_long_jsonl_records_offer_exact_unicode_character_pages(self) -> None:
        line = json.dumps({"role": "user", "content": "甲😀乙" * 20_000 + "TAIL"}, ensure_ascii=False)
        path = self.own / "large.jsonl"
        path.write_text(line + "\n", encoding="utf-8")
        for offset in (0, 8189, 20_003, len(line) - 4, len(line) + 5):
            with self.subTest(offset=offset):
                output = self.tool.run(str(path), message_limit=1, char_offset=offset, char_limit=97)
                header, fragment = output.split("\n", 1)
                self.assertEqual(fragment, line[offset:offset + 97])
                self.assertIn(f"total_chars={len(line)}", header)
                if offset + 97 < len(line):
                    self.assertIn(f"next_char_offset={offset + 97}", header)

    def test_entire_response_is_bounded_for_many_huge_records(self) -> None:
        path = self.write_history([{"content": "x" * 40_000} for _ in range(12)])
        output = self.tool.run(str(path), message_limit=10, char_limit=4000)
        self.assertLessEqual(len(output), 16_000)
        for index in range(1, 11):
            self.assertIn(f"message={index} ", output)
        self.assertIn("next_message_offset=11", output)

    def test_record_reader_never_requests_unbounded_lines(self) -> None:
        class BoundedReader(io.StringIO):
            def readline(self, size=-1):
                if not 0 < size <= 8192:
                    raise AssertionError("unbounded line allocation")
                return super().readline(size)

        reader = BoundedReader("前" * 100_000 + "尾\nnext\n")
        fragment, total = _history_record(reader, offset=99_997, limit=4)
        self.assertEqual((fragment, total), ("前前前尾", 100_001))
        self.assertEqual(_history_record(reader, offset=0, limit=4), ("next", 4))

    def test_other_agents_traversal_wrong_type_and_missing_files_are_denied(self) -> None:
        other = self.root / "other.jsonl"
        other.write_text("private", encoding="utf-8")
        plain = self.own / "credentials.txt"
        plain.write_text("private", encoding="utf-8")
        for name in (str(other), "../other.jsonl", "credentials.txt", "missing.jsonl", ""):
            with self.subTest(name=name):
                result = self.tool.run(name)
                self.assertTrue(result.startswith("Error:"))
                self.assertNotIn("private", result)

    def test_symlink_is_denied_even_when_target_is_inside_root(self) -> None:
        path = self.write_history([{"content": "private"}])
        link = self.own / "linked.jsonl"
        try:
            link.symlink_to(path)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        output = self.tool.run(str(link))
        self.assertTrue(output.startswith("Error:"))
        self.assertNotIn("private", output)

    def test_windows_reparse_points_are_rejected(self) -> None:
        path = self.write_history([{"content": "private"}])
        with patch.object(Path, "lstat", return_value=SimpleNamespace(st_file_attributes=0x400)), patch.object(Path, "is_symlink", return_value=False):
            output = self.tool.run(str(path))
        self.assertIn("junctions are not allowed", output)

    def test_symlink_detection_covers_file_root_and_parent(self) -> None:
        path = self.write_history([{"content": "private"}])
        for linked in (path, self.own, self.root):
            with self.subTest(linked=linked), patch.object(Path, "is_symlink", autospec=True, side_effect=lambda item: item == linked):
                self.assertIn("symbolic links", self.tool.run(str(path)))

    def test_invalid_ranges_fail_without_reading(self) -> None:
        for key, value in (("message_offset", 0), ("message_limit", 11), ("char_offset", -1), ("char_limit", 0), ("char_limit", 4001), ("char_limit", True), ("message_offset", "1")):
            with self.subTest(key=key, value=value):
                self.assertTrue(self.tool.run("history.jsonl", **{key: value}).startswith("Error:"))

    def test_read_only_discuss_guard_allows_history_lookup(self) -> None:
        self.assertIsNone(discuss_tool_guard(ToolUse("read-history", "load_context_history", {"file_path": "history.jsonl"})))

    def test_cli_subagent_registration_cannot_read_parent_archive(self) -> None:
        env = EnvironmentConfig(model_id="fake", enable_skills=False, memory_config=MemoryConfig(enabled=False))
        tools, _hooks, context = create_default_subagent_environment(self.root, None, None, env, self.root / "context")
        own = context.config.transcript_dir
        own.mkdir(parents=True)
        archive = own / "history.jsonl"
        archive.write_text('{"content":"child"}\n', encoding="utf-8")
        self.assertIn("child", tools.execute("load_context_history", {"file_path": str(archive)}))
        parent = self.write_history([{"content": "parent-private"}])
        self.assertEqual(tools.execute("load_context_history", {"file_path": str(parent)}).status, "error")

    def test_web_root_and_subagent_register_isolated_history_readers(self) -> None:
        repository = SQLiteRepository(self.root / "state.db", recover_incomplete=False)
        self.addCleanup(repository.close)
        conversation = repository.create_conversation(title="history", workspace=str(self.root))
        env = EnvironmentConfig(model_id="fake", data_dir=self.root / "data", enable_skills=False, context_config=ContextConfig(mode="off"), memory_config=MemoryConfig(enabled=False))
        factory = WebAgentFactory(env, self.root, repository)
        self.addCleanup(factory.close)
        emitter = EventEmitter(context=ExecutionContext(conversation_id=conversation.id, run_id="history-run"))
        with patch.object(EnvironmentConfig, "create_anthropic_client", return_value=SimpleNamespace()):
            agent = factory.create(event_emitter=emitter, cancellation=CancellationToken(), permission_broker=WaitingPermissionBroker())
        root_dir = agent.context.config.transcript_dir
        root_dir.mkdir(parents=True)
        parent = root_dir / "parent.jsonl"
        parent.write_text('{"content":"root history"}\n', encoding="utf-8")
        self.assertIn("root history", agent.tools.execute("load_context_history", {"file_path": str(parent)}))
        tools, _hooks, context = agent.subagent_environment_factory()
        context.config.transcript_dir.mkdir(parents=True)
        child = context.config.transcript_dir / "child.jsonl"
        child.write_text('{"content":"child history"}\n', encoding="utf-8")
        self.assertIn("child history", tools.execute("load_context_history", {"file_path": str(child)}))
        self.assertEqual(tools.execute("load_context_history", {"file_path": str(parent)}).status, "error")
        self.assertEqual(agent.tools.execute("load_context_history", {"file_path": str(child)}).status, "error")


if __name__ == "__main__":
    unittest.main()
