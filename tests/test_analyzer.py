from __future__ import annotations

import unittest

from core.schema import validate_schema
from modules.conflict_engine import analyze_chapter


class AnalyzerTest(unittest.TestCase):
    def test_extracts_summary_events_world_changes_and_locations(self) -> None:
        chapter = (
            "The first alarm sounded in the shelter. "
            "The protagonist had to choose between the serum and a rescue, creating open conflict. "
            "A new infection zone cut through the corridor."
        )

        analysis = analyze_chapter(chapter, {"ok": True})

        self.assertTrue(analysis["summary"])
        self.assertGreaterEqual(len(analysis["events"]), 1)
        self.assertIn("infection_pressure", {item["type"] for item in analysis["world_changes"]})
        self.assertIn("serum_focus", {item["type"] for item in analysis["world_changes"]})
        self.assertIn("shelter", analysis["new_locations"])
        self.assertEqual("corridor", analysis["story_state"]["last_scene_location"])
        self.assertIn("infection zone", analysis["story_state"]["open_threads"][0])
        self.assertIn("corridor", analysis["spatial_state"]["spaces"])
        self.assertTrue(analysis["validation_ok"])
        self.assertIs(analysis, validate_schema(analysis, "analysis_result.schema.json"))

    def test_extracts_character_status_and_location_changes(self) -> None:
        chapter = (
            "Mira was injured during the rescue. "
            "Jon returned to the shelter before the infection gate closed."
        )

        analysis = analyze_chapter(chapter, {"ok": True})

        self.assertIn(
            {"name": "Mira", "status": "injured", "text": "Mira was injured during the rescue."},
            analysis["character_changes"],
        )
        self.assertIn(
            {
                "name": "Jon",
                "current_location": "shelter",
                "text": "Jon returned to the shelter before the infection gate closed.",
            },
            analysis["character_changes"],
        )


if __name__ == "__main__":
    unittest.main()
