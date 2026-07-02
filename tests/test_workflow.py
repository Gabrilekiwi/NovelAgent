from __future__ import annotations

import unittest

from core.engine.workflow import WorkflowError, build_workflow, build_workflow_plan, validate_workflow_plan
from core.schema import validate_schema
from workflows.dynamic_flow import build_dynamic_flow, build_dynamic_flow_plan


class WorkflowTest(unittest.TestCase):
    def test_builds_director_ordered_workflow(self) -> None:
        workflow = build_workflow(
            {
                "actions": ["generate_chapter", "polish", "validate", "repair_if_needed"],
            }
        )

        self.assertEqual(
            ["generate_chapter", "polish", "validate", "repair_if_needed"],
            workflow,
        )

    def test_missing_actions_uses_default_workflow(self) -> None:
        self.assertEqual(
            ["generate_chapter", "polish", "validate", "repair_if_needed"],
            build_workflow({}),
        )

    def test_builds_auditable_workflow_plan(self) -> None:
        plan = build_workflow_plan(
            {
                "goal": "recover_from_rejected_run",
                "actions": ["generate_chapter", "validate", "repair_if_needed"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 2,
            }
        )

        self.assertEqual("recover_from_rejected_run", plan["goal"])
        self.assertTrue(plan["recovery"])
        self.assertEqual(["generate_chapter", "validate", "repair_if_needed"], plan["actions"])
        self.assertEqual(["logic"], plan["validation_focus"])
        self.assertEqual(2, plan["max_repair_attempts"])
        self.assertEqual("generate_chapter", plan["steps"][0]["action"])
        self.assertEqual(["chapter"], plan["steps"][0]["produces"])
        self.assertEqual("required", plan["steps"][0]["mode"])
        self.assertFalse(plan["steps"][0]["skippable"])
        self.assertIn("validation", plan["steps"][2]["requires"])
        self.assertEqual("conditional", plan["steps"][2]["mode"])
        self.assertTrue(plan["steps"][2]["skippable"])
        self.assertEqual("fail_run", plan["steps"][2]["failure_policy"])
        self.assertIs(plan, validate_schema(plan, "workflow_plan.schema.json"))

    def test_polish_step_is_optional_but_failure_still_fails_run(self) -> None:
        plan = build_workflow_plan(
            {
                "goal": "continue_existing_arc",
                "actions": ["generate_chapter", "polish", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
            }
        )

        polish_step = plan["steps"][1]
        self.assertEqual("polish", polish_step["action"])
        self.assertEqual("optional", polish_step["mode"])
        self.assertTrue(polish_step["skippable"])
        self.assertIn("Director omits polish", polish_step["skip_condition"])
        self.assertEqual("fail_run", polish_step["failure_policy"])
        self.assertIs(plan, validate_schema(plan, "workflow_plan.schema.json"))

    def test_dynamic_flow_uses_workflow_plan_builder(self) -> None:
        decision = {
            "goal": "continue_existing_arc",
            "actions": ["generate_chapter", "polish", "validate", "repair_if_needed"],
            "validation_focus": ["continuity", "logic"],
            "max_repair_attempts": 1,
        }

        self.assertEqual(build_workflow(decision), build_dynamic_flow(decision))
        self.assertEqual(build_workflow_plan(decision), build_dynamic_flow_plan(decision))

    def test_builds_extended_bridge_workflow_plan(self) -> None:
        plan = build_workflow_plan(
            {
                "goal": "continue_existing_arc",
                "actions": [
                    "build_snapshot",
                    "pre_validate_bridge",
                    "generate_chapter",
                    "validate",
                    "repair_if_needed",
                    "commit_snapshot",
                ],
                "validation_focus": ["spatial"],
                "max_repair_attempts": 2,
            }
        )

        self.assertEqual(
            [
                "build_snapshot",
                "pre_validate_bridge",
                "generate_chapter",
                "validate",
                "repair_if_needed",
                "commit_snapshot",
            ],
            plan["actions"],
        )
        self.assertEqual(["snapshot"], plan["steps"][0]["produces"])
        self.assertEqual(["snapshot"], plan["steps"][1]["requires"])
        self.assertEqual(["bridge_validation"], plan["steps"][1]["produces"])
        self.assertEqual("commit_snapshot", plan["steps"][-1]["action"])
        self.assertIs(plan, validate_schema(plan, "workflow_plan.schema.json"))

    def test_rejects_repair_before_validate(self) -> None:
        with self.assertRaises(WorkflowError):
            build_workflow(
                {
                    "actions": ["generate_chapter", "repair_if_needed", "validate"],
                }
            )

    def test_rejects_polish_after_validate(self) -> None:
        with self.assertRaises(WorkflowError):
            build_workflow(
                {
                    "actions": ["generate_chapter", "validate", "polish"],
                }
            )

    def test_rejects_duplicate_action(self) -> None:
        with self.assertRaises(WorkflowError):
            build_workflow(
                {
                    "actions": ["generate_chapter", "generate_chapter", "validate"],
                }
            )

    def test_rejects_unknown_action(self) -> None:
        with self.assertRaises(WorkflowError):
            build_workflow(
                {
                    "actions": ["generate_chapter", "archive_notes"],
                }
            )

    def test_rejects_missing_required_validate_action(self) -> None:
        with self.assertRaises(WorkflowError) as context:
            build_workflow({"actions": ["generate_chapter"]})

        self.assertIn("missing required actions", str(context.exception))
        self.assertIn("validate", str(context.exception))

    def test_rejects_empty_explicit_actions(self) -> None:
        with self.assertRaises(WorkflowError) as context:
            build_workflow({"actions": []})

        self.assertIn("generate_chapter", str(context.exception))
        self.assertIn("validate", str(context.exception))

    def test_workflow_plan_rejects_unknown_validation_focus(self) -> None:
        with self.assertRaises(WorkflowError):
            build_workflow_plan(
                {
                    "goal": "continue_existing_arc",
                    "actions": ["generate_chapter", "validate"],
                    "validation_focus": ["tone"],
                    "max_repair_attempts": 1,
                }
            )

    def test_workflow_plan_rejects_step_action_mismatch(self) -> None:
        plan = build_workflow_plan(
            {
                "goal": "continue_existing_arc",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
            }
        )
        plan["steps"][1]["action"] = "repair_if_needed"

        with self.assertRaises(WorkflowError) as context:
            validate_workflow_plan(plan)

        self.assertIn("actions must match step actions", str(context.exception))

    def test_workflow_plan_rejects_non_contiguous_step_index(self) -> None:
        plan = build_workflow_plan(
            {
                "goal": "continue_existing_arc",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
            }
        )
        plan["steps"][1]["index"] = 4

        with self.assertRaises(WorkflowError) as context:
            validate_workflow_plan(plan)

        self.assertIn("non-contiguous index", str(context.exception))

    def test_workflow_plan_rejects_action_metadata_drift(self) -> None:
        plan = build_workflow_plan(
            {
                "goal": "continue_existing_arc",
                "actions": ["generate_chapter", "polish", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
            }
        )
        plan["steps"][1]["mode"] = "required"

        with self.assertRaises(WorkflowError) as context:
            validate_workflow_plan(plan)

        self.assertIn("does not match action metadata", str(context.exception))


if __name__ == "__main__":
    unittest.main()
