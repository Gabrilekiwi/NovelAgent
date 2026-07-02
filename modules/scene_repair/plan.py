from __future__ import annotations

from typing import Any

from core.schema import validate_schema


REPAIR_ACTIONS = {
    "empty_chapter": {
        "action": "seed_conflict_scene",
        "priority": 10,
        "strategy": "Create a minimal scene with danger, choice, and conflict.",
    },
    "chapter_too_short": {
        "action": "expand_scene",
        "priority": 20,
        "strategy": "Add consequence, cost, and concrete plot movement.",
    },
    "missing_conflict_marker": {
        "action": "add_conflict_signal",
        "priority": 30,
        "strategy": "Add explicit danger, choice, threat, secret, cost, or conflict.",
    },
    "forbidden_constraint_term": {
        "action": "remove_forbidden_term",
        "priority": 40,
        "strategy": "Replace the forbidden term while preserving unresolved tension.",
    },
    "missing_required_constraint_term": {
        "action": "add_required_term",
        "priority": 50,
        "strategy": "Mention the required term without resolving the constraint.",
    },
    "no_known_location": {
        "action": "anchor_known_location",
        "priority": 60,
        "strategy": "Anchor the scene to a known location or alias from the snapshot.",
    },
    "character_unknown_location": {
        "action": "flag_unknown_location",
        "priority": 70,
        "strategy": "Avoid relying on an unknown character location; keep the scene spatially explicit.",
    },
    "character_location_not_mentioned": {
        "action": "add_character_location",
        "priority": 80,
        "strategy": "Mention the character with their current known location.",
    },
    "missing_opening_bridge": {
        "action": "insert_opening_bridge",
        "priority": 55,
        "strategy": "Insert a direct opening bridge from the previous chapter ending before any new scene movement.",
    },
    "unexplained_location_shift": {
        "action": "rewrite_spatial_transition",
        "priority": 56,
        "strategy": "Rewrite the opening movement so the route from the last location to the new location is explicit.",
    },
    "invalid_spatial_transition": {
        "action": "add_transition_event",
        "priority": 57,
        "strategy": "Add a transition event that explains a valid unblocked route between spaces.",
    },
    "missing_last_scene_continuity": {
        "action": "anchor_last_scene_state",
        "priority": 58,
        "strategy": "Anchor the opening to the last scene location, characters, and immediate consequence.",
    },
    "character_position_conflict": {
        "action": "repair_character_position",
        "priority": 59,
        "strategy": "Correct the character's position or add a valid transition before their action.",
    },
    "inactive_character_action": {
        "action": "rewrite_inactive_character_action",
        "priority": 90,
        "strategy": "Replace direct action with absence, memory, consequence, or another active character's reaction.",
    },
    "chapter_index_mismatch": {
        "action": "correct_chapter_index",
        "priority": 100,
        "strategy": "Correct the declared chapter number to match the runtime snapshot.",
    },
}

PARAMETER_FIELDS = {
    "seed_conflict_scene": (),
    "expand_scene": (),
    "add_conflict_signal": (),
    "remove_forbidden_term": ("term",),
    "add_required_term": ("term",),
    "anchor_known_location": ("suggested_term",),
    "insert_opening_bridge": ("bridge", "location"),
    "rewrite_spatial_transition": ("expected", "actual"),
    "anchor_last_scene_state": ("location", "character"),
    "repair_character_position": ("character", "expected", "actual"),
    "add_transition_event": ("expected", "actual"),
    "flag_unknown_location": ("character", "location"),
    "add_character_location": ("character", "location"),
    "rewrite_inactive_character_action": ("character",),
    "correct_chapter_index": ("expected", "actual"),
    "manual_review": ("raw_problem",),
}


SEVERITY_ORDER = ("critical", "high", "medium", "low")


def build_repair_plan(
    validation: dict[str, Any],
    *,
    repair_budget: int | None = None,
    attempt: int | None = None,
    recovery_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    problems = [problem for problem in validation.get("problems", []) if isinstance(problem, dict)]
    steps = [_repair_step(index, problem) for index, problem in enumerate(problems, start=1)]
    steps.sort(key=lambda step: (step["priority"], step["index"]))
    severity_counts = _severity_counts(steps)
    manual_review_count = sum(1 for step in steps if step["action"] == "manual_review")
    plan = {
        "problem_count": len(problems),
        "blocking_problem_count": sum(1 for step in steps if step["blocking"]),
        "warning_count": sum(1 for step in steps if not step["blocking"]),
        "severity_counts": severity_counts,
        "risk_level": _risk_level(severity_counts),
        "repair_budget": repair_budget,
        "attempt": attempt,
        "deterministic_step_count": len(steps) - manual_review_count,
        "manual_review_count": manual_review_count,
        "actions": [step["action"] for step in steps],
        "recovery": _recovery_summary(steps, recovery_context),
        "steps": steps,
    }
    return validate_schema(plan, "repair_plan.schema.json")


def _repair_step(index: int, problem: dict[str, Any]) -> dict[str, Any]:
    code = str(problem.get("code") or "unknown")
    metadata = REPAIR_ACTIONS.get(
        code,
        {
            "action": "manual_review",
            "priority": 1000,
            "strategy": "No deterministic repair strategy is registered for this validation problem.",
        },
    )
    action = _repair_action(problem, str(metadata["action"]))
    return {
        "index": index,
        "code": code,
        "message": str(problem.get("message") or ""),
        "validator": str(problem.get("validator") or ""),
        "severity": str(problem.get("severity") or "medium"),
        "blocking": bool(problem.get("blocking", True)),
        "repair_hint": str(problem.get("repair_hint") or metadata["strategy"]),
        "evidence": _problem_evidence(problem),
        "action": action,
        "priority": metadata["priority"],
        "strategy": metadata["strategy"],
        "parameters": _problem_parameters(problem, action),
    }


def _repair_action(problem: dict[str, Any], default_action: str) -> str:
    action = str(problem.get("repair_action") or default_action)
    return action if action in PARAMETER_FIELDS else default_action


def _problem_parameters(problem: dict[str, Any], action: str) -> dict[str, Any]:
    repair_parameters = problem.get("repair_parameters")
    if isinstance(repair_parameters, dict):
        return _filter_parameters(repair_parameters, action)
    fields = PARAMETER_FIELDS.get(action, ())
    if action == "manual_review":
        return {"raw_problem": _raw_problem_parameters(problem)}
    return {field: str(problem.get(field) or "") for field in fields}


def _filter_parameters(parameters: dict[str, Any], action: str) -> dict[str, Any]:
    fields = PARAMETER_FIELDS.get(action, ())
    if action == "manual_review":
        raw_problem = parameters.get("raw_problem")
        return {"raw_problem": raw_problem if isinstance(raw_problem, dict) else {}}
    return {field: str(parameters.get(field) or "") for field in fields}


def _raw_problem_parameters(problem: dict[str, Any]) -> dict[str, Any]:
    ignored = {
        "code",
        "message",
        "validator",
        "severity",
        "blocking",
        "category",
        "repair_hint",
        "repair_action",
        "repair_parameters",
        "evidence",
    }
    return {str(key): value for key, value in problem.items() if key not in ignored}


def _problem_evidence(problem: dict[str, Any]) -> list[dict[str, str]]:
    evidence = problem.get("evidence")
    if not isinstance(evidence, list):
        return []
    result: list[dict[str, str]] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        value = str(item.get("value") or "").strip()
        if kind and value:
            result.append({"kind": kind, "value": value})
    return result


def _severity_counts(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for step in steps:
        severity = str(step.get("severity") or "medium")
        counts[severity] = counts.get(severity, 0) + 1
    return [{"severity": severity, "count": counts[severity]} for severity in SEVERITY_ORDER if severity in counts]


def _risk_level(severity_counts: list[dict[str, Any]]) -> str:
    present = {item.get("severity") for item in severity_counts if isinstance(item, dict)}
    for severity in SEVERITY_ORDER:
        if severity in present:
            return severity
    return "none"


def _recovery_summary(steps: list[dict[str, Any]], recovery_context: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(recovery_context, dict) or not bool(recovery_context.get("available")):
        return {
            "available": False,
            "source_run_id": None,
            "source_status": None,
            "source_problem_codes": [],
            "repeated_problem_codes": [],
            "unresolved_problem_codes": [],
            "new_problem_codes": [],
            "skipped_checks": [],
            "previous_repair_attempts": 0,
            "previous_repair_risk_level": None,
            "previous_manual_review_count": 0,
            "repair_stalled": False,
            "repair_introduced_new_problems": False,
            "repair_budget_exhausted": False,
            "failure_modes": [],
        }

    current_codes = [str(step.get("code")) for step in steps if step.get("code")]
    source_problem_codes = _string_list(recovery_context.get("problem_codes"))
    repair_deltas = [delta for delta in recovery_context.get("repair_deltas", []) if isinstance(delta, dict)]
    unresolved_problem_codes = _unique_strings(
        code
        for delta in repair_deltas
        for code in _string_list(delta.get("remaining_problem_codes"))
    )
    new_problem_codes = _unique_strings(
        code
        for delta in repair_deltas
        for code in _string_list(delta.get("new_problem_codes"))
    )
    repeated_problem_codes = _unique_strings(code for code in current_codes if code in source_problem_codes)
    repair_plan = recovery_context.get("repair_plan") if isinstance(recovery_context.get("repair_plan"), dict) else {}
    previous_repair_attempts = _int_or_zero(recovery_context.get("repair_attempts"))
    repair_budget = _int_or_zero(repair_plan.get("repair_budget"))
    repair_attempt = _int_or_zero(repair_plan.get("attempt"))
    repair_budget_exhausted = bool(repair_budget and repair_attempt >= repair_budget and not recovery_context.get("committed"))
    repair_stalled = bool(unresolved_problem_codes and not new_problem_codes)
    repair_introduced_new_problems = bool(new_problem_codes)
    previous_manual_review_count = _int_or_zero(repair_plan.get("manual_review_count"))
    skipped_checks = _validation_names(recovery_context.get("skipped_checks"))

    return {
        "available": True,
        "source_run_id": recovery_context.get("source_run_id"),
        "source_status": recovery_context.get("status"),
        "source_problem_codes": source_problem_codes,
        "repeated_problem_codes": repeated_problem_codes,
        "unresolved_problem_codes": unresolved_problem_codes,
        "new_problem_codes": new_problem_codes,
        "skipped_checks": skipped_checks,
        "previous_repair_attempts": previous_repair_attempts,
        "previous_repair_risk_level": repair_plan.get("risk_level"),
        "previous_manual_review_count": previous_manual_review_count,
        "repair_stalled": repair_stalled,
        "repair_introduced_new_problems": repair_introduced_new_problems,
        "repair_budget_exhausted": repair_budget_exhausted,
        "failure_modes": _failure_modes(
            repeated_problem_codes=repeated_problem_codes,
            unresolved_problem_codes=unresolved_problem_codes,
            new_problem_codes=new_problem_codes,
            skipped_checks=skipped_checks,
            previous_manual_review_count=previous_manual_review_count,
            repair_budget_exhausted=repair_budget_exhausted,
        ),
    }


def _failure_modes(
    *,
    repeated_problem_codes: list[str],
    unresolved_problem_codes: list[str],
    new_problem_codes: list[str],
    skipped_checks: list[str],
    previous_manual_review_count: int,
    repair_budget_exhausted: bool,
) -> list[str]:
    modes: list[str] = []
    if repeated_problem_codes:
        modes.append("previous_problem_repeated")
    if unresolved_problem_codes:
        modes.append("previous_repair_stalled")
    if new_problem_codes:
        modes.append("previous_repair_introduced_new_problems")
    if skipped_checks:
        modes.append("previous_validation_skipped")
    if previous_manual_review_count > 0:
        modes.append("previous_manual_review_required")
    if repair_budget_exhausted:
        modes.append("previous_repair_budget_exhausted")
    return modes


def _validation_names(value: Any) -> list[str]:
    names = _string_list(value)
    return [name for name in ("continuity", "spatial", "logic") if name in names]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


def _unique_strings(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value)
        if text and text not in result:
            result.append(text)
    return result


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


__all__ = ["PARAMETER_FIELDS", "REPAIR_ACTIONS", "build_repair_plan"]
