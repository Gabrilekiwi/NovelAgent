from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from core.quality import evaluate_chapter_quality
from core.review import build_human_review_report
from core.rules import (
    build_rule_repair_plan,
    build_rule_repair_prompt,
    validate_chapter_against_rules,
)
from core.schema import validate_schema


FIXTURE_DIR = Path("tests/fixtures/chapter_quality")


class HumanReviewReportTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_human_review_report" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _snapshot(self) -> dict:
        return json.loads((FIXTURE_DIR / "snapshot.json").read_text(encoding="utf-8"))

    def _chapter(self, name: str) -> str:
        return (FIXTURE_DIR / name).read_text(encoding="utf-8")

    def _artifacts(self, chapter_name: str = "bad_chapter.md") -> dict:
        chapter = self._chapter(chapter_name)
        snapshot = self._snapshot()
        quality = evaluate_chapter_quality(
            chapter_text=chapter,
            snapshot=snapshot,
            previous_chapter_text=self._chapter("previous_chapter.md"),
        )
        validation = validate_chapter_against_rules(
            chapter_text=chapter,
            snapshot=snapshot,
            previous_chapter_text=self._chapter("previous_chapter.md"),
            use_default_rules=True,
            quality_report=quality,
        )
        plan = build_rule_repair_plan(rule_validation_report=validation)
        prompt = build_rule_repair_prompt(
            chapter_text=chapter,
            snapshot=snapshot,
            previous_chapter_text=self._chapter("previous_chapter.md"),
            rule_repair_plan=plan,
        )
        return {
            "chapter": chapter,
            "quality": quality,
            "validation": validation,
            "plan": plan,
            "prompt_metadata": prompt["metadata"],
        }

    def test_build_report_from_bad_chapter_artifacts(self) -> None:
        artifacts = self._artifacts()

        result = build_human_review_report(
            chapter_text=artifacts["chapter"],
            chapter_quality_report=artifacts["quality"],
            rule_validation_report=artifacts["validation"],
            rule_repair_plan=artifacts["plan"],
            rule_repair_prompt_metadata=artifacts["prompt_metadata"],
        )

        markdown = result["markdown"]
        self.assertIn("小说章节审稿报告", markdown)
        self.assertIn("总体结论", markdown)
        self.assertIn("严重问题", markdown)
        self.assertIn("修复计划摘要", markdown)
        self.assertIn("下一步建议", markdown)

    def test_report_includes_decision(self) -> None:
        artifacts = self._artifacts()

        metadata = build_human_review_report(
            chapter_quality_report=artifacts["quality"],
            rule_validation_report=artifacts["validation"],
            rule_repair_plan=artifacts["plan"],
        )["metadata"]

        self.assertIn("decision", metadata)
        self.assertIn("label", metadata["decision"])
        self.assertIn("allowed_next_steps", metadata["decision"])
        self.assertEqual("blocked", metadata["decision"]["decision"])

    def test_report_includes_severe_and_warning_issues(self) -> None:
        artifacts = self._artifacts()

        markdown = build_human_review_report(
            chapter_quality_report=artifacts["quality"],
            rule_validation_report=artifacts["validation"],
            rule_repair_plan=artifacts["plan"],
        )["markdown"]

        self.assertIn("prose_only_no_meta_output", markdown)
        self.assertIn("## 4. 警告问题", markdown)
        self.assertTrue("continue_previous_ending" in markdown or "暂无。" in markdown)

    def test_report_includes_skipped_rules(self) -> None:
        artifacts = self._artifacts()

        markdown = build_human_review_report(
            chapter_quality_report=artifacts["quality"],
            rule_validation_report=artifacts["validation"],
            rule_repair_plan=artifacts["plan"],
        )["markdown"]

        self.assertIn("已跳过规则", markdown)
        self.assertIn("no_quality_check_mapping", markdown)

    def test_report_includes_repair_tasks(self) -> None:
        artifacts = self._artifacts()

        markdown = build_human_review_report(
            chapter_quality_report=artifacts["quality"],
            rule_validation_report=artifacts["validation"],
            rule_repair_plan=artifacts["plan"],
        )["markdown"]

        self.assertIn("repair_001", markdown)
        self.assertIn("repair_type", markdown)
        self.assertIn("blocking", markdown)

    def test_no_issue_report_works(self) -> None:
        artifacts = self._artifacts("good_chapter.md")
        artifacts["quality"]["status"] = "pass"
        artifacts["quality"]["score"] = 100
        artifacts["quality"]["summary"] = {"passed": 1, "warnings": 0, "failed": 0, "skipped": 0}
        artifacts["quality"]["checks"] = [
            {
                "code": "no_meta_output",
                "status": "pass",
                "severity": "critical",
                "message": "Synthetic pass.",
                "evidence": {},
            }
        ]
        artifacts["validation"]["status"] = "pass"
        artifacts["validation"]["score"] = 100
        artifacts["validation"]["summary"] = {"passed": 1, "warnings": 0, "failed": 0, "skipped": 0}
        artifacts["validation"]["rules"] = [
            {
                "code": "prose_only_no_meta_output",
                "title": "Only prose",
                "category": "output_contract",
                "severity": "critical",
                "status": "pass",
                "quality_check_codes": ["no_meta_output"],
                "matched_quality_checks": [],
                "reason": None,
            }
        ]
        artifacts["validation"]["violations"] = []
        artifacts["plan"] = self._no_task_plan()

        result = build_human_review_report(
            chapter_quality_report=artifacts["quality"],
            rule_validation_report=validate_schema(artifacts["validation"], "rule_validation_report.schema.json"),
            rule_repair_plan=artifacts["plan"],
        )

        self.assertEqual("accept", result["metadata"]["decision"]["decision"])
        self.assertIn("暂无。", result["markdown"])

    def test_metadata_schema_validates(self) -> None:
        artifacts = self._artifacts()

        metadata = build_human_review_report(
            chapter_quality_report=artifacts["quality"],
            rule_validation_report=artifacts["validation"],
            rule_repair_plan=artifacts["plan"],
            rule_repair_prompt_metadata=artifacts["prompt_metadata"],
        )["metadata"]

        self.assertIs(metadata, validate_schema(metadata, "human_review_report_metadata.schema.json"))

    def test_does_not_modify_inputs(self) -> None:
        artifacts = self._artifacts()
        chapter_before = str(artifacts["chapter"])
        quality_before = copy.deepcopy(artifacts["quality"])
        validation_before = copy.deepcopy(artifacts["validation"])
        plan_before = copy.deepcopy(artifacts["plan"])
        prompt_before = copy.deepcopy(artifacts["prompt_metadata"])

        build_human_review_report(
            chapter_text=artifacts["chapter"],
            chapter_quality_report=artifacts["quality"],
            rule_validation_report=artifacts["validation"],
            rule_repair_plan=artifacts["plan"],
            rule_repair_prompt_metadata=artifacts["prompt_metadata"],
        )

        self.assertEqual(chapter_before, artifacts["chapter"])
        self.assertEqual(quality_before, artifacts["quality"])
        self.assertEqual(validation_before, artifacts["validation"])
        self.assertEqual(plan_before, artifacts["plan"])
        self.assertEqual(prompt_before, artifacts["prompt_metadata"])

    def test_cli_can_write_markdown_and_metadata(self) -> None:
        case_dir = self._case_dir("cli")
        paths = self._write_artifacts(case_dir)
        report_path = case_dir / "human_review_report.md"
        metadata_path = case_dir / "human_review_report_metadata.json"

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/build_human_review_report.py",
                "--chapter",
                str(FIXTURE_DIR / "bad_chapter.md"),
                "--quality-report",
                str(paths["quality"]),
                "--rule-validation-report",
                str(paths["validation"]),
                "--rule-repair-plan",
                str(paths["plan"]),
                "--repair-prompt-metadata",
                str(paths["prompt_metadata"]),
                "--out",
                str(report_path),
                "--metadata-out",
                str(metadata_path),
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(report_path.exists())
        self.assertTrue(metadata_path.exists())
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertIs(metadata, validate_schema(metadata, "human_review_report_metadata.schema.json"))

    def test_cli_json_outputs_pure_json(self) -> None:
        paths = self._write_artifacts(self._case_dir("cli_json"))

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/build_human_review_report.py",
                "--quality-report",
                str(paths["quality"]),
                "--rule-validation-report",
                str(paths["validation"]),
                "--rule-repair-plan",
                str(paths["plan"]),
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
        self.assertIn("decision", metadata)
        self.assertIn("source_reports", metadata)
        self.assertIn("summary", metadata)

    def test_cli_print_outputs_markdown(self) -> None:
        paths = self._write_artifacts(self._case_dir("cli_print"))

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/build_human_review_report.py",
                "--quality-report",
                str(paths["quality"]),
                "--rule-validation-report",
                str(paths["validation"]),
                "--rule-repair-plan",
                str(paths["plan"]),
                "--print",
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("小说章节审稿报告", result.stdout)
        self.assertIn("总体结论", result.stdout)

    def _write_artifacts(self, case_dir: Path) -> dict[str, Path]:
        artifacts = self._artifacts()
        paths = {
            "quality": case_dir / "chapter_quality_report.json",
            "validation": case_dir / "rule_validation_report.json",
            "plan": case_dir / "rule_repair_plan.json",
            "prompt_metadata": case_dir / "rule_repair_prompt_metadata.json",
        }
        for key, path in paths.items():
            value = artifacts["prompt_metadata"] if key == "prompt_metadata" else artifacts[key]
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return paths

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
