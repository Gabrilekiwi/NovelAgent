from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from core.runtime_paths import (
    DEFAULT_CHAPTER_DIR,
    DEFAULT_MEMORY_OUTBOX,
    DEFAULT_MEMORY_PATH,
    DEFAULT_RUN_DIR,
    DEFAULT_RUNTIME_DIR,
    DEFAULT_SNAPSHOT_PATH,
    init_runtime_state,
)


class RuntimePathsTest(unittest.TestCase):
    def test_default_runtime_paths_stay_under_tmp_runtime(self) -> None:
        self.assertEqual(Path(".tmp/runtime"), DEFAULT_RUNTIME_DIR)
        self.assertEqual(Path(".tmp/runtime/snapshot.json"), DEFAULT_SNAPSHOT_PATH)
        self.assertEqual(Path(".tmp/runtime/notion_memory.json"), DEFAULT_MEMORY_PATH)
        self.assertEqual(Path(".tmp/runtime/runs"), DEFAULT_RUN_DIR)
        self.assertEqual(Path(".tmp/runtime/chapters"), DEFAULT_CHAPTER_DIR)
        self.assertEqual(Path(".tmp/runtime/memory_outbox.jsonl"), DEFAULT_MEMORY_OUTBOX)

    def test_init_runtime_state_copies_examples_without_overwriting(self) -> None:
        case_dir = Path.cwd() / ".tmp" / "test_runtime_paths" / uuid.uuid4().hex
        snapshot_source = case_dir / "snapshot.example.json"
        memory_source = case_dir / "notion_memory.example.json"
        snapshot_target = case_dir / "runtime" / "snapshot.json"
        memory_target = case_dir / "runtime" / "notion_memory.json"
        snapshot_source.parent.mkdir(parents=True)
        snapshot_source.write_text(json.dumps({"chapter_index": 1}), encoding="utf-8")
        memory_source.write_text(json.dumps({"pages": []}), encoding="utf-8")

        first = init_runtime_state(
            snapshot_source=snapshot_source,
            memory_source=memory_source,
            snapshot_target=snapshot_target,
            memory_target=memory_target,
        )
        second = init_runtime_state(
            snapshot_source=snapshot_source,
            memory_source=memory_source,
            snapshot_target=snapshot_target,
            memory_target=memory_target,
        )

        self.assertEqual(str(case_dir / "runtime"), first["runtime_dir"])
        self.assertTrue(snapshot_target.exists())
        self.assertTrue(memory_target.exists())
        self.assertTrue((case_dir / "runtime" / "runs").is_dir())
        self.assertTrue((case_dir / "runtime" / "chapters").is_dir())
        self.assertEqual(["snapshot", "memory"], [item["name"] for item in first["copied"]])
        self.assertEqual(["snapshot", "memory"], [item["name"] for item in second["skipped"]])


if __name__ == "__main__":
    unittest.main()
