from __future__ import annotations

import hashlib
import os
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

from api.contracts import ModelResponse
from core.context_budget import (
    CalibratedTokenEstimator,
    ContextBudget,
    ContextBudgetError,
    RunBudgetLimits,
    RunBudgetTracker,
    TokenCounter,
    default_context_budget,
    preview_chinese_output_compatibility,
)
from core.prompt_compiler import compile_prompt_contexts
from core.schema import validate_schema
from core.state.input_pack import build_input_pack
from core.story_project.mapper import build_story_project_runtime_context
from modules.chapter_generator.pipeline import run_chapter_pipeline


def character_counter(text: str) -> int:
    return len(text)


character_counter.version = "character-counter-test-v1"
character_counter.tokenizer = "character-counter"
character_counter.model_is_known = True


class ContextBudgetTest(unittest.TestCase):
    def _budget(
        self,
        *,
        window: int = 10_000,
        max_input: int = 7_000,
        provider: str = "test-provider",
        model: str = "test-model",
        endpoint_type: str = "unknown",
    ) -> ContextBudget:
        return ContextBudget(
            provider=provider,
            model=model,
            model_context_window=window,
            output_reserve_tokens=1_000,
            protocol_overhead_tokens=500,
            safety_margin_tokens=500,
            max_input_tokens=max_input,
            story_project_tokens=4_000,
            previous_chapter_tokens=2_000,
            endpoint_type=endpoint_type,
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
        self.assertEqual("model_tokenizer", report["count_mode"])
        self.assertEqual("character-counter-test-v1", report["counter_version"])
        self.assertEqual("character-counter", report["count_metadata"]["tokenizer"])
        self.assertEqual("unknown", report["count_metadata"]["endpoint_type"])

    def test_fallback_estimator_is_versioned_and_applies_safety_ratio(self) -> None:
        report = self._budget().measure("中文", stage="scene")

        self.assertEqual("calibrated_estimate", report["count_mode"])
        self.assertEqual("utf8-upper-bound-v1", report["counter_version"])
        self.assertEqual(6, report["raw_input_tokens"])
        self.assertEqual(7, report["budgeted_input_tokens"])
        self.assertEqual("utf8-upper-bound-v1", report["count_metadata"]["calibration_version"])

    def test_official_known_model_may_report_provider_exact_with_bound_metadata(self) -> None:
        budget = self._budget(provider="openai", model="gpt-known", endpoint_type="official")
        counter = TokenCounter(
            counter=character_counter,
            count_mode="provider_exact",
            provider="openai",
            model="gpt-known",
            endpoint_type="official",
            version="provider-usage-v1",
            model_is_known=True,
        )

        report = budget.measure("Hello，世界", stage="scene", token_counter=counter)

        self.assertEqual("provider_exact", report["count_mode"])
        self.assertEqual(len("Hello，世界"), report["raw_input_tokens"])
        self.assertEqual("provider_usage", report["count_metadata"]["counter_source"])
        self.assertEqual("provider-usage-v1", report["count_metadata"]["provider_counter_version"])

    def test_compatible_endpoint_and_unknown_model_cannot_claim_provider_exact(self) -> None:
        unsafe_cases = (
            ("openai_compatible", True),
            ("official", False),
        )
        for endpoint_type, model_is_known in unsafe_cases:
            with self.subTest(endpoint_type=endpoint_type, model_is_known=model_is_known):
                with self.assertRaises(ContextBudgetError) as raised:
                    TokenCounter(
                        counter=character_counter,
                        count_mode="provider_exact",
                        provider="openai",
                        model="gpt-compatible-alias",
                        endpoint_type=endpoint_type,
                        version="provider-usage-v1",
                        model_is_known=model_is_known,
                    )
                self.assertEqual("token_counter_unsafe_exact", raised.exception.code)

        compatible = self._budget(
            provider="openai",
            model="gpt-compatible-alias",
            endpoint_type="openai_compatible",
        ).measure("mixed 中英", stage="scene", exact_counter=character_counter)
        self.assertEqual("model_tokenizer", compatible["count_mode"])
        self.assertNotEqual("provider_exact", compatible["count_mode"])
        self.assertEqual("openai_compatible", compatible["count_metadata"]["endpoint_type"])

        unknown = self._budget(provider="openai", model="unknown-model").measure(
            "mixed 中英",
            stage="scene",
        )
        self.assertEqual("calibrated_estimate", unknown["count_mode"])
        self.assertFalse(unknown["count_metadata"]["model_is_known"])

    def test_model_tokenizer_and_mixed_language_calibration_are_explicit(self) -> None:
        budget = self._budget(provider="openai", model="gpt-known", endpoint_type="official")
        tokenizer = TokenCounter(
            counter=character_counter,
            count_mode="model_tokenizer",
            provider="openai",
            model="gpt-known",
            endpoint_type="official",
            version="tokenizer-build-2026-07",
            tokenizer="test-bpe",
            model_is_known=True,
        )
        tokenized = budget.measure("English 与中文 mixed", stage="plan", token_counter=tokenizer)
        self.assertEqual("model_tokenizer", tokenized["count_mode"])
        self.assertEqual("test-bpe", tokenized["count_metadata"]["tokenizer"])
        self.assertEqual(
            "tokenizer-build-2026-07",
            tokenized["count_metadata"]["tokenizer_version"],
        )

        calibration = CalibratedTokenEstimator(
            version="mixed-language-heldout-v2",
            tokens_per_utf8_byte=0.5,
            fixed_overhead_tokens=2,
        )
        estimated = budget.measure(
            "Hello世界",
            stage="plan",
            calibrated_estimator=calibration,
        )
        self.assertEqual(8, estimated["raw_input_tokens"])
        self.assertEqual(10, estimated["budgeted_input_tokens"])
        self.assertEqual("calibrated_estimate", estimated["count_mode"])
        self.assertEqual(
            "mixed-language-heldout-v2",
            estimated["count_metadata"]["calibration_version"],
        )

    def test_schema_reads_legacy_modes_but_new_measurements_never_write_them(self) -> None:
        current = self._budget().measure("history", stage="plan")
        for legacy_mode in ("exact", "estimate"):
            historical = dict(current)
            historical.pop("count_metadata")
            historical["count_mode"] = legacy_mode
            validated = validate_schema(historical, "context_budget_report.schema.json")
            self.assertEqual(legacy_mode, validated["count_mode"])

        self.assertNotIn(current["count_mode"], {"exact", "estimate"})
        legacy_counter_report = self._budget().measure(
            "history",
            stage="plan",
            exact_counter=character_counter,
        )
        self.assertNotIn(legacy_counter_report["count_mode"], {"exact", "estimate"})

    def test_chinese_output_range_preview_checks_max_output_tokens_purely(self) -> None:
        calibration = CalibratedTokenEstimator(
            version="cjk-one-token-per-char-v1",
            tokens_per_utf8_byte=1 / 3,
        )
        compatible = preview_chinese_output_compatibility(
            4_500,
            calibrated_estimator=calibration,
            safety_ratio=0,
        )
        short = preview_chinese_output_compatibility(
            4_499,
            calibrated_estimator=calibration,
            safety_ratio=0,
        )

        self.assertEqual(3_000, compatible["minimum_required_tokens"])
        self.assertEqual(4_500, compatible["maximum_required_tokens"])
        self.assertTrue(compatible["compatible"])
        self.assertTrue(short["minimum_target_compatible"])
        self.assertFalse(short["full_target_range_compatible"])
        self.assertEqual(1, short["shortfall_tokens"])
        self.assertEqual("cjk-one-token-per-char-v1", short["calibration_version"])

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

    def test_model_call_reservation_settles_actual_usage_and_timeout_stays_charged(self) -> None:
        tracker = RunBudgetTracker(
            RunBudgetLimits(
                max_provider_calls=3,
                max_total_input_tokens=100,
                max_total_output_tokens=50,
                max_elapsed_seconds=10,
            )
        )
        tracker.reserve_model_call(
            input_tokens=10,
            max_output_tokens=30,
            call_id="call-1",
            attempt_id="call-1-a1",
        )
        self.assertEqual(30, tracker.report()["reserved_output_tokens"])
        tracker.record_model_response(
            response=ModelResponse("正文", usage={"output_tokens": 7}),
            call_id="call-1",
            attempt_id="call-1-a1",
        )
        settled = tracker.report()
        self.assertEqual(7, settled["total_output_tokens"])
        self.assertEqual(0, settled["reserved_output_tokens"])

        tracker.reserve_model_call(
            input_tokens=5,
            max_output_tokens=20,
            call_id="call-2",
            attempt_id="call-2-a1",
        )
        uncertain = tracker.report()
        self.assertEqual(20, uncertain["reserved_output_tokens"])
        self.assertEqual(27, uncertain["charged_output_tokens"])
        self.assertEqual(1, uncertain["unsettled_attempt_count"])

    def test_model_call_reservation_rejects_output_before_network(self) -> None:
        tracker = RunBudgetTracker(
            RunBudgetLimits(
                max_provider_calls=2,
                max_total_input_tokens=100,
                max_total_output_tokens=10,
                max_elapsed_seconds=10,
            )
        )
        with self.assertRaises(ContextBudgetError) as caught:
            tracker.reserve_model_call(
                input_tokens=1,
                max_output_tokens=11,
                call_id="call",
                attempt_id="call-a1",
            )
        self.assertEqual("run_output_token_budget_exceeded", caught.exception.code)
        self.assertEqual(0, tracker.report()["provider_calls"])

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
        mapped_source = tracking.read_bytes().decode("utf-8-sig")
        self.assertEqual(
            hashlib.sha256(mapped_source.encode("utf-8")).hexdigest(),
            tracked["selection"]["source_sha256"],
        )
        for item in tracked["selection"]["selected_items"]:
            selected_source = mapped_source[item["start_char"] : item["end_char"]]
            self.assertEqual(item["original_chars"], len(selected_source))
            self.assertEqual(item["sha256"], hashlib.sha256(selected_source.encode("utf-8")).hexdigest())
            self.assertIn(selected_source, tracked["text"])
        self.assertEqual("发现门禁记录", context.chapter_blueprint.required_beats[0]["text"])
        self.assertEqual("警报响起", context.chapter_blueprint.ending_pressure)

        pack = build_input_pack(
            {"chapter_index": 2, "world_state": {}, "characters": {}, "timeline": []},
            story_project_context=context.to_dict(),
        )
        self.assertLess(len(pack), 20_000)
        self.assertIn("最新事实：门已打开", pack)
        self.assertIn(tracked["selection"]["source_sha256"], pack)
        self.assertNotIn("旧事实。" * 2_000, pack)

    def test_pipeline_records_stage_budget_reports(self) -> None:
        pipeline = run_chapter_pipeline(
            "# Story State\n{}\n\n# Requirements\nReturn prose.",
            chapter_index=1,
            dry_run=True,
        )

        self.assertEqual(64, len(pipeline["context_budget"]["context_digest"]))
        self.assertTrue(pipeline["context_budget"]["plan"]["within_budget"])
        self.assertEqual("calibrated_estimate", pipeline["context_budget"]["scene"]["count_mode"])


if __name__ == "__main__":
    unittest.main()
