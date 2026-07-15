from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from core.autonomy.outline import (
    OutlineCheckpointError,
    OutlineCheckpointStore,
    build_outline_checkpoint,
)


NOW = "2026-07-14T00:00:00+00:00"


def _checkpoint(*, source: str = "1", epoch: int = 1, head: str = "2", text: str = "# Outline\n"):
    return build_outline_checkpoint(
        book_id="book-outline",
        session_id="session-outline",
        plan_id="plan-outline",
        arc_plan_id="arc-outline",
        chapter_index=4,
        planned_target_hash="3" * 64,
        source_snapshot_hash=source * 64,
        authority_epoch=epoch,
        authority_head_event_hash=head * 64,
        outline_input_digest=("4" if source == "1" else "5") * 64,
        provider_profile="trusted-provider",
        execution_kind="deterministic",
        outline_text=text,
        canonical_relative_path="outlines/chapter-4.md",
        canonical_before_sha256=None,
        created_at=NOW,
    )


class OutlineCheckpointStoreTest(unittest.TestCase):
    def _root(self) -> Path:
        root = Path.cwd() / ".tmp" / "outline" / uuid.uuid4().hex[:10]
        root.mkdir(parents=True)
        return root

    def test_retry_reuses_exact_revision_and_authority_change_invalidates(self):
        store = OutlineCheckpointStore(self._root())
        first = _checkpoint()
        self.assertEqual(first, store.create(first))
        self.assertEqual(first, store.create(first))
        self.assertEqual(1, len(store.history("session-outline", 4)))

        replacement = _checkpoint(source="6", epoch=2, head="7", text="# Revised\n")
        self.assertNotEqual(first["checkpoint_id"], replacement["checkpoint_id"])
        self.assertEqual(replacement, store.create(replacement, invalidated_at=NOW))
        self.assertEqual(replacement, store.load("session-outline", 4))
        self.assertEqual(2, len(store.history("session-outline", 4)))
        invalidations = store.invalidations("session-outline", 4)
        self.assertEqual(1, len(invalidations))
        self.assertEqual(first["checkpoint_hash"], invalidations[0]["invalidated_checkpoint_hash"])
        self.assertEqual(replacement["checkpoint_hash"], invalidations[0]["replacement_checkpoint_hash"])

    def test_same_scope_different_bytes_and_committed_replacement_fail_closed(self):
        store = OutlineCheckpointStore(self._root())
        first = store.create(_checkpoint())
        changed_bytes = dict(first)
        changed_bytes["created_at"] = "2026-07-14T00:00:01+00:00"
        from core.autonomy.common import canonical_hash

        changed_bytes["checkpoint_hash"] = canonical_hash(
            changed_bytes, exclude_fields=("checkpoint_hash",)
        )
        with self.assertRaisesRegex(
            OutlineCheckpointError, "outline_checkpoint_replay_conflict"
        ):
            store.create(changed_bytes)

        with self.assertRaisesRegex(
            OutlineCheckpointError, "outline_checkpoint_chapter_committed"
        ):
            store.create(
                _checkpoint(source="8", epoch=3, head="9", text="# Too late\n"),
                chapter_committed=True,
            )


if __name__ == "__main__":
    unittest.main()
