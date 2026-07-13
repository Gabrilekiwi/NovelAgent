from __future__ import annotations

import hashlib
import json
import unittest
import uuid
from pathlib import Path

from core.engine.persistence_v2 import (
    PersistenceV2IntegrityError,
    PersistenceV2Target,
    PersistenceV2Transaction,
    bind_final_run_record_receipt,
    committed_from_publication_receipt,
    gc_persistence_v2,
    load_commit_marker_v2,
    load_persistence_manifest_v2,
    reconcile_pending_persistence_v2,
    validate_persistence_manifest_v2,
    verify_publication_receipt,
)
from core.path_refs import path_ref_for
from core.schema import validate_schema


class SimulatedCrash(BaseException):
    pass


class PersistenceV2Test(unittest.TestCase):
    def _case(self, name: str) -> dict:
        base = Path.cwd() / ".tmp" / "test_persistence_v2" / f"{name}_{uuid.uuid4().hex}"
        story = base / "story"
        runtime = base / "runtime"
        chapters = base / "chapters"
        delivery = base / "delivery"
        for path in (story, runtime, chapters, delivery):
            path.mkdir(parents=True)
        snapshot = runtime / "snapshot.json"
        snapshot.write_bytes(b'{"chapter":1}\n')
        return {
            "base": base,
            "story": story,
            "runtime": runtime,
            "chapters": chapters,
            "delivery": delivery,
            "snapshot": snapshot,
            "transaction_root": runtime / "persistence",
            "root_map": {
                "story_project": story,
                "runtime": runtime,
                "snapshot": runtime,
                "chapter_artifacts": chapters,
                "delivery_store": delivery,
            },
        }

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _prepared(self, case: dict, run_id: str = "run-1", *, fault_injector=None) -> tuple[PersistenceV2Transaction, dict]:
        runtime = case["runtime"]
        receipt_ref = path_ref_for(
            runtime / "receipts" / f"{run_id}.json",
            root_id="runtime",
            root=runtime,
        )
        final_ref = path_ref_for(
            runtime / "runs" / f"{run_id}.json",
            root_id="runtime",
            root=runtime,
        )
        final_record = bind_final_run_record_receipt(
            {
                "id": run_id,
                "status": "committed",
                "committed": True,
                "chapter_index": 2,
                "immutable_value": "bound",
            },
            receipt_id=f"receipt-{run_id}",
            receipt_path_ref=receipt_ref,
        )
        transaction = PersistenceV2Transaction(
            transaction_root=case["transaction_root"],
            run_id=run_id,
            book_id="book-1",
            root_map=case["root_map"],
            fault_injector=fault_injector,
        )
        manifest = transaction.prepare(
            apply_targets=[
                PersistenceV2Target(
                    target_id="snapshot",
                    kind="snapshot",
                    path_ref=path_ref_for(
                        case["snapshot"],
                        root_id="snapshot",
                        root=case["runtime"],
                    ),
                    content='{"chapter":2}\n',
                    expected_before_exists=True,
                    expected_before_sha256=self._digest('{"chapter":1}\n'),
                )
            ],
            artifacts=[
                PersistenceV2Target(
                    target_id="chapter",
                    kind="chapter_artifact",
                    path_ref=path_ref_for(
                        case["chapters"] / f"{run_id}.md",
                        root_id="chapter_artifacts",
                        root=case["chapters"],
                    ),
                    content="chapter\n",
                    phase="publication",
                ),
                PersistenceV2Target(
                    target_id="review",
                    kind="review_artifact",
                    path_ref=path_ref_for(
                        case["runtime"] / "reviews" / f"{run_id}.json",
                        root_id="runtime",
                        root=case["runtime"],
                    ),
                    content='{"ok":true}\n',
                    phase="publication",
                ),
            ],
            final_run_record=final_record,
            final_run_path_ref=final_ref,
            receipt_id=f"receipt-{run_id}",
            receipt_path_ref=receipt_ref,
            context_digest=self._digest("context"),
            generation_input_context_digest=self._digest("input-context"),
            story_project_source_revision_after={"revision": 2, "digest": self._digest("story-after")},
            candidate_result={"run": {"id": run_id}, "candidate": True},
            delivery_jobs=[
                {
                    "id": f"delivery-{run_id}",
                    "payload_hash": self._digest("payload"),
                    "policy": {"required": True, "target": "file"},
                }
            ],
        )
        return transaction, manifest

    def test_commit_requires_valid_receipt_and_binds_hash_dag(self) -> None:
        case = self._case("commit")
        transaction, manifest = self._prepared(case)

        result = transaction.commit()
        receipt_path = case["runtime"] / "receipts" / "run-1.json"
        final_path = case["runtime"] / "runs" / "run-1.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        marker = load_commit_marker_v2(transaction.marker_path)

        self.assertEqual("completed", result["state"])
        self.assertTrue(result["committed"])
        self.assertEqual(manifest["manifest_digest"], marker["manifest_digest"])
        self.assertEqual(receipt["marker"]["sha256"], marker["marker_hash"])
        self.assertEqual(receipt["final_run"]["sha256"], marker["final_run_hash"])
        self.assertNotIn("receipt_hash", json.loads(final_path.read_text(encoding="utf-8"))["publication_receipt"])
        verification = verify_publication_receipt(receipt_path, root_map=case["root_map"])
        self.assertTrue(verification["valid"])
        self.assertEqual(receipt["delivery_jobs"], verification["delivery_jobs"])
        self.assertTrue(
            committed_from_publication_receipt(final_path, receipt_path, root_map=case["root_map"])
        )
        self.assertFalse(transaction.pending_entry_path.exists())
        self.assertTrue((case["transaction_root"] / "registry" / "completed" / "run-1.json").exists())

    def test_orphan_final_run_is_not_committed_and_marker_recovery_ignores_candidate(self) -> None:
        case = self._case("orphan")

        def crash(event: str, _index: int | None, _path: Path | None) -> None:
            if event == "before_publication_receipt":
                raise SimulatedCrash("power loss before receipt")

        transaction, _ = self._prepared(case, fault_injector=crash)
        with self.assertRaises(SimulatedCrash):
            transaction.commit()

        final_path = case["runtime"] / "runs" / "run-1.json"
        receipt_path = case["runtime"] / "receipts" / "run-1.json"
        self.assertTrue(final_path.exists())
        self.assertFalse(receipt_path.exists())
        self.assertFalse(
            committed_from_publication_receipt(final_path, receipt_path, root_map=case["root_map"])
        )
        transaction.candidate_path.write_text("corrupted after marker", encoding="utf-8")

        report = reconcile_pending_persistence_v2(case["transaction_root"])

        self.assertTrue(report["ok"])
        self.assertEqual("completed", report["transactions"][0]["state"])
        self.assertTrue(receipt_path.exists())

    def test_crash_before_marker_rolls_back_with_cas(self) -> None:
        case = self._case("rollback")

        def crash(event: str, _index: int | None, _path: Path | None) -> None:
            if event == "after_apply_target":
                raise SimulatedCrash("power loss before marker")

        transaction, _ = self._prepared(case, fault_injector=crash)
        with self.assertRaises(SimulatedCrash):
            transaction.commit()
        self.assertEqual('{"chapter":2}\n', case["snapshot"].read_text(encoding="utf-8"))

        report = reconcile_pending_persistence_v2(case["transaction_root"])

        self.assertEqual("rolled_back", report["transactions"][0]["state"])
        self.assertEqual('{"chapter":1}\n', case["snapshot"].read_text(encoding="utf-8"))
        self.assertTrue((transaction.journal_dir / "failure_receipt.json").exists())

    def test_pending_candidate_corruption_fails_closed(self) -> None:
        case = self._case("candidate")
        transaction, _ = self._prepared(case)
        transaction.candidate_path.write_text("bad", encoding="utf-8")

        report = reconcile_pending_persistence_v2(case["transaction_root"])

        self.assertFalse(report["ok"])
        self.assertEqual(["run-1"], report["recovery_required"])
        self.assertEqual('{"chapter":1}\n', case["snapshot"].read_text(encoding="utf-8"))
        self.assertEqual("recovery_required", load_persistence_manifest_v2(transaction.manifest_path)["state"])

    def test_post_marker_recovery_never_rolls_back_external_state_edit(self) -> None:
        case = self._case("post_marker_drift")

        def crash(event: str, _index: int | None, _path: Path | None) -> None:
            if event == "after_commit_marker":
                raise SimulatedCrash("power loss after marker")

        transaction, _ = self._prepared(case, fault_injector=crash)
        with self.assertRaises(SimulatedCrash):
            transaction.commit()
        case["snapshot"].write_text("external post-marker edit\n", encoding="utf-8")

        report = reconcile_pending_persistence_v2(case["transaction_root"])

        self.assertEqual(["run-1"], report["recovery_required"])
        self.assertEqual("external post-marker edit\n", case["snapshot"].read_text(encoding="utf-8"))
        self.assertTrue(transaction.marker_path.exists())
        self.assertEqual("recovery_required", load_persistence_manifest_v2(transaction.manifest_path)["state"])

    def test_external_edit_before_apply_is_never_overwritten_by_rollback(self) -> None:
        case = self._case("external_edit")
        transaction, _ = self._prepared(case)
        case["snapshot"].write_text("external edit\n", encoding="utf-8")

        result = transaction.commit()

        self.assertEqual("recovery_required", result["state"])
        self.assertFalse(result["committed"])
        self.assertEqual("external edit\n", case["snapshot"].read_text(encoding="utf-8"))

    def test_final_run_tampering_invalidates_receipt(self) -> None:
        case = self._case("final_tamper")
        transaction, _ = self._prepared(case)
        transaction.commit()
        final_path = case["runtime"] / "runs" / "run-1.json"
        record = json.loads(final_path.read_text(encoding="utf-8"))
        record["immutable_value"] = "tampered"
        final_path.write_text(json.dumps(record), encoding="utf-8")

        verification = verify_publication_receipt(
            case["runtime"] / "receipts" / "run-1.json",
            root_map=case["root_map"],
        )

        self.assertFalse(verification["valid"])
        self.assertFalse(verification["committed"])

    def test_marker_tampering_invalidates_receipt(self) -> None:
        case = self._case("marker_tamper")
        transaction, _ = self._prepared(case)
        transaction.commit()
        marker = json.loads(transaction.marker_path.read_text(encoding="utf-8"))
        marker["candidate_digest"] = "0" * 64
        transaction.marker_path.write_text(json.dumps(marker), encoding="utf-8")

        verification = verify_publication_receipt(
            case["runtime"] / "receipts" / "run-1.json",
            root_map=case["root_map"],
        )

        self.assertFalse(verification["valid"])

    def test_manifest_digest_excludes_mutable_state_but_binds_immutable_section(self) -> None:
        case = self._case("manifest_digest")
        transaction, manifest = self._prepared(case)
        changed_state = json.loads(json.dumps(manifest))
        changed_state["state"] = "applying"
        changed_state["errors"].append({"code": "test"})
        self.assertIs(changed_state, validate_persistence_manifest_v2(changed_state))

        changed_immutable = json.loads(json.dumps(manifest))
        changed_immutable["immutable"]["context_digest"] = "0" * 64
        with self.assertRaisesRegex(PersistenceV2IntegrityError, "digest mismatch"):
            validate_persistence_manifest_v2(changed_immutable)

        loaded = load_persistence_manifest_v2(transaction.manifest_path)
        self.assertTrue(
            all("path_ref" in target and "path" not in target for target in loaded["immutable"]["targets"])
        )

    def test_completed_transactions_are_not_scanned_or_candidate_validated_at_startup(self) -> None:
        case = self._case("completed_startup")
        transaction, _ = self._prepared(case)
        transaction.commit()
        transaction.candidate_path.write_text("damaged but no longer required", encoding="utf-8")
        stray = case["transaction_root"] / "journals" / "stray-completed"
        stray.mkdir(parents=True)
        (stray / "manifest.json").write_text("not json", encoding="utf-8")

        report = reconcile_pending_persistence_v2(case["transaction_root"])

        self.assertEqual(0, report["transaction_count"])
        self.assertTrue(report["ok"])

    def test_gc_keeps_completed_and_rolled_back_limits_separately(self) -> None:
        case = self._case("gc")
        root = case["transaction_root"]
        for state in ("completed", "rolled_back"):
            for index in range(3):
                run_id = f"{state}-{index}"
                journal = root / "journals" / run_id
                (journal / "staged").mkdir(parents=True)
                (journal / "backups").mkdir()
                (journal / "staged" / "target.bin").write_bytes(b"x" * (index + 1))
                (journal / "backups" / "target.bin").write_bytes(b"b")
                (journal / "candidate_result.json").write_bytes(b"c")
                entry = {
                    "schema_version": "2.0",
                    "book_id": "book-1",
                    "run_id": run_id,
                    "state": state,
                    "journal_relative_path": f"journals/{run_id}",
                    "manifest_digest": self._digest(run_id),
                    "registered_at": f"2026-01-0{index + 1}T00:00:00+00:00",
                    "receipt": None,
                    "error_count": 0,
                }
                path = root / "registry" / state / f"{run_id}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(entry), encoding="utf-8")
                validate_schema(entry, "persistence_registry_entry.schema.json")

        dry = gc_persistence_v2(root, dry_run=True, completed_keep=1, rolled_back_keep=1)
        real = gc_persistence_v2(root, dry_run=False, completed_keep=1, rolled_back_keep=1)

        self.assertEqual(dry["deleted"], real["deleted"])
        self.assertEqual(12, len(real["deleted"]))
        self.assertTrue((root / "journals" / "completed-2" / "candidate_result.json").exists())
        self.assertTrue((root / "journals" / "rolled_back-2" / "candidate_result.json").exists())

    def test_gc_refuses_to_run_while_recovery_is_required(self) -> None:
        case = self._case("gc_recovery")
        root = case["transaction_root"]
        entry = {
            "schema_version": "2.0",
            "book_id": "book-1",
            "run_id": "recovery-1",
            "state": "recovery_required",
            "journal_relative_path": "journals/recovery-1",
            "manifest_digest": self._digest("manifest"),
            "registered_at": "2026-01-01T00:00:00+00:00",
            "receipt": None,
            "error_count": 1,
        }
        path = root / "registry" / "recovery_required" / "recovery-1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry), encoding="utf-8")

        report = gc_persistence_v2(root, dry_run=True)

        self.assertEqual([], report["deleted"])
        self.assertIn("recovery_required_transactions_are_permanently_retained", report["skipped_reasons"])


if __name__ == "__main__":
    unittest.main()
