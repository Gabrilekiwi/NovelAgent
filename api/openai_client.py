from __future__ import annotations

from typing import Any, Callable, Mapping

from api.contracts import (
    MODEL_ENDPOINT_OFFICIAL,
    MODEL_ENDPOINT_OPENAI_COMPATIBLE,
    MODEL_ENDPOINT_UNKNOWN,
    ModelCallError,
    ModelResponse,
)
from api.retry import (
    MODEL_READ_GENERATION,
    PartialResponseError,
    RetryOperationError,
    RetryPolicy,
    retry_policy_for_profile,
)
from core.config import get_config
from core.model_call_runtime import ModelCallRuntimeContext, resolve_model_call_runtime


def chat_completion(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.8,
    stage: str = "chat_completion",
    max_tokens: int | None = None,
    retry_policy: RetryPolicy | None = None,
    retry_budget_remaining: Callable[[], float | None] | None = None,
    model_call_runtime: ModelCallRuntimeContext | None = None,
    call_id: str | None = None,
    input_tokens: int | None = None,
) -> ModelResponse:
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

    runtime = resolve_model_call_runtime(model_call_runtime)
    endpoint_type = (
        MODEL_ENDPOINT_OPENAI_COMPATIBLE
        if config.openai_base_url
        else MODEL_ENDPOINT_OFFICIAL
    )
    if runtime is None:
        raise ModelCallError(
            "A durable ModelCallRuntime is required before any physical OpenAI request.",
            provider="openai",
            stage=stage,
            model=resolved_model,
            failure_category="durable_evidence_required",
            retryable=False,
        )
    resolved_call_id = call_id or (
        runtime.new_call_id(provider="openai", stage=stage)
    )
    if int(resolved_max_tokens or 0) <= 0:
        raise ValueError(
            "durable model calls require a positive OpenAI max output token reservation"
        )
    stream_request_kwargs = dict(request_kwargs)
    if config.openai_stream and endpoint_type == MODEL_ENDPOINT_OFFICIAL:
        stream_request_kwargs["stream_options"] = {"include_usage": True}
    evidence_request = dict(stream_request_kwargs)
    evidence_request["stream"] = bool(config.openai_stream)
    client_holder: dict[str, Any] = {}
    physical_attempt = 0

    def invoke_provider(client: Any) -> ModelResponse:
        if config.openai_stream:
            response = client.chat.completions.create(
                **stream_request_kwargs,
                stream=True,
            )
            return _extract_streamed_message_content(
                response,
                stage=stage,
                model=resolved_model,
                endpoint_type=endpoint_type,
            )
        response = client.chat.completions.create(**request_kwargs)
        return _extract_message_content(
            response,
            stage=stage,
            model=resolved_model,
            endpoint_type=endpoint_type,
        )

    def invoke() -> ModelResponse:
        nonlocal physical_attempt
        client = client_holder.get("client")
        if client is None:
            client = OpenAI(**client_kwargs)
            client_holder["client"] = client
        physical_attempt += 1
        return runtime.execute_attempt(
            call_id=resolved_call_id,
            attempt_number=physical_attempt,
            provider="openai",
            model=resolved_model,
            stage=stage,
            endpoint_type=endpoint_type,
            request=evidence_request,
            max_output_tokens=max(0, int(resolved_max_tokens or 0)),
            operation=lambda: invoke_provider(client),
            input_tokens=input_tokens,
        )

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


def _extract_message_content(
    response: Any,
    *,
    stage: str,
    model: str | None,
    endpoint_type: str = MODEL_ENDPOINT_UNKNOWN,
) -> ModelResponse:
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
    choice = choices[0]
    return ModelResponse(
        content,
        usage=_sdk_mapping(getattr(response, "usage", None)),
        finish_reason=_optional_metadata(getattr(choice, "finish_reason", None)),
        request_id=_response_request_id(response),
        actual_model=_optional_metadata(getattr(response, "model", None)) or model,
        endpoint_type=endpoint_type,
    )


def _extract_streamed_message_content(
    response: Any,
    *,
    stage: str,
    model: str | None,
    endpoint_type: str = MODEL_ENDPOINT_UNKNOWN,
) -> ModelResponse:
    parts: list[str] = []
    usage: dict[str, Any] = {}
    finish_reason: str | None = None
    request_id = _response_request_id(response)
    actual_model: str | None = model
    try:
        for chunk in response:
            chunk_request_id = _response_request_id(chunk)
            if chunk_request_id:
                request_id = chunk_request_id
            chunk_model = _optional_metadata(getattr(chunk, "model", None))
            if chunk_model:
                actual_model = chunk_model
            chunk_usage = _sdk_mapping(getattr(chunk, "usage", None))
            if chunk_usage:
                usage = chunk_usage
            choices = getattr(chunk, "choices", None)
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0]
            reason = _optional_metadata(getattr(choice, "finish_reason", None))
            if reason:
                finish_reason = reason
            delta = getattr(choice, "delta", None)
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
    return ModelResponse(
        text,
        usage=usage,
        finish_reason=finish_reason,
        request_id=request_id,
        actual_model=actual_model,
        endpoint_type=endpoint_type,
    )


def _response_request_id(response: Any) -> str | None:
    for value in (
        getattr(response, "_request_id", None),
        getattr(response, "request_id", None),
        getattr(response, "id", None),
    ):
        normalized = _optional_metadata(value)
        if normalized:
            return normalized
    return None


def _sdk_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): child for key, child in value.items()}
    for method_name in ("model_dump", "to_dict", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            rendered = method()
            if isinstance(rendered, Mapping):
                return {str(key): child for key, child in rendered.items()}
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return {
            str(key): child
            for key, child in attributes.items()
            if not str(key).startswith("_")
        }
    return {}


def _optional_metadata(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
