from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
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
from core.memory_v2 import (
    CURRENT_REDUCER_VERSION,
    apply_genesis_event,
    apply_memory_patch,
    create_genesis_memory_batch,
    create_memory_event_batch,
    create_memory_patch,
)
from core.memory_v2.canonical import canonical_json_hash


NOW = "2026-07-14T00:00:00+00:00"
BOOK = "book-delivery-intent"
RUN = "chapter_11_20260714T000000000000Z"
CHAPTER_BODY = "林雪抵达旧站。"
BODY = hashlib.sha256(CHAPTER_BODY.encode("utf-8")).hexdigest()


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

    def _intent(self, *, batch: dict | None = None, body_hash: str = BODY) -> dict:
        return build_file_delivery_intent(
            profile=self._profile(),
            book_id=BOOK,
            run_id=RUN,
            chapter_index=11,
            event_batch=self._batch() if batch is None else batch,
            chapter_body_sha256=body_hash,
            policy="required",
            created_at=NOW,
        )

    def _batch(self, *, book_id: str = BOOK, body: str = CHAPTER_BODY) -> dict:
        genesis = create_genesis_memory_batch(
            book_id=book_id,
            title="Delivery contract fixture",
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
                    "value": {"label": "chapter 11", "elapsed_minutes": 10, "chapter_index": 11},
                }
            ],
        )
        _, events = apply_memory_patch(
            projection,
            patch,
            reducer_version=CURRENT_REDUCER_VERSION,
            event_context={
                "chapter_body": body,
                "evidence_spans": [{"start_char": 0, "end_char": len(body), "quote": body}],
                "authority_epoch": 1,
            },
        )
        return create_memory_event_batch(
            book_id=book_id,
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
            {"batch_hash": "a" * 64, "token": "hidden", "events": []},
        ):
            with self.subTest(batch=batch):
                with self.assertRaisesRegex(
                    DeliveryIntentError, "delivery_intent_credential_forbidden"
                ):
                    self._intent(batch=batch)

        for batch in (
            {"batch_hash": "a" * 64, "evidence_path": "C:/private/chapter.md", "events": []},
            {"batch_hash": "a" * 64, "evidence_path": r"\\server\private\chapter.md", "events": []},
            {"batch_hash": "a" * 64, "source": "/home/writer/private/chapter.md", "events": []},
            {"batch_hash": "a" * 64, "source": "/50/private/chapter.md", "events": []},
        ):
            with self.subTest(batch=batch):
                with self.assertRaisesRegex(
                    DeliveryIntentError, "delivery_intent_absolute_path_forbidden"
                ):
                    self._intent(batch=batch)

    def test_narrative_slash_prefix_is_not_mistaken_for_an_absolute_path(self) -> None:
        body = "/50号站台的钟声响起，林雪继续向前。"
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

        intent = self._intent(batch=self._batch(body=body), body_hash=body_hash)

        self.assertEqual(body_hash, intent["canonical_payload"]["chapter_body_sha256"])
        self.assertEqual(intent, validate_delivery_intent(intent))

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

    def test_intent_requires_same_book_committed_chapter_and_body_evidence(self) -> None:
        with self.assertRaisesRegex(DeliveryIntentError, "delivery_intent_scope_mismatch"):
            self._intent(batch=self._batch(book_id="another-book"))

        genesis = create_genesis_memory_batch(
            book_id=BOOK,
            title="Not a chapter",
            source_project_digest="a" * 64,
            context_digest="b" * 64,
        )
        with self.assertRaisesRegex(DeliveryIntentError, "delivery_intent_event_batch_invalid"):
            self._intent(batch=genesis)

        with self.assertRaisesRegex(DeliveryIntentError, "delivery_intent_body_hash_mismatch"):
            self._intent(batch=self._batch(body="另一段正文"))

    def test_nested_event_validation_errors_are_normalized(self) -> None:
        tampered = self._batch()
        tampered["events"][0]["after"]["label"] = "forged"
        with self.assertRaisesRegex(DeliveryIntentError, "delivery_intent_event_batch_invalid"):
            self._intent(batch=tampered)

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
