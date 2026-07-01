from __future__ import annotations

from typing import Any

from api.contracts import ModelCallError
from core.config import get_config


def chat_completion(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.8,
    stage: str = "chat_completion",
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

    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if config.openai_base_url:
        client_kwargs["base_url"] = config.openai_base_url

    try:
        client = OpenAI(**client_kwargs)
        response = client.chat.completions.create(
            model=resolved_model,
            messages=messages,
            temperature=temperature,
        )
    except Exception as exc:  # noqa: BLE001 - preserve provider failure context.
        raise ModelCallError(
            f"OpenAI chat completion failed: {exc}",
            provider="openai",
            stage=stage,
            model=resolved_model,
            cause=exc,
        ) from exc

    return _extract_message_content(response, stage=stage, model=resolved_model)


def _extract_message_content(response: Any, *, stage: str, model: str | None) -> str:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise ModelCallError(
            "OpenAI response did not include choices.",
            provider="openai",
            stage=stage,
            model=model,
        )

    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if content is None:
        raise ModelCallError(
            "OpenAI response did not include message content.",
            provider="openai",
            stage=stage,
            model=model,
        )
    if not isinstance(content, str):
        raise ModelCallError(
            "OpenAI response message content must be a string.",
            provider="openai",
            stage=stage,
            model=model,
        )
    return content
