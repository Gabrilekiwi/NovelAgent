from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import core.autonomy as autonomy
import core.stage_control as stage_control
from core.autonomy.contracts import (
    BookRunContractError,
    BookRunPlan,
    BookRunSession,
    ChapterOutline,
    materialize_book_run_session,
    validate_book_run_plan,
    validate_book_run_session,
    validate_chapter_outline,
)
from core.autonomy.outline import build_outline_checkpoint, validate_outline_checkpoint
from core.autonomy.plans import validate_instruction_plan
from core.autonomy.session import AutonomySessionStore
from tests.test_autonomy_plans import (
    instruction_plan,
    source_snapshot,
    trusted_profiles,
    workspace_case,
)


NOW = "2026-07-15T00:00:00+00:00"


def _outline() -> dict:
    return build_outline_checkpoint(
        book_id="book-contract",
        session_id="session-contract",
        plan_id="plan-contract",
        arc_plan_id="arc-contract",
        chapter_index=11,
        planned_target_hash="1" * 64,
        source_snapshot_hash="2" * 64,
        authority_epoch=3,
        authority_head_event_hash="3" * 64,
        outline_input_digest="4" * 64,
        provider_profile="trusted-provider",
        execution_kind="deterministic",
        outline_text="# Chapter 11\n",
        canonical_relative_path="outlines/chapter-11.md",
        canonical_before_sha256=None,
        created_at=NOW,
    )


def _session() -> dict:
    return {
        "schema_version": "1.0",
        "session_id": "session-contract",
        "book_id": "book-contract",
        "plan_id": "plan-contract",
        "plan_hash": "1" * 64,
        "arc_plan_id": "arc-contract",
        "arc_plan_hash": "2" * 64,
        "state": "active",
        "lease_held": True,
        "lease_hash": "3" * 64,
        "event_count": 1,
        "last_event_hash": "4" * 64,
        "requested_chapter_count": 2,
        "trusted_profiles_current": True,
        "completed_count": 1,
        "completed_chapters": [11],
        "canonical_next_chapter": 12,
        "last_completion_receipt_hash": "5" * 64,
        "expected_source_snapshot_hash": "6" * 64,
        "delivery_blocked": False,
        "delivery_blocked_chapters": [],
        "finalization_required": False,
        "root_remap_blocked": True,
    }


def _canonical_bytes(value: dict) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class PublicAutonomyContractCompatibilityTest(unittest.TestCase):
    def test_book_run_plan_is_the_existing_instruction_plan_contract(self) -> None:
        plan = instruction_plan(count=1)
        before = _canonical_bytes(plan)

        public = validate_book_run_plan(plan)

        self.assertEqual(validate_instruction_plan(plan), public)
        self.assertEqual(before, _canonical_bytes(public))
        self.assertEqual("InstructionPlan", _schema_title("instruction_plan.schema.json"))

    def test_chapter_outline_is_the_existing_checkpoint_contract(self) -> None:
        outline = _outline()
        before = _canonical_bytes(outline)

        public = validate_chapter_outline(outline)

        self.assertEqual(validate_outline_checkpoint(outline), public)
        self.assertEqual(before, _canonical_bytes(public))
        self.assertEqual(
            "AutonomyOutlineCheckpoint",
            _schema_title("autonomy_outline_checkpoint.schema.json"),
        )

    def test_markerless_historical_outline_remains_readable(self) -> None:
        legacy = _outline()
        legacy.pop("recovery_protocol")
        from core.autonomy.common import canonical_hash

        legacy["checkpoint_hash"] = canonical_hash(
            legacy, exclude_fields=("checkpoint_hash",)
        )
        self.assertEqual(legacy, validate_chapter_outline(legacy))

    def test_book_run_session_is_a_validated_rebuilt_projection(self) -> None:
        expected = _session()

        class Source:
            def __init__(self) -> None:
                self.calls: list[tuple[str | None, str | None]] = []

            def status(self, session_id=None, *, at=None):
                self.calls.append((session_id, at))
                return copy.deepcopy(expected)

        source = Source()
        materialized = materialize_book_run_session(
            source, "session-contract", at=NOW
        )
        self.assertEqual(expected, materialized)
        self.assertEqual([("session-contract", NOW)], source.calls)

    def test_actual_session_store_status_materializes_as_book_run_session(self) -> None:
        with workspace_case("public_book_run_session") as temporary:
            store = AutonomySessionStore(
                Path(temporary), trusted_profiles=trusted_profiles()
            )
            started = store.execute_plan(
                instruction_plan(count=1),
                source_snapshot_loader=source_snapshot,
                at=NOW,
            )

            self.assertEqual(
                started,
                materialize_book_run_session(store, started["session_id"], at=NOW),
            )

    def test_session_projection_rejects_cross_field_fictions(self) -> None:
        cases = {
            "gap": {"completed_chapters": [10]},
            "missing_receipt": {"last_completion_receipt_hash": None},
            "false_finalization": {"completed_count": 2, "completed_chapters": [10, 11]},
            "unsafe_remap": {"root_remap_blocked": False},
            "blocked_not_completed": {
                "delivery_blocked": True,
                "delivery_blocked_chapters": [12],
            },
        }
        for name, changes in cases.items():
            with self.subTest(name=name):
                candidate = {**_session(), **changes}
                with self.assertRaises(BookRunContractError):
                    validate_book_run_session(candidate)

    def test_public_contract_types_and_readiness_contracts_are_exported(self) -> None:
        self.assertIs(BookRunPlan, autonomy.BookRunPlan)
        self.assertIs(ChapterOutline, autonomy.ChapterOutline)
        self.assertIs(BookRunSession, autonomy.BookRunSession)
        self.assertEqual(
            frozenset({"recovery_protocol"}), ChapterOutline.__optional_keys__
        )
        self.assertIn("validate_outline_readiness", stage_control.__all__)
        self.assertIn("validate_draft_readiness", stage_control.__all__)


def _schema_title(name: str) -> str:
    from core.schema import load_schema

    return str(load_schema(name)["title"])


if __name__ == "__main__":
    unittest.main()
