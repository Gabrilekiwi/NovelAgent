from __future__ import annotations

import hashlib
import json
import unittest
import uuid
from pathlib import Path

from core.memory_v2 import (
    MemoryEventStoreError,
    MemoryIntegrityError,
    MemoryPatchConflictError,
    canonical_json_hash,
    commit_memory_patch,
    create_empty_canonical_memory,
    create_memory_patch,
    load_latest_memory_checkpoint,
    load_memory_event_batches,
    rebuild_canonical_memory,
    replay_memory_events,
    verify_memory_projection,
)


class MemoryV21EventStoreTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        root = Path.cwd() / ".tmp" / "test_memory_v2_event_store" / f"{name}_{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        return root

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _patch(patch_id: str, value: int) -> dict:
        return create_memory_patch(
            patch_id=patch_id,
            source_kind="chapter_analysis",
            source_path="C:/relocatable/project/chapter.md",
            operations=[{"op": "update_world", "value": {"level": value}}],
        )

    def _commit(
        self,
        root: Path,
        patch_id: str,
        value: int,
        *,
        batch_kind: str = "source_sync",
        publication_status: str | None = None,
        quality_state: dict | None = None,
        checkpoint_interval: int = 20,
    ) -> dict:
        return commit_memory_patch(
            store_dir=root / "events",
            canonical_path=root / "canonical_memory.json",
            patch=self._patch(patch_id, value),
            source_project_digest=self._digest("project"),
            context_digest=self._digest(f"context-{patch_id}"),
            initial_memory=create_empty_canonical_memory(book_id="book-1", title="Book"),
            batch_kind=batch_kind,
            publication_status=publication_status,
            quality_state=quality_state,
            checkpoint_interval=checkpoint_interval,
        )

    def test_commits_immutable_batch_with_event_and_chain_hashes(self) -> None:
        root = self._case_dir("hashes")

        first = self._commit(root, "patch-1", 1)
        second = self._commit(root, "patch-2", 2)
        batches = load_memory_event_batches(root / "events")

        self.assertEqual("2.1", first["batch"]["schema_version"])
        self.assertEqual("2.1", first["events"][0]["schema_version"])
        self.assertEqual(64, len(first["events"][0]["event_hash"]))
        self.assertEqual(first["batch"]["batch_hash"], second["batch"]["previous_batch_hash"])
        self.assertEqual([2, 3], [batch["first_revision"] for batch in batches])

    def test_same_patch_id_and_content_is_no_op(self) -> None:
        root = self._case_dir("idempotent")
        first = self._commit(root, "patch-1", 1)

        second = self._commit(root, "patch-1", 1)

        self.assertEqual("no_op", second["status"])
        self.assertEqual(first["projection"], second["projection"])
        self.assertEqual(1, len(load_memory_event_batches(root / "events")))

    def test_same_patch_id_with_different_content_is_conflict(self) -> None:
        root = self._case_dir("conflict")
        self._commit(root, "patch-1", 1)

        with self.assertRaises(MemoryPatchConflictError):
            self._commit(root, "patch-1", 2)

    def test_replay_fails_closed_on_batch_content_tampering(self) -> None:
        root = self._case_dir("tamper")
        result = self._commit(root, "patch-1", 1)
        path = root / "events" / "batches" / f"{result['batch']['batch_id']}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["patch"]["operations"][0]["value"]["level"] = 99
        path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(MemoryIntegrityError):
            replay_memory_events(root / "events")

    def test_replay_fails_closed_on_revision_fork(self) -> None:
        root = self._case_dir("fork")
        first = self._commit(root, "patch-1", 1)
        second = self._commit(root, "patch-2", 2)
        path = root / "events" / "batches" / f"{second['batch']['batch_id']}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["previous_batch_hash"] = "0" * 64
        payload["batch_hash"] = canonical_json_hash(payload, exclude_fields=("batch_hash",))
        path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(MemoryIntegrityError, "hash chain"):
            replay_memory_events(root / "events")

        self.assertNotEqual("0" * 64, first["batch"]["batch_hash"])

    def test_checkpoint_counts_only_committed_chapters_and_replays_tail(self) -> None:
        root = self._case_dir("checkpoint")
        self._commit(root, "chapter-1", 1, batch_kind="chapter", checkpoint_interval=2)
        second = self._commit(root, "chapter-2", 2, batch_kind="chapter", checkpoint_interval=2)

        checkpoint = load_latest_memory_checkpoint(root / "events")
        self.assertIsNotNone(checkpoint)
        self.assertEqual(2, checkpoint["committed_chapter_count"])
        self.assertEqual(second["projection"], checkpoint["projection"])

        self._commit(root, "source-sync-1", 3, checkpoint_interval=2)
        replay = replay_memory_events(root / "events")

        self.assertEqual(checkpoint["checkpoint_id"], replay["checkpoint_id"])
        self.assertEqual(2, replay["committed_chapter_count"])
        self.assertEqual(1, replay["batch_count"])
        self.assertEqual(3, replay["projection"]["world"]["level"])

    def test_checkpoint_tampering_fails_closed(self) -> None:
        root = self._case_dir("checkpoint_tamper")
        self._commit(root, "chapter-1", 1, batch_kind="chapter", checkpoint_interval=1)
        checkpoint_path = next((root / "events" / "checkpoints").glob("*.json"))
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        payload["projection"]["world"]["level"] = 7
        checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(MemoryIntegrityError):
            replay_memory_events(root / "events")

    def test_rebuild_restores_deleted_cache_and_verifies_projection(self) -> None:
        root = self._case_dir("rebuild")
        committed = self._commit(root, "patch-1", 1)
        cache_path = root / "canonical_memory.json"
        cache_path.unlink()

        rebuilt = rebuild_canonical_memory(root / "events", cache_path)
        verification = verify_memory_projection(root / "events", cache_path)

        self.assertEqual(committed["projection"], rebuilt)
        self.assertTrue(verification["matches"])

    def test_projection_verification_reports_mismatch(self) -> None:
        root = self._case_dir("verify_mismatch")
        committed = self._commit(root, "patch-1", 1)
        changed = dict(committed["projection"])
        changed["world"] = {"level": 4}

        report = verify_memory_projection(root / "events", changed)

        self.assertFalse(report["matches"])
        self.assertEqual("mismatch", report["status"])

    def test_quality_summary_stays_outside_world_projection(self) -> None:
        root = self._case_dir("quality")
        self._commit(
            root,
            "chapter-1",
            1,
            batch_kind="chapter",
            quality_state={"accepted": True, "policy": "standard"},
        )

        replay = replay_memory_events(root / "events")

        self.assertNotIn("quality_state", replay["projection"])
        self.assertEqual(1, len(replay["quality_state"]))

    def test_rejected_or_preview_chapter_cannot_enter_world_memory(self) -> None:
        root = self._case_dir("rejected")

        for status in ("rejected", "failed", "preview"):
            with self.subTest(status=status):
                with self.assertRaises(MemoryEventStoreError):
                    self._commit(
                        root,
                        f"chapter-{status}",
                        1,
                        batch_kind="chapter",
                        publication_status=status,
                    )

        self.assertFalse((root / "events" / "batches").exists())


if __name__ == "__main__":
    unittest.main()
