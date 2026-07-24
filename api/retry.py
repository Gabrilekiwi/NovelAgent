from __future__ import annotations

import email.utils
import os
import random
import time
import warnings
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Generic, Mapping, TypeVar

from api.contracts import ModelResponse, classify_model_failure, is_retryable_failure
from core.schema import validate_schema


T = TypeVar("T")
Clock = Callable[[], float]
Sleeper = Callable[[float], None]
RandomSource = Callable[[], float]
BudgetRemaining = Callable[[], float | None]

MODEL_READ_GENERATION = "model_read_generation"
CLAUDE_POLISH = "claude_polish"
NOTION_READ_QUERY = "notion_read_query"
NOTION_CREATE = "notion_create"
RETRY_PROFILES = frozenset(
    {MODEL_READ_GENERATION, CLAUDE_POLISH, NOTION_READ_QUERY, NOTION_CREATE}
)
MAX_PROVIDER_ATTEMPTS = 10

_LAST_RETRY_REPORTS: ContextVar[tuple[dict[str, Any], ...]] = ContextVar(
    "novelagent_retry_reports",
    default=(),
)


class PartialResponseError(RuntimeError):
    """Marks a streaming failure and whether any response content was observed."""

    def __init__(
        self,
        cause: BaseException,
        *,
        partial_content_received: bool,
        partial_response: ModelResponse | None = None,
    ) -> None:
        if partial_response is not None and not isinstance(partial_response, ModelResponse):
            raise TypeError("partial_response must be a ModelResponse or None")
        super().__init__(f"{type(cause).__name__} during streamed response")
        self.cause = cause
        self.partial_response = partial_response
        self.partial_content_received = bool(
            partial_content_received or (partial_response is not None and partial_response.text)
        )


class RetryOperationError(RuntimeError):
    def __init__(
        self,
        *,
        cause: BaseException,
        report: Mapping[str, Any],
        failure_category: str,
        retryable: bool,
        partial_content_received: bool,
    ) -> None:
        super().__init__(f"provider operation stopped: {report.get('stop_reason')}")
        self.cause = cause
        self.report = dict(report)
        self.failure_category = failure_category
        self.retryable = bool(retryable)
        self.partial_content_received = bool(partial_content_received)


@dataclass(frozen=True)
class RetryExecution(Generic[T]):
    value: T
    report: dict[str, Any]


@dataclass(frozen=True)
class RetryPolicy:
    profile: str
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 8.0
    jitter_ratio: float = 0.2
    deadline_seconds: float = 180.0
    allow_retry: bool = True
    clock: Clock = field(default=time.monotonic, repr=False, compare=False)
    sleep: Sleeper = field(default=time.sleep, repr=False, compare=False)
    random_source: RandomSource = field(default=random.random, repr=False, compare=False)
    wall_clock: Callable[[], datetime] = field(
        default=lambda: datetime.now(timezone.utc),
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.profile not in RETRY_PROFILES:
            raise ValueError(f"unsupported retry operation profile: {self.profile}")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.max_attempts > MAX_PROVIDER_ATTEMPTS:
            raise ValueError(f"max_attempts cannot exceed {MAX_PROVIDER_ATTEMPTS}")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds cannot be smaller than base_delay_seconds")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")
        if self.deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive")
        if self.profile == NOTION_CREATE and (self.allow_retry or self.max_attempts != 1):
            raise ValueError("Notion create must use a single attempt with generic retry disabled")

    def execute(
        self,
        operation: Callable[[], T],
        *,
        budget_remaining_seconds: BudgetRemaining | None = None,
    ) -> RetryExecution[T]:
        started = self.clock()
        history: list[dict[str, Any]] = []
        last_error: BaseException | None = None
        last_category = "provider_error"
        last_retryable = False
        last_partial = False

        for attempt_number in range(1, self.max_attempts + 1):
            attempt_started = self.clock()
            try:
                value = operation()
            except Exception as raw_error:
                last_error = raw_error
                last_partial = partial_content_received(raw_error)
                last_category = classify_retry_exception(raw_error)
                last_retryable = is_retryable_failure(last_category)
                attempt_finished = self.clock()
                entry = {
                    "attempt": attempt_number,
                    "outcome": "failed",
                    "failure_category": last_category,
                    "retryable": last_retryable,
                    "partial_content_received": last_partial,
                    "cause_type": type(_underlying_error(raw_error)).__name__,
                    "elapsed_ms": _milliseconds(attempt_finished - attempt_started),
                    "delay_ms": 0,
                    "retry_after_ms": None,
                }
                stop_reason = self._stop_reason_before_delay(
                    attempt_number=attempt_number,
                    retryable=last_retryable,
                    partial=last_partial,
                    now=attempt_finished,
                    started=started,
                )
                delay = 0.0
                if stop_reason is None:
                    retry_after = retry_after_seconds(raw_error, now=self.wall_clock())
                    if retry_after is not None:
                        entry["retry_after_ms"] = _milliseconds(retry_after)
                        delay = retry_after
                    else:
                        delay = self._backoff_delay(attempt_number)
                    stop_reason = self._delay_stop_reason(
                        delay=delay,
                        now=attempt_finished,
                        started=started,
                        budget_remaining_seconds=budget_remaining_seconds,
                    )
                history.append(entry)
                if stop_reason is not None:
                    report = _retry_report(
                        self,
                        history,
                        elapsed=self.clock() - started,
                        completed=False,
                        stop_reason=stop_reason,
                    )
                    record_retry_report(report)
                    raise RetryOperationError(
                        cause=raw_error,
                        report=report,
                        failure_category=last_category,
                        retryable=last_retryable,
                        partial_content_received=last_partial,
                    ) from raw_error
                entry["delay_ms"] = _milliseconds(delay)
                self.sleep(delay)
                continue

            attempt_finished = self.clock()
            history.append(
                {
                    "attempt": attempt_number,
                    "outcome": "succeeded",
                    "failure_category": None,
                    "retryable": False,
                    "partial_content_received": False,
                    "cause_type": None,
                    "elapsed_ms": _milliseconds(attempt_finished - attempt_started),
                    "delay_ms": 0,
                    "retry_after_ms": None,
                }
            )
            report = _retry_report(
                self,
                history,
                elapsed=attempt_finished - started,
                completed=True,
                stop_reason="succeeded",
            )
            record_retry_report(report)
            return RetryExecution(value=value, report=report)

        # The loop is structurally exhaustive, but keep a fail-closed guard for future changes.
        assert last_error is not None
        report = _retry_report(
            self,
            history,
            elapsed=self.clock() - started,
            completed=False,
            stop_reason="max_attempts",
        )
        record_retry_report(report)
        raise RetryOperationError(
            cause=last_error,
            report=report,
            failure_category=last_category,
            retryable=last_retryable,
            partial_content_received=last_partial,
        ) from last_error

    def _stop_reason_before_delay(
        self,
        *,
        attempt_number: int,
        retryable: bool,
        partial: bool,
        now: float,
        started: float,
    ) -> str | None:
        if partial:
            return "partial_content_received"
        if not self.allow_retry or not retryable:
            return "non_retryable"
        if attempt_number >= self.max_attempts:
            return "max_attempts"
        if now - started >= self.deadline_seconds:
            return "deadline_exhausted"
        return None

    def _delay_stop_reason(
        self,
        *,
        delay: float,
        now: float,
        started: float,
        budget_remaining_seconds: BudgetRemaining | None,
    ) -> str | None:
        remaining_deadline = self.deadline_seconds - (now - started)
        if remaining_deadline <= 0 or delay >= remaining_deadline:
            return "deadline_exhausted"
        if budget_remaining_seconds is not None:
            remaining_budget = budget_remaining_seconds()
            if remaining_budget is not None and (remaining_budget <= 0 or delay >= remaining_budget):
                return "run_budget_exhausted"
        return None

    def _backoff_delay(self, attempt_number: int) -> float:
        base = min(self.max_delay_seconds, self.base_delay_seconds * (2 ** (attempt_number - 1)))
        random_value = min(1.0, max(0.0, float(self.random_source())))
        multiplier = 1.0 + ((random_value * 2.0) - 1.0) * self.jitter_ratio
        return max(0.0, base * multiplier)


def retry_policy_for_profile(
    profile: str,
    *,
    config=None,
    legacy_openai_compat: bool = False,
    clock: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
    random_source: RandomSource = random.random,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> RetryPolicy:
    if profile not in RETRY_PROFILES:
        raise ValueError(f"unsupported retry operation profile: {profile}")
    if config is None:
        from core.config import get_config

        config = get_config()
    if profile == NOTION_CREATE:
        return RetryPolicy(
            profile=profile,
            max_attempts=1,
            allow_retry=False,
            deadline_seconds=max(1.0, float(config.provider_retry_deadline_seconds)),
            clock=clock,
            sleep=sleep,
            random_source=random_source,
            wall_clock=wall_clock,
        )

    max_attempts = min(MAX_PROVIDER_ATTEMPTS, max(1, int(config.provider_max_attempts)))
    if legacy_openai_compat and _configured_env("OPENAI_MAX_RETRIES"):
        warnings.warn(
            "OPENAI_MAX_RETRIES is deprecated; use PROVIDER_MAX_ATTEMPTS. "
            "It is temporarily mapped to max_attempts=max_retries+1.",
            FutureWarning,
            stacklevel=2,
        )
        if not _configured_env("PROVIDER_MAX_ATTEMPTS"):
            max_attempts = min(
                MAX_PROVIDER_ATTEMPTS,
                max(1, int(config.openai_max_retries) + 1),
            )
    return RetryPolicy(
        profile=profile,
        max_attempts=max_attempts,
        base_delay_seconds=max(0.0, float(config.provider_retry_base_delay_seconds)),
        max_delay_seconds=max(
            max(0.0, float(config.provider_retry_base_delay_seconds)),
            float(config.provider_retry_max_delay_seconds),
        ),
        jitter_ratio=min(1.0, max(0.0, float(config.provider_retry_jitter_ratio))),
        deadline_seconds=max(1.0, float(config.provider_retry_deadline_seconds)),
        allow_retry=True,
        clock=clock,
        sleep=sleep,
        random_source=random_source,
        wall_clock=wall_clock,
    )


def classify_retry_exception(error: BaseException) -> str:
    chain = _error_chain(error)
    for item in chain:
        explicit = getattr(item, "failure_category", None)
        if isinstance(explicit, str) and explicit:
            return explicit
    for item in chain:
        status = _status_code(item)
        if status == 429:
            return "rate_limit"
        if status in {500, 502, 503, 504}:
            return "transient_provider_error"
        if status in {401, 403}:
            return "authentication"
        if status is not None and 400 <= status < 500:
            return "configuration"
    for item in chain:
        lowered_type = type(item).__name__.lower()
        if "schema" in lowered_type:
            return "schema"
        if "output" in lowered_type or "validation" in lowered_type:
            return "output_contract"
    return classify_model_failure(
        " ".join(type(item).__name__ for item in chain),
        " ".join(str(item) for item in chain),
    )


def retry_after_seconds(error: BaseException, *, now: datetime | None = None) -> float | None:
    value = None
    for item in _error_chain(error):
        headers = getattr(item, "headers", None)
        response = getattr(item, "response", None)
        if headers is None and response is not None:
            headers = getattr(response, "headers", None)
        value = _header_value(headers, "retry-after")
        if value is not None:
            break
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (parsed.astimezone(timezone.utc) - current.astimezone(timezone.utc)).total_seconds())


def partial_content_received(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if bool(getattr(current, "partial_content_received", False)):
            return True
        current = _next_cause(current)
    return False


def record_retry_report(report: Mapping[str, Any]) -> None:
    current = _LAST_RETRY_REPORTS.get()
    _LAST_RETRY_REPORTS.set((*current[-99:], dict(report)))


def reset_retry_telemetry() -> None:
    _LAST_RETRY_REPORTS.set(())


def consume_retry_telemetry() -> list[dict[str, Any]]:
    reports = [dict(report) for report in _LAST_RETRY_REPORTS.get()]
    _LAST_RETRY_REPORTS.set(())
    return reports


def retry_telemetry_snapshot() -> list[dict[str, Any]]:
    return [dict(report) for report in _LAST_RETRY_REPORTS.get()]


def _retry_report(
    policy: RetryPolicy,
    history: list[dict[str, Any]],
    *,
    elapsed: float,
    completed: bool,
    stop_reason: str,
) -> dict[str, Any]:
    return validate_schema({
        "schema_version": "1.0",
        "profile": policy.profile,
        "max_attempts": policy.max_attempts,
        "attempts": len(history),
        "completed": bool(completed),
        "stop_reason": stop_reason,
        "elapsed_ms": _milliseconds(elapsed),
        "history": [dict(entry) for entry in history],
    }, "provider_retry_report.schema.json")


def _underlying_error(error: BaseException) -> BaseException:
    return _error_chain(error)[-1]


def _error_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = _next_cause(current)
    return chain


def _next_cause(error: BaseException) -> BaseException | None:
    explicit = getattr(error, "cause", None)
    if isinstance(explicit, BaseException):
        return explicit
    if isinstance(error.__cause__, BaseException):
        return error.__cause__
    return None


def _status_code(error: BaseException) -> int | None:
    for value in (
        getattr(error, "status_code", None),
        getattr(error, "status", None),
        getattr(getattr(error, "response", None), "status_code", None),
        getattr(getattr(error, "response", None), "status", None),
    ):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _header_value(headers: Any, name: str) -> Any:
    if not isinstance(headers, Mapping):
        return None
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return value
    return None


def _configured_env(name: str) -> bool:
    value = os.getenv(name)
    return value is not None and bool(value.strip())


def _milliseconds(seconds: float) -> int:
    return int(max(0.0, float(seconds)) * 1000)


__all__ = [
    "CLAUDE_POLISH",
    "MODEL_READ_GENERATION",
    "MAX_PROVIDER_ATTEMPTS",
    "NOTION_CREATE",
    "NOTION_READ_QUERY",
    "PartialResponseError",
    "RETRY_PROFILES",
    "RetryExecution",
    "RetryOperationError",
    "RetryPolicy",
    "classify_retry_exception",
    "consume_retry_telemetry",
    "partial_content_received",
    "record_retry_report",
    "reset_retry_telemetry",
    "retry_after_seconds",
    "retry_policy_for_profile",
    "retry_telemetry_snapshot",
]
