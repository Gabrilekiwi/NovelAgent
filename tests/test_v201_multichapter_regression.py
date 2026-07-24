from __future__ import annotations

import copy
import unittest

from core.memory_v2 import (
    MemoryReducerError,
    apply_memory_events,
    apply_memory_patch,
    create_empty_typed_canonical_memory,
    create_memory_patch,
)


class V201MultiChapterRegressionTests(unittest.TestCase):
    def test_ten_chapter_authoritative_state_chain_replays_exactly(self) -> None:
        genesis = create_empty_typed_canonical_memory(book_id="v201-ten-chapter")
        current = copy.deepcopy(genesis)
        all_events: list[dict] = []

        for chapter_index in range(1, 11):
            previous_value = chapter_index - 1
            member_id = f"survivor_temp_{chapter_index:03d}"
            event_id = f"chapter-{chapter_index:04d}-event-001"
            chapter_body = (
                f"第{chapter_index}章证据：队伍接纳{member_id}，"
                f"侵蚀值从{previous_value}/100变为{chapter_index}/100。"
            )
            character_changes = (
                [
                    {
                        "character_id": "char_qian_ming",
                        "canonical_name": "钱明",
                        "aliases": ["王主管"],
                        "identity": "商场主管",
                        "active_motivations": [{"target_id": "char_qian_wife"}],
                    }
                ]
                if chapter_index == 1
                else []
            )
            relationship_changes = (
                [
                    {
                        "relationship_id": "rel_qian_ming_spouse",
                        "source_character_id": "char_qian_ming",
                        "target_character_id": "char_qian_wife",
                        "type": "spouse",
                        "status": "active",
                        "introduced_chapter": 1,
                    }
                ]
                if chapter_index == 1
                else []
            )
            delta = {
                "source_tier": "chapter_event",
                "character_changes": character_changes,
                "relationship_changes": relationship_changes,
                "roster_changes": [
                    {
                        "roster_id": "main_survivor_group",
                        "operation": "join",
                        "member_ids": [member_id],
                        "members": [
                            {
                                "member_id": member_id,
                                "descriptor": f"第{chapter_index}位幸存者",
                                "status": "active",
                                "joined_chapter": chapter_index,
                                "joined_event_id": event_id,
                            }
                        ],
                        "delta": 1,
                        "declared_count": chapter_index,
                    }
                ],
                "numeric_changes": [
                    {
                        "counter_id": "erosion",
                        "owner_id": "char_qian_ming",
                        "previous_value": previous_value,
                        "delta": 1,
                        "expected_value": chapter_index,
                        "declared_value": chapter_index,
                        "minimum": 0,
                        "maximum": 100,
                        "rule": "monotonic_non_decreasing",
                        "last_updated_chapter": chapter_index,
                        "source_event_id": event_id,
                    }
                ],
                "inventory_changes": [
                    {
                        "owner_id": "main_survivor_group",
                        "item_id": "medicine",
                        "previous_quantity": previous_value,
                        "delta": 1,
                        "declared_quantity": chapter_index,
                        "source_event_id": event_id,
                    }
                ],
                "location_changes": [
                    {
                        "entity_id": "main_survivor_group",
                        "before": f"zone_{previous_value:02d}",
                        "after": f"zone_{chapter_index:02d}",
                        "source_event_id": event_id,
                    }
                ],
                "events": [
                    {
                        "event_id": event_id,
                        "type": "survivor_joined",
                        "subjects": ["char_qian_ming"],
                        "objects": [member_id],
                        "location": f"zone_{chapter_index:02d}",
                        "status": "completed",
                    }
                ],
            }
            patch = create_memory_patch(
                patch_id=f"patch-v201-chapter-{chapter_index:04d}",
                source_kind="chapter",
                operations=[{"op": "update_authoritative_state", "value": delta}],
            )
            current, chapter_events = apply_memory_patch(
                current,
                patch,
                event_context={
                    "chapter_body": chapter_body,
                    "evidence_spans": [
                        {
                            "start_char": 0,
                            "end_char": len(chapter_body),
                            "quote": chapter_body,
                        }
                    ],
                    "authority_epoch": 1,
                },
            )
            all_events.extend(chapter_events)

            authority = current["authoritative_state"]
            self.assertEqual(
                chapter_index,
                authority["roster"]["main_survivor_group"]["computed_count"],
            )
            self.assertEqual(
                chapter_index,
                authority["numeric_counters"]["erosion"]["current_value"],
            )
            self.assertEqual(
                chapter_index,
                authority["inventory"]["main_survivor_group:medicine"]["quantity"],
            )
            self.assertEqual(
                f"zone_{chapter_index:02d}",
                authority["locations"]["main_survivor_group"]["location_id"],
            )

        replayed = apply_memory_events(
            genesis,
            all_events,
            reducer_version="memory-reducer-2.2",
        )
        self.assertEqual(current, replayed)
        self.assertEqual(10, len(all_events))
        self.assertEqual(
            "char_qian_ming",
            next(iter(current["authoritative_state"]["characters"])),
        )
        self.assertEqual(
            "spouse",
            current["authoritative_state"]["relationships"]["rel_qian_ming_spouse"]["type"],
        )

    def test_chapter_eleven_conflicts_are_blocked_without_mutating_chapter_ten(self) -> None:
        memory = create_empty_typed_canonical_memory(book_id="v201-conflict")
        authority = memory["authoritative_state"]
        authority["characters"]["char_qian_ming"] = {
            "character_id": "char_qian_ming",
            "canonical_name": "钱明",
            "aliases": ["王主管"],
            "identity": "商场主管",
            "source_tier": "chapter_event",
        }
        authority["relationships"]["rel_qian_ming_spouse"] = {
            "relationship_id": "rel_qian_ming_spouse",
            "source_character_id": "char_qian_ming",
            "target_character_id": "char_qian_wife",
            "type": "spouse",
            "status": "active",
            "source_tier": "chapter_event",
        }
        authority["roster"]["main_survivor_group"] = {
            "roster_id": "main_survivor_group",
            "members": [
                {
                    "member_id": f"survivor_temp_{index:03d}",
                    "descriptor": f"第{index}位幸存者",
                }
                for index in range(1, 11)
            ],
            "declared_count": 10,
            "computed_count": 10,
            "source_tier": "chapter_event",
        }
        authority["numeric_counters"]["erosion"] = {
            "counter_id": "erosion",
            "current_value": 10,
            "minimum": 0,
            "maximum": 100,
            "rule": "monotonic_non_decreasing",
            "source_tier": "chapter_event",
        }
        before = copy.deepcopy(memory)
        conflict_delta = {
            "source_tier": "chapter_event",
            "character_changes": [
                {
                    "character_id": "char_manager_reinterpreted",
                    "canonical_name": "王主管",
                    "aliases": [],
                    "identity": "临时安保",
                }
            ],
            "relationship_changes": [
                {
                    "relationship_id": "rel_qian_ming_spouse",
                    "source_character_id": "char_qian_ming",
                    "target_character_id": "char_qian_cousin",
                    "type": "cousin",
                    "status": "active",
                }
            ],
            "roster_changes": [
                {
                    "roster_id": "main_survivor_group",
                    "operation": "replace",
                    "member_ids": [
                        *(f"survivor_temp_{index:03d}" for index in range(1, 11)),
                        "survivor_temp_011",
                    ],
                    "members": [
                        {
                            "member_id": f"survivor_temp_{index:03d}",
                            "descriptor": (
                                "身份被偷换的成年人"
                                if index == 1
                                else f"第{index}位幸存者"
                            ),
                        }
                        for index in range(1, 12)
                    ],
                    "delta": 1,
                    "declared_count": 12,
                }
            ],
            "numeric_changes": [
                {
                    "counter_id": "erosion",
                    "previous_value": 10,
                    "delta": -8,
                    "expected_value": 2,
                    "declared_value": 2,
                }
            ],
            "inventory_changes": [],
            "location_changes": [],
            "events": [],
        }
        patch = create_memory_patch(
            patch_id="patch-v201-chapter-0011-conflict",
            source_kind="chapter",
            operations=[
                {
                    "op": "update_authoritative_state",
                    "value": conflict_delta,
                }
            ],
        )

        with self.assertRaises(MemoryReducerError) as captured:
            apply_memory_patch(
                memory,
                patch,
                event_context={
                    "chapter_body": "第十一章冲突证据",
                    "evidence_spans": [
                        {
                            "start_char": 0,
                            "end_char": 8,
                            "quote": "第十一章冲突证据",
                        }
                    ],
                    "authority_epoch": 1,
                },
            )

        message = str(captured.exception)
        for code in (
            "character_identity_drift",
            "character_relationship_drift",
            "roster_member_identity_drift",
            "roster_count_mismatch",
            "numeric_counter_rollback",
        ):
            self.assertIn(code, message)
        self.assertEqual(before, memory)


if __name__ == "__main__":
    unittest.main()
