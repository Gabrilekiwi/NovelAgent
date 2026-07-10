from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from core.schema import validate_schema
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.oh_story_detection import detect_oh_story_compatibility


class OhStoryDetectionTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_oh_story_detection" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _story_project_root(self, name: str) -> Path:
        root = self._case_dir(name)
        for directory_name in CORE_DIRECTORY_NAMES:
            (root / directory_name).mkdir()
        return root

    def test_core_story_project_dirs_are_not_enough_to_detect_oh_story(self) -> None:
        root = self._story_project_root("no_markers")

        report = detect_oh_story_compatibility(root)

        self.assertIs(report, validate_schema(report, "oh_story_compatibility.schema.json"))
        self.assertFalse(report["detected"])
        self.assertEqual("none", report["confidence"])
        self.assertTrue(report["capabilities"]["story_project_core_dirs"])
        self.assertEqual(4, report["summary"]["present_count"])

    def test_story_deployed_marker_sets_low_confidence(self) -> None:
        root = self._story_project_root("deployed")
        (root / ".story-deployed").write_text("ok", encoding="utf-8")

        report = detect_oh_story_compatibility(root)

        self.assertTrue(report["detected"])
        self.assertEqual("low", report["confidence"])
        marker = self._marker(report, ".story-deployed")
        self.assertTrue(marker["present"])
        self.assertEqual("deployment_marker", marker["kind"])

    def test_codex_hooks_marker_is_read_but_not_executed(self) -> None:
        root = self._story_project_root("hooks")
        hooks = root / ".codex" / "hooks.json"
        hooks.parent.mkdir()
        hooks.write_text(json.dumps({"PreToolUse": [{"command": "node should-not-run.js"}]}), encoding="utf-8")

        report = detect_oh_story_compatibility(root)

        marker = self._marker(report, ".codex/hooks.json")
        self.assertTrue(marker["present"])
        self.assertTrue(marker["details"]["json_valid"])
        self.assertEqual("medium", report["confidence"])
        self.assertFalse(report["capabilities"]["oh_story_js_execution"])

    def test_invalid_hooks_json_records_warning(self) -> None:
        root = self._story_project_root("invalid_hooks")
        hooks = root / ".codex" / "hooks.json"
        hooks.parent.mkdir()
        hooks.write_text("{bad json", encoding="utf-8")

        report = detect_oh_story_compatibility(root)

        self.assertTrue(self._marker(report, ".codex/hooks.json")["present"])
        self.assertTrue(any("invalid JSON" in warning for warning in report["warnings"]))

    def test_agents_directories_are_markers_only(self) -> None:
        root = self._story_project_root("agents")
        (root / ".claude" / "agents").mkdir(parents=True)
        (root / ".codex" / "agents").mkdir(parents=True)

        report = detect_oh_story_compatibility(root)

        self.assertTrue(self._marker(report, ".claude/agents")["present"])
        self.assertTrue(self._marker(report, ".codex/agents")["present"])
        self.assertEqual("medium", report["confidence"])

    def test_package_story_scripts_are_reported_without_execution(self) -> None:
        root = self._story_project_root("package_scripts")
        (root / "package.json").write_text(
            json.dumps({"scripts": {"story:check": "node scripts/check-ai-patterns.js", "test": "pytest"}}),
            encoding="utf-8",
        )

        report = detect_oh_story_compatibility(root)

        marker = self._marker(report, "package.json:scripts")
        self.assertTrue(marker["present"])
        self.assertEqual([{"name": "story:check", "command": "node scripts/check-ai-patterns.js"}], marker["details"]["scripts"])
        self.assertFalse(report["capabilities"]["oh_story_js_execution"])

    def test_invalid_package_json_records_warning_and_does_not_crash(self) -> None:
        root = self._story_project_root("invalid_package")
        (root / "package.json").write_text("{bad json", encoding="utf-8")

        report = detect_oh_story_compatibility(root)

        self.assertTrue(self._marker(report, "package.json:scripts")["present"])
        self.assertTrue(any("package.json: invalid JSON" in warning for warning in report["warnings"]))

    def _marker(self, report: dict, name: str) -> dict:
        for marker in report["markers"]:
            if marker["name"] == name:
                return marker
        raise AssertionError(f"marker not found: {name}")


if __name__ == "__main__":
    unittest.main()
