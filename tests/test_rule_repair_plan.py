from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from core.rules import build_rule_repair_plan, validate_chapter_against_rules
from core.schema import validate_schema


FIXTURE_DIR = Path("tests/fixtures/chapter_quality")


class RuleRepairPlanTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_rule_repair_plan" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _snapshot(self) -> dict:
        return json.loads((FIXTURE_DIR / "snapshot.json").read_text(encoding="utf-8"))

    def _chapter(self, name: str) -> str:
        return (FIXTURE_DIR / name).read_text(encoding="utf-8")

    def _validation_report(self, chapter_name: str = "bad_chapter.md") -> dict:
        return validate_chapter_against_rules(
            chapter_text=self._chapter(chapter_name),
            snapshot=self._snapshot(),
            previous_chapter_text=self._chapter("previous_chapter.md"),
            use_default_rules=True,
        )

    def _task(self, plan: dict, rule_code: str) -> dict:
        for task in plan["tasks"]:
            if task["rule_code"] == rule_code:
                return task
        self.fail(f"missing repair task: {rule_code}")

    def test_no_repair_needed_when_report_has_no_violations(self) -> None:
        report = self._validation_report("good_chapter.md")
        report["violations"] = []

        plan = build_rule_repair_plan(rule_validation_report=report)

        self.assertEqual("no_repair_needed", plan["status"])
        self.assertEqual(0, plan["summary"]["task_count"])
        self.assertEqual([], plan["tasks"])

    def test_bad_chapter_creates_repair_tasks(self) -> None:
        plan = build_rule_repair_plan(rule_validation_report=self._validation_report())

        self.assertGreaterEqual(plan["summary"]["task_count"], 1)
        task = self._task(plan, "prose_only_no_meta_output")
        self.assertEqual("remove_meta_output", task["repair_type"])

    def test_blocking_rule_logic(self) -> None:
        blocked = build_rule_repair_plan(rule_validation_report=self._custom_report([
            self._custom_rule("prose_only_no_meta_output", "fail", "critical", "output_contract", ["no_meta_output"]),
        ]))

        self.assertEqual("blocked", blocked["status"])
        self.assertTrue(blocked["tasks"][0]["blocking"])

        warning = build_rule_repair_plan(rule_validation_report=self._custom_report([
            self._custom_rule("avoid_repetition_and_stalling", "warning", "medium", "pacing", ["repetition_or_stalling"]),
        ]))

        self.assertEqual("needs_repair", warning["status"])
        self.assertFalse(warning["tasks"][0]["blocking"])

    def test_fail_only_excludes_warnings(self) -> None:
        report = self._custom_report([
            self._custom_rule("prose_only_no_meta_output", "fail", "critical", "output_contract", ["no_meta_output"]),
            self._custom_rule("avoid_repetition_and_stalling", "warning", "medium", "pacing", ["repetition_or_stalling"]),
        ])

        plan = build_rule_repair_plan(rule_validation_report=report, include_warnings=False)

        self.assertEqual(1, len(plan["tasks"]))
        self.assertEqual("fail", plan["tasks"][0]["rule_status"])

    def test_max_tasks_limits_output(self) -> None:
        report = self._custom_report([
            self._custom_rule("avoid_repetition_and_stalling", "warning", "medium", "pacing", ["repetition_or_stalling"]),
            self._custom_rule("follow_target_language", "fail", "high", "language", ["language_consistency"]),
            self._custom_rule("prose_only_no_meta_output", "fail", "critical", "output_contract", ["no_meta_output"]),
        ])

        plan = build_rule_repair_plan(rule_validation_report=report, max_tasks=1)

        self.assertEqual(1, len(plan["tasks"]))
        self.assertEqual("repair_001", plan["tasks"][0]["task_id"])
        self.assertEqual("prose_only_no_meta_output", plan["tasks"][0]["rule_code"])

    def test_evidence_is_carried_over(self) -> None:
        plan = build_rule_repair_plan(rule_validation_report=self._validation_report())

        task = self._task(plan, "prose_only_no_meta_output")
        self.assertIn("matched_markers", task["evidence"])
        self.assertTrue(task["evidence"]["matched_markers"])

    def test_report_schema_validates(self) -> None:
        plan = build_rule_repair_plan(rule_validation_report=self._validation_report())

        self.assertIs(plan, validate_schema(plan, "rule_repair_plan.schema.json"))

    def test_does_not_modify_input_report(self) -> None:
        report = self._validation_report()
        before = copy.deepcopy(report)

        build_rule_repair_plan(rule_validation_report=report)

        self.assertEqual(before, report)

    def test_cli_can_write_repair_plan(self) -> None:
        case_dir = self._case_dir("cli")
        report_path = case_dir / "rule_validation_report.json"
        plan_path = case_dir / "rule_repair_plan.json"
        report_path.write_text(json.dumps(self._validation_report(), ensure_ascii=False, indent=2), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/build_rule_repair_plan.py",
                "--rule-validation-report",
                str(report_path),
                "--out",
                str(plan_path),
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(plan_path.exists())
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertIs(plan, validate_schema(plan, "rule_repair_plan.schema.json"))

    def test_cli_json_outputs_pure_json(self) -> None:
        case_dir = self._case_dir("cli_json")
        report_path = case_dir / "rule_validation_report.json"
        report_path.write_text(json.dumps(self._validation_report(), ensure_ascii=False, indent=2), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/build_rule_repair_plan.py",
                "--rule-validation-report",
                str(report_path),
                "--json",
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        plan = json.loads(result.stdout)
        self.assertIn("status", plan)
        self.assertIn("tasks", plan)
        self.assertIn("summary", plan)

    def test_cli_fail_only_works(self) -> None:
        case_dir = self._case_dir("cli_fail_only")
        report_path = case_dir / "rule_validation_report.json"
        plan_path = case_dir / "fail_only_plan.json"
        report = self._custom_report([
            self._custom_rule("prose_only_no_meta_output", "fail", "critical", "output_contract", ["no_meta_output"]),
            self._custom_rule("avoid_repetition_and_stalling", "warning", "medium", "pacing", ["repetition_or_stalling"]),
        ])
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/build_rule_repair_plan.py",
                "--rule-validation-report",
                str(report_path),
                "--fail-only",
                "--out",
                str(plan_path),
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertTrue(all(task["rule_status"] == "fail" for task in plan["tasks"]))

    def _custom_report(self, rules: list[dict]) -> dict:
        summary = {
            "passed": 0,
            "warnings": sum(1 for rule in rules if rule["status"] == "warning"),
            "failed": sum(1 for rule in rules if rule["status"] == "fail"),
            "skipped": 0,
        }
        report = {
            "schema_version": "1.0",
            "status": "fail" if summary["failed"] else "warning" if summary["warnings"] else "pass",
            "score": 50 if summary["failed"] else 88 if summary["warnings"] else 100,
            "summary": summary,
            "rule_pack": {
                "rule_pack_id": "test_rules",
                "version": "1.0.0",
                "language": "zh-CN",
            },
            "quality_report": {
                "status": "fail" if summary["failed"] else "warning" if summary["warnings"] else "pass",
                "score": 50 if summary["failed"] else 88 if summary["warnings"] else 100,
                "check_count": len(rules),
            },
            "rules": rules,
            "violations": [
                {
                    "rule_code": rule["code"],
                    "status": rule["status"],
                    "severity": rule["severity"],
                    "category": rule["category"],
                    "message": f"Rule {rule['status']}",
                    "quality_check_codes": rule["quality_check_codes"],
                }
                for rule in rules
                if rule["status"] in {"fail", "warning"}
            ],
            "metadata": {
                "created_by": "NovelAgent",
                "source": "rule-aware-validation",
                "ready_for_next_flow": summary["failed"] == 0,
            },
        }
        return validate_schema(report, "rule_validation_report.schema.json")

    def _custom_rule(
        self,
        code: str,
        status: str,
        severity: str,
        category: str,
        quality_check_codes: list[str],
    ) -> dict:
        return {
            "code": code,
            "title": code.replace("_", " ").title(),
            "category": category,
            "severity": severity,
            "status": status,
            "quality_check_codes": quality_check_codes,
            "matched_quality_checks": [
                {
                    "code": quality_check_codes[0],
                    "status": status,
                    "severity": severity,
                    "message": "Synthetic check evidence.",
                    "evidence": {"marker": code},
                }
            ] if quality_check_codes else [],
            "reason": None,
        }


if __name__ == "__main__":
    unittest.main()
