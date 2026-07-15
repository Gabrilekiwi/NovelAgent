from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest
import uuid

from core.memory_v2 import (
    capture_historical_revision_dependency_inventory,
    load_memory_event_batches,
    replay_memory_events,
)
from core.memory_v2.storage import load_canonical_memory
from core.story_project.history_revision_execution import execute_amend_transaction
from core.story_project.identity import ensure_project_identity, load_project_identity, project_identity_path
from core.story_project.authority import AuthorityError, activate_event_authority, project_identity_sha256
from core.story_project.mapper import SETTING_DIR_NAME, TRACKING_DIR_NAME
from core.story_project.migration_execution import (
    MigrationExecutionError,
    execute_event_authority_migration,
)
from core.story_project.migration_v2 import (
    MigrationPlanStaleError,
    MigrationV2Error,
    build_migration_approval,
    build_migration_plan,
)
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.paths import canonical_prose_path
from core.story_project.read_set import capture_story_project_read_set


NOW = "2026-07-14T00:00:00+00:00"


class StoryProjectMigrationExecutionTest(unittest.TestCase):
    def _book(self, name: str) -> Path:
        root = (
            Path.cwd()
            / ".tmp"
            / "test_story_project_migration_execution"
            / f"{name}_{uuid.uuid4().hex}"
            / "book"
        )
        for directory in CORE_DIRECTORY_NAMES:
            (root / directory).mkdir(parents=True)
        ensure_project_identity(root, book_id=f"book-{name}")
        canonical_prose_path(root, 1).write_text("Chapter one happened.\n", encoding="utf-8")
        canonical_prose_path(root, 10).write_text("Chapter ten opened the gate.\n", encoding="utf-8")
        (root / SETTING_DIR_NAME / "world.md").write_text(
            "Gravity is constant.\n", encoding="utf-8"
        )
        (root / TRACKING_DIR_NAME / "notes.md").write_text(
            "Legacy tracking projection.\n", encoding="utf-8"
        )
        legacy = root / ".novelagent" / "runtime" / "runs" / "legacy.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b'{"legacy":true}\r\n')
        return root

    @staticmethod
    def _decisions() -> dict:
        return {
            "timeline_elapsed_minutes": 155,
            "chapter_10_character_state": {
                "hero": {"location": "gate", "condition": "injured"}
            },
            "open_foreshadowing": [
                {"id": "thread-door", "status": "open", "description": "door remains open"}
            ],
            "inventory": {"hero": {"key": 1, "water": 0}},
            "lexicon": {"black_tide": {"known_by": ["hero"]}},
            "corruption": {"hero": 3},
        }

    def _approved(self, root: Path) -> tuple[dict, dict]:
        plan = build_migration_plan(root, created_at=NOW)
        approval = build_migration_approval(
            plan,
            decisions=self._decisions(),
            approver_id="operator-1",
            approved_at=NOW,
        )
        return plan, approval

    def test_approved_bootstrap_is_one_receipted_source_sync_and_is_idempotent(self) -> None:
        root = self._book("happy")
        plan, approval = self._approved(root)
        preserved = {
            item["relative_path"]: (root / item["relative_path"]).read_bytes()
            for item in plan["sources"]
            if item["role"] != "project_identity"
        }

        result = execute_event_authority_migration(root, plan=plan, approval=approval)

        self.assertEqual("completed", result["status"])
        self.assertFalse(result["idempotent"])
        self.assertTrue(result["verification"]["valid"])
        identity = load_project_identity(root)
        self.assertEqual("2.0", identity.schema_version)
        self.assertEqual("event_v1", identity.authority["mode"])
        self.assertEqual(result["head_event_hash"], identity.authority["head_event_hash"])
        memory_root = root / ".novelagent" / "runtime" / "memory" / "v2"
        batches = load_memory_event_batches(memory_root / "events")
        self.assertEqual(["genesis", "source_sync"], [item["batch_kind"] for item in batches])
        self.assertEqual(["genesis", "source_sync"], [item["publication_status"] for item in batches])
        replay = replay_memory_events(memory_root / "events")
        self.assertEqual(result["head_event_hash"], replay["projection"]["head_event_hash"])
        self.assertEqual([], replay["projection"]["timeline"])
        self.assertEqual(11, replay["projection"]["current_state"]["chapter_index"])
        checkpoint = next((memory_root / "events" / "checkpoints").glob("*.json"))
        checkpoint_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(0, checkpoint_payload["committed_chapter_count"])
        for relative, content in preserved.items():
            self.assertEqual(content, (root / relative).read_bytes(), relative)

        repeated = execute_event_authority_migration(root, plan=plan, approval=approval)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(result["publication_receipt"], repeated["publication_receipt"])
        with self.assertRaisesRegex(MigrationV2Error, "migration_event_authority_already_active"):
            build_migration_plan(root, created_at=NOW)

        completed_entries = list(
            (root / ".novelagent" / "migration-v2" / "tx" / "registry" / "completed").glob(
                "*.json"
            )
        )
        self.assertEqual(1, len(completed_entries))

    def test_missing_approval_is_non_mutating(self) -> None:
        root = self._book("unconfirmed")
        plan = build_migration_plan(root, created_at=NOW)
        identity_before = project_identity_path(root).read_bytes()

        with self.assertRaisesRegex(MigrationExecutionError, "migration_approval_required"):
            execute_event_authority_migration(root, plan=plan, approval=None)

        self.assertEqual(identity_before, project_identity_path(root).read_bytes())
        self.assertFalse((root / ".novelagent" / "runtime" / "memory" / "v2").exists())
        self.assertFalse((root / ".novelagent" / "migration-v2").exists())

    def test_source_drift_after_approval_expires_plan_before_bootstrap(self) -> None:
        root = self._book("source-drift")
        plan, approval = self._approved(root)
        identity_before = project_identity_path(root).read_bytes()
        canonical_prose_path(root, 10).write_text("changed after approval\n", encoding="utf-8")

        with self.assertRaises(MigrationPlanStaleError):
            execute_event_authority_migration(root, plan=plan, approval=approval)

        self.assertEqual(identity_before, project_identity_path(root).read_bytes())
        self.assertFalse(
            (root / ".novelagent" / "runtime" / "memory" / "v2" / "canonical_memory.json").exists()
        )

    def test_failure_after_activation_receipt_before_identity_rolls_back_and_retries(self) -> None:
        root = self._book("pre-identity-fault")
        plan, approval = self._approved(root)
        identity_before = project_identity_path(root).read_bytes()

        def fail_before_identity(point: str, index: int | None, _path: Path | None) -> None:
            if point == "before_apply_target" and index == 9:
                raise OSError("injected before identity")

        with self.assertRaisesRegex(MigrationExecutionError, "migration_bootstrap_incomplete"):
            execute_event_authority_migration(
                root, plan=plan, approval=approval, fault_injector=fail_before_identity
            )

        self.assertEqual(identity_before, project_identity_path(root).read_bytes())
        receipt_dir = root / ".novelagent" / "authority" / "receipts"
        self.assertEqual([], list(receipt_dir.glob("*.json")) if receipt_dir.exists() else [])
        memory_root = root / ".novelagent" / "runtime" / "memory" / "v2"
        self.assertEqual([], list(memory_root.rglob("*.json")) if memory_root.exists() else [])

        recovered = execute_event_authority_migration(root, plan=plan, approval=approval)
        self.assertEqual("completed", recovered["status"])
        self.assertFalse(recovered["idempotent"])

    def test_failure_after_marker_recovers_same_transaction_without_downgrade(self) -> None:
        root = self._book("post-marker-fault")
        plan, approval = self._approved(root)

        def fail_after_marker(point: str, _index: int | None, _path: Path | None) -> None:
            if point == "after_commit_marker":
                raise OSError("injected after marker")

        with self.assertRaisesRegex(MigrationExecutionError, "migration_bootstrap_incomplete"):
            execute_event_authority_migration(
                root, plan=plan, approval=approval, fault_injector=fail_after_marker
            )

        interim = load_project_identity(root)
        self.assertEqual("event_v1", interim.authority["mode"])
        self.assertEqual(approval["source_digest"], plan["source_digest"])

        recovered = execute_event_authority_migration(root, plan=plan, approval=approval)
        self.assertTrue(recovered["idempotent"])
        self.assertEqual(interim.authority["head_event_hash"], recovered["head_event_hash"])

    def test_real_history_entry_recovers_marked_migration_then_replays(self) -> None:
        root = Path.cwd() / ".tmp" / "mh" / uuid.uuid4().hex[:8] / "b"
        for directory in CORE_DIRECTORY_NAMES:
            (root / directory).mkdir(parents=True)
        ensure_project_identity(root, book_id=f"book-mh-{uuid.uuid4().hex[:8]}")
        canonical_prose_path(root, 1).write_text(
            "Chapter one happened.\n", encoding="utf-8"
        )
        canonical_prose_path(root, 10).write_text(
            "Chapter ten opened the gate.\n", encoding="utf-8"
        )
        (root / SETTING_DIR_NAME / "world.md").write_text(
            "Gravity is constant.\n", encoding="utf-8"
        )
        (root / TRACKING_DIR_NAME / "notes.md").write_text(
            "Legacy tracking projection.\n", encoding="utf-8"
        )
        plan, approval = self._approved(root)

        def fail_after_marker(point: str, _index: int | None, _path: Path | None) -> None:
            if point == "after_commit_marker":
                raise OSError("injected after migration marker")

        with self.assertRaisesRegex(MigrationExecutionError, "migration_bootstrap_incomplete"):
            execute_event_authority_migration(
                root,
                plan=plan,
                approval=approval,
                fault_injector=fail_after_marker,
            )

        global_pending = (
            root / ".novelagent" / "runtime" / "ea" / "r" / "p"
        )
        self.assertEqual(1, len(list(global_pending.glob("*.json"))))
        identity = load_project_identity(root)
        memory_root = root / ".novelagent" / "runtime" / "memory" / "v2"
        projection = load_canonical_memory(memory_root / "canonical_memory.json")
        next_chapter = int(projection["current_state"]["chapter_index"])
        read_set = capture_story_project_read_set(
            root,
            next_chapter,
            project_identity=identity,
        )
        inventory = capture_historical_revision_dependency_inventory(
            story_project_root=root,
            book_id=identity.book_id,
            authority_epoch=int(identity.authority["authority_epoch"]),
            head_event_hash=projection["head_event_hash"],
            historical_chapter_index=1,
            canonical_next_chapter_index=next_chapter,
        )
        historical = canonical_prose_path(root, 1)
        revision_source = root.parent / "r.txt"
        revision_text = "Official correction: chapter one happened at dusk."
        revision_source.write_text(revision_text, encoding="utf-8")
        evidence_start = revision_text.index("chapter one")
        kwargs = {
            "memory_root": memory_root,
            "story_project_root": root,
            "transaction_id": "mh-001",
            "historical_chapter_index": 1,
            "historical_chapter_path": historical,
            "expected_historical_chapter_sha256": hashlib.sha256(
                historical.read_bytes()
            ).hexdigest(),
            "revision_source_path": revision_source,
            "expected_revision_source_sha256": hashlib.sha256(
                revision_source.read_bytes()
            ).hexdigest(),
            "evidence_spans": [
                {
                    "start_char": evidence_start,
                    "end_char": evidence_start + len("chapter one"),
                    "quote": "chapter one",
                }
            ],
            "operations": [
                {"op": "update_world", "value": {"chapter_1_time": "dusk"}}
            ],
            "authority_epoch": int(identity.authority["authority_epoch"]),
            "expected_head_event_hash": projection["head_event_hash"],
            "expected_revision": projection["revision"],
            "source_project_digest": read_set["membership_fingerprint"],
            "context_digest": read_set["context_digest"],
            "dependency_inventory": inventory,
        }

        revised = execute_amend_transaction(**kwargs)
        self.assertEqual("completed", revised["status"])
        self.assertEqual([], list(global_pending.glob("*.json")))
        completed_entries = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (
                root / ".novelagent" / "runtime" / "ea" / "r" / "c"
            ).glob("*.json")
        ]
        self.assertEqual(
            {"migration", "history_revision"},
            {entry["writer_kind"] for entry in completed_entries},
        )

        replayed = execute_amend_transaction(**kwargs)
        self.assertEqual("already_committed", replayed["status"])
        self.assertEqual(revised["head_event_hash"], replayed["head_event_hash"])

    def test_non_markdown_source_drift_at_marker_rolls_back_identity(self) -> None:
        root = self._book("marker-drift")
        plan, approval = self._approved(root)
        identity_before = project_identity_path(root).read_bytes()
        legacy = root / ".novelagent" / "runtime" / "runs" / "legacy.json"

        def mutate_legacy(point: str, _index: int | None, _path: Path | None) -> None:
            if point == "before_commit_marker":
                legacy.write_bytes(b'{"legacy":"changed"}\n')

        with self.assertRaisesRegex(MigrationExecutionError, "migration_bootstrap_incomplete"):
            execute_event_authority_migration(
                root, plan=plan, approval=approval, fault_injector=mutate_legacy
            )

        self.assertEqual(identity_before, project_identity_path(root).read_bytes())
        self.assertEqual("legacy_markdown_v1", load_project_identity(root).authority["mode"])

    def test_preexisting_event_store_is_never_merged_into_baseline(self) -> None:
        root = self._book("foreign-event-store")
        foreign = root / ".novelagent" / "runtime" / "memory" / "v2" / "foreign.json"
        foreign.parent.mkdir(parents=True)
        foreign.write_text('{"foreign":true}\n', encoding="utf-8")
        plan, approval = self._approved(root)

        with self.assertRaisesRegex(MigrationExecutionError, "migration_event_store_not_empty"):
            execute_event_authority_migration(root, plan=plan, approval=approval)

        self.assertEqual("legacy_markdown_v1", load_project_identity(root).authority["mode"])
        self.assertEqual('{"foreign":true}\n', foreign.read_text(encoding="utf-8"))

    def test_old_book_cannot_bypass_approval_through_direct_activation(self) -> None:
        root = self._book("direct-bypass")
        identity_before = project_identity_path(root).read_bytes()

        with self.assertRaisesRegex(
            AuthorityError, "migration_approval_required_for_existing_book"
        ):
            activate_event_authority(
                root,
                expected_identity_sha256=project_identity_sha256(root),
                head_event_hash="a" * 64,
            )

        self.assertEqual(identity_before, project_identity_path(root).read_bytes())
        plan, approval = self._approved(root)
        migrated = execute_event_authority_migration(root, plan=plan, approval=approval)
        self.assertEqual("completed", migrated["status"])


if __name__ == "__main__":
    unittest.main()
