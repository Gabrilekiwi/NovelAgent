from __future__ import annotations

import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from core.engine.executor import AgentExecutor
from core.review.runtime import RuntimeReviewConfig
from core.schema import validate_schema


ROOT = Path(__file__).resolve().parents[1]


class RuntimeReviewIntegrationTests(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = ROOT / ".tmp" / "test_runtime_review_integration" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True, exist_ok=True)
        return case_dir

    def _write_snapshot(self, case_dir: Path) -> Path:
        path = case_dir / "snapshot.json"
        path.write_text(
            json.dumps(
                {
                    "chapter_index": 2,
                    "project_profile": {"language": ""},
                    "world_state": {"locations": {"shelter": {}, "sealed gate": {}}},
                    "characters": {},
                    "timeline": [],
                    "story_state": {
                        "last_chapter_ending": "The team waited in the shelter.",
                        "last_scene_location": "shelter",
                        "last_scene_characters": [],
                        "open_threads": ["protect the serum sample"],
                        "required_opening_bridge": "shelter alarm serum",
                    },
                    "spatial_state": {
                        "spaces": {"shelter": {}, "sealed gate": {}},
                        "connections": [{"from": "shelter", "to": "sealed gate"}],
                        "character_positions": {},
                        "blocked_paths": [],
                        "last_transition": {},
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_default_runtime_does_not_run_review(self) -> None:
        case_dir = self._case_dir("default_off")
        snapshot_path = self._write_snapshot(case_dir)
        reviews_dir = case_dir / "reviews"

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=case_dir / "missing_memory.json",
            run_dir=case_dir / "runs",
            chapter_dir=case_dir / "chapters",
            dry_run=True,
        ).run_once(persist=True)

        self.assertNotIn("review_pipeline", result)
        self.assertNotIn("review_pipeline", result["run"])
        self.assertFalse(reviews_dir.exists())

    def test_enable_review_pipeline_creates_artifacts_and_run_record_summary(self) -> None:
        case_dir = self._case_dir("enabled")
        snapshot_path = self._write_snapshot(case_dir)
        reviews_dir = case_dir / "reviews"

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=case_dir / "missing_memory.json",
            run_dir=case_dir / "runs",
            chapter_dir=case_dir / "chapters",
            dry_run=True,
            review_config=RuntimeReviewConfig(enabled=True, output_dir=reviews_dir),
        ).run_once(persist=True)

        review = result["run"]["review_pipeline"]
        self.assertTrue(review["enabled"])
        self.assertIn("status", review)
        self.assertIn("decision", review)
        self.assertIn("quality_score", review)
        self.assertIn("rule_score", review)
        self.assertIn("repair_task_count", review)
        self.assertIn("blocking_task_count", review)
        self.assertTrue(Path(review["artifacts_dir"]).exists())
        self.assertTrue(Path(review["summary_path"]).exists())
        self.assertTrue((Path(review["artifacts_dir"]) / "human_review_report.md").exists())
        validate_schema(json.loads(Path(review["summary_path"]).read_text(encoding="utf-8")), "review_pipeline_summary.schema.json")

        run_files = list((case_dir / "runs").glob("chapter_2_*.json"))
        self.assertEqual(1, len(run_files))
        saved = json.loads(run_files[0].read_text(encoding="utf-8"))
        self.assertEqual(review, saved["run"]["review_pipeline"])

    def test_no_repair_prompt_option(self) -> None:
        case_dir = self._case_dir("no_prompt")
        snapshot_path = self._write_snapshot(case_dir)
        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=case_dir / "missing_memory.json",
            run_dir=case_dir / "runs",
            chapter_dir=case_dir / "chapters",
            dry_run=True,
            review_config=RuntimeReviewConfig(
                enabled=True,
                output_dir=case_dir / "reviews",
                build_repair_prompt=False,
            ),
        ).run_once(persist=True)

        review = result["run"]["review_pipeline"]
        summary = json.loads(Path(review["summary_path"]).read_text(encoding="utf-8"))
        self.assertFalse(summary["flags"]["has_repair_prompt"])
        self.assertFalse((Path(review["artifacts_dir"]) / "rule_repair_prompt.md").exists())
        self.assertTrue((Path(review["artifacts_dir"]) / "human_review_report.md").exists())

    def test_no_human_report_option(self) -> None:
        case_dir = self._case_dir("no_human")
        snapshot_path = self._write_snapshot(case_dir)
        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=case_dir / "missing_memory.json",
            run_dir=case_dir / "runs",
            chapter_dir=case_dir / "chapters",
            dry_run=True,
            review_config=RuntimeReviewConfig(
                enabled=True,
                output_dir=case_dir / "reviews",
                build_human_report=False,
            ),
        ).run_once(persist=True)

        review = result["run"]["review_pipeline"]
        summary = json.loads(Path(review["summary_path"]).read_text(encoding="utf-8"))
        self.assertFalse(summary["flags"]["has_human_review_report"])
        self.assertEqual("unknown", summary["reports"]["human_review_decision"])
        self.assertFalse((Path(review["artifacts_dir"]) / "human_review_report.md").exists())

    def test_custom_rules_path_works(self) -> None:
        case_dir = self._case_dir("custom_rules")
        snapshot_path = self._write_snapshot(case_dir)
        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=case_dir / "missing_memory.json",
            run_dir=case_dir / "runs",
            chapter_dir=case_dir / "chapters",
            dry_run=True,
            review_config=RuntimeReviewConfig(
                enabled=True,
                output_dir=case_dir / "reviews",
                rules_path=ROOT / "rules" / "default_narrative_rule_pack.json",
                use_default_rules=False,
            ),
        ).run_once(persist=True)

        summary = json.loads(Path(result["run"]["review_pipeline"]["summary_path"]).read_text(encoding="utf-8"))
        self.assertFalse(summary["flags"]["used_default_rules"])

    def test_review_failure_does_not_delete_generated_chapter(self) -> None:
        case_dir = self._case_dir("review_failure")
        snapshot_path = self._write_snapshot(case_dir)
        bad_rules = case_dir / "bad_rules.json"
        bad_rules.write_text('{"not": "a rule pack"}', encoding="utf-8")

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=case_dir / "missing_memory.json",
            run_dir=case_dir / "runs",
            chapter_dir=case_dir / "chapters",
            dry_run=True,
            review_config=RuntimeReviewConfig(
                enabled=True,
                output_dir=case_dir / "reviews",
                rules_path=bad_rules,
                use_default_rules=False,
            ),
        ).run_once(persist=True)

        review = result["run"]["review_pipeline"]
        self.assertEqual("error", review["status"])
        self.assertTrue(review["error"])
        chapter_artifact = Path(result["run"]["chapter"]["artifact"]["path"])
        self.assertTrue(chapter_artifact.exists())

    def test_cli_output_run_json_includes_review_pipeline(self) -> None:
        case_dir = self._case_dir("cli")
        snapshot_path = self._write_snapshot(case_dir)
        result = subprocess.run(
            [
                sys.executable,
                "main.py",
                "--dry-run",
                "--persist-dry-run",
                "--enable-review-pipeline",
                "--review-output-dir",
                str(case_dir / "reviews"),
                "--memory",
                "data/notion_memory.example.json",
                "--snapshot",
                str(snapshot_path),
                "--run-dir",
                str(case_dir / "runs"),
                "--chapter-dir",
                str(case_dir / "chapters"),
                "--output-run-json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        run = json.loads(result.stdout)
        self.assertTrue(run["review_pipeline"]["enabled"])
        self.assertTrue(run["review_pipeline"]["summary_path"])

    def test_cli_no_default_rules_without_custom_rules_fails(self) -> None:
        case_dir = self._case_dir("cli_no_rules")
        snapshot_path = self._write_snapshot(case_dir)
        result = subprocess.run(
            [
                sys.executable,
                "main.py",
                "--dry-run",
                "--enable-review-pipeline",
                "--review-no-default-rules",
                "--memory",
                "data/notion_memory.example.json",
                "--snapshot",
                str(snapshot_path),
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
