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
from core.context_budget import ContextBudgetError, RunBudgetLimits, RunBudgetTracker
from core.engine.persistence import atomic_write_text
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
