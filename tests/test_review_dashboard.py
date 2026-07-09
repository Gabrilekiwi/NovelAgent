from __future__ import annotations

import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from core.review.dashboard import build_review_dashboard, build_review_dashboard_from_index
from core.review.index import build_review_index_entry, load_review_index, update_review_index


ROOT = Path(__file__).resolve().parents[1]


class ReviewDashboardTests(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        path = ROOT / ".tmp" / "test_review_dashboard" / f"{name}_{uuid.uuid4().hex}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _entry(self, case_dir: Path, run_id: str, *, status: str = "warning", gate_status: str = "pass") -> dict:
        artifacts_dir = case_dir / "reviews" / run_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "human_review_report.md").write_text("# review\n", encoding="utf-8")
        (artifacts_dir / "rule_repair_prompt.md").write_text("# prompt\n", encoding="utf-8")
        (artifacts_dir / "review_pipeline_summary.json").write_text("{}\n", encoding="utf-8")
        return build_review_index_entry(
            run_id=run_id,
            artifacts_dir=artifacts_dir,
            review_pipeline={
                "enabled": True,
                "status": status,
                "decision": "blocked" if status == "blocked" else "accept_with_warnings",
                "quality_score": 78,
                "rule_score": 66,
                "repair_task_count": 2,
                "blocking_task_count": 1 if status == "blocked" else 0,
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

    def _write_index(self, case_dir: Path) -> Path:
        review_dir = case_dir / "reviews"
        update_review_index(
            review_output_dir=review_dir,
            entry=self._entry(case_dir, "chapter_1_20260709T101010000000Z", status="warning"),
        )
        update_review_index(
            review_output_dir=review_dir,
            entry=self._entry(case_dir, "chapter_2_20260709T102020000000Z", status="blocked", gate_status="fail"),
        )
        return review_dir

    def test_build_dashboard_from_empty_index(self) -> None:
        review_dir = self._case_dir("empty")
        result = build_review_dashboard(review_index=load_review_index(review_output_dir=review_dir))

        self.assertEqual(0, result["metadata"]["entry_count"])
        self.assertIn("NovelAgent Review Dashboard", result["html"])
        self.assertIn("No review entries found.", result["html"])

    def test_build_dashboard_from_populated_index(self) -> None:
        review_dir = self._write_index(self._case_dir("populated"))

        result = build_review_dashboard(review_index=load_review_index(review_output_dir=review_dir))

        self.assertEqual(2, result["metadata"]["entry_count"])
        self.assertEqual("chapter_2_20260709T102020000000Z", result["metadata"]["latest_run_id"])
        self.assertIn("chapter_2_20260709T102020000000Z", result["html"])
        self.assertIn("blocked", result["html"])
        self.assertIn("78", result["html"])
        self.assertIn("human_review_report.md", result["html"])
        self.assertIn("rule_repair_prompt.md", result["html"])

    def test_html_escapes_index_values(self) -> None:
        review_dir = self._case_dir("escape")
        index = load_review_index(review_output_dir=review_dir)
        index["review_output_dir"] = '<review&"dir">'

        result = build_review_dashboard(review_index=index, title='<Title&"Bad">')

        self.assertNotIn('<Title&"Bad">', result["html"])
        self.assertNotIn('<review&"dir">', result["html"])
        self.assertIn("&lt;Title&amp;&quot;Bad&quot;&gt;", result["html"])
        self.assertIn("&lt;review&amp;&quot;dir&quot;&gt;", result["html"])

    def test_html_escapes_table_values_that_look_like_links(self) -> None:
        review_dir = self._case_dir("escape_table")
        index = load_review_index(review_output_dir=review_dir)
        entry = self._entry(review_dir, "chapter_1_20260709T101010000000Z")
        entry["run_id"] = '<a href="bad">bad</a>'
        index["latest_run_id"] = entry["run_id"]
        index["summary"]["entry_count"] = 1
        index["summary"]["warning_count"] = 1
        index["entries"] = [entry]

        result = build_review_dashboard(review_index=index)

        self.assertNotIn('<a href="bad">bad</a>', result["html"])
        self.assertIn("&lt;a href=&quot;bad&quot;&gt;bad&lt;/a&gt;", result["html"])

    def test_output_path_writes_dashboard(self) -> None:
        review_dir = self._write_index(self._case_dir("write"))
        output_path = review_dir / "dashboard.html"

        result = build_review_dashboard_from_index(review_output_dir=review_dir, output_path=output_path)

        self.assertTrue(output_path.exists())
        self.assertEqual(str(output_path), result["metadata"]["output_path"])
        self.assertEqual(output_path.read_text(encoding="utf-8"), result["html"])

    def test_dashboard_uses_relative_links_when_output_path_is_known(self) -> None:
        review_dir = self._write_index(self._case_dir("links"))
        output_path = review_dir / "dashboard.html"

        result = build_review_dashboard_from_index(review_output_dir=review_dir, output_path=output_path)

        self.assertIn('href="chapter_2_20260709T102020000000Z/human_review_report.md"', result["html"])
        self.assertNotIn('href="' + str(ROOT).replace("\\", "/"), result["html"])

    def test_main_review_dashboard_json(self) -> None:
        review_dir = self._write_index(self._case_dir("main_cli"))
        result = subprocess.run(
            [
                sys.executable,
                "main.py",
                "--review-dashboard",
                "--review-output-dir",
                str(review_dir),
                "--output-json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(2, payload["dashboard"]["entry_count"])
        self.assertTrue((review_dir / "dashboard.html").exists())

    def test_script_build_review_dashboard_json(self) -> None:
        review_dir = self._write_index(self._case_dir("script_cli"))
        output_path = review_dir / "custom_dashboard.html"
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "scripts/build_review_dashboard.py",
                "--review-output-dir",
                str(review_dir),
                "--out",
                str(output_path),
                "--json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(str(output_path), payload["dashboard"]["output_path"])
        self.assertTrue(output_path.exists())


if __name__ == "__main__":
    unittest.main()
