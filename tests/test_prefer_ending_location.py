from __future__ import annotations

import unittest

from core.engine.executor import _prefer_ending_consistent_location


class PreferEndingConsistentLocationTest(unittest.TestCase):
    def test_does_not_prefer_discourse_adverb_over_scene_final(self) -> None:
        chapter = (
            "陆沉把配给表压在消防站侧首段递送路线的旧取水图旁。"
            "屋里只剩手摇机组模糊的摩擦声。"
            "最后，她把旧取水图重新摊回桌面。"
        )
        self.assertEqual(
            "消防站侧首段递送路线",
            _prefer_ending_consistent_location(
                scene_location="消防站侧首段递送路线",
                analyzer_location="最后",
                chapter_text=chapter,
            ),
        )

    def test_keeps_more_specific_scene_location_when_analyzer_is_a_stem(self) -> None:
        chapter = (
            "陆沉把配给表压在消防站侧首段递送路线的旧取水图旁。"
            "没有托片离开消防站侧。"
        )
        self.assertEqual(
            "消防站侧首段递送路线",
            _prefer_ending_consistent_location(
                scene_location="消防站侧首段递送路线",
                analyzer_location="消防站侧",
                chapter_text=chapter,
            ),
        )
