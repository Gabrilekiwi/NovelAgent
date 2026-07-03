from __future__ import annotations

import unittest

from api.contracts import ModelOutputError
from core.schema import validate_schema
from modules.chapter_generator import run_chapter_pipeline
import modules.chapter_generator.pipeline as pipeline_module


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

    def test_model_plan_accepts_fenced_json_response(self) -> None:
        original_chat_completion = pipeline_module.chat_completion
        pipeline_module.chat_completion = lambda messages, **kwargs: """```json
{"goal": "Open the first rift.", "scenes": [{"index": 1, "type": "opening_bridge", "goal": "Begin at the observatory.", "required_beats": ["old observatory", "danger"]}]}
```"""
        try:
            plan = pipeline_module.plan_chapter("input pack", chapter_index=1, dry_run=False)
        finally:
            pipeline_module.chat_completion = original_chat_completion

        self.assertEqual("Open the first rift.", plan["goal"])
        self.assertEqual("opening_bridge", plan["scenes"][0]["type"])

    def test_model_plan_accepts_json_embedded_in_text(self) -> None:
        original_chat_completion = pipeline_module.chat_completion
        pipeline_module.chat_completion = lambda messages, **kwargs: (
            "Here is the plan:\n"
            '{"goal": "Enter the mirror waste.", "scenes": [{"index": 1, "goal": "Start from the last state.", "required_beats": ["bridge"]}]}'
        )
        try:
            plan = pipeline_module.plan_chapter("input pack", chapter_index=1, dry_run=False)
        finally:
            pipeline_module.chat_completion = original_chat_completion

        self.assertEqual("Enter the mirror waste.", plan["goal"])

    def test_scene_generation_respects_configured_chinese_language(self) -> None:
        original_chat_completion = pipeline_module.chat_completion
        pipeline_module.chat_completion = lambda messages, **kwargs: "The ferry crossed the black water."
        try:
            with self.assertRaisesRegex(ModelOutputError, "Simplified Chinese"):
                pipeline_module.generate_scenes(
                    "input pack",
                    {
                        "goal": "continue",
                        "scenes": [{"index": 1, "goal": "continue", "required_beats": ["bridge"]}],
                    },
                    dry_run=False,
                    language="zh-CN",
                )
        finally:
            pipeline_module.chat_completion = original_chat_completion


if __name__ == "__main__":
    unittest.main()
