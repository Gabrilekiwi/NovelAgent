from __future__ import annotations

import unittest

from core.review.repair_loop import ReviewRepairConfig, run_review_repair_loop
from core.schema import validate_schema


def _validation(ok: bool = True, code: str | None = None) -> dict:
    problems = []
    if not ok:
        problems.append(
            {
                "code": code or "missing_conflict_marker",
                "message": "Needs more conflict.",
                "validator": "logic",
                "severity": "high",
                "blocking": True,
                "category": "blocking",
                "repair_hint": "Add a conflict signal.",
                "repair_action": "add_conflict_signal",
                "repair_parameters": {},
                "evidence": [{"kind": "test", "value": "conflict"}],
            }
        )
    return validate_schema(
        {
            "ok": ok,
            "requested_focus": ["logic"],
            "executed_checks": ["logic"],
            "skipped_checks": [],
            "checks": [{"name": "logic", "ok": ok, "problems": problems}],
            "problems": problems,
            "blocking_problem_count": len(problems),
            "warning_count": 0,
            "severity_counts": [{"severity": "high", "count": len(problems)}] if problems else [],
            "deterministic_repair_count": len(problems),
            "manual_review_count": 0,
            "repair_action_counts": [{"action": "add_conflict_signal", "count": len(problems)}] if problems else [],
        },
        "validation_result.schema.json",
    )


class ReviewRepairLoopTests(unittest.TestCase):
    def test_review_pass_does_not_trigger_repair(self) -> None:
        result = run_review_repair_loop(
            chapter_text="chapter",
            validation=_validation(),
            before_review={"enabled": True, "status": "pass", "decision": "accept"},
            config=ReviewRepairConfig(enabled=True),
            repair=lambda *_args: self.fail("repair should not run"),
            validate=lambda _chapter: _validation(),
            review=lambda _chapter, _attempt: {"enabled": True, "status": "pass", "decision": "accept"},
        )

        self.assertFalse(result["attempted"])
        self.assertFalse(result["accepted"])
        self.assertEqual("review_status_does_not_require_repair", result["rejected_reason"])

    def test_dry_run_builds_plan_without_mutating_chapter(self) -> None:
        result = run_review_repair_loop(
            chapter_text="original",
            validation=_validation(),
            before_review={"enabled": True, "status": "blocked", "decision": "blocked", "blocking_task_count": 1},
            config=ReviewRepairConfig(enabled=True, dry_run=True),
            repair=lambda *_args: self.fail("dry-run repair should not run"),
            validate=lambda _chapter: _validation(),
            review=lambda _chapter, _attempt: {"enabled": True, "status": "pass", "decision": "accept"},
        )

        self.assertTrue(result["attempted"])
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["accepted"])
        self.assertEqual("original", result["final_chapter"])
        self.assertEqual("review_repair_dry_run", result["rejected_reason"])
        self.assertIn("scene_repair_plan", result["repair_plan"])

    def test_successful_repair_reruns_validation_and_review(self) -> None:
        calls = {"validate": 0, "review": 0}

        def validate(chapter: str) -> dict:
            calls["validate"] += 1
            self.assertIn("fixed", chapter)
            return _validation()

        def review(chapter: str, attempt: int) -> dict:
            calls["review"] += 1
            self.assertEqual(1, attempt)
            self.assertIn("fixed", chapter)
            return {"enabled": True, "status": "warning", "decision": "accept_with_warnings"}

        result = run_review_repair_loop(
            chapter_text="original",
            validation=_validation(),
            before_review={"enabled": True, "status": "needs_revision", "decision": "needs_revision"},
            config=ReviewRepairConfig(enabled=True),
            repair=lambda chapter, _validation, _plan: chapter + " fixed",
            validate=validate,
            review=review,
        )

        self.assertTrue(result["accepted"])
        self.assertEqual("original fixed", result["final_chapter"])
        self.assertEqual(1, calls["validate"])
        self.assertEqual(1, calls["review"])

    def test_repairer_error_is_audited(self) -> None:
        result = run_review_repair_loop(
            chapter_text="original",
            validation=_validation(),
            before_review={"enabled": True, "status": "blocked", "decision": "blocked"},
            config=ReviewRepairConfig(enabled=True),
            repair=lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")),
            validate=lambda _chapter: _validation(),
            review=lambda _chapter, _attempt: {"enabled": True, "status": "pass", "decision": "accept"},
        )

        self.assertFalse(result["accepted"])
        self.assertEqual("repairer_failed", result["rejected_reason"])
        self.assertEqual(1, len(result["errors"]))
        self.assertIn("boom", result["errors"][0]["error"])


if __name__ == "__main__":
    unittest.main()
