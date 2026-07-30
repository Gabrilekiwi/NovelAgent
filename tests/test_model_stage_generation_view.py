from __future__ import annotations

import copy
import json
import unittest

from core.director.model_director import ModelDirector
from core.state.authoritative import empty_authoritative_state
from core.state.chapter_read_set import normalize_chapter_context_read_set
from core.state.generation_state_view import (
    build_generation_state_view,
    project_snapshot_for_generation,
)
from core.validator.llm import _llm_messages


def _authority() -> dict:
    state = empty_authoritative_state()
    state["characters"] = {
        "hero": {
            "character_id": "hero",
            "canonical_name": "Hero",
            "aliases": ["Hero"],
            "condition": "ready",
        },
        "raw-bystander": {
            "character_id": "raw-bystander",
            "canonical_name": "RAW-FULL-CHARACTER",
        },
    }
    state["relationships"] = {
        "hero->ally": {
            "relationship_id": "hero->ally",
            "source_character_id": "hero",
            "target_character_id": "ally",
            "type": "cooperation",
            "field": "boundary",
            "before": "old",
            "after": "shared shelter",
            "boundary": "shared shelter",
        }
    }
    state["roster"] = {
        "team": {
            "roster_id": "team",
            "members": [],
            "unresolved_count": 3,
            "declared_count": 3,
            "computed_count": 3,
        }
    }
    state["numeric_counters"] = {
        "erosion": {
            "counter_id": "erosion",
            "owner_id": "hero",
            "current_value": 6,
        }
    }
    state["inventory"] = {
        "hero:ammo": {
            "inventory_id": "hero:ammo",
            "owner_id": "hero",
            "item_id": "ammo",
            "quantity": 8,
        }
    }
    state["locations"] = {
        "hero": {
            "entity_id": "hero",
            "location_id": "radio-room",
            "certainty": "confirmed",
        }
    }
    state["events"] = {
        "chapter-0017-beat-004": {
            "event_id": "chapter-0017-beat-004",
            "type": "checkpoint",
            "subjects": ["hero"],
            "objects": [],
            "location": "radio-room",
            "status": "completed",
        }
    }
    return state


def _bounded_snapshot() -> tuple[dict, dict]:
    read_set = normalize_chapter_context_read_set(
        {
            "schema_version": "1.0",
            "mode": "explicit",
            "chapter_index": 18,
            "required_state_item_ids": [
                "characters/hero",
                "relationships/hero->ally",
                "roster/team",
                "numeric_counters/erosion",
                "inventory/hero:ammo",
                "locations/hero",
            ],
            "required_event_item_ids": [
                "events/chapter-0017-beat-004"
            ],
            "continuity": {
                "last_scene_location": "radio-room",
                "last_scene_character_ids": ["hero"],
                "required_opening_bridge": "continue the countdown",
            },
            "narrative_constraints": [
                {
                    "constraint_id": "fs-siren",
                    "lifecycle_action": "active",
                    "instruction": "Keep the siren source unresolved.",
                }
            ],
            "expected_new_entities": [
                {
                    "kind": "scene_local",
                    "entity_id": "seeker-001",
                    "display_name": "Seeker",
                }
            ],
        },
        chapter_index=18,
        source_outline_sha256="a" * 64,
    )
    authority = _authority()
    view = build_generation_state_view(authority, read_set)
    raw_snapshot = {
        "book_id": "book",
        "chapter_index": 18,
        "project_profile": {
            "language": "en",
            "known_characters": ["hero", "Hero", "raw-bystander"],
            "known_locations": ["radio-room", "RAW-PROFILE-LOCATION"],
        },
        "world_state": {
            "infection_level": "high",
            "text": "RAW-WORLD-DOCUMENT",
            "locations": {
                "radio-room": {"status": "known"},
                "RAW-PROFILE-LOCATION": {"detail": "RAW-WORLD-LOCATION"},
            },
        },
        "story_state": {
            "last_scene_location": "old-room",
            "last_scene_characters": ["raw-bystander"],
            "required_opening_bridge": "old bridge",
            "last_chapter_ending": "RAW-LAST-CHAPTER",
        },
        "spatial_state": {
            "spaces": {
                "radio-room": {},
                "RAW-PROFILE-LOCATION": {},
            },
            "connections": [],
            "character_positions": {
                "hero": "old-room",
                "raw-bystander": "RAW-PROFILE-LOCATION",
            },
            "blocked_paths": [],
            "last_transition": {},
        },
        "characters": {
            "hero": {"condition": "stale"},
            "raw-bystander": {"detail": "RAW-FULL-CHARACTER"},
        },
        "authoritative_state": authority,
        "timeline": [
            {"chapter_index": 1, "detail": "RAW-TIMELINE-HISTORY"}
        ],
        "memory": {"detail": "RAW-RUNTIME-MEMORY"},
        "constraints": ["RAW-LEGACY-CONSTRAINT"],
    }
    return project_snapshot_for_generation(raw_snapshot, view), view


def _director_snapshot(
    snapshot: dict,
) -> tuple[dict, list[dict[str, str]]]:
    director = ModelDirector(completion=lambda _messages: "{}")
    messages = director._messages(snapshot, {})
    payload = json.loads(messages[1]["content"])
    return payload["snapshot"], messages


def _validator_snapshot(
    snapshot: dict,
) -> tuple[dict, list[dict[str, str]]]:
    messages = _llm_messages(
        snapshot,
        "The countdown continued.",
        {"chapter_index": 18},
    )
    payload = json.loads(messages[1]["content"])
    return payload["snapshot"], messages


class ModelStageGenerationViewTests(unittest.TestCase):
    def test_director_and_validator_receive_the_same_bounded_working_set(
        self,
    ) -> None:
        snapshot, view = _bounded_snapshot()

        director_snapshot, director_messages = _director_snapshot(snapshot)
        validator_snapshot, validator_messages = _validator_snapshot(snapshot)

        self.assertEqual(snapshot, director_snapshot)
        self.assertEqual(snapshot, validator_snapshot)
        self.assertEqual(director_snapshot, validator_snapshot)
        self.assertEqual(
            view["read_set_digest"],
            director_snapshot["generation_state_view"]["read_set_digest"],
        )
        self.assertEqual(
            view["projection_sha256"],
            validator_snapshot["generation_state_view"][
                "projection_sha256"
            ],
        )
        self.assertEqual(
            "shared shelter",
            validator_snapshot["relationships"]["hero->ally"]["boundary"],
        )
        self.assertEqual(
            3,
            validator_snapshot["roster"]["team"]["declared_count"],
        )
        self.assertEqual(
            6,
            validator_snapshot["numeric_counters"]["erosion"][
                "current_value"
            ],
        )
        self.assertEqual(
            8,
            validator_snapshot["inventory"]["hero:ammo"]["quantity"],
        )
        self.assertIn(
            "chapter-0017-beat-004",
            validator_snapshot["required_events"],
        )
        self.assertEqual(
            "seeker-001",
            validator_snapshot["expected_new_entities"][0]["entity_id"],
        )

        rendered = json.dumps(
            [director_messages, validator_messages],
            ensure_ascii=False,
            sort_keys=True,
        )
        for sentinel in (
            "RAW-WORLD-DOCUMENT",
            "RAW-WORLD-LOCATION",
            "RAW-LAST-CHAPTER",
            "RAW-FULL-CHARACTER",
            "RAW-TIMELINE-HISTORY",
            "RAW-RUNTIME-MEMORY",
            "RAW-LEGACY-CONSTRAINT",
        ):
            self.assertNotIn(sentinel, rendered)
        self.assertNotIn("authoritative_state", director_snapshot)
        self.assertNotIn("authoritative_state", validator_snapshot)
        self.assertNotIn("timeline", director_snapshot)
        self.assertNotIn("timeline", validator_snapshot)
        self.assertNotIn("memory", director_snapshot)
        self.assertNotIn("memory", validator_snapshot)

        self.assertEqual(
            director_messages,
            _director_snapshot(snapshot)[1],
        )
        self.assertEqual(
            validator_messages,
            _validator_snapshot(snapshot)[1],
        )

    def test_validator_rejects_accidental_raw_expansion_in_projected_snapshot(
        self,
    ) -> None:
        snapshot, _view = _bounded_snapshot()
        tainted = copy.deepcopy(snapshot)
        tainted["authoritative_state"] = {"detail": "RAW-AUTHORITY-SENTINEL"}
        tainted["timeline"] = [{"detail": "RAW-TIMELINE-SENTINEL"}]
        tainted["memory"] = {"detail": "RAW-MEMORY-SENTINEL"}
        tainted["characters"]["intruder"] = {
            "detail": "RAW-INTRUDER-SENTINEL"
        }
        tainted["world_state"]["text"] = "RAW-WORLD-SENTINEL"
        tainted["world_state"]["locations"]["intruder-space"] = {
            "detail": "RAW-SPACE-SENTINEL"
        }
        tainted["authority_summary"]["raw"] = "RAW-AUTHORITY-SUMMARY"
        tainted["timeline_summary"]["raw"] = "RAW-TIMELINE-SUMMARY"
        tainted["generation_state_view"]["raw"] = "RAW-VIEW-SUMMARY"

        projected, messages = _validator_snapshot(tainted)
        rendered = json.dumps(messages, ensure_ascii=False, sort_keys=True)

        self.assertEqual({"hero"}, set(projected["characters"]))
        self.assertEqual({"radio-room"}, set(projected["world_state"]["locations"]))
        for sentinel in (
            "RAW-AUTHORITY-SENTINEL",
            "RAW-TIMELINE-SENTINEL",
            "RAW-MEMORY-SENTINEL",
            "RAW-INTRUDER-SENTINEL",
            "RAW-WORLD-SENTINEL",
            "RAW-SPACE-SENTINEL",
            "RAW-AUTHORITY-SUMMARY",
            "RAW-TIMELINE-SUMMARY",
            "RAW-VIEW-SUMMARY",
        ):
            self.assertNotIn(sentinel, rendered)


if __name__ == "__main__":
    unittest.main()
