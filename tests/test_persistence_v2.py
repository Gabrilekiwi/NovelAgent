from __future__ import annotations

import hashlib
import json
import os
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from core.delivery import DeliveryQueue, FileDeliveryAdapter, delivery_payload_hash
from core.engine.persistence_v2 import (
    PersistenceV2IntegrityError,
    PersistenceV2PreparationError,
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
from core.engine.run_record import build_run_record, validate_run_result
from core.engine.safe_paths import SafePathResolver
from core.path_refs import path_ref_for, validate_path_ref
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

    @staticmethod
    def _run_result_envelope(run_id: str) -> dict:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = build_run_record(
            started_at=now,
            finished_at=now,
            base_snapshot={"chapter_index": 2},
            runtime_snapshot={"chapter_index": 2},
            memory_context={"source": "test", "status": "ready", "items": []},
            decision={
                "chapter_index": 2,
                "goal": "continue_existing_arc",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 1,
                "notes": [],
            },
            workflow=["generate_chapter", "validate"],
            input_pack="input",
            chapter="The shelter faced danger and the team chose a costly rescue.",
            validation={"ok": True, "problems": []},
            analysis={
                "validation_ok": True,
                "conflicts": ["danger"],
                "events": [{"text": "The team chose a rescue."}],
                "character_changes": [],
                "world_changes": [],
                "new_locations": [],
                "summary": "The team chose a rescue.",
            },
            repair_attempts=0,
            committed=True,
        )
        record["id"] = run_id
        return validate_run_result({"run": record})

    def _prepared(
        self,
        case: dict,
        run_id: str = "run-1",
        *,
        fault_injector=None,
        final_payload: dict | None = None,
        delivery_jobs: list[dict] | None = None,
    ) -> tuple[PersistenceV2Transaction, dict]:
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
            final_payload
            if final_payload is not None
            else {
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
            delivery_jobs=(
                delivery_jobs
                if delivery_jobs is not None
                else [
                    {
                        "id": f"delivery-{run_id}",
                        "payload_hash": self._digest("payload"),
                        "policy": {"required": True, "target": "file"},
                    }
                ]
            ),
        )
        return transaction, manifest

    def test_receipt_binding_supports_bare_run_and_valid_run_result_envelope(self) -> None:
        case = self._case("binding_shapes")
        receipt_ref = path_ref_for(
            case["runtime"] / "receipts" / "shape.json",
            root_id="runtime",
            root=case["runtime"],
        )
        expected = {"id": "receipt-shape", "path_ref": receipt_ref.to_dict()}

        bare = {"id": "bare"}
        bound_bare = bind_final_run_record_receipt(
            bare,
            receipt_id="receipt-shape",
            receipt_path_ref=receipt_ref,
        )
        self.assertEqual(expected, bound_bare["publication_receipt"])
        self.assertNotIn("publication_receipt", bare)

        envelope = self._run_result_envelope("run-envelope")
        bound_envelope = bind_final_run_record_receipt(
            envelope,
            receipt_id="receipt-shape",
            receipt_path_ref=receipt_ref,
        )
        self.assertNotIn("publication_receipt", bound_envelope)
        self.assertEqual(expected, bound_envelope["run"]["publication_receipt"])
        self.assertNotIn("publication_receipt", envelope["run"])
        self.assertIs(bound_envelope, validate_run_result(bound_envelope))

    def test_run_result_envelope_rejects_duplicate_receipt_pointer_locations(self) -> None:
        case = self._case("ambiguous_pointer")
        receipt_ref = path_ref_for(
            case["runtime"] / "receipts" / "ambiguous.json",
            root_id="runtime",
            root=case["runtime"],
        )
        pointer = {"id": "receipt-ambiguous", "path_ref": receipt_ref.to_dict()}
        envelope = self._run_result_envelope("run-ambiguous")
        envelope["run"]["publication_receipt"] = pointer
        envelope["publication_receipt"] = pointer

        with self.assertRaisesRegex(PersistenceV2PreparationError, "outer publication receipt"):
            bind_final_run_record_receipt(
                envelope,
                receipt_id="receipt-ambiguous",
                receipt_path_ref=receipt_ref,
            )

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

    def test_run_result_envelope_is_published_whole_and_tampering_invalidates_receipt(self) -> None:
        case = self._case("envelope_commit")
        envelope = self._run_result_envelope("run-envelope")
        transaction, _ = self._prepared(
            case,
            "run-envelope",
            final_payload=envelope,
        )

        result = transaction.commit()
        final_path = case["runtime"] / "runs" / "run-envelope.json"
        receipt_path = case["runtime"] / "receipts" / "run-envelope.json"
        published = json.loads(final_path.read_text(encoding="utf-8"))

        self.assertEqual("completed", result["state"])
        self.assertNotIn("publication_receipt", published)
        self.assertEqual(
            "receipt-run-envelope",
            published["run"]["publication_receipt"]["id"],
        )
        self.assertIn(
            "root_uuid",
            published["run"]["publication_receipt"]["path_ref"],
        )
        self.assertIs(published, validate_run_result(published))
        self.assertTrue(
            committed_from_publication_receipt(
                published,
                receipt_path,
                root_map=case["root_map"],
            )
        )
        self.assertTrue(
            verify_publication_receipt(receipt_path, root_map=case["root_map"])["valid"]
        )

        published["run"]["id"] = "tampered-envelope"
        final_path.write_text(json.dumps(published), encoding="utf-8")

        verification = verify_publication_receipt(receipt_path, root_map=case["root_map"])
        self.assertFalse(verification["valid"])
        self.assertFalse(verification["committed"])

    def test_final_run_result_bytes_never_change_during_delivery_reconcile(self) -> None:
        case = self._case("run_result_delivery_immutable")
        run_id = "run-result-delivery"
        payload = {"content": "canonical export\n", "encoding": "utf-8"}
        job_id = f"delivery-{run_id}"
        transaction, _ = self._prepared(
            case,
            run_id=run_id,
            final_payload=self._run_result_envelope(run_id),
            delivery_jobs=[
                {
                    "id": job_id,
                    "payload_hash": delivery_payload_hash(payload),
                    "policy": {"required": True, "target": "file"},
                }
            ],
        )
        committed = transaction.commit()
        self.assertTrue(committed["committed"])

        final_path = case["runtime"] / "runs" / f"{run_id}.json"
        receipt_path = case["runtime"] / "receipts" / f"{run_id}.json"
        immutable_bytes = final_path.read_bytes()
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        parsed_before = json.loads(immutable_bytes.decode("utf-8"))
        self.assertIs(parsed_before, validate_run_result(parsed_before))

        queue = DeliveryQueue(case["delivery"])
        export_path = case["delivery"] / "exports" / f"{run_id}.txt"
        queue.enqueue(
            job_id=job_id,
            book_id="book-1",
            run_id=run_id,
            publication_receipt_hash=receipt["receipt_hash"],
            target_type="file",
            target={
                "path_ref": path_ref_for(
                    export_path,
                    root_id="delivery_store",
                    root=case["delivery"],
                ).to_dict()
            },
            payload=payload,
            policy="required",
        )
        self.assertEqual(immutable_bytes, final_path.read_bytes())

        report = queue.reconcile(
            adapters={"file": FileDeliveryAdapter(root_map=case["root_map"])},
            worker_id="worker-1",
            run_id=run_id,
        )

        self.assertTrue(report["required_succeeded"], report)
        self.assertEqual(b"canonical export\n", export_path.read_bytes())
        self.assertEqual(immutable_bytes, final_path.read_bytes())
        parsed_after = json.loads(final_path.read_text(encoding="utf-8"))
        self.assertIs(parsed_after, validate_run_result(parsed_after))

    def test_orphan_run_result_envelope_recovers_without_changing_its_shape(self) -> None:
        case = self._case("envelope_orphan")

        def crash(event: str, _index: int | None, _path: Path | None) -> None:
            if event == "before_publication_receipt":
                raise SimulatedCrash("power loss before envelope receipt")

        transaction, _ = self._prepared(
            case,
            "run-envelope-orphan",
            fault_injector=crash,
            final_payload=self._run_result_envelope("run-envelope-orphan"),
        )
        with self.assertRaises(SimulatedCrash):
            transaction.commit()

        final_path = case["runtime"] / "runs" / "run-envelope-orphan.json"
        receipt_path = case["runtime"] / "receipts" / "run-envelope-orphan.json"
        orphan = json.loads(final_path.read_text(encoding="utf-8"))
        self.assertNotIn("publication_receipt", orphan)
        self.assertIn("publication_receipt", orphan["run"])
        self.assertFalse(receipt_path.exists())
        self.assertFalse(
            committed_from_publication_receipt(
                final_path,
                receipt_path,
                root_map=case["root_map"],
            )
        )

        report = reconcile_pending_persistence_v2(case["transaction_root"])

        self.assertTrue(report["ok"])
        self.assertEqual("completed", report["transactions"][0]["state"])
        recovered = json.loads(final_path.read_text(encoding="utf-8"))
        self.assertEqual(orphan, recovered)
        self.assertIs(recovered, validate_run_result(recovered))
        self.assertTrue(
            verify_publication_receipt(receipt_path, root_map=case["root_map"])["valid"]
        )

    def test_receipt_parent_swap_after_ensure_parent_fails_closed(self) -> None:
        case = self._case("receipt_parent_swap")
        transaction, _ = self._prepared(case)
        original_ensure_parent = SafePathResolver.ensure_parent
        swapped = False

        def swap_receipt_parent(resolver, value, **kwargs):
            nonlocal swapped
            resolved = original_ensure_parent(resolver, value, **kwargs)
            ref = validate_path_ref(value)
            if not swapped and ref.relative_path == "receipts/run-1.json":
                swapped = True
                receipt_parent = case["runtime"] / "receipts"
                receipt_parent.rename(case["runtime"] / "receipts-before-swap")
                receipt_parent.mkdir()
            return resolved

        with patch.object(
            SafePathResolver,
            "ensure_parent",
            new=swap_receipt_parent,
        ):
            result = transaction.commit()

        self.assertTrue(swapped)
        self.assertEqual("commit_marked", result["state"])
        self.assertFalse(result["committed"])
        self.assertFalse((case["runtime"] / "receipts" / "run-1.json").exists())
        self.assertFalse(
            (case["runtime"] / "receipts-before-swap" / "run-1.json").exists()
        )

    def test_apply_create_with_new_parent_uses_full_guard_and_commits(self) -> None:
        case = self._case("apply_new_parent")
        target_path = case["story"] / "genesis" / "canonical.json"
        transaction = PersistenceV2Transaction(
            transaction_root=case["transaction_root"],
            run_id="run-genesis-parent",
            book_id="book-1",
            root_map=case["root_map"],
        )
        receipt_ref = path_ref_for(
            case["runtime"] / "receipts" / "run-genesis-parent.json",
            root_id="runtime",
            root=case["runtime"],
        )
        final_ref = path_ref_for(
            case["runtime"] / "runs" / "run-genesis-parent.json",
            root_id="runtime",
            root=case["runtime"],
        )
        final = bind_final_run_record_receipt(
            {"id": "run-genesis-parent", "committed": True},
            receipt_id="receipt-run-genesis-parent",
            receipt_path_ref=receipt_ref,
        )
        transaction.prepare(
            apply_targets=[
                PersistenceV2Target(
                    target_id="genesis-canonical",
                    kind="canonical_memory",
                    path_ref=path_ref_for(
                        target_path,
                        root_id="story_project",
                        root=case["story"],
                    ),
                    content='{"genesis":true}\n',
                    expected_before_exists=False,
                )
            ],
            artifacts=[],
            final_run_record=final,
            final_run_path_ref=final_ref,
            receipt_id="receipt-run-genesis-parent",
            receipt_path_ref=receipt_ref,
            context_digest=self._digest("genesis-context"),
            generation_input_context_digest=self._digest("genesis-input"),
            story_project_source_revision_after={
                "revision": 1,
                "digest": self._digest("genesis-after"),
            },
            candidate_result={"run": {"id": "run-genesis-parent"}},
        )

        result = transaction.commit()

        self.assertTrue(result["committed"], result)
        self.assertEqual(b'{"genesis":true}\n', target_path.read_bytes())

    def test_publication_parent_mkdir_is_an_injectable_durability_boundary(self) -> None:
        case = self._case("publication_parent_mkdir_fault")
        review_parent = case["runtime"] / "reviews"
        injected = False

        def crash(event: str, _index: int | None, path: Path | None) -> None:
            nonlocal injected
            if event == "after_durability_directory_mkdir" and path == review_parent:
                injected = True
                raise SimulatedCrash("power loss after safe parent mkdir")

        transaction, _ = self._prepared(case, fault_injector=crash)
        with self.assertRaises(SimulatedCrash):
            transaction.commit()

        self.assertTrue(injected)
        self.assertTrue(transaction.marker_path.exists())
        report = reconcile_pending_persistence_v2(case["transaction_root"])
        self.assertTrue(report["ok"], report)
        self.assertEqual("completed", report["transactions"][0]["state"])
        self.assertTrue((review_parent / "run-1.json").is_file())

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

    def test_durability_fault_events_are_ordered_and_repeatable(self) -> None:
        def trace(name: str) -> list[tuple[str, int, str]]:
            case = self._case(name)
            events: list[tuple[str, int, str]] = []

            def capture(event: str, index: int | None, path: Path | None) -> None:
                if not event.startswith(("before_durability_", "after_durability_")):
                    return
                self.assertIsNotNone(index)
                self.assertIsNotNone(path)
                events.append(
                    (
                        event,
                        int(index),
                        Path(path).relative_to(case["base"]).as_posix(),
                    )
                )

            transaction, _ = self._prepared(case, fault_injector=capture)
            self.assertTrue(transaction.commit()["committed"])
            return events

        first = trace("durability_trace_a")
        second = trace("durability_trace_b")

        self.assertEqual(first, second)
        before = [item for item in first if item[0].startswith("before_")]
        after = [item for item in first if item[0].startswith("after_")]
        self.assertEqual(list(range(len(before))), [item[1] for item in before])
        self.assertEqual(len(before), len(after))
        self.assertEqual(
            [item[0].replace("before_", "", 1) for item in before],
            [item[0].replace("after_", "", 1) for item in after],
        )
        self.assertEqual(
            [(item[1], item[2]) for item in before],
            [(item[1], item[2]) for item in after],
        )
        event_names = {item[0] for item in first}
        self.assertIn("before_durability_directory_mkdir", event_names)
        self.assertIn("before_durability_file_fsync", event_names)
        self.assertIn("before_durability_replace_rename", event_names)
        self.assertIn("before_durability_journal_rename", event_names)
        self.assertIn(
            "before_durability_create_rename"
            if os.name == "nt"
            else "before_durability_create_link",
            event_names,
        )

    def test_every_durability_boundary_recovers_by_commit_marker_presence(self) -> None:
        baseline = self._case("durability_fault_matrix_baseline")
        durability_events: list[tuple[str, int]] = []

        def capture(event: str, index: int | None, _path: Path | None) -> None:
            if event.startswith(("before_durability_", "after_durability_")):
                self.assertIsNotNone(index)
                durability_events.append((event, int(index)))

        transaction, _ = self._prepared(baseline, fault_injector=capture)
        self.assertTrue(transaction.commit()["committed"])
        self.assertTrue(durability_events)

        for ordinal, (fault_event, fault_index) in enumerate(durability_events):
            with self.subTest(event=fault_event, index=fault_index):
                case = self._case(f"durability_fault_matrix_{ordinal}")
                injected = False

                def crash(
                    event: str,
                    index: int | None,
                    _path: Path | None,
                    *,
                    expected_event: str = fault_event,
                    expected_index: int = fault_index,
                ) -> None:
                    nonlocal injected
                    if event == expected_event and index == expected_index:
                        injected = True
                        raise SimulatedCrash(f"{event}:{index}")

                with self.assertRaises(SimulatedCrash):
                    faulted, _ = self._prepared(case, fault_injector=crash)
                    faulted.commit()

                self.assertTrue(injected)
                marker = (
                    case["transaction_root"]
                    / "journals"
                    / "run-1"
                    / "commit.marker"
                )
                marker_was_durable = marker.exists()
                report = reconcile_pending_persistence_v2(case["transaction_root"])
                states = [item["state"] for item in report["transactions"]]

                self.assertTrue(report["ok"], report)
                if marker_was_durable:
                    self.assertIn("completed", states, report)
                    self.assertEqual(
                        b'{"chapter":2}\n', case["snapshot"].read_bytes()
                    )
                else:
                    self.assertNotIn("completed", states, report)
                    self.assertEqual(
                        b'{"chapter":1}\n', case["snapshot"].read_bytes()
                    )

    def test_apply_rename_fault_before_marker_rolls_back(self) -> None:
        case = self._case("apply_rename_fault")

        def crash(event: str, _index: int | None, path: Path | None) -> None:
            if event == "after_durability_replace_rename" and path == case["snapshot"]:
                raise SimulatedCrash("power loss after apply rename")

        transaction, _ = self._prepared(case, fault_injector=crash)
        with self.assertRaises(SimulatedCrash):
            transaction.commit()

        self.assertFalse(transaction.marker_path.exists())
        self.assertEqual('{"chapter":2}\n', case["snapshot"].read_text(encoding="utf-8"))
        report = reconcile_pending_persistence_v2(case["transaction_root"])
        self.assertEqual("rolled_back", report["transactions"][0]["state"])
        self.assertEqual('{"chapter":1}\n', case["snapshot"].read_text(encoding="utf-8"))

    def test_marker_fsync_fault_rolls_back_but_marker_publish_fault_rolls_forward(self) -> None:
        before_case = self._case("marker_fsync_fault")
        before_marker = (
            before_case["transaction_root"] / "journals" / "run-1" / "commit.marker"
        )

        def crash_before_publish(
            event: str,
            _index: int | None,
            path: Path | None,
        ) -> None:
            if event == "after_durability_file_fsync" and path == before_marker:
                raise SimulatedCrash("power loss after marker temp-file fsync")

        before_transaction, _ = self._prepared(
            before_case,
            fault_injector=crash_before_publish,
        )
        with self.assertRaises(SimulatedCrash):
            before_transaction.commit()
        self.assertFalse(before_transaction.marker_path.exists())
        before_report = reconcile_pending_persistence_v2(before_case["transaction_root"])
        self.assertEqual("rolled_back", before_report["transactions"][0]["state"])
        self.assertEqual(
            '{"chapter":1}\n',
            before_case["snapshot"].read_text(encoding="utf-8"),
        )

        after_case = self._case("marker_publish_fault")
        after_marker = (
            after_case["transaction_root"] / "journals" / "run-1" / "commit.marker"
        )
        publish_event = (
            "after_durability_create_rename"
            if os.name == "nt"
            else "after_durability_create_link"
        )

        def crash_after_publish(
            event: str,
            _index: int | None,
            path: Path | None,
        ) -> None:
            if event == publish_event and path == after_marker:
                raise SimulatedCrash("power loss after marker namespace publication")

        after_transaction, _ = self._prepared(
            after_case,
            fault_injector=crash_after_publish,
        )
        with self.assertRaises(SimulatedCrash):
            after_transaction.commit()
        self.assertTrue(after_transaction.marker_path.exists())
        after_report = reconcile_pending_persistence_v2(after_case["transaction_root"])
        self.assertEqual("completed", after_report["transactions"][0]["state"])
        self.assertTrue(after_report["transactions"][0]["committed"])
        self.assertEqual(
            '{"chapter":2}\n',
            after_case["snapshot"].read_text(encoding="utf-8"),
        )

    def test_reconcile_exposes_rollback_durability_boundaries(self) -> None:
        case = self._case("reconcile_durability")

        def crash(event: str, _index: int | None, _path: Path | None) -> None:
            if event == "after_apply_target":
                raise SimulatedCrash("power loss before marker")

        transaction, _ = self._prepared(case, fault_injector=crash)
        with self.assertRaises(SimulatedCrash):
            transaction.commit()

        recovery_events: list[tuple[str, int | None, Path | None]] = []

        def capture(event: str, index: int | None, path: Path | None) -> None:
            if event.startswith(("before_durability_", "after_durability_")):
                recovery_events.append((event, index, path))

        report = reconcile_pending_persistence_v2(
            case["transaction_root"],
            fault_injector=capture,
        )

        self.assertEqual("rolled_back", report["transactions"][0]["state"])
        self.assertTrue(
            any(
                event == "after_durability_replace_rename"
                and index is not None
                and path == case["snapshot"]
                for event, index, path in recovery_events
            )
        )
        self.assertEqual(
            '{"chapter":1}\n',
            case["snapshot"].read_text(encoding="utf-8"),
        )

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
