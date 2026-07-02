from __future__ import annotations

import unittest
from pathlib import Path

from core.engine.preflight import V1_STRUCTURE_PATHS


class RepoHygieneTest(unittest.TestCase):
    def test_v1_standard_engineering_structure_exists(self) -> None:
        root = Path.cwd()
        missing = sorted(str(path) for path in V1_STRUCTURE_PATHS if not (root / path).exists())
        self.assertEqual([], missing)

    def test_legacy_core_files_are_thin_wrappers(self) -> None:
        wrapper_imports = {
            "core/generator.py": "modules.chapter_generator",
            "core/analyzer.py": "modules.conflict_engine",
            "core/input_pack.py": "core.state.input_pack",
            "core/snapshot.py": "core.state.snapshot",
            "core/updater.py": "core.state.snapshot",
        }

        for path, expected_import in wrapper_imports.items():
            content = (Path.cwd() / path).read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertIn(expected_import, content)
                self.assertNotIn("from api.openai_client import chat_completion", content)
                self.assertNotIn("from api.claude_client import polish_chapter", content)

    def test_runtime_entrypoints_are_v1_orchestrator_surfaces(self) -> None:
        import core
        import core.engine as engine
        import core.orchestrator as orchestrator

        self.assertIs(orchestrator.run_agent_once, core.run_agent_once)
        self.assertIs(orchestrator.run_agent_loop, core.run_agent_loop)
        self.assertIs(orchestrator.check_runtime, core.check_runtime)
        self.assertIs(orchestrator.report_runs, core.report_runs)
        self.assertTrue(callable(engine.run_once))
        self.assertTrue(callable(engine.run_loop))
        self.assertTrue(callable(engine.run_preflight))
        self.assertTrue(callable(engine.build_run_report))

    def test_gitignore_covers_runtime_and_cache_artifacts(self) -> None:
        patterns = {
            line.strip()
            for line in (Path.cwd() / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }

        required_patterns = {
            ".env",
            ".tmp/",
            ".venv/",
            ".idea/",
            "__pycache__/",
            "*.pyc",
            ".pytest_cache/",
            ".mypy_cache/",
            ".ruff_cache/",
            ".coverage",
            "htmlcov/",
            "*.log",
            "data/runs/",
            "data/chapters/",
            "data/snapshot.json",
            "data/memory.json",
            "data/memory_outbox.jsonl",
            "data/memory_outbox*.jsonl",
        }

        self.assertEqual(set(), required_patterns - patterns)

    def test_committed_runtime_samples_use_example_files(self) -> None:
        root = Path.cwd()
        self.assertTrue((root / "data" / "snapshot.example.json").is_file())
        self.assertTrue((root / "data" / "notion_memory.example.json").is_file())
        self.assertFalse((root / "data" / "snapshot.example.json").read_text(encoding="utf-8").strip() == "")
        self.assertFalse((root / "data" / "notion_memory.example.json").read_text(encoding="utf-8").strip() == "")

    def test_env_example_contains_only_names_and_recommended_models(self) -> None:
        content = (Path.cwd() / ".env.example").read_text(encoding="utf-8")
        values = {}
        for line in content.splitlines():
            if not line.strip():
                continue
            key, separator, value = line.partition("=")
            with self.subTest(line=line):
                self.assertEqual("=", separator)
                self.assertTrue(key)
                values[key] = value

        self.assertEqual("gpt-4.1-mini", values["OPENAI_MODEL"])
        self.assertEqual("claude-3-5-sonnet-latest", values["CLAUDE_MODEL"])
        for key, value in values.items():
            if key.endswith("_MODEL"):
                continue
            with self.subTest(key=key):
                self.assertEqual("", value)


if __name__ == "__main__":
    unittest.main()
