from __future__ import annotations

import unittest

from core.memory_v2 import (
    MemoryReducerError,
    apply_memory_events,
    apply_memory_patch,
    create_empty_typed_canonical_memory,
    create_memory_patch,
)
from core.engine.executor import _attach_authoritative_scene_delta
from core.memory_v2.runtime import _chapter_operations
from core.state.authoritative import (
    empty_authoritative_state,
    validate_authoritative_state_delta,
)


def _codes(report: dict) -> set[str]:
    return {
        str(item.get("code"))
        for item in report.get("findings") or []
        if isinstance(item, dict)
    }


def _context() -> dict:
    return {
        "chapter_body": "evidence",
        "evidence_spans": [{"start_char": 0, "end_char": 1, "quote": "e"}],
        "authority_epoch": 1,
    }


class AuthoritativeStateTests(unittest.TestCase):
    def test_valid_changes_advance_all_ledgers(self) -> None:
        base = empty_authoritative_state()
        report = validate_authoritative_state_delta(
            base_state=base,
            chapter_text="",
            state_delta={
                "source_tier": "chapter_event",
                "character_changes": [
                    {
                        "character_id": "char_qian_ming",
                        "canonical_name": "钱明",
                        "aliases": ["王主管"],
                        "identity": "商场主管",
                    }
                ],
                "relationship_changes": [
                    {
                        "relationship_id": "rel_qian_spouse",
                        "source_character_id": "char_qian_ming",
                        "target_character_id": "char_spouse",
                        "type": "spouse",
                        "status": "active",
                    }
                ],
                "roster_changes": [
                    {
                        "roster_id": "main_group",
                        "operation": "join",
                        "member_ids": ["member_001"],
                        "members": [{"member_id": "member_001", "descriptor": "男孩"}],
                        "delta": 1,
                        "declared_count": 1,
                    }
                ],
                "numeric_changes": [
                    {
                        "counter_id": "erosion",
                        "previous_value": 0,
                        "delta": 2,
                        "expected_value": 2,
                        "declared_value": 2,
                        "rule": "monotonic_non_decreasing",
                    }
                ],
                "inventory_changes": [
                    {
                        "owner_id": "char_qian_ming",
                        "item_id": "medicine",
                        "previous_quantity": 0,
                        "delta": 1,
                        "declared_quantity": 1,
                    }
                ],
                "location_changes": [
                    {
                        "entity_id": "char_qian_ming",
                        "before": "mall",
                        "after": "corridor",
                    }
                ],
                "events": [
                    {
                        "event_id": "chapter-0001-event-001",
                        "type": "rescue_completed",
                        "subjects": ["char_qian_ming"],
                        "objects": ["main_group"],
                        "location": "corridor",
                        "status": "completed",
                    }
                ],
            },
        )

        self.assertTrue(report["accepted"])
        state = report["state_after"]
        self.assertEqual("char_qian_ming", state["characters"]["char_qian_ming"]["character_id"])
        self.assertEqual(1, state["roster"]["main_group"]["computed_count"])
        self.assertEqual(2, state["numeric_counters"]["erosion"]["current_value"])
        self.assertEqual(1, state["inventory"]["char_qian_ming:medicine"]["quantity"])
        self.assertEqual("corridor", state["locations"]["char_qian_ming"]["location_id"])

    def test_alias_cannot_create_a_second_character(self) -> None:
        base = empty_authoritative_state()
        base["characters"]["char_qian_ming"] = {
            "character_id": "char_qian_ming",
            "canonical_name": "钱明",
            "aliases": ["王主管"],
            "identity": "商场主管",
        }

        report = validate_authoritative_state_delta(
            base_state=base,
            chapter_text="",
            state_delta={
                "character_changes": [
                    {
                        "character_id": "char_manager_new",
                        "canonical_name": "王主管",
                        "aliases": [],
                    }
                ]
            },
        )

        self.assertFalse(report["accepted"])
        self.assertIn("character_identity_drift", _codes(report))

    def test_model_inference_cannot_override_event_authority(self) -> None:
        base = empty_authoritative_state()
        base["characters"]["char_qian_ming"] = {
            "character_id": "char_qian_ming",
            "canonical_name": "钱明",
            "aliases": ["王主管"],
            "status": "alive",
            "source_tier": "chapter_event",
        }

        report = validate_authoritative_state_delta(
            base_state=base,
            chapter_text="",
            state_delta={
                "source_tier": "model_inference",
                "character_changes": [
                    {
                        "character_id": "char_qian_ming",
                        "canonical_name": "钱明",
                        "status": "dead",
                    }
                ],
            },
        )

        self.assertFalse(report["accepted"])
        self.assertIn("source_precedence_conflict", _codes(report))

    def test_roster_leave_uses_stable_ids_without_redeclaring_member_records(self) -> None:
        base = empty_authoritative_state()
        base["roster"]["main_group"] = {
            "roster_id": "main_group",
            "members": [
                {"member_id": "member_001", "descriptor": "男孩"},
                {"member_id": "member_002", "descriptor": "女孩"},
            ],
            "declared_count": 2,
            "computed_count": 2,
        }

        report = validate_authoritative_state_delta(
            base_state=base,
            chapter_text="",
            state_delta={
                "roster_changes": [
                    {
                        "roster_id": "main_group",
                        "operation": "leave",
                        "member_ids": ["member_002"],
                        "members": [],
                        "delta": -1,
                        "declared_count": 1,
                    }
                ]
            },
        )

        self.assertTrue(report["accepted"])
        self.assertEqual(1, report["state_after"]["roster"]["main_group"]["computed_count"])

    def test_scene_deltas_replace_prose_inference_in_memory_operations(self) -> None:
        analysis = {
            "summary": "chapter",
            "events": [],
            "character_changes": [{"name": "inferred duplicate"}],
            "world_changes": [],
            "new_locations": ["inferred place"],
        }
        baseline = empty_authoritative_state()
        pipeline = {
            "scene_drafts": [
                {
                    "index": 1,
                    "events": [
                        {
                            "event_id": "event-1",
                            "type": "arrival",
                            "subjects": ["char_1"],
                            "objects": [],
                            "location": "loc_1",
                            "status": "completed",
                        }
                    ],
                    "deltas": {
                        "characters": [],
                        "relationships": [],
                        "rosters": [],
                        "locations": [
                            {
                                "entity_id": "char_1",
                                "before": "loc_0",
                                "after": "loc_1",
                            }
                        ],
                        "inventory": [],
                        "counters": [],
                    },
                }
            ]
        }

        attached = _attach_authoritative_scene_delta(
            analysis,
            pipeline,
            {"authoritative_state": baseline},
        )
        operations = _chapter_operations(1, attached)

        self.assertEqual("update_authoritative_state", operations[0]["op"])
        self.assertEqual(baseline, operations[0]["value"]["baseline_state"])
        self.assertFalse(any(item["op"] == "upsert_character" for item in operations))
        self.assertFalse(any(item["op"] == "upsert_location" for item in operations))

    def test_memory_event_is_the_replayable_authority(self) -> None:
        memory = create_empty_typed_canonical_memory(book_id="book-authority")
        delta = {
            "source_tier": "chapter_event",
            "character_changes": [
                {
                    "character_id": "char_qian_ming",
                    "canonical_name": "钱明",
                    "aliases": ["王主管"],
                }
            ],
            "relationship_changes": [],
            "roster_changes": [],
            "numeric_changes": [],
            "inventory_changes": [],
            "location_changes": [],
            "events": [],
        }
        patch = create_memory_patch(
            patch_id="patch-authority-1",
            source_kind="chapter",
            operations=[{"op": "update_authoritative_state", "value": delta}],
        )

        updated, events = apply_memory_patch(memory, patch, event_context=_context())
        replayed = apply_memory_events(
            memory,
            events,
            reducer_version="memory-reducer-2.2",
        )

        self.assertEqual(updated, replayed)
        self.assertEqual("authoritative_state", events[0]["field"])
        self.assertEqual(
            "钱明",
            replayed["authoritative_state"]["characters"]["char_qian_ming"]["canonical_name"],
        )

    def test_memory_reducer_hard_fails_on_numeric_rollback(self) -> None:
        memory = create_empty_typed_canonical_memory()
        memory["authoritative_state"]["numeric_counters"]["erosion"] = {
            "counter_id": "erosion",
            "current_value": 7,
            "minimum": 0,
            "maximum": 100,
            "rule": "monotonic_non_decreasing",
        }
        patch = create_memory_patch(
            patch_id="patch-authority-rollback",
            source_kind="chapter",
            operations=[
                {
                    "op": "update_authoritative_state",
                    "value": {
                        "numeric_changes": [
                            {
                                "counter_id": "erosion",
                                "previous_value": 7,
                                "delta": -5,
                                "expected_value": 2,
                                "declared_value": 2,
                            }
                        ]
                    },
                }
            ],
        )

        with self.assertRaisesRegex(MemoryReducerError, "numeric_counter_rollback"):
            apply_memory_patch(memory, patch, event_context=_context())

    def test_numeric_arithmetic_mismatch_uses_unified_blocking_code(self) -> None:
        report = validate_authoritative_state_delta(
            base_state=empty_authoritative_state(),
            chapter_text="",
            state_delta={
                "numeric_changes": [
                    {
                        "counter_id": "erosion",
                        "previous_value": 7,
                        "delta": 2,
                        "expected_value": 8,
                        "declared_value": 9,
                    }
                ]
            },
        )

        self.assertFalse(report["accepted"])
        self.assertEqual({"numeric_counter_mismatch"}, _codes(report))
