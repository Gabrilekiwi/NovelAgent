from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from core.autonomy.common import canonical_hash
from core.autonomy.receipts import (
    AutonomyReceiptError,
    CompletionLedger,
    StageReceiptStore,
)
from core.stage_control import build_stage_authorization, build_stage_receipt
from tests.test_autonomy_plans import instruction_plan, source_snapshot, workspace_case


NOW = "2026-07-14T00:00:00+00:00"


def stage_pair(
    *,
    chapter: int,
    status: str = "succeeded",
    previous: str | None = None,
    stage: str = "outline",
    session_id: str = "session-receipts",
    plan_id: str | None = None,
) -> tuple[dict, dict]:
    resolved_plan_id = plan_id or instruction_plan()["plan_id"]
    authorization = build_stage_authorization(
        stage=stage,
        book_id="book-autonomy",
        session_id=session_id,
        plan_id=resolved_plan_id,
        chapter_index=chapter,
        authority_epoch=2,
        authority_head_event_hash="2" * 64,
        input_digest="3" * 64,
        previous_stage_receipt_hash=previous,
        provider_profile="balanced",
        max_output_tokens=16000,
        issued_at=NOW,
    )
    receipt = build_stage_receipt(
        authorization,
        status=status,
        output_digest="4" * 64 if status == "succeeded" else None,
        model_call_receipt_hash="5" * 64 if status == "succeeded" else None,
        created_at=NOW,
    )
    return authorization, receipt


def append_successful_stage_chain(
    store: StageReceiptStore,
    *,
    lease_hash: str,
    chapter: int,
    session_id: str = "session-receipts",
    plan_id: str | None = None,
) -> dict:
    previous = None
    final = None
    for stage in ("outline", "scene_plan", "draft", "polish", "validator"):
        authorization, receipt = stage_pair(
            chapter=chapter,
            stage=stage,
            previous=previous,
            session_id=session_id,
            plan_id=plan_id,
        )
        store.append(
            receipt,
            authorization=authorization,
            expected_lease_hash=lease_hash,
            at=NOW,
        )
        previous = receipt["receipt_hash"]
        final = receipt
    assert final is not None
    return final


def publication(chapter: int) -> dict:
    payload = {
        "schema_version": "test-verified",
        "book_id": "book-autonomy",
        "final_run": {"chapter_index": chapter},
    }
    payload["receipt_hash"] = canonical_hash(payload, exclude_fields=("receipt_hash",))
    return payload


def verify_publication(value: dict) -> dict:
    expected = canonical_hash(value, exclude_fields=("receipt_hash",))
    if value.get("receipt_hash") != expected:
        raise ValueError("publication hash mismatch")
    return value


class DurableReceiptTest(unittest.TestCase):
    def test_stage_receipts_are_append_only_chained_and_replay_safe(self) -> None:
        with workspace_case("stage_receipts") as temporary:
            root = Path(temporary)
            store = StageReceiptStore(root)
            lease = store.leases.acquire(
                book_id="book-autonomy",
                session_id="session-receipts",
                plan_id=instruction_plan()["plan_id"],
                ttl_seconds=3600,
                at=NOW,
            )
            first_authorization, first = stage_pair(chapter=11, stage="outline")
            self.assertEqual(
                first,
                store.append(
                    first,
                    authorization=first_authorization,
                    expected_lease_hash=lease["lease_hash"],
                    at=NOW,
                ),
            )
            self.assertEqual(
                first,
                store.append(
                    first,
                    authorization=first_authorization,
                    expected_lease_hash=lease["lease_hash"],
                    at=NOW,
                ),
            )
            previous = first["receipt_hash"]
            for stage in ("scene_plan", "draft", "polish", "validator"):
                authorization, receipt = stage_pair(
                    chapter=11, stage=stage, previous=previous
                )
                store.append(
                    receipt,
                    authorization=authorization,
                    expected_lease_hash=lease["lease_hash"],
                    at=NOW,
                )
                previous = receipt["receipt_hash"]
            self.assertEqual(5, len(store.load_chain("session-receipts", 11)))

            receipt_file = sorted(root.rglob("0005-*.json"))[0]
            tampered = json.loads(receipt_file.read_text(encoding="utf-8"))
            tampered["status"] = "failed"
            receipt_file.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaises(Exception):
                store.load_chain("session-receipts", 11)

    def test_stage_sequence_and_lease_takeover_fail_closed(self) -> None:
        with workspace_case("stage_fence") as temporary:
            root = Path(temporary)
            plan = instruction_plan()
            store = StageReceiptStore(root)
            lease = store.leases.acquire(
                book_id="book-autonomy",
                session_id="session-receipts",
                plan_id=plan["plan_id"],
                ttl_seconds=3600,
                at=NOW,
            )
            authorization, uncertain = stage_pair(
                chapter=11, stage="outline", status="provider_call_uncertain"
            )
            store.append(
                uncertain,
                authorization=authorization,
                expected_lease_hash=lease["lease_hash"],
                at=NOW,
            )
            next_authorization, next_receipt = stage_pair(
                chapter=11,
                stage="scene_plan",
                previous=uncertain["receipt_hash"],
            )
            with self.assertRaisesRegex(
                AutonomyReceiptError, "stage_receipt_terminal_failure"
            ):
                store.append(
                    next_receipt,
                    authorization=next_authorization,
                    expected_lease_hash=lease["lease_hash"],
                    at=NOW,
                )

        with workspace_case("stage_takeover") as temporary:
            root = Path(temporary)
            plan = instruction_plan()
            store = StageReceiptStore(root)
            lease = store.leases.acquire(
                book_id="book-autonomy",
                session_id="session-receipts",
                plan_id=plan["plan_id"],
                ttl_seconds=3600,
                at=NOW,
            )
            final = append_successful_stage_chain(
                store, lease_hash=lease["lease_hash"], chapter=11
            )
            store.leases.release(
                book_id="book-autonomy",
                session_id="session-receipts",
                plan_id=plan["plan_id"],
                expected_lease_hash=lease["lease_hash"],
                at="2026-07-14T00:01:00+00:00",
            )
            takeover = store.leases.acquire(
                book_id="book-autonomy",
                session_id="session-takeover",
                plan_id="plan-takeover",
                ttl_seconds=3600,
                at="2026-07-14T00:01:00+00:00",
            )
            store.leases.release(
                book_id="book-autonomy",
                session_id="session-takeover",
                plan_id="plan-takeover",
                expected_lease_hash=takeover["lease_hash"],
                at="2026-07-14T00:02:00+00:00",
            )
            store.leases.acquire(
                book_id="book-autonomy",
                session_id="session-receipts",
                plan_id=plan["plan_id"],
                ttl_seconds=3600,
                at="2026-07-14T00:02:00+00:00",
            )
            ledger = CompletionLedger(
                root,
                instruction_plan=plan,
                session_id="session-receipts",
                arc_plan_id="arc-receipts",
                stage_receipts=store,
                publication_verifier=verify_publication,
            )
            with self.assertRaisesRegex(
                Exception, "book_lease_fence_owner_discontinuity"
            ):
                ledger.append(
                    final_stage_receipt=final,
                    publication_receipt=publication(11),
                    planned_target_hash="6" * 64,
                    chapter_body_hash="7" * 64,
                    source_snapshot_after=source_snapshot(chapter=12, digest="a" * 64),
                    created_at="2026-07-14T00:02:00+00:00",
                )

    def test_completion_requires_configured_durable_publication_roots(self) -> None:
        with workspace_case("publication_roots_required") as temporary:
            root = Path(temporary)
            plan = instruction_plan()
            stages = StageReceiptStore(root)
            lease = stages.leases.acquire(
                book_id="book-autonomy",
                session_id="session-receipts",
                plan_id=plan["plan_id"],
                ttl_seconds=3600,
                at=NOW,
            )
            final = append_successful_stage_chain(
                stages, lease_hash=lease["lease_hash"], chapter=11
            )
            ledger = CompletionLedger(
                root,
                instruction_plan=plan,
                session_id="session-receipts",
                arc_plan_id="arc-receipts",
                stage_receipts=stages,
            )
            with self.assertRaisesRegex(
                AutonomyReceiptError, "chapter_completion_publication_roots_required"
            ):
                ledger.append(
                    final_stage_receipt=final,
                    publication_receipt=publication(11),
                    planned_target_hash="6" * 64,
                    chapter_body_hash="7" * 64,
                    source_snapshot_after=source_snapshot(chapter=12, digest="a" * 64),
                    created_at=NOW,
                )

    def test_completion_count_rebuilds_only_verified_contiguous_receipts(self) -> None:
        with workspace_case("completion_failed") as temporary:
            root = Path(temporary)
            plan = instruction_plan(count=3)
            stages = StageReceiptStore(root)
            lease = stages.leases.acquire(
                book_id="book-autonomy",
                session_id="session-receipts",
                plan_id=plan["plan_id"],
                ttl_seconds=3600,
                at=NOW,
            )
            ledger = CompletionLedger(
                root,
                instruction_plan=plan,
                session_id="session-receipts",
                arc_plan_id="arc-receipts",
                stage_receipts=stages,
                publication_verifier=verify_publication,
            )
            failed_authorization, failed = stage_pair(chapter=11, status="failed")
            stages.append(
                failed,
                authorization=failed_authorization,
                expected_lease_hash=lease["lease_hash"],
                at=NOW,
            )
            with self.assertRaisesRegex(
                AutonomyReceiptError, "chapter_completion_stage_failed"
            ):
                ledger.append(
                    final_stage_receipt=failed,
                    publication_receipt=publication(11),
                    planned_target_hash="6" * 64,
                    chapter_body_hash="7" * 64,
                    source_snapshot_after=source_snapshot(chapter=12, digest="a" * 64),
                )
            self.assertEqual(0, ledger.summary()["completed_count"])

        # Use a clean root because a failed StageReceipt is terminal for that
        # chapter's model-stage chain and must never be rewritten as success.
        with workspace_case("completion_success") as temporary:
            root = Path(temporary)
            stages = StageReceiptStore(root)
            lease = stages.leases.acquire(
                book_id="book-autonomy",
                session_id="session-receipts",
                plan_id=plan["plan_id"],
                ttl_seconds=3600,
                at=NOW,
            )
            ledger = CompletionLedger(
                root,
                instruction_plan=plan,
                session_id="session-receipts",
                arc_plan_id="arc-receipts",
                stage_receipts=stages,
                publication_verifier=verify_publication,
            )
            success = append_successful_stage_chain(
                stages, lease_hash=lease["lease_hash"], chapter=11
            )
            first = ledger.append(
                final_stage_receipt=success,
                publication_receipt=publication(11),
                planned_target_hash="6" * 64,
                chapter_body_hash="7" * 64,
                source_snapshot_after=source_snapshot(chapter=12, digest="a" * 64),
                status="local_committed_delivery_blocked",
                created_at=NOW,
            )
            replay = ledger.append(
                final_stage_receipt=success,
                publication_receipt=publication(11),
                planned_target_hash="6" * 64,
                chapter_body_hash="7" * 64,
                source_snapshot_after=source_snapshot(chapter=12, digest="a" * 64),
                status="local_committed_delivery_blocked",
                created_at=NOW,
            )
            self.assertEqual(first, replay)
            self.assertEqual(
                {
                    "completed_count": 1,
                    "completed_chapters": [11],
                    "canonical_next_chapter": 12,
                    "last_completion_receipt_hash": first["receipt_hash"],
                    "expected_source_snapshot_hash": source_snapshot(
                        chapter=12, digest="a" * 64
                    )["snapshot_hash"],
                    "delivery_blocked": True,
                    "delivery_blocked_chapters": [11],
                },
                ledger.summary(),
            )

            skipped = append_successful_stage_chain(
                stages, lease_hash=lease["lease_hash"], chapter=13
            )
            with self.assertRaisesRegex(
                AutonomyReceiptError, "chapter_completion_not_canonical_next"
            ):
                ledger.append(
                    final_stage_receipt=skipped,
                    publication_receipt=publication(13),
                    planned_target_hash="8" * 64,
                    chapter_body_hash="9" * 64,
                    source_snapshot_after=source_snapshot(chapter=14, digest="b" * 64),
                    created_at=NOW,
                )

            publication_file = next((root / "completion_ledgers").rglob("publications/*.json"))
            tampered = json.loads(publication_file.read_text(encoding="utf-8"))
            tampered["book_id"] = "another-book"
            publication_file.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(
                AutonomyReceiptError, "chapter_completion_publication_invalid"
            ):
                ledger.rebuild()


if __name__ == "__main__":
    unittest.main()
