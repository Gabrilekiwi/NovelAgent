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

    def test_last_scene_follows_new_ending_location_not_known_opening(self) -> None:
        chapter = (
            "上一轮通信结束后，陆沉和顾岚仍留在未登记电缆夹层靠外盲区，守着预警纸。"
            "苏晴说必须等顾岚复核之后才能动。"
            "两人确认靠外夹层已经清空，才沿复核过的低矮段撤进废弃管廊支仓。"
            "陆沉守在支仓外侧的标线后，等待下一次定时联络。"
            "脱离扫描区不过是换了一个落脚处，算不上进入安全基地。"
            "门外，水流撞上废管，发出一声比刚才更近的空响。"
        )
        snapshot = {
            "project_profile": {
                "known_characters": ["陆沉", "顾岚"],
                "known_locations": ["未登记电缆夹层靠外盲区"],
            }
        }

        analysis = analyze_chapter(chapter, {"ok": True}, snapshot=snapshot)

        self.assertEqual("废弃管廊支仓", analysis["story_state"]["last_scene_location"])
        self.assertIn("废弃管廊支仓", analysis["new_locations"])
        self.assertNotEqual("未登记电缆夹层靠外盲区", analysis["story_state"]["last_scene_location"])
        self.assertNotIn("安全基地", analysis["story_state"]["last_scene_location"])
        self.assertIn("废弃管廊支仓", analysis["story_state"]["required_opening_bridge"])
        self.assertEqual(
            {"to": "废弃管廊支仓", "source": "chapter_analysis"},
            analysis["spatial_state"]["last_transition"],
        )

    def test_movement_destination_starting_with_embedded_particle_does_not_crash(self) -> None:
        # "另一侧折返点" contains the trailing particle "一侧" at index 1.
        # Trimming must keep the full name instead of calling min() on an empty cut list.
        from modules.conflict_engine.analyzer import _trim_movement_destination

        self.assertEqual("另一侧折返点", _trim_movement_destination("另一侧折返点"))
        self.assertEqual("废弃管廊支仓", _trim_movement_destination("废弃管廊支仓外侧"))
        self.assertEqual("外侧", _trim_movement_destination("外侧"))

        chapter = (
            "医院侧递送者只走到另一侧折返点，中间靠无人交接。"
            "陆沉仍守在废弃管廊支仓，等待下一次定时联络。"
        )
        snapshot = {
            "project_profile": {
                "known_characters": ["陆沉"],
                "known_locations": ["废弃管廊支仓"],
            }
        }
        analysis = analyze_chapter(chapter, {"ok": True}, snapshot=snapshot)
        self.assertIn("另一侧折返点", analysis["new_locations"])
        self.assertEqual("废弃管廊支仓", analysis["story_state"]["last_scene_location"])


if __name__ == "__main__":
    unittest.main()


class LocationMentionIndexTest(unittest.TestCase):
    def test_optional_de_particle_matches_canonical_location(self) -> None:
        from modules.conflict_engine.analyzer import (
            _extract_candidate_locations,
            _last_known_location,
            _location_mention_index,
        )

        prose = "两人转入人防医院支线外侧的隔离撤回口，没有直接进入病区。"
        canonical = "人防医院支线外侧隔离撤回口"
        self.assertGreaterEqual(_location_mention_index(prose, canonical), 0)
        self.assertIn(canonical, _extract_candidate_locations(prose, [canonical, "北侧维护通道"]))
        self.assertEqual(
            canonical,
            _last_known_location(prose, [], [canonical, "北侧维护通道"]),
        )

    def test_exact_location_still_preferred_when_present(self) -> None:
        from modules.conflict_engine.analyzer import _location_mention_index

        prose = "他们停在北侧维护通道。随后转入人防医院支线外侧隔离撤回口。"
        self.assertGreater(
            _location_mention_index(prose, "人防医院支线外侧隔离撤回口"),
            _location_mention_index(prose, "北侧维护通道"),
        )


class DiscourseLocationTest(unittest.TestCase):
    def test_last_scene_ignores_zuihou_adverb_even_when_already_known(self) -> None:
        chapter = (
            "二层通信室里，林夏接通了通往消防站侧首段递送路线的有线回路。"
            "陆沉和顾岚守在线路另一侧，没有开无线电。"
            "韩野把旧取水记录压回图上。"
            "屋里只剩手摇机组模糊的摩擦声——没有托片离开消防站侧，也没有一条求援被送往医院方向。"
            "最后，她把旧取水图重新摊回桌面。手动阀旁那行褪色的小字依旧清晰。"
            "静默的边界已经守住，真正留给他们的，仍是阀口那一分钟——任何记录都覆盖不了的一分钟。"
        )
        snapshot = {
            "project_profile": {
                "known_characters": ["林夏", "陆沉", "顾岚", "韩野"],
                "known_locations": [
                    "最后",
                    "的人",
                    "和撤回",
                    "原处",
                    "二层通信室",
                    "消防站侧首段递送路线",
                    "消防站侧",
                ],
            }
        }

        analysis = analyze_chapter(chapter, {"ok": True}, snapshot=snapshot)

        self.assertNotEqual("最后", analysis["story_state"]["last_scene_location"])
        self.assertNotIn("最后", analysis["new_locations"])
        self.assertNotIn("的人", analysis["new_locations"])
        self.assertNotIn("和撤回", analysis["new_locations"])
        self.assertNotIn("最后", analysis["story_state"]["required_opening_bridge"])
        self.assertIn(
            analysis["story_state"]["last_scene_location"],
            {"消防站侧", "消防站侧首段递送路线", "二层通信室"},
        )
        self.assertNotEqual(
            {"to": "最后", "source": "chapter_analysis"},
            analysis["spatial_state"]["last_transition"],
        )


class UsableLocationNameTest(unittest.TestCase):
    def test_rejects_discourse_and_grammatical_fragments(self) -> None:
        from core.location_names import is_usable_location_name

        self.assertFalse(is_usable_location_name("最后"))
        self.assertFalse(is_usable_location_name("的人"))
        self.assertFalse(is_usable_location_name("和撤回"))
        self.assertFalse(is_usable_location_name("然后"))
        self.assertFalse(is_usable_location_name(""))
        self.assertTrue(is_usable_location_name("消防站侧首段递送路线"))
        self.assertTrue(is_usable_location_name("二层通信室"))
        self.assertTrue(is_usable_location_name("废弃管廊支仓"))
