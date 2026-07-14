from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
import unittest
import uuid

from api.contracts import ModelResponse
from core.engine.persistence import atomic_write_json
from core.model_calls import (
    PROVIDER_CALL_UNCERTAIN,
    ModelCallConflictError,
    ModelCallEvidenceError,
    ModelCallIntegrityError,
    ModelCallIntent,
    ModelCallSafetyError,
    ModelCallStore,
    build_model_call_intent,
    build_model_call_receipt,
    canonical_model_request_digest,
    load_model_call_intent,
    load_model_call_receipt,
    model_response_artifact_hash,
)
from core.schema import validate_schema


class ModelCallEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (
            Path.cwd()
            / ".tmp"
            / "test_model_calls"
            / f"case_{uuid.uuid4().hex}"
        )
        self.store = ModelCallStore(self.root)
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def _intent(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "call_id": "chapter-12-draft",
            "attempt_id": "chapter-12-draft-a1",
            "provider": "openai",
            "model": "gpt-test",
            "stage": "chapter_draft",
            "budget_reservation": {
                "reserved_input_tokens": 1200,
                "reserved_output_tokens": 4000,
            },
            "request": {
                "messages": [
                    {"role": "user", "content": "the full prompt must never persist"}
                ],
                "local_source": "C:\\Users\\writer\\novel.md",
            },
            "created_at": self.now,
        }
        values.update(overrides)
        return build_model_call_intent(**values)

    def _response(self, text: str = "模型正文") -> ModelResponse:
        return ModelResponse(
            text=text,
            usage={"input_tokens": 111, "output_tokens": 22, "total_tokens": 133},
            finish_reason="stop",
            request_id="req-123",
            actual_model="gpt-test-2026-01-01",
            endpoint_type="official",
        )

    def _receipt(self, intent: dict[str, object], **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "response": self._response(),
            "response_artifact_ref": "responses/chapter-12-draft-a1.txt",
            "status": "succeeded",
            "received_at": self.now,
        }
        values.update(overrides)
        return build_model_call_receipt(intent, **values)

    def test_model_response_is_structured_and_matches_schema(self) -> None:
        response = self._response()

        self.assertEqual("模型正文", response.text)
        self.assertEqual(133, response.usage["total_tokens"])
        self.assertEqual("official", response.endpoint_type)
        payload = response.to_dict()
        self.assertIs(payload, validate_schema(payload, "model_response.schema.json"))

    def test_intent_persists_only_digest_and_safe_metadata(self) -> None:
        intent = self._intent()
        serialized = json.dumps(intent, ensure_ascii=False).lower()

        self.assertEqual(
            canonical_model_request_digest(
                {
                    "messages": [
                        {"role": "user", "content": "the full prompt must never persist"}
                    ],
                    "local_source": "C:\\Users\\writer\\novel.md",
                }
            ),
            intent["request_digest"],
        )
        self.assertNotIn("full prompt", serialized)
        self.assertNotIn("users\\\\writer", serialized)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("messages", intent)
        self.assertIs(intent, validate_schema(intent, "model_call_intent.schema.json"))

    def test_contract_objects_are_frozen_and_hash_verified(self) -> None:
        record = ModelCallIntent.from_dict(self._intent())

        with self.assertRaises(FrozenInstanceError):
            record.provider = "changed"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            record.budget_reservation["reserved_output_tokens"] = 1  # type: ignore[index]

    def test_safety_rejects_credentials_payload_fields_and_absolute_refs(self) -> None:
        with self.assertRaises(ModelCallSafetyError):
            self._intent(budget_reservation={"api_key": 1})
        with self.assertRaises(ModelCallSafetyError):
            self._intent(model="C:\\models\\local")

        intent = self._intent()
        with self.assertRaises(ModelCallSafetyError):
            self._receipt(intent, response_artifact_ref="C:\\runtime\\response.txt")
        with self.assertRaises(ModelCallSafetyError):
            self._receipt(intent, usage={"authorization": "Bearer secret"})

    def test_receipt_binds_response_hash_and_omits_response_text(self) -> None:
        intent = self._intent()
        response = self._response("不可写入回执的正文")
        receipt = self._receipt(intent, response=response)
        serialized = json.dumps(receipt, ensure_ascii=False)

        self.assertEqual(model_response_artifact_hash(response), receipt["response_artifact_hash"])
        self.assertEqual(intent["intent_hash"], receipt["intent_hash"])
        self.assertNotIn(response.text, serialized)
        self.assertEqual("req-123", receipt["request_id"])
        self.assertIs(receipt, validate_schema(receipt, "model_call_receipt.schema.json"))

    def test_tampering_is_detected_for_intent_and_receipt(self) -> None:
        intent = self.store.record_intent(self._intent())
        receipt = self.store.record_receipt(self._receipt(intent))

        tampered_intent = dict(intent)
        tampered_intent["model"] = "tampered-model"
        atomic_write_json(self.store.intent_path(intent["attempt_id"]), tampered_intent)
        with self.assertRaisesRegex(ModelCallIntegrityError, "hash mismatch"):
            load_model_call_intent(self.store.intent_path(intent["attempt_id"]))

        # Restore the valid intent, then independently alter receipt metadata.
        atomic_write_json(self.store.intent_path(intent["attempt_id"]), intent)
        tampered_receipt = dict(receipt)
        tampered_receipt["request_id"] = "forged-request"
        atomic_write_json(self.store.receipt_path(intent["attempt_id"]), tampered_receipt)
        with self.assertRaisesRegex(ModelCallIntegrityError, "hash mismatch"):
            load_model_call_receipt(self.store.receipt_path(intent["attempt_id"]))

    def test_intent_without_receipt_is_provider_call_uncertain(self) -> None:
        intent = self.store.record_intent(self._intent())

        uncertain = self.store.list_uncertain_calls()

        self.assertEqual(1, len(uncertain))
        self.assertEqual(PROVIDER_CALL_UNCERTAIN, uncertain[0]["status"])
        self.assertEqual(intent["attempt_id"], uncertain[0]["attempt_id"])
        self.store.record_receipt(self._receipt(intent))
        self.assertEqual([], self.store.list_uncertain_calls())

    def test_identical_records_are_idempotent_but_existing_files_never_overwrite(self) -> None:
        intent = self._intent()
        self.assertEqual(intent, self.store.record_intent(intent))
        self.assertEqual(intent, self.store.record_intent(intent))

        conflicting_intent = self._intent(provider="anthropic")
        with self.assertRaises(ModelCallConflictError):
            self.store.record_intent(conflicting_intent)
        self.assertEqual(intent, self.store.load_intent(intent["attempt_id"]))

        receipt = self._receipt(intent)
        self.assertEqual(receipt, self.store.record_receipt(receipt))
        self.assertEqual(receipt, self.store.record_receipt(receipt))
        conflicting_receipt = self._receipt(intent, response=self._response("different response"))
        with self.assertRaises(ModelCallConflictError):
            self.store.record_receipt(conflicting_receipt)
        self.assertEqual(receipt, self.store.load_receipt(intent["attempt_id"]))

    def test_receipt_cannot_be_persisted_before_or_against_its_intent(self) -> None:
        intent = self._intent()
        receipt = self._receipt(intent)
        with self.assertRaises(ModelCallEvidenceError):
            self.store.record_receipt(receipt)

        self.store.record_intent(intent)
        another = self._intent(
            call_id="another-call",
            attempt_id="another-attempt",
        )
        mismatched = self._receipt(another)
        mismatched["attempt_id"] = intent["attempt_id"]
        # Re-hashing a forged receipt still cannot defeat the intent binding.
        from core.memory_v2.canonical import canonical_json_hash

        mismatched["receipt_hash"] = canonical_json_hash(
            mismatched,
            exclude_fields=("receipt_hash",),
            exclude_environment_fields=False,
        )
        with self.assertRaisesRegex(ModelCallIntegrityError, "call_id"):
            self.store.record_receipt(mismatched)


if __name__ == "__main__":
    unittest.main()
