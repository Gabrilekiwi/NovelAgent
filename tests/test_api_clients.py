from __future__ import annotations

import os
from types import SimpleNamespace
import unittest

from api.contracts import ModelCallError
from api.claude_client import _extract_message_text, polish_chapter
from api.openai_client import _extract_message_content, chat_completion


class ApiClientTest(unittest.TestCase):
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
