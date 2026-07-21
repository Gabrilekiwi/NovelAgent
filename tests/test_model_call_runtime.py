from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch
import uuid

from api.claude_client import _extract_message_text, _stream_message_text, polish_chapter
from api.contracts import ModelCallError, ModelResponse
from api.openai_client import (
    _extract_message_content,
    _extract_streamed_message_content,
    chat_completion,
)
from api.retry import CLAUDE_POLISH, MODEL_READ_GENERATION, RetryPolicy
from core.context_budget import (
    conservative_calibrated_token_estimate,
    ContextBudgetError,
    ESTIMATOR_ENFORCEMENT_FIXED_OVERHEAD_TOKENS,
    RunBudgetLimits,
    RunBudgetTracker,
    TokenCounter,
)
from core.engine.persistence import atomic_write_text
from core.memory_v2.canonical import canonical_json_bytes
from core.model_call_runtime import (
    ModelCallRuntimeContext,
    ProviderCallUncertainError,
    use_model_call_runtime,
)
from core.model_calls import ModelCallConflictError, ModelCallIntegrityError, ModelCallStore


class RecordingTracker:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def reserve_model_call(self, **kwargs: object) -> None:
        self.events.append(("reserve", dict(kwargs)))

    def record_model_response(self, **kwargs: object) -> None:
        self.events.append(("response", dict(kwargs)))


class ModelCallRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (
            Path.cwd()
            / ".tmp"
            / "test_model_call_runtime"
            / uuid.uuid4().hex
        )
        self.store = ModelCallStore(self.root)
        self.tracker = RecordingTracker()
        self.runtime = ModelCallRuntimeContext(
            self.store,
            tracker=self.tracker,
            id_factory=lambda: "stable",
        )

    @staticmethod
    def _retry_policy(profile: str) -> RetryPolicy:
        return RetryPolicy(
            profile=profile,
            max_attempts=3,
            base_delay_seconds=0,
            max_delay_seconds=0,
            jitter_ratio=0,
            deadline_seconds=10,
        )

    def test_runtime_orders_intent_budget_network_artifact_and_receipt(self) -> None:
        request = {
            "messages": [{"role": "user", "content": "private full prompt"}],
            "source_hint": "C:\\private\\novel.md",
        }

        def network() -> ModelResponse:
            self.assertTrue(self.store.intent_path("call-1-a1").is_file())
            self.assertFalse(self.store.receipt_path("call-1-a1").exists())
            self.assertEqual("reserve", self.tracker.events[0][0])
            return ModelResponse(
                "正文响应",
                usage={"input_tokens": 7, "output_tokens": 3},
                finish_reason="stop",
                request_id="request-1",
                actual_model="actual-model",
                endpoint_type="official",
            )

        response = self.runtime.execute_attempt(
            call_id="call-1",
            attempt_number=1,
            provider="openai",
            model="requested-model",
            stage="chapter_draft",
            endpoint_type="official",
            request=request,
            max_output_tokens=40,
            input_tokens=12,
            operation=network,
        )

        intent = self.store.load_intent("call-1-a1")
        receipt = self.store.load_receipt("call-1-a1")
        artifact = self.root / receipt["response_artifact_ref"]
        persisted = json.dumps(intent, ensure_ascii=False).lower()
        self.assertIsInstance(response, ModelResponse)
        self.assertEqual("正文响应", response)
        self.assertEqual(
            {
                "reserved_input_tokens": 12,
                "reserved_output_tokens": 40,
                "reserved_total_tokens": 52,
            },
            intent["budget_reservation"],
        )
        self.assertNotIn("private full prompt", persisted)
        self.assertNotIn("c:\\\\private", persisted)
        self.assertEqual("正文响应", artifact.read_text(encoding="utf-8"))
        self.assertEqual("request-1", receipt["request_id"])
        self.assertEqual(["reserve", "response"], [item[0] for item in self.tracker.events])

    def test_default_request_reservation_uses_calibration_and_settles_provider_input(self) -> None:
        tracker = RunBudgetTracker(
            RunBudgetLimits(
                max_provider_calls=2,
                max_total_input_tokens=1_000,
                max_total_output_tokens=20,
                max_elapsed_seconds=30,
            )
        )
        runtime = ModelCallRuntimeContext(self.store, tracker=tracker)
        request = {
            "messages": [
                {"role": "user", "content": "中文 and English calibration"}
            ]
        }
        reserved = runtime.estimate_input_tokens(request)
        self.assertGreater(reserved, 0)
        self.assertEqual(
            conservative_calibrated_token_estimate(
                canonical_json_bytes(
                    request,
                    exclude_environment_fields=False,
                ).decode("utf-8")
            ),
            reserved,
        )
        self.assertLess(
            reserved,
            len(canonical_json_bytes(request, exclude_environment_fields=False))
            + ESTIMATOR_ENFORCEMENT_FIXED_OVERHEAD_TOKENS,
        )

        runtime.execute_attempt(
            call_id="calibrated-input",
            attempt_number=1,
            provider="openai",
            model="gpt-test",
            stage="draft",
            endpoint_type="official",
            request=request,
            max_output_tokens=8,
            operation=lambda: ModelResponse(
                "done",
                usage={"input_tokens": 9, "output_tokens": 2},
                endpoint_type="official",
            ),
        )

        intent = self.store.load_intent("calibrated-input-a1")
        self.assertEqual(
            reserved,
            intent["budget_reservation"]["reserved_input_tokens"],
        )
        self.assertEqual(9, tracker.report()["total_input_tokens"])

    def test_long_chinese_default_reservation_no_longer_rejects_before_provider(self) -> None:
        tracker = RunBudgetTracker(
            RunBudgetLimits(
                max_provider_calls=1,
                max_total_input_tokens=6_000,
                max_total_output_tokens=20,
                max_elapsed_seconds=30,
            )
        )
        runtime = ModelCallRuntimeContext(self.store, tracker=tracker)
        provider_calls = 0

        def network() -> ModelResponse:
            nonlocal provider_calls
            provider_calls += 1
            return ModelResponse(
                "done",
                usage={"input_tokens": 4_200, "output_tokens": 2},
                endpoint_type="official",
            )

        runtime.execute_attempt(
            call_id="long-chinese",
            attempt_number=1,
            provider="openai",
            model="unknown-test-model",
            stage="draft",
            endpoint_type="official",
            request={"messages": [{"role": "user", "content": "字" * 4_000}]},
            max_output_tokens=8,
            operation=network,
        )

        intent = self.store.load_intent("long-chinese-a1")
        self.assertEqual(1, provider_calls)
        self.assertLess(
            intent["budget_reservation"]["reserved_input_tokens"],
            6_000,
        )
        self.assertEqual("succeeded", self.store.load_receipt("long-chinese-a1")["status"])

    def test_runtime_resolves_tokenizer_from_actual_call_identity(self) -> None:
        counter = TokenCounter(
            counter=lambda text: len(text) // 4,
            count_mode="model_tokenizer",
            provider="openai",
            model="gpt-bound",
            endpoint_type="official",
            version="bound-tokenizer-v1",
            tokenizer="bound-test",
            model_is_known=True,
            fixed_overhead_tokens=7,
        )
        request = {"messages": [{"role": "user", "content": "bound request"}]}
        canonical = canonical_json_bytes(
            request,
            exclude_environment_fields=False,
        ).decode("utf-8")

        with patch(
            "core.model_call_runtime.model_token_counter",
            return_value=counter,
        ) as resolver:
            self.runtime.execute_attempt(
                call_id="bound-counter",
                attempt_number=1,
                provider="openai",
                model="gpt-bound",
                stage="draft",
                endpoint_type="official",
                request=request,
                max_output_tokens=8,
                operation=lambda: ModelResponse("done", endpoint_type="official"),
            )

        resolver.assert_called_once_with(
            provider="openai",
            model="gpt-bound",
            endpoint_type="official",
        )
        intent = self.store.load_intent("bound-counter-a1")
        self.assertEqual(
            len(canonical) // 4 + 7,
            intent["budget_reservation"]["reserved_input_tokens"],
        )

    def test_injected_input_counter_precedes_automatic_model_tokenizer(self) -> None:
        runtime = ModelCallRuntimeContext(
            self.store,
            tracker=self.tracker,
            input_token_counter=lambda request: 5,
        )

        with patch("core.model_call_runtime.model_token_counter") as resolver:
            reserved = runtime.estimate_input_tokens(
                {"messages": [{"role": "user", "content": "request"}]},
                provider="openai",
                model="gpt-4.1-mini",
                endpoint_type="official",
            )

        self.assertEqual(5, reserved)
        resolver.assert_not_called()

    def test_explicit_input_tokens_precede_injected_and_automatic_counters(self) -> None:
        def fail_injected_counter(request: object) -> int:
            self.fail("injected counter must not run when input_tokens is explicit")

        runtime = ModelCallRuntimeContext(
            self.store,
            tracker=self.tracker,
            input_token_counter=fail_injected_counter,
        )
        with patch("core.model_call_runtime.model_token_counter") as resolver:
            runtime.execute_attempt(
                call_id="explicit-counter-priority",
                attempt_number=1,
                provider="openai",
                model="gpt-4.1-mini",
                stage="draft",
                endpoint_type="official",
                request={"messages": [{"role": "user", "content": "request"}]},
                max_output_tokens=8,
                input_tokens=4,
                operation=lambda: ModelResponse("done", endpoint_type="official"),
            )

        resolver.assert_not_called()
        intent = self.store.load_intent("explicit-counter-priority-a1")
        self.assertEqual(
            4,
            intent["budget_reservation"]["reserved_input_tokens"],
        )

    def test_receipt_replays_without_network_and_rejects_request_collision(self) -> None:
        calls = 0

        def network() -> ModelResponse:
            nonlocal calls
            calls += 1
            return ModelResponse("once", endpoint_type="official")

        arguments = {
            "call_id": "replay-call",
            "attempt_number": 1,
            "provider": "openai",
            "model": "gpt-test",
            "stage": "draft",
            "endpoint_type": "official",
            "request": {"messages": [{"role": "user", "content": "same"}]},
            "max_output_tokens": 10,
            "input_tokens": 4,
        }
        first = self.runtime.execute_attempt(operation=network, **arguments)
        second = self.runtime.execute_attempt(
            operation=lambda: self.fail("network must not be replayed"),
            **arguments,
        )

        self.assertEqual("once", first)
        self.assertEqual("once", second)
        self.assertEqual(1, calls)
        changed = dict(arguments)
        changed["request"] = {"messages": [{"role": "user", "content": "changed"}]}
        with self.assertRaises(ModelCallConflictError):
            self.runtime.execute_attempt(operation=network, **changed)

    def test_intent_only_is_uncertain_and_never_reinvoked(self) -> None:
        calls = 0

        def timeout() -> ModelResponse:
            nonlocal calls
            calls += 1
            raise TimeoutError("response boundary unknown")

        with self.assertRaises(ProviderCallUncertainError):
            self.runtime.execute_attempt(
                call_id="uncertain-call",
                attempt_number=1,
                provider="openai",
                model="gpt-test",
                stage="draft",
                endpoint_type="official",
                request={"messages": []},
                max_output_tokens=10,
                input_tokens=1,
                operation=timeout,
            )
        with self.assertRaises(ProviderCallUncertainError):
            self.runtime.execute_attempt(
                call_id="uncertain-call",
                attempt_number=1,
                provider="openai",
                model="gpt-test",
                stage="draft",
                endpoint_type="official",
                request={"messages": []},
                max_output_tokens=10,
                input_tokens=1,
                operation=timeout,
            )

        self.assertEqual(1, calls)
        uncertain = self.store.list_uncertain_calls()
        self.assertEqual("provider_call_uncertain", uncertain[0]["status"])

    def test_response_boundary_faults_leave_intent_only_and_restart_never_resends(self) -> None:
        fault_points = (
            ("after_provider_response_before_artifact", False),
            ("after_response_artifact_before_receipt", True),
        )
        for ordinal, (fault_point, artifact_expected) in enumerate(
            fault_points, start=1
        ):
            with self.subTest(fault_point=fault_point):
                root = self.root / f"fault-{ordinal}"
                store = ModelCallStore(root)
                tracker = RunBudgetTracker(
                    RunBudgetLimits(
                        max_provider_calls=5,
                        max_total_input_tokens=100,
                        max_total_output_tokens=100,
                        max_elapsed_seconds=30,
                    )
                )
                provider_calls = 0

                def inject(
                    event: str,
                    _attempt_id: str,
                    _path: Path | None,
                ) -> None:
                    if event == fault_point:
                        raise RuntimeError(f"simulated crash at {event}")

                def provider() -> ModelResponse:
                    nonlocal provider_calls
                    provider_calls += 1
                    return ModelResponse(
                        "provider returned normally",
                        usage={"input_tokens": 4, "output_tokens": 3},
                        endpoint_type="official",
                    )

                runtime = ModelCallRuntimeContext(
                    store,
                    tracker=tracker,
                    fault_injector=inject,
                )
                arguments = {
                    "call_id": f"fault-call-{ordinal}",
                    "attempt_number": 1,
                    "provider": "openai",
                    "model": "gpt-test",
                    "stage": "draft",
                    "endpoint_type": "official",
                    "request": {"messages": []},
                    "max_output_tokens": 20,
                    "input_tokens": 4,
                }
                attempt_id = f"fault-call-{ordinal}-a1"

                with self.assertRaises(ProviderCallUncertainError) as raised:
                    runtime.execute_attempt(operation=provider, **arguments)

                self.assertEqual("provider_call_uncertain", raised.exception.failure_category)
                self.assertEqual(1, provider_calls)
                self.assertTrue(store.intent_path(attempt_id).is_file())
                self.assertFalse(store.receipt_path(attempt_id).exists())
                self.assertEqual(
                    artifact_expected,
                    (root / "responses" / f"{attempt_id}.txt").is_file(),
                )

                restarted_tracker = RunBudgetTracker(
                    RunBudgetLimits(
                        max_provider_calls=5,
                        max_total_input_tokens=100,
                        max_total_output_tokens=100,
                        max_elapsed_seconds=30,
                    )
                )
                restarted = ModelCallRuntimeContext(
                    store,
                    tracker=restarted_tracker,
                )
                self.assertEqual([], restarted.hydrate_tracker_from_store())
                self.assertEqual(1, restarted_tracker.provider_calls)
                self.assertEqual(4, restarted_tracker.total_input_tokens)
                self.assertEqual(0, restarted_tracker.total_output_tokens)
                self.assertEqual(20, restarted_tracker.reserved_output_tokens)

                with self.assertRaises(ProviderCallUncertainError) as replay:
                    restarted.execute_attempt(
                        operation=lambda: self.fail("provider must never be resent"),
                        **arguments,
                    )
                self.assertEqual("provider_call_uncertain", replay.exception.failure_category)
                self.assertEqual(1, provider_calls)
                self.assertFalse(store.receipt_path(attempt_id).exists())

    def test_budget_rejection_writes_terminal_receipt_without_calling_provider(self) -> None:
        tracker = RunBudgetTracker(
            RunBudgetLimits(
                max_provider_calls=1,
                max_total_input_tokens=100,
                max_total_output_tokens=5,
                max_elapsed_seconds=30,
            )
        )
        runtime = ModelCallRuntimeContext(self.store, tracker=tracker)
        provider_calls = 0

        def network() -> ModelResponse:
            nonlocal provider_calls
            provider_calls += 1
            return ModelResponse("must not run")

        with self.assertRaises(ContextBudgetError):
            runtime.execute_attempt(
                call_id="budget-blocked",
                attempt_number=1,
                provider="openai",
                model="gpt-test",
                stage="draft",
                endpoint_type="official",
                request={"messages": [{"role": "user", "content": "private"}]},
                max_output_tokens=6,
                input_tokens=1,
                operation=network,
            )

        self.assertEqual(0, provider_calls)
        receipt = self.store.load_receipt("budget-blocked-a1")
        self.assertEqual("budget_rejected", receipt["status"])
        self.assertEqual([], self.store.list_uncertain_calls())

        fresh_tracker = RunBudgetTracker(
            RunBudgetLimits(
                max_provider_calls=2,
                max_total_input_tokens=100,
                max_total_output_tokens=100,
                max_elapsed_seconds=30,
            )
        )
        with self.assertRaises(ModelCallIntegrityError):
            ModelCallRuntimeContext(
                self.store,
                tracker=fresh_tracker,
            ).execute_attempt(
                call_id="budget-blocked",
                attempt_number=1,
                provider="openai",
                model="gpt-test",
                stage="draft",
                endpoint_type="official",
                request={"messages": [{"role": "user", "content": "private"}]},
                max_output_tokens=6,
                input_tokens=1,
                operation=lambda: self.fail("provider must not run"),
            )
        self.assertEqual(0, fresh_tracker.provider_calls)
        self.assertEqual(0, fresh_tracker.total_input_tokens)
        self.assertEqual(0, fresh_tracker.reserved_output_tokens)

    def test_hydration_restores_succeeded_and_uncertain_budget_without_charging_rejection(self) -> None:
        writer = ModelCallRuntimeContext(
            self.store,
            tracker=RunBudgetTracker(
                RunBudgetLimits(
                    max_provider_calls=10,
                    max_total_input_tokens=100,
                    max_total_output_tokens=100,
                    max_elapsed_seconds=30,
                )
            ),
        )
        writer.execute_attempt(
            call_id="succeeded",
            attempt_number=1,
            provider="openai",
            model="gpt-test",
            stage="draft",
            endpoint_type="official",
            request={"messages": []},
            max_output_tokens=10,
            input_tokens=4,
            operation=lambda: ModelResponse(
                "done",
                usage={"input_tokens": 4, "output_tokens": 3},
                endpoint_type="official",
            ),
        )
        with self.assertRaises(ProviderCallUncertainError):
            writer.execute_attempt(
                call_id="uncertain",
                attempt_number=1,
                provider="openai",
                model="gpt-test",
                stage="draft",
                endpoint_type="official",
                request={"messages": []},
                max_output_tokens=7,
                input_tokens=5,
                operation=lambda: (_ for _ in ()).throw(TimeoutError("unknown")),
            )

        rejecting = ModelCallRuntimeContext(
            self.store,
            tracker=RunBudgetTracker(
                RunBudgetLimits(
                    max_provider_calls=1,
                    max_total_input_tokens=100,
                    max_total_output_tokens=1,
                    max_elapsed_seconds=30,
                )
            ),
        )
        with self.assertRaises(ContextBudgetError):
            rejecting.execute_attempt(
                call_id="rejected",
                attempt_number=1,
                provider="openai",
                model="gpt-test",
                stage="draft",
                endpoint_type="official",
                request={"messages": []},
                max_output_tokens=2,
                input_tokens=6,
                operation=lambda: self.fail("provider must not run"),
            )

        fresh_tracker = RunBudgetTracker(
            RunBudgetLimits(
                max_provider_calls=10,
                max_total_input_tokens=100,
                max_total_output_tokens=100,
                max_elapsed_seconds=30,
            )
        )
        receipt_hashes = ModelCallRuntimeContext(
            self.store,
            tracker=fresh_tracker,
        ).hydrate_tracker_from_store()

        succeeded_receipt = self.store.load_receipt("succeeded-a1")
        self.assertEqual([succeeded_receipt["receipt_hash"]], receipt_hashes)
        self.assertEqual(2, fresh_tracker.provider_calls)
        self.assertEqual(9, fresh_tracker.total_input_tokens)
        self.assertEqual(3, fresh_tracker.total_output_tokens)
        self.assertEqual(7, fresh_tracker.reserved_output_tokens)
        self.assertEqual(
            ["uncertain-a1"],
            [item["attempt_id"] for item in self.store.list_uncertain_calls()],
        )
        self.assertFalse(self.store.receipt_path("uncertain-a1").exists())

    def test_estimator_drift_replays_stored_receipt_and_preserves_uncertain_intent(self) -> None:
        limits = RunBudgetLimits(
            max_provider_calls=5,
            max_total_input_tokens=100,
            max_total_output_tokens=100,
            max_elapsed_seconds=30,
        )
        receipt_store = ModelCallStore(self.root / "counter-drift-receipt")
        original = ModelCallRuntimeContext(
            receipt_store,
            tracker=RunBudgetTracker(limits),
            input_token_counter=lambda _request: 5,
        )
        arguments = {
            "call_id": "counter-drift",
            "attempt_number": 1,
            "provider": "openai",
            "model": "gpt-test",
            "stage": "draft",
            "endpoint_type": "official",
            "request": {"messages": []},
            "max_output_tokens": 10,
        }
        original.execute_attempt(
            operation=lambda: ModelResponse(
                "done",
                usage={"input_tokens": 5, "output_tokens": 2},
                endpoint_type="official",
            ),
            **arguments,
        )

        replay_calls = 0

        def forbidden_replay() -> ModelResponse:
            nonlocal replay_calls
            replay_calls += 1
            return ModelResponse("must not run")

        replay_tracker = RunBudgetTracker(limits)
        restarted = ModelCallRuntimeContext(
            receipt_store,
            tracker=replay_tracker,
            input_token_counter=lambda _request: 6,
        )
        self.assertEqual(
            "done",
            restarted.execute_attempt(operation=forbidden_replay, **arguments).text,
        )
        restarted.execute_attempt(operation=forbidden_replay, **arguments)
        self.assertEqual(0, replay_calls)
        self.assertEqual(1, replay_tracker.provider_calls)
        self.assertEqual(5, replay_tracker.total_input_tokens)
        self.assertEqual(2, replay_tracker.total_output_tokens)

        with self.assertRaises(ModelCallConflictError):
            restarted.execute_attempt(
                operation=forbidden_replay,
                input_tokens=6,
                **arguments,
            )
        changed_output = dict(arguments)
        changed_output["max_output_tokens"] = 11
        with self.assertRaises(ModelCallConflictError):
            restarted.execute_attempt(
                operation=forbidden_replay,
                **changed_output,
            )

        uncertain_store = ModelCallStore(self.root / "counter-drift-uncertain")
        uncertain_writer = ModelCallRuntimeContext(
            uncertain_store,
            tracker=RunBudgetTracker(limits),
            input_token_counter=lambda _request: 5,
        )
        with self.assertRaises(ProviderCallUncertainError):
            uncertain_writer.execute_attempt(
                operation=lambda: (_ for _ in ()).throw(TimeoutError("unknown")),
                **arguments,
            )

        uncertain_tracker = RunBudgetTracker(limits)
        uncertain_restart = ModelCallRuntimeContext(
            uncertain_store,
            tracker=uncertain_tracker,
            input_token_counter=lambda _request: 6,
        )
        with self.assertRaises(ProviderCallUncertainError):
            uncertain_restart.execute_attempt(
                operation=lambda: self.fail("provider must not be resent"),
                **arguments,
            )
        with self.assertRaises(ProviderCallUncertainError):
            uncertain_restart.execute_attempt(
                operation=lambda: self.fail("provider must not be resent"),
                **arguments,
            )
        self.assertEqual(1, uncertain_tracker.provider_calls)
        self.assertEqual(5, uncertain_tracker.total_input_tokens)
        self.assertEqual(10, uncertain_tracker.reserved_output_tokens)

    def test_missing_and_cached_usage_match_live_hydration_and_replay(self) -> None:
        cases = (
            (
                "missing",
                {},
                4,
                20,
            ),
            (
                "input-only",
                {"input_tokens": 3},
                3,
                20,
            ),
            (
                "anthropic-cache",
                {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 3,
                    "cache_read_input_tokens": 11,
                    "output_tokens": 4,
                },
                16,
                4,
            ),
        )
        for name, usage, expected_input, expected_output in cases:
            with self.subTest(name=name):
                store = ModelCallStore(self.root / f"settlement-{name}")
                limits = RunBudgetLimits(
                    max_provider_calls=5,
                    max_total_input_tokens=100,
                    max_total_output_tokens=100,
                    max_elapsed_seconds=30,
                )
                live_tracker = RunBudgetTracker(limits)
                writer = ModelCallRuntimeContext(store, tracker=live_tracker)
                arguments = {
                    "call_id": f"settlement-{name}",
                    "attempt_number": 1,
                    "provider": "anthropic",
                    "model": "claude-test",
                    "stage": "draft",
                    "endpoint_type": "official",
                    "request": {"messages": []},
                    "max_output_tokens": 20,
                    "input_tokens": 4,
                }
                writer.execute_attempt(
                    operation=lambda usage=usage: ModelResponse(
                        "正文",
                        usage=usage,
                        endpoint_type="official",
                    ),
                    **arguments,
                )
                live_report = live_tracker.report()
                self.assertEqual(expected_input, live_report["total_input_tokens"])
                self.assertEqual(expected_output, live_report["total_output_tokens"])
                self.assertEqual(0, live_report["reserved_output_tokens"])
                self.assertEqual(expected_output, live_report["charged_output_tokens"])
                self.assertEqual(0, live_report["unsettled_attempt_count"])

                fresh_tracker = RunBudgetTracker(limits)
                restarted = ModelCallRuntimeContext(store, tracker=fresh_tracker)
                receipt_hashes = restarted.hydrate_tracker_from_store()
                self.assertEqual(1, len(receipt_hashes))
                self.assertEqual(receipt_hashes, restarted.hydrate_tracker_from_store())
                restarted.execute_attempt(
                    operation=lambda: self.fail("provider must not be resent"),
                    **arguments,
                )
                fresh_report = fresh_tracker.report()
                for field in (
                    "provider_calls",
                    "total_input_tokens",
                    "total_output_tokens",
                    "reserved_output_tokens",
                    "charged_output_tokens",
                    "unsettled_attempt_count",
                ):
                    self.assertEqual(live_report[field], fresh_report[field])

    def test_tampered_response_artifact_fails_closed(self) -> None:
        arguments = {
            "call_id": "tamper-call",
            "attempt_number": 1,
            "provider": "anthropic",
            "model": "claude-test",
            "stage": "polish",
            "endpoint_type": "official",
            "request": {"chapter_digest": "a" * 64},
            "max_output_tokens": 10,
            "input_tokens": 2,
        }
        self.runtime.execute_attempt(
            operation=lambda: ModelResponse("original", endpoint_type="official"),
            **arguments,
        )
        atomic_write_text(self.root / "responses" / "tamper-call-a1.txt", "tampered")

        with self.assertRaises(ModelCallIntegrityError):
            self.runtime.execute_attempt(
                operation=lambda: self.fail("network must not run"),
                **arguments,
            )

    def test_openai_client_context_records_metadata_and_receipt_prevents_resend(self) -> None:
        calls = 0

        class FakeCompletions:
            def create(inner_self, **kwargs: object) -> object:
                nonlocal calls
                calls += 1
                self.assertTrue(self.store.intent_path("openai-call-a1").is_file())
                return SimpleNamespace(
                    id="chatcmpl-1",
                    model="gpt-actual",
                    usage=SimpleNamespace(
                        prompt_tokens=5,
                        completion_tokens=2,
                        total_tokens=7,
                    ),
                    choices=[
                        SimpleNamespace(
                            finish_reason="stop",
                            message=SimpleNamespace(content="openai text"),
                        )
                    ],
                )

        class FakeOpenAI:
            def __init__(inner_self, **kwargs: object) -> None:
                inner_self.chat = SimpleNamespace(completions=FakeCompletions())

        environment = {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_MODEL": "gpt-requested",
            "OPENAI_BASE_URL": "",
            "OPENAI_STREAM": "false",
            "OPENAI_MAX_OUTPUT_TOKENS": "20",
        }
        with patch.dict(os.environ, environment, clear=False):
            with patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=FakeOpenAI)}):
                with use_model_call_runtime(self.runtime):
                    first = chat_completion(
                        [{"role": "user", "content": "prompt"}],
                        call_id="openai-call",
                        input_tokens=6,
                    )
                    second = chat_completion(
                        [{"role": "user", "content": "prompt"}],
                        call_id="openai-call",
                        input_tokens=6,
                    )

        self.assertIsInstance(first, ModelResponse)
        self.assertEqual("openai text", second)
        self.assertEqual(1, calls)
        self.assertEqual("chatcmpl-1", first.request_id)
        self.assertEqual("gpt-actual", first.actual_model)
        self.assertEqual("stop", first.finish_reason)
        self.assertEqual(7, first.usage["total_tokens"])
        self.assertEqual("official", first.endpoint_type)

    def test_openai_client_passes_configured_identity_to_token_resolver(self) -> None:
        class FakeCompletions:
            @staticmethod
            def create(**kwargs: object) -> object:
                return SimpleNamespace(
                    id="chatcmpl-compatible",
                    model="gateway-actual",
                    usage=SimpleNamespace(
                        prompt_tokens=5,
                        completion_tokens=2,
                        total_tokens=7,
                    ),
                    choices=[
                        SimpleNamespace(
                            finish_reason="stop",
                            message=SimpleNamespace(content="gateway text"),
                        )
                    ],
                )

        class FakeOpenAI:
            def __init__(inner_self, **kwargs: object) -> None:
                inner_self.chat = SimpleNamespace(completions=FakeCompletions())

        messages = [{"role": "user", "content": "生产身份绑定"}]
        environment = {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_MODEL": "gateway-model",
            "OPENAI_BASE_URL": "https://gateway.invalid/v1",
            "OPENAI_STREAM": "false",
        }
        with (
            patch.dict(os.environ, environment, clear=False),
            patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=FakeOpenAI)}),
            patch(
                "core.model_call_runtime.model_token_counter",
                return_value=None,
            ) as resolver,
        ):
            response = chat_completion(
                messages,
                call_id="openai-configured-identity",
                max_tokens=20,
                model_call_runtime=self.runtime,
            )

        self.assertEqual("gateway text", response)
        resolver.assert_called_once_with(
            provider="openai",
            model="gateway-model",
            endpoint_type="openai_compatible",
        )
        evidence_request = {
            "model": "gateway-model",
            "messages": messages,
            "temperature": 0.8,
            "max_tokens": 20,
            "stream": False,
        }
        expected_reservation = conservative_calibrated_token_estimate(
            canonical_json_bytes(
                evidence_request,
                exclude_environment_fields=False,
            ).decode("utf-8")
        )
        intent = self.store.load_intent("openai-configured-identity-a1")
        self.assertEqual(
            expected_reservation,
            intent["budget_reservation"]["reserved_input_tokens"],
        )

    def test_openai_uncertain_runtime_stops_generic_retry(self) -> None:
        calls = 0

        class FakeCompletions:
            def create(inner_self, **kwargs: object) -> object:
                nonlocal calls
                calls += 1
                raise TimeoutError("unknown provider boundary")

        class FakeOpenAI:
            def __init__(inner_self, **kwargs: object) -> None:
                inner_self.chat = SimpleNamespace(completions=FakeCompletions())

        environment = {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_MODEL": "gpt-test",
            "OPENAI_BASE_URL": "",
            "OPENAI_STREAM": "false",
        }
        with patch.dict(os.environ, environment, clear=False):
            with patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=FakeOpenAI)}):
                with self.assertRaises(ModelCallError) as raised:
                    chat_completion(
                        [{"role": "user", "content": "prompt"}],
                        call_id="openai-uncertain",
                        input_tokens=3,
                        model_call_runtime=self.runtime,
                        retry_policy=self._retry_policy(MODEL_READ_GENERATION),
                    )

        self.assertEqual(1, calls)
        self.assertEqual("provider_call_uncertain", raised.exception.failure_category)
        self.assertEqual("non_retryable", raised.exception.retry_stop_reason)

    def test_claude_client_uses_contextvar_and_extracts_metadata(self) -> None:
        class FakeMessages:
            def create(inner_self, **kwargs: object) -> object:
                self.assertTrue(self.store.intent_path("claude-call-a1").is_file())
                return SimpleNamespace(
                    id="msg-1",
                    model="claude-actual",
                    stop_reason="end_turn",
                    usage=SimpleNamespace(input_tokens=8, output_tokens=4),
                    content=[SimpleNamespace(text="polished")],
                )

        class FakeAnthropic:
            def __init__(inner_self, **kwargs: object) -> None:
                inner_self.messages = FakeMessages()

        environment = {
            "ANTHROPIC_API_KEY": "test-key",
            "ANTHROPIC_AUTH_TOKEN": "",
            "CLAUDE_MODEL": "claude-requested",
            "ANTHROPIC_MODEL": "",
            "CLAUDE_BASE_URL": "",
            "ANTHROPIC_BASE_URL": "",
            "CLAUDE_STREAM": "false",
            "CLAUDE_MAX_TOKENS": "30",
        }
        with patch.dict(os.environ, environment, clear=False):
            with patch.dict(
                sys.modules,
                {"anthropic": SimpleNamespace(Anthropic=FakeAnthropic)},
            ):
                with use_model_call_runtime(self.runtime):
                    response = polish_chapter(
                        "chapter",
                        call_id="claude-call",
                        input_tokens=9,
                    )

        self.assertIsInstance(response, ModelResponse)
        self.assertEqual("msg-1", response.request_id)
        self.assertEqual("claude-actual", response.actual_model)
        self.assertEqual("end_turn", response.finish_reason)
        self.assertEqual(4, response.usage["output_tokens"])
        self.assertEqual("official", response.endpoint_type)

    def test_stream_extractors_capture_terminal_metadata(self) -> None:
        openai_stream = [
            SimpleNamespace(
                id="stream-1",
                model="gpt-stream",
                usage=None,
                choices=[
                    SimpleNamespace(
                        finish_reason=None,
                        delta=SimpleNamespace(content="stream "),
                    )
                ],
            ),
            SimpleNamespace(
                id="stream-1",
                model="gpt-stream",
                usage=SimpleNamespace(
                    prompt_tokens=3,
                    completion_tokens=2,
                    total_tokens=5,
                ),
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        delta=SimpleNamespace(content="text"),
                    )
                ],
            ),
        ]
        openai = _extract_streamed_message_content(
            openai_stream,
            stage="draft",
            model="requested",
            endpoint_type="official",
        )

        final_message = SimpleNamespace(
            id="msg-stream",
            model="claude-stream",
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=6, output_tokens=2),
        )

        class FakeStream:
            text_stream = ["claude ", "text"]

            def __enter__(inner_self) -> "FakeStream":
                return inner_self

            def __exit__(inner_self, *args: object) -> None:
                return None

            def get_final_message(inner_self) -> object:
                return final_message

        fake_client = SimpleNamespace(
            messages=SimpleNamespace(stream=lambda **kwargs: FakeStream())
        )
        claude = _stream_message_text(
            fake_client,
            {"model": "requested"},
            model="requested",
            endpoint_type="official",
        )

        self.assertEqual("stream text", openai)
        self.assertEqual("stop", openai.finish_reason)
        self.assertEqual(5, openai.usage["total_tokens"])
        self.assertEqual("claude text", claude)
        self.assertEqual("msg-stream", claude.request_id)
        self.assertEqual("end_turn", claude.finish_reason)
        self.assertEqual(2, claude.usage["output_tokens"])

    def test_nonstream_extractors_are_structured_but_string_compatible(self) -> None:
        openai = _extract_message_content(
            SimpleNamespace(
                id="id-1",
                model="gpt-actual",
                usage={"total_tokens": 2},
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(content="text"),
                    )
                ],
            ),
            stage="draft",
            model="gpt-requested",
        )
        claude = _extract_message_text(
            SimpleNamespace(
                id="msg-1",
                model="claude-actual",
                stop_reason="end_turn",
                usage={"output_tokens": 1},
                content=[SimpleNamespace(text="polished")],
            ),
            model="claude-requested",
        )

        self.assertIsInstance(openai, str)
        self.assertIsInstance(openai, ModelResponse)
        self.assertEqual("gpt-actual", openai.actual_model)
        self.assertIsInstance(claude, ModelResponse)
        self.assertEqual("claude-actual", claude.actual_model)


if __name__ == "__main__":
    unittest.main()
