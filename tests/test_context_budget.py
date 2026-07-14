from __future__ import annotations

import hashlib
import os
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

from core.context_budget import (
    ContextBudget,
    ContextBudgetError,
    RunBudgetLimits,
    RunBudgetTracker,
    default_context_budget,
)
from core.prompt_compiler import compile_prompt_contexts
from core.state.input_pack import build_input_pack
from core.story_project.mapper import build_story_project_runtime_context
from modules.chapter_generator.pipeline import run_chapter_pipeline


def character_counter(text: str) -> int:
    return len(text)


character_counter.version = "character-counter-test-v1"


class ContextBudgetTest(unittest.TestCase):
    def _budget(self, *, window: int = 10_000, max_input: int = 7_000) -> ContextBudget:
        return ContextBudget(
            provider="test-provider",
            model="test-model",
            model_context_window=window,
            output_reserve_tokens=1_000,
            protocol_overhead_tokens=500,
            safety_margin_tokens=500,
            max_input_tokens=max_input,
            story_project_tokens=4_000,
            previous_chapter_tokens=2_000,
        )

    def _case_dir(self, name: str) -> Path:
        path = Path(".tmp") / "test_context_budget" / f"{name}_{uuid.uuid4().hex}"
        path.mkdir(parents=True)
        return path

    def test_model_aware_formula_does_not_double_subtract_output_reserve(self) -> None:
        budget = self._budget()

        self.assertEqual(8_000, budget.usable_input_tokens)
        self.assertEqual(7_000, budget.hard_input_limit)
        report = budget.require_input("abc", stage="plan", exact_counter=character_counter)
        self.assertEqual(3, report["raw_input_tokens"])
        self.assertEqual(3, report["budgeted_input_tokens"])
        self.assertEqual("exact", report["count_mode"])
        self.assertEqual("character-counter-test-v1", report["counter_version"])

    def test_fallback_estimator_is_versioned_and_applies_safety_ratio(self) -> None:
        report = self._budget().measure("中文", stage="scene")

        self.assertEqual("estimate", report["count_mode"])
        self.assertEqual("utf8-upper-bound-v1", report["counter_version"])
        self.assertEqual(6, report["raw_input_tokens"])
        self.assertEqual(7, report["budgeted_input_tokens"])

    def test_default_budget_allows_bounded_max_input_override(self) -> None:
        with patch.dict(
            os.environ,
            {
                "NOVELAGENT_MODEL_CONTEXT_WINDOW": "128000",
                "NOVELAGENT_MAX_INPUT_TOKENS": "64000",
            },
        ):
            budget = default_context_budget()

        self.assertEqual(64_000, budget.max_input_tokens)
        self.assertEqual(64_000, budget.hard_input_limit)

    def test_mandatory_context_over_budget_fails_before_provider(self) -> None:
        input_pack = "# Story State\n" + ("必须事实" * 300) + "\n\n# Requirements\nReturn prose."
        budget = ContextBudget(
            provider="test",
            model="tiny",
            model_context_window=600,
            output_reserve_tokens=100,
            protocol_overhead_tokens=50,
            safety_margin_tokens=50,
            max_input_tokens=400,
        )

        with self.assertRaises(ContextBudgetError) as raised:
            compile_prompt_contexts(input_pack, budget=budget, exact_counter=character_counter)

        self.assertEqual("story_project_context_budget_exceeded", raised.exception.code)

    def test_plan_scene_and_repair_share_digest_but_drop_unneeded_payloads(self) -> None:
        input_pack = (
            "# Project Profile\n{}\n\n"
            "# Story State\n{\"location\":\"station\"}\n\n"
            "# Spatial State\n{}\n\n"
            "# Memory Index\n" + ("optional-memory\n" * 200) + "\n"
            "# Requirements\nReturn prose."
        )
        budget = ContextBudget(
            provider="test",
            model="compact",
            model_context_window=1_500,
            output_reserve_tokens=100,
            protocol_overhead_tokens=50,
            safety_margin_tokens=50,
            max_input_tokens=1_300,
        )

        bundle = compile_prompt_contexts(input_pack, budget=budget, exact_counter=character_counter)

        expected_digest = hashlib.sha256(input_pack.encode("utf-8")).hexdigest()
        self.assertEqual(expected_digest, bundle.context_digest)
        self.assertNotIn("optional-memory", bundle.scene.text)
        self.assertNotIn("optional-memory", bundle.repair.text)
        self.assertIn("# Story State", bundle.plan.text)
        self.assertIn("# Context Digest", bundle.scene.text)
        self.assertTrue(bundle.to_dict()["plan"]["report"]["within_budget"])

    def test_run_budget_tracker_enforces_calls_input_output_cost_and_elapsed(self) -> None:
        clock = [0.0]
        tracker = RunBudgetTracker(
            RunBudgetLimits(
                max_provider_calls=2,
                max_total_input_tokens=10,
                max_total_output_tokens=5,
                max_elapsed_seconds=10,
                max_estimated_cost=1.0,
            ),
            now=lambda: clock[0],
        )
        tracker.reserve_call(4, estimated_cost=0.2)
        tracker.record_output(2, estimated_cost=0.1)
        tracker.reserve_call(6, estimated_cost=0.2)

        with self.assertRaises(ContextBudgetError) as calls:
            tracker.reserve_call(0)
        self.assertEqual("run_provider_call_budget_exceeded", calls.exception.code)
        with self.assertRaises(ContextBudgetError) as output:
            tracker.record_output(4)
        self.assertEqual("run_output_token_budget_exceeded", output.exception.code)
        clock[0] = 11.0
        with self.assertRaises(ContextBudgetError) as elapsed:
            tracker.record_output(0)
        self.assertEqual("run_elapsed_budget_exceeded", elapsed.exception.code)

    def test_mapper_hashes_full_long_files_but_prompt_excerpt_keeps_latest_tail(self) -> None:
        root = self._case_dir("long_files") / "book"
        for directory in ("设定", "大纲", "正文", "追踪"):
            (root / directory).mkdir(parents=True)
        outline = (
            "# 第二章\n核心事件：进入控制室\n\n"
            + ("背景段落。" * 20_000)
            + "\n\n## 必写节拍\n- 发现门禁记录\n\n结尾压力：警报响起\n"
        )
        (root / "大纲" / "细纲_第002章.md").write_text(outline, encoding="utf-8")
        (root / "正文" / "第001章_一.md").write_text("上一章", encoding="utf-8")
        tracking_text = "# 上下文\n" + ("旧事实。" * 25_000) + "\n\n- 最新事实：门已打开"
        tracking = root / "追踪" / "上下文.md"
        tracking.write_text(tracking_text, encoding="utf-8")

        context = build_story_project_runtime_context(root, 2, max_file_chars=200)
        tracked = context.tracking_files["上下文.md"]

        self.assertEqual(len(tracking.read_bytes().decode("utf-8-sig")), tracked["chars"])
        self.assertEqual(hashlib.sha256(tracking.read_bytes()).hexdigest(), tracked["sha256"])
        self.assertTrue(tracked["truncated"])
        self.assertIn("最新事实：门已打开", tracked["text"])
        self.assertEqual("发现门禁记录", context.chapter_blueprint.required_beats[0]["text"])
        self.assertEqual("警报响起", context.chapter_blueprint.ending_pressure)

        pack = build_input_pack(
            {"chapter_index": 2, "world_state": {}, "characters": {}, "timeline": []},
            story_project_context=context.to_dict(),
        )
        self.assertLess(len(pack), 20_000)
        self.assertIn("最新事实：门已打开", pack)
        self.assertNotIn("旧事实。" * 2_000, pack)

    def test_pipeline_records_stage_budget_reports(self) -> None:
        pipeline = run_chapter_pipeline(
            "# Story State\n{}\n\n# Requirements\nReturn prose.",
            chapter_index=1,
            dry_run=True,
        )

        self.assertEqual(64, len(pipeline["context_budget"]["context_digest"]))
        self.assertTrue(pipeline["context_budget"]["plan"]["within_budget"])
        self.assertEqual("estimate", pipeline["context_budget"]["scene"]["count_mode"])


if __name__ == "__main__":
    unittest.main()
