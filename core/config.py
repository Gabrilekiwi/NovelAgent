from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


_ENV_LOADED = False


def load_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        _ENV_LOADED = True
        return
    load_dotenv()
    _ENV_LOADED = True


@dataclass(frozen=True)
class RuntimeConfig:
    openai_api_key: str | None
    openai_base_url: str | None
    openai_model: str
    anthropic_api_key: str | None
    claude_model: str | None
    claude_max_tokens: int
    memory_path: Path
    notion_api_key: str | None
    notion_database_id: str | None

    @property
    def has_notion_api(self) -> bool:
        return bool(self.notion_api_key and self.notion_database_id)


def get_config() -> RuntimeConfig:
    load_env()
    return RuntimeConfig(
        openai_api_key=_env("OPENAI_API_KEY"),
        openai_base_url=_env("OPENAI_BASE_URL"),
        openai_model=_env("OPENAI_MODEL") or "gpt-4.1-mini",
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        claude_model=_env("CLAUDE_MODEL"),
        claude_max_tokens=_int_env("CLAUDE_MAX_TOKENS", 3000),
        memory_path=Path(_env("NOVELAGENT_MEMORY_PATH") or "data/memory.json"),
        notion_api_key=_env("NOTION_API_KEY"),
        notion_database_id=_env("NOTION_DATABASE_ID") or _env("NOVELAGENT_NOTION_DATABASE_ID"),
    )


def _env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _int_env(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default
