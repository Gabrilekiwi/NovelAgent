from __future__ import annotations

import copy
import json
import subprocess
import sys
import uuid
import unittest
from pathlib import Path

from core.review import run_review_pipeline
from core.rules import load_default_narrative_rule_pack
from core.schema import validate_schema


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "review_regression" / "cases"
TMP_ROOT = ROOT / ".tmp" / "test_review_pipeline"


def _case_paths(case_id: str) -> dict[str, Path]:
    case_dir = FIXTURES / case_id
    return {
        "chapter": case_dir / "chapter.md",
        "snapshot": case_dir / "snapshot.json",
        "previous": case_dir / "previous_chapter.md",
    }


def _load_case(case_id: str) -> tuple[str, dict, str]:
    paths = _case_paths(case_id)
    return (
        paths["chapter"].read_text(encoding="utf-8"),
        json.loads(paths["snapshot"].read_text(encoding="utf-8")),
        paths["previous"].read_text(encoding="utf-8"),
    )


def _tmp_dir(name: str) -> Path:
    path = TMP_ROOT / f"{name}_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


class ReviewPipelineTests(unittest.TestCase):
    def test_pipeline_on_bad_fixture_blocks(self) -> None:
        chapter_text, snapshot, previous = _load_case("metro_meta_output_bad")
        summary = run_review_pipeline(
            chapter_text=chapter_text,
            snapshot=snapshot,
            previous_chapter_text=previous,
        )

        validate_schema(summary, "review_pipeline_summary.schema.json")
        self.assertEqual("blocked", summary["status"])
        self.assertEqual("blocked", summary["decision"]["decision"])
        self.assertLessEqual(summary["scores"]["quality_score"], 80)
        self.assertLessEqual(summary["scores"]["rule_score"], 80)
        self.assertGreaterEqual(summary["tasks"]["blocking_task_count"], 1)
        self.assertTrue(summary["flags"]["has_repair_prompt"])
        self.assertTrue(summary["flags"]["has_human_review_report"])

    def test_pipeline_on_good_fixture_is_not_blocked(self) -> None:
        chapter_text, snapshot, previous = _load_case("metro_continuity_good")
        summary = run_review_pipeline(
            chapter_text=chapter_text,
            snapshot=snapshot,
            previous_chapter_text=previous,
        )

        validate_schema(summary, "review_pipeline_summary.schema.json")
        self.assertIn(summary["status"], {"pass", "warning", "needs_revision"})
        self.assertNotEqual("blocked", summary["status"])
        self.assertEqual(0, summary["tasks"]["blocking_task_count"])

    def test_output_artifacts_are_written(self) -> None:
        chapter_text, snapshot, previous = _load_case("metro_meta_output_bad")
        output_dir = _tmp_dir("artifacts")
        summary = run_review_pipeline(
            chapter_text=chapter_text,
            snapshot=snapshot,
            previous_chapter_text=previous,
            output_dir=output_dir,
        )

        for filename in (
            "chapter_quality_report.json",
            "rule_validation_report.json",
            "rule_repair_plan.json",
            "rule_repair_prompt.md",
            "rule_repair_prompt_metadata.json",
            "human_review_report.md",
            "human_review_report_metadata.json",
            "review_pipeline_summary.json",
        ):
            self.assertTrue((output_dir / filename).exists(), filename)
        saved_summary = json.loads((output_dir / "review_pipeline_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], saved_summary["status"])

    def test_no_repair_prompt_mode(self) -> None:
        chapter_text, snapshot, previous = _load_case("metro_meta_output_bad")
        output_dir = _tmp_dir("no_prompt")
        summary = run_review_pipeline(
            chapter_text=chapter_text,
            snapshot=snapshot,
            previous_chapter_text=previous,
            output_dir=output_dir,
            build_repair_prompt=False,
        )

        validate_schema(summary, "review_pipeline_summary.schema.json")
        self.assertFalse(summary["flags"]["has_repair_prompt"])
        self.assertTrue(summary["flags"]["has_human_review_report"])
        self.assertIsNone(summary["artifacts"]["rule_repair_prompt"])
        self.assertIsNone(summary["artifacts"]["rule_repair_prompt_metadata"])
        self.assertFalse((output_dir / "rule_repair_prompt.md").exists())
        metadata = json.loads((output_dir / "human_review_report_metadata.json").read_text(encoding="utf-8"))
        self.assertFalse(metadata["source_reports"]["has_repair_prompt_metadata"])

    def test_no_human_report_mode(self) -> None:
        chapter_text, snapshot, previous = _load_case("metro_meta_output_bad")
        output_dir = _tmp_dir("no_human")
        summary = run_review_pipeline(
            chapter_text=chapter_text,
            snapshot=snapshot,
            previous_chapter_text=previous,
            output_dir=output_dir,
            build_human_report=False,
        )

        validate_schema(summary, "review_pipeline_summary.schema.json")
        self.assertFalse(summary["flags"]["has_human_review_report"])
        self.assertEqual("unknown", summary["decision"]["decision"])
        self.assertEqual("unknown", summary["reports"]["human_review_decision"])
        self.assertEqual("blocked", summary["status"])
        self.assertIsNone(summary["artifacts"]["human_review_report"])
        self.assertIsNone(summary["artifacts"]["human_review_report_metadata"])
        self.assertFalse((output_dir / "human_review_report.md").exists())

    def test_pipeline_does_not_modify_inputs(self) -> None:
        chapter_text, snapshot, previous = _load_case("metro_meta_output_bad")
        rule_pack = load_default_narrative_rule_pack()
        original_chapter = str(chapter_text)
        original_snapshot = copy.deepcopy(snapshot)
        original_rule_pack = copy.deepcopy(rule_pack)

        run_review_pipeline(
            chapter_text=chapter_text,
            snapshot=snapshot,
            previous_chapter_text=previous,
            rule_pack=rule_pack,
            use_default_rules=False,
        )

        self.assertEqual(original_chapter, chapter_text)
        self.assertEqual(original_snapshot, snapshot)
        self.assertEqual(original_rule_pack, rule_pack)

    def test_cli_writes_artifacts(self) -> None:
        paths = _case_paths("metro_meta_output_bad")
        output_dir = _tmp_dir("cli")
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts" / "run_review_pipeline.py"),
                "--chapter",
                str(paths["chapter"]),
                "--snapshot",
                str(paths["snapshot"]),
                "--previous",
                str(paths["previous"]),
                "--out-dir",
                str(output_dir),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        summary_path = output_dir / "review_pipeline_summary.json"
        self.assertTrue(summary_path.exists())
        self.assertTrue((output_dir / "human_review_report.md").exists())
        validate_schema(json.loads(summary_path.read_text(encoding="utf-8")), "review_pipeline_summary.schema.json")

    def test_cli_json_outputs_pure_json(self) -> None:
        paths = _case_paths("metro_meta_output_bad")
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts" / "run_review_pipeline.py"),
                "--chapter",
                str(paths["chapter"]),
                "--snapshot",
                str(paths["snapshot"]),
                "--previous",
                str(paths["previous"]),
                "--json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        summary = json.loads(result.stdout)
        for key in ("schema_version", "status", "decision", "scores", "reports", "tasks"):
            self.assertIn(key, summary)
        self.assertEqual("", result.stderr)

    def test_cli_no_default_rules_without_custom_rules_fails(self) -> None:
        paths = _case_paths("metro_meta_output_bad")
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts" / "run_review_pipeline.py"),
                "--chapter",
                str(paths["chapter"]),
                "--snapshot",
                str(paths["snapshot"]),
                "--no-default-rules",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertTrue("rules" in result.stderr or "default" in result.stderr)


if __name__ == "__main__":
    unittest.main()
