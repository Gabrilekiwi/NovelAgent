from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from api.contracts import (
    MODEL_ENDPOINT_OFFICIAL,
    MODEL_ENDPOINT_UNKNOWN,
    ModelCallError,
    ModelResponse,
)
from api.retry import (
    CLAUDE_POLISH,
    PartialResponseError,
    RetryOperationError,
    RetryPolicy,
    retry_policy_for_profile,
)
from core.config import get_config
from core.model_call_runtime import ModelCallRuntimeContext, resolve_model_call_runtime


def _load_prompt() -> str:
    prompt_path = Path("prompts/polish_prompt.md")
    if not prompt_path.exists():
        return (
            "Polish the chapter prose while preserving every plot fact, character action, "
            "location, and timeline detail. Return only the polished chapter."
        )
    return prompt_path.read_text(encoding="utf-8")


def polish_chapter(
    chapter_text: str,
    *,
    dry_run: bool = False,
    retry_policy: RetryPolicy | None = None,
    retry_budget_remaining: Callable[[], float | None] | None = None,
    model_call_runtime: ModelCallRuntimeContext | None = None,
    call_id: str | None = None,
    input_tokens: int | None = None,
) -> ModelResponse:
    if dry_run:
        return ModelResponse(
            chapter_text,
            finish_reason="dry_run",
            endpoint_type=MODEL_ENDPOINT_UNKNOWN,
        )

    config = get_config()
    api_key = config.anthropic_api_key
    model = config.claude_model
    if not api_key:
        raise ModelCallError(
            "ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is required for non-dry-run Claude polish.",
            provider="anthropic",
            stage="claude_polish",
            model=model,
        )
    if not model:
        raise ModelCallError(
            "CLAUDE_MODEL or ANTHROPIC_MODEL is required for non-dry-run Claude polish.",
            provider="anthropic",
            stage="claude_polish",
            model=model,
        )

    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise ModelCallError(
            "Install the anthropic package to use non-dry-run Claude polish.",
            provider="anthropic",
            stage="claude_polish",
            model=model,
            cause=exc,
        ) from exc

    policy = retry_policy or retry_policy_for_profile(CLAUDE_POLISH, config=config)
    if policy.profile != CLAUDE_POLISH:
        raise ValueError("Claude polish requires claude_polish retry profile")

    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if config.claude_base_url:
        client_kwargs["base_url"] = config.claude_base_url
    if config.claude_timeout_seconds > 0:
        client_kwargs["timeout"] = min(
            float(config.claude_timeout_seconds),
            policy.deadline_seconds,
        )
    if config.claude_user_agent:
        client_kwargs["default_headers"] = {"User-Agent": config.claude_user_agent}
    client_kwargs["max_retries"] = 0

    request_kwargs = {
        "model": model,
        "max_tokens": _polish_max_tokens(chapter_text, config.claude_max_tokens),
        "system": _load_prompt(),
        "messages": [
            {
                "role": "user",
                "content": chapter_text,
            }
        ],
    }

    runtime = resolve_model_call_runtime(model_call_runtime)
    endpoint_type = (
        MODEL_ENDPOINT_UNKNOWN if config.claude_base_url else MODEL_ENDPOINT_OFFICIAL
    )
    if runtime is None:
        raise ModelCallError(
            "A durable ModelCallRuntime is required before any physical Anthropic request.",
            provider="anthropic",
            stage="claude_polish",
            model=model,
            failure_category="durable_evidence_required",
            retryable=False,
        )
    resolved_call_id = call_id or (
        runtime.new_call_id(provider="anthropic", stage="claude_polish")
    )
    evidence_request = dict(request_kwargs)
    evidence_request["stream"] = bool(config.claude_stream)
    client_holder: dict[str, Any] = {}
    physical_attempt = 0

    def invoke_provider(client: Any) -> ModelResponse:
        if config.claude_stream:
            return _stream_message_text(
                client,
                request_kwargs,
                model=model,
                endpoint_type=endpoint_type,
            )
        response = client.messages.create(**request_kwargs)
        return _extract_message_text(
            response,
            model=model,
            endpoint_type=endpoint_type,
        )

    def invoke() -> ModelResponse:
        nonlocal physical_attempt
        client = client_holder.get("client")
        if client is None:
            client = Anthropic(**client_kwargs)
            client_holder["client"] = client
        physical_attempt += 1
        return runtime.execute_attempt(
            call_id=resolved_call_id,
            attempt_number=physical_attempt,
            provider="anthropic",
            model=model,
            stage="claude_polish",
            endpoint_type=endpoint_type,
            request=evidence_request,
            max_output_tokens=max(0, int(request_kwargs["max_tokens"])),
            operation=lambda: invoke_provider(client),
            input_tokens=input_tokens,
        )

    try:
        execution = policy.execute(invoke, budget_remaining_seconds=retry_budget_remaining)
    except RetryOperationError as exc:
        report = exc.report
        raise ModelCallError(
            f"Claude polish failed ({exc.failure_category}; {type(exc.cause).__name__}).",
            provider="anthropic",
            stage="claude_polish",
            model=model,
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


def _polish_max_tokens(chapter_text: str, configured_max_tokens: int) -> int:
    configured = max(1, int(configured_max_tokens or 0))
    source_chars = len(str(chapter_text or ""))
    if source_chars < 1200:
        return configured
    dynamic_budget = source_chars * 2
    return max(configured, dynamic_budget)


def _stream_message_text(
    client: Any,
    request_kwargs: dict[str, Any],
    *,
    model: str | None,
    endpoint_type: str = MODEL_ENDPOINT_UNKNOWN,
) -> ModelResponse:
    stream_factory = getattr(client.messages, "stream", None)
    if stream_factory is None:
        response = client.messages.create(**request_kwargs)
        return _extract_message_text(
            response,
            model=model,
            endpoint_type=endpoint_type,
        )

    parts: list[str] = []
    usage: dict[str, Any] = {}
    finish_reason: str | None = None
    request_id: str | None = None
    actual_model: str | None = model
    try:
        with stream_factory(**request_kwargs) as stream:
            text_stream = getattr(stream, "text_stream", None)
            if text_stream is not None:
                for part in text_stream:
                    if part:
                        parts.append(str(part))
            if not parts:
                for event in stream:
                    text = getattr(event, "text", None)
                    if text:
                        parts.append(str(text))
                    message = getattr(event, "message", None)
                    if message is not None:
                        request_id = _anthropic_request_id(message) or request_id
                        actual_model = _optional_metadata(
                            getattr(message, "model", None)
                        ) or actual_model
                        message_usage = _sdk_mapping(getattr(message, "usage", None))
                        if message_usage:
                            usage.update(message_usage)
                    delta = getattr(event, "delta", None)
                    if delta is not None:
                        finish_reason = _optional_metadata(
                            getattr(delta, "stop_reason", None)
                        ) or finish_reason
                    event_usage = _sdk_mapping(getattr(event, "usage", None))
                    if event_usage:
                        usage.update(event_usage)

            final_message = None
            final_getter = getattr(stream, "get_final_message", None)
            if callable(final_getter):
                final_message = final_getter()
            if final_message is not None:
                final_usage = _sdk_mapping(getattr(final_message, "usage", None))
                if final_usage:
                    usage = final_usage
                finish_reason = _optional_metadata(
                    getattr(final_message, "stop_reason", None)
                ) or finish_reason
                request_id = _anthropic_request_id(final_message) or request_id
                actual_model = _optional_metadata(
                    getattr(final_message, "model", None)
                ) or actual_model
            if parts:
                return ModelResponse(
                    "".join(parts),
                    usage=usage,
                    finish_reason=finish_reason,
                    request_id=request_id,
                    actual_model=actual_model,
                    endpoint_type=endpoint_type,
                )
    except Exception as exc:
        raise PartialResponseError(exc, partial_content_received=bool(parts)) from exc

    raise ModelCallError(
        "Claude streamed response did not include text content.",
        provider="anthropic",
        stage="claude_polish",
        model=model,
        failure_category="output_contract",
        retryable=False,
    )


def _extract_message_text(
    response: Any,
    *,
    model: str | None,
    endpoint_type: str = MODEL_ENDPOINT_UNKNOWN,
) -> ModelResponse:
    content = getattr(response, "content", None)
    if not isinstance(content, list):
        raise ModelCallError(
            "Claude response did not include content blocks.",
            provider="anthropic",
            stage="claude_polish",
            model=model,
            failure_category="output_contract",
            retryable=False,
        )

    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))

    if not parts:
        raise ModelCallError(
            "Claude response did not include text content.",
            provider="anthropic",
            stage="claude_polish",
            model=model,
            failure_category="output_contract",
            retryable=False,
        )
    return ModelResponse(
        "\n".join(parts),
        usage=_sdk_mapping(getattr(response, "usage", None)),
        finish_reason=_optional_metadata(getattr(response, "stop_reason", None)),
        request_id=_anthropic_request_id(response),
        actual_model=_optional_metadata(getattr(response, "model", None)) or model,
        endpoint_type=endpoint_type,
    )


def _anthropic_request_id(response: Any) -> str | None:
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
