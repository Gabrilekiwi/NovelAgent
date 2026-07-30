from __future__ import annotations

import copy
import json
import unittest

from core.state.authoritative import empty_authoritative_state
from core.state.generation_state_view import (
    GENERATION_STATE_VIEW_HEADING,
    apply_generation_state_view_to_snapshot,
    build_scene_generation_state_reference,
    build_generation_state_view,
    filter_scene_state_for_generation,
    generation_state_view_from_markdown,
    project_snapshot_for_generation,
    scene_generation_state_reference_from_markdown,
)
from core.structured_context import StructuredContextError, sha256_text


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _read_set(
    *,
    state_ids: list[str],
    event_ids: list[str],
    expected_new_entities: list[dict] | None = None,
) -> dict:
    value = {
        "schema_version": "1.0",
        "mode": "explicit",
        "chapter_index": 18,
        "required_state_item_ids": sorted(state_ids),
        "required_event_item_ids": sorted(event_ids),
        "continuity": {
            "last_scene_location": "二层通信室",
            "last_scene_character_ids": ["hero"],
            "required_opening_bridge": "从通信室的倒计时继续。",
        },
        "narrative_constraints": [
            {
                "constraint_id": "fs-siren",
                "lifecycle_action": "active",
                "instruction": "保留警报来源悬念。",
            }
        ],
        "expected_new_entities": expected_new_entities or [],
        "source_outline_sha256": "a" * 64,
    }
    value["contract_sha256"] = sha256_text(_canonical_json(value))
    return value


def _authority() -> dict:
    state = empty_authoritative_state()
    state["characters"] = {
        "hero": {
            "character_id": "hero",
            "canonical_name": "陆沉",
            "aliases": ["陆沉"],
            "current_goal": "仍停留在旧章节的目标",
            "current_location": "冷库",
            "last_observation": "旧章节追加日志",
            "last_seen_chapter": 10,
            "field": "伤势",
            "before": "轻伤",
            "after": "稳定",
            "伤势": "稳定",
            "reason": "上一章处置",
            "source_event_id": "chapter-0017-beat-004",
            "source_tier": "chapter_event",
        },
        "bystander": {
            "character_id": "bystander",
            "canonical_name": "无关者",
        },
        "scout": {
            "character_id": "scout",
            "canonical_name": "侦察员",
        },
    }
    state["relationships"] = {
        "hero->ally": {
            "relationship_id": "hero->ally",
            "source_character_id": "hero",
            "target_character_id": "ally",
            "type": "合作",
            "field": "合作边界",
            "before": None,
            "after": "共享避险区但组织不合并",
            "合作边界": "共享避险区但组织不合并",
            "scene_index": 8,
            "source_event_id": "chapter-0017-beat-004",
        }
    }
    state["roster"] = {
        "fireseed": {
            "roster_id": "fireseed",
            "name": "火种一队",
            "members": [],
            "unresolved_count": 17,
            "declared_count": 17,
            "computed_count": 17,
            "baseline_evidence": {
                "source_path": "chapters/17.md",
                "sha256": "b" * 64,
            },
        }
    }
    state["numeric_counters"] = {
        "erosion": {
            "counter_id": "erosion",
            "owner_id": "hero",
            "previous_value": 5,
            "delta": 1,
            "expected_value": 6,
            "declared_value": 6,
            "current_value": 6,
            "minimum": 0,
            "maximum": 100,
            "rule": "monotonic_non_decreasing",
            "source_event_id": "chapter-0017-beat-004",
        }
    }
    state["inventory"] = {
        "hero:ammo": {
            "inventory_id": "hero:ammo",
            "owner_id": "hero",
            "item_id": "ammo",
            "previous_quantity": 10,
            "delta": -2,
            "declared_quantity": 8,
            "quantity": 8,
        }
    }
    state["locations"] = {
        "hero": {
            "entity_id": "hero",
            "before": "车库",
            "after": "二层通信室",
            "location_id": "二层通信室",
            "certainty": "confirmed",
            "status": "current",
            "last_reported_chapter": 17,
        },
        "scout": {
            "entity_id": "scout",
            "location_id": "北站外沿",
            "certainty": "self_reported",
            "status": "unverified",
            "last_reported_chapter": 17,
        },
    }
    state["events"] = {
        "chapter-0017-beat-004": {
            "event_id": "chapter-0017-beat-004",
            "type": "checkpoint",
            "subjects": ["hero"],
            "objects": [],
            "location": "二层通信室",
            "status": "completed",
            "detail": "已完成通信室处置。",
        },
        "chapter-0001-beat-001": {
            "event_id": "chapter-0001-beat-001",
            "type": "old_history",
            "subjects": ["bystander"],
            "objects": [],
            "status": "completed",
        },
    }
    return state


class GenerationStateViewTests(unittest.TestCase):
    def test_build_is_exact_typed_auditable_and_keeps_dynamic_current_field(
        self,
    ) -> None:
        authority = _authority()
        original = copy.deepcopy(authority)
        read_set = _read_set(
            state_ids=[
                "characters/hero",
                "relationships/hero->ally",
                "roster/fireseed",
                "numeric_counters/erosion",
                "inventory/hero:ammo",
                "locations/hero",
                "locations/scout",
            ],
            event_ids=["events/chapter-0017-beat-004"],
            expected_new_entities=[
                {
                    "kind": "scene_local",
                    "entity_id": "seeker-001",
                    "display_name": "寻声者",
                }
            ],
        )

        view = build_generation_state_view(authority, read_set)

        self.assertEqual(original, authority)
        self.assertEqual(18, view["chapter_index"])
        self.assertEqual(read_set["contract_sha256"], view["read_set_digest"])
        self.assertEqual({"hero"}, set(view["current_state"]["characters"]))
        self.assertNotIn("bystander", view["current_state"]["characters"])
        relationship = view["current_state"]["relationships"]["hero->ally"]
        self.assertEqual("合作边界", relationship["field"])
        self.assertEqual(
            "共享避险区但组织不合并",
            relationship["合作边界"],
        )
        character = view["current_state"]["characters"]["hero"]
        self.assertEqual("稳定", character["伤势"])
        for legacy_projection_field in (
            "current_goal",
            "current_location",
            "location_id",
            "last_observation",
            "last_seen_chapter",
        ):
            self.assertNotIn(legacy_projection_field, character)
        for audit_field in (
            "before",
            "after",
            "delta",
            "reason",
            "scene_index",
            "source_event_id",
            "baseline_evidence",
        ):
            self.assertNotIn(audit_field, _canonical_json(view["current_state"]))
        self.assertEqual(
            {"chapter-0017-beat-004"},
            set(view["required_events"]),
        )
        self.assertNotIn("chapter-0001-beat-001", view["required_events"])
        self.assertEqual(
            "self_reported",
            view["current_state"]["locations"]["scout"]["certainty"],
        )
        self.assertEqual(
            "unverified",
            view["current_state"]["locations"]["scout"]["status"],
        )
        self.assertEqual(
            17,
            view["current_state"]["locations"]["scout"][
                "last_reported_chapter"
            ],
        )

        hash_input = copy.deepcopy(view)
        projection_sha256 = hash_input.pop("projection_sha256")
        self.assertEqual(
            sha256_text(_canonical_json(hash_input)),
            projection_sha256,
        )

    def test_missing_wrong_collection_and_expected_collision_fail_closed(
        self,
    ) -> None:
        authority = _authority()
        cases = {
            "missing": _read_set(
                state_ids=["characters/missing"],
                event_ids=[],
            ),
            "wrong_collection": _read_set(
                state_ids=[],
                event_ids=["characters/hero"],
            ),
            "shorthand": _read_set(
                state_ids=["hero"],
                event_ids=[],
            ),
            "expected_collision": _read_set(
                state_ids=[],
                event_ids=[],
                expected_new_entities=[
                    {
                        "kind": "character",
                        "entity_id": "new-id",
                        "display_name": "陆沉",
                    }
                ],
            ),
        }

        for name, read_set in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(StructuredContextError):
                    build_generation_state_view(authority, read_set)

    def test_read_set_contract_and_markdown_projection_hash_are_verified(
        self,
    ) -> None:
        authority = _authority()
        read_set = _read_set(
            state_ids=["characters/hero"],
            event_ids=["events/chapter-0017-beat-004"],
        )
        view = build_generation_state_view(authority, read_set)
        markdown = (
            "# Project Profile\nexample\n\n"
            f"# {GENERATION_STATE_VIEW_HEADING}\n"
            + json.dumps(view, ensure_ascii=False, indent=2)
            + "\n\n# Requirements\ncontinue"
        )

        self.assertEqual(view, generation_state_view_from_markdown(markdown))
        self.assertIsNone(
            generation_state_view_from_markdown("# Requirements\ncontinue")
        )

        tampered = copy.deepcopy(view)
        tampered["current_state"]["characters"]["hero"]["伤势"] = "恶化"
        with self.assertRaises(StructuredContextError):
            generation_state_view_from_markdown(
                f"# {GENERATION_STATE_VIEW_HEADING}\n"
                + json.dumps(tampered, ensure_ascii=False)
            )

        unsupported_policy = copy.deepcopy(view)
        unsupported_policy["policy"] = "unbounded_legacy_projection"
        unsupported_policy.pop("projection_sha256")
        unsupported_policy["projection_sha256"] = sha256_text(
            _canonical_json(unsupported_policy)
        )
        with self.assertRaises(StructuredContextError):
            generation_state_view_from_markdown(
                f"# {GENERATION_STATE_VIEW_HEADING}\n"
                + json.dumps(unsupported_policy, ensure_ascii=False)
            )

        bad_read_set = copy.deepcopy(read_set)
        bad_read_set["continuity"]["last_scene_location"] = "一层车库"
        with self.assertRaises(StructuredContextError):
            build_generation_state_view(authority, bad_read_set)

    def test_apply_updates_only_explicit_confirmed_continuity_on_a_copy(
        self,
    ) -> None:
        authority = _authority()
        view = build_generation_state_view(
            authority,
            _read_set(
                state_ids=[
                    "characters/hero",
                    "characters/scout",
                    "relationships/hero->ally",
                    "roster/fireseed",
                    "numeric_counters/erosion",
                    "inventory/hero:ammo",
                    "locations/hero",
                    "locations/scout",
                ],
                event_ids=[],
            ),
        )
        snapshot = {
            "authoritative_state": copy.deepcopy(authority),
            "story_state": {
                "last_scene_location": "车库",
                "unrelated": "preserve",
            },
            "characters": {
                "hero": {"role": "主角", "current_location": "车库"},
                "scout": {"role": "侦察员", "current_location": "消防站"},
            },
            "world_state": {"locations": {"车库": {"known": True}}},
            "spatial_state": {
                "spaces": {"车库": {}},
                "character_positions": {
                    "hero": "车库",
                    "scout": "消防站",
                },
            },
            "relationships": {
                "hero->ally": {
                    "relationship_id": "hero->ally",
                    "合作边界": "旧边界",
                }
            },
            "roster": {
                "fireseed": {
                    "roster_id": "fireseed",
                    "declared_count": 99,
                }
            },
            "numeric_counters": {
                "erosion": {
                    "counter_id": "erosion",
                    "current_value": 2,
                }
            },
            "inventory": {
                "hero:ammo": {
                    "inventory_id": "hero:ammo",
                    "quantity": 99,
                }
            },
            "locations": {
                "hero": {
                    "entity_id": "hero",
                    "location_id": "车库",
                }
            },
        }
        original = copy.deepcopy(snapshot)

        applied = apply_generation_state_view_to_snapshot(snapshot, view)

        self.assertEqual(original, snapshot)
        self.assertEqual(
            original["authoritative_state"],
            applied["authoritative_state"],
        )
        self.assertEqual(
            "二层通信室",
            applied["story_state"]["last_scene_location"],
        )
        self.assertEqual("preserve", applied["story_state"]["unrelated"])
        self.assertEqual(
            ["hero"],
            applied["story_state"]["last_scene_characters"],
        )
        self.assertNotIn(
            "last_scene_character_ids",
            applied["story_state"],
        )
        self.assertEqual(
            "共享避险区但组织不合并",
            applied["relationships"]["hero->ally"]["合作边界"],
        )
        self.assertEqual(
            17,
            applied["roster"]["fireseed"]["declared_count"],
        )
        self.assertEqual(
            6,
            applied["numeric_counters"]["erosion"]["current_value"],
        )
        self.assertEqual(
            8,
            applied["inventory"]["hero:ammo"]["quantity"],
        )
        self.assertEqual(
            "二层通信室",
            applied["locations"]["hero"]["location_id"],
        )
        self.assertEqual(
            "二层通信室",
            applied["characters"]["hero"]["current_location"],
        )
        self.assertEqual(
            "二层通信室",
            applied["spatial_state"]["character_positions"]["hero"],
        )
        self.assertNotIn(
            "current_location",
            applied["characters"]["scout"],
        )
        self.assertNotIn(
            "scout",
            applied["spatial_state"]["character_positions"],
        )
        self.assertIn("二层通信室", applied["world_state"]["locations"])
        self.assertIn("二层通信室", applied["spatial_state"]["spaces"])
        self.assertIn("北站外沿", applied["world_state"]["locations"])
        self.assertIn("北站外沿", applied["spatial_state"]["spaces"])

    def test_filter_scene_state_keeps_only_working_set_and_explicit_history(
        self,
    ) -> None:
        authority = _authority()
        view = build_generation_state_view(
            authority,
            _read_set(
                state_ids=[
                    "characters/hero",
                    "relationships/hero->ally",
                    "roster/fireseed",
                    "numeric_counters/erosion",
                    "inventory/hero:ammo",
                    "locations/hero",
                ],
                event_ids=["events/chapter-0017-beat-004"],
                expected_new_entities=[
                    {
                        "kind": "scene_local",
                        "entity_id": "seeker-001",
                        "display_name": "寻声者",
                    }
                ],
            ),
        )
        scene_state = {
            "schema_version": "1.0",
            "characters": {
                "hero": {
                    "伤势": "稳定",
                    "current_goal": "旧章节目标",
                    "current_location": "冷库",
                    "last_observation": "旧日志",
                    "before": "轻伤",
                    "after": "稳定",
                    "source_event_id": "stale-event",
                },
                "bystander": {"role": "无关"},
                "seeker-001": {"role": "新实体"},
            },
            "relationships": {
                "hero->ally": {
                    "合作边界": "共享避险区但组织不合并",
                    "before": None,
                    "after": "共享避险区但组织不合并",
                    "scene_index": 8,
                },
                "other": {"status": "noise"},
            },
            "rosters": {
                "fireseed": {"declared_count": 17},
                "other": {"declared_count": 99},
            },
            "locations": {
                "hero": "二层通信室",
                "bystander": "远处",
                "seeker-001": "消防站外",
            },
            "inventories": {
                "hero:ammo": 8,
                "other:item": 20,
            },
            "counters": {"erosion": 6, "noise": 100},
            "completed_event_ids": [
                "chapter-0001-beat-001",
                "chapter-0018-beat-001",
            ],
            "completed_events": [
                {
                    "event_id": "chapter-0001-beat-001",
                    "status": "completed",
                },
                {
                    "event_id": "chapter-0018-beat-001",
                    "status": "completed",
                },
            ],
            "characters_present": ["陆沉", "无关者", "seeker-001"],
            "current_location": "二层通信室",
            "open_action": "等待倒计时",
            "open_actions": [
                {"event_id": "noise", "subject": "bystander"},
                {"event_id": "current", "subject": "hero"},
            ],
        }
        original = copy.deepcopy(scene_state)

        filtered = filter_scene_state_for_generation(
            scene_state,
            view,
            active_event_ids=["chapter-0018-beat-001"],
        )

        self.assertEqual(original, scene_state)
        self.assertEqual(
            {"hero", "seeker-001"},
            set(filtered["characters"]),
        )
        self.assertEqual({"hero->ally"}, set(filtered["relationships"]))
        self.assertEqual({"fireseed"}, set(filtered["rosters"]))
        self.assertEqual({"hero:ammo"}, set(filtered["inventories"]))
        self.assertEqual({"erosion"}, set(filtered["counters"]))
        self.assertEqual(
            [
                "chapter-0017-beat-004",
                "chapter-0018-beat-001",
            ],
            filtered["completed_event_ids"],
        )
        self.assertEqual([], filtered["completed_events"])
        self.assertEqual(
            [{"event_id": "current", "subject": "hero"}],
            filtered["open_actions"],
        )
        self.assertNotIn("无关者", filtered["characters_present"])
        for stale_field in (
            "current_goal",
            "current_location",
            "last_observation",
            "before",
            "after",
            "source_event_id",
        ):
            self.assertNotIn(stale_field, filtered["characters"]["hero"])
        for stale_field in ("before", "after", "scene_index"):
            self.assertNotIn(
                stale_field,
                filtered["relationships"]["hero->ally"],
            )

    def test_scene_reference_binds_full_view_without_repeating_current_values(
        self,
    ) -> None:
        view = build_generation_state_view(
            _authority(),
            _read_set(
                state_ids=[
                    "characters/hero",
                    "relationships/hero->ally",
                    "roster/fireseed",
                    "numeric_counters/erosion",
                    "inventory/hero:ammo",
                    "locations/hero",
                ],
                event_ids=["events/chapter-0017-beat-004"],
            ),
        )

        reference = build_scene_generation_state_reference(view)
        markdown = (
            "# Generation State View Reference\n"
            + _canonical_json(reference)
        )

        self.assertEqual(
            reference,
            scene_generation_state_reference_from_markdown(markdown),
        )
        self.assertEqual(
            view["projection_sha256"],
            reference["source_generation_state_view_sha256"],
        )
        self.assertNotIn("current_state", reference)
        self.assertNotIn("chapter_context_read_set", reference)
        self.assertEqual(
            "current_scene_state",
            reference["current_state_binding"]["payload_field"],
        )
        self.assertEqual(
            {"chapter-0017-beat-004"},
            set(reference["required_events"]),
        )

    def test_project_snapshot_removes_raw_history_and_keeps_current_working_set(
        self,
    ) -> None:
        authority = _authority()
        view = build_generation_state_view(
            authority,
            _read_set(
                state_ids=[
                    "characters/hero",
                    "relationships/hero->ally",
                    "roster/fireseed",
                    "numeric_counters/erosion",
                    "inventory/hero:ammo",
                    "locations/hero",
                    "locations/scout",
                ],
                event_ids=["events/chapter-0017-beat-004"],
            ),
        )
        snapshot = {
            "book_id": "book",
            "chapter_index": 18,
            "project_profile": {
                "language": "zh-CN",
                "known_characters": ["hero", "bystander"],
                "known_locations": ["二层通信室", "RAW-LOCATION"],
            },
            "world_state": {
                "infection_level": "high",
                "text": "RAW-WORLD-DOCUMENT",
                "locations": {
                    "二层通信室": {"known": True},
                    "RAW-LOCATION": {"detail": "RAW-LOCATION-DETAIL"},
                },
            },
            "story_state": {
                "last_scene_location": "old",
                "last_scene_characters": ["bystander"],
                "required_opening_bridge": "old",
                "last_chapter_ending": "RAW-LAST-ENDING",
            },
            "spatial_state": {
                "spaces": {
                    "二层通信室": {},
                    "北站外沿": {},
                    "RAW-LOCATION": {},
                },
                "connections": [
                    {"from": "二层通信室", "to": "北站外沿"},
                    {"from": "二层通信室", "to": "RAW-LOCATION"},
                ],
                "character_positions": {
                    "hero": "旧位置",
                    "scout": "消防站",
                    "bystander": "RAW-LOCATION",
                },
                "blocked_paths": [],
                "last_transition": {
                    "from": "车库",
                    "to": "二层通信室",
                },
            },
            "characters": {
                "hero": {"伤势": "旧状态"},
                "bystander": {"detail": "RAW-BYSTANDER"},
                "scout": {"current_location": "消防站"},
            },
            "authoritative_state": copy.deepcopy(authority),
            "timeline": [
                {"chapter_index": 1, "detail": "RAW-TIMELINE-EVENT"}
            ],
            "constraints": ["RAW-CONSTRAINT"],
            "memory": {"detail": "RAW-MEMORY"},
        }
        original = copy.deepcopy(snapshot)

        projected = project_snapshot_for_generation(snapshot, view)

        self.assertEqual(original, snapshot)
        rendered = _canonical_json(projected)
        for marker in (
            "RAW-WORLD-DOCUMENT",
            "RAW-LOCATION-DETAIL",
            "RAW-LAST-ENDING",
            "RAW-BYSTANDER",
            "RAW-TIMELINE-EVENT",
            "RAW-CONSTRAINT",
            "RAW-MEMORY",
        ):
            self.assertNotIn(marker, rendered)
        self.assertNotIn("authoritative_state", projected)
        self.assertNotIn("timeline", projected)
        self.assertEqual(1, projected["timeline_summary"]["entry_count"])
        self.assertEqual({"hero"}, set(projected["characters"]))
        self.assertEqual(
            "稳定",
            projected["characters"]["hero"]["伤势"],
        )
        self.assertEqual(
            "共享避险区但组织不合并",
            projected["relationships"]["hero->ally"]["合作边界"],
        )
        self.assertEqual(
            6,
            projected["numeric_counters"]["erosion"]["current_value"],
        )
        self.assertEqual(
            8,
            projected["inventory"]["hero:ammo"]["quantity"],
        )
        self.assertEqual(
            "二层通信室",
            projected["spatial_state"]["character_positions"]["hero"],
        )
        self.assertEqual(
            "消防站",
            snapshot["spatial_state"]["character_positions"]["scout"],
        )
        self.assertNotIn(
            "scout",
            projected["spatial_state"]["character_positions"],
        )
        self.assertEqual(
            view["projection_sha256"],
            projected["generation_state_view"]["projection_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
