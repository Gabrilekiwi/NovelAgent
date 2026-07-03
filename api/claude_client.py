from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from api.contracts import ModelCallError, classify_model_failure, is_retryable_failure
from core.config import get_config


def _load_prompt() -> str:
    prompt_path = Path("prompts/polish_prompt.md")
    if not prompt_path.exists():
        return (
            "Polish the chapter prose while preserving every plot fact, character action, "
            "location, and timeline detail. Return only the polished chapter."
        )
    return prompt_path.read_text(encoding="utf-8")


def polish_chapter(chapter_text: str, *, dry_run: bool = False) -> str:
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

    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if config.claude_base_url:
        client_kwargs["base_url"] = config.claude_base_url
    if config.claude_timeout_seconds > 0:
        client_kwargs["timeout"] = config.claude_timeout_seconds
    if config.claude_user_agent:
        client_kwargs["default_headers"] = {"User-Agent": config.claude_user_agent}

    request_kwargs = {
        "model": model,
        "max_tokens": config.claude_max_tokens,
        "system": _load_prompt(),
        "messages": [
            {
                "role": "user",
                "content": chapter_text,
            }
        ],
    }

    client = Anthropic(**client_kwargs)
    started = time.monotonic()
    try:
        if config.claude_stream:
            return _stream_message_text(client, request_kwargs, model=model)
        response = client.messages.create(**request_kwargs)
    except Exception as exc:  # noqa: BLE001 - preserve provider failure context.
        failure_category = classify_model_failure(type(exc).__name__, str(exc))
        elapsed_ms = int(max(0.0, (time.monotonic() - started) * 1000))
        raise ModelCallError(
            f"Claude polish failed: {exc}",
            provider="anthropic",
            stage="claude_polish",
            model=model,
            cause=exc,
            failure_category=failure_category,
            retryable=is_retryable_failure(failure_category),
            attempts=1,
            elapsed_ms=elapsed_ms,
        ) from exc

    return _extract_message_text(response, model=model)


def _stream_message_text(client: Any, request_kwargs: dict[str, Any], *, model: str | None) -> str:
    stream_factory = getattr(client.messages, "stream", None)
    if stream_factory is None:
        response = client.messages.create(**request_kwargs)
        return _extract_message_text(response, model=model)

    with stream_factory(**request_kwargs) as stream:
        text_stream = getattr(stream, "text_stream", None)
        if text_stream is not None:
            text = "".join(str(part) for part in text_stream if part)
            if text.strip():
                return text

        parts: list[str] = []
        for event in stream:
            text = getattr(event, "text", None)
            if text:
                parts.append(str(text))
        if parts:
            return "".join(parts)

    raise ModelCallError(
        "Claude streamed response did not include text content.",
        provider="anthropic",
        stage="claude_polish",
        model=model,
    )


def _extract_message_text(response: Any, *, model: str | None) -> str:
    content = getattr(response, "content", None)
    if not isinstance(content, list):
        raise ModelCallError(
            "Claude response did not include content blocks.",
            provider="anthropic",
            stage="claude_polish",
            model=model,
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
        )
    return "\n".join(parts)
