from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
import time
from typing import Any, Callable, Iterable, Mapping

from core.schema import validate_schema


CONTEXT_BUDGET_SCHEMA_VERSION = "1.0"
ESTIMATOR_VERSION = "utf8-upper-bound-v1"
NEW_TOKEN_COUNT_MODES = frozenset(
    {
        "provider_exact",
        "model_tokenizer",
        "calibrated_estimate",
    }
)
LEGACY_TOKEN_COUNT_MODES = frozenset({"exact", "estimate"})
SAFE_ENDPOINT_TYPES = frozenset({"official", "openai_compatible", "unknown"})
TokenCounterCallable = Callable[[str], int]
ExactTokenCounter = TokenCounterCallable


class ContextBudgetError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _require_endpoint_type(endpoint_type: str) -> None:
    if endpoint_type not in SAFE_ENDPOINT_TYPES:
        raise ContextBudgetError(
            "endpoint_type_invalid",
            f"endpoint_type must be one of {sorted(SAFE_ENDPOINT_TYPES)}",
        )


def _require_safe_metadata(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContextBudgetError("token_metadata_invalid", f"{name} must be a non-empty string")
    if len(value) > 200 or any(ord(character) < 32 for character in value):
        raise ContextBudgetError(
            "token_metadata_invalid",
            f"{name} must be a compact, control-character-free label",
        )


@dataclass(frozen=True)
class CalibratedTokenEstimator:
    """Deterministic, offline token estimate with explicit calibration metadata."""

    version: str = ESTIMATOR_VERSION
    tokens_per_utf8_byte: float = 1.0
    fixed_overhead_tokens: int = 0

    def __post_init__(self) -> None:
        _require_safe_metadata(self.version, "calibration version")
        ratio = self.tokens_per_utf8_byte
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(ratio) or ratio <= 0:
            raise ContextBudgetError(
                "token_calibration_invalid",
                "tokens_per_utf8_byte must be a positive finite number",
            )
        overhead = self.fixed_overhead_tokens
        if isinstance(overhead, bool) or not isinstance(overhead, int) or overhead < 0:
            raise ContextBudgetError(
                "token_calibration_invalid",
                "fixed_overhead_tokens must be a non-negative integer",
            )

    def estimate(self, text: str) -> int:
        byte_count = len(str(text).encode("utf-8"))
        return math.ceil(byte_count * self.tokens_per_utf8_byte) + self.fixed_overhead_tokens

    def __call__(self, text: str) -> int:
        return self.estimate(text)


DEFAULT_CALIBRATED_ESTIMATOR = CalibratedTokenEstimator()
CJK_CHARACTER_OUTPUT_ESTIMATOR = CalibratedTokenEstimator(
    version="cjk-one-token-per-character-v1",
    tokens_per_utf8_byte=1 / 3,
)


@dataclass(frozen=True)
class TokenCounter:
    """A counter bound to the model and endpoint it is safe to describe."""

    counter: TokenCounterCallable
    count_mode: str
    provider: str
    model: str
    endpoint_type: str
    version: str
    tokenizer: str | None = None
    model_is_known: bool = False

    def __post_init__(self) -> None:
        if not callable(self.counter):
            raise ContextBudgetError("token_counter_invalid", "counter must be callable")
        if self.count_mode not in {"provider_exact", "model_tokenizer"}:
            raise ContextBudgetError(
                "token_counter_invalid",
                "counter mode must be provider_exact or model_tokenizer",
            )
        _require_safe_metadata(self.provider, "counter provider")
        _require_safe_metadata(self.model, "counter model")
        _require_endpoint_type(self.endpoint_type)
        _require_safe_metadata(self.version, "counter version")
        if not isinstance(self.model_is_known, bool):
            raise ContextBudgetError("token_counter_invalid", "model_is_known must be boolean")
        if self.tokenizer is not None:
            _require_safe_metadata(self.tokenizer, "tokenizer")
        if self.count_mode == "model_tokenizer" and not self.tokenizer:
            raise ContextBudgetError(
                "token_counter_invalid",
                "model_tokenizer requires an explicit tokenizer name",
            )
        if self.count_mode == "provider_exact" and (
            self.endpoint_type != "official" or not self.model_is_known
        ):
            raise ContextBudgetError(
                "token_counter_unsafe_exact",
                "provider_exact requires an official endpoint and an explicitly known model",
            )

    def count(self, text: str) -> int:
        value = self.counter(text)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContextBudgetError("token_counter_invalid", "token counter returned an invalid value")
        return value

    def metadata(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "endpoint_type": self.endpoint_type,
            "model_is_known": self.model_is_known,
            "counter_source": "provider_usage" if self.count_mode == "provider_exact" else "tokenizer",
        }
        if self.tokenizer:
            result["tokenizer"] = self.tokenizer
            result["tokenizer_version"] = self.version
        else:
            result["provider_counter_version"] = self.version
        return result


@dataclass(frozen=True)
class ContextBudget:
    provider: str
    model: str
    model_context_window: int
    output_reserve_tokens: int = 8_000
    protocol_overhead_tokens: int = 1_000
    safety_margin_tokens: int = 1_000
    max_input_tokens: int = 32_000
    story_project_tokens: int = 16_000
    previous_chapter_tokens: int = 6_000
    safety_ratio: float = 0.15
    endpoint_type: str = "unknown"

    def __post_init__(self) -> None:
        integer_fields = (
            "model_context_window",
            "output_reserve_tokens",
            "protocol_overhead_tokens",
            "safety_margin_tokens",
            "max_input_tokens",
            "story_project_tokens",
            "previous_chapter_tokens",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContextBudgetError("context_budget_invalid", f"{name} must be a non-negative integer")
        if self.model_context_window < 1 or self.max_input_tokens < 1:
            raise ContextBudgetError("context_budget_invalid", "context window and max input must be positive")
        if not 0 <= self.safety_ratio < 1:
            raise ContextBudgetError("context_budget_invalid", "safety_ratio must be in [0, 1)")
        if not self.provider.strip() or not self.model.strip():
            raise ContextBudgetError("context_budget_invalid", "provider and model must be explicit")
        _require_safe_metadata(self.provider, "provider")
        _require_safe_metadata(self.model, "model")
        _require_endpoint_type(self.endpoint_type)
        if self.usable_input_tokens <= 0:
            raise ContextBudgetError("context_budget_invalid", "reserves leave no usable input tokens")

    @property
    def usable_input_tokens(self) -> int:
        return max(
            0,
            self.model_context_window
            - self.output_reserve_tokens
            - self.protocol_overhead_tokens
            - self.safety_margin_tokens,
        )

    @property
    def hard_input_limit(self) -> int:
        return min(self.max_input_tokens, self.usable_input_tokens)

    def measure(
        self,
        text: str,
        *,
        stage: str,
        exact_counter: ExactTokenCounter | None = None,
        token_counter: TokenCounter | None = None,
        calibrated_estimator: CalibratedTokenEstimator | None = None,
        protocol_texts: Iterable[str] = (),
    ) -> dict[str, Any]:
        combined = "\n".join([*(str(item) for item in protocol_texts), str(text)])
        if token_counter is not None and exact_counter is not None:
            raise ContextBudgetError(
                "token_counter_invalid",
                "token_counter and legacy exact_counter are mutually exclusive",
            )
        if calibrated_estimator is not None and (token_counter is not None or exact_counter is not None):
            raise ContextBudgetError(
                "token_counter_invalid",
                "calibrated_estimator cannot be combined with a token counter",
            )

        effective_counter = token_counter
        if effective_counter is None and exact_counter is not None:
            effective_counter = _legacy_model_tokenizer(
                exact_counter,
                provider=self.provider,
                model=self.model,
                endpoint_type=self.endpoint_type,
            )

        if effective_counter is not None:
            self._validate_counter_binding(effective_counter)
            raw_tokens = effective_counter.count(combined)
            mode = effective_counter.count_mode
            counter_version = effective_counter.version
            budgeted_tokens = raw_tokens
            count_metadata = effective_counter.metadata()
        else:
            estimator = calibrated_estimator or DEFAULT_CALIBRATED_ESTIMATOR
            raw_tokens = estimator.estimate(combined)
            mode = "calibrated_estimate"
            counter_version = estimator.version
            budgeted_tokens = math.ceil(raw_tokens * (1 + self.safety_ratio))
            count_metadata = {
                "provider": self.provider,
                "model": self.model,
                "endpoint_type": self.endpoint_type,
                "model_is_known": False,
                "counter_source": "calibration",
                "calibration_version": estimator.version,
            }
        report = {
            "schema_version": CONTEXT_BUDGET_SCHEMA_VERSION,
            "stage": stage,
            "provider": self.provider,
            "model": self.model,
            "model_context_window": self.model_context_window,
            "usable_input_tokens": self.usable_input_tokens,
            "hard_input_limit": self.hard_input_limit,
            "raw_input_tokens": raw_tokens,
            "budgeted_input_tokens": budgeted_tokens,
            "count_mode": mode,
            "counter_version": str(counter_version),
            "count_metadata": count_metadata,
            "within_budget": budgeted_tokens <= self.hard_input_limit,
            "context_digest": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
        }
        return validate_schema(report, "context_budget_report.schema.json")

    def _validate_counter_binding(self, counter: TokenCounter) -> None:
        if counter.provider != self.provider or counter.model != self.model:
            raise ContextBudgetError(
                "token_counter_binding_mismatch",
                "token counter provider/model does not match the context budget",
            )
        if counter.endpoint_type != self.endpoint_type:
            raise ContextBudgetError(
                "token_counter_binding_mismatch",
                "token counter endpoint_type does not match the context budget",
            )

    def require_input(self, text: str, *, stage: str, **kwargs: Any) -> dict[str, Any]:
        report = self.measure(text, stage=stage, **kwargs)
        if not report["within_budget"]:
            raise ContextBudgetError(
                "story_project_context_budget_exceeded",
                f"{stage} input requires {report['budgeted_input_tokens']} tokens; "
                f"hard limit is {report['hard_input_limit']}",
            )
        return report


@dataclass(frozen=True)
class RunBudgetLimits:
    max_provider_calls: int = 20
    max_total_input_tokens: int = 160_000
    max_total_output_tokens: int = 40_000
    max_elapsed_seconds: float = 900.0
    max_estimated_cost: float | None = None


class RunBudgetTracker:
    def __init__(
        self,
        limits: RunBudgetLimits,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self._now = now
        self._started_at = now()
        self.provider_calls = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.reserved_output_tokens = 0
        self.estimated_cost = 0.0
        self._model_reservations: dict[str, dict[str, Any]] = {}

    def reserve_call(self, input_tokens: int, *, estimated_cost: float = 0.0) -> None:
        self._check_elapsed()
        self._require_non_negative(input_tokens, "input_tokens")
        if self.provider_calls + 1 > self.limits.max_provider_calls:
            raise ContextBudgetError("run_provider_call_budget_exceeded", "max_provider_calls exceeded")
        if self.total_input_tokens + input_tokens > self.limits.max_total_input_tokens:
            raise ContextBudgetError("run_input_token_budget_exceeded", "max_total_input_tokens exceeded")
        if (
            self.limits.max_estimated_cost is not None
            and self.estimated_cost + estimated_cost > self.limits.max_estimated_cost
        ):
            raise ContextBudgetError("run_estimated_cost_budget_exceeded", "max_estimated_cost exceeded")
        self.provider_calls += 1
        self.total_input_tokens += input_tokens
        self.estimated_cost += estimated_cost

    def record_output(self, output_tokens: int, *, estimated_cost: float = 0.0) -> None:
        self._check_elapsed()
        self._require_non_negative(output_tokens, "output_tokens")
        if self.total_output_tokens + output_tokens > self.limits.max_total_output_tokens:
            raise ContextBudgetError("run_output_token_budget_exceeded", "max_total_output_tokens exceeded")
        if (
            self.limits.max_estimated_cost is not None
            and self.estimated_cost + estimated_cost > self.limits.max_estimated_cost
        ):
            raise ContextBudgetError("run_estimated_cost_budget_exceeded", "max_estimated_cost exceeded")
        self.total_output_tokens += output_tokens
        self.estimated_cost += estimated_cost

    def reserve_model_call(
        self,
        *,
        input_tokens: int,
        max_output_tokens: int,
        call_id: str,
        attempt_id: str,
        estimated_cost: float = 0.0,
    ) -> None:
        """Reserve one physical attempt, including its timeout upper bound."""

        self._check_elapsed()
        self._require_non_negative(input_tokens, "input_tokens")
        self._require_non_negative(max_output_tokens, "max_output_tokens")
        if not isinstance(call_id, str) or not call_id:
            raise ContextBudgetError("run_budget_usage_invalid", "call_id must be non-empty")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ContextBudgetError("run_budget_usage_invalid", "attempt_id must be non-empty")
        if attempt_id in self._model_reservations:
            raise ContextBudgetError(
                "run_budget_attempt_conflict",
                f"attempt_id {attempt_id} was already reserved",
            )
        if self.provider_calls + 1 > self.limits.max_provider_calls:
            raise ContextBudgetError("run_provider_call_budget_exceeded", "max_provider_calls exceeded")
        if self.total_input_tokens + input_tokens > self.limits.max_total_input_tokens:
            raise ContextBudgetError("run_input_token_budget_exceeded", "max_total_input_tokens exceeded")
        if (
            self.total_output_tokens
            + self.reserved_output_tokens
            + max_output_tokens
            > self.limits.max_total_output_tokens
        ):
            raise ContextBudgetError(
                "run_output_token_budget_exceeded",
                "reserved output exceeds remaining max_total_output_tokens",
            )
        if (
            self.limits.max_estimated_cost is not None
            and self.estimated_cost + estimated_cost > self.limits.max_estimated_cost
        ):
            raise ContextBudgetError("run_estimated_cost_budget_exceeded", "max_estimated_cost exceeded")
        self.provider_calls += 1
        self.total_input_tokens += input_tokens
        self.reserved_output_tokens += max_output_tokens
        self.estimated_cost += estimated_cost
        self._model_reservations[attempt_id] = {
            "call_id": call_id,
            "max_output_tokens": max_output_tokens,
            "status": "reserved",
            "actual_output_tokens": None,
        }

    def record_model_response(
        self,
        *,
        response: Any,
        call_id: str,
        attempt_id: str,
    ) -> None:
        """Settle a reservation from provider usage or a conservative fallback."""

        self._check_elapsed()
        reservation = self._model_reservations.get(attempt_id)
        if reservation is None or reservation.get("call_id") != call_id:
            raise ContextBudgetError(
                "run_budget_attempt_missing",
                f"attempt_id {attempt_id} has no matching reservation",
            )
        if reservation["status"] == "settled":
            return
        actual_output = _model_response_output_tokens(response)
        if actual_output is None:
            actual_output = conservative_token_estimate(str(getattr(response, "text", response)))
        self._require_non_negative(actual_output, "actual_output_tokens")
        reserved = int(reservation["max_output_tokens"])
        prospective = self.total_output_tokens + self.reserved_output_tokens - reserved + actual_output
        if prospective > self.limits.max_total_output_tokens:
            raise ContextBudgetError(
                "run_output_token_budget_exceeded",
                "provider output exceeds remaining max_total_output_tokens",
            )
        self.reserved_output_tokens -= reserved
        self.total_output_tokens += actual_output
        reservation["status"] = "settled"
        reservation["actual_output_tokens"] = actual_output

    def remaining_seconds(self) -> float:
        return max(0.0, self.limits.max_elapsed_seconds - (self._now() - self._started_at))

    def report(self) -> dict[str, Any]:
        return {
            "provider_calls": self.provider_calls,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "charged_output_tokens": self.total_output_tokens + self.reserved_output_tokens,
            "unsettled_attempt_count": sum(
                item["status"] == "reserved" for item in self._model_reservations.values()
            ),
            "elapsed_seconds": max(0.0, self._now() - self._started_at),
            "estimated_cost": self.estimated_cost,
        }

    def _check_elapsed(self) -> None:
        if self._now() - self._started_at > self.limits.max_elapsed_seconds:
            raise ContextBudgetError("run_elapsed_budget_exceeded", "max_elapsed_seconds exceeded")

    @staticmethod
    def _require_non_negative(value: int, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContextBudgetError("run_budget_usage_invalid", f"{name} must be a non-negative integer")


def _model_response_output_tokens(response: Any) -> int | None:
    usage = getattr(response, "usage", None)
    if not isinstance(usage, Mapping):
        return None
    for key in ("output_tokens", "completion_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    nested = usage.get("output_tokens_details")
    if isinstance(nested, Mapping):
        value = nested.get("total")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def default_context_budget(*, provider: str = "openai", model: str = "runtime-default") -> ContextBudget:
    window = _positive_env("NOVELAGENT_MODEL_CONTEXT_WINDOW", 128_000)
    max_input_tokens = _positive_env("NOVELAGENT_MAX_INPUT_TOKENS", 32_000)
    return ContextBudget(
        provider=provider,
        model=model,
        model_context_window=window,
        max_input_tokens=max_input_tokens,
    )


def conservative_token_estimate(text: str) -> int:
    if not text:
        return 0
    return len(text.encode("utf-8"))


def preview_chinese_output_compatibility(
    max_output_tokens: int,
    *,
    minimum_chinese_chars: int = 3_000,
    maximum_chinese_chars: int = 4_500,
    calibrated_estimator: CalibratedTokenEstimator | None = None,
    safety_ratio: float = 0.15,
) -> dict[str, Any]:
    """Pure preview of whether an output cap can cover a Chinese target range."""

    for name, value in (
        ("max_output_tokens", max_output_tokens),
        ("minimum_chinese_chars", minimum_chinese_chars),
        ("maximum_chinese_chars", maximum_chinese_chars),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ContextBudgetError("output_compatibility_invalid", f"{name} must be a positive integer")
    if maximum_chinese_chars < minimum_chinese_chars:
        raise ContextBudgetError(
            "output_compatibility_invalid",
            "maximum_chinese_chars must be at least minimum_chinese_chars",
        )
    if isinstance(safety_ratio, bool) or not isinstance(safety_ratio, (int, float)) or not 0 <= safety_ratio < 1:
        raise ContextBudgetError("output_compatibility_invalid", "safety_ratio must be in [0, 1)")

    estimator = calibrated_estimator or DEFAULT_CALIBRATED_ESTIMATOR
    minimum_raw_tokens = estimator.estimate("字" * minimum_chinese_chars)
    maximum_raw_tokens = estimator.estimate("字" * maximum_chinese_chars)
    minimum_required_tokens = math.ceil(minimum_raw_tokens * (1 + safety_ratio))
    maximum_required_tokens = math.ceil(maximum_raw_tokens * (1 + safety_ratio))
    minimum_compatible = max_output_tokens >= minimum_required_tokens
    full_range_compatible = max_output_tokens >= maximum_required_tokens
    return {
        "minimum_chinese_chars": minimum_chinese_chars,
        "maximum_chinese_chars": maximum_chinese_chars,
        "max_output_tokens": max_output_tokens,
        "minimum_required_tokens": minimum_required_tokens,
        "maximum_required_tokens": maximum_required_tokens,
        "minimum_target_compatible": minimum_compatible,
        "full_target_range_compatible": full_range_compatible,
        "compatible": full_range_compatible,
        "shortfall_tokens": max(0, maximum_required_tokens - max_output_tokens),
        "count_mode": "calibrated_estimate",
        "calibration_version": estimator.version,
    }


def _legacy_model_tokenizer(
    counter: ExactTokenCounter,
    *,
    provider: str,
    model: str,
    endpoint_type: str,
) -> TokenCounter:
    version = str(getattr(counter, "version", None) or "legacy-tokenizer-v1")
    tokenizer = str(getattr(counter, "tokenizer", None) or getattr(counter, "__name__", None) or "legacy-callable")
    return TokenCounter(
        counter=counter,
        count_mode="model_tokenizer",
        provider=provider,
        model=model,
        endpoint_type=endpoint_type,
        version=version,
        tokenizer=tokenizer,
        model_is_known=bool(getattr(counter, "model_is_known", False)),
    )


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


__all__ = [
    "CalibratedTokenEstimator",
    "CJK_CHARACTER_OUTPUT_ESTIMATOR",
    "CONTEXT_BUDGET_SCHEMA_VERSION",
    "ContextBudget",
    "ContextBudgetError",
    "DEFAULT_CALIBRATED_ESTIMATOR",
    "ESTIMATOR_VERSION",
    "LEGACY_TOKEN_COUNT_MODES",
    "NEW_TOKEN_COUNT_MODES",
    "RunBudgetLimits",
    "RunBudgetTracker",
    "SAFE_ENDPOINT_TYPES",
    "TokenCounter",
    "conservative_token_estimate",
    "default_context_budget",
    "preview_chinese_output_compatibility",
]
