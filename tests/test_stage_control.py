from __future__ import annotations

import copy
import hashlib
import json
import unittest

from core.memory_v2.canonical import canonical_json_bytes

from core.stage_control import (
    StageControlError,
    assert_receipt_matches_authorization,
    assert_stage_authorized,
    build_stage_authorization,
    build_stage_receipt,
    derive_draft_readiness,
    derive_outline_readiness,
    validate_stage_authorization,
    validate_stage_receipt,
    validate_stage_receipt_chain,
)


BOOK = "book-stage-control"
SESSION = "session-stage-control"
PLAN = "plan-stage-control"
CONTEXT = "1" * 64
HEAD = "2" * 64
OUTLINE = "3" * 64
MODEL_RECEIPT = "4" * 64
NOW = "2026-07-14T00:00:00+00:00"


class StageControlTest(unittest.TestCase):
    def _authorization(
        self,
        *,
        stage: str = "outline",
        previous: str | None = None,
        chapter: int = 11,
    ) -> dict:
        return build_stage_authorization(
            stage=stage,
            book_id=BOOK,
            session_id=SESSION,
            plan_id=PLAN,
            chapter_index=chapter,
            authority_epoch=1,
            authority_head_event_hash=HEAD,
            input_digest=CONTEXT,
            previous_stage_receipt_hash=previous,
            provider_profile="trusted-default",
            max_output_tokens=16_000,
            issued_at=NOW,
        )

    def test_outline_readiness_allows_missing_outline(self) -> None:
        readiness = derive_outline_readiness(
            book_id=BOOK,
            expected_book_id=BOOK,
            requested_chapter=11,
            canonical_next_chapter=11,
            authority_epoch=1,
            authority_head_event_hash=HEAD,
            context_digest=CONTEXT,
            book_lease_held=True,
            required_delivery_allows_progress=True,
            sources_current=True,
            outline_exists=False,
            checked_at=NOW,
        )

        self.assertTrue(readiness["ok"])
        self.assertFalse(readiness["evidence"]["outline_exists"])
        self.assertEqual([], readiness["reasons"])

    def test_outline_readiness_blocks_progress_preconditions(self) -> None:
        readiness = derive_outline_readiness(
            book_id=BOOK,
            expected_book_id="another-book",
            requested_chapter=12,
            canonical_next_chapter=11,
            authority_epoch=1,
            authority_head_event_hash=HEAD,
            context_digest=CONTEXT,
            book_lease_held=False,
            required_delivery_allows_progress=False,
            sources_current=False,
            outline_exists=True,
            checked_at=NOW,
        )

        self.assertFalse(readiness["ok"])
        self.assertEqual(
            [
                "project_identity_mismatch",
                "requested_chapter_not_canonical_next",
                "book_lease_missing",
                "required_delivery_blocked",
                "outline_source_drift",
            ],
            readiness["reasons"],
        )

    def test_stage_authorization_is_hash_bound_and_caps_budget(self) -> None:
        authorization = self._authorization()
        validated = assert_stage_authorized(
            authorization,
            stage="outline",
            book_id=BOOK,
            session_id=SESSION,
            plan_id=PLAN,
            chapter_index=11,
            authority_epoch=1,
            authority_head_event_hash=HEAD,
            input_digest=CONTEXT,
            previous_stage_receipt_hash=None,
            provider_profile="trusted-default",
            requested_max_output_tokens=8_000,
        )
        self.assertEqual(authorization["authorization_hash"], validated["authorization_hash"])

        with self.assertRaisesRegex(StageControlError, "stage_authorization_budget_escalation"):
            assert_stage_authorized(
                authorization,
                stage="outline",
                book_id=BOOK,
                session_id=SESSION,
                plan_id=PLAN,
                chapter_index=11,
                authority_epoch=1,
                authority_head_event_hash=HEAD,
                input_digest=CONTEXT,
                previous_stage_receipt_hash=None,
                provider_profile="trusted-default",
                requested_max_output_tokens=16_001,
            )

        tampered = copy.deepcopy(authorization)
        tampered["provider_profile"] = "untrusted"
        with self.assertRaisesRegex(StageControlError, "stage_authorization_hash_mismatch"):
            validate_stage_authorization(tampered)

    def test_stage_authorization_fails_on_authority_or_input_drift(self) -> None:
        authorization = self._authorization()
        common = {
            "stage": "outline",
            "book_id": BOOK,
            "session_id": SESSION,
            "plan_id": PLAN,
            "chapter_index": 11,
            "authority_epoch": 1,
            "authority_head_event_hash": HEAD,
            "input_digest": CONTEXT,
            "previous_stage_receipt_hash": None,
            "provider_profile": "trusted-default",
            "requested_max_output_tokens": 8_000,
        }
        for field, value in (
            ("authority_head_event_hash", "5" * 64),
            ("input_digest", "6" * 64),
            ("plan_id", "another-plan"),
        ):
            changed = dict(common)
            changed[field] = value
            with self.subTest(field=field):
                with self.assertRaisesRegex(StageControlError, "stage_authorization_drift"):
                    assert_stage_authorized(authorization, **changed)

    def test_stage_receipt_is_immutable_and_bound_to_authorization(self) -> None:
        authorization = self._authorization()
        receipt = build_stage_receipt(
            authorization,
            status="succeeded",
            output_digest=OUTLINE,
            model_call_receipt_hash=MODEL_RECEIPT,
            created_at=NOW,
        )
        assert_receipt_matches_authorization(receipt, authorization)

        tampered = copy.deepcopy(receipt)
        tampered["output_digest"] = "7" * 64
        with self.assertRaisesRegex(StageControlError, "stage_receipt_hash_mismatch"):
            validate_stage_receipt(tampered)

        with self.assertRaisesRegex(StageControlError, "stage_receipt_failed_output_present"):
            build_stage_receipt(
                authorization,
                status="provider_call_uncertain",
                output_digest=OUTLINE,
                model_call_receipt_hash=None,
                created_at=NOW,
            )

    def test_draft_readiness_requires_same_outline_input_and_authority(self) -> None:
        receipt = build_stage_receipt(
            self._authorization(),
            status="succeeded",
            output_digest=OUTLINE,
            model_call_receipt_hash=MODEL_RECEIPT,
            created_at=NOW,
        )
        ready = derive_draft_readiness(
            outline_stage_receipt=receipt,
            book_id=BOOK,
            session_id=SESSION,
            plan_id=PLAN,
            chapter_index=11,
            authority_epoch=1,
            authority_head_event_hash=HEAD,
            current_outline_input_digest=CONTEXT,
            current_outline_hash=OUTLINE,
            checked_at=NOW,
        )
        self.assertTrue(ready["ok"])

        stale = derive_draft_readiness(
            outline_stage_receipt=receipt,
            book_id=BOOK,
            session_id=SESSION,
            plan_id=PLAN,
            chapter_index=11,
            authority_epoch=2,
            authority_head_event_hash="8" * 64,
            current_outline_input_digest="9" * 64,
            current_outline_hash="a" * 64,
            checked_at=NOW,
        )
        self.assertFalse(stale["ok"])
        self.assertIn("outline_authority_stale", stale["reasons"])
        self.assertIn("outline_input_drift", stale["reasons"])
        self.assertIn("outline_hash_drift", stale["reasons"])

    def test_stage_receipt_chain_rejects_broken_links_and_chapter_changes(self) -> None:
        first = build_stage_receipt(
            self._authorization(),
            status="succeeded",
            output_digest=OUTLINE,
            model_call_receipt_hash=MODEL_RECEIPT,
            created_at=NOW,
        )
        second = build_stage_receipt(
            self._authorization(stage="draft", previous=first["receipt_hash"]),
            status="succeeded",
            output_digest="b" * 64,
            model_call_receipt_hash="c" * 64,
            created_at=NOW,
        )
        self.assertEqual(2, len(validate_stage_receipt_chain([first, second])))

        broken = copy.deepcopy(second)
        broken["previous_stage_receipt_hash"] = None
        broken["receipt_hash"] = "d" * 64
        with self.assertRaises(StageControlError):
            validate_stage_receipt_chain([first, broken])

        next_chapter = build_stage_receipt(
            self._authorization(stage="outline", previous=first["receipt_hash"], chapter=12),
            status="succeeded",
            output_digest="e" * 64,
            model_call_receipt_hash="f" * 64,
            created_at=NOW,
        )
        with self.assertRaisesRegex(StageControlError, "stage_receipt_chapter_changed"):
            validate_stage_receipt_chain([first, next_chapter])

    def test_legacy_v1_golden_bytes_and_hashes_remain_unchanged(self) -> None:
        authorization_bytes = (
            b'{"authority":{"epoch":3,"head_event_hash":"2222222222222222222222222222222222222222222222222222222222222222"},'
            b'"authorization_hash":"21a2f93992b20e7cf0874d7e7ea75b9bcd14b972e9dca15f339266eae2d28e97",'
            b'"book_id":"book-golden","chapter_index":7,"input_digest":"1111111111111111111111111111111111111111111111111111111111111111",'
            b'"issued_at":"2026-01-02T03:04:05+00:00","max_output_tokens":4096,"plan_id":"plan-golden",'
            b'"previous_stage_receipt_hash":null,"provider_profile":"golden-provider","schema_version":"1.0",'
            b'"session_id":"session-golden","stage":"draft"}'
        )
        receipt_bytes = (
            b'{"authority":{"epoch":3,"head_event_hash":"2222222222222222222222222222222222222222222222222222222222222222"},'
            b'"authorization_hash":"21a2f93992b20e7cf0874d7e7ea75b9bcd14b972e9dca15f339266eae2d28e97",'
            b'"book_id":"book-golden","chapter_index":7,"created_at":"2026-01-02T03:05:06+00:00",'
            b'"input_digest":"1111111111111111111111111111111111111111111111111111111111111111",'
            b'"model_call_receipt_hash":"4444444444444444444444444444444444444444444444444444444444444444",'
            b'"output_digest":"3333333333333333333333333333333333333333333333333333333333333333",'
            b'"plan_id":"plan-golden","previous_stage_receipt_hash":null,'
            b'"receipt_hash":"c024eeeb462eaf813f2837f074f08476d96e4ec7c695bda80a2b746158547bb0",'
            b'"schema_version":"1.0","session_id":"session-golden","stage":"draft","status":"succeeded"}'
        )
        authorization = validate_stage_authorization(json.loads(authorization_bytes))
        receipt = validate_stage_receipt(json.loads(receipt_bytes))
        self.assertEqual(
            authorization_bytes,
            canonical_json_bytes(authorization, exclude_environment_fields=False),
        )
        self.assertEqual(
            receipt_bytes,
            canonical_json_bytes(receipt, exclude_environment_fields=False),
        )
        self.assertEqual(
            "3b0553b4b8e6980c7a9ab66de948252ac50f077d23373c8ddab2af11c365c6b7",
            hashlib.sha256(authorization_bytes).hexdigest(),
        )
        self.assertEqual(
            "f82ccd9b1b611aa6e325b7ef0e842c25ccd74c2b174a6bd0fcd66655bc2f08ea",
            hashlib.sha256(receipt_bytes).hexdigest(),
        )

        malformed_v11 = dict(authorization)
        malformed_v11["schema_version"] = "1.1"
        with self.assertRaisesRegex(
            StageControlError, "stage_authorization_version_invalid"
        ):
            validate_stage_authorization(malformed_v11)


if __name__ == "__main__":
    unittest.main()
