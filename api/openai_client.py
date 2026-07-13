from __future__ import annotations

from typing import Any, Callable

from api.contracts import ModelCallError
from api.retry import (
    MODEL_READ_GENERATION,
    PartialResponseError,
    RetryOperationError,
    RetryPolicy,
    retry_policy_for_profile,
)
from core.config import get_config


def chat_completion(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.8,
    stage: str = "chat_completion",
    max_tokens: int | None = None,
    retry_policy: RetryPolicy | None = None,
    retry_budget_remaining: Callable[[], float | None] | None = None,
) -> str:
    config = get_config()
    api_key = config.openai_api_key
    resolved_model = model or config.openai_model
    if not api_key:
        raise ModelCallError(
            "OPENAI_API_KEY is required for non-dry-run generation.",
            provider="openai",
            stage=stage,
            model=resolved_model,
        )

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ModelCallError(
            "Install the openai package to use non-dry-run OpenAI generation.",
            provider="openai",
            stage=stage,
            model=resolved_model,
            cause=exc,
        ) from exc

    policy = retry_policy or retry_policy_for_profile(
        MODEL_READ_GENERATION,
        config=config,
        legacy_openai_compat=True,
    )
    if policy.profile != MODEL_READ_GENERATION:
        raise ValueError("OpenAI chat completion requires model_read_generation retry profile")

    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if config.openai_base_url:
        client_kwargs["base_url"] = config.openai_base_url
    if config.openai_timeout_seconds > 0:
        client_kwargs["timeout"] = min(
            float(config.openai_timeout_seconds),
            policy.deadline_seconds,
        )
    client_kwargs["max_retries"] = 0

    resolved_max_tokens = max_tokens if max_tokens is not None else config.openai_max_output_tokens
    request_kwargs: dict[str, Any] = {
        "model": resolved_model,
        "messages": messages,
        "temperature": temperature,
    }
    if resolved_max_tokens > 0:
        request_kwargs["max_tokens"] = resolved_max_tokens

    client_holder: dict[str, Any] = {}

    def invoke() -> str:
        client = client_holder.get("client")
        if client is None:
            client = OpenAI(**client_kwargs)
            client_holder["client"] = client
        if config.openai_stream:
            response = client.chat.completions.create(**request_kwargs, stream=True)
            return _extract_streamed_message_content(response, stage=stage, model=resolved_model)
        response = client.chat.completions.create(**request_kwargs)
        return _extract_message_content(response, stage=stage, model=resolved_model)

    try:
        execution = policy.execute(invoke, budget_remaining_seconds=retry_budget_remaining)
    except RetryOperationError as exc:
        report = exc.report
        raise ModelCallError(
            f"OpenAI chat completion failed ({exc.failure_category}; {type(exc.cause).__name__}).",
            provider="openai",
            stage=stage,
            model=resolved_model,
            cause=exc.cause,
            failure_category=exc.failure_category,
            retryable=exc.retryable,
            attempts=int(report["attempts"]),
            elapsed_ms=int(report["elapsed_ms"]),
            attempt_history=list(report["history"]),
            retry_stop_reason=str(report["stop_reason"]),
            partial_content_received=exc.partial_content_received,
        ) from exc
    return execution.value


def _extract_message_content(response: Any, *, stage: str, model: str | None) -> str:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise ModelCallError(
            "OpenAI response did not include choices.",
            provider="openai",
            stage=stage,
            model=model,
            failure_category="output_contract",
            retryable=False,
        )

    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if content is None:
        raise ModelCallError(
            "OpenAI response did not include message content.",
            provider="openai",
            stage=stage,
            model=model,
            failure_category="output_contract",
            retryable=False,
        )
    if not isinstance(content, str):
        raise ModelCallError(
            "OpenAI response message content must be a string.",
            provider="openai",
            stage=stage,
            model=model,
            failure_category="output_contract",
            retryable=False,
        )
    return content


def _extract_streamed_message_content(response: Any, *, stage: str, model: str | None) -> str:
    parts: list[str] = []
    try:
        for chunk in response:
            choices = getattr(chunk, "choices", None)
            if not isinstance(choices, list) or not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            content = getattr(delta, "content", None)
            if isinstance(content, str):
                parts.append(content)
    except Exception as exc:
        raise PartialResponseError(exc, partial_content_received=bool(parts)) from exc
    text = "".join(parts)
    if not text:
        raise ModelCallError(
            "OpenAI streamed response did not include message content.",
            provider="openai",
            stage=stage,
            model=model,
            failure_category="output_contract",
            retryable=False,
        )
    return text
