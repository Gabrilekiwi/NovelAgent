from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
import time
from typing import Any, Callable, Iterable

from core.schema import validate_schema


CONTEXT_BUDGET_SCHEMA_VERSION = "1.0"
ESTIMATOR_VERSION = "utf8-upper-bound-v1"
ExactTokenCounter = Callable[[str], int]


class ContextBudgetError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


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
        protocol_texts: Iterable[str] = (),
    ) -> dict[str, Any]:
        combined = "\n".join([*(str(item) for item in protocol_texts), str(text)])
        if exact_counter is not None:
            raw_tokens = exact_counter(combined)
            if isinstance(raw_tokens, bool) or not isinstance(raw_tokens, int) or raw_tokens < 0:
                raise ContextBudgetError("token_counter_invalid", "exact token counter returned an invalid value")
            mode = "exact"
            counter_version = getattr(exact_counter, "version", None) or "provider-tokenizer"
            budgeted_tokens = raw_tokens
        else:
            raw_tokens = conservative_token_estimate(combined)
            mode = "estimate"
            counter_version = ESTIMATOR_VERSION
            budgeted_tokens = math.ceil(raw_tokens * (1 + self.safety_ratio))
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
            "within_budget": budgeted_tokens <= self.hard_input_limit,
            "context_digest": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
        }
        return validate_schema(report, "context_budget_report.schema.json")

    def require_input(self, text: str, *, stage: str, **kwargs: Any) -> dict[str, Any]:
        report = self.measure(text, stage=stage, **kwargs)
        if not report["within_budget"]:
            raise ContextBudgetError(
                "story_project_context_budget_exceeded",
                f"{stage} input requires {report['budgeted_input_tokens']} tokens; hard limit is {report['hard_input_limit']}",
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
        self.estimated_cost = 0.0

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

    def report(self) -> dict[str, Any]:
        return {
            "provider_calls": self.provider_calls,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
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


def default_context_budget(*, provider: str = "openai", model: str = "runtime-default") -> ContextBudget:
    window = _positive_env("NOVELAGENT_MODEL_CONTEXT_WINDOW", 128_000)
    return ContextBudget(provider=provider, model=model, model_context_window=window)


def conservative_token_estimate(text: str) -> int:
    if not text:
        return 0
    return len(text.encode("utf-8"))


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


__all__ = [
    "CONTEXT_BUDGET_SCHEMA_VERSION",
    "ContextBudget",
    "ContextBudgetError",
    "ESTIMATOR_VERSION",
    "RunBudgetLimits",
    "RunBudgetTracker",
    "conservative_token_estimate",
    "default_context_budget",
]
