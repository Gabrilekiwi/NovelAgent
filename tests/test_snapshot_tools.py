from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from core.state.snapshot_tools import inspect_snapshot_text, load_normalized_snapshot, write_normalized_snapshot


class SnapshotToolsTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_snapshot_tools" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def test_inspect_snapshot_flags_replacement_text(self) -> None:
        report = inspect_snapshot_text({"characters": {"????": {"status": "unknown"}}})

        self.assertFalse(report["ok"])
        self.assertEqual(1, report["suspicious_count"])

    def test_write_normalized_snapshot_preserves_utf8_json(self) -> None:
        path = self._case_dir("utf8") / "snapshot.json"
        write_normalized_snapshot(
            {
                "chapter_index": 1,
                "world_state": {"locations": {"第七码头": {}}},
                "characters": {"陆砚": {}},
                "timeline": [],
            },
            path,
        )

        loaded = load_normalized_snapshot(path)

        self.assertIn("第七码头", loaded["world_state"]["locations"])
        self.assertIn("陆砚", loaded["characters"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["chapter_index"], 1)


if __name__ == "__main__":
    unittest.main()
