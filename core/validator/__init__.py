from __future__ import annotations

from typing import Any

from core.schema import validate_schema
from core.story_project.coverage import validate_blueprint_coverage
from core.validator.common import enrich_check
from core.validator.continuity import validate_continuity
from core.validator.llm import validate_llm
from core.validator.logic import validate_logic
from core.validator.spatial import validate_spatial

_VALIDATORS = {
    "continuity": validate_continuity,
    "spatial": validate_spatial,
    "logic": validate_logic,
}
_DEFAULT_FOCUS = ["continuity", "spatial", "logic"]


def validate_chapter(
    snapshot: dict[str, Any],
    chapter_text: str,
    decision: dict[str, Any] | None = None,
    *,
    enable_llm: bool = False,
    llm_validator=validate_llm,
    chapter_blueprint: dict[str, Any] | None = None,
    blueprint_coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested_focus, executed_focus, skipped_checks = _validation_coverage(decision)
    checks = [
        enrich_check(_VALIDATORS[focus](snapshot, chapter_text, decision))
        for focus in executed_focus
    ]
    if isinstance(chapter_blueprint, dict) and isinstance(blueprint_coverage, dict):
        checks.append(enrich_check(validate_blueprint_coverage(chapter_blueprint, blueprint_coverage)))
        executed_focus = [*executed_focus, "story_project"]
    if enable_llm:
        checks.append(llm_validator(snapshot, chapter_text, decision))
        executed_focus = [*executed_focus, "llm"]
    problems = [problem for check in checks for problem in check.get("problems", [])]
    repair_counts = _repair_action_counts(problems)
    return validate_schema({
        "ok": not any(problem.get("blocking") for problem in problems),
        "requested_focus": requested_focus,
        "executed_checks": executed_focus,
        "skipped_checks": skipped_checks,
        "checks": checks,
        "problems": problems,
        "blocking_problem_count": sum(1 for problem in problems if problem.get("blocking")),
        "warning_count": sum(1 for problem in problems if not problem.get("blocking")),
        "severity_counts": _severity_counts(problems),
        "deterministic_repair_count": sum(1 for problem in problems if problem.get("repair_action") != "manual_review"),
        "manual_review_count": sum(1 for problem in problems if problem.get("repair_action") == "manual_review"),
        "repair_action_counts": repair_counts,
    }, "validation_result.schema.json")


def _validation_coverage(decision: dict[str, Any] | None) -> tuple[list[str], list[str], list[str]]:
    requested_focus = _requested_validation_focus(decision)
    executed_focus = _normalized_validation_focus(requested_focus)
    skipped_checks = [name for name in _DEFAULT_FOCUS if name not in executed_focus]
    return requested_focus, executed_focus, skipped_checks


def _requested_validation_focus(decision: dict[str, Any] | None) -> list[str]:
    if not isinstance(decision, dict):
        return list(_DEFAULT_FOCUS)

    raw_focus = decision.get("validation_focus")
    if not isinstance(raw_focus, list) or not raw_focus:
        return list(_DEFAULT_FOCUS)

    requested: list[str] = []
    for item in raw_focus:
        key = str(item)
        if key in _VALIDATORS and key not in requested:
            requested.append(key)

    return requested or list(_DEFAULT_FOCUS)


def _normalized_validation_focus(requested_focus: list[str]) -> list[str]:
    focus: list[str] = []
    for item in requested_focus:
        if item in _VALIDATORS and item not in focus:
            focus.append(item)
    return focus or list(_DEFAULT_FOCUS)


def _severity_counts(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    order = ["critical", "high", "medium", "low"]
    for problem in problems:
        severity = str(problem.get("severity") or "medium")
        counts[severity] = counts.get(severity, 0) + 1
    return [
        {"severity": severity, "count": counts[severity]}
        for severity in order
        if severity in counts
    ]


def _repair_action_counts(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for problem in problems:
        action = str(problem.get("repair_action") or "manual_review")
        counts[action] = counts.get(action, 0) + 1
    return [
        {"action": action, "count": counts[action]}
        for action in sorted(counts)
    ]


__all__ = ["validate_chapter"]
