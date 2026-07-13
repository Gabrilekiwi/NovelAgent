from __future__ import annotations

import unittest

from core.quality_decision import build_quality_decision
from core.review.repair_loop import ReviewRepairConfig, build_review_repair_plan, run_review_repair_loop
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
    def test_repair_plan_uses_quality_finding_identity_as_primary_task(self) -> None:
        validation = _validation(False, "missing_conflict_marker")
        decision = build_quality_decision(
            policy="standard",
            validation=validation,
            review_pipeline={"status": "pass"},
        )

        plan = build_review_repair_plan(
            before_review={"enabled": True, "status": "blocked", "decision": "blocked"},
            validation=validation,
            quality_decision=decision,
            attempt=1,
            max_attempts=2,
            dry_run=False,
        )

        self.assertEqual(decision["decision_digest"], plan["quality_decision_digest"])
        self.assertTrue(plan["repair_tasks"][0]["id"].startswith("qf:v1:"))
        self.assertEqual("add_conflict_signal", plan["scene_repair_plan"]["steps"][0]["action"])

    def test_invalid_gate_threshold_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported review gate threshold"):
            run_review_repair_loop(
                chapter_text="chapter",
                validation=_validation(),
                before_review={"enabled": True, "status": "pass", "decision": "accept"},
                config=ReviewRepairConfig(enabled=True, gate_threshold="invalid"),
                repair=lambda *_args: "chapter",
                validate=lambda _chapter: _validation(),
                review=lambda _chapter, _attempt: {"enabled": True, "status": "pass", "decision": "accept"},
            )

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
        self.assertTrue(result["accepted"])
        self.assertTrue(result["final_quality_decision"]["accepted"])
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

    def test_warning_gate_triggers_repair_and_requires_pass(self) -> None:
        calls = {"repair": 0, "review": 0}

        def repair(chapter: str, _validation: dict, _plan: dict) -> str:
            calls["repair"] += 1
            return f"{chapter} fixed-{calls['repair']}"

        def review(_chapter: str, attempt: int) -> dict:
            calls["review"] += 1
            return {
                "enabled": True,
                "status": "warning" if attempt == 1 else "pass",
                "decision": "accept_with_warnings" if attempt == 1 else "accept",
            }

        result = run_review_repair_loop(
            chapter_text="original",
            validation=_validation(),
            before_review={"enabled": True, "status": "warning", "decision": "accept_with_warnings"},
            config=ReviewRepairConfig(enabled=True, max_attempts=2, gate_threshold="warning"),
            repair=repair,
            validate=lambda _chapter: _validation(),
            review=review,
        )

        self.assertTrue(result["attempted"])
        self.assertTrue(result["accepted"])
        self.assertEqual(2, result["attempt_count"])
        self.assertEqual(2, calls["repair"])
        self.assertFalse(result["repair_deltas"][0]["accepted"])
        self.assertEqual(1, result["repair_deltas"][0]["after_gate_exit_code"])
        self.assertTrue(result["repair_deltas"][1]["accepted"])
        self.assertEqual("pass", result["after_gate"]["status"])

    def test_warning_gate_exhaustion_is_not_accepted(self) -> None:
        result = run_review_repair_loop(
            chapter_text="original",
            validation=_validation(),
            before_review={"enabled": True, "status": "warning", "decision": "accept_with_warnings"},
            config=ReviewRepairConfig(enabled=True, max_attempts=2, gate_threshold="warning"),
            repair=lambda chapter, _validation, _plan: chapter + " fixed",
            validate=lambda _chapter: _validation(),
            review=lambda _chapter, _attempt: {
                "enabled": True,
                "status": "warning",
                "decision": "accept_with_warnings",
            },
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(2, result["attempt_count"])
        self.assertEqual("post_repair_review_gate_failed", result["rejected_reason"])
        self.assertEqual(1, result["after_gate"]["exit_code"])

    def test_initial_review_error_fails_closed_without_repair(self) -> None:
        result = run_review_repair_loop(
            chapter_text="original",
            validation=_validation(),
            before_review={"enabled": True, "status": "error", "decision": None, "error": "review crashed"},
            config=ReviewRepairConfig(enabled=True, max_attempts=3),
            repair=lambda *_args: self.fail("review errors must not be repaired"),
            validate=lambda _chapter: self.fail("validation must not run"),
            review=lambda _chapter, _attempt: self.fail("review must not rerun"),
        )

        self.assertFalse(result["attempted"])
        self.assertFalse(result["accepted"])
        self.assertEqual("review_error", result["rejected_reason"])
        self.assertEqual("disabled", result["before_gate"]["status"])
        self.assertEqual(0, result["before_gate"]["exit_code"])
        self.assertEqual("original", result["final_chapter"])

    def test_post_repair_review_error_stops_further_attempts(self) -> None:
        calls = {"repair": 0}

        def repair(chapter: str, _validation: dict, _plan: dict) -> str:
            calls["repair"] += 1
            return chapter + " fixed"

        result = run_review_repair_loop(
            chapter_text="original",
            validation=_validation(),
            before_review={"enabled": True, "status": "blocked", "decision": "blocked"},
            config=ReviewRepairConfig(enabled=True, max_attempts=3, gate_threshold="blocked"),
            repair=repair,
            validate=lambda _chapter: _validation(),
            review=lambda _chapter, _attempt: {
                "enabled": True,
                "status": "error",
                "decision": None,
                "error": "review crashed",
            },
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(1, result["attempt_count"])
        self.assertEqual(1, calls["repair"])
        self.assertEqual("post_repair_review_error", result["rejected_reason"])
        self.assertEqual("fail", result["after_gate"]["status"])
        self.assertEqual("error", result["after_gate"]["review_status"])
        self.assertEqual(1, result["after_gate"]["exit_code"])

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
