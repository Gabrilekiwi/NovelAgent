from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from api.contracts import ModelOutputError
from api.retry import (
    MODEL_READ_GENERATION,
    NOTION_CREATE,
    PartialResponseError,
    RetryOperationError,
    RetryPolicy,
    consume_retry_telemetry,
    reset_retry_telemetry,
    retry_policy_for_profile,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class HttpFailure(RuntimeError):
    def __init__(self, status_code: int, *, retry_after: str | None = None) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.headers = {"Retry-After": retry_after} if retry_after is not None else {}


def _config(**overrides):
    values = {
        "provider_max_attempts": 3,
        "provider_retry_base_delay_seconds": 1.0,
        "provider_retry_max_delay_seconds": 8.0,
        "provider_retry_jitter_ratio": 0.2,
        "provider_retry_deadline_seconds": 180.0,
        "openai_max_retries": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ProviderRetryTest(unittest.TestCase):
    def tearDown(self) -> None:
        reset_retry_telemetry()

    def test_retries_only_retryable_failures_with_deterministic_backoff(self) -> None:
        clock = FakeClock()
        random_values = iter([0.0, 1.0])
        policy = RetryPolicy(
            profile=MODEL_READ_GENERATION,
            max_attempts=3,
            base_delay_seconds=1.0,
            max_delay_seconds=8.0,
            jitter_ratio=0.2,
            deadline_seconds=20,
            clock=clock,
            sleep=clock.sleep,
            random_source=lambda: next(random_values),
        )
        calls = 0

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("secret request body timed out")
            if calls == 2:
                raise ConnectionError("connection reset")
            return "ok"

        execution = policy.execute(operation)

        self.assertEqual("ok", execution.value)
        self.assertEqual(3, execution.report["attempts"])
        self.assertEqual([0.8, 2.4], clock.sleeps)
        self.assertEqual(
            ["timeout", "connection", None],
            [item["failure_category"] for item in execution.report["history"]],
        )
        self.assertNotIn("secret request body", json.dumps(execution.report))

    def test_retry_after_takes_precedence(self) -> None:
        clock = FakeClock()
        policy = RetryPolicy(
            profile=MODEL_READ_GENERATION,
            max_attempts=2,
            deadline_seconds=10,
            jitter_ratio=0,
            clock=clock,
            sleep=clock.sleep,
        )
        calls = 0

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise HttpFailure(429, retry_after="3")
            return "ok"

        execution = policy.execute(operation)

        self.assertEqual([3.0], clock.sleeps)
        self.assertEqual(3000, execution.report["history"][0]["retry_after_ms"])

    def test_retry_after_cannot_cross_deadline(self) -> None:
        clock = FakeClock()
        policy = RetryPolicy(
            profile=MODEL_READ_GENERATION,
            max_attempts=3,
            deadline_seconds=2,
            clock=clock,
            sleep=clock.sleep,
        )

        with self.assertRaises(RetryOperationError) as context:
            policy.execute(lambda: (_ for _ in ()).throw(HttpFailure(429, retry_after="3")))

        self.assertEqual("deadline_exhausted", context.exception.report["stop_reason"])
        self.assertEqual([], clock.sleeps)
        self.assertEqual(1, context.exception.report["attempts"])

    def test_run_budget_blocks_next_attempt(self) -> None:
        clock = FakeClock()
        policy = RetryPolicy(
            profile=MODEL_READ_GENERATION,
            max_attempts=3,
            jitter_ratio=0,
            deadline_seconds=20,
            clock=clock,
            sleep=clock.sleep,
        )

        with self.assertRaises(RetryOperationError) as context:
            policy.execute(
                lambda: (_ for _ in ()).throw(TimeoutError("timeout")),
                budget_remaining_seconds=lambda: 0.5,
            )

        self.assertEqual("run_budget_exhausted", context.exception.report["stop_reason"])
        self.assertEqual([], clock.sleeps)

    def test_configuration_schema_and_output_failures_do_not_retry(self) -> None:
        for error in (
            HttpFailure(401),
            type("SchemaFailure", (RuntimeError,), {})("bad schema"),
            ModelOutputError("output contract invalid"),
        ):
            with self.subTest(error=type(error).__name__):
                clock = FakeClock()
                policy = RetryPolicy(
                    profile=MODEL_READ_GENERATION,
                    max_attempts=3,
                    clock=clock,
                    sleep=clock.sleep,
                )
                with self.assertRaises(RetryOperationError) as context:
                    policy.execute(lambda error=error: (_ for _ in ()).throw(error))
                self.assertEqual(1, context.exception.report["attempts"])
                self.assertEqual("non_retryable", context.exception.report["stop_reason"])
                self.assertEqual([], clock.sleeps)

    def test_outer_auth_status_is_not_hidden_by_connection_cause(self) -> None:
        error = HttpFailure(401)
        error.__cause__ = ConnectionError("socket closed")
        policy = RetryPolicy(profile=MODEL_READ_GENERATION, max_attempts=3)

        with self.assertRaises(RetryOperationError) as context:
            policy.execute(lambda: (_ for _ in ()).throw(error))

        self.assertEqual("authentication", context.exception.failure_category)
        self.assertEqual(1, context.exception.report["attempts"])

    def test_partial_stream_content_forbids_replay_but_empty_stream_can_retry(self) -> None:
        clock = FakeClock()
        policy = RetryPolicy(
            profile=MODEL_READ_GENERATION,
            max_attempts=2,
            base_delay_seconds=0,
            max_delay_seconds=0,
            jitter_ratio=0,
            deadline_seconds=10,
            clock=clock,
            sleep=clock.sleep,
        )
        with self.assertRaises(RetryOperationError) as context:
            policy.execute(
                lambda: (_ for _ in ()).throw(
                    PartialResponseError(TimeoutError("timeout"), partial_content_received=True)
                )
            )
        self.assertEqual("partial_content_received", context.exception.report["stop_reason"])
        self.assertEqual(1, context.exception.report["attempts"])

        calls = 0

        def empty_then_success() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PartialResponseError(TimeoutError("timeout"), partial_content_received=False)
            return "ok"

        self.assertEqual("ok", policy.execute(empty_then_success).value)
        self.assertEqual(2, calls)

    def test_notion_create_profile_cannot_enable_generic_retry(self) -> None:
        policy = retry_policy_for_profile(NOTION_CREATE, config=_config())
        self.assertEqual(1, policy.max_attempts)
        self.assertFalse(policy.allow_retry)
        with self.assertRaisesRegex(ValueError, "Notion create"):
            RetryPolicy(profile=NOTION_CREATE)

    def test_legacy_openai_retries_map_to_attempts_with_warning(self) -> None:
        with patch.dict(
            os.environ,
            {"OPENAI_MAX_RETRIES": "4", "PROVIDER_MAX_ATTEMPTS": ""},
        ), self.assertWarns(FutureWarning):
            policy = retry_policy_for_profile(
                MODEL_READ_GENERATION,
                config=_config(openai_max_retries=4),
                legacy_openai_compat=True,
            )

        self.assertEqual(5, policy.max_attempts)

    def test_context_telemetry_records_success_and_failure_without_messages(self) -> None:
        reset_retry_telemetry()
        policy = RetryPolicy(
            profile=MODEL_READ_GENERATION,
            max_attempts=1,
            deadline_seconds=10,
        )
        self.assertEqual("ok", policy.execute(lambda: "ok").value)
        with self.assertRaises(RetryOperationError):
            policy.execute(lambda: (_ for _ in ()).throw(ValueError("api_key=secret full prose")))

        reports = consume_retry_telemetry()
        self.assertEqual(2, len(reports))
        serialized = json.dumps(reports)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("full prose", serialized)


if __name__ == "__main__":
    unittest.main()
