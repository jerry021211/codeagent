from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeagent.tools import LoadToolOutputTool


class LoadToolOutputToolTests(unittest.TestCase):
    def test_reads_only_the_current_agent_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            own = root / "own"
            other = root / "other"
            own.mkdir()
            other.mkdir()
            output = own / "tool.txt"
            output.write_text("one\ntwo\nthree\n", encoding="utf-8")
            secret = other / "secret.txt"
            secret.write_text("secret", encoding="utf-8")
            tool = LoadToolOutputTool(own)

            self.assertIn("2\ttwo", tool.run(str(output), offset=2, limit=1))
            self.assertIn("not allowed", tool.run(str(secret)))
            self.assertIn("not allowed", tool.run("../other/secret.txt"))


if __name__ == "__main__":
    unittest.main()
