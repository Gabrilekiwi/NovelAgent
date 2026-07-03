from __future__ import annotations

import unittest

from core.director import DirectorDecisionError, decide_next_step, validate_decision


class DirectorDecisionTest(unittest.TestCase):
    def test_rejects_unknown_action(self) -> None:
        decision = {
            "chapter_index": 1,
            "goal": "continue_existing_arc",
            "actions": ["generate_chapter", "validate", "publish"],
            "validation_focus": ["logic"],
            "max_repair_attempts": 1,
            "notes": [],
        }

        with self.assertRaises(DirectorDecisionError):
            validate_decision(decision)

    def test_rejects_missing_required_validation_action(self) -> None:
        decision = {
            "chapter_index": 1,
            "goal": "continue_existing_arc",
            "actions": ["generate_chapter"],
            "validation_focus": ["logic"],
            "max_repair_attempts": 1,
            "notes": [],
        }

        with self.assertRaises(DirectorDecisionError):
            validate_decision(decision)

    def test_uses_rejected_last_run_as_recovery_context(self) -> None:
        decision = decide_next_step(
            {"chapter_index": 2, "world_state": {}, "characters": {}, "timeline": []},
            {
                "source": "test",
                "status": "ready",
                "items": [],
                "last_run": {
                    "status": "rejected",
                    "problem_codes": ["no_known_location", "missing_required_constraint_term"],
                },
            },
        )

        self.assertEqual("recover_from_rejected_run", decision["goal"])
        self.assertEqual(2, decision["max_repair_attempts"])
        self.assertNotIn("polish", decision["actions"])
        self.assertEqual("spatial", decision["validation_focus"][0])
        self.assertIn("logic", decision["validation_focus"])
        self.assertTrue(any("Previous run rejected" in note for note in decision["notes"]))
        self.assertTrue(any("blocking problems" in note for note in decision["notes"]))

    def test_uses_failed_last_run_as_recovery_context(self) -> None:
        decision = decide_next_step(
            {"chapter_index": 2, "world_state": {}, "characters": {}, "timeline": []},
            {
                "source": "test",
                "status": "ready",
                "items": [],
                "last_run": {
                    "status": "failed",
                    "problem_codes": ["execution_error"],
                    "workflow": ["generate_chapter", "validate"],
                    "error_type": "ModelOutputError",
                    "error_message": "chapter output is empty",
                },
            },
        )

        self.assertEqual("recover_from_failed_run", decision["goal"])
        self.assertEqual(2, decision["max_repair_attempts"])
        self.assertNotIn("polish", decision["actions"])
        self.assertIn("logic", decision["validation_focus"])
        self.assertTrue(any("Previous run failed" in note for note in decision["notes"]))
        self.assertTrue(any("ModelOutputError" in note for note in decision["notes"]))
        self.assertTrue(any("Previous workflow" in note for note in decision["notes"]))

    def test_pre_generation_bridge_failure_keeps_normal_polish_workflow(self) -> None:
        decision = decide_next_step(
            {
                "chapter_index": 15,
                "world_state": {"locations": {"pier": {}}},
                "characters": {},
                "timeline": [{"summary": "prior chapter"}],
                "story_state": {
                    "last_scene_location": "pier",
                    "required_opening_bridge": "Continue from pier",
                },
                "spatial_state": {
                    "spaces": {"pier": {}},
                    "connections": [],
                    "character_positions": {},
                },
            },
            {
                "source": "test",
                "status": "ready",
                "items": [],
                "last_run": {
                    "status": "failed",
                    "problem_codes": ["execution_error"],
                    "workflow": ["build_snapshot", "pre_validate_bridge", "generate_chapter", "validate"],
                    "error_type": "ValueError",
                    "error_message": "bridge pre-validation failed: invalid_spatial_transition",
                },
            },
        )

        self.assertEqual("continue_existing_arc", decision["goal"])
        self.assertIn("polish", decision["actions"])
        self.assertTrue(any("pre-validation before generation" in note for note in decision["notes"]))

    def test_recovery_prioritizes_previous_skipped_validation_checks(self) -> None:
        decision = decide_next_step(
            {"chapter_index": 2, "world_state": {}, "characters": {}, "timeline": []},
            {
                "source": "test",
                "status": "ready",
                "items": [],
                "last_run": {
                    "status": "rejected",
                    "problem_codes": ["missing_conflict_marker"],
                    "executed_checks": ["logic"],
                    "skipped_checks": ["continuity", "spatial"],
                },
            },
        )

        self.assertEqual("recover_from_rejected_run", decision["goal"])
        self.assertEqual(["continuity", "spatial", "logic"], decision["validation_focus"])
        self.assertTrue(any("Previous validation coverage" in note for note in decision["notes"]))
        self.assertTrue(any("executed=logic" in note for note in decision["notes"]))
        self.assertTrue(any("skipped=continuity,spatial" in note for note in decision["notes"]))
        self.assertTrue(any("Prioritize validation checks skipped" in note for note in decision["notes"]))

    def test_critical_or_many_blocking_problems_raise_recovery_budget(self) -> None:
        decision = decide_next_step(
            {"chapter_index": 2, "world_state": {}, "characters": {}, "timeline": []},
            {
                "source": "test",
                "status": "ready",
                "items": [],
                "last_run": {
                    "status": "rejected",
                    "problem_codes": [
                        "forbidden_constraint_term",
                        "missing_conflict_marker",
                        "no_known_location",
                    ],
                    "blocking_problem_count": 3,
                    "severity_counts": [
                        {"severity": "critical", "count": 1},
                        {"severity": "high", "count": 1},
                        {"severity": "medium", "count": 1},
                    ],
                },
            },
        )

        self.assertEqual(3, decision["max_repair_attempts"])
        self.assertTrue(any("critical=1" in note for note in decision["notes"]))

    def test_repair_delta_stall_raises_recovery_budget_and_focus(self) -> None:
        decision = decide_next_step(
            {"chapter_index": 2, "world_state": {}, "characters": {}, "timeline": []},
            {
                "source": "test",
                "status": "ready",
                "items": [],
                "last_run": {
                    "status": "rejected",
                    "problem_codes": ["missing_conflict_marker"],
                    "blocking_problem_count": 1,
                    "severity_counts": [{"severity": "high", "count": 1}],
                    "repair_deltas": [
                        {
                            "attempt": 1,
                            "before_problem_count": 1,
                            "after_problem_count": 1,
                            "resolved_problem_codes": [],
                            "new_problem_codes": [],
                            "remaining_problem_codes": ["no_known_location"],
                        }
                    ],
                    "repair_stalled": True,
                },
            },
        )

        self.assertEqual(3, decision["max_repair_attempts"])
        self.assertEqual("spatial", decision["validation_focus"][0])
        self.assertTrue(any("Previous repair delta" in note for note in decision["notes"]))
        self.assertTrue(any("did not resolve" in note for note in decision["notes"]))

    def test_repair_delta_new_problem_raises_recovery_budget(self) -> None:
        decision = decide_next_step(
            {"chapter_index": 2, "world_state": {}, "characters": {}, "timeline": []},
            {
                "source": "test",
                "status": "ready",
                "items": [],
                "last_run": {
                    "status": "rejected",
                    "problem_codes": ["missing_conflict_marker"],
                    "repair_deltas": [
                        {
                            "attempt": 1,
                            "before_problem_count": 1,
                            "after_problem_count": 1,
                            "resolved_problem_codes": ["missing_conflict_marker"],
                            "new_problem_codes": ["character_location_not_mentioned"],
                            "remaining_problem_codes": [],
                        }
                    ],
                    "repair_introduced_new_problems": True,
                },
            },
        )

        self.assertEqual(3, decision["max_repair_attempts"])
        self.assertEqual("spatial", decision["validation_focus"][0])
        self.assertTrue(any("introduced new validation problems" in note for note in decision["notes"]))

    def test_previous_repair_plan_manual_review_uses_full_focus(self) -> None:
        decision = decide_next_step(
            {"chapter_index": 2, "world_state": {}, "characters": {}, "timeline": []},
            {
                "source": "test",
                "status": "ready",
                "items": [],
                "last_run": {
                    "status": "rejected",
                    "committed": False,
                    "problem_codes": ["new_problem"],
                    "blocking_problem_count": 1,
                    "severity_counts": [{"severity": "medium", "count": 1}],
                    "repair_plan": {
                        "risk_level": "medium",
                        "repair_budget": 2,
                        "attempt": 1,
                        "manual_review_count": 1,
                    },
                },
            },
        )

        self.assertEqual(3, decision["max_repair_attempts"])
        self.assertEqual(["continuity", "spatial", "logic"], decision["validation_focus"])
        self.assertTrue(any("manual review" in note for note in decision["notes"]))

    def test_previous_repair_budget_exhausted_increases_budget(self) -> None:
        decision = decide_next_step(
            {"chapter_index": 2, "world_state": {}, "characters": {}, "timeline": []},
            {
                "source": "test",
                "status": "ready",
                "items": [],
                "last_run": {
                    "status": "rejected",
                    "committed": False,
                    "problem_codes": ["missing_conflict_marker"],
                    "blocking_problem_count": 1,
                    "severity_counts": [{"severity": "high", "count": 1}],
                    "repair_plan": {
                        "risk_level": "high",
                        "repair_budget": 3,
                        "attempt": 3,
                        "manual_review_count": 0,
                    },
                },
            },
        )

        self.assertEqual(4, decision["max_repair_attempts"])
        self.assertTrue(any("consumed its budget" in note for note in decision["notes"]))

    def test_current_snapshot_builder_memory_quality_risk_skips_polish(self) -> None:
        decision = decide_next_step(
            {"chapter_index": 2, "world_state": {}, "characters": {}, "timeline": []},
            {
                "source": "test",
                "status": "ready",
                "items": [],
                "snapshot_builder_audit": {
                    "skipped_count": 2,
                    "skipped_type_counts": [{"type": "location", "count": 2}],
                    "skipped_reason_counts": [{"reason_code": "missing_name", "count": 2}],
                    "skipped_severity_counts": [{"severity": "medium", "count": 2}],
                    "skipped_blocking_count": 0,
                },
            },
        )

        self.assertEqual("resolve_memory_quality_risk", decision["goal"])
        self.assertEqual(["generate_chapter", "validate", "repair_if_needed"], decision["actions"])
        self.assertEqual(2, decision["max_repair_attempts"])
        self.assertEqual(["continuity", "spatial", "logic"], decision["validation_focus"])
        self.assertTrue(any("missing_name=2" in note for note in decision["notes"]))
        self.assertTrue(any("location=2" in note and "blocking=0" in note for note in decision["notes"]))
        self.assertTrue(any("Skip polish" in note for note in decision["notes"]))

    def test_low_duplicate_memory_skip_only_adds_note(self) -> None:
        decision = decide_next_step(
            {"chapter_index": 2, "world_state": {}, "characters": {}, "timeline": []},
            {
                "source": "test",
                "status": "ready",
                "items": [],
                "snapshot_builder_audit": {
                    "skipped_count": 1,
                    "skipped_type_counts": [{"type": "timeline_event", "count": 1}],
                    "skipped_reason_counts": [{"reason_code": "duplicate_memory", "count": 1}],
                    "skipped_severity_counts": [{"severity": "low", "count": 1}],
                    "skipped_blocking_count": 0,
                },
            },
        )

        self.assertEqual("establish_story_baseline", decision["goal"])
        self.assertIn("polish", decision["actions"])
        self.assertEqual(1, decision["max_repair_attempts"])
        self.assertTrue(any("duplicate_memory=1" in note for note in decision["notes"]))
        self.assertTrue(any("timeline_event=1" in note and "blocking=0" in note for note in decision["notes"]))


if __name__ == "__main__":
    unittest.main()
