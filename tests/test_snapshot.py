from __future__ import annotations

import unittest

from core.schema import validate_schema
from core.state.snapshot import (
    SnapshotError,
    build_state_update_audit,
    normalize_snapshot,
    update_snapshot,
    validate_snapshot,
)


class SnapshotTest(unittest.TestCase):
    def test_normalize_fills_missing_runtime_fields(self) -> None:
        snapshot = normalize_snapshot({"chapter_index": 1})

        self.assertEqual(1, snapshot["chapter_index"])
        self.assertEqual({}, snapshot["characters"])
        self.assertEqual([], snapshot["timeline"])
        self.assertEqual({}, snapshot["world_state"]["locations"])
        self.assertEqual("", snapshot["story_state"]["last_chapter_ending"])
        self.assertEqual({}, snapshot["spatial_state"]["spaces"])

    def test_rejects_non_object_locations(self) -> None:
        with self.assertRaises(SnapshotError):
            normalize_snapshot(
                {
                    "chapter_index": 1,
                    "world_state": {"locations": []},
                    "characters": {},
                    "timeline": [],
                }
            )

    def test_rejects_zero_chapter_index(self) -> None:
        with self.assertRaises(SnapshotError):
            normalize_snapshot({"chapter_index": 0})

    def test_rejects_non_object_timeline_entries(self) -> None:
        with self.assertRaises(SnapshotError):
            validate_snapshot(
                {
                    "chapter_index": 1,
                    "world_state": {"locations": {}},
                    "characters": {},
                    "timeline": ["bad-entry"],
                }
            )

    def test_update_snapshot_merges_analysis_locations_and_world_changes(self) -> None:
        snapshot = normalize_snapshot({"chapter_index": 2})
        updated = update_snapshot(
            snapshot,
            {
                "summary": "A shelter conflict escalates.",
                "events": [{"text": "The alarm sounded."}],
                "world_changes": [{"type": "infection_pressure"}],
                "new_locations": ["shelter"],
                "story_state": {
                    "last_chapter_ending": "Mira was injured at the shelter.",
                    "last_scene_location": "shelter",
                    "last_scene_characters": ["Mira"],
                    "open_threads": ["Mira needs help."],
                    "required_opening_bridge": "Continue from shelter",
                },
                "spatial_state": {
                    "spaces": {"shelter": {"risk": "rising"}},
                    "connections": [{"from": "shelter", "to": "sealed gate"}],
                    "character_positions": {"Mira": "shelter"},
                    "blocked_paths": [],
                    "last_transition": {"from": "shelter", "to": "sealed gate"},
                },
                "character_changes": [
                    {
                        "name": "Mira",
                        "status": "injured",
                        "current_location": "shelter",
                        "text": "Mira was injured at the shelter.",
                    }
                ],
                "conflicts": ["conflict"],
                "validation_ok": True,
            },
            {"ok": True},
            source_run_id="chapter_2_run",
        )

        self.assertEqual(3, updated["chapter_index"])
        self.assertEqual(2, updated["world_state"]["locations"]["shelter"]["first_seen_chapter"])
        self.assertEqual("chapter_analysis", updated["world_state"]["locations"]["shelter"]["source"])
        self.assertEqual([{"type": "infection_pressure"}], updated["world_state"]["last_world_changes"])
        self.assertEqual("injured", updated["characters"]["Mira"]["status"])
        self.assertEqual("shelter", updated["characters"]["Mira"]["current_location"])
        self.assertEqual(2, updated["characters"]["Mira"]["last_seen_chapter"])
        self.assertEqual("Mira was injured at the shelter.", updated["story_state"]["last_chapter_ending"])
        self.assertEqual("shelter", updated["story_state"]["last_scene_location"])
        self.assertEqual("Mira", updated["story_state"]["last_scene_characters"][0])
        self.assertEqual("shelter", updated["spatial_state"]["character_positions"]["Mira"])
        self.assertEqual(2, updated["spatial_state"]["spaces"]["shelter"]["last_seen_chapter"])
        self.assertEqual("A shelter conflict escalates.", updated["timeline"][0]["summary"])
        self.assertEqual("shelter", updated["timeline"][0]["story_state"]["last_scene_location"])
        self.assertEqual("shelter", updated["timeline"][0]["spatial_state"]["character_positions"]["Mira"])
        self.assertEqual("chapter_2:timeline_event:chapter_2_summary", updated["timeline"][0]["memory_id"])
        self.assertEqual(
            [
                "chapter_2:timeline_event:chapter_2_summary",
                "chapter_2:timeline_event:chapter_2_event_1",
            ],
            updated["timeline"][0]["memory_ids"],
        )
        self.assertEqual("chapter_2_run", updated["timeline"][0]["source_run_id"])

    def test_update_snapshot_does_not_mutate_input_snapshot(self) -> None:
        snapshot = normalize_snapshot({"chapter_index": 2, "timeline": []})

        updated = update_snapshot(
            snapshot,
            {
                "summary": "A shelter conflict escalates.",
                "events": [],
                "world_changes": [],
                "new_locations": [],
                "character_changes": [],
                "conflicts": [],
                "validation_ok": True,
            },
            {"ok": True},
        )

        self.assertEqual([], snapshot["timeline"])
        self.assertEqual(1, len(updated["timeline"]))

    def test_update_snapshot_rejects_invalid_analysis_contract(self) -> None:
        snapshot = normalize_snapshot({"chapter_index": 2})

        with self.assertRaisesRegex(SnapshotError, "analysis_result.schema.json"):
            update_snapshot(
                snapshot,
                {
                    "summary": "A shelter conflict escalates.",
                    "events": [],
                    "world_changes": [],
                    "new_locations": [],
                    "character_changes": [],
                    "conflicts": [],
                },
                {"ok": True},
            )

    def test_build_state_update_audit_summarizes_commit_effects(self) -> None:
        snapshot = normalize_snapshot({"chapter_index": 2})
        analysis = {
            "summary": "A shelter conflict escalates.",
            "events": [{"text": "The alarm sounded."}],
            "world_changes": [{"type": "infection_pressure"}],
            "new_locations": ["shelter"],
            "character_changes": [{"name": "Mira", "status": "injured"}],
            "conflicts": ["conflict"],
            "validation_ok": True,
        }
        updated = update_snapshot(snapshot, analysis, {"ok": True})
        audit = build_state_update_audit(
            snapshot=snapshot,
            next_snapshot=updated,
            analysis=analysis,
            memory_updates=[
                {"type": "timeline_event", "data": {}},
                {"type": "character", "data": {}},
                {"type": "location", "data": {}},
            ],
            applied=True,
        )

        self.assertTrue(audit["applied"])
        self.assertEqual(2, audit["chapter_index"])
        self.assertEqual(3, audit["next_chapter_index"])
        self.assertEqual(1, audit["timeline_added"])
        self.assertEqual(1, audit["character_update_count"])
        self.assertEqual(1, audit["location_update_count"])
        self.assertEqual(1, audit["world_change_count"])
        self.assertFalse(audit["story_state_updated"])
        self.assertFalse(audit["spatial_state_updated"])
        self.assertEqual(3, audit["memory_update_count"])
        self.assertIn({"type": "timeline_event", "count": 1}, audit["memory_update_types"])
        self.assertIs(audit, validate_schema(audit, "state_update_audit.schema.json"))

    def test_build_state_update_audit_rejects_invalid_analysis_contract(self) -> None:
        snapshot = normalize_snapshot({"chapter_index": 2})

        with self.assertRaisesRegex(SnapshotError, "analysis_result.schema.json"):
            build_state_update_audit(
                snapshot=snapshot,
                next_snapshot=snapshot,
                analysis={
                    "summary": "A shelter conflict escalates.",
                    "events": [],
                    "world_changes": [],
                    "new_locations": [],
                    "character_changes": [],
                    "conflicts": [],
                    "unexpected": True,
                },
                applied=False,
            )


if __name__ == "__main__":
    unittest.main()
