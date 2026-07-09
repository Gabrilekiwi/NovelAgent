from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from core.rules import (
    build_rule_repair_plan,
    build_rule_repair_prompt,
    validate_chapter_against_rules,
)
from core.schema import validate_schema


FIXTURE_DIR = Path("tests/fixtures/chapter_quality")


class RuleRepairPromptTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_rule_repair_prompt" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _snapshot(self) -> dict:
        return json.loads((FIXTURE_DIR / "snapshot.json").read_text(encoding="utf-8"))

    def _chapter(self, name: str) -> str:
        return (FIXTURE_DIR / name).read_text(encoding="utf-8")

    def _repair_plan(self) -> dict:
        report = validate_chapter_against_rules(
            chapter_text=self._chapter("bad_chapter.md"),
            snapshot=self._snapshot(),
            previous_chapter_text=self._chapter("previous_chapter.md"),
            use_default_rules=True,
        )
        return build_rule_repair_plan(rule_validation_report=report)

    def _prompt_result(self, **kwargs: object) -> dict:
        return build_rule_repair_prompt(
            chapter_text=self._chapter("bad_chapter.md"),
            snapshot=self._snapshot(),
            previous_chapter_text=self._chapter("previous_chapter.md"),
            rule_repair_plan=self._repair_plan(),
            **kwargs,
        )

    def test_build_prompt_from_repair_plan(self) -> None:
        result = self._prompt_result()

        prompt = result["prompt"]
        self.assertIn("Rule-aware Chapter Repair Prompt", prompt)
        self.assertIn("Original Chapter", prompt)
        self.assertIn("Repair Tasks", prompt)
        self.assertIn("Acceptance Criteria", prompt)

    def test_prompt_includes_repair_task_details(self) -> None:
        prompt = self._prompt_result()["prompt"]

        self.assertIn("repair_001", prompt)
        self.assertIn("repair_type", prompt)
        self.assertIn("instruction", prompt)
        self.assertIn("blocking", prompt)
        self.assertIn("evidence", prompt)
        self.assertIn("remove_meta_output", prompt)

    def test_prompt_includes_snapshot_context(self) -> None:
        prompt = self._prompt_result()["prompt"]

        self.assertIn("Snapshot Context", prompt)
        self.assertIn("chapter_index", prompt)
        self.assertIn("story_state", prompt)
        self.assertIn("spatial_state", prompt)
        self.assertIn("characters", prompt)

    def test_prompt_includes_previous_chapter_when_provided(self) -> None:
        with_previous = self._prompt_result()["prompt"]
        without_previous = build_rule_repair_prompt(
            chapter_text=self._chapter("bad_chapter.md"),
            snapshot=self._snapshot(),
            rule_repair_plan=self._repair_plan(),
        )["prompt"]

        self.assertIn("Previous Chapter Context", with_previous)
        self.assertNotIn("Previous Chapter Context", without_previous)

    def test_blocking_only_excludes_non_blocking_tasks(self) -> None:
        plan = self._mixed_plan()

        result = build_rule_repair_prompt(
            chapter_text=self._chapter("bad_chapter.md"),
            snapshot=self._snapshot(),
            rule_repair_plan=plan,
            include_non_blocking=False,
        )

        self.assertEqual(1, result["metadata"]["prompt"]["task_count"])
        self.assertIn("repair_001", result["prompt"])
        self.assertNotIn("repair_002", result["prompt"])

    def test_max_tasks_limits_tasks(self) -> None:
        result = self._prompt_result(max_tasks=1)

        self.assertEqual(1, result["metadata"]["prompt"]["task_count"])
        self.assertIn("repair_001", result["prompt"])
        self.assertNotIn("repair_002", result["prompt"])

    def test_no_tasks_still_builds_prompt(self) -> None:
        plan = self._no_task_plan()

        result = build_rule_repair_prompt(
            chapter_text=self._chapter("good_chapter.md"),
            snapshot=self._snapshot(),
            rule_repair_plan=plan,
        )

        self.assertIn("当前没有需要修复的任务", result["prompt"])
        self.assertEqual(0, result["metadata"]["prompt"]["task_count"])

    def test_metadata_schema_validates(self) -> None:
        metadata = self._prompt_result()["metadata"]

        self.assertIs(metadata, validate_schema(metadata, "rule_repair_prompt_metadata.schema.json"))

    def test_does_not_modify_inputs(self) -> None:
        chapter_text = self._chapter("bad_chapter.md")
        snapshot = self._snapshot()
        plan = self._repair_plan()
        chapter_before = str(chapter_text)
        snapshot_before = copy.deepcopy(snapshot)
        plan_before = copy.deepcopy(plan)

        build_rule_repair_prompt(
            chapter_text=chapter_text,
            snapshot=snapshot,
            rule_repair_plan=plan,
        )

        self.assertEqual(chapter_before, chapter_text)
        self.assertEqual(snapshot_before, snapshot)
        self.assertEqual(plan_before, plan)

    def test_cli_can_write_prompt_and_metadata(self) -> None:
        case_dir = self._case_dir("cli")
        plan_path = case_dir / "rule_repair_plan.json"
        prompt_path = case_dir / "rule_repair_prompt.md"
        metadata_path = case_dir / "rule_repair_prompt_metadata.json"
        plan_path.write_text(json.dumps(self._repair_plan(), ensure_ascii=False, indent=2), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/build_rule_repair_prompt.py",
                "--chapter",
                str(FIXTURE_DIR / "bad_chapter.md"),
                "--snapshot",
                str(FIXTURE_DIR / "snapshot.json"),
                "--previous",
                str(FIXTURE_DIR / "previous_chapter.md"),
                "--rule-repair-plan",
                str(plan_path),
                "--out",
                str(prompt_path),
                "--metadata-out",
                str(metadata_path),
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(prompt_path.exists())
        self.assertTrue(metadata_path.exists())
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertIs(metadata, validate_schema(metadata, "rule_repair_prompt_metadata.schema.json"))

    def test_cli_json_outputs_pure_json(self) -> None:
        case_dir = self._case_dir("cli_json")
        plan_path = case_dir / "rule_repair_plan.json"
        plan_path.write_text(json.dumps(self._repair_plan(), ensure_ascii=False, indent=2), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/build_rule_repair_prompt.py",
                "--chapter",
                str(FIXTURE_DIR / "bad_chapter.md"),
                "--snapshot",
                str(FIXTURE_DIR / "snapshot.json"),
                "--rule-repair-plan",
                str(plan_path),
                "--json",
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        metadata = json.loads(result.stdout)
        self.assertIn("schema_version", metadata)
        self.assertIn("kind", metadata)
        self.assertIn("chars", metadata)
        self.assertIn("prompt", metadata)
        self.assertIn("source_plan", metadata)

    def test_cli_print_outputs_prompt(self) -> None:
        case_dir = self._case_dir("cli_print")
        plan_path = case_dir / "rule_repair_plan.json"
        plan_path.write_text(json.dumps(self._repair_plan(), ensure_ascii=False, indent=2), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/build_rule_repair_prompt.py",
                "--chapter",
                str(FIXTURE_DIR / "bad_chapter.md"),
                "--snapshot",
                str(FIXTURE_DIR / "snapshot.json"),
                "--rule-repair-plan",
                str(plan_path),
                "--print",
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Rule-aware Chapter Repair Prompt", result.stdout)
        self.assertIn("Repair Tasks", result.stdout)

    def test_narrative_rules_section_is_optional(self) -> None:
        without_rules = self._prompt_result()["prompt"]
        with_rules = self._prompt_result(narrative_rules="# Rules\nKeep prose only.")["prompt"]

        self.assertNotIn("Narrative Rules", without_rules)
        self.assertIn("Narrative Rules", with_rules)
        self.assertTrue(self._prompt_result(narrative_rules="# Rules")["metadata"]["prompt"]["has_narrative_rules"])

    def _mixed_plan(self) -> dict:
        plan = self._repair_plan()
        plan["tasks"] = [
            task for task in plan["tasks"]
            if task["blocking"]
        ][:1] + [
            task for task in plan["tasks"]
            if not task["blocking"]
        ][:1]
        plan["summary"] = {
            "task_count": len(plan["tasks"]),
            "blocking_task_count": sum(1 for task in plan["tasks"] if task["blocking"]),
            "human_review_task_count": sum(1 for task in plan["tasks"] if task["requires_human_review"]),
            "fail_task_count": sum(1 for task in plan["tasks"] if task["rule_status"] == "fail"),
            "warning_task_count": sum(1 for task in plan["tasks"] if task["rule_status"] == "warning"),
        }
        plan["status"] = "blocked"
        return validate_schema(plan, "rule_repair_plan.schema.json")

    def _no_task_plan(self) -> dict:
        return validate_schema(
            {
                "schema_version": "1.0",
                "status": "no_repair_needed",
                "summary": {
                    "task_count": 0,
                    "blocking_task_count": 0,
                    "human_review_task_count": 0,
                    "fail_task_count": 0,
                    "warning_task_count": 0,
                },
                "source_report": {
                    "status": "pass",
                    "score": 100,
                    "rule_pack_id": "test_rules",
                    "violation_count": 0,
                },
                "tasks": [],
                "metadata": {
                    "created_by": "NovelAgent",
                    "source": "rule-aware-repair-plan",
                },
            },
            "rule_repair_plan.schema.json",
        )


if __name__ == "__main__":
    unittest.main()
