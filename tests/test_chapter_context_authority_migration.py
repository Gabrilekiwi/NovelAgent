from __future__ import annotations

import copy
import hashlib
import json
import unittest
import uuid
from pathlib import Path

from core.memory_v2.canonical import canonical_json_hash
from core.state.authoritative import empty_authoritative_state
from core.state.snapshot import normalize_snapshot
from core.state.chapter_context_authority_migration import (
    ChapterContextAuthorityMigrationError,
    run_chapter_context_authority_migration,
)
from core.story_project.authority import (
    activate_event_authority,
    project_identity_sha256,
)
from core.story_project.identity import (
    ensure_project_identity,
)


_BOOK_ID = "book-chapter-context-migration"
_MIGRATION_ID = "chapter18-authority-gap-v1"
_EVENT_ID = "chapter-0017-beat-009"
_EXACT_QUOTE = "Hero recovers the red archive record."


class SimulatedPowerLoss(BaseException):
    pass


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class ChapterContextAuthorityMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = (
            Path.cwd()
            / ".tmp"
            / "ccam"
            / uuid.uuid4().hex
        )
        self.base.mkdir(parents=True, exist_ok=False)

    def _case(
        self,
        name: str,
        *,
        existing_event: dict | None = None,
    ) -> dict[str, object]:
        root = self.base / name / "book"
        root.mkdir(parents=True)
        ensure_project_identity(root, book_id=_BOOK_ID)

        authority = empty_authoritative_state()
        identical_character = {
            "character_id": "hero",
            "canonical_name": "Hero",
            "aliases": ["Hero"],
            "source_tier": "story_project_standard",
        }
        authority["characters"]["hero"] = copy.deepcopy(
            identical_character
        )
        if existing_event is not None:
            authority["events"][_EVENT_ID] = copy.deepcopy(existing_event)
        snapshot = normalize_snapshot(
            {
                "book_id": _BOOK_ID,
                "chapter_index": 18,
                "authoritative_state": authority,
                "unrelated": {
                    "nested": ["preserve", {"exact": True}],
                    "counter": 7,
                },
            }
        )
        snapshot_path = root / ".novelagent" / "runtime" / "snapshot.json"
        snapshot_path.parent.mkdir(parents=True)
        snapshot_bytes = _json_bytes(snapshot)
        snapshot_path.write_bytes(snapshot_bytes)

        evidence_path = root / "evidence" / "chapter-17.txt"
        evidence_path.parent.mkdir(parents=True)
        evidence_bytes = (
            "Earlier line.\n"
            + _EXACT_QUOTE
            + "\nLater line.\n"
        ).encode("utf-8")
        evidence_path.write_bytes(evidence_bytes)

        incoming_event = {
            "event_id": _EVENT_ID,
            "type": "archive_record_recovered",
            "subjects": ["hero"],
            "objects": ["red-record"],
            "location": "archive-room",
            "status": "completed",
        }
        manifest = {
            "schema_version": "1.0",
            "migration_id": _MIGRATION_ID,
            "book_id": _BOOK_ID,
            "snapshot": {
                "path": ".novelagent/runtime/snapshot.json",
                "sha256_before": _sha256(snapshot_bytes),
            },
            "evidence": [
                {
                    "evidence_id": "chapter17-red-record",
                    "path": "evidence/chapter-17.txt",
                    "sha256": _sha256(evidence_bytes),
                    "exact_quote": _EXACT_QUOTE,
                    "line": 2,
                }
            ],
            "upserts": [
                {
                    "collection": "events",
                    "record_id": _EVENT_ID,
                    "record": incoming_event,
                    "evidence_ids": ["chapter17-red-record"],
                },
                {
                    "collection": "characters",
                    "record_id": "hero",
                    "record": identical_character,
                    "evidence_ids": ["chapter17-red-record"],
                },
            ],
        }
        manifest_path = root / "chapter-context-authority-migration.json"
        manifest_path.write_bytes(_json_bytes(manifest))
        return {
            "root": root,
            "snapshot": snapshot,
            "snapshot_path": snapshot_path,
            "snapshot_bytes": snapshot_bytes,
            "evidence_path": evidence_path,
            "evidence_bytes": evidence_bytes,
            "manifest": manifest,
            "manifest_path": manifest_path,
            "incoming_event": incoming_event,
        }

    @staticmethod
    def _replace_pinned_snapshot(
        case: dict[str, object],
        snapshot: dict,
    ) -> None:
        snapshot_path = case["snapshot_path"]
        manifest_path = case["manifest_path"]
        assert isinstance(snapshot_path, Path)
        assert isinstance(manifest_path, Path)
        snapshot_bytes = _json_bytes(snapshot)
        snapshot_path.write_bytes(snapshot_bytes)
        manifest = copy.deepcopy(case["manifest"])
        assert isinstance(manifest, dict)
        manifest["snapshot"]["sha256_before"] = _sha256(
            snapshot_bytes
        )
        manifest_path.write_bytes(_json_bytes(manifest))
        case["snapshot"] = snapshot
        case["snapshot_bytes"] = snapshot_bytes
        case["manifest"] = manifest

    def test_preview_is_default_and_performs_zero_writes(self) -> None:
        case = self._case("preview")
        root = case["root"]
        assert isinstance(root, Path)
        before = _files(root)

        result = run_chapter_context_authority_migration(
            story_project=root,
            manifest_path=case["manifest_path"],
        )

        self.assertEqual("preview", result["mode"])
        self.assertEqual("ready", result["status"])
        self.assertFalse(result["writes_performed"])
        self.assertEqual(before, _files(root))
        self.assertFalse(
            (
                root
                / ".novelagent/runtime/migrations"
                / "cca"
                / _MIGRATION_ID
            ).exists()
        )
        self.assertEqual(
            ["identical", "inserted"],
            [item["action"] for item in result["upserts"]],
        )

    def test_apply_creates_exact_backup_bound_receipt_and_is_idempotent(
        self,
    ) -> None:
        case = self._case("apply")
        root = case["root"]
        snapshot_path = case["snapshot_path"]
        assert isinstance(root, Path)
        assert isinstance(snapshot_path, Path)
        original_snapshot = copy.deepcopy(case["snapshot"])
        original_bytes = bytes(case["snapshot_bytes"])

        applied = run_chapter_context_authority_migration(
            story_project=root,
            manifest_path=case["manifest_path"],
            apply=True,
        )

        self.assertEqual("applied", applied["status"])
        self.assertTrue(applied["writes_performed"])
        backup_path = root / applied["snapshot"]["backup_path"]
        immutable_manifest_path = (
            root / applied["manifest"]["immutable_path"]
        )
        receipt_path = root / applied["receipt_path"]
        persistence_receipt_path = (
            root / applied["persistence_receipt_path"]
        )
        self.assertEqual(original_bytes, backup_path.read_bytes())
        self.assertEqual(
            Path(case["manifest_path"]).read_bytes(),
            immutable_manifest_path.read_bytes(),
        )
        receipt_bytes = receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes.decode("utf-8"))
        persistence_receipt = json.loads(
            persistence_receipt_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            receipt["receipt_hash"],
            canonical_json_hash(
                receipt,
                exclude_fields=("receipt_hash",),
            ),
        )
        self.assertEqual(
            applied["snapshot"]["before_sha256"],
            receipt["snapshot"]["backup_sha256"],
        )
        self.assertEqual(
            persistence_receipt["receipt_id"],
            receipt["publication_receipt"]["id"],
        )
        self.assertEqual(
            persistence_receipt["receipt_path_ref"],
            receipt["publication_receipt"]["path_ref"],
        )
        after = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(original_snapshot["unrelated"], after["unrelated"])
        self.assertEqual(
            case["incoming_event"],
            after["authoritative_state"]["events"][_EVENT_ID],
        )
        self.assertEqual(
            original_snapshot["authoritative_state"]["characters"]["hero"],
            after["authoritative_state"]["characters"]["hero"],
        )

        after_bytes = snapshot_path.read_bytes()
        rerun = run_chapter_context_authority_migration(
            story_project=root,
            manifest_path=case["manifest_path"],
            apply=True,
        )

        self.assertEqual("already_applied", rerun["status"])
        self.assertFalse(rerun["writes_performed"])
        self.assertEqual(after_bytes, snapshot_path.read_bytes())
        self.assertEqual(receipt_bytes, receipt_path.read_bytes())
        self.assertEqual(original_bytes, backup_path.read_bytes())
        self.assertEqual(
            Path(case["manifest_path"]).read_bytes(),
            immutable_manifest_path.read_bytes(),
        )

    def test_apply_rolls_forward_after_commit_marker_power_loss(
        self,
    ) -> None:
        case = self._case("receipt-recovery")
        root = case["root"]
        snapshot_path = case["snapshot_path"]
        assert isinstance(root, Path)
        assert isinstance(snapshot_path, Path)

        def crash(
            point: str,
            _index: int | None,
            _path: Path | None,
        ) -> None:
            if point == "after_commit_marker":
                raise SimulatedPowerLoss(point)

        with self.assertRaises(SimulatedPowerLoss):
            run_chapter_context_authority_migration(
                story_project=root,
                manifest_path=case["manifest_path"],
                apply=True,
                _fault_injector=crash,
            )

        after_failure = json.loads(
            snapshot_path.read_text(encoding="utf-8")
        )
        self.assertIn(
            _EVENT_ID,
            after_failure["authoritative_state"]["events"],
        )
        migration_root = (
            root
            / ".novelagent/runtime/migrations"
            / "cca"
            / _MIGRATION_ID
        )
        self.assertFalse((migration_root / "manifest.json").exists())
        self.assertFalse(
            (migration_root / "preimage.snapshot.json").exists()
        )
        self.assertFalse((migration_root / "receipt.json").exists())

        recovered = run_chapter_context_authority_migration(
            story_project=root,
            manifest_path=case["manifest_path"],
            apply=True,
        )

        self.assertEqual("receipt_recovered", recovered["status"])
        self.assertTrue(recovered["writes_performed"])
        receipt = json.loads(
            (migration_root / "receipt.json").read_text(encoding="utf-8")
        )
        self.assertTrue((migration_root / "manifest.json").is_file())
        self.assertTrue(
            (migration_root / "preimage.snapshot.json").is_file()
        )
        self.assertTrue(
            (migration_root / "publication-receipt.json").is_file()
        )
        self.assertEqual("applied", receipt["status"])

    def test_pre_marker_power_loss_rolls_back_before_retry(self) -> None:
        case = self._case("pre-marker-recovery")
        root = case["root"]
        snapshot_path = case["snapshot_path"]
        assert isinstance(root, Path)
        assert isinstance(snapshot_path, Path)
        original_bytes = bytes(case["snapshot_bytes"])

        def crash(
            point: str,
            index: int | None,
            _path: Path | None,
        ) -> None:
            if point == "after_apply_target" and index == 0:
                raise SimulatedPowerLoss(point)

        with self.assertRaises(SimulatedPowerLoss):
            run_chapter_context_authority_migration(
                story_project=root,
                manifest_path=case["manifest_path"],
                apply=True,
                _fault_injector=crash,
            )

        self.assertNotEqual(original_bytes, snapshot_path.read_bytes())
        migration_root = (
            root
            / ".novelagent/runtime/migrations"
            / "cca"
            / _MIGRATION_ID
        )
        self.assertFalse((migration_root / "receipt.json").exists())

        applied = run_chapter_context_authority_migration(
            story_project=root,
            manifest_path=case["manifest_path"],
            apply=True,
        )

        self.assertEqual("applied", applied["status"])
        self.assertTrue((migration_root / "receipt.json").is_file())
        after = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(
            case["incoming_event"],
            after["authoritative_state"]["events"][_EVENT_ID],
        )

    def test_completed_receipt_remains_idempotent_after_book_advances(
        self,
    ) -> None:
        case = self._case("advanced-idempotence")
        root = case["root"]
        snapshot_path = case["snapshot_path"]
        assert isinstance(root, Path)
        assert isinstance(snapshot_path, Path)
        run_chapter_context_authority_migration(
            story_project=root,
            manifest_path=case["manifest_path"],
            apply=True,
        )
        advanced = json.loads(
            snapshot_path.read_text(encoding="utf-8")
        )
        advanced["chapter_index"] += 1
        advanced["unrelated"]["counter"] = 99
        snapshot_path.write_bytes(_json_bytes(advanced))
        advanced_bytes = snapshot_path.read_bytes()

        rerun = run_chapter_context_authority_migration(
            story_project=root,
            manifest_path=case["manifest_path"],
            apply=True,
        )

        self.assertEqual("already_applied", rerun["status"])
        self.assertFalse(rerun["writes_performed"])
        self.assertEqual(advanced_bytes, snapshot_path.read_bytes())

    def test_snapshot_and_evidence_drift_are_refused(self) -> None:
        snapshot_case = self._case("snapshot-drift")
        snapshot_path = snapshot_case["snapshot_path"]
        assert isinstance(snapshot_path, Path)
        changed = copy.deepcopy(snapshot_case["snapshot"])
        changed["unrelated"]["counter"] = 8
        snapshot_path.write_bytes(_json_bytes(changed))

        with self.assertRaises(
            ChapterContextAuthorityMigrationError
        ) as snapshot_error:
            run_chapter_context_authority_migration(
                story_project=snapshot_case["root"],
                manifest_path=snapshot_case["manifest_path"],
            )
        self.assertEqual(
            "snapshot_precondition_failed",
            snapshot_error.exception.code,
        )

        evidence_case = self._case("evidence-drift")
        evidence_path = evidence_case["evidence_path"]
        assert isinstance(evidence_path, Path)
        evidence_path.write_text(
            "Changed evidence.\n",
            encoding="utf-8",
        )
        with self.assertRaises(
            ChapterContextAuthorityMigrationError
        ) as evidence_error:
            run_chapter_context_authority_migration(
                story_project=evidence_case["root"],
                manifest_path=evidence_case["manifest_path"],
            )
        self.assertEqual(
            "evidence_sha256_mismatch",
            evidence_error.exception.code,
        )

    def test_invalid_full_snapshot_is_refused(self) -> None:
        case = self._case("invalid-full-snapshot")
        invalid = copy.deepcopy(case["snapshot"])
        assert isinstance(invalid, dict)
        del invalid["story_state"]
        self._replace_pinned_snapshot(case, invalid)

        with self.assertRaises(
            ChapterContextAuthorityMigrationError
        ) as raised:
            run_chapter_context_authority_migration(
                story_project=case["root"],
                manifest_path=case["manifest_path"],
            )

        self.assertEqual(
            "snapshot_validation_failed",
            raised.exception.code,
        )

    def test_authority_normalization_difference_is_refused(
        self,
    ) -> None:
        case = self._case("authority-normalization")
        snapshot = copy.deepcopy(case["snapshot"])
        assert isinstance(snapshot, dict)
        snapshot["authoritative_state"]["unrecognized"] = {
            "must_not_be_silently_dropped": True
        }
        self._replace_pinned_snapshot(case, snapshot)

        with self.assertRaises(
            ChapterContextAuthorityMigrationError
        ) as raised:
            run_chapter_context_authority_migration(
                story_project=case["root"],
                manifest_path=case["manifest_path"],
            )

        self.assertEqual(
            "authoritative_state_normalization_mismatch",
            raised.exception.code,
        )

    def test_non_identical_existing_record_is_a_conflict(self) -> None:
        case = self._case(
            "conflict",
            existing_event={
                "event_id": _EVENT_ID,
                "type": "different_fact",
                "subjects": ["hero"],
                "objects": [],
                "location": "elsewhere",
                "status": "completed",
            },
        )

        with self.assertRaises(
            ChapterContextAuthorityMigrationError
        ) as raised:
            run_chapter_context_authority_migration(
                story_project=case["root"],
                manifest_path=case["manifest_path"],
            )

        self.assertEqual(
            "authoritative_record_conflict",
            raised.exception.code,
        )

    def test_existing_null_record_is_not_treated_as_missing(self) -> None:
        case = self._case("null-conflict")
        snapshot = copy.deepcopy(case["snapshot"])
        assert isinstance(snapshot, dict)
        snapshot["authoritative_state"]["events"][_EVENT_ID] = None
        self._replace_pinned_snapshot(case, snapshot)

        with self.assertRaises(
            ChapterContextAuthorityMigrationError
        ) as raised:
            run_chapter_context_authority_migration(
                story_project=case["root"],
                manifest_path=case["manifest_path"],
            )

        self.assertEqual(
            "authoritative_record_conflict",
            raised.exception.code,
        )

    def test_evidence_is_rechecked_before_commit_marker(self) -> None:
        case = self._case("evidence-commit-cas")
        root = case["root"]
        evidence_path = case["evidence_path"]
        snapshot_path = case["snapshot_path"]
        assert isinstance(root, Path)
        assert isinstance(evidence_path, Path)
        assert isinstance(snapshot_path, Path)
        before = snapshot_path.read_bytes()

        def mutate(
            point: str,
            _index: int | None,
            _path: Path | None,
        ) -> None:
            if point == "before_commit_marker":
                evidence_path.write_text(
                    "mutated after prepare\n",
                    encoding="utf-8",
                )

        with self.assertRaises(
            ChapterContextAuthorityMigrationError
        ) as raised:
            run_chapter_context_authority_migration(
                story_project=root,
                manifest_path=case["manifest_path"],
                apply=True,
                _fault_injector=mutate,
            )

        self.assertEqual(
            "evidence_sha256_mismatch",
            raised.exception.code,
        )
        self.assertEqual(before, snapshot_path.read_bytes())
        self.assertFalse(
            (
                root
                / ".novelagent/runtime/migrations"
                / "cca"
                / _MIGRATION_ID
                / "receipt.json"
            ).exists()
        )

    def test_event_v1_refuses_legacy_cache_migration(self) -> None:
        case = self._case("event-v1")
        root = case["root"]
        assert isinstance(root, Path)
        activate_event_authority(
            root,
            expected_identity_sha256=project_identity_sha256(root),
            canonical_state_sha256=_sha256(case["snapshot_bytes"]),
        )

        with self.assertRaises(
            ChapterContextAuthorityMigrationError
        ) as raised:
            run_chapter_context_authority_migration(
                story_project=root,
                manifest_path=case["manifest_path"],
                apply=True,
            )

        self.assertEqual(
            "event_v1_history_revision_required",
            raised.exception.code,
        )
        self.assertIn("history-revision", str(raised.exception))
        self.assertFalse(
            (
                root
                / ".novelagent/runtime/migrations"
                / "cca"
                / _MIGRATION_ID
            ).exists()
        )

    def test_invalid_collection_and_record_are_refused(self) -> None:
        invalid_collection = self._case("invalid-collection")
        collection_manifest = copy.deepcopy(
            invalid_collection["manifest"]
        )
        collection_manifest["upserts"][0]["collection"] = "unknown"
        collection_path = invalid_collection["manifest_path"]
        assert isinstance(collection_path, Path)
        collection_path.write_bytes(_json_bytes(collection_manifest))
        with self.assertRaises(
            ChapterContextAuthorityMigrationError
        ) as collection_error:
            run_chapter_context_authority_migration(
                story_project=invalid_collection["root"],
                manifest_path=collection_path,
            )
        self.assertEqual(
            "manifest_schema_invalid",
            collection_error.exception.code,
        )

        invalid_record = self._case("invalid-record")
        record_manifest = copy.deepcopy(invalid_record["manifest"])
        record_manifest["upserts"][0]["record"] = "not-an-object"
        record_path = invalid_record["manifest_path"]
        assert isinstance(record_path, Path)
        record_path.write_bytes(_json_bytes(record_manifest))
        with self.assertRaises(
            ChapterContextAuthorityMigrationError
        ) as record_error:
            run_chapter_context_authority_migration(
                story_project=invalid_record["root"],
                manifest_path=record_path,
            )
        self.assertEqual(
            "manifest_schema_invalid",
            record_error.exception.code,
        )

        mismatched_id = self._case("mismatched-record-id")
        mismatch_manifest = copy.deepcopy(mismatched_id["manifest"])
        mismatch_manifest["upserts"][0]["record"]["event_id"] = "other"
        mismatch_path = mismatched_id["manifest_path"]
        assert isinstance(mismatch_path, Path)
        mismatch_path.write_bytes(_json_bytes(mismatch_manifest))
        with self.assertRaises(
            ChapterContextAuthorityMigrationError
        ) as mismatch_error:
            run_chapter_context_authority_migration(
                story_project=mismatched_id["root"],
                manifest_path=mismatch_path,
            )
        self.assertEqual(
            "manifest_record_id_mismatch",
            mismatch_error.exception.code,
        )


if __name__ == "__main__":
    unittest.main()
