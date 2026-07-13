from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from core.runtime_paths import DEFAULT_MEMORY_PATH


_ENV_LOADED = False
PROXY_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def load_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    if _skip_dotenv():
        _ENV_LOADED = True
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
    openai_timeout_seconds: int
    openai_max_output_tokens: int
    openai_max_retries: int
    openai_stream: bool
    anthropic_api_key: str | None
    claude_base_url: str | None
    claude_user_agent: str | None
    claude_model: str | None
    claude_max_tokens: int
    claude_timeout_seconds: int
    claude_stream: bool
    memory_path: Path
    notion_api_key: str | None
    notion_database_id: str | None
    notion_timeout_seconds: int
    provider_max_attempts: int = 3
    provider_retry_base_delay_seconds: float = 1.0
    provider_retry_max_delay_seconds: float = 8.0
    provider_retry_jitter_ratio: float = 0.2
    provider_retry_deadline_seconds: float = 180.0

    @property
    def has_notion_api(self) -> bool:
        return bool(self.notion_api_key and self.notion_database_id)


def get_config() -> RuntimeConfig:
    load_env()
    return RuntimeConfig(
        openai_api_key=_env("OPENAI_API_KEY"),
        openai_base_url=_env("OPENAI_BASE_URL"),
        openai_model=_env("OPENAI_MODEL") or "gpt-4.1-mini",
        openai_timeout_seconds=_int_env("OPENAI_TIMEOUT_SECONDS", 90),
        openai_max_output_tokens=_int_env("OPENAI_MAX_OUTPUT_TOKENS", 1200),
        openai_max_retries=max(0, _int_env("OPENAI_MAX_RETRIES", 0)),
        openai_stream=_bool_env("OPENAI_STREAM", True),
        anthropic_api_key=_env("ANTHROPIC_API_KEY") or _env("ANTHROPIC_AUTH_TOKEN"),
        claude_base_url=_env("CLAUDE_BASE_URL") or _env("ANTHROPIC_BASE_URL"),
        claude_user_agent=_env("CLAUDE_USER_AGENT"),
        claude_model=_env("CLAUDE_MODEL") or _env("ANTHROPIC_MODEL"),
        claude_max_tokens=_int_env("CLAUDE_MAX_TOKENS", 8000),
        claude_timeout_seconds=_int_env("CLAUDE_TIMEOUT_SECONDS", 90),
        claude_stream=_bool_env("CLAUDE_STREAM", True),
        memory_path=Path(_env("NOVELAGENT_MEMORY_PATH") or DEFAULT_MEMORY_PATH),
        notion_api_key=_env("NOTION_API_KEY"),
        notion_database_id=_env("NOTION_DATABASE_ID") or _env("NOVELAGENT_NOTION_DATABASE_ID"),
        notion_timeout_seconds=_int_env("NOTION_TIMEOUT_SECONDS", 30),
        provider_max_attempts=min(10, max(1, _int_env("PROVIDER_MAX_ATTEMPTS", 3))),
        provider_retry_base_delay_seconds=max(0.0, _float_env("PROVIDER_RETRY_BASE_DELAY_SECONDS", 1.0)),
        provider_retry_max_delay_seconds=max(0.0, _float_env("PROVIDER_RETRY_MAX_DELAY_SECONDS", 8.0)),
        provider_retry_jitter_ratio=min(1.0, max(0.0, _float_env("PROVIDER_RETRY_JITTER_RATIO", 0.2))),
        provider_retry_deadline_seconds=max(1.0, _float_env("PROVIDER_RETRY_DEADLINE_SECONDS", 180.0)),
    )


def proxy_disabled_by_env() -> bool:
    load_env()
    return _bool_env("NOVELAGENT_NO_PROXY", False)


def clear_proxy_env() -> None:
    for name in PROXY_ENV_NAMES:
        os.environ.pop(name, None)


def _skip_dotenv() -> bool:
    if os.getenv("NOVELAGENT_SKIP_DOTENV", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if "unittest" in sys.modules:
        return True
    argv = [str(part).replace("\\", "/").lower() for part in sys.argv]
    return any(part.endswith("/unittest/__main__.py") or part.endswith("/unittest") for part in argv[:1])


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


def _float_env(name: str, default: float) -> float:
    value = _env(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}
