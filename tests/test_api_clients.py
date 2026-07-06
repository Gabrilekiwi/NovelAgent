from __future__ import annotations

import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from api.contracts import ModelCallError
from api.claude_client import _extract_message_text, _polish_max_tokens, polish_chapter
from api.openai_client import _extract_message_content, chat_completion


class ApiClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self._claude_alias_env = {
            "ANTHROPIC_AUTH_TOKEN": os.environ.get("ANTHROPIC_AUTH_TOKEN"),
            "ANTHROPIC_MODEL": os.environ.get("ANTHROPIC_MODEL"),
        }
        os.environ["ANTHROPIC_AUTH_TOKEN"] = ""
        os.environ["ANTHROPIC_MODEL"] = ""

    def tearDown(self) -> None:
        for name, value in self._claude_alias_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_openai_client_requires_api_key(self) -> None:
        original_key = os.environ.get("OPENAI_API_KEY")
        original_model = os.environ.get("OPENAI_MODEL")
        os.environ["OPENAI_API_KEY"] = ""
        os.environ["OPENAI_MODEL"] = ""
        try:
            with self.assertRaises(ModelCallError) as context:
                chat_completion([{"role": "user", "content": "hello"}])
        finally:
            if original_key is not None:
                os.environ["OPENAI_API_KEY"] = original_key
            else:
                os.environ.pop("OPENAI_API_KEY", None)
            if original_model is not None:
                os.environ["OPENAI_MODEL"] = original_model
            else:
                os.environ.pop("OPENAI_MODEL", None)

        self.assertEqual("openai", context.exception.provider)
        self.assertEqual("chat_completion", context.exception.stage)
        self.assertEqual("gpt-4.1-mini", context.exception.model)

    def test_openai_client_preserves_stage_context(self) -> None:
        original_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = ""
        try:
            with self.assertRaises(ModelCallError) as context:
                chat_completion([{"role": "user", "content": "hello"}], stage="chapter_generation")
        finally:
            if original_key is not None:
                os.environ["OPENAI_API_KEY"] = original_key
            else:
                os.environ.pop("OPENAI_API_KEY", None)

        self.assertEqual("chapter_generation", context.exception.stage)

    def test_openai_response_extraction_wraps_missing_choices(self) -> None:
        with self.assertRaises(ModelCallError) as context:
            _extract_message_content(SimpleNamespace(choices=[]), stage="chapter_generation", model="gpt-test")

        self.assertEqual("openai", context.exception.provider)
        self.assertEqual("chapter_generation", context.exception.stage)
        self.assertEqual("gpt-test", context.exception.model)
        self.assertIn("choices", str(context.exception))

    def test_openai_response_extraction_wraps_non_string_content(self) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content={"text": "not prose"}),
                )
            ]
        )

        with self.assertRaises(ModelCallError) as context:
            _extract_message_content(response, stage="scene_repair", model="gpt-test")

        self.assertEqual("openai", context.exception.provider)
        self.assertEqual("scene_repair", context.exception.stage)
        self.assertEqual("gpt-test", context.exception.model)
        self.assertIn("string", str(context.exception))

    def test_openai_client_passes_timeout_and_max_tokens(self) -> None:
        captured: dict[str, object] = {}

        class FakeCompletions:
            def create(self, **kwargs: object) -> object:
                captured["request_kwargs"] = kwargs
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

        class FakeOpenAI:
            def __init__(self, **kwargs: object) -> None:
                captured["client_kwargs"] = kwargs
                self.chat = SimpleNamespace(completions=FakeCompletions())

        fake_module = SimpleNamespace(OpenAI=FakeOpenAI)
        originals = {
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
            "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL"),
            "OPENAI_MODEL": os.environ.get("OPENAI_MODEL"),
            "OPENAI_TIMEOUT_SECONDS": os.environ.get("OPENAI_TIMEOUT_SECONDS"),
            "OPENAI_MAX_OUTPUT_TOKENS": os.environ.get("OPENAI_MAX_OUTPUT_TOKENS"),
            "OPENAI_MAX_RETRIES": os.environ.get("OPENAI_MAX_RETRIES"),
            "OPENAI_STREAM": os.environ.get("OPENAI_STREAM"),
        }
        os.environ["OPENAI_API_KEY"] = "test-key"
        os.environ["OPENAI_BASE_URL"] = ""
        os.environ["OPENAI_MODEL"] = "test-model"
        os.environ["OPENAI_TIMEOUT_SECONDS"] = "9"
        os.environ["OPENAI_MAX_OUTPUT_TOKENS"] = "77"
        os.environ["OPENAI_MAX_RETRIES"] = "3"
        os.environ["OPENAI_STREAM"] = "false"
        try:
            with patch.dict(sys.modules, {"openai": fake_module}):
                self.assertEqual("ok", chat_completion([{"role": "user", "content": "hello"}]))
        finally:
            for name, value in originals.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.assertEqual({"api_key": "test-key", "timeout": 9, "max_retries": 3}, captured["client_kwargs"])
        self.assertEqual(
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": "hello"}],
                "temperature": 0.8,
                "max_tokens": 77,
            },
            captured["request_kwargs"],
        )

    def test_openai_client_streams_response_by_default(self) -> None:
        captured: dict[str, object] = {}

        class FakeCompletions:
            def create(self, **kwargs: object) -> object:
                captured["request_kwargs"] = kwargs
                return [
                    SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="streamed "))]),
                    SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="ok"))]),
                ]

        class FakeOpenAI:
            def __init__(self, **kwargs: object) -> None:
                self.chat = SimpleNamespace(completions=FakeCompletions())

        fake_module = SimpleNamespace(OpenAI=FakeOpenAI)
        originals = {
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
            "OPENAI_MODEL": os.environ.get("OPENAI_MODEL"),
            "OPENAI_STREAM": os.environ.get("OPENAI_STREAM"),
        }
        os.environ["OPENAI_API_KEY"] = "test-key"
        os.environ["OPENAI_MODEL"] = "test-model"
        os.environ["OPENAI_STREAM"] = ""
        try:
            with patch.dict(sys.modules, {"openai": fake_module}):
                self.assertEqual("streamed ok", chat_completion([{"role": "user", "content": "hello"}]))
        finally:
            for name, value in originals.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.assertTrue(captured["request_kwargs"]["stream"])

    def test_openai_client_timeout_failure_records_attempt_diagnostics(self) -> None:
        class FakeCompletions:
            def create(self, **kwargs: object) -> object:
                raise TimeoutError("Request timed out.")

        class FakeOpenAI:
            def __init__(self, **kwargs: object) -> None:
                self.chat = SimpleNamespace(completions=FakeCompletions())

        fake_module = SimpleNamespace(OpenAI=FakeOpenAI)
        originals = {
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
            "OPENAI_MODEL": os.environ.get("OPENAI_MODEL"),
            "OPENAI_STREAM": os.environ.get("OPENAI_STREAM"),
        }
        os.environ["OPENAI_API_KEY"] = "test-key"
        os.environ["OPENAI_MODEL"] = "test-model"
        os.environ["OPENAI_STREAM"] = "false"
        try:
            with patch.dict(sys.modules, {"openai": fake_module}):
                with self.assertRaises(ModelCallError) as context:
                    chat_completion([{"role": "user", "content": "hello"}])
        finally:
            for name, value in originals.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        diagnostic = context.exception.to_dict()
        self.assertEqual("timeout", diagnostic["failure_category"])
        self.assertTrue(diagnostic["retryable"])
        self.assertEqual(1, diagnostic["attempts"])
        self.assertIsInstance(diagnostic["elapsed_ms"], int)

    def test_claude_client_requires_api_key(self) -> None:
        original_key = os.environ.get("ANTHROPIC_API_KEY")
        original_model = os.environ.get("CLAUDE_MODEL")
        os.environ["ANTHROPIC_API_KEY"] = ""
        os.environ["CLAUDE_MODEL"] = "test-model"
        try:
            with self.assertRaises(ModelCallError) as context:
                polish_chapter("chapter text", dry_run=False)
        finally:
            if original_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = original_key
            if original_model is None:
                os.environ.pop("CLAUDE_MODEL", None)
            else:
                os.environ["CLAUDE_MODEL"] = original_model

        self.assertEqual("anthropic", context.exception.provider)
        self.assertEqual("claude_polish", context.exception.stage)
        self.assertEqual("test-model", context.exception.model)

    def test_claude_dry_run_returns_input(self) -> None:
        self.assertEqual("chapter text", polish_chapter("chapter text", dry_run=True))

    def test_claude_client_passes_timeout_and_max_tokens(self) -> None:
        captured: dict[str, object] = {}

        class FakeMessages:
            def create(self, **kwargs: object) -> object:
                captured["request_kwargs"] = kwargs
                return SimpleNamespace(content=[SimpleNamespace(text="polished")])

        class FakeAnthropic:
            def __init__(self, **kwargs: object) -> None:
                captured["client_kwargs"] = kwargs
                self.messages = FakeMessages()

        fake_module = SimpleNamespace(Anthropic=FakeAnthropic)
        originals = {
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
            "CLAUDE_BASE_URL": os.environ.get("CLAUDE_BASE_URL"),
            "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL"),
            "CLAUDE_USER_AGENT": os.environ.get("CLAUDE_USER_AGENT"),
            "CLAUDE_MODEL": os.environ.get("CLAUDE_MODEL"),
            "CLAUDE_MAX_TOKENS": os.environ.get("CLAUDE_MAX_TOKENS"),
            "CLAUDE_TIMEOUT_SECONDS": os.environ.get("CLAUDE_TIMEOUT_SECONDS"),
            "CLAUDE_STREAM": os.environ.get("CLAUDE_STREAM"),
        }
        os.environ["ANTHROPIC_API_KEY"] = "test-anthropic"
        os.environ["CLAUDE_BASE_URL"] = "https://claude.example.test"
        os.environ["ANTHROPIC_BASE_URL"] = ""
        os.environ["CLAUDE_USER_AGENT"] = "claude-cli/1.0 test"
        os.environ["CLAUDE_MODEL"] = "claude-test"
        os.environ["CLAUDE_MAX_TOKENS"] = "55"
        os.environ["CLAUDE_TIMEOUT_SECONDS"] = "8"
        os.environ["CLAUDE_STREAM"] = "false"
        try:
            with patch.dict(sys.modules, {"anthropic": fake_module}):
                self.assertEqual("polished", polish_chapter("chapter text", dry_run=False))
        finally:
            for name, value in originals.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.assertEqual(
            {
                "api_key": "test-anthropic",
                "base_url": "https://claude.example.test",
                "timeout": 8,
                "default_headers": {"User-Agent": "claude-cli/1.0 test"},
            },
            captured["client_kwargs"],
        )
        self.assertEqual("claude-test", captured["request_kwargs"]["model"])
        self.assertEqual(55, captured["request_kwargs"]["max_tokens"])

    def test_claude_client_raises_polish_budget_for_long_chapters(self) -> None:
        captured: dict[str, object] = {}

        class FakeMessages:
            def create(self, **kwargs: object) -> object:
                captured["request_kwargs"] = kwargs
                return SimpleNamespace(content=[SimpleNamespace(text="polished")])

        class FakeAnthropic:
            def __init__(self, **kwargs: object) -> None:
                self.messages = FakeMessages()

        fake_module = SimpleNamespace(Anthropic=FakeAnthropic)
        originals = {
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
            "CLAUDE_MODEL": os.environ.get("CLAUDE_MODEL"),
            "CLAUDE_MAX_TOKENS": os.environ.get("CLAUDE_MAX_TOKENS"),
            "CLAUDE_STREAM": os.environ.get("CLAUDE_STREAM"),
        }
        os.environ["ANTHROPIC_API_KEY"] = "test-anthropic"
        os.environ["CLAUDE_MODEL"] = "claude-test"
        os.environ["CLAUDE_MAX_TOKENS"] = "55"
        os.environ["CLAUDE_STREAM"] = "false"
        try:
            with patch.dict(sys.modules, {"anthropic": fake_module}):
                self.assertEqual("polished", polish_chapter("章" * 10085, dry_run=False))
        finally:
            for name, value in originals.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.assertEqual(20170, captured["request_kwargs"]["max_tokens"])

    def test_polish_max_tokens_uses_configured_floor_for_short_chapters(self) -> None:
        self.assertEqual(8000, _polish_max_tokens("short chapter", 8000))

    def test_claude_client_streams_response_by_default(self) -> None:
        captured: dict[str, object] = {}

        class FakeStream:
            text_stream = ["streamed ", "polish"]

            def __enter__(self) -> "FakeStream":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                return None

        class FakeMessages:
            def stream(self, **kwargs: object) -> object:
                captured["request_kwargs"] = kwargs
                return FakeStream()

        class FakeAnthropic:
            def __init__(self, **kwargs: object) -> None:
                captured["client_kwargs"] = kwargs
                self.messages = FakeMessages()

        fake_module = SimpleNamespace(Anthropic=FakeAnthropic)
        originals = {
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
            "CLAUDE_MODEL": os.environ.get("CLAUDE_MODEL"),
            "CLAUDE_STREAM": os.environ.get("CLAUDE_STREAM"),
        }
        os.environ["ANTHROPIC_API_KEY"] = "test-anthropic"
        os.environ["CLAUDE_MODEL"] = "claude-test"
        os.environ["CLAUDE_STREAM"] = ""
        try:
            with patch.dict(sys.modules, {"anthropic": fake_module}):
                self.assertEqual("streamed polish", polish_chapter("chapter text", dry_run=False))
        finally:
            for name, value in originals.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.assertEqual("claude-test", captured["request_kwargs"]["model"])

    def test_claude_timeout_failure_records_attempt_diagnostics(self) -> None:
        class FakeMessages:
            def create(self, **kwargs: object) -> object:
                raise TimeoutError("Request timed out or interrupted.")

        class FakeAnthropic:
            def __init__(self, **kwargs: object) -> None:
                self.messages = FakeMessages()

        fake_module = SimpleNamespace(Anthropic=FakeAnthropic)
        originals = {
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
            "CLAUDE_MODEL": os.environ.get("CLAUDE_MODEL"),
            "CLAUDE_STREAM": os.environ.get("CLAUDE_STREAM"),
        }
        os.environ["ANTHROPIC_API_KEY"] = "test-anthropic"
        os.environ["CLAUDE_MODEL"] = "claude-test"
        os.environ["CLAUDE_STREAM"] = "false"
        try:
            with patch.dict(sys.modules, {"anthropic": fake_module}):
                with self.assertRaises(ModelCallError) as context:
                    polish_chapter("chapter text", dry_run=False)
        finally:
            for name, value in originals.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        diagnostic = context.exception.to_dict()
        self.assertEqual("timeout", diagnostic["failure_category"])
        self.assertTrue(diagnostic["retryable"])
        self.assertEqual(1, diagnostic["attempts"])
        self.assertIsInstance(diagnostic["elapsed_ms"], int)

    def test_claude_response_extraction_wraps_missing_content_blocks(self) -> None:
        with self.assertRaises(ModelCallError) as context:
            _extract_message_text(SimpleNamespace(content=None), model="claude-test")

        self.assertEqual("anthropic", context.exception.provider)
        self.assertEqual("claude_polish", context.exception.stage)
        self.assertEqual("claude-test", context.exception.model)
        self.assertIn("content blocks", str(context.exception))

    def test_claude_response_extraction_wraps_missing_text_content(self) -> None:
        with self.assertRaises(ModelCallError) as context:
            _extract_message_text(SimpleNamespace(content=[SimpleNamespace(type="tool_use")]), model="claude-test")

        self.assertEqual("anthropic", context.exception.provider)
        self.assertEqual("claude_polish", context.exception.stage)
        self.assertEqual("claude-test", context.exception.model)
        self.assertIn("text content", str(context.exception))


if __name__ == "__main__":
    unittest.main()
