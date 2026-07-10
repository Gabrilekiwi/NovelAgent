from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from core.schema import validate_schema
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.oh_story_detection import (
    QUALITY_SCRIPT_NAMES,
    STORY_AGENT_NAMES,
    detect_oh_story_compatibility,
)


class OhStoryDetectionTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_oh_story_detection" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _story_project_root(self, name: str, *, parent: Path | None = None) -> Path:
        root = (parent / "book") if parent is not None else self._case_dir(name)
        root.mkdir(parents=True, exist_ok=True)
        for directory_name in CORE_DIRECTORY_NAMES:
            (root / directory_name).mkdir()
        return root

    def test_core_story_project_dirs_are_not_enough_to_detect_oh_story(self) -> None:
        root = self._story_project_root("no_markers")

        report = detect_oh_story_compatibility(root)

        self.assertIs(report, validate_schema(report, "oh_story_compatibility.schema.json"))
        self.assertFalse(report["detected"])
        self.assertEqual("none", report["confidence"])
        self.assertEqual(str(root), report["workspace_root"])
        self.assertTrue(report["capabilities"]["story_project_core_dirs"])
        self.assertTrue(report["capabilities"]["chapter_blueprint"])
        self.assertTrue(report["capabilities"]["story_project_writeback"])
        self.assertFalse(report["capabilities"]["active_book"])
        self.assertFalse(report["capabilities"]["story_setup"])
        self.assertFalse(report["capabilities"]["codex_hooks"])
        self.assertFalse(report["capabilities"]["story_agents"])
        self.assertFalse(report["capabilities"]["quality_scripts"])
        self.assertEqual(4, report["summary"]["present_count"])

    def test_workspace_root_is_distinct_from_story_project_root(self) -> None:
        workspace = self._case_dir("workspace_scope")
        root = self._story_project_root("workspace_scope", parent=workspace)
        (workspace / ".story-deployed").write_text("target_cli: codex\n", encoding="utf-8")
        (workspace / ".active-book").write_text("book\n", encoding="utf-8")

        report = detect_oh_story_compatibility(root, workspace_root=workspace)

        self.assertEqual(str(root), report["root"])
        self.assertEqual(str(workspace), report["workspace_root"])
        self.assertTrue(report["detected"])
        self.assertEqual("low", report["confidence"])
        self.assertTrue(report["capabilities"]["active_book"])
        marker = self._marker(report, ".story-deployed")
        self.assertEqual(str(workspace / ".story-deployed"), marker["path"])

    def test_missing_story_project_root_has_no_detected_capabilities(self) -> None:
        missing = self._case_dir("missing_root") / "missing"

        report = detect_oh_story_compatibility(missing)

        self.assertFalse(report["detected"])
        self.assertEqual("none", report["confidence"])
        self.assertFalse(any(report["capabilities"].values()))
        self.assertTrue(any("does not exist" in warning for warning in report["warnings"]))

    def test_story_setup_requires_recognizable_skill_content(self) -> None:
        workspace = self._case_dir("story_setup")
        root = self._story_project_root("story_setup", parent=workspace)
        skill = workspace / "skills" / "story-setup" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# unrelated setup notes\n", encoding="utf-8")

        unrelated = detect_oh_story_compatibility(root, workspace_root=workspace)
        self.assertFalse(self._marker(unrelated, "skills/story-setup/SKILL.md")["present"])
        self.assertFalse(unrelated["detected"])

        skill.write_text(
            "---\nname: story-setup\n---\nDeploy .story-deployed, hooks, and agents safely.\n",
            encoding="utf-8",
        )
        report = detect_oh_story_compatibility(root, workspace_root=workspace)

        self.assertTrue(self._marker(report, "skills/story-setup/SKILL.md")["present"])
        self.assertTrue(report["capabilities"]["story_setup"])

    def test_unrelated_codex_hooks_do_not_count_as_oh_story(self) -> None:
        root = self._story_project_root("unrelated_hooks")
        hooks = root / ".codex" / "hooks.json"
        hooks.parent.mkdir()
        hooks.write_text(json.dumps({"Stop": [{"command": "python tools/history.py"}]}), encoding="utf-8")

        report = detect_oh_story_compatibility(root)

        marker = self._marker(report, ".codex/hooks.json")
        self.assertFalse(marker["present"])
        self.assertTrue(marker["details"]["json_valid"])
        self.assertEqual([], marker["details"]["story_routes"])
        self.assertFalse(report["detected"])

    def test_codex_hooks_require_valid_route_and_adapter_content(self) -> None:
        root = self._story_project_root("story_hooks")
        hooks = root / ".codex" / "hooks.json"
        adapter = root / ".codex" / "hooks" / "story_codex_hook.py"
        adapter.parent.mkdir(parents=True)
        hooks.write_text(
            json.dumps(
                {
                    "SessionStart": [
                        {"command": "python .codex/hooks/story_codex_hook.py session-start"}
                    ]
                }
            ),
            encoding="utf-8",
        )
        adapter.write_text(
            '"""Codex hook adapter for oh-story."""\n'
            'SENTINEL = ".story-deployed"\n'
            'EVENT = "pre-tool-prose-guard"\n',
            encoding="utf-8",
        )

        report = detect_oh_story_compatibility(root)

        self.assertTrue(self._marker(report, ".codex/hooks.json")["present"])
        self.assertTrue(self._marker(report, ".codex/hooks/story_codex_hook.py")["present"])
        self.assertTrue(report["capabilities"]["codex_hooks"])
        self.assertEqual("medium", report["confidence"])
        self.assertFalse(report["capabilities"]["oh_story_js_execution"])

    def test_invalid_hooks_json_warns_but_is_not_a_signal(self) -> None:
        root = self._story_project_root("invalid_hooks")
        hooks = root / ".codex" / "hooks.json"
        hooks.parent.mkdir()
        hooks.write_text("{bad json", encoding="utf-8")

        report = detect_oh_story_compatibility(root)

        marker = self._marker(report, ".codex/hooks.json")
        self.assertFalse(marker["present"])
        self.assertTrue(marker["details"]["exists"])
        self.assertFalse(marker["details"]["json_valid"])
        self.assertFalse(report["detected"])
        self.assertTrue(any("invalid JSON" in warning for warning in report["warnings"]))

    def test_agents_directory_or_random_agent_is_not_a_signal(self) -> None:
        root = self._story_project_root("random_agents")
        agents = root / ".codex" / "agents"
        agents.mkdir(parents=True)
        (agents / "general.toml").write_text('name = "general"\ndescription = "General helper"\n', encoding="utf-8")

        report = detect_oh_story_compatibility(root)

        marker = self._marker(report, "story-agents")
        self.assertFalse(marker["present"])
        self.assertEqual(0, marker["details"]["present_count"])
        self.assertFalse(report["detected"])

    def test_all_seven_named_agents_require_valid_content(self) -> None:
        root = self._story_project_root("story_agents")
        agents = root / ".codex" / "agents"
        agents.mkdir(parents=True)
        for name in STORY_AGENT_NAMES:
            (agents / f"{name}.toml").write_text(
                f'name = "{name}"\ndescription = "oh-story role for {name}"\n',
                encoding="utf-8",
            )

        report = detect_oh_story_compatibility(root)

        marker = self._marker(report, "story-agents")
        self.assertTrue(marker["present"])
        self.assertTrue(marker["details"]["complete"])
        self.assertEqual(7, marker["details"]["present_count"])
        self.assertEqual([], marker["details"]["missing_agents"])
        self.assertTrue(report["capabilities"]["story_agents"])

    def test_partial_named_agent_set_is_not_a_detection_signal(self) -> None:
        root = self._story_project_root("partial_story_agents")
        agents = root / ".codex" / "agents"
        agents.mkdir(parents=True)
        for name in STORY_AGENT_NAMES[:3]:
            (agents / f"{name}.toml").write_text(
                f'name = "{name}"\ndescription = "oh-story role for {name}"\n',
                encoding="utf-8",
            )

        report = detect_oh_story_compatibility(root)

        marker = self._marker(report, "story-agents")
        self.assertFalse(marker["present"])
        self.assertFalse(marker["details"]["complete"])
        self.assertEqual(3, marker["details"]["present_count"])
        self.assertFalse(report["detected"])

    def test_agents_doc_requires_story_routing_content(self) -> None:
        root = self._story_project_root("agents_doc")
        agents_doc = root / "AGENTS.md"
        agents_doc.write_text("# General coding instructions\nRun unit tests.\n", encoding="utf-8")

        unrelated = detect_oh_story_compatibility(root)
        self.assertFalse(self._marker(unrelated, "AGENTS.md:story-routing")["present"])
        self.assertFalse(unrelated["detected"])

        agents_doc.write_text(
            "# Story routing\nUse $story-setup before $story-long-write.\n",
            encoding="utf-8",
        )
        report = detect_oh_story_compatibility(root)

        marker = self._marker(report, "AGENTS.md:story-routing")
        self.assertTrue(marker["present"])
        self.assertEqual(["story-setup", "story-long-write"], marker["details"]["matched_routes"][:2])

    def test_exact_quality_scripts_are_detected_without_execution(self) -> None:
        root = self._story_project_root("quality_scripts")
        scripts = root / "skills" / "story-deslop" / "scripts"
        scripts.mkdir(parents=True)
        for script_name in QUALITY_SCRIPT_NAMES:
            (scripts / script_name).write_text(
                f'const scriptName = "{script_name}";\nconsole.log(scriptName);\n',
                encoding="utf-8",
            )

        report = detect_oh_story_compatibility(root)

        for script_name in QUALITY_SCRIPT_NAMES:
            self.assertTrue(self._marker(report, script_name)["present"])
        self.assertTrue(report["capabilities"]["quality_scripts"])
        self.assertFalse(report["capabilities"]["oh_story_js_execution"])

    def test_package_history_script_is_not_a_false_positive(self) -> None:
        root = self._story_project_root("package_history")
        (root / "package.json").write_text(
            json.dumps({"scripts": {"history": "python tools/history.py", "test": "pytest"}}),
            encoding="utf-8",
        )

        report = detect_oh_story_compatibility(root)

        marker = self._marker(report, "package.json:quality-scripts")
        self.assertFalse(marker["present"])
        self.assertEqual([], marker["details"]["scripts"])
        self.assertFalse(report["detected"])

    def test_package_only_matches_exact_quality_script_names(self) -> None:
        root = self._story_project_root("package_quality")
        (root / "package.json").write_text(
            json.dumps(
                {
                    "scripts": {
                        "story:check": "node skills/story-deslop/scripts/check-ai-patterns.js",
                        "history": "node tools/history.js",
                    }
                }
            ),
            encoding="utf-8",
        )

        report = detect_oh_story_compatibility(root)

        marker = self._marker(report, "package.json:quality-scripts")
        self.assertTrue(marker["present"])
        self.assertEqual(
            [
                {
                    "name": "story:check",
                    "command": "node skills/story-deslop/scripts/check-ai-patterns.js",
                    "quality_scripts": ["check-ai-patterns.js"],
                }
            ],
            marker["details"]["scripts"],
        )

    def test_invalid_package_json_warns_and_is_not_a_signal(self) -> None:
        root = self._story_project_root("invalid_package")
        (root / "package.json").write_text("{bad json", encoding="utf-8")

        report = detect_oh_story_compatibility(root)

        marker = self._marker(report, "package.json:quality-scripts")
        self.assertFalse(marker["present"])
        self.assertTrue(marker["details"]["exists"])
        self.assertFalse(marker["details"]["json_valid"])
        self.assertFalse(report["detected"])
        self.assertTrue(any("package.json: invalid JSON" in warning for warning in report["warnings"]))

    def _marker(self, report: dict, name: str) -> dict:
        for marker in report["markers"]:
            if marker["name"] == name:
                return marker
        raise AssertionError(f"marker not found: {name}")


if __name__ == "__main__":
    unittest.main()
