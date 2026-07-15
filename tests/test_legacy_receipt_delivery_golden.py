from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import unittest
import uuid

from core.delivery import load_delivery_job
from core.engine.persistence import reconcile_persistence_transaction
from core.engine.persistence_v2 import validate_publication_receipt
from core.story_project.identity import ensure_project_identity
from core.story_project.migration_execution import execute_event_authority_migration
from core.story_project.migration_v2 import build_migration_approval, build_migration_plan
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.paths import canonical_prose_path


LEGACY_PUBLICATION_RECEIPT_BYTES = (
    '{"apply_targets":[],"artifact_bundle_digest":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",'
    '"artifacts":[],"book_id":"legacy-book-1","candidate_digest":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",'
    '"canonical_json_algorithm":"novelagent-canonical-json-v1","context_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    '"delivery_jobs":[],"final_run":{"kind":"final_run_record","path_ref":{"relative_path":"runs/legacy-run-1.json",'
    '"root_id":"runtime"},"sha256":"1111111111111111111111111111111111111111111111111111111111111111","size":2,"target_id":"final"},'
    '"generation_input_context_digest":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
    '"manifest":{"path_ref":{"relative_path":"persistence/journals/legacy-run-1/manifest.json","root_id":"runtime"},'
    '"sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"},'
    '"marker":{"path_ref":{"relative_path":"persistence/markers/legacy-run-1.json","root_id":"runtime"},'
    '"sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"},'
    '"published_at":"2026-01-01T00:00:00+00:00","receipt_hash":"c8bf82b3749d43c2989e689046d2248f0221d07b79e0467e4646ed60e0c29fbd",'
    '"receipt_id":"legacy-receipt-1","receipt_path_ref":{"relative_path":"receipts/legacy.json","root_id":"runtime"},'
    '"run_id":"legacy-run-1","schema_version":"2.0","story_project_source_revision_after":{"revision":1}}\n'
).encode("utf-8")

LEGACY_DELIVERY_JOB_BYTES = (
    '{"attempt_count":0,"book_id":"legacy-book-1","confirmed_absent_at":null,"created_at":"2026-01-01T00:00:00+00:00",'
    '"job_id":"legacy-job-1","last_attempt_receipt":null,"lease":null,'
    '"operation_id":"novelagent:61acfd2da78b12b0a55eccc1411a5f646e70e54b62d273eff61c817ba15bb3bc",'
    '"payload":{},"payload_hash":"44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",'
    '"policy":"not_required","publication_receipt_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    '"run_id":"legacy-run-1","schema_version":"1.0","state":"not_required","target":{},"target_type":"none",'
    '"uncertain_since":null,"updated_at":"2026-01-01T00:00:00+00:00"}\n'
).encode("utf-8")

LEGACY_JOURNAL_MANIFEST_WINDOWS_BYTES = (
    '{"allowed_roots":["C:/novelagent-v1-golden"],"book_id":"legacy-book-1","candidate_result_path":null,'
    '"candidate_sha256":null,"commit_marker":"commit.marker","created_at":"2026-01-01T00:00:00+00:00",'
    '"errors":[],"read_set_declared_writes":[],"run_id":"legacy-journal-1","schema_version":1,"state":"completed",'
    '"story_project_read_set":null,"targets":[],"updated_at":"2026-01-01T00:00:01+00:00"}\n'
).encode("utf-8")

LEGACY_JOURNAL_MANIFEST_POSIX_BYTES = (
    '{"allowed_roots":["/var/tmp/novelagent-v1-golden"],"book_id":"legacy-book-1","candidate_result_path":null,'
    '"candidate_sha256":null,"commit_marker":"commit.marker","created_at":"2026-01-01T00:00:00+00:00",'
    '"errors":[],"read_set_declared_writes":[],"run_id":"legacy-journal-1","schema_version":1,"state":"completed",'
    '"story_project_read_set":null,"targets":[],"updated_at":"2026-01-01T00:00:01+00:00"}\n'
).encode("utf-8")

LEGACY_JOURNAL_MARKER_BYTES = (
    '{"candidate_sha256":null,"committed_at":"2026-01-01T00:00:01+00:00","run_id":"legacy-journal-1"}\n'
).encode("utf-8")

LEGACY_RUN_RECORD_BYTES = (
    '{"run":{"committed":true,"id":"legacy-run-1","schema_version":"1.0","status":"committed"}}\r\n'
).encode("utf-8")


def _legacy_journal_manifest_bytes() -> bytes:
    return (
        LEGACY_JOURNAL_MANIFEST_WINDOWS_BYTES
        if os.name == "nt"
        else LEGACY_JOURNAL_MANIFEST_POSIX_BYTES
    )


def _legacy_journal_manifest_sha256() -> str:
    return (
        "15394fa267a287cf3c6e21b52765d452feafaf6bb627747560b9730bee893f0e"
        if os.name == "nt"
        else "de5e11e92613c5a2cac44aca702c95977e6116318b11044f9ab99176d7296a97"
    )


class LegacyReceiptDeliveryGoldenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (
            Path.cwd()
            / ".tmp"
            / "test_legacy_receipt_delivery_golden"
            / uuid.uuid4().hex
        )
        self.root.mkdir(parents=True)

    @staticmethod
    def _write_legacy_journal(parent: Path) -> Path:
        journal = parent / "legacy-journal-1"
        journal.mkdir(parents=True)
        (journal / "manifest.json").write_bytes(_legacy_journal_manifest_bytes())
        (journal / "commit.marker").write_bytes(LEGACY_JOURNAL_MARKER_BYTES)
        return journal

    @staticmethod
    def _journal_validation(journal: Path) -> dict:
        result = reconcile_persistence_transaction(journal)
        return {
            "state": result.state,
            "committed": result.committed,
            "partial": result.partial,
            "targets": result.targets,
            "errors": result.errors,
            "candidate_result_path": result.candidate_result_path,
        }

    def test_publication_receipt_v20_validates_without_rewrite(self) -> None:
        path = self.root / "publication-receipt-v2.0.json"
        path.write_bytes(LEGACY_PUBLICATION_RECEIPT_BYTES)
        before = path.read_bytes()

        validated = validate_publication_receipt(
            json.loads(before.decode("utf-8"))
        )

        self.assertEqual("2.0", validated["schema_version"])
        self.assertEqual(
            "c8bf82b3749d43c2989e689046d2248f0221d07b79e0467e4646ed60e0c29fbd",
            validated["receipt_hash"],
        )
        self.assertEqual(
            "0b0100abda4e217640a506f2567d555acf7f8abf1e5ac1cc633bfe6c4807f03f",
            hashlib.sha256(before).hexdigest(),
        )
        self.assertEqual(before, path.read_bytes())

    def test_delivery_job_v10_loads_without_rewrite(self) -> None:
        path = self.root / "delivery-job-v1.0.json"
        path.write_bytes(LEGACY_DELIVERY_JOB_BYTES)
        before = path.read_bytes()

        loaded = load_delivery_job(path)

        self.assertEqual("1.0", loaded["schema_version"])
        self.assertEqual("not_required", loaded["state"])
        self.assertEqual(
            "f343640a76ad2b86d8be25c56676150666c7dc0cd8dc24e435e4d75a3b01e55f",
            hashlib.sha256(before).hexdigest(),
        )
        self.assertEqual(before, path.read_bytes())

    def test_persistence_journal_v1_reconciles_without_rewrite(self) -> None:
        journal = self._write_legacy_journal(self.root)
        manifest_path = journal / "manifest.json"
        marker_path = journal / "commit.marker"
        before = {
            manifest_path: manifest_path.read_bytes(),
            marker_path: marker_path.read_bytes(),
        }

        validated = self._journal_validation(journal)

        self.assertEqual(
            {
                "state": "completed",
                "committed": True,
                "partial": False,
                "targets": (),
                "errors": (),
                "candidate_result_path": None,
            },
            validated,
        )
        self.assertEqual(
            _legacy_journal_manifest_sha256(),
            hashlib.sha256(before[manifest_path]).hexdigest(),
        )
        self.assertEqual(
            "baa62be77c30b1ceae53d07c14d5c3387436de7215fb5699b9edd5044427db51",
            hashlib.sha256(before[marker_path]).hexdigest(),
        )
        for path, content in before.items():
            self.assertEqual(content, path.read_bytes(), path)

    def test_event_authority_upgrade_keeps_legacy_bytes_and_validation_results(self) -> None:
        book = self.root / "book"
        for directory in CORE_DIRECTORY_NAMES:
            (book / directory).mkdir(parents=True)
        ensure_project_identity(book, book_id="legacy-book-1")
        canonical_prose_path(book, 1).write_text("Chapter one happened.\n", encoding="utf-8")
        canonical_prose_path(book, 10).write_text("Chapter ten happened.\n", encoding="utf-8")

        runtime = book / ".novelagent" / "runtime"
        run_record_path = runtime / "runs" / "legacy-run-1.json"
        receipt_path = runtime / "receipts" / "legacy-publication-receipt.json"
        delivery_job_path = runtime / "deliveries" / "jobs" / "legacy-job-1.json"
        for path in (run_record_path, receipt_path, delivery_job_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        run_record_path.write_bytes(LEGACY_RUN_RECORD_BYTES)
        receipt_path.write_bytes(LEGACY_PUBLICATION_RECEIPT_BYTES)
        delivery_job_path.write_bytes(LEGACY_DELIVERY_JOB_BYTES)
        journal = self._write_legacy_journal(runtime / "runs" / "transactions")

        legacy_paths = (
            run_record_path,
            receipt_path,
            delivery_job_path,
            journal / "manifest.json",
            journal / "commit.marker",
        )
        before_bytes = {path: path.read_bytes() for path in legacy_paths}
        self.assertEqual(
            "88cc94567c4988488fb096ff9e8ece2df3301d4d86abc9f358cbb4eca8d4bf27",
            hashlib.sha256(before_bytes[run_record_path]).hexdigest(),
        )
        before_validation = {
            "journal": self._journal_validation(journal),
            "receipt": validate_publication_receipt(
                json.loads(receipt_path.read_text(encoding="utf-8"))
            ),
            "delivery_job": load_delivery_job(delivery_job_path),
        }

        plan = build_migration_plan(book, created_at="2026-07-14T00:00:00+00:00")
        source_hashes = {
            item["relative_path"]: item["sha256"] for item in plan["sources"]
        }
        for path in (
            run_record_path,
            receipt_path,
            delivery_job_path,
            journal / "manifest.json",
            journal / "commit.marker",
        ):
            relative = path.relative_to(book).as_posix()
            self.assertEqual(
                hashlib.sha256(before_bytes[path]).hexdigest(), source_hashes[relative]
            )
        approval = build_migration_approval(
            plan,
            decisions={
                "timeline_elapsed_minutes": 155,
                "chapter_10_character_state": {
                    "hero": {"location": "gate", "condition": "injured"}
                },
                "open_foreshadowing": [
                    {
                        "id": "thread-door",
                        "status": "open",
                        "description": "door remains open",
                    }
                ],
                "inventory": {"hero": {"key": 1}},
                "lexicon": {"black_tide": {"known_by": ["hero"]}},
                "corruption": {"hero": 3},
            },
            approver_id="compatibility-test",
            approved_at="2026-07-14T00:00:00+00:00",
        )
        result = execute_event_authority_migration(book, plan=plan, approval=approval)

        after_validation = {
            "journal": self._journal_validation(journal),
            "receipt": validate_publication_receipt(
                json.loads(receipt_path.read_text(encoding="utf-8"))
            ),
            "delivery_job": load_delivery_job(delivery_job_path),
        }
        self.assertEqual("completed", result["status"])
        self.assertEqual(before_validation, after_validation)
        for path, content in before_bytes.items():
            self.assertEqual(content, path.read_bytes(), path)


if __name__ == "__main__":
    unittest.main()
