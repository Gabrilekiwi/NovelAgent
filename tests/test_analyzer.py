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

    def test_extracts_minimum_chinese_character_and_location_changes(self) -> None:
        chapter = (
            "林雪在A线车厢听见第二次警报。"
            "她必须选择是否打开备用门。"
            "林雪退到备用通道，车厢后的灯全部熄灭。"
        )

        analysis = analyze_chapter(chapter, {"ok": True})

        self.assertIn("A线车厢", analysis["new_locations"])
        self.assertIn("备用通道", analysis["new_locations"])
        self.assertIn(
            {
                "name": "林雪",
                "current_location": "A线车厢",
                "text": "林雪在A线车厢听见第二次警报。",
            },
            analysis["character_changes"],
        )
        self.assertIn(
            {
                "name": "林雪",
                "current_location": "备用通道",
                "text": "林雪退到备用通道，车厢后的灯全部熄灭。",
            },
            analysis["character_changes"],
        )
        self.assertEqual("备用通道", analysis["story_state"]["last_scene_location"])
        self.assertEqual("备用通道", analysis["spatial_state"]["character_positions"]["林雪"])
        self.assertEqual({"to": "备用通道", "source": "chapter_analysis"}, analysis["spatial_state"]["last_transition"])

    def test_chinese_analyzer_does_not_invent_characters_from_phrases(self) -> None:
        chapter = (
            "幸存岛，陆砚仍站在废灯塔里；旧影像在白墙上熄灭后，黑暗并没有立刻回来。"
            "门票在潮里，不在岛上。火雨辨伪在陆砚眼里轻轻一跳。"
            "陆砚和阿照一起坠入市街，黑月集市不是单纯的地点，它本身就是交易。"
            "银面具抬起头，声音像秤砣落地。"
            "价高者，可上第七码头；价低者，留市抵债。"
        )

        analysis = analyze_chapter(chapter, {"ok": True})

        names = {change["name"] for change in analysis["character_changes"]}
        self.assertEqual({"陆砚"}, names)
        self.assertNotIn("旧影像", names)
        self.assertNotIn("门票", names)
        self.assertEqual("第七码头", analysis["story_state"]["last_scene_location"])
        self.assertEqual("第七码头 陆砚", analysis["story_state"]["required_opening_bridge"])
        self.assertEqual("第七码头", analysis["spatial_state"]["character_positions"]["陆砚"])

    def test_chinese_analyzer_uses_snapshot_project_profile_terms(self) -> None:
        chapter = "顾北在星槎港听见钟声。顾北进入回潮桥，桥下的光突然熄灭。"
        snapshot = {
            "project_profile": {
                "known_characters": ["顾北"],
                "known_locations": ["星槎港", "回潮桥"],
            }
        }

        analysis = analyze_chapter(chapter, {"ok": True}, snapshot=snapshot)

        self.assertEqual("回潮桥", analysis["story_state"]["last_scene_location"])
        self.assertEqual("回潮桥", analysis["spatial_state"]["character_positions"]["顾北"])


if __name__ == "__main__":
    unittest.main()
