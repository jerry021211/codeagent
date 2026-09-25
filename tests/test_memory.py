from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from codeagent import MemoryConfig, MemoryManager, MemoryStore, ModelResponse
from codeagent.memory import MemoryAccessController, MemoryWriteBlocked
from codeagent.tools.memory import LoadMemoryTool, RememberTool, SearchMemoryTool


class MemoryStoreTests(unittest.TestCase):
    def test_store_remembers_loads_searches_and_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir))

            saved = store.remember(
                name="Project Style",
                memory_type="project",
                description="Explain call chains before functions.",
                content="When explaining this project, start with the call chain.",
            )

            self.assertEqual(saved.name, "Project Style")
            self.assertEqual(
                store.load("Project Style").content,
                "When explaining this project, start with the call chain.",
            )
            self.assertEqual(store.search("call chain")[0].name, "Project Style")
            self.assertTrue((Path(temp_dir) / "MEMORY.md").exists())

    def test_store_rejects_empty_names_and_missing_loads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir))

            with self.assertRaises(ValueError):
                store.remember(
                    name="",
                    memory_type="project",
                    description="bad",
                    content="bad",
                )
            with self.assertRaises(KeyError):
                store.load("../missing")

    def test_empty_index_maintenance_does_not_create_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "memory"
            store = MemoryStore(root)

            store.rebuild_index()

            self.assertFalse(root.exists())

    def test_active_team_blocks_all_store_writes_but_not_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            project.mkdir()
            active = False
            controller = MemoryAccessController(lambda _workspace: active)
            store = MemoryStore(root / "memory", access_policy=controller.policy(project))
            store.remember(
                name="Existing",
                description="Readable during Team runs.",
                content="stable",
            )
            active = True

            self.assertEqual(store.load("Existing").content, "stable")
            with self.assertRaises(MemoryWriteBlocked):
                store.remember(
                    name="Blocked",
                    description="Must not be written.",
                    content="blocked",
                )

    def test_team_creation_waits_for_an_in_progress_memory_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            project.mkdir()
            active = False
            controller = MemoryAccessController(lambda _workspace: active)
            policy = controller.policy(project)
            write_started = threading.Event()
            release_write = threading.Event()
            team_created = threading.Event()

            def write_memory() -> None:
                with policy.writing():
                    write_started.set()
                    release_write.wait(timeout=2)

            def create_team() -> None:
                nonlocal active
                with controller.creating_team(project):
                    active = True
                team_created.set()

            writer = threading.Thread(target=write_memory)
            creator = threading.Thread(target=create_team)
            writer.start()
            self.assertTrue(write_started.wait(timeout=1))
            creator.start()
            self.assertFalse(team_created.wait(timeout=0.05))
            release_write.set()
            writer.join(timeout=1)
            creator.join(timeout=1)

            self.assertTrue(team_created.is_set())
            with self.assertRaises(MemoryWriteBlocked):
                with policy.writing():
                    pass

    def test_teammate_policy_is_read_only_even_without_an_active_team_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            controller = MemoryAccessController(lambda _workspace: False)
            store = MemoryStore(
                root / "memory",
                access_policy=controller.policy(project, always_read_only=True),
            )
            manager = MemoryManager(store, MemoryConfig(auto_extract=False))

            manager.after_turn([], client=None, model="fake", max_tokens=100)

            self.assertFalse(store.root.exists())
            with self.assertRaises(MemoryWriteBlocked):
                store.remember(
                    name="Teammate",
                    description="Must not write.",
                    content="blocked",
                )


class MemoryToolTests(unittest.TestCase):
    def test_memory_tools_save_search_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir))

            save_result = RememberTool(store).run(
                name="User Preference",
                type="user",
                description="Prefers concise final summaries.",
                content="Final answers should be concise and start with outcome.",
            )
            search_result = SearchMemoryTool(store).run("concise")
            load_result = LoadMemoryTool(store).run("User Preference")

            self.assertIn("[memory saved] User Preference", save_result)
            self.assertIn("User Preference [user]", search_result)
            self.assertIn("[memory loaded] User Preference [user]", load_result)
            self.assertIn("Final answers should be concise", load_result)


class FakeExtractionClient:
    def create_message(self, **kwargs):
        return ModelResponse(
            stop_reason="end_turn",
            content=[
                {
                    "type": "text",
                    "text": (
                        "["
                        "{\"name\":\"Explain Preference\","
                        "\"type\":\"user\","
                        "\"description\":\"Explain call chains first.\","
                        "\"content\":\"When explaining code, start with the call chain.\"}"
                        "]"
                    ),
                }
            ],
        )


class FakeSelectionClient:
    def create_message(self, **kwargs):
        return ModelResponse(
            stop_reason="end_turn",
            content=[
                {
                    "type": "text",
                    "text": '{"selected_memories":["project-style.md"]}',
                }
            ],
        )


class MemoryManagerTests(unittest.TestCase):
    def test_manager_can_auto_extract_recent_memories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir))
            manager = MemoryManager(
                store,
                MemoryConfig(auto_extract=True, consolidate_threshold=99),
            )

            manager.after_turn(
                [{"role": "user", "content": "Prefer call-chain explanations."}],
                client=FakeExtractionClient(),
                model="fake-model",
                max_tokens=8000,
            )

            loaded = store.load("Explain Preference")
            self.assertEqual(loaded.memory_type, "user")
            self.assertIn("call chain", loaded.content)

    def test_manager_selects_memory_files_with_llm_and_loads_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir))
            store.remember(
                name="Project Style",
                memory_type="project",
                description="Explain call chains before key functions.",
                content="Always explain the call chain first for this project.",
            )
            manager = MemoryManager(
                store,
                MemoryConfig(max_loaded_items=5, session_budget_chars=60_000),
            )

            context = manager.select_context(
                [{"role": "user", "content": "Explain agent.py"}],
                client=FakeSelectionClient(),
                model="deepseek-v4-pro",
                max_tokens=8000,
            )

            self.assertIn("本轮选取的长期记忆", context)
            self.assertIn('file="project-style.md"', context)
            self.assertIn("Always explain the call chain first", context)


if __name__ == "__main__":
    unittest.main()
