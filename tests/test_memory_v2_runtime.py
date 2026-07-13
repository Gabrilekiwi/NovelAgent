from __future__ import annotations

from pathlib import Path
import unittest
import uuid

from core.memory_v2 import (
    ensure_memory_v2_storage_layout,
    load_canonical_memory,
    prepare_chapter_memory_commit,
    replay_memory_events,
)


class MemoryV2RuntimeTest(unittest.TestCase):
    def _root(self, name: str) -> Path:
        root = Path.cwd() / ".tmp" / "test_memory_v2_runtime" / f"{name}_{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        return root

    @staticmethod
    def _analysis(chapter: int) -> dict:
        return {
            "summary": f"第{chapter}章推进",
            "events": [{"text": "进入控制室"}],
            "character_changes": [
                {"name": "林澈", "status": "警觉", "current_location": "控制室"}
            ],
            "world_changes": [{"type": "gate", "text": "闸门关闭"}],
            "new_locations": ["控制室"],
            "story_state": {
                "last_scene_location": "控制室",
                "open_threads": ["闸门信号"],
            },
            "spatial_state": {"character_positions": {"林澈": "控制室"}},
        }

    @staticmethod
    def _materialize(prepared: dict) -> None:
        for target in prepared["targets"]:
            target["path"].parent.mkdir(parents=True, exist_ok=True)
            target["path"].write_text(target["content"], encoding="utf-8")

    def test_storage_layout_prepares_transaction_target_parents(self) -> None:
        layout = ensure_memory_v2_storage_layout(self._root("layout") / "v2")

        self.assertTrue(layout["batches"].is_dir())
        self.assertTrue(layout["checkpoints"].is_dir())

    def test_two_chapter_commits_form_replayable_chain_and_idempotent_patch(self) -> None:
        root = self._root("chain")
        first = prepare_chapter_memory_commit(
            memory_root=root,
            book_id="book-1",
            run_id="run-1",
            chapter_index=1,
            analysis=self._analysis(1),
            source_project_digest="a" * 64,
            context_digest="b" * 64,
        )
        self._materialize(first)

        duplicate = prepare_chapter_memory_commit(
            memory_root=root,
            book_id="book-1",
            run_id="run-1",
            chapter_index=1,
            analysis=self._analysis(1),
            source_project_digest="a" * 64,
            context_digest="b" * 64,
        )
        self.assertEqual("no_op", duplicate["status"])
        self.assertEqual([], duplicate["targets"])

        second = prepare_chapter_memory_commit(
            memory_root=root,
            book_id="book-1",
            run_id="run-2",
            chapter_index=2,
            analysis=self._analysis(2),
            source_project_digest="c" * 64,
            context_digest="d" * 64,
        )
        self.assertEqual(first["audit"]["revision"], second["audit"]["previous_revision"])
        self._materialize(second)

        replay = replay_memory_events(root / "events")
        canonical = load_canonical_memory(root / "canonical_memory.json")
        self.assertEqual(2, replay["committed_chapter_count"])
        self.assertEqual(second["audit"]["revision"], replay["revision"])
        self.assertEqual(replay["projection"], canonical)
        self.assertEqual(2, len(replay["projection"]["timeline"]))

    def test_checkpoint_is_prepared_inside_the_same_target_set(self) -> None:
        root = self._root("checkpoint")
        prepared = prepare_chapter_memory_commit(
            memory_root=root,
            book_id="book-1",
            run_id="run-checkpoint",
            chapter_index=1,
            analysis=self._analysis(1),
            source_project_digest="a" * 64,
            context_digest="b" * 64,
            checkpoint_interval=1,
        )

        self.assertIn("memory_checkpoint", {target["kind"] for target in prepared["targets"]})
        self.assertIsNotNone(prepared["audit"]["checkpoint_id"])


if __name__ == "__main__":
    unittest.main()
