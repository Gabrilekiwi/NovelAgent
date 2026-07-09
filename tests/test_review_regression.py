from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from core.review import evaluate_regression_expectations, run_review_regression_suite
from core.schema import validate_schema


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "fixtures" / "review_regression" / "manifest.json"
TMP_ROOT = ROOT / ".tmp" / "unit_review_regression"


class ReviewRegressionTests(unittest.TestCase):
    def test_manifest_schema_validates(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        validate_schema(manifest, "review_regression_manifest.schema.json")
        self.assertGreaterEqual(len(manifest["cases"]), 6)

    def test_all_fixture_files_exist(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for case in manifest["cases"]:
            for key in ("snapshot_path", "previous_chapter_path", "chapter_path", "expected_path"):
                self.assertTrue((MANIFEST.parent / case[key]).exists(), f"{case['case_id']} missing {key}")

    def test_suite_summary_schema_validates_and_passes(self) -> None:
        summary = run_review_regression_suite(manifest_path=MANIFEST)
        validate_schema(summary, "review_regression_summary.schema.json")
        self.assertEqual("pass", summary["status"])
        self.assertGreaterEqual(summary["summary"]["case_count"], 6)
        self.assertEqual(0, summary["summary"]["failed"])

    def test_meta_bad_catches_meta_rule_and_artifact_plan(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        artifacts_dir = TMP_ROOT / "meta_artifacts"
        summary = run_review_regression_suite(manifest_path=MANIFEST, artifacts_dir=artifacts_dir)
        meta_case = next(case for case in summary["cases"] if case["case_id"] == "metro_meta_output_bad")
        self.assertEqual("pass", meta_case["status"])
        self.assertEqual("blocked", meta_case["decision"])

        plan_path = artifacts_dir / "metro_meta_output_bad" / "rule_repair_plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertIn("remove_meta_output", {task["repair_type"] for task in plan["tasks"]})

    def test_good_continuity_is_not_blocked(self) -> None:
        summary = run_review_regression_suite(manifest_path=MANIFEST)
        good_case = next(case for case in summary["cases"] if case["case_id"] == "metro_continuity_good")
        self.assertNotEqual("blocked", good_case["decision"])
        self.assertEqual(0, good_case["blocking_task_count"])

    def test_expectation_failure_reports_case_id(self) -> None:
        failures = evaluate_regression_expectations(
            case_id="forced_failure",
            expected={"expected_decision": "blocked"},
            quality_report={"score": 100},
            rule_validation_report={"score": 100, "rules": []},
            rule_repair_plan={"tasks": []},
            human_review_metadata={"decision": {"decision": "accept"}},
        )
        self.assertTrue(failures)
        self.assertIn("forced_failure", failures[0])

    def test_cli_writes_summary_and_artifacts(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        summary_path = TMP_ROOT / "cli_summary.json"
        artifacts_dir = TMP_ROOT / "cli_artifacts"
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts" / "run_review_regression.py"),
                "--manifest",
                str(MANIFEST),
                "--out",
                str(summary_path),
                "--artifacts-dir",
                str(artifacts_dir),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(summary_path.exists())
        self.assertTrue((artifacts_dir / "metro_meta_output_bad" / "human_review_report.md").exists())

    def test_cli_json_is_pure_json(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts" / "run_review_regression.py"),
                "--manifest",
                str(MANIFEST),
                "--json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual("pass", summary["status"])
        self.assertEqual("", result.stderr)


if __name__ == "__main__":
    unittest.main()
