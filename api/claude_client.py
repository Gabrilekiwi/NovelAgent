from __future__ import annotations

from pathlib import Path
from typing import Any

from api.contracts import ModelCallError
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
            "ANTHROPIC_API_KEY is required for non-dry-run Claude polish.",
            provider="anthropic",
            stage="claude_polish",
            model=model,
        )
    if not model:
        raise ModelCallError(
            "CLAUDE_MODEL is required for non-dry-run Claude polish.",
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

    client = Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=config.claude_max_tokens,
            system=_load_prompt(),
            messages=[
                {
                    "role": "user",
                    "content": chapter_text,
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 - preserve provider failure context.
        raise ModelCallError(
            f"Claude polish failed: {exc}",
            provider="anthropic",
            stage="claude_polish",
            model=model,
            cause=exc,
        ) from exc

    return _extract_message_text(response, model=model)


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
