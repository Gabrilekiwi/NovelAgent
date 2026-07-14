from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import unittest
import uuid

from core.memory_v2 import (
    CURRENT_REDUCER_VERSION,
    MemoryIntegrityError,
    create_genesis_memory_batch,
    ensure_memory_v2_storage_layout,
    load_canonical_memory,
    prepare_chapter_memory_commit,
    prepare_event_authority_chapter_commit,
    replay_memory_events,
    save_canonical_memory,
    write_memory_event_batch,
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

    @staticmethod
    def _tree_bytes(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def _event_authority_case(self, name: str) -> tuple[Path, dict]:
        root = self._root(name)
        event_store = root / "events"
        genesis = create_genesis_memory_batch(
            book_id="book-2",
            title="旧站",
            source_project_digest="1" * 64,
            context_digest="2" * 64,
            authority_epoch=3,
        )
        write_memory_event_batch(event_store, genesis)
        projection = replay_memory_events(event_store)["projection"]
        save_canonical_memory(root / "canonical_memory.json", projection)
        return root, projection

    def _prepare_event_authority(self, root: Path, projection: dict, **overrides) -> dict:
        body = "林澈进入旧站控制室，确认闸门已经关闭。"
        arguments = {
            "memory_root": root,
            "book_id": "book-2",
            "run_id": "run-v22-1",
            "chapter_index": 1,
            "analysis": self._analysis(1),
            "chapter_body": body,
            "chapter_body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "evidence_spans": [{"start": 0, "end": 2, "quote": "林澈"}],
            "authority_epoch": 3,
            "expected_head_event_hash": projection["head_event_hash"],
            "expected_revision": projection["revision"],
            "source_project_digest": "3" * 64,
            "context_digest": "4" * 64,
            "checkpoint_interval": 1,
        }
        arguments.update(overrides)
        return prepare_event_authority_chapter_commit(**arguments)

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
        self.assertEqual("2.1", first["audit"]["schema_version"])
        self.assertEqual("2.1", json.loads(first["targets"][0]["content"])["schema_version"])
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

    def test_event_authority_prepare_is_pure_replayable_and_deterministic(self) -> None:
        root, base = self._event_authority_case("event_authority")
        before = self._tree_bytes(root)

        prepared = self._prepare_event_authority(root, base)
        repeated = self._prepare_event_authority(root, base)

        self.assertEqual(before, self._tree_bytes(root), "prepare must not write any artifact")
        self.assertEqual(prepared, repeated)
        self.assertEqual("prepared", prepared["status"])
        self.assertEqual("2.2", prepared["batch"]["schema_version"])
        self.assertEqual(CURRENT_REDUCER_VERSION, prepared["batch"]["reducer_version"])
        self.assertEqual("chapter", prepared["batch"]["batch_kind"])
        self.assertEqual("committed", prepared["batch"]["publication_status"])
        self.assertEqual("2.2", prepared["projection"]["schema_version"])
        self.assertEqual(3, prepared["projection"]["authority_epoch"])
        self.assertEqual(2, prepared["projection"]["current_state"]["chapter_index"])
        self.assertEqual(
            1, prepared["projection"]["current_state"]["last_committed_chapter_index"]
        )
        self.assertEqual(
            base["head_event_hash"],
            prepared["batch"]["events"][0]["precondition"]["expected_head_event_hash"],
        )
        self.assertEqual(
            prepared["batch"]["events"][-1]["event_hash"],
            prepared["projection"]["head_event_hash"],
        )
        expected_body_hash = hashlib.sha256(
            "林澈进入旧站控制室，确认闸门已经关闭。".encode("utf-8")
        ).hexdigest()
        for event in prepared["batch"]["events"]:
            self.assertEqual(expected_body_hash, event["chapter_body_sha256"])
            self.assertEqual(3, event["authority_epoch"])
            self.assertEqual([{"start": 0, "end": 2, "quote": "林澈"}], event["evidence_spans"])
        self.assertEqual("2.2", prepared["checkpoint"]["schema_version"])
        self.assertEqual(CURRENT_REDUCER_VERSION, prepared["checkpoint"]["reducer_version"])
        self.assertEqual(
            prepared["audit"]["projection_hash"],
            prepared["projection_receipts"]["snapshot"]["source_projection_hash"],
        )
        self.assertEqual(
            prepared["audit"]["projection_hash"],
            prepared["projection_receipts"]["tracking"]["source_projection_hash"],
        )
        kinds = [target["kind"] for target in prepared["targets"]]
        self.assertEqual(1, kinds.count("memory_event_batch"))
        self.assertEqual(1, kinds.count("memory_projection"))
        self.assertEqual(1, kinds.count("memory_checkpoint"))
        self.assertEqual(1, kinds.count("memory_snapshot_projection"))
        self.assertEqual(4, kinds.count("memory_tracking_projection"))
        self.assertEqual(1, kinds.count("memory_snapshot_projection_receipt"))
        self.assertEqual(1, kinds.count("memory_tracking_projection_receipt"))

        self._materialize(prepared)
        replay = replay_memory_events(root / "events")
        self.assertEqual(prepared["projection"], replay["projection"])
        self.assertEqual(prepared["audit"]["projection_hash"], replay["projection_hash"])
        duplicate = self._prepare_event_authority(root, prepared["projection"])
        self.assertEqual("no_op", duplicate["status"])
        self.assertEqual([], duplicate["targets"])

    def test_event_authority_prepare_rejects_body_epoch_and_head_mismatch(self) -> None:
        root, base = self._event_authority_case("event_authority_guards")
        with self.assertRaisesRegex(ValueError, "chapter_body_sha256"):
            self._prepare_event_authority(root, base, chapter_body_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "does not match chapter_body"):
            self._prepare_event_authority(
                root,
                base,
                evidence_spans=[{"start": 1, "end": 3, "quote": "林澈"}],
            )
        with self.assertRaisesRegex(MemoryIntegrityError, "authority_epoch"):
            self._prepare_event_authority(root, base, authority_epoch=4)
        with self.assertRaisesRegex(MemoryIntegrityError, "head_event_hash"):
            self._prepare_event_authority(root, base, expected_head_event_hash="f" * 64)
        with self.assertRaisesRegex(MemoryIntegrityError, "revision mismatch"):
            self._prepare_event_authority(root, base, expected_revision=base["revision"] + 1)

    def test_event_authority_prepare_rejects_canonical_cache_drift(self) -> None:
        root, base = self._event_authority_case("event_authority_cache_drift")
        drifted = copy.deepcopy(base)
        drifted["title"] = "伪造标题"
        save_canonical_memory(root / "canonical_memory.json", drifted)

        with self.assertRaisesRegex(MemoryIntegrityError, "cache differs"):
            self._prepare_event_authority(root, base)


if __name__ == "__main__":
    unittest.main()
