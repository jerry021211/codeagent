from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codeagent import EnvironmentConfig, MemoryStore
from codeagent.mcp import McpRouter
from codeagent.memory import MemoryAccessController
from codeagent.web.factory import WebAgentFactory


class ConcurrentMcpTests(unittest.TestCase):
    def test_all_callers_wait_for_discovery_and_receive_the_same_result(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temporary:
                router = McpRouter(Path(temporary) / "missing.json")
                router.servers = [object()]
                entered, release = threading.Event(), threading.Event()
                calls = []

                def discover():
                    calls.append(1)
                    entered.set()
                    release.wait(3)
                    if fail:
                        router._startup_error = ValueError("discovery failed")
                    else:
                        router._tools = [SimpleNamespace(definition=SimpleNamespace(name="mcp__demo__tool"))]
                    router._ready.set()

                with patch.object(router, "_thread_main", side_effect=discover), ThreadPoolExecutor(max_workers=2) as executor:
                    try:
                        first = executor.submit(router.list_tools)
                        self.assertTrue(entered.wait(2))
                        second_entered = threading.Event()

                        def second_call():
                            second_entered.set()
                            return router.list_tools()

                        second = executor.submit(second_call)
                        self.assertTrue(second_entered.wait(2))
                        self.assertFalse(first.done())
                        self.assertFalse(second.done())
                        release.set()
                        for future in (first, second):
                            if fail:
                                with self.assertRaisesRegex(RuntimeError, "discovery failed"):
                                    future.result(timeout=2)
                            else:
                                self.assertEqual(future.result(timeout=2), ["mcp__demo__tool"])
                        self.assertEqual(len(calls), 1)
                    finally:
                        release.set()
                        router.close()
                with self.assertRaisesRegex(RuntimeError, "closed"):
                    router.start()

    def test_factory_cache_is_atomic_workspace_bound_and_reload_keeps_old_connections(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = EnvironmentConfig(model_id="test", mcp_config_path=root / "shared.json")
            factory = WebAgentFactory(env, root, SimpleNamespace())
            first = factory.for_workspace(root)
            second = factory.for_workspace(root)
            with patch("codeagent.web.factory.McpRouter") as constructor:
                constructor.side_effect = lambda *_args, **_kwargs: SimpleNamespace(close=lambda: closed.append(1))
                closed = []
                barrier = threading.Barrier(3)

                def lookup(owner):
                    barrier.wait(timeout=3)
                    return owner._mcp_router()

                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(lookup, owner) for owner in (first, second)]
                    barrier.wait(timeout=3)
                    routers = [future.result(timeout=3) for future in futures]
                self.assertIs(routers[0], routers[1])
                self.assertEqual(constructor.call_count, 1)
                (root / "a").mkdir()
                (root / "b").mkdir()
                team_a = factory.for_team_workspace(root / "a", project_workspace=root)
                team_b = factory.for_team_workspace(root / "b", project_workspace=root)
                self.assertIsNot(team_a._mcp_router(force_workspace_cwd=True), team_b._mcp_router(force_workspace_cwd=True))
                factory.reload_mcp(root)
                self.assertFalse(closed)
                self.assertIsNot(first._mcp_router(), routers[0])
                factory.close()
                factory.close()
                self.assertEqual(len(closed), 4)


class ConcurrentMemoryTests(unittest.TestCase):
    def test_readers_wait_until_whole_memory_update_is_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = MemoryAccessController(lambda _workspace: False)
            writer = MemoryStore(root, access_policy=controller.policy(root))
            reader = MemoryStore(root, access_policy=controller.policy(root, always_read_only=True))
            writer.remember(name="note", description="test", content="old")
            # Simulate the interval between clearing/replacing a file and completing
            # a write. Another Agent must never observe the intermediate bytes.
            for read in (reader.list_memories, lambda: reader.load("note"), lambda: reader.load_file("note.md")):
                entered = threading.Event()

                def load():
                    entered.set()
                    return read()

                with ThreadPoolExecutor(max_workers=1) as executor:
                    with writer.writing():
                        (root / "note.md").write_text("partial", encoding="utf-8")
                        future = executor.submit(load)
                        self.assertTrue(entered.wait(2))
                        self.assertFalse(future.done())
                        (root / "note.md").unlink()
                        writer.remember(name="note", description="test", content="complete")
                    result = future.result(timeout=2)
                self.assertEqual((result[0] if isinstance(result, list) else result).content, "complete")


if __name__ == "__main__":
    unittest.main()
