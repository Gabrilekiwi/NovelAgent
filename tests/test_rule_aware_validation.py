from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from core.quality import evaluate_chapter_quality
from core.rules import (
    RuleValidationError,
    load_default_narrative_rule_pack,
    validate_chapter_against_rules,
)
from core.schema import validate_schema


FIXTURE_DIR = Path("tests/fixtures/chapter_quality")


class RuleAwareValidationTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_rule_aware_validation" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _snapshot(self) -> dict:
        return json.loads((FIXTURE_DIR / "snapshot.json").read_text(encoding="utf-8"))

    def _chapter(self, name: str) -> str:
        return (FIXTURE_DIR / name).read_text(encoding="utf-8")

    def _validate(self, chapter_name: str = "good_chapter.md", **kwargs: object) -> dict:
        return validate_chapter_against_rules(
            chapter_text=self._chapter(chapter_name),
            snapshot=self._snapshot(),
            previous_chapter_text=self._chapter("previous_chapter.md"),
            use_default_rules=True,
            **kwargs,
        )

    def _rule(self, report: dict, code: str) -> dict:
        for rule in report["rules"]:
            if rule["code"] == code:
                return rule
        self.fail(f"missing rule result: {code}")

    def test_good_chapter_rule_validation(self) -> None:
        report = self._validate()

        self.assertIn(report["status"], {"pass", "warning"})
        self.assertGreaterEqual(report["score"], 75)
        self.assertNotEqual("fail", self._rule(report, "continue_previous_ending")["status"])
        self.assertEqual("pass", self._rule(report, "prose_only_no_meta_output")["status"])
        self.assertEqual("pass", self._rule(report, "follow_target_language")["status"])
        self.assertLessEqual(len(report["violations"]), 3)

    def test_bad_chapter_rule_validation(self) -> None:
        good = self._validate()
        bad = self._validate("bad_chapter.md")

        self.assertIn(bad["status"], {"warning", "fail"})
        self.assertLess(bad["score"], good["score"])
        self.assertGreaterEqual(len(bad["violations"]), 1)
        self.assertIn(self._rule(bad, "prose_only_no_meta_output")["status"], {"fail", "warning"})
        self.assertIn(self._rule(bad, "follow_target_language")["status"], {"fail", "warning"})

    def test_report_schema_validates(self) -> None:
        report = self._validate()

        self.assertIs(report, validate_schema(report, "rule_validation_report.schema.json"))

    def test_quality_report_can_be_reused(self) -> None:
        quality_report = evaluate_chapter_quality(
            chapter_text=self._chapter("good_chapter.md"),
            snapshot=self._snapshot(),
            previous_chapter_text=self._chapter("previous_chapter.md"),
        )

        report = validate_chapter_against_rules(
            chapter_text=self._chapter("good_chapter.md"),
            snapshot=self._snapshot(),
            previous_chapter_text=self._chapter("previous_chapter.md"),
            use_default_rules=True,
            quality_report=quality_report,
        )

        self.assertEqual(quality_report["score"], report["quality_report"]["score"])
        self.assertEqual(len(quality_report["checks"]), report["quality_report"]["check_count"])

    def test_unmapped_rules_are_skipped(self) -> None:
        report = self._validate()

        for code in ("preserve_character_state", "preserve_world_rules", "no_unseeded_major_reveal"):
            rule = self._rule(report, code)
            self.assertEqual("skip", rule["status"])
            self.assertEqual("no_quality_check_mapping", rule["reason"])

    def test_min_severity_filter_works(self) -> None:
        report = self._validate(min_severity="critical")

        self.assertGreater(len(report["rules"]), 0)
        self.assertEqual({"critical"}, {rule["severity"] for rule in report["rules"]})

    def test_category_filter_works(self) -> None:
        report = self._validate(categories=["output_contract"])

        self.assertGreater(len(report["rules"]), 0)
        self.assertEqual({"output_contract"}, {rule["category"] for rule in report["rules"]})
        self.assertEqual(["prose_only_no_meta_output"], [rule["code"] for rule in report["rules"]])

    def test_missing_rule_pack_errors(self) -> None:
        with self.assertRaisesRegex(RuleValidationError, "rule pack is required"):
            validate_chapter_against_rules(
                chapter_text=self._chapter("good_chapter.md"),
                snapshot=self._snapshot(),
                previous_chapter_text=self._chapter("previous_chapter.md"),
            )

    def test_does_not_modify_snapshot_or_rule_pack(self) -> None:
        snapshot = self._snapshot()
        rule_pack = load_default_narrative_rule_pack()
        snapshot_before = copy.deepcopy(snapshot)
        rule_pack_before = copy.deepcopy(rule_pack)

        validate_chapter_against_rules(
            chapter_text=self._chapter("good_chapter.md"),
            snapshot=snapshot,
            previous_chapter_text=self._chapter("previous_chapter.md"),
            rule_pack=rule_pack,
        )

        self.assertEqual(snapshot_before, snapshot)
        self.assertEqual(rule_pack_before, rule_pack)

    def test_custom_rule_pack_path_can_be_used(self) -> None:
        case_dir = self._case_dir("custom_rules")
        rule_pack_path = case_dir / "rules.json"
        rule_pack_path.write_text(
            json.dumps(load_default_narrative_rule_pack(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        report = validate_chapter_against_rules(
            chapter_text=self._chapter("good_chapter.md"),
            snapshot=self._snapshot(),
            previous_chapter_text=self._chapter("previous_chapter.md"),
            rule_pack_path=rule_pack_path,
        )

        self.assertEqual("default_narrative_rules", report["rule_pack"]["rule_pack_id"])

    def test_cli_can_write_report(self) -> None:
        output_path = self._case_dir("cli") / "report.json"

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/validate_chapter_rules.py",
                "--chapter",
                str(FIXTURE_DIR / "good_chapter.md"),
                "--snapshot",
                str(FIXTURE_DIR / "snapshot.json"),
                "--previous",
                str(FIXTURE_DIR / "previous_chapter.md"),
                "--default-rules",
                "--out",
                str(output_path),
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(output_path.exists())
        report = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertIs(report, validate_schema(report, "rule_validation_report.schema.json"))

    def test_cli_json_outputs_pure_json(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/validate_chapter_rules.py",
                "--chapter",
                str(FIXTURE_DIR / "good_chapter.md"),
                "--snapshot",
                str(FIXTURE_DIR / "snapshot.json"),
                "--previous",
                str(FIXTURE_DIR / "previous_chapter.md"),
                "--default-rules",
                "--json",
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        self.assertIn("status", report)
        self.assertIn("rules", report)
        self.assertIn("violations", report)

    def test_cli_can_reuse_quality_report(self) -> None:
        case_dir = self._case_dir("quality_report")
        quality_path = case_dir / "chapter_quality_report.json"
        output_path = case_dir / "rule_validation_report.json"
        quality_report = evaluate_chapter_quality(
            chapter_text=self._chapter("good_chapter.md"),
            snapshot=self._snapshot(),
            previous_chapter_text=self._chapter("previous_chapter.md"),
        )
        quality_path.write_text(json.dumps(quality_report, ensure_ascii=False, indent=2), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/validate_chapter_rules.py",
                "--chapter",
                str(FIXTURE_DIR / "good_chapter.md"),
                "--snapshot",
                str(FIXTURE_DIR / "snapshot.json"),
                "--previous",
                str(FIXTURE_DIR / "previous_chapter.md"),
                "--default-rules",
                "--quality-report",
                str(quality_path),
                "--out",
                str(output_path),
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(quality_report["score"], report["quality_report"]["score"])


if __name__ == "__main__":
    unittest.main()
