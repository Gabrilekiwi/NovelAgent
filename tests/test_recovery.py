from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from core.engine.executor import AgentExecutor
from core.engine.recovery import recover_latest_chapter_draft


class RecoveryTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_recovery" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def test_recovers_latest_failed_pre_polish_draft_without_snapshot_update(self) -> None:
        case_dir = self._case_dir("pre_polish")
        snapshot_path = case_dir / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"chapter_index": 2, "world_state": {}, "characters": {}, "timeline": []}),
            encoding="utf-8",
        )

        def fail_polish(chapter: str) -> str:
            raise RuntimeError("polish failed")

        with self.assertRaises(RuntimeError):
            AgentExecutor(
                snapshot_path=snapshot_path,
                run_dir=case_dir / "runs",
                chapter_dir=case_dir / "chapters",
                dry_run=True,
                polisher=fail_polish,
            ).run_once(persist=True)

        result = recover_latest_chapter_draft(run_dir=case_dir / "runs", chapter_dir=case_dir / "chapters")

        self.assertTrue(result["ok"])
        self.assertEqual(2, result["chapter_index"])
        artifact_path = Path(result["artifact"]["path"])
        self.assertTrue(artifact_path.exists())
        self.assertIn("Snapshot Updated: `False`", artifact_path.read_text(encoding="utf-8"))
        saved_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(2, saved_snapshot["chapter_index"])


if __name__ == "__main__":
    unittest.main()
