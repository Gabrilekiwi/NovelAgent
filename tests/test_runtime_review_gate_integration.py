from __future__ import annotations

import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from core.engine.executor import AgentExecutor
from core.quality_decision import build_quality_decision
from core.review.runtime import RuntimeReviewConfig
from core.schema import validate_schema


ROOT = Path(__file__).resolve().parents[1]
REGRESSION_CASE = ROOT / "tests" / "fixtures" / "review_regression" / "cases" / "metro_meta_output_bad"


class RuntimeReviewGateIntegrationTests(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        case_dir = ROOT / ".tmp" / "test_runtime_review_gate" / f"{name}_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True, exist_ok=True)
        return case_dir

    def _write_snapshot(self, case_dir: Path) -> Path:
        snapshot = json.loads((REGRESSION_CASE / "snapshot.json").read_text(encoding="utf-8"))
        snapshot.setdefault("project_profile", {})["language"] = ""
        path = case_dir / "snapshot.json"
        path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        return path

    def test_default_runtime_no_gate(self) -> None:
        case_dir = self._case_dir("default")
        snapshot_path = self._write_snapshot(case_dir)

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=case_dir / "missing_memory.json",
            run_dir=case_dir / "runs",
            chapter_dir=case_dir / "chapters",
            dry_run=True,
        ).run_once(persist=True)

        self.assertNotIn("review_pipeline", result["run"])
        self.assertNotIn("review_gate", result["run"])

    def test_review_enabled_but_gate_off_does_not_record_gate(self) -> None:
        case_dir = self._case_dir("gate_off")
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
                gate_threshold="off",
            ),
        ).run_once(persist=True)

        self.assertTrue(result["run"]["review_pipeline"]["enabled"])
        self.assertNotIn("review_gate", result["run"])

    def test_gate_blocked_rejects_before_state_commit(self) -> None:
        case_dir = self._case_dir("blocked")
        snapshot_path = self._write_snapshot(case_dir)
        chapter = (REGRESSION_CASE / "chapter.md").read_text(encoding="utf-8")

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=case_dir / "missing_memory.json",
            run_dir=case_dir / "runs",
            chapter_dir=case_dir / "chapters",
            dry_run=True,
            director=_director,
            generator=lambda input_pack: chapter,
            polisher=lambda text: text,
            validator=_passing_validator,
            analyzer=_analysis,
            review_config=RuntimeReviewConfig(
                enabled=True,
                output_dir=case_dir / "reviews",
                gate_threshold="blocked",
            ),
        ).run_once(persist=True)

        self.assertEqual("blocked", result["run"]["review_pipeline"]["status"])
        self.assertEqual("fail", result["run"]["review_gate"]["status"])
        self.assertEqual(1, result["run"]["review_gate"]["exit_code"])
        self.assertTrue(result["run"]["review_gate"]["matched"])
        self.assertFalse(result["run"]["accepted"])
        self.assertFalse(result["run"]["committed"])
        self.assertFalse(result["accepted"])
        self.assertFalse(result["committed"])
        self.assertEqual("rejected", result["run"]["status"])
        validate_schema(result["run"]["review_gate"], "review_gate_result.schema.json")

        saved = json.loads(next((case_dir / "runs").glob("chapter_34_*.json")).read_text(encoding="utf-8"))
        self.assertEqual(result["run"]["review_gate"], saved["run"]["review_gate"])

    def test_gate_failure_cannot_be_overridden_by_accepted_quality_decision(self) -> None:
        case_dir = self._case_dir("independent_gate")
        snapshot_path = self._write_snapshot(case_dir)
        chapter = (REGRESSION_CASE / "chapter.md").read_text(encoding="utf-8")
        executor = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=case_dir / "missing_memory.json",
            run_dir=case_dir / "runs",
            chapter_dir=case_dir / "chapters",
            dry_run=True,
            director=_director,
            generator=lambda input_pack: chapter,
            polisher=lambda text: text,
            validator=_passing_validator,
            analyzer=_analysis,
            review_config=RuntimeReviewConfig(
                enabled=True,
                output_dir=case_dir / "reviews",
                gate_threshold="blocked",
            ),
        )
        accepted_quality = build_quality_decision(
            policy="minimal",
            validation=_passing_validator({}, chapter, {}),
            chapter_index=34,
        )
        executor.quality_coordinator.decide = lambda **_kwargs: accepted_quality

        result = executor.run_once(persist=True)

        self.assertTrue(result["run"]["quality_decision"]["accepted"])
        self.assertEqual("fail", result["run"]["review_gate"]["status"])
        self.assertFalse(result["run"]["accepted"])
        self.assertFalse(result["run"]["committed"])

    def test_cli_gate_off_returncode_zero(self) -> None:
        case_dir = self._case_dir("cli_off")
        result = subprocess.run(
            [
                sys.executable,
                "main.py",
                "--dry-run",
                "--enable-review-pipeline",
                "--review-gate",
                "off",
                "--review-output-dir",
                str(case_dir / "reviews"),
                "--memory",
                "data/notion_memory.example.json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)

    def test_cli_gate_requires_review_pipeline(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "main.py",
                "--dry-run",
                "--review-gate",
                "blocked",
                "--memory",
                "data/notion_memory.example.json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("--review-gate requires --enable-review-pipeline", result.stderr)

    def test_cli_output_run_json_includes_review_gate_and_can_exit_nonzero(self) -> None:
        case_dir = self._case_dir("cli_fail")
        bad_rules = case_dir / "bad_rules.json"
        bad_rules.write_text('{"not": "a valid rule pack"}', encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                "main.py",
                "--dry-run",
                "--persist-dry-run",
                "--enable-review-pipeline",
                "--review-gate",
                "blocked",
                "--review-no-default-rules",
                "--review-rules",
                str(bad_rules),
                "--review-output-dir",
                str(case_dir / "reviews"),
                "--memory",
                "data/notion_memory.example.json",
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

        self.assertEqual(1, result.returncode)
        run = json.loads(result.stdout)
        self.assertEqual("error", run["review_pipeline"]["status"])
        self.assertEqual("blocked", run["review_gate"]["threshold"])
        self.assertEqual(1, run["review_gate"]["exit_code"])
        validate_schema(run["review_gate"], "review_gate_result.schema.json")


def _director(snapshot: dict, memory: dict | None) -> dict:
    return {
        "chapter_index": 34,
        "goal": "review gate regression fixture",
        "actions": ["build_snapshot", "generate_chapter", "validate", "commit_snapshot"],
        "validation_focus": ["logic"],
        "max_repair_attempts": 0,
        "notes": [],
    }


def _passing_validator(snapshot: dict, chapter: str, decision: dict) -> dict:
    return {
        "ok": True,
        "requested_focus": ["logic"],
        "executed_checks": ["logic"],
        "skipped_checks": [],
        "checks": [{"name": "logic", "ok": True, "problems": []}],
        "problems": [],
        "blocking_problem_count": 0,
        "warning_count": 0,
        "severity_counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        "deterministic_repair_count": 0,
        "manual_review_count": 0,
        "repair_action_counts": {},
    }


def _analysis(chapter: str, validation: dict) -> dict:
    return {
        "summary": "Regression fixture accepted by validator for review gate coverage.",
        "events": [],
        "character_changes": [],
        "world_changes": [],
        "new_locations": [],
        "story_state": {
            "last_chapter_ending": "",
            "last_scene_location": "",
            "last_scene_characters": [],
            "open_threads": [],
            "required_opening_bridge": "",
        },
        "spatial_state": {
            "spaces": {},
            "connections": [],
            "character_positions": {},
            "blocked_paths": [],
            "last_transition": {},
        },
        "conflicts": [],
        "validation_ok": True,
    }


if __name__ == "__main__":
    unittest.main()
