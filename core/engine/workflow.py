from __future__ import annotations

from typing import Any

from core.schema import SchemaValidationError, validate_schema


DEFAULT_ACTIONS = ["generate_chapter", "polish", "validate", "repair_if_needed"]
REQUIRED_ACTIONS = {"generate_chapter", "validate"}
ACTION_METADATA = {
    "generate_chapter": {
        "requires": [],
        "produces": ["chapter"],
        "purpose": "Generate draft chapter prose from the input pack.",
        "mode": "required",
        "skippable": False,
        "skip_condition": None,
        "failure_policy": "fail_run",
    },
    "polish": {
        "requires": ["chapter"],
        "produces": ["chapter"],
        "purpose": "Refine chapter prose without changing the planned facts.",
        "mode": "optional",
        "skippable": True,
        "skip_condition": "Director omits polish when recovery or repair should run before prose refinement.",
        "failure_policy": "fail_run",
    },
    "validate": {
        "requires": ["chapter"],
        "produces": ["validation"],
        "purpose": "Check continuity, spatial, and logic constraints selected by the Director.",
        "mode": "required",
        "skippable": False,
        "skip_condition": None,
        "failure_policy": "fail_run",
    },
    "repair_if_needed": {
        "requires": ["chapter", "validation"],
        "produces": ["chapter", "validation"],
        "purpose": "Repair failed validation problems within the Director repair budget.",
        "mode": "conditional",
        "skippable": True,
        "skip_condition": "Runs only when validation is not ok and max_repair_attempts is greater than 0.",
        "failure_policy": "fail_run",
    },
}


class WorkflowError(ValueError):
    pass


def build_workflow(decision: dict[str, Any]) -> list[str]:
    actions = DEFAULT_ACTIONS if decision.get("actions") is None else decision.get("actions")
    workflow = [str(action) for action in actions]
    validate_workflow(workflow)
    return workflow


def build_workflow_plan(decision: dict[str, Any]) -> dict[str, Any]:
    workflow = build_workflow(decision)
    goal = str(decision.get("goal") or "")
    plan = {
        "goal": goal,
        "actions": workflow,
        "steps": [
            {
                "index": index,
                "action": action,
                **ACTION_METADATA[action],
            }
            for index, action in enumerate(workflow, start=1)
        ],
        "validation_focus": list(decision.get("validation_focus") or []),
        "max_repair_attempts": int(decision.get("max_repair_attempts") or 0),
        "recovery": goal.startswith("recover_from_"),
    }
    return validate_workflow_plan(plan)


def validate_workflow_plan(plan: dict[str, Any]) -> dict[str, Any]:
    try:
        validate_schema(plan, "workflow_plan.schema.json")
    except SchemaValidationError as exc:
        raise WorkflowError(str(exc)) from exc

    actions = plan.get("actions")
    steps = plan.get("steps")
    if not isinstance(actions, list) or not isinstance(steps, list):
        raise WorkflowError("workflow plan actions and steps must be arrays")

    step_actions: list[str] = []
    errors: list[str] = []
    for expected_index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            errors.append(f"step {expected_index} must be an object")
            continue
        action = step.get("action")
        step_actions.append(str(action))
        if step.get("index") != expected_index:
            errors.append(f"step {expected_index} has non-contiguous index {step.get('index')}")
        metadata = ACTION_METADATA.get(str(action))
        if metadata is None:
            continue
        for key, expected in metadata.items():
            if step.get(key) != expected:
                errors.append(f"step {expected_index} {action}.{key} does not match action metadata")

    if [str(action) for action in actions] != step_actions:
        errors.append("workflow plan actions must match step actions in order")

    if errors:
        raise WorkflowError("; ".join(errors))
    return plan


def validate_workflow(workflow: list[str]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()

    for index, action in enumerate(workflow):
        if action not in ACTION_METADATA:
            errors.append(f"unknown action: {action}")
            continue

        if action in seen:
            errors.append(f"duplicate action: {action}")
        seen.add(action)

        if action in {"polish", "validate", "repair_if_needed"} and "generate_chapter" not in seen:
            errors.append(f"{action} requires generate_chapter before it")
        if action == "repair_if_needed" and "validate" not in seen:
            errors.append("repair_if_needed requires validate before it")
        if "validate" in seen and action not in {"validate", "repair_if_needed"} and index > workflow.index("validate"):
            errors.append(f"{action} cannot run after validate")

    missing_actions = sorted(REQUIRED_ACTIONS - seen)
    if missing_actions:
        errors.append(f"missing required actions: {missing_actions}")

    if errors:
        raise WorkflowError("; ".join(errors))

    return workflow


__all__ = [
    "ACTION_METADATA",
    "DEFAULT_ACTIONS",
    "REQUIRED_ACTIONS",
    "WorkflowError",
    "build_workflow",
    "build_workflow_plan",
    "validate_workflow_plan",
    "validate_workflow",
]
