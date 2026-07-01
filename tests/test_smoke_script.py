from __future__ import annotations

import subprocess
import sys
import unittest
import uuid
from pathlib import Path


class SmokeScriptTest(unittest.TestCase):
    def test_v1_smoke_script_runs_runtime_gate_without_tests(self) -> None:
        work_dir = Path.cwd() / ".tmp" / "test_smoke_script" / uuid.uuid4().hex

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/smoke_v1.py",
                "--skip-tests",
                "--work-dir",
                str(work_dir),
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("Smoke v1: OK", completed.stdout)
        self.assertTrue((work_dir / "snapshot.json").exists())
        self.assertTrue((work_dir / "memory_outbox.jsonl").exists())
        self.assertEqual(1, len(list((work_dir / "runs").glob("chapter_*.json"))))
        self.assertEqual(1, len(list((work_dir / "chapters").glob("chapter_*.md"))))


if __name__ == "__main__":
    unittest.main()
