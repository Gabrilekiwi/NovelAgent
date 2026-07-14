from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import unittest
import uuid

from core.delivery import DeliveryQueue
from core.delivery_intents import build_file_delivery_intent, delivery_intent_receipt_binding
from core.engine.delivery_intent_recovery import (
    DeliveryIntentRecoveryError,
    recover_completed_delivery_jobs,
    recover_delivery_jobs_for_receipt,
)
from core.engine.persistence_v2 import (
    PersistenceV2Target,
    PersistenceV2Transaction,
    bind_final_run_record_receipt,
)
from core.memory_v2 import (
    CURRENT_REDUCER_VERSION,
    apply_genesis_event,
    apply_memory_patch,
    create_genesis_memory_batch,
    create_memory_event_batch,
    create_memory_patch,
)
from core.path_refs import path_ref_for


NOW = "2026-07-14T00:00:00+00:00"
BOOK_ID = "book-delivery-recovery"
CHAPTER_BODY = "Lin reached the old station."
CHAPTER_BODY_SHA256 = hashlib.sha256(CHAPTER_BODY.encode("utf-8")).hexdigest()


class DeliveryIntentRecoveryTest(unittest.TestCase):
    def _case(self, name: str) -> dict:
        base = Path.cwd() / ".tmp" / "test_delivery_intent_recovery" / f"{name}_{uuid.uuid4().hex}"
        story = base / "story"
        runtime = base / "runtime"
        chapters = base / "chapters"
        delivery = base / "delivery"
        external = base / "external"
        for path in (story, runtime, chapters, delivery, external):
            path.mkdir(parents=True)
        snapshot = runtime / "snapshot.json"
        snapshot.write_bytes(b'{"chapter":10}\n')
        return {
            "base": base,
            "story": story,
            "runtime": runtime,
            "chapters": chapters,
            "delivery": delivery,
            "external": external,
            "snapshot": snapshot,
            "transaction_root": runtime / "persistence",
            "root_map": {
                "story_project": story,
                "runtime": runtime,
                "snapshot": runtime,
                "chapter_artifacts": chapters,
                "delivery_store": delivery,
                "external:canonical-json-export": external,
            },
        }

    def _batch(self) -> dict:
        genesis = create_genesis_memory_batch(
            book_id=BOOK_ID,
            title="Delivery recovery fixture",
            source_project_digest="a" * 64,
            context_digest="b" * 64,
        )
        projection = apply_genesis_event(genesis["events"][0])
        patch = create_memory_patch(
            patch_id="chapter-11",
            source_kind="chapter",
            operations=[
                {
                    "op": "update_story_time",
                    "value": {
                        "label": "chapter 11",
                        "elapsed_minutes": 10,
                        "chapter_index": 11,
                    },
                }
            ],
        )
        _, events = apply_memory_patch(
            projection,
            patch,
            reducer_version=CURRENT_REDUCER_VERSION,
            event_context={
                "chapter_body": CHAPTER_BODY,
                "evidence_spans": [
                    {"start_char": 0, "end_char": len(CHAPTER_BODY), "quote": CHAPTER_BODY}
                ],
                "authority_epoch": 1,
            },
        )
        return create_memory_event_batch(
            book_id=BOOK_ID,
            patch=patch,
            events=events,
            expected_revision=projection["revision"],
            previous_batch_hash=genesis["batch_hash"],
            source_project_digest="c" * 64,
            context_digest="d" * 64,
            batch_kind="chapter",
            publication_status="committed",
            schema_version="2.2",
            reducer_version=CURRENT_REDUCER_VERSION,
        )

    def _intent(self, run_id: str) -> dict:
        return build_file_delivery_intent(
            profile={
                "schema_version": "1.0",
                "profile_id": "canonical-json-export",
                "root_id": "external:canonical-json-export",
                "root_uuid": None,
                "relative_directory": "canonical-chapters",
                "filename_template": "chapter-{chapter_index}-{run_id}.json",
            },
            book_id=BOOK_ID,
            run_id=run_id,
            chapter_index=11,
            event_batch=self._batch(),
            chapter_body_sha256=CHAPTER_BODY_SHA256,
            policy="required",
            created_at=NOW,
        )

    def _complete(
        self,
        case: dict,
        *,
        run_id: str = "run-11",
        publish_intent_artifact: bool = True,
    ) -> dict:
        intent = self._intent(run_id)
        receipt_path = case["runtime"] / "receipts" / f"{run_id}.json"
        receipt_ref = path_ref_for(receipt_path, root_id="runtime", root=case["runtime"])
        final_ref = path_ref_for(
            case["runtime"] / "runs" / f"{run_id}.json",
            root_id="runtime",
            root=case["runtime"],
        )
        final_record = bind_final_run_record_receipt(
            {
                "id": run_id,
                "status": "committed",
                "committed": True,
                "chapter_index": 11,
            },
            receipt_id=f"receipt-{run_id}",
            receipt_path_ref=receipt_ref,
        )
        artifacts = []
        intent_path = case["runtime"] / "delivery-intents" / f"{run_id}.json"
        if publish_intent_artifact:
            artifacts.append(
                PersistenceV2Target(
                    target_id=f"delivery-intent-{run_id}",
                    kind="delivery_intent",
                    path_ref=path_ref_for(intent_path, root_id="runtime", root=case["runtime"]),
                    content=json.dumps(intent, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    phase="publication",
                )
            )
        transaction = PersistenceV2Transaction(
            transaction_root=case["transaction_root"],
            run_id=run_id,
            book_id=BOOK_ID,
            root_map=case["root_map"],
        )
        transaction.prepare(
            apply_targets=[
                PersistenceV2Target(
                    target_id=f"snapshot-{run_id}",
                    kind="snapshot",
                    path_ref=path_ref_for(
                        case["snapshot"], root_id="snapshot", root=case["runtime"]
                    ),
                    content='{"chapter":11}\n',
                    expected_before_exists=True,
                    expected_before_sha256=hashlib.sha256(b'{"chapter":10}\n').hexdigest(),
                )
            ],
            artifacts=artifacts,
            final_run_record=final_record,
            final_run_path_ref=final_ref,
            receipt_id=f"receipt-{run_id}",
            receipt_path_ref=receipt_ref,
            context_digest="e" * 64,
            generation_input_context_digest="f" * 64,
            story_project_source_revision_after={"revision": 11},
            candidate_result={"run_id": run_id, "accepted": True},
            delivery_jobs=[delivery_intent_receipt_binding(intent)],
        )
        result = transaction.commit()
        self.assertTrue(result["committed"], result)
        return {
            "intent": intent,
            "intent_path": intent_path,
            "receipt_path": receipt_path,
            "result": result,
        }

    def _queue(self, case: dict) -> DeliveryQueue:
        return DeliveryQueue(
            case["delivery"],
            clock=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
        )

    def test_receipt_recovery_materializes_once_without_delivering(self) -> None:
        case = self._case("receipt_idempotent")
        completed = self._complete(case)
        queue = self._queue(case)

        first = recover_delivery_jobs_for_receipt(
            completed["receipt_path"], root_map=case["root_map"], queue=queue
        )
        second = recover_delivery_jobs_for_receipt(
            completed["receipt_path"], root_map=case["root_map"], queue=queue
        )

        self.assertTrue(first["ok"])
        self.assertEqual(1, first["job_count"])
        self.assertEqual(first["jobs"], second["jobs"])
        job = queue.load(completed["intent"]["intent_id"])
        self.assertEqual("pending", job["state"])
        self.assertEqual(0, job["attempt_count"])
        self.assertEqual([], queue.inspect(job["job_id"])["attempts"])
        self.assertEqual([], list(case["external"].rglob("*.json")))

    def test_completed_registry_scan_recovers_the_post_receipt_crash_window(self) -> None:
        case = self._case("completed_scan")
        completed = self._complete(case)
        queue = self._queue(case)

        report = recover_completed_delivery_jobs(
            case["transaction_root"], root_map=case["root_map"], queue=queue
        )
        repeated = recover_completed_delivery_jobs(
            case["transaction_root"], root_map=case["root_map"], queue=queue
        )

        self.assertEqual(1, report["receipt_count"])
        self.assertEqual(1, report["job_count"])
        self.assertEqual(report["jobs"], repeated["jobs"])
        self.assertEqual("pending", queue.load(completed["intent"]["intent_id"])["state"])

    def test_explicit_intent_path_can_recover_when_receipt_has_no_intent_artifact(self) -> None:
        case = self._case("explicit")
        completed = self._complete(case, publish_intent_artifact=False)
        explicit_path = case["base"] / "explicit-intent.json"
        explicit_path.write_text(
            json.dumps(completed["intent"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        queue = self._queue(case)

        report = recover_completed_delivery_jobs(
            case["transaction_root"],
            root_map=case["root_map"],
            queue=queue,
            intent_paths=[explicit_path],
        )

        self.assertEqual(1, report["job_count"])
        self.assertEqual("explicit", report["jobs"][0]["intent_source"])
        self.assertEqual("pending", queue.load(completed["intent"]["intent_id"])["state"])

    def test_missing_or_orphan_intent_fails_before_any_job_is_created(self) -> None:
        case = self._case("missing")
        self._complete(case, publish_intent_artifact=False)
        queue = self._queue(case)

        with self.assertRaisesRegex(
            DeliveryIntentRecoveryError, "delivery_recovery_intent_missing"
        ):
            recover_completed_delivery_jobs(
                case["transaction_root"], root_map=case["root_map"], queue=queue
            )
        self.assertFalse(queue.jobs_dir.exists())

        case = self._case("orphan")
        self._complete(case)
        orphan = self._intent("orphan-run")
        orphan_path = case["base"] / "orphan.json"
        orphan_path.write_text(json.dumps(orphan, sort_keys=True) + "\n", encoding="utf-8")
        queue = self._queue(case)
        with self.assertRaisesRegex(
            DeliveryIntentRecoveryError, "delivery_recovery_orphan_intent"
        ):
            recover_completed_delivery_jobs(
                case["transaction_root"],
                root_map=case["root_map"],
                queue=queue,
                intent_paths=[orphan_path],
            )
        self.assertFalse(queue.jobs_dir.exists())

    def test_tampered_receipt_or_intent_artifact_fails_closed(self) -> None:
        case = self._case("tampered_intent")
        completed = self._complete(case)
        completed["intent_path"].write_text('{"tampered":true}\n', encoding="utf-8")
        queue = self._queue(case)
        with self.assertRaisesRegex(
            DeliveryIntentRecoveryError, "delivery_recovery_receipt_untrusted"
        ):
            recover_completed_delivery_jobs(
                case["transaction_root"], root_map=case["root_map"], queue=queue
            )
        self.assertFalse(queue.jobs_dir.exists())

        case = self._case("tampered_receipt")
        completed = self._complete(case)
        receipt = json.loads(completed["receipt_path"].read_text(encoding="utf-8"))
        receipt["delivery_jobs"] = []
        completed["receipt_path"].write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        queue = self._queue(case)
        with self.assertRaisesRegex(
            DeliveryIntentRecoveryError, "delivery_recovery_receipt_untrusted"
        ):
            recover_delivery_jobs_for_receipt(
                completed["receipt_path"], root_map=case["root_map"], queue=queue
            )
        self.assertFalse(queue.jobs_dir.exists())

    def test_existing_incompatible_job_is_rejected_without_overwrite(self) -> None:
        case = self._case("job_collision")
        completed = self._complete(case)
        intent = completed["intent"]
        queue = self._queue(case)
        existing = queue.enqueue(
            job_id=intent["intent_id"],
            book_id=intent["book_id"],
            run_id=intent["run_id"],
            publication_receipt_hash="0" * 64,
            target_type="file",
            target=intent["target"],
            payload={"content": "forged\n"},
            policy="required",
        )

        with self.assertRaisesRegex(
            DeliveryIntentRecoveryError, "delivery_recovery_job_collision"
        ):
            recover_delivery_jobs_for_receipt(
                completed["receipt_path"], root_map=case["root_map"], queue=queue
            )

        after = queue.load(intent["intent_id"])
        self.assertEqual(existing, after)
        self.assertEqual("pending", after["state"])


if __name__ == "__main__":
    unittest.main()
