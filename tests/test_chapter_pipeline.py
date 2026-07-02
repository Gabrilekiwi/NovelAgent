from __future__ import annotations

import unittest

from core.schema import validate_schema
from modules.chapter_generator import run_chapter_pipeline


class ChapterPipelineTest(unittest.TestCase):
    def test_dry_run_scene_limit_bounds_scene_drafts(self) -> None:
        pipeline = run_chapter_pipeline(
            "Input pack for a smoke-sized chapter generation check.",
            chapter_index=2,
            dry_run=True,
            scene_limit=1,
        )

        self.assertIs(pipeline, validate_schema(pipeline, "chapter_pipeline.schema.json"))
        self.assertEqual(1, len(pipeline["plan"]["scenes"]))
        self.assertEqual(1, len(pipeline["scene_drafts"]))
        self.assertEqual(1, len(pipeline["scene_spans"]))
        self.assertEqual("opening_bridge", pipeline["plan"]["scenes"][0]["type"])
        self.assertEqual("Continue directly from last_chapter_ending", pipeline["plan"]["scenes"][0]["goal"])
        self.assertEqual(
            [
                "repeat last known location",
                "show immediate consequence",
                "explain transition before new scene",
            ],
            pipeline["plan"]["scenes"][0]["required_beats"],
        )
        span = pipeline["scene_spans"][0]
        scene_text = pipeline["scene_drafts"][0]["text"]
        self.assertEqual(0, span["start_char"])
        self.assertEqual(len(scene_text), span["end_char"])
        self.assertEqual(scene_text, pipeline["merged_chapter"][span["start_char"]:span["end_char"]])
        self.assertEqual(1, pipeline["stages"][0]["summary"]["scene_count"])
        self.assertEqual(1, pipeline["stages"][1]["summary"]["scene_count"])


if __name__ == "__main__":
    unittest.main()
