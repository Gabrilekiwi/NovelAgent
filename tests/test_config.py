from __future__ import annotations

import os
import unittest
from pathlib import Path

from core.config import get_config
from core.runtime_paths import DEFAULT_MEMORY_PATH


class ConfigTest(unittest.TestCase):
    def test_empty_env_values_are_treated_as_missing(self) -> None:
        original_openai = os.environ.get("OPENAI_API_KEY")
        original_model = os.environ.get("OPENAI_MODEL")
        os.environ["OPENAI_API_KEY"] = "   "
        os.environ["OPENAI_MODEL"] = ""
        try:
            config = get_config()
        finally:
            if original_openai is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = original_openai
            if original_model is None:
                os.environ.pop("OPENAI_MODEL", None)
            else:
                os.environ["OPENAI_MODEL"] = original_model

        self.assertIsNone(config.openai_api_key)
        self.assertEqual("gpt-4.1-mini", config.openai_model)

    def test_default_memory_path_uses_local_runtime_dir(self) -> None:
        original_memory_path = os.environ.get("NOVELAGENT_MEMORY_PATH")
        os.environ["NOVELAGENT_MEMORY_PATH"] = ""
        try:
            config = get_config()
        finally:
            if original_memory_path is None:
                os.environ.pop("NOVELAGENT_MEMORY_PATH", None)
            else:
                os.environ["NOVELAGENT_MEMORY_PATH"] = original_memory_path

        self.assertEqual(Path(DEFAULT_MEMORY_PATH), config.memory_path)

    def test_openai_timeout_defaults_and_parses_env(self) -> None:
        original_timeout = os.environ.get("OPENAI_TIMEOUT_SECONDS")
        os.environ["OPENAI_TIMEOUT_SECONDS"] = "7"
        try:
            config = get_config()
        finally:
            if original_timeout is None:
                os.environ.pop("OPENAI_TIMEOUT_SECONDS", None)
            else:
                os.environ["OPENAI_TIMEOUT_SECONDS"] = original_timeout

        self.assertEqual(7, config.openai_timeout_seconds)

    def test_openai_max_output_tokens_defaults_and_parses_env(self) -> None:
        original_max_tokens = os.environ.get("OPENAI_MAX_OUTPUT_TOKENS")
        os.environ["OPENAI_MAX_OUTPUT_TOKENS"] = "321"
        try:
            config = get_config()
        finally:
            if original_max_tokens is None:
                os.environ.pop("OPENAI_MAX_OUTPUT_TOKENS", None)
            else:
                os.environ["OPENAI_MAX_OUTPUT_TOKENS"] = original_max_tokens

        self.assertEqual(321, config.openai_max_output_tokens)

    def test_openai_max_output_default_covers_full_chapter_target(self) -> None:
        original_max_tokens = os.environ.get("OPENAI_MAX_OUTPUT_TOKENS")
        os.environ["OPENAI_MAX_OUTPUT_TOKENS"] = ""
        try:
            config = get_config()
        finally:
            if original_max_tokens is None:
                os.environ.pop("OPENAI_MAX_OUTPUT_TOKENS", None)
            else:
                os.environ["OPENAI_MAX_OUTPUT_TOKENS"] = original_max_tokens

        self.assertEqual(16000, config.openai_max_output_tokens)

    def test_openai_max_retries_defaults_to_zero_and_parses_env(self) -> None:
        original_retries = os.environ.get("OPENAI_MAX_RETRIES")
        os.environ["OPENAI_MAX_RETRIES"] = "4"
        try:
            config = get_config()
        finally:
            if original_retries is None:
                os.environ.pop("OPENAI_MAX_RETRIES", None)
            else:
                os.environ["OPENAI_MAX_RETRIES"] = original_retries

        self.assertEqual(4, config.openai_max_retries)

    def test_openai_stream_defaults_and_parses_env(self) -> None:
        original_stream = os.environ.get("OPENAI_STREAM")
        os.environ["OPENAI_STREAM"] = "false"
        try:
            config = get_config()
        finally:
            if original_stream is None:
                os.environ.pop("OPENAI_STREAM", None)
            else:
                os.environ["OPENAI_STREAM"] = original_stream

        self.assertFalse(config.openai_stream)

    def test_provider_timeouts_parse_env(self) -> None:
        originals = {
            "CLAUDE_TIMEOUT_SECONDS": os.environ.get("CLAUDE_TIMEOUT_SECONDS"),
            "CLAUDE_STREAM": os.environ.get("CLAUDE_STREAM"),
            "NOTION_TIMEOUT_SECONDS": os.environ.get("NOTION_TIMEOUT_SECONDS"),
        }
        os.environ["CLAUDE_TIMEOUT_SECONDS"] = "11"
        os.environ["CLAUDE_STREAM"] = "false"
        os.environ["NOTION_TIMEOUT_SECONDS"] = "13"
        try:
            config = get_config()
        finally:
            for name, value in originals.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.assertEqual(11, config.claude_timeout_seconds)
        self.assertFalse(config.claude_stream)
        self.assertEqual(13, config.notion_timeout_seconds)

    def test_unified_provider_retry_config_parses_and_clamps(self) -> None:
        names = (
            "PROVIDER_MAX_ATTEMPTS",
            "PROVIDER_RETRY_BASE_DELAY_SECONDS",
            "PROVIDER_RETRY_MAX_DELAY_SECONDS",
            "PROVIDER_RETRY_JITTER_RATIO",
            "PROVIDER_RETRY_DEADLINE_SECONDS",
        )
        originals = {name: os.environ.get(name) for name in names}
        os.environ["PROVIDER_MAX_ATTEMPTS"] = "4"
        os.environ["PROVIDER_RETRY_BASE_DELAY_SECONDS"] = "0.5"
        os.environ["PROVIDER_RETRY_MAX_DELAY_SECONDS"] = "5"
        os.environ["PROVIDER_RETRY_JITTER_RATIO"] = "2"
        os.environ["PROVIDER_RETRY_DEADLINE_SECONDS"] = "30"
        try:
            config = get_config()
        finally:
            for name, value in originals.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.assertEqual(4, config.provider_max_attempts)
        self.assertEqual(0.5, config.provider_retry_base_delay_seconds)
        self.assertEqual(5.0, config.provider_retry_max_delay_seconds)
        self.assertEqual(1.0, config.provider_retry_jitter_ratio)
        self.assertEqual(30.0, config.provider_retry_deadline_seconds)

    def test_claude_max_tokens_default_supports_long_polish(self) -> None:
        original_max_tokens = os.environ.get("CLAUDE_MAX_TOKENS")
        os.environ["CLAUDE_MAX_TOKENS"] = ""
        try:
            config = get_config()
        finally:
            if original_max_tokens is None:
                os.environ.pop("CLAUDE_MAX_TOKENS", None)
            else:
                os.environ["CLAUDE_MAX_TOKENS"] = original_max_tokens

        self.assertEqual(16000, config.claude_max_tokens)

    def test_claude_base_url_and_user_agent_parse_env(self) -> None:
        originals = {
            "CLAUDE_BASE_URL": os.environ.get("CLAUDE_BASE_URL"),
            "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL"),
            "CLAUDE_USER_AGENT": os.environ.get("CLAUDE_USER_AGENT"),
        }
        os.environ["CLAUDE_BASE_URL"] = ""
        os.environ["ANTHROPIC_BASE_URL"] = "https://claude.example.test"
        os.environ["CLAUDE_USER_AGENT"] = "claude-cli/1.0 test"
        try:
            config = get_config()
        finally:
            for name, value in originals.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.assertEqual("https://claude.example.test", config.claude_base_url)
        self.assertEqual("claude-cli/1.0 test", config.claude_user_agent)

    def test_claude_auth_token_and_model_aliases_parse_env(self) -> None:
        originals = {
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
            "ANTHROPIC_AUTH_TOKEN": os.environ.get("ANTHROPIC_AUTH_TOKEN"),
            "CLAUDE_MODEL": os.environ.get("CLAUDE_MODEL"),
            "ANTHROPIC_MODEL": os.environ.get("ANTHROPIC_MODEL"),
        }
        os.environ["ANTHROPIC_API_KEY"] = ""
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "token-from-docs"
        os.environ["CLAUDE_MODEL"] = ""
        os.environ["ANTHROPIC_MODEL"] = "claude-doc-model"
        try:
            config = get_config()
        finally:
            for name, value in originals.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.assertEqual("token-from-docs", config.anthropic_api_key)
        self.assertEqual("claude-doc-model", config.claude_model)

    def test_notion_api_config_detects_complete_pair(self) -> None:
        originals = {
            "NOTION_API_KEY": os.environ.get("NOTION_API_KEY"),
            "NOTION_DATABASE_ID": os.environ.get("NOTION_DATABASE_ID"),
        }
        os.environ["NOTION_API_KEY"] = "secret"
        os.environ["NOTION_DATABASE_ID"] = "database"
        try:
            config = get_config()
        finally:
            for name, value in originals.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.assertTrue(config.has_notion_api)


if __name__ == "__main__":
    unittest.main()
