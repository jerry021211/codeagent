from __future__ import annotations

import contextlib
import io
import unittest

import codeagent.__main__
from codeagent.cli import main


class CliTests(unittest.TestCase):
    def test_main_module_imports(self) -> None:
        self.assertTrue(callable(codeagent.__main__.main))

    def test_help_exits_successfully(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                main(["--help"])

        self.assertEqual(exc.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
