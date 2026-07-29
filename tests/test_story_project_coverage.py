from __future__ import annotations

import unittest

from core.story_project.coverage import (
    build_blueprint_coverage,
    build_blueprint_plan,
    validate_blueprint_coverage,
)
from modules.chapter_generator.pipeline import plan_scenes


def _nine_beat_blueprint() -> dict:
    return {
        "chapter_index": 18,
        "outline_path": "book/大纲/细纲_第018章.md",
        "title": "警报里的名字",
        "core_event": "确认防空警报中的自主信号并定位节点四",
        "required_beats": [
            {"index": index, "text": f"按顺序完成第{index}个唯一剧情节拍"}
            for index in range(1, 10)
        ],
        "ending_pressure": "节点四开始倒计时",
        "source_path": "book/大纲/细纲_第018章.md",
        "missing_fields": [],
    }


def _beat_index_groups(plan: dict) -> list[list[int]]:
    return [
        [int(value) for value in scene["required_beat_indexes"]]
        for scene in plan["scenes"]
    ]


class StoryProjectCoverageTests(unittest.TestCase):
    def test_default_groups_nine_beats_into_four_contiguous_balanced_scenes(
        self,
    ) -> None:
        plan = build_blueprint_plan(_nine_beat_blueprint())

        self.assertEqual(
            [[1, 2, 3], [4, 5], [6, 7], [8, 9]],
            _beat_index_groups(plan),
        )
        self.assertEqual(
            [
                [f"按顺序完成第{index}个唯一剧情节拍" for index in (1, 2, 3)],
                [f"按顺序完成第{index}个唯一剧情节拍" for index in (4, 5)],
                [f"按顺序完成第{index}个唯一剧情节拍" for index in (6, 7)],
                [f"按顺序完成第{index}个唯一剧情节拍" for index in (8, 9)],
            ],
            [scene["required_beats"] for scene in plan["scenes"]],
        )

    def test_scene_limit_only_tightens_four_scene_default_and_preserves_order(
        self,
    ) -> None:
        expected = {
            1: [[1, 2, 3, 4, 5, 6, 7, 8, 9]],
            2: [[1, 2, 3, 4, 5], [6, 7, 8, 9]],
            3: [[1, 2, 3], [4, 5, 6], [7, 8, 9]],
            4: [[1, 2, 3], [4, 5], [6, 7], [8, 9]],
            9: [[1, 2, 3], [4, 5], [6, 7], [8, 9]],
        }
        blueprint = _nine_beat_blueprint()

        for scene_limit, expected_groups in expected.items():
            with self.subTest(scene_limit=scene_limit):
                plan = build_blueprint_plan(
                    blueprint,
                    scene_limit=scene_limit,
                )
                groups = _beat_index_groups(plan)
                flattened = [
                    beat_index
                    for group in groups
                    for beat_index in group
                ]

                self.assertEqual(expected_groups, groups)
                self.assertEqual(list(range(1, 10)), flattened)
                self.assertEqual(len(flattened), len(set(flattened)))
                sizes = [len(group) for group in groups]
                self.assertLessEqual(max(sizes) - min(sizes), 1)
                self.assertLessEqual(len(groups), 4)

    def test_grouping_keeps_stable_beat_event_ids_and_full_coverage(
        self,
    ) -> None:
        blueprint = _nine_beat_blueprint()
        expected_event_ids = [
            f"chapter-0018-beat-{index:03d}"
            for index in range(1, 10)
        ]

        for scene_limit in (1, 2, 3, 4, 9):
            with self.subTest(scene_limit=scene_limit):
                plan = plan_scenes(
                    "StoryProject plan construction is deterministic.",
                    chapter_index=18,
                    chapter_blueprint=blueprint,
                    scene_limit=scene_limit,
                )
                flattened_indexes: list[int] = []
                flattened_event_ids: list[str] = []
                prior_event_ids: list[str] = []
                scene_drafts: list[dict] = []
                for scene in plan["scenes"]:
                    beat_indexes = [
                        int(value)
                        for value in scene["required_beat_indexes"]
                    ]
                    scene_event_ids = [
                        str(value)
                        for value in scene["required_event_ids"]
                    ]
                    expected_scene_event_ids = [
                        f"chapter-0018-beat-{index:03d}"
                        for index in beat_indexes
                    ]

                    self.assertEqual(
                        expected_scene_event_ids,
                        scene_event_ids,
                    )
                    self.assertEqual(
                        prior_event_ids,
                        scene["forbidden_event_ids"],
                    )
                    flattened_indexes.extend(beat_indexes)
                    flattened_event_ids.extend(scene_event_ids)
                    prior_event_ids.extend(scene_event_ids)
                    scene_drafts.append(
                        {
                            "covered_beat_indexes": beat_indexes,
                        }
                    )

                self.assertEqual(list(range(1, 10)), flattened_indexes)
                self.assertEqual(expected_event_ids, flattened_event_ids)
                self.assertEqual(
                    len(flattened_event_ids),
                    len(set(flattened_event_ids)),
                )

                merged_chapter = "\n".join(
                    [
                        *[
                            str(beat["text"])
                            for beat in blueprint["required_beats"]
                        ],
                        str(blueprint["ending_pressure"]),
                    ]
                )
                coverage = build_blueprint_coverage(
                    blueprint,
                    scene_drafts,
                    merged_chapter,
                )
                validation = validate_blueprint_coverage(
                    blueprint,
                    coverage,
                )

                self.assertEqual(
                    list(range(1, 10)),
                    coverage["covered_beat_indexes"],
                )
                self.assertEqual([], coverage["missing_beat_indexes"])
                self.assertTrue(coverage["ending_pressure_covered"])
                self.assertTrue(validation["ok"])
                self.assertEqual([], validation["problems"])


if __name__ == "__main__":
    unittest.main()
