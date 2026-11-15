"""The core package must not load the optional SDK during import."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ImportBoundaryTests(unittest.TestCase):
    def test_importing_the_agent_does_not_import_openai(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        env.pop("OPENAI_API_KEY", None)
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import rewindagent.agent; import rewindagent.durable; "
                "assert 'openai' not in sys.modules; assert 'httpx2' not in sys.modules",
            ],
            cwd="/tmp",
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
