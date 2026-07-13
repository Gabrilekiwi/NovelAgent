from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from api.contracts import ModelCallError
from api.retry import (
    CLAUDE_POLISH,
    PartialResponseError,
    RetryOperationError,
    RetryPolicy,
    retry_policy_for_profile,
)
from core.config import get_config


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
) -> str:
    if dry_run:
        return chapter_text

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

    client_holder: dict[str, Any] = {}

    def invoke() -> str:
        client = client_holder.get("client")
        if client is None:
            client = Anthropic(**client_kwargs)
            client_holder["client"] = client
        if config.claude_stream:
            return _stream_message_text(client, request_kwargs, model=model)
        response = client.messages.create(**request_kwargs)
        return _extract_message_text(response, model=model)

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


def _stream_message_text(client: Any, request_kwargs: dict[str, Any], *, model: str | None) -> str:
    stream_factory = getattr(client.messages, "stream", None)
    if stream_factory is None:
        response = client.messages.create(**request_kwargs)
        return _extract_message_text(response, model=model)

    parts: list[str] = []
    try:
        with stream_factory(**request_kwargs) as stream:
            text_stream = getattr(stream, "text_stream", None)
            if text_stream is not None:
                for part in text_stream:
                    if part:
                        parts.append(str(part))
                if "".join(parts).strip():
                    return "".join(parts)

            for event in stream:
                text = getattr(event, "text", None)
                if text:
                    parts.append(str(text))
            if parts:
                return "".join(parts)
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


def _extract_message_text(response: Any, *, model: str | None) -> str:
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
    return "\n".join(parts)
