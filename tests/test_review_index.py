from __future__ import annotations

import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from core.engine.executor import AgentExecutor
from core.review.index import (
    build_review_index_entry,
    get_latest_review,
    list_recent_reviews,
    load_review_index,
    update_review_index,
)
from core.review.runtime import RuntimeReviewConfig
from core.schema import validate_schema


ROOT = Path(__file__).resolve().parents[1]


class ReviewIndexTests(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        path = ROOT / ".tmp" / "test_review_index" / f"{name}_{uuid.uuid4().hex}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _entry(self, run_id: str, *, status: str = "warning", gate_status: str = "pass") -> dict:
        case_dir = self._case_dir(f"entry_{status}")
        artifacts_dir = case_dir / run_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "human_review_report.md").write_text("# review\n", encoding="utf-8")
        (artifacts_dir / "rule_repair_prompt.md").write_text("# prompt\n", encoding="utf-8")
        return build_review_index_entry(
            run_id=run_id,
            artifacts_dir=artifacts_dir,
            review_pipeline={
                "enabled": True,
                "status": status,
                "decision": "blocked" if status == "blocked" else "accept_with_warnings",
                "quality_score": 42,
                "rule_score": 45,
                "repair_task_count": 2,
                "blocking_task_count": 1,
                "artifacts_dir": str(artifacts_dir),
                "summary_path": str(artifacts_dir / "review_pipeline_summary.json"),
            },
            review_gate={
                "enabled": gate_status != "disabled",
                "threshold": "blocked",
                "status": gate_status,
                "exit_code": 1 if gate_status == "fail" else 0,
            },
        )

    def _write_snapshot(self, case_dir: Path) -> Path:
        snapshot_path = case_dir / "snapshot.json"
        snapshot_path.write_text(
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
        return snapshot_path

    def test_build_index_entry_from_review_pipeline(self) -> None:
        entry = self._entry("chapter_2_20260709T102020000000Z", status="blocked", gate_status="fail")

        self.assertEqual("chapter_2_20260709T102020000000Z", entry["run_id"])
        self.assertEqual(2, entry["chapter_index"])
        self.assertEqual("blocked", entry["review_status"])
        self.assertEqual("blocked", entry["review_decision"])
        self.assertEqual(42, entry["quality_score"])
        self.assertEqual(45, entry["rule_score"])
        self.assertEqual(2, entry["repair_task_count"])
        self.assertEqual(1, entry["blocking_task_count"])
        self.assertEqual("fail", entry["gate_status"])

    def test_update_index_creates_file(self) -> None:
        case_dir = self._case_dir("create")
        entry = self._entry("chapter_1_20260709T101010000000Z")

        index = update_review_index(review_output_dir=case_dir, entry=entry)

        self.assertTrue((case_dir / "review_index.json").exists())
        validate_schema(index, "review_index.schema.json")
        self.assertEqual(1, index["summary"]["entry_count"])
        self.assertEqual(entry["run_id"], index["latest_run_id"])

    def test_update_index_replaces_same_run_id(self) -> None:
        case_dir = self._case_dir("replace")
        run_id = "chapter_1_20260709T101010000000Z"
        update_review_index(review_output_dir=case_dir, entry=self._entry(run_id, status="warning"))
        index = update_review_index(review_output_dir=case_dir, entry=self._entry(run_id, status="blocked"))

        self.assertEqual(1, index["summary"]["entry_count"])
        self.assertEqual("blocked", index["entries"][0]["review_status"])

    def test_index_keeps_latest_first_and_max_entries(self) -> None:
        case_dir = self._case_dir("latest")
        for index in range(1, 6):
            update_review_index(
                review_output_dir=case_dir,
                entry=self._entry(f"chapter_{index}_20260709T1010{index:02d}000000Z"),
                max_entries=3,
            )

        index = load_review_index(review_output_dir=case_dir)
        self.assertEqual(3, index["summary"]["entry_count"])
        self.assertEqual("chapter_5_20260709T101005000000Z", index["entries"][0]["run_id"])
        self.assertEqual(index["entries"][0]["run_id"], index["latest_run_id"])

    def test_get_latest_review(self) -> None:
        empty_dir = self._case_dir("empty")
        self.assertIsNone(get_latest_review(review_output_dir=empty_dir))

        update_review_index(review_output_dir=empty_dir, entry=self._entry("chapter_1_20260709T101010000000Z"))
        update_review_index(review_output_dir=empty_dir, entry=self._entry("chapter_2_20260709T102020000000Z"))
        self.assertEqual("chapter_2_20260709T102020000000Z", get_latest_review(review_output_dir=empty_dir)["run_id"])

    def test_list_recent_reviews_filters_status_and_gate_status(self) -> None:
        case_dir = self._case_dir("filters")
        update_review_index(review_output_dir=case_dir, entry=self._entry("chapter_1_20260709T101010000000Z", status="pass"))
        update_review_index(review_output_dir=case_dir, entry=self._entry("chapter_2_20260709T102020000000Z", status="blocked", gate_status="fail"))
        update_review_index(review_output_dir=case_dir, entry=self._entry("chapter_3_20260709T103030000000Z", status="error"))

        blocked = list_recent_reviews(review_output_dir=case_dir, status="blocked")
        failed_gate = list_recent_reviews(review_output_dir=case_dir, gate_status="fail")

        self.assertEqual(["blocked"], [entry["review_status"] for entry in blocked])
        self.assertEqual(["fail"], [entry["gate_status"] for entry in failed_gate])

    def test_runtime_review_updates_index(self) -> None:
        case_dir = self._case_dir("runtime")
        snapshot_path = self._write_snapshot(case_dir)

        result = AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=case_dir / "missing_memory.json",
            run_dir=case_dir / "runs",
            chapter_dir=case_dir / "chapters",
            dry_run=True,
            review_config=RuntimeReviewConfig(enabled=True, output_dir=case_dir / "reviews"),
        ).run_once(persist=True)

        index_path = case_dir / "reviews" / "review_index.json"
        self.assertTrue(index_path.exists())
        self.assertTrue(result["run"]["review_index"]["enabled"])
        self.assertEqual(str(index_path), result["run"]["review_index"]["index_path"])
        latest = get_latest_review(review_output_dir=case_dir / "reviews")
        self.assertEqual(result["run"]["id"], latest["run_id"])

    def test_review_failure_is_indexed(self) -> None:
        case_dir = self._case_dir("runtime_error")
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

        self.assertEqual("error", result["run"]["review_pipeline"]["status"])
        latest = get_latest_review(review_output_dir=case_dir / "reviews")
        self.assertEqual("error", latest["review_status"])

    def test_main_review_latest_and_list_json(self) -> None:
        case_dir = self._case_dir("cli")
        review_dir = case_dir / "reviews"
        update_review_index(review_output_dir=review_dir, entry=self._entry("chapter_1_20260709T101010000000Z", status="blocked"))

        latest = subprocess.run(
            [
                sys.executable,
                "main.py",
                "--review-latest",
                "--review-output-dir",
                str(review_dir),
                "--output-json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        listing = subprocess.run(
            [
                sys.executable,
                "main.py",
                "--review-list",
                "--review-output-dir",
                str(review_dir),
                "--review-list-limit",
                "5",
                "--output-json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, latest.returncode, latest.stderr)
        self.assertEqual(0, listing.returncode, listing.stderr)
        latest_payload = json.loads(latest.stdout)
        list_payload = json.loads(listing.stdout)
        self.assertTrue(latest_payload["ok"])
        self.assertEqual("blocked", latest_payload["latest"]["review_status"])
        self.assertTrue(list_payload["ok"])
        self.assertEqual(1, list_payload["count"])


if __name__ == "__main__":
    unittest.main()
