from __future__ import annotations

import os
import unittest

from core.config import get_config


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
