from __future__ import annotations

import json
import unittest

from api.contracts import ModelOutputError
from core.schema import validate_schema
from core.story_project.coverage import validate_blueprint_coverage
from modules.chapter_generator import run_chapter_pipeline
import modules.chapter_generator.pipeline as pipeline_module


class ChapterPipelineTest(unittest.TestCase):
    def test_plan_chapter_is_compatibility_alias_for_plan_scenes(self) -> None:
        expected = pipeline_module.plan_scenes("input pack", chapter_index=7, dry_run=True)

        self.assertEqual(
            expected,
            pipeline_module.plan_chapter("input pack", chapter_index=7, dry_run=True),
        )

    def _blueprint(self) -> dict:
        return {
            "chapter_index": 3,
            "outline_path": "book/大纲/细纲_第003章.md",
            "title": "Pressure Test",
            "core_event": "The crew enters the sealed station.",
            "required_beats": [
                {"index": 1, "text": "open the sealed station"},
                {"index": 2, "text": "discover the missing signal"},
                {"index": 3, "text": "choose who carries the serum"},
            ],
            "ending_pressure": "the signal starts counting down",
            "source_path": "book/大纲/细纲_第003章.md",
            "missing_fields": [],
        }

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
        self.assertIsNone(pipeline.get("story_project"))
        self.assertIsNone(pipeline.get("chapter_blueprint"))
        self.assertIsNone(pipeline.get("blueprint_coverage"))

    def test_story_project_scene_limit_one_keeps_all_required_beats(self) -> None:
        pipeline = run_chapter_pipeline(
            "StoryProject input pack.",
            chapter_index=3,
            dry_run=True,
            scene_limit=1,
            chapter_blueprint=self._blueprint(),
        )

        self.assertEqual(1, len(pipeline["plan"]["scenes"]))
        self.assertEqual([1, 2, 3], pipeline["plan"]["scenes"][0]["required_beat_indexes"])
        self.assertEqual([1, 2, 3], pipeline["scene_drafts"][0]["covered_beat_indexes"])
        self.assertEqual([], pipeline["blueprint_coverage"]["missing_beat_indexes"])
        self.assertEqual([1, 2, 3], pipeline["blueprint_coverage"]["covered_beat_indexes"])
        self.assertTrue(pipeline["blueprint_coverage"]["ending_pressure_covered"])

    def test_scene_request_bounds_zh_chapter_length_and_warns_against_restarts(self) -> None:
        payload = json.loads(
            pipeline_module._scene_request_payload(
                input_pack="context",
                plan={"scenes": [{"index": 1}, {"index": 2}, {"index": 3}]},
                scene={"index": 1},
                scene_required_beats=[],
                blueprint=None,
            )
        )

        self.assertIn("1000-1500 Chinese characters", payload["instruction"])
        self.assertIn("Do not restart, duplicate, or retell", payload["instruction"])

    def test_scene_request_compacts_large_sections_and_drops_memory_index(self) -> None:
        context = "\n\n".join(
            [f"# Section {index}\nHEAD-{index}\n" + (str(index) * 5_000) + f"\nTAIL-{index}" for index in range(8)]
            + ["# Memory Index\n" + ("memory" * 1_000)]
        )

        payload = json.loads(
            pipeline_module._scene_request_payload(
                input_pack=context,
                plan={"scenes": [{"index": 1}]},
                scene={"index": 1},
                scene_required_beats=[],
                blueprint=None,
            )
        )

        self.assertNotIn("Memory Index", payload["shared_context"])
        self.assertIn("完整条目已省略", payload["shared_context"])
        self.assertIn("TAIL-7", payload["shared_context"])
        self.assertLessEqual(len(payload["shared_context"]), 1_500 * 7)
        manifest = json.loads(payload["shared_context"].split("# Structured Context Manifest\n", 1)[1])
        self.assertEqual(len(context), manifest["original_chars"])
        self.assertEqual(64, len(manifest["source_sha256"]))
        self.assertTrue(manifest["selected_items"])

    def test_story_project_plan_does_not_call_model_planner(self) -> None:
        original_chat_completion = pipeline_module.chat_completion

        def fail_if_called(*args, **kwargs):
            raise AssertionError("StoryProject planning must not call OpenAI")

        pipeline_module.chat_completion = fail_if_called
        try:
            plan = pipeline_module.plan_chapter(
                "input pack",
                chapter_index=3,
                dry_run=False,
                chapter_blueprint=self._blueprint(),
            )
        finally:
            pipeline_module.chat_completion = original_chat_completion

        self.assertEqual("The crew enters the sealed station.", plan["goal"])
        self.assertEqual([1], plan["scenes"][0]["required_beat_indexes"])

    def test_story_project_generation_blocks_missing_ending_pressure(self) -> None:
        blueprint = self._blueprint()
        blueprint["ending_pressure"] = None
        blueprint["missing_fields"] = ["ending_pressure"]

        with self.assertRaisesRegex(ValueError, "ending_pressure"):
            run_chapter_pipeline(
                "StoryProject input pack.",
                chapter_index=3,
                dry_run=True,
                chapter_blueprint=blueprint,
            )

    def test_story_project_missing_coverage_can_be_validated(self) -> None:
        blueprint = self._blueprint()
        validation = validate_blueprint_coverage(
            blueprint,
            {
                "required_beat_count": 3,
                "covered_beat_indexes": [1, 2],
                "missing_beat_indexes": [3],
                "ending_pressure_required": True,
                "ending_pressure_covered": False,
            },
        )

        codes = [problem["code"] for problem in validation["problems"]]
        self.assertIn("missing_required_beat", codes)
        self.assertIn("missing_ending_pressure", codes)

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

    def test_model_plan_repairs_invalid_json_once(self) -> None:
        calls: list[tuple[list[dict[str, str]], dict]] = []
        outputs = [
            "goal: Open the first rift\nscenes: opening bridge",
            '{"goal": "Open the first rift.", "scenes": [{"index": 1, "type": "opening_bridge", "goal": "Begin at the observatory.", "required_beats": ["old observatory"]}]}',
        ]
        original_chat_completion = pipeline_module.chat_completion

        def completion(messages, **kwargs):
            calls.append((messages, kwargs))
            return outputs.pop(0)

        pipeline_module.chat_completion = completion
        try:
            plan = pipeline_module.plan_chapter("# Chapter Index\n3\n\ninput pack", chapter_index=3, dry_run=False)
        finally:
            pipeline_module.chat_completion = original_chat_completion

        self.assertEqual("Open the first rift.", plan["goal"])
        self.assertEqual(2, len(calls))
        self.assertEqual(0.0, calls[1][1]["temperature"])
        self.assertIn("invalid_response", calls[1][0][1]["content"])

    def test_model_plan_still_fails_when_json_repair_fails(self) -> None:
        outputs = ["not json", "still not json"]
        original_chat_completion = pipeline_module.chat_completion
        pipeline_module.chat_completion = lambda messages, **kwargs: outputs.pop(0)
        try:
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                pipeline_module.plan_chapter("input pack", chapter_index=1, dry_run=False)
        finally:
            pipeline_module.chat_completion = original_chat_completion

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
