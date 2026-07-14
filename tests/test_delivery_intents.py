from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
import unittest
import uuid

from core.delivery import DeliveryQueue
from core.delivery_intents import (
    DeliveryIntentError,
    build_file_delivery_intent,
    delivery_intent_receipt_binding,
    materialize_delivery_job,
    validate_delivery_intent,
    validate_file_delivery_profile,
)
from core.memory_v2.canonical import canonical_json_hash
from core.memory_v2.event_store import create_genesis_memory_batch


NOW = "2026-07-14T00:00:00+00:00"
BOOK = "book-delivery-intent"
RUN = "chapter_11_20260714T000000000000Z"
BODY = "b" * 64


class DeliveryIntentTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        path = Path.cwd() / ".tmp" / "test_delivery_intents" / f"{name}_{uuid.uuid4().hex}"
        path.mkdir(parents=True)
        return path

    def _profile(self) -> dict:
        return {
            "schema_version": "1.0",
            "profile_id": "canonical-json-export",
            "root_id": "external:canonical-json-export",
            "root_uuid": "12345678-1234-4123-8123-123456789abc",
            "relative_directory": "canonical-chapters",
            "filename_template": "chapter-{chapter_index}-{run_id}.json",
        }

    def _intent(self, *, batch: dict | None = None) -> dict:
        return build_file_delivery_intent(
            profile=self._profile(),
            book_id=BOOK,
            run_id=RUN,
            chapter_index=11,
            event_batch=batch or self._batch(),
            chapter_body_sha256=BODY,
            policy="required",
            created_at=NOW,
        )

    def _batch(self) -> dict:
        return create_genesis_memory_batch(
            book_id=BOOK,
            title="Delivery contract fixture",
            source_project_digest="a" * 64,
            context_digest="b" * 64,
        )

    def _receipt(self, intent: dict) -> dict:
        digest = "c" * 64
        receipt = {
            "schema_version": "2.1",
            "receipt_id": f"receipt-{RUN}",
            "receipt_path_ref": {"root_id": "runtime", "relative_path": f"receipts/{RUN}.json"},
            "book_id": BOOK,
            "run_id": RUN,
            "context_digest": digest,
            "generation_input_context_digest": "d" * 64,
            "story_project_source_revision_after": {"revision": 12, "digest": "e" * 64},
            "manifest": {"path_ref": {}, "sha256": "f" * 64, "size": 1},
            "marker": {"path_ref": {}, "sha256": "1" * 64, "size": 1},
            "candidate_digest": "2" * 64,
            "artifact_bundle_digest": "3" * 64,
            "final_run": {"target_id": "final", "kind": "final_run_record"},
            "artifacts": [],
            "apply_targets": [],
            "delivery_jobs": [delivery_intent_receipt_binding(intent)],
            "canonical_json_algorithm": "novelagent-canonical-json-v1",
            "published_at": NOW,
        }
        receipt["receipt_hash"] = canonical_json_hash(receipt)
        return receipt

    def test_file_intent_freezes_canonical_batch_body_and_unique_target(self) -> None:
        intent = self._intent()

        self.assertEqual("file", intent["target_type"])
        self.assertEqual(self._batch()["batch_hash"], intent["canonical_payload"]["event_batch_hash"])
        self.assertEqual(BODY, intent["canonical_payload"]["chapter_body_sha256"])
        self.assertIn(RUN, intent["target"]["path_ref"]["relative_path"])
        self.assertIn("000011", intent["target"]["path_ref"]["relative_path"])
        self.assertEqual(intent, validate_delivery_intent(intent))

    def test_profile_is_trusted_relative_file_configuration_only(self) -> None:
        self.assertEqual(self._profile(), validate_file_delivery_profile(self._profile()))

        for field, value in (
            ("root_id", "runtime"),
            ("relative_directory", "C:/exports"),
            ("relative_directory", "../exports"),
            ("filename_template", "static.json"),
            ("filename_template", "{chapter_index}.txt"),
            ("filename_template", "../{run_id}.json"),
        ):
            profile = self._profile()
            profile[field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(DeliveryIntentError):
                    validate_file_delivery_profile(profile)

    def test_intent_rejects_credentials_and_absolute_paths_in_event_batch(self) -> None:
        for batch in (
            {"batch_hash": "a" * 64, "api_key": "hidden", "events": []},
            {"batch_hash": "a" * 64, "evidence_path": "C:/private/chapter.md", "events": []},
            {"batch_hash": "a" * 64, "token": "hidden", "events": []},
        ):
            with self.subTest(batch=batch):
                with self.assertRaises(DeliveryIntentError):
                    self._intent(batch=batch)

    def test_intent_hash_and_batch_binding_detect_tampering(self) -> None:
        intent = self._intent()
        tampered = copy.deepcopy(intent)
        tampered["canonical_payload"]["chapter_body_sha256"] = "9" * 64
        with self.assertRaises(DeliveryIntentError):
            validate_delivery_intent(tampered)

        tampered = copy.deepcopy(intent)
        tampered["canonical_payload"]["event_batch"]["batch_hash"] = "8" * 64
        with self.assertRaisesRegex(DeliveryIntentError, "delivery_intent_event_batch_invalid"):
            validate_delivery_intent(tampered)

    def test_intent_rejects_a_hash_bound_but_structurally_invalid_batch(self) -> None:
        invalid = {"schema_version": "2.2", "batch_hash": "a" * 64, "events": []}
        with self.assertRaisesRegex(DeliveryIntentError, "delivery_intent_event_batch_invalid"):
            self._intent(batch=invalid)

    def test_receipt_materialization_is_idempotent_and_does_not_deliver(self) -> None:
        intent = self._intent()
        receipt = self._receipt(intent)
        queue = DeliveryQueue(
            self._case_dir("queue"),
            clock=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
        )

        first = materialize_delivery_job(intent, publication_receipt=receipt, queue=queue)
        second = materialize_delivery_job(intent, publication_receipt=receipt, queue=queue)

        self.assertEqual(first, second)
        self.assertEqual("pending", first["state"])
        self.assertEqual(0, first["attempt_count"])
        self.assertEqual(intent["job_payload_hash"], first["payload_hash"])
        self.assertEqual([], queue.inspect(first["job_id"])["attempts"])

    def test_materialization_requires_exact_published_binding(self) -> None:
        intent = self._intent()
        receipt = self._receipt(intent)
        receipt["delivery_jobs"] = []
        receipt["receipt_hash"] = canonical_json_hash(receipt, exclude_fields=("receipt_hash",))
        queue = DeliveryQueue(self._case_dir("missing_binding"))

        with self.assertRaisesRegex(DeliveryIntentError, "delivery_intent_not_published"):
            materialize_delivery_job(intent, publication_receipt=receipt, queue=queue)


if __name__ == "__main__":
    unittest.main()
