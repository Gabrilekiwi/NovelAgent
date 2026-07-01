from __future__ import annotations

import unittest

from core.schema import validate_schema
from modules.scene_repair import apply_repair_plan, build_repair_plan, repair_scene


class SceneRepairTest(unittest.TestCase):
    def test_build_repair_plan_orders_actions_by_priority(self) -> None:
        plan = build_repair_plan(
            {
                "ok": False,
                "problems": [
                    {
                        "code": "missing_required_constraint_term",
                        "message": "Missing required term.",
                        "validator": "logic",
                        "term": "serum",
                        "evidence": [{"kind": "missing_required_term", "value": "serum"}],
                    },
                    {
                        "code": "chapter_too_short",
                        "message": "Chapter is too short.",
                        "validator": "logic",
                    },
                    {
                        "code": "no_known_location",
                        "message": "No known location.",
                        "validator": "spatial",
                        "suggested_term": "shelter",
                    },
                ],
            }
        )

        self.assertEqual(3, plan["problem_count"])
        self.assertEqual(3, plan["blocking_problem_count"])
        self.assertEqual(0, plan["warning_count"])
        self.assertEqual([{"severity": "medium", "count": 3}], plan["severity_counts"])
        self.assertEqual("medium", plan["risk_level"])
        self.assertIsNone(plan["repair_budget"])
        self.assertIsNone(plan["attempt"])
        self.assertEqual(3, plan["deterministic_step_count"])
        self.assertEqual(0, plan["manual_review_count"])
        self.assertEqual(
            ["expand_scene", "add_required_term", "anchor_known_location"],
            plan["actions"],
        )
        self.assertEqual("chapter_too_short", plan["steps"][0]["code"])
        self.assertEqual("logic", plan["steps"][0]["validator"])
        self.assertEqual("spatial", plan["steps"][2]["validator"])
        self.assertEqual("medium", plan["steps"][0]["severity"])
        self.assertTrue(plan["steps"][0]["blocking"])
        self.assertTrue(plan["steps"][0]["repair_hint"])
        self.assertEqual({"term": "serum"}, plan["steps"][1]["parameters"])
        self.assertEqual(
            [{"kind": "missing_required_term", "value": "serum"}],
            plan["steps"][1]["evidence"],
        )
        self.assertIs(plan, validate_schema(plan, "repair_plan.schema.json"))

    def test_build_repair_plan_marks_unknown_problem_for_manual_review(self) -> None:
        plan = build_repair_plan(
            {
                "ok": False,
                "problems": [{"code": "new_problem", "message": "Something new."}],
            }
        )

        self.assertEqual(["manual_review"], plan["actions"])
        self.assertEqual(0, plan["deterministic_step_count"])
        self.assertEqual(1, plan["manual_review_count"])
        self.assertEqual("new_problem", plan["steps"][0]["code"])
        self.assertEqual({"raw_problem": {}}, plan["steps"][0]["parameters"])
        self.assertIs(plan, validate_schema(plan, "repair_plan.schema.json"))

    def test_build_repair_plan_records_budget_attempt_and_risk(self) -> None:
        plan = build_repair_plan(
            {
                "ok": False,
                "problems": [
                    {
                        "code": "forbidden_constraint_term",
                        "message": "Forbidden term.",
                        "severity": "critical",
                        "blocking": True,
                        "term": "cure",
                    },
                    {
                        "code": "new_warning",
                        "message": "Review later.",
                        "severity": "low",
                        "blocking": False,
                    },
                ],
            },
            repair_budget=3,
            attempt=2,
        )

        self.assertEqual("critical", plan["risk_level"])
        self.assertEqual(3, plan["repair_budget"])
        self.assertEqual(2, plan["attempt"])
        self.assertEqual(1, plan["blocking_problem_count"])
        self.assertEqual(1, plan["warning_count"])
        self.assertEqual([{"severity": "critical", "count": 1}, {"severity": "low", "count": 1}], plan["severity_counts"])
        self.assertEqual(1, plan["deterministic_step_count"])
        self.assertEqual(1, plan["manual_review_count"])
        self.assertIs(plan, validate_schema(plan, "repair_plan.schema.json"))

    def test_build_repair_plan_normalizes_known_parameters(self) -> None:
        plan = build_repair_plan(
            {
                "ok": False,
                "problems": [
                    {
                        "code": "chapter_index_mismatch",
                        "message": "Wrong chapter.",
                        "expected": 3,
                        "actual": 2,
                        "debug": "not part of repair contract",
                    },
                    {
                        "code": "character_unknown_location",
                        "message": "Unknown location.",
                        "character": "Mira",
                        "location": "basement",
                    },
                ],
            }
        )

        by_action = {step["action"]: step for step in plan["steps"]}
        self.assertEqual({"expected": "3", "actual": "2"}, by_action["correct_chapter_index"]["parameters"])
        self.assertEqual(
            {"character": "Mira", "location": "basement"},
            by_action["flag_unknown_location"]["parameters"],
        )
        self.assertNotIn("debug", by_action["correct_chapter_index"]["parameters"])
        self.assertIs(plan, validate_schema(plan, "repair_plan.schema.json"))

    def test_build_repair_plan_prefers_validator_repair_contract_fields(self) -> None:
        plan = build_repair_plan(
            {
                "ok": False,
                "problems": [
                    {
                        "code": "missing_required_constraint_term",
                        "message": "Missing serum.",
                        "validator": "logic",
                        "term": "ignored-direct-field",
                        "repair_action": "add_required_term",
                        "repair_parameters": {"term": "serum"},
                    }
                ],
            }
        )

        self.assertEqual("add_required_term", plan["steps"][0]["action"])
        self.assertEqual({"term": "serum"}, plan["steps"][0]["parameters"])
        self.assertIs(plan, validate_schema(plan, "repair_plan.schema.json"))

    def test_build_repair_plan_summarizes_recovery_failure_modes(self) -> None:
        plan = build_repair_plan(
            {
                "ok": False,
                "problems": [
                    {
                        "code": "missing_conflict_marker",
                        "message": "Still missing conflict.",
                        "validator": "logic",
                    },
                    {
                        "code": "no_known_location",
                        "message": "New spatial issue.",
                        "validator": "spatial",
                    },
                ],
            },
            repair_budget=2,
            attempt=2,
            recovery_context={
                "available": True,
                "source_run_id": "chapter_1_test",
                "status": "rejected",
                "committed": False,
                "problem_codes": ["missing_conflict_marker"],
                "skipped_checks": ["continuity"],
                "repair_attempts": 1,
                "repair_plan": {
                    "risk_level": "high",
                    "repair_budget": 1,
                    "attempt": 1,
                    "manual_review_count": 1,
                },
                "repair_deltas": [
                    {
                        "remaining_problem_codes": ["missing_conflict_marker"],
                        "new_problem_codes": ["no_known_location"],
                    }
                ],
            },
        )

        recovery = plan["recovery"]
        self.assertTrue(recovery["available"])
        self.assertEqual("chapter_1_test", recovery["source_run_id"])
        self.assertEqual(["missing_conflict_marker"], recovery["repeated_problem_codes"])
        self.assertEqual(["missing_conflict_marker"], recovery["unresolved_problem_codes"])
        self.assertEqual(["no_known_location"], recovery["new_problem_codes"])
        self.assertEqual(["continuity"], recovery["skipped_checks"])
        self.assertTrue(recovery["repair_introduced_new_problems"])
        self.assertTrue(recovery["repair_budget_exhausted"])
        self.assertIn("previous_problem_repeated", recovery["failure_modes"])
        self.assertIn("previous_repair_stalled", recovery["failure_modes"])
        self.assertIn("previous_repair_introduced_new_problems", recovery["failure_modes"])
        self.assertIn("previous_validation_skipped", recovery["failure_modes"])
        self.assertIn("previous_manual_review_required", recovery["failure_modes"])
        self.assertIn("previous_repair_budget_exhausted", recovery["failure_modes"])
        self.assertIs(plan, validate_schema(plan, "repair_plan.schema.json"))

    def test_dry_run_repair_uses_plan_actions(self) -> None:
        repaired = repair_scene(
            "The team waited.",
            {
                "ok": False,
                "problems": [
                    {"code": "missing_conflict_marker", "message": "Missing conflict."},
                    {"code": "missing_required_constraint_term", "message": "Missing serum.", "term": "serum"},
                ],
            },
            "input pack",
            dry_run=True,
        )

        self.assertIn("serum", repaired.lower())
        self.assertIn("conflict", repaired.lower())

    def test_repair_scene_uses_provided_repair_plan(self) -> None:
        provided_plan = build_repair_plan(
            {
                "ok": False,
                "problems": [{"code": "missing_conflict_marker", "message": "Missing conflict."}],
            },
            repair_budget=4,
            attempt=3,
        )

        repaired = repair_scene(
            "The team waited.",
            {
                "ok": False,
                "problems": [
                    {"code": "missing_required_constraint_term", "message": "Missing serum.", "term": "serum"}
                ],
            },
            "input pack",
            dry_run=True,
            repair_plan=provided_plan,
        )

        self.assertIn("conflict", repaired.lower())
        self.assertNotIn("serum", repaired.lower())

    def test_apply_repair_plan_dispatches_steps_by_priority(self) -> None:
        repaired = apply_repair_plan(
            "Chapter 9: The team waited in a blank room.",
            {
                "problem_count": 3,
                "actions": ["add_required_term", "correct_chapter_index", "anchor_known_location"],
                "steps": [
                    {
                        "index": 1,
                        "code": "missing_required_constraint_term",
                        "message": "Missing term.",
                        "severity": "high",
                        "blocking": True,
                        "repair_hint": "Add serum.",
                        "action": "add_required_term",
                        "priority": 50,
                        "strategy": "Mention required term.",
                        "parameters": {"term": "serum"},
                    },
                    {
                        "index": 2,
                        "code": "chapter_index_mismatch",
                        "message": "Wrong chapter.",
                        "severity": "high",
                        "blocking": True,
                        "repair_hint": "Correct chapter.",
                        "action": "correct_chapter_index",
                        "priority": 10,
                        "strategy": "Correct chapter number.",
                        "parameters": {"expected": 3},
                    },
                    {
                        "index": 3,
                        "code": "no_known_location",
                        "message": "No known location.",
                        "severity": "medium",
                        "blocking": True,
                        "repair_hint": "Anchor location.",
                        "action": "anchor_known_location",
                        "priority": 20,
                        "strategy": "Mention known location.",
                        "parameters": {"suggested_term": "shelter"},
                    },
                ],
            },
        )

        self.assertIn("Chapter 3", repaired)
        self.assertLess(repaired.index("shelter"), repaired.index("serum"))

    def test_apply_repair_plan_manual_review_is_noop(self) -> None:
        text = "The team waited."
        repaired = apply_repair_plan(
            text,
            {
                "problem_count": 1,
                "actions": ["manual_review"],
                "steps": [
                    {
                        "index": 1,
                        "code": "new_problem",
                        "message": "Unknown.",
                        "severity": "low",
                        "blocking": False,
                        "repair_hint": "Review manually.",
                        "action": "manual_review",
                        "priority": 1000,
                        "strategy": "No deterministic strategy.",
                        "parameters": {},
                    }
                ],
            },
        )

        self.assertEqual(text, repaired)


if __name__ == "__main__":
    unittest.main()
