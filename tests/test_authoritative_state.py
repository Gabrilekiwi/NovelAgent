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
    adapt_scene_deltas_to_authoritative_delta,
    empty_authoritative_state,
    seed_authoritative_state_from_snapshot,
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
                        "reason_event_id": "chapter-0001-event-001",
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
                        "source_event_id": "chapter-0001-event-001",
                    }
                ],
                "inventory_changes": [
                    {
                        "owner_id": "char_qian_ming",
                        "item_id": "medicine",
                        "previous_quantity": 0,
                        "delta": 1,
                        "declared_quantity": 1,
                        "source_event_id": "chapter-0001-event-001",
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
                        "reason_event_id": "member-002-left",
                    }
                ],
                "events": [
                    {
                        "event_id": "member-002-left",
                        "type": "member_left",
                        "subjects": ["member_002"],
                        "objects": ["main_group"],
                        "location": "",
                        "status": "completed",
                    }
                ],
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
                        "source_event_id": "erosion-change",
                    }
                ],
                "events": [
                    {
                        "event_id": "erosion-change",
                        "type": "ability_used",
                        "subjects": ["char_protagonist"],
                        "objects": ["erosion"],
                        "location": "",
                        "status": "completed",
                    }
                ],
            },
        )

        self.assertFalse(report["accepted"])
        self.assertEqual({"numeric_counter_mismatch"}, _codes(report))

    def test_legacy_snapshot_seeds_character_location_and_erosion(self) -> None:
        state = seed_authoritative_state_from_snapshot(
            {
                "characters": {
                    "陆沉": {
                        "role": "主角",
                        "erosion": 0,
                        "current_location": "冷库",
                    }
                },
                "spatial_state": {"character_positions": {"陆沉": "冷库"}},
            }
        )

        self.assertEqual("陆沉", state["characters"]["陆沉"]["canonical_name"])
        self.assertEqual("冷库", state["locations"]["陆沉"]["location_id"])
        self.assertEqual(
            0,
            state["numeric_counters"]["陆沉侵蚀值"]["current_value"],
        )
        self.assertEqual(
            "model_inference",
            state["locations"]["陆沉"]["source_tier"],
        )
        self.assertEqual(
            "model_inference",
            state["numeric_counters"]["陆沉侵蚀值"]["source_tier"],
        )

    def test_chapter_events_supersede_legacy_inferred_mutable_state(self) -> None:
        base = seed_authoritative_state_from_snapshot(
            {
                "characters": {
                    "陆沉": {
                        "role": "主角",
                        "erosion": 0,
                        "current_location": "冷库",
                    }
                },
                "spatial_state": {"character_positions": {"陆沉": "冷库"}},
            }
        )
        delta = adapt_scene_deltas_to_authoritative_delta(
            [
                {
                    "index": 4,
                    "events": [
                        {
                            "event_id": "chapter-0017-beat-004",
                            "type": "alarm_disabled",
                            "subjects": ["陆沉"],
                            "objects": ["fire_alarm"],
                            "location": "消防站器材口检修夹层",
                            "status": "completed",
                        }
                    ],
                    "deltas": {
                        "characters": [],
                        "relationships": [],
                        "rosters": [],
                        "locations": [
                            {
                                "entity_id": "陆沉",
                                "before": None,
                                "after": "消防站器材口检修夹层",
                                "reason": "legacy location is stale",
                            }
                        ],
                        "inventory": [],
                        "counters": [
                            {
                                "counter_id": "陆沉侵蚀值",
                                "before": 6,
                                "delta": 0,
                                "after": 6,
                                "source_event_id": "chapter-0017-beat-004",
                            }
                        ],
                    },
                }
            ],
            base_state=base,
        )

        report = validate_authoritative_state_delta(
            base_state=base,
            state_delta=delta,
            chapter_text="陆沉确认侵蚀值仍是6/100。",
        )

        self.assertTrue(report["accepted"], report["findings"])
        self.assertEqual(
            "消防站器材口检修夹层",
            report["state_after"]["locations"]["陆沉"]["location_id"],
        )
        self.assertEqual(
            6,
            report["state_after"]["numeric_counters"]["陆沉侵蚀值"]["current_value"],
        )
        self.assertEqual(
            "chapter_event",
            report["state_after"]["locations"]["陆沉"]["source_tier"],
        )
        self.assertEqual(
            "chapter_event",
            report["state_after"]["numeric_counters"]["陆沉侵蚀值"]["source_tier"],
        )

    def test_scene_field_deltas_adapt_to_full_authority_records(self) -> None:
        delta = adapt_scene_deltas_to_authoritative_delta(
            [
                {
                    "index": 3,
                    "events": [
                        {
                            "event_id": "chapter-0016-beat-003",
                            "type": "identity_confirmed",
                            "subjects": ["韩野"],
                            "objects": [],
                            "location": "消防站",
                            "status": "completed",
                        }
                    ],
                    "deltas": {
                        "characters": [
                            {
                                "character_id": "韩野",
                                "field": "canonical_name",
                                "before": None,
                                "after": "韩野",
                            },
                            {
                                "character_id": "韩野",
                                "field": "role",
                                "before": None,
                                "after": "消防站负责人",
                            },
                        ],
                        "relationships": [],
                        "rosters": [],
                        "locations": [],
                        "inventory": [],
                        "counters": [],
                    },
                }
            ],
            base_state=empty_authoritative_state(),
        )
        report = validate_authoritative_state_delta(
            base_state=empty_authoritative_state(),
            state_delta=delta,
            chapter_text="",
        )

        self.assertTrue(report["accepted"], report["findings"])
        self.assertEqual(
            "消防站负责人",
            report["state_after"]["characters"]["韩野"]["role"],
        )

    def test_chapter_counter_declaration_must_match_seeded_state(self) -> None:
        base = seed_authoritative_state_from_snapshot(
            {"characters": {"陆沉": {"role": "主角", "erosion": 0}}}
        )

        report = validate_authoritative_state_delta(
            base_state=base,
            state_delta={},
            chapter_text="陆沉侵蚀值6/100。",
        )

        self.assertFalse(report["accepted"])
        self.assertIn("numeric_counter_mismatch", _codes(report))

    def test_chapter_counter_transition_may_name_before_and_after_values(self) -> None:
        base = empty_authoritative_state()
        base["numeric_counters"]["erosion"] = {
            "counter_id": "erosion",
            "current_value": 7,
            "minimum": 0,
            "maximum": 100,
            "rule": "monotonic_non_decreasing",
        }
        report = validate_authoritative_state_delta(
            base_state=base,
            state_delta={
                "numeric_changes": [
                    {
                        "counter_id": "erosion",
                        "previous_value": 7,
                        "delta": 2,
                        "expected_value": 9,
                        "declared_value": 9,
                        "source_event_id": "ability-used",
                    }
                ],
                "events": [
                    {
                        "event_id": "ability-used",
                        "type": "ability_used",
                        "subjects": ["hero"],
                        "objects": ["erosion"],
                        "location": "gate",
                        "status": "completed",
                    }
                ],
            },
            chapter_text="代价立刻显现，侵蚀值由7/100升至9/100。",
        )

        self.assertTrue(report["accepted"], report["findings"])

    def test_existing_authority_alias_owns_seeded_legacy_counter(self) -> None:
        authority = empty_authoritative_state()
        authority["characters"]["char_lu"] = {
            "character_id": "char_lu",
            "canonical_name": "陆沉",
            "aliases": ["队长"],
        }
        state = seed_authoritative_state_from_snapshot(
            {
                "authoritative_state": authority,
                "characters": {"陆沉": {"erosion": 6}},
            }
        )

        self.assertEqual(
            "char_lu",
            state["numeric_counters"]["陆沉侵蚀值"]["owner_id"],
        )

    def test_two_new_characters_cannot_claim_the_same_alias(self) -> None:
        report = validate_authoritative_state_delta(
            base_state=empty_authoritative_state(),
            state_delta={
                "character_changes": [
                    {
                        "character_id": "char-1",
                        "canonical_name": "钱明",
                        "aliases": ["王主管"],
                    },
                    {
                        "character_id": "char-2",
                        "canonical_name": "王主管",
                        "aliases": [],
                    },
                ]
            },
            chapter_text="",
        )

        self.assertFalse(report["accepted"])
        self.assertIn("character_identity_drift", _codes(report))

    def test_relationship_update_uses_the_selected_record_with_multiple_relations(
        self,
    ) -> None:
        base = empty_authoritative_state()
        base["relationships"] = {
            "rel-a": {
                "relationship_id": "rel-a",
                "source_character_id": "a",
                "target_character_id": "b",
                "type": "ally",
                "status": "active",
            },
            "rel-unrelated": {
                "relationship_id": "rel-unrelated",
                "source_character_id": "x",
                "target_character_id": "y",
                "type": "rival",
                "status": "active",
            },
        }
        report = validate_authoritative_state_delta(
            base_state=base,
            state_delta={
                "relationship_changes": [
                    {
                        "relationship_id": "rel-a",
                        "source_character_id": "a",
                        "target_character_id": "b",
                        "type": "ally",
                        "field": "status",
                        "before": "active",
                        "after": "strained",
                    }
                ]
            },
            chapter_text="",
        )

        self.assertTrue(report["accepted"], report["findings"])
        self.assertEqual(
            "strained",
            report["state_after"]["relationships"]["rel-a"]["status"],
        )

    def test_bidirectional_relationship_field_deltas_remain_independent(
        self,
    ) -> None:
        delta = adapt_scene_deltas_to_authoritative_delta(
            [
                {
                    "index": 6,
                    "events": [],
                    "deltas": {
                        "characters": [],
                        "relationships": [
                            {
                                "source_id": "陆沉",
                                "target_id": "韩野",
                                "field": "combat_coordination",
                                "before": None,
                                "after": "首次完成并肩作战配合",
                            }
                        ],
                        "rosters": [],
                        "locations": [],
                        "inventory": [],
                        "counters": [],
                    },
                },
                {
                    "index": 7,
                    "events": [],
                    "deltas": {
                        "characters": [],
                        "relationships": [
                            {
                                "source_id": "韩野",
                                "target_id": "陆沉",
                                "field": "threat_assessment",
                                "before": None,
                                "after": "警惕未消但认可其自我限制",
                            }
                        ],
                        "rosters": [],
                        "locations": [],
                        "inventory": [],
                        "counters": [],
                    },
                },
            ],
            base_state=empty_authoritative_state(),
        )

        report = validate_authoritative_state_delta(
            base_state=empty_authoritative_state(),
            state_delta=delta,
            chapter_text="",
        )

        self.assertTrue(report["accepted"], report["findings"])
        self.assertEqual(
            {"陆沉->韩野", "韩野->陆沉"},
            set(report["state_after"]["relationships"]),
        )
        self.assertEqual(
            "首次完成并肩作战配合",
            report["state_after"]["relationships"]["陆沉->韩野"][
                "combat_coordination"
            ],
        )
        self.assertEqual(
            "警惕未消但认可其自我限制",
            report["state_after"]["relationships"]["韩野->陆沉"][
                "threat_assessment"
            ],
        )

    def test_stateful_ledgers_require_declared_event_references(self) -> None:
        changes = {
            "roster_changes": [
                {
                    "roster_id": "main",
                    "operation": "join",
                    "member_ids": ["member-1"],
                    "members": [{"member_id": "member-1"}],
                    "delta": 1,
                    "declared_count": 1,
                }
            ],
            "numeric_changes": [
                {
                    "counter_id": "erosion",
                    "previous_value": 0,
                    "delta": 1,
                    "expected_value": 1,
                    "declared_value": 1,
                }
            ],
            "inventory_changes": [
                {
                    "owner_id": "main",
                    "item_id": "water",
                    "previous_quantity": 1,
                    "delta": -1,
                    "declared_quantity": 0,
                }
            ],
        }

        for key, value in changes.items():
            with self.subTest(ledger=key):
                report = validate_authoritative_state_delta(
                    base_state=empty_authoritative_state(),
                    state_delta={key: value},
                    chapter_text="",
                )
                self.assertFalse(report["accepted"])
                self.assertIn("missing_authority_event_reference", _codes(report))

    def test_relationship_field_delta_rejects_stale_before_state(self) -> None:
        base = empty_authoritative_state()
        base["relationships"]["fire->main"] = {
            "relationship_id": "fire->main",
            "source_character_id": "fire",
            "target_character_id": "main",
            "type": "cooperation",
            "合作边界": "共享避险区但组织未合并",
        }
        delta = adapt_scene_deltas_to_authoritative_delta(
            [
                {
                    "index": 1,
                    "events": [],
                    "deltas": {
                        "characters": [],
                        "relationships": [
                            {
                                "source_id": "fire",
                                "target_id": "main",
                                "field": "合作边界",
                                "before": None,
                                "after": "双方合并",
                            }
                        ],
                        "rosters": [],
                        "locations": [],
                        "inventory": [],
                        "counters": [],
                    },
                }
            ],
            base_state=base,
        )

        report = validate_authoritative_state_delta(
            base_state=base,
            state_delta=delta,
            chapter_text="",
        )

        self.assertFalse(report["accepted"])
        self.assertIn("relationship_state_rollback", _codes(report))

    def test_five_six_and_twelve_person_roster_errors_are_blocked(self) -> None:
        cases = (
            (4, 1, 5),
            (5, 1, 6),
            (7, 5, 12),
        )
        for previous_count, joined_count, expected_count in cases:
            with self.subTest(expected_count=expected_count):
                base = empty_authoritative_state()
                existing_members = [
                    {"member_id": f"member-{index:02d}"}
                    for index in range(1, previous_count + 1)
                ]
                base["roster"]["main"] = {
                    "roster_id": "main",
                    "members": existing_members,
                    "declared_count": previous_count,
                    "computed_count": previous_count,
                }
                joined_members = [
                    {"member_id": f"member-{previous_count + index:02d}"}
                    for index in range(1, joined_count + 1)
                ]
                event_id = f"join-to-{expected_count}"
                report = validate_authoritative_state_delta(
                    base_state=base,
                    chapter_text="",
                    state_delta={
                        "roster_changes": [
                            {
                                "roster_id": "main",
                                "operation": "join",
                                "member_ids": [
                                    item["member_id"] for item in joined_members
                                ],
                                "members": joined_members,
                                "delta": joined_count,
                                "declared_count": expected_count - 1,
                                "reason_event_id": event_id,
                            }
                        ],
                        "events": [
                            {
                                "event_id": event_id,
                                "type": "survivors_joined",
                                "subjects": [],
                                "objects": [
                                    item["member_id"] for item in joined_members
                                ],
                                "location": "shelter",
                                "status": "completed",
                            }
                        ],
                    },
                )

                self.assertFalse(report["accepted"])
                self.assertIn("roster_count_mismatch", _codes(report))
