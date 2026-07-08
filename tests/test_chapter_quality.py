from __future__ import annotations

import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from core.quality import evaluate_chapter_quality
from core.schema import validate_schema


FIXTURE_DIR = Path("tests/fixtures/chapter_quality")


class ChapterQualityTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = Path.cwd() / ".tmp" / "test_chapter_quality" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True)
        return case_dir

    def _snapshot(self) -> dict:
        return json.loads((FIXTURE_DIR / "snapshot.json").read_text(encoding="utf-8"))

    def _chapter(self, name: str) -> str:
        return (FIXTURE_DIR / name).read_text(encoding="utf-8")

    def _check(self, report: dict, code: str) -> dict:
        for check in report["checks"]:
            if check["code"] == code:
                return check
        self.fail(f"missing check: {code}")

    def test_good_chapter_report(self) -> None:
        report = evaluate_chapter_quality(
            chapter_text=self._chapter("good_chapter.md"),
            snapshot=self._snapshot(),
            previous_chapter_text=self._chapter("previous_chapter.md"),
        )

        self.assertIn(report["status"], {"pass", "warning"})
        self.assertGreaterEqual(report["score"], 75)
        self.assertNotEqual("fail", self._check(report, "continues_previous_ending")["status"])
        self.assertNotEqual("fail", self._check(report, "preserves_last_scene_location")["status"])
        self.assertEqual("pass", self._check(report, "no_meta_output")["status"])
        self.assertEqual("pass", self._check(report, "language_consistency")["status"])

    def test_bad_chapter_report(self) -> None:
        snapshot = self._snapshot()
        good = evaluate_chapter_quality(
            chapter_text=self._chapter("good_chapter.md"),
            snapshot=snapshot,
            previous_chapter_text=self._chapter("previous_chapter.md"),
        )
        bad = evaluate_chapter_quality(
            chapter_text=self._chapter("bad_chapter.md"),
            snapshot=snapshot,
            previous_chapter_text=self._chapter("previous_chapter.md"),
        )

        self.assertIn(bad["status"], {"warning", "fail"})
        self.assertLess(bad["score"], good["score"])
        self.assertGreaterEqual(bad["summary"]["failed"], 1)
        self.assertIn(self._check(bad, "no_meta_output")["status"], {"fail", "warning"})

    def test_report_schema_validates(self) -> None:
        report = evaluate_chapter_quality(
            chapter_text=self._chapter("good_chapter.md"),
            snapshot=self._snapshot(),
            previous_chapter_text=self._chapter("previous_chapter.md"),
        )

        self.assertIs(report, validate_schema(report, "chapter_quality_report.schema.json"))

    def test_snapshot_compatibility_passes_for_valid_snapshot(self) -> None:
        report = evaluate_chapter_quality(
            chapter_text=self._chapter("good_chapter.md"),
            snapshot=self._snapshot(),
            previous_chapter_text=self._chapter("previous_chapter.md"),
        )

        self.assertEqual("pass", self._check(report, "snapshot_compatibility")["status"])

    def test_repetition_check_warns_on_repeated_paragraphs(self) -> None:
        repeated = "备用通道里，林雪停下脚步。\n\n" * 4 + "林雪看向陈岚，主控室阀门仍在震动。"

        report = evaluate_chapter_quality(
            chapter_text=repeated,
            snapshot=self._snapshot(),
            previous_chapter_text=self._chapter("previous_chapter.md"),
        )

        self.assertIn(self._check(report, "repetition_or_stalling")["status"], {"warning", "fail"})

    def test_language_check_warns_or_fails_for_english_text_with_zh_cn_target(self) -> None:
        english_text = "The corridor is silent. The hero explains the plan and walks away." * 20

        report = evaluate_chapter_quality(
            chapter_text=english_text,
            snapshot=self._snapshot(),
            language="zh-CN",
        )

        self.assertIn(self._check(report, "language_consistency")["status"], {"warning", "fail"})

    def test_cli_can_run_and_write_report(self) -> None:
        output_path = self._case_dir("cli") / "report.json"

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/evaluate_chapter_quality.py",
                "--chapter",
                str(FIXTURE_DIR / "good_chapter.md"),
                "--snapshot",
                str(FIXTURE_DIR / "snapshot.json"),
                "--previous",
                str(FIXTURE_DIR / "previous_chapter.md"),
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
        self.assertIs(report, validate_schema(report, "chapter_quality_report.schema.json"))

    def test_cli_json_outputs_pure_json(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/evaluate_chapter_quality.py",
                "--chapter",
                str(FIXTURE_DIR / "good_chapter.md"),
                "--snapshot",
                str(FIXTURE_DIR / "snapshot.json"),
                "--previous",
                str(FIXTURE_DIR / "previous_chapter.md"),
                "--json",
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        self.assertIn("status", report)


if __name__ == "__main__":
    unittest.main()
