from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from api.contracts import ModelResponse
from core.context_budget import (
    CalibratedTokenEstimator,
    ContextBudget,
    ContextBudgetError,
    ESTIMATOR_ASCII_FLOOR_TOKENS_PER_BYTE,
    ESTIMATOR_ENFORCEMENT_FIXED_OVERHEAD_TOKENS,
    ESTIMATOR_ENFORCEMENT_FLOOR_TOKENS_PER_UTF8_BYTE,
    ESTIMATOR_ENFORCEMENT_METHOD,
    MODEL_TOKENIZER_FIXED_OVERHEAD_TOKENS,
    RunBudgetLimits,
    RunBudgetTracker,
    TokenCounter,
    default_context_budget,
    model_token_counter,
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

    def test_default_run_input_budget_covers_validation_after_complex_drafting(self) -> None:
        limits = RunBudgetLimits()

        self.assertEqual(256_000, limits.max_total_input_tokens)
        self.assertGreaterEqual(
            limits.max_total_input_tokens,
            149_107 + 37_375 + 32_000 + 37_375,
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
        self.assertEqual(
            "mixed-endpoint-synthetic-calibration-v1",
            report["counter_version"],
        )
        self.assertEqual(3, report["raw_input_tokens"])
        self.assertEqual(68, report["budgeted_input_tokens"])
        self.assertEqual(
            "mixed-endpoint-synthetic-calibration-v1",
            report["count_metadata"]["calibration_version"],
        )
        self.assertEqual(
            "synthetic_acceptance_v1",
            report["count_metadata"]["calibration_source"],
        )
        self.assertFalse(
            report["count_metadata"]["calibration_real_provider_verified"]
        )
        self.assertEqual("evaluation_only", report["count_metadata"]["holdout_role"])
        self.assertEqual(
            "max-observed-tokens-per-utf8-byte-v1",
            report["count_metadata"]["calibration_method"],
        )
        self.assertEqual(
            ESTIMATOR_ENFORCEMENT_METHOD,
            report["count_metadata"]["enforcement_method"],
        )
        self.assertEqual(
            ESTIMATOR_ENFORCEMENT_FLOOR_TOKENS_PER_UTF8_BYTE,
            report["count_metadata"]["enforcement_floor_tokens_per_utf8_byte"],
        )
        self.assertEqual(
            ESTIMATOR_ASCII_FLOOR_TOKENS_PER_BYTE,
            report["count_metadata"]["ascii_floor_tokens_per_byte"],
        )
        self.assertEqual(
            ESTIMATOR_ENFORCEMENT_FIXED_OVERHEAD_TOKENS,
            report["count_metadata"]["enforcement_fixed_overhead_tokens"],
        )
        self.assertEqual(
            self._budget().safety_ratio,
            report["count_metadata"]["enforcement_safety_ratio"],
        )
        self.assertFalse(report["count_metadata"]["ascii_floor_applied"])

    def test_long_chinese_admission_uses_calibration_not_global_byte_floor(self) -> None:
        payload = "字" * 3_000

        report = self._budget(window=8_000, max_input=6_000).require_input(
            payload,
            stage="long-zh",
        )

        self.assertEqual(3_500, report["raw_input_tokens"])
        self.assertEqual(4_089, report["budgeted_input_tokens"])
        self.assertLess(
            report["budgeted_input_tokens"],
            len(payload.encode("utf-8")) // 2,
        )
        self.assertFalse(report["count_metadata"]["ascii_floor_applied"])

        mixed = ("a" * 100) + payload
        mixed_report = self._budget(window=8_000, max_input=6_000).require_input(
            mixed,
            stage="mixed-long-zh",
        )
        self.assertEqual(4_189, mixed_report["budgeted_input_tokens"])
        self.assertTrue(mixed_report["count_metadata"]["ascii_floor_applied"])

    def test_ascii_floor_covers_punctuation_entropy_and_json_escapes(self) -> None:
        payloads = (
            "!@#$%^&*()[]{};:,.<>?/\\|~`" * 80,
            "a!0?B#1$c%2^D&3*e(4)F-5_" * 80,
            '{"escaped":"' + ("\\u4e2d" * 200) + '"}',
        )

        for payload in payloads:
            with self.subTest(payload_prefix=payload[:24]):
                report = self._budget().measure(payload, stage="ascii-guard")
                self.assertTrue(report["count_metadata"]["ascii_floor_applied"])
                self.assertGreaterEqual(
                    report["budgeted_input_tokens"],
                    len(payload.encode("ascii"))
                    + ESTIMATOR_ENFORCEMENT_FIXED_OVERHEAD_TOKENS,
                )

        mixed = "中英混合-token-边界" * 80
        mixed_report = self._budget().measure(mixed, stage="mixed")
        self.assertTrue(mixed_report["count_metadata"]["ascii_floor_applied"])
        self.assertLess(
            mixed_report["budgeted_input_tokens"],
            len(mixed.encode("utf-8")) + ESTIMATOR_ENFORCEMENT_FIXED_OVERHEAD_TOKENS,
        )

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

    def test_official_openai_model_resolves_local_tokenizer_without_claiming_exact(self) -> None:
        class FakeEncoding:
            name = "fake-openai-bpe"

            @staticmethod
            def encode(text: str, *, disallowed_special=()):
                self.assertEqual((), disallowed_special)
                return list(range(max(1, len(text) // 2)))

        fake_tiktoken = SimpleNamespace(
            encoding_name_for_model=lambda model: "fake-openai-bpe"
            if model == "gpt-bound"
            else (_ for _ in ()).throw(KeyError(model)),
        )
        with (
            patch.dict(sys.modules, {"tiktoken": fake_tiktoken}),
            patch(
                "core.context_budget._cached_tiktoken_encoding",
                return_value=FakeEncoding(),
            ),
            patch("core.context_budget.importlib.metadata.version", return_value="test-build"),
        ):
            counter = model_token_counter(
                provider="openai",
                model="gpt-bound",
                endpoint_type="official",
            )

        self.assertIsNotNone(counter)
        assert counter is not None
        self.assertEqual("model_tokenizer", counter.count_mode)
        self.assertNotEqual("provider_exact", counter.count_mode)
        self.assertEqual("fake-openai-bpe", counter.tokenizer)
        self.assertEqual(
            max(1, len("中文 and English") // 2)
            + MODEL_TOKENIZER_FIXED_OVERHEAD_TOKENS,
            counter.count("中文 and English"),
        )

    def test_compatible_or_unknown_model_does_not_guess_openai_tokenizer(self) -> None:
        compatible_encoding_calls: list[str] = []

        def compatible_encoding_for_model(model: str) -> str:
            compatible_encoding_calls.append(model)
            return "must-not-be-used"

        compatible_tiktoken = SimpleNamespace(
            encoding_name_for_model=compatible_encoding_for_model,
            get_encoding=lambda name: self.fail("compatible endpoint must not load an encoding"),
        )
        with patch.dict(sys.modules, {"tiktoken": compatible_tiktoken}):
            compatible = model_token_counter(
                provider="openai",
                model="gpt-4.1-mini",
                endpoint_type="openai_compatible",
            )

        unknown_tiktoken = SimpleNamespace(
            encoding_name_for_model=lambda model: (_ for _ in ()).throw(KeyError(model)),
            get_encoding=lambda name: self.fail("unknown model must not load an encoding"),
        )
        with patch.dict(sys.modules, {"tiktoken": unknown_tiktoken}):
            unknown = model_token_counter(
                provider="openai",
                model="gateway-alias",
                endpoint_type="official",
            )

        self.assertIsNone(compatible)
        self.assertEqual([], compatible_encoding_calls)
        self.assertIsNone(unknown)

    def test_tiktoken_cache_miss_does_not_attempt_encoding_load(self) -> None:
        import tiktoken
        import tiktoken.registry

        empty_cache = (
            Path.cwd()
            / ".tmp"
            / "test_tiktoken_cache_miss"
            / uuid.uuid4().hex
        )
        empty_cache.mkdir(parents=True)
        with (
            patch.dict(
                os.environ,
                {"TIKTOKEN_CACHE_DIR": str(empty_cache)},
                clear=False,
            ),
            patch.dict(
                tiktoken.registry.ENCODINGS,
                {
                    "o200k_base": SimpleNamespace(
                        encode=lambda text, **kwargs: self.fail(
                            "mutable registry entries must not bypass local verification"
                        )
                    )
                },
                clear=True,
            ),
            patch.object(tiktoken, "get_encoding") as get_encoding,
            patch("tiktoken.load.read_file") as read_file,
        ):
            counter = model_token_counter(
                provider="openai",
                model="gpt-4.1-mini",
                endpoint_type="official",
            )

        self.assertIsNone(counter)
        get_encoding.assert_not_called()
        read_file.assert_not_called()

    def test_corrupt_tiktoken_cache_is_not_deleted_or_downloaded(self) -> None:
        import tiktoken.registry

        cache_dir = (
            Path.cwd()
            / ".tmp"
            / "test_tiktoken_corrupt_cache"
            / uuid.uuid4().hex
        )
        cache_dir.mkdir(parents=True)
        blob_url = (
            "https://openaipublic.blob.core.windows.net/encodings/"
            "o200k_base.tiktoken"
        )
        cache_path = cache_dir / hashlib.sha1(blob_url.encode("utf-8")).hexdigest()
        corrupt = b"not-a-valid-tokenizer-asset"
        cache_path.write_bytes(corrupt)

        with (
            patch.dict(
                os.environ,
                {"TIKTOKEN_CACHE_DIR": str(cache_dir)},
                clear=False,
            ),
            patch.dict(tiktoken.registry.ENCODINGS, {}, clear=True),
            patch("tiktoken.load.read_file") as read_file,
        ):
            counter = model_token_counter(
                provider="openai",
                model="gpt-4.1-mini",
                endpoint_type="official",
            )

        self.assertIsNone(counter)
        self.assertEqual(corrupt, cache_path.read_bytes())
        read_file.assert_not_called()

    def test_provider_exact_counter_rejects_added_overhead(self) -> None:
        with self.assertRaises(ContextBudgetError) as raised:
            TokenCounter(
                counter=lambda text: 7,
                count_mode="provider_exact",
                provider="openai",
                model="gpt-exact",
                endpoint_type="official",
                version="provider-v1",
                model_is_known=True,
                fixed_overhead_tokens=1,
            )

        self.assertEqual("token_counter_invalid", raised.exception.code)
        exact = TokenCounter(
            counter=lambda text: 7,
            count_mode="provider_exact",
            provider="openai",
            model="gpt-exact",
            endpoint_type="official",
            version="provider-v1",
            model_is_known=True,
        )
        self.assertEqual(7, exact.count("anything"))

    def test_bound_counter_identity_mismatch_fails_closed(self) -> None:
        counter = TokenCounter(
            counter=character_counter,
            count_mode="model_tokenizer",
            provider="openai",
            model="gpt-a",
            endpoint_type="official",
            version="test-v1",
            tokenizer="test-bpe",
            model_is_known=True,
        )

        with self.assertRaises(ContextBudgetError) as raised:
            ContextBudget(
                provider="openai",
                model="gpt-b",
                endpoint_type="official",
                model_context_window=10_000,
                bound_token_counter=counter,
            )

        self.assertEqual("token_counter_binding_mismatch", raised.exception.code)

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
            budget = default_context_budget(enable_model_tokenizer=False)

        self.assertEqual(64_000, budget.max_input_tokens)
        self.assertEqual(64_000, budget.hard_input_limit)

    def test_default_budget_binds_configured_model_and_endpoint(self) -> None:
        config = SimpleNamespace(
            openai_model="gpt-configured",
            openai_base_url="https://gateway.invalid/v1",
            claude_model=None,
            claude_base_url=None,
        )
        with patch("core.config.get_config", return_value=config):
            budget = default_context_budget(enable_model_tokenizer=False)

        self.assertEqual("gpt-configured", budget.model)
        self.assertEqual("openai_compatible", budget.endpoint_type)

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
            response=ModelResponse(
                "正文", usage={"input_tokens": 6, "output_tokens": 7}
            ),
            call_id="call-1",
            attempt_id="call-1-a1",
        )
        settled = tracker.report()
        self.assertEqual(6, settled["total_input_tokens"])
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

    def test_provider_input_usage_settles_estimate_and_overrun_is_charged(self) -> None:
        tracker = RunBudgetTracker(
            RunBudgetLimits(
                max_provider_calls=2,
                max_total_input_tokens=10,
                max_total_output_tokens=20,
                max_elapsed_seconds=10,
            )
        )
        tracker.reserve_model_call(
            input_tokens=4,
            max_output_tokens=5,
            call_id="call-input",
            attempt_id="call-input-a1",
        )

        with self.assertRaises(ContextBudgetError) as raised:
            tracker.record_model_response(
                response=ModelResponse(
                    "done", usage={"input_tokens": 11, "output_tokens": 2}
                ),
                call_id="call-input",
                attempt_id="call-input-a1",
            )

        self.assertEqual("run_input_token_budget_exceeded", raised.exception.code)
        report = tracker.report()
        self.assertEqual(11, report["total_input_tokens"])
        self.assertEqual(2, report["total_output_tokens"])
        self.assertEqual(0, report["reserved_output_tokens"])
        self.assertEqual(0, report["unsettled_attempt_count"])
        with self.assertRaises(ContextBudgetError) as replayed:
            tracker.record_model_response(
                response=ModelResponse("ignored"),
                call_id="call-input",
                attempt_id="call-input-a1",
            )
        self.assertEqual("run_input_token_budget_exceeded", replayed.exception.code)

    def test_provider_usage_formats_are_reconciled_without_cache_or_total_leaks(self) -> None:
        cases = (
            (
                "anthropic-cache-components",
                {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 3,
                    "cache_read_input_tokens": 11,
                    "output_tokens": 4,
                },
                16,
                4,
            ),
            (
                "anthropic-null-optional-details",
                {
                    "input_tokens": 2,
                    "input_tokens_details": None,
                    "cache_creation_input_tokens": 3,
                    "cache_read_input_tokens": 11,
                    "output_tokens": 4,
                    "output_tokens_details": None,
                },
                16,
                4,
            ),
            (
                "openai-cached-subset",
                {
                    "prompt_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 15},
                    "completion_tokens": 2,
                    "total_tokens": 22,
                },
                20,
                2,
            ),
            (
                "derive-output-from-total",
                {"input_tokens": 9, "total_tokens": 14},
                9,
                5,
            ),
            (
                "derive-input-from-total",
                {"output_tokens": 5, "total_tokens": 14},
                9,
                5,
            ),
            (
                "total-only-fails-closed",
                {"total_tokens": 14},
                14,
                30,
            ),
        )
        for ordinal, (name, usage, expected_input, expected_output) in enumerate(
            cases, start=1
        ):
            with self.subTest(name=name):
                tracker = RunBudgetTracker(
                    RunBudgetLimits(
                        max_provider_calls=2,
                        max_total_input_tokens=100,
                        max_total_output_tokens=100,
                        max_elapsed_seconds=10,
                    )
                )
                attempt_id = f"usage-{ordinal}-a1"
                tracker.reserve_model_call(
                    input_tokens=6,
                    max_output_tokens=30,
                    call_id=f"usage-{ordinal}",
                    attempt_id=attempt_id,
                )
                tracker.record_model_response(
                    response=ModelResponse("x", usage=usage),
                    call_id=f"usage-{ordinal}",
                    attempt_id=attempt_id,
                )
                report = tracker.report()
                self.assertEqual(expected_input, report["total_input_tokens"])
                self.assertEqual(expected_output, report["total_output_tokens"])

    def test_contradictory_total_fails_closed_after_conservative_settlement(self) -> None:
        tracker = RunBudgetTracker(
            RunBudgetLimits(
                max_provider_calls=2,
                max_total_input_tokens=100,
                max_total_output_tokens=100,
                max_elapsed_seconds=10,
            )
        )
        tracker.reserve_model_call(
            input_tokens=2,
            max_output_tokens=10,
            call_id="contradictory",
            attempt_id="contradictory-a1",
        )

        with self.assertRaises(ContextBudgetError) as raised:
            tracker.record_model_response(
                response=ModelResponse(
                    "x",
                    usage={"input_tokens": 4, "output_tokens": 3, "total_tokens": 6},
                ),
                call_id="contradictory",
                attempt_id="contradictory-a1",
            )

        self.assertEqual("run_budget_usage_invalid", raised.exception.code)
        report = tracker.report()
        self.assertEqual(6, report["total_input_tokens"])
        self.assertEqual(10, report["total_output_tokens"])
        self.assertEqual(0, report["unsettled_attempt_count"])
        with self.assertRaises(ContextBudgetError) as replayed:
            tracker.record_model_response(
                response=ModelResponse("ignored"),
                call_id="contradictory",
                attempt_id="contradictory-a1",
            )
        self.assertEqual("run_budget_usage_invalid", replayed.exception.code)

    def test_malformed_recognized_usage_field_fails_closed(self) -> None:
        tracker = RunBudgetTracker(
            RunBudgetLimits(
                max_provider_calls=2,
                max_total_input_tokens=100,
                max_total_output_tokens=100,
                max_elapsed_seconds=10,
            )
        )
        tracker.reserve_model_call(
            input_tokens=5,
            max_output_tokens=12,
            call_id="malformed",
            attempt_id="malformed-a1",
        )
        with self.assertRaises(ContextBudgetError) as raised:
            tracker.record_model_response(
                response=ModelResponse(
                    "x",
                    usage={"input_tokens": -1, "output_tokens": 2},
                ),
                call_id="malformed",
                attempt_id="malformed-a1",
            )
        self.assertEqual("run_budget_usage_invalid", raised.exception.code)
        self.assertEqual(5, tracker.total_input_tokens)
        self.assertEqual(12, tracker.total_output_tokens)
        self.assertEqual(0, tracker.reserved_output_tokens)

    def test_ensure_model_call_is_idempotent_but_rejects_evidence_drift(self) -> None:
        tracker = RunBudgetTracker(
            RunBudgetLimits(
                max_provider_calls=2,
                max_total_input_tokens=100,
                max_total_output_tokens=100,
                max_elapsed_seconds=10,
            )
        )
        arguments = {
            "input_tokens": 7,
            "max_output_tokens": 11,
            "call_id": "ensure",
            "attempt_id": "ensure-a1",
        }
        self.assertTrue(tracker.ensure_model_call(**arguments))
        self.assertFalse(tracker.ensure_model_call(**arguments))
        self.assertEqual(1, tracker.provider_calls)
        self.assertEqual(7, tracker.total_input_tokens)
        self.assertEqual(11, tracker.reserved_output_tokens)

        with self.assertRaises(ContextBudgetError) as raised:
            tracker.ensure_model_call(**{**arguments, "input_tokens": 8})
        self.assertEqual("run_budget_attempt_conflict", raised.exception.code)

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

    def test_converging_repair_authorization_adds_only_exact_token_deficit(self) -> None:
        tracker = RunBudgetTracker(
            RunBudgetLimits(
                max_provider_calls=5,
                max_total_input_tokens=100,
                max_total_output_tokens=10,
                max_elapsed_seconds=30,
            )
        )
        tracker.reserve_model_call(
            input_tokens=1,
            max_output_tokens=8,
            call_id="baseline",
            attempt_id="baseline-a1",
            stage="scene_repair",
        )
        tracker.record_model_response(
            response=ModelResponse("draft", usage={"input_tokens": 1, "output_tokens": 8}),
            call_id="baseline",
            attempt_id="baseline-a1",
        )
        tracker.authorize_elastic_tokens(
            authorization_id="progress-4-to-3",
            reason="repair_validation_problem_count_reduced",
            evidence={"problem_counts": [4, 3]},
            stages=("scene_repair", "llm_validation"),
        )

        tracker.reserve_model_call(
            input_tokens=1,
            max_output_tokens=5,
            call_id="validator",
            attempt_id="validator-a1",
            stage="llm_validation",
        )

        report = tracker.report()
        self.assertEqual(3, report["elastic_budget"]["output_tokens_added"])
        self.assertEqual(13, report["elastic_budget"]["effective_output_token_limit"])
        self.assertEqual("llm_validation", report["elastic_budget"]["grants"][0]["stage"])
        self.assertEqual([4, 3], report["elastic_budget"]["grants"][0]["evidence"]["problem_counts"])

    def test_elastic_authorization_cannot_be_reused_for_same_stage(self) -> None:
        tracker = RunBudgetTracker(
            RunBudgetLimits(
                max_provider_calls=5,
                max_total_input_tokens=100,
                max_total_output_tokens=10,
                max_elapsed_seconds=30,
            )
        )
        tracker.authorize_elastic_tokens(
            authorization_id="single-cycle",
            reason="repair_validation_problem_count_reduced",
            evidence={"problem_counts": [3, 2]},
            stages=("llm_validation",),
        )
        tracker.reserve_model_call(
            input_tokens=1,
            max_output_tokens=12,
            call_id="first",
            attempt_id="first-a1",
            stage="llm_validation",
        )

        with self.assertRaises(ContextBudgetError) as raised:
            tracker.reserve_model_call(
                input_tokens=1,
                max_output_tokens=1,
                call_id="second",
                attempt_id="second-a1",
                stage="llm_validation",
            )

        self.assertEqual("run_output_token_budget_exceeded", raised.exception.code)

    def test_elastic_authorization_never_expands_unrelated_stage(self) -> None:
        tracker = RunBudgetTracker(
            RunBudgetLimits(
                max_provider_calls=2,
                max_total_input_tokens=10,
                max_total_output_tokens=10,
                max_elapsed_seconds=30,
            )
        )
        tracker.authorize_elastic_tokens(
            authorization_id="repair-only",
            reason="repair_validation_problem_count_reduced",
            evidence={"problem_counts": [2, 1]},
            stages=("scene_repair", "llm_validation"),
        )

        with self.assertRaises(ContextBudgetError) as raised:
            tracker.reserve_model_call(
                input_tokens=1,
                max_output_tokens=11,
                call_id="draft",
                attempt_id="draft-a1",
                stage="chapter_generation",
            )

        self.assertEqual("run_output_token_budget_exceeded", raised.exception.code)

    def test_durable_attempt_restore_rehydrates_elastic_token_reservation(self) -> None:
        tracker = RunBudgetTracker(
            RunBudgetLimits(
                max_provider_calls=3,
                max_total_input_tokens=100,
                max_total_output_tokens=10,
                max_elapsed_seconds=30,
            )
        )
        tracker.restore_model_call(
            input_tokens=1,
            max_output_tokens=12,
            call_id="durable",
            attempt_id="durable-a1",
            stage="llm_validation",
        )

        report = tracker.report()
        self.assertEqual(2, report["elastic_budget"]["output_tokens_added"])
        self.assertTrue(report["elastic_budget"]["grants"][0]["restored"])
        self.assertEqual(12, report["reserved_output_tokens"])

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
