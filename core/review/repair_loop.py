from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.schema import validate_schema
from modules.scene_repair import build_repair_plan


REPAIR_TRIGGER_STATUSES = {"needs_revision", "blocked"}
REPAIR_ACCEPT_STATUSES = {"pass", "warning"}

RepairCallback = Callable[[str, dict[str, Any], dict[str, Any]], str]
ValidateCallback = Callable[[str], dict[str, Any]]
ReviewCallback = Callable[[str, int], dict[str, Any]]


@dataclass(frozen=True)
class ReviewRepairConfig:
    enabled: bool = False
    max_attempts: int = 1
    dry_run: bool = False


def validate_review_repair_config(config: ReviewRepairConfig) -> ReviewRepairConfig:
    if config.max_attempts < 1 or config.max_attempts > 3:
        raise ValueError("--review-repair-max-attempts must be between 1 and 3")
    if config.dry_run and not config.enabled:
        raise ValueError("--review-repair-dry-run requires --review-auto-repair")
    return config


def disabled_review_repair() -> dict[str, Any]:
    return {
        "enabled": False,
        "attempted": False,
        "accepted": False,
        "dry_run": False,
        "attempt_count": 0,
        "max_attempts": 0,
        "trigger_status": None,
        "before_review": None,
        "after_review": None,
        "repair_plan": None,
        "repair_deltas": [],
        "errors": [],
        "rejected_reason": None,
        "artifacts": {},
    }


def run_review_repair_loop(
    *,
    chapter_text: str,
    validation: dict[str, Any],
    before_review: dict[str, Any],
    config: ReviewRepairConfig,
    repair: RepairCallback,
    validate: ValidateCallback,
    review: ReviewCallback,
) -> dict[str, Any]:
    config = validate_review_repair_config(config)
    trigger_status = str(before_review.get("status") or "")
    if not config.enabled:
        return disabled_review_repair()
    if trigger_status not in REPAIR_TRIGGER_STATUSES:
        result = disabled_review_repair()
        result.update(
            {
                "enabled": True,
                "max_attempts": config.max_attempts,
                "trigger_status": trigger_status or None,
                "before_review": before_review,
                "rejected_reason": "review_status_does_not_require_repair",
            }
        )
        return result

    repair_plan = build_review_repair_plan(
        before_review=before_review,
        validation=validation,
        attempt=1,
        max_attempts=config.max_attempts,
        dry_run=config.dry_run,
    )
    if config.dry_run:
        return {
            "enabled": True,
            "attempted": True,
            "accepted": False,
            "dry_run": True,
            "attempt_count": 0,
            "max_attempts": config.max_attempts,
            "trigger_status": trigger_status,
            "before_review": before_review,
            "after_review": None,
            "repair_plan": repair_plan,
            "repair_deltas": [],
            "errors": [],
            "rejected_reason": "review_repair_dry_run",
            "artifacts": {},
            "final_chapter": chapter_text,
            "final_validation": validation,
            "final_review": before_review,
        }

    current_chapter = chapter_text
    current_validation = validation
    current_review = before_review
    repair_deltas: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for attempt in range(1, config.max_attempts + 1):
        attempt_plan = repair_plan if attempt == 1 else build_review_repair_plan(
            before_review=current_review,
            validation=current_validation,
            attempt=attempt,
            max_attempts=config.max_attempts,
            dry_run=False,
        )
        before_chapter = current_chapter
        before_validation = current_validation
        before_attempt_review = current_review
        try:
            repaired_chapter = repair(current_chapter, current_validation, attempt_plan)
            repaired_validation = validate(repaired_chapter)
            repaired_review = review(repaired_chapter, attempt)
        except Exception as exc:  # noqa: BLE001 - repair attempts are audited rather than hidden.
            errors.append({"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"})
            repair_deltas.append(
                _attempt_delta(
                    attempt=attempt,
                    before_chapter=before_chapter,
                    after_chapter=current_chapter,
                    before_validation=before_validation,
                    after_validation=current_validation,
                    before_review=before_attempt_review,
                    after_review=current_review,
                    accepted=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            break

        accepted = _accepted(validation=repaired_validation, review=repaired_review)
        repair_deltas.append(
            _attempt_delta(
                attempt=attempt,
                before_chapter=before_chapter,
                after_chapter=repaired_chapter,
                before_validation=before_validation,
                after_validation=repaired_validation,
                before_review=before_attempt_review,
                after_review=repaired_review,
                accepted=accepted,
                error=None,
            )
        )
        current_chapter = repaired_chapter
        current_validation = repaired_validation
        current_review = repaired_review
        if accepted:
            break

    accepted = _accepted(validation=current_validation, review=current_review)
    return {
        "enabled": True,
        "attempted": True,
        "accepted": accepted,
        "dry_run": False,
        "attempt_count": len(repair_deltas),
        "max_attempts": config.max_attempts,
        "trigger_status": trigger_status,
        "before_review": before_review,
        "after_review": current_review,
        "repair_plan": repair_plan,
        "repair_deltas": repair_deltas,
        "errors": errors,
        "rejected_reason": None if accepted else _rejected_reason(current_validation, current_review, errors),
        "artifacts": {},
        "final_chapter": current_chapter,
        "final_validation": current_validation,
        "final_review": current_review,
    }


def build_review_repair_plan(
    *,
    before_review: dict[str, Any],
    validation: dict[str, Any],
    attempt: int,
    max_attempts: int,
    dry_run: bool,
) -> dict[str, Any]:
    review_tasks = _review_tasks(before_review)
    synthetic_validation = _synthetic_validation(review_tasks, validation)
    scene_repair_plan = build_repair_plan(
        synthetic_validation,
        repair_budget=max_attempts,
        attempt=attempt,
    )
    return {
        "enabled": True,
        "source": "review_pipeline",
        "attempt": attempt,
        "max_attempts": max_attempts,
        "review_status_before": before_review.get("status"),
        "review_decision_before": before_review.get("decision"),
        "blocking_task_count": int(before_review.get("blocking_task_count") or 0),
        "repair_tasks": review_tasks,
        "risk_level": scene_repair_plan.get("risk_level"),
        "dry_run": dry_run,
        "scene_repair_plan": scene_repair_plan,
    }


def _review_tasks(before_review: dict[str, Any]) -> list[dict[str, Any]]:
    rule_plan = _load_rule_repair_plan(before_review)
    raw_tasks = rule_plan.get("tasks") if isinstance(rule_plan, dict) else None
    if isinstance(raw_tasks, list) and raw_tasks:
        tasks = []
        for item in raw_tasks:
            if not isinstance(item, dict):
                continue
            tasks.append(
                {
                    "id": str(item.get("task_id") or item.get("id") or f"review_task_{len(tasks) + 1:03d}"),
                    "severity": str(item.get("severity") or "medium"),
                    "category": str(item.get("category") or item.get("repair_type") or "review"),
                    "instruction": str(item.get("instruction") or item.get("title") or "Revise chapter according to review findings."),
                    "evidence": item.get("evidence") if isinstance(item.get("evidence"), (dict, list)) else [],
                    "target_scope": "chapter",
                    "source_review_task_id": str(item.get("task_id") or ""),
                    "repair_type": str(item.get("repair_type") or "manual_review"),
                    "blocking": bool(item.get("blocking")),
                }
            )
        if tasks:
            return tasks
    return [
        {
            "id": "review_task_001",
            "severity": "high" if before_review.get("status") == "blocked" else "medium",
            "category": "review",
            "instruction": "Revise chapter to resolve review pipeline findings.",
            "evidence": [],
            "target_scope": "chapter",
            "source_review_task_id": "",
            "repair_type": "manual_review",
            "blocking": bool(before_review.get("blocking_task_count")),
        }
    ]


def _load_rule_repair_plan(before_review: dict[str, Any]) -> dict[str, Any] | None:
    summary_path = before_review.get("summary_path")
    if not summary_path:
        return None
    try:
        summary = json.loads(Path(str(summary_path)).read_text(encoding="utf-8-sig"))
        artifacts = summary.get("artifacts") if isinstance(summary, dict) else {}
        plan_path = artifacts.get("rule_repair_plan") if isinstance(artifacts, dict) else None
        if not plan_path:
            return None
        return json.loads(Path(str(plan_path)).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _synthetic_validation(review_tasks: list[dict[str, Any]], validation: dict[str, Any]) -> dict[str, Any]:
    problems = []
    for index, task in enumerate(review_tasks, start=1):
        problems.append(
            {
                "code": _problem_code_for_task(task),
                "message": str(task.get("instruction") or "Review repair task."),
                "validator": "review_pipeline",
                "severity": str(task.get("severity") or "medium"),
                "blocking": bool(task.get("blocking", True)),
                "category": "blocking" if bool(task.get("blocking", True)) else "warning",
                "repair_action": _repair_action_for_task(task),
                "repair_hint": str(task.get("instruction") or "Revise chapter according to review."),
                "repair_parameters": {},
                "evidence": _evidence(task, index),
            }
        )
    return validate_schema(
        {
            "ok": False,
            "requested_focus": list(validation.get("requested_focus") or ["logic"]),
            "executed_checks": list(validation.get("executed_checks") or ["logic"]),
            "skipped_checks": list(validation.get("skipped_checks") or []),
            "checks": [{"name": "review_pipeline", "ok": False, "problems": problems}],
            "problems": problems,
            "blocking_problem_count": sum(1 for problem in problems if problem["blocking"]),
            "warning_count": sum(1 for problem in problems if not problem["blocking"]),
            "severity_counts": _severity_counts(problems),
            "deterministic_repair_count": sum(1 for problem in problems if problem["repair_action"] != "manual_review"),
            "manual_review_count": sum(1 for problem in problems if problem["repair_action"] == "manual_review"),
            "repair_action_counts": _repair_action_counts(problems),
        },
        "validation_result.schema.json",
    )


def _problem_code_for_task(task: dict[str, Any]) -> str:
    repair_type = str(task.get("repair_type") or "")
    if repair_type in {"advance_conflict_or_thread", "reduce_repetition_and_stalling"}:
        return "missing_conflict_marker"
    if repair_type in {"adjust_chapter_length"}:
        return "chapter_too_short"
    if repair_type in {"strengthen_opening_continuity"}:
        return "missing_opening_bridge"
    if repair_type in {"fix_location_transition"}:
        return "unexplained_location_shift"
    return "review_repair_task"


def _repair_action_for_task(task: dict[str, Any]) -> str:
    repair_type = str(task.get("repair_type") or "")
    mapping = {
        "advance_conflict_or_thread": "add_conflict_signal",
        "reduce_repetition_and_stalling": "add_conflict_signal",
        "adjust_chapter_length": "expand_scene",
        "strengthen_opening_continuity": "insert_opening_bridge",
        "fix_location_transition": "rewrite_spatial_transition",
    }
    return mapping.get(repair_type, "manual_review")


def _accepted(*, validation: dict[str, Any], review: dict[str, Any]) -> bool:
    return bool(validation.get("ok")) and str(review.get("status")) in REPAIR_ACCEPT_STATUSES


def _rejected_reason(validation: dict[str, Any], review: dict[str, Any], errors: list[dict[str, Any]]) -> str:
    if errors:
        return "repairer_failed"
    if not validation.get("ok"):
        codes = [str(problem.get("code")) for problem in validation.get("problems", []) if isinstance(problem, dict)]
        if "missing_required_beat" in codes:
            return "post_repair_missing_required_beat"
        if "missing_ending_pressure" in codes:
            return "post_repair_missing_ending_pressure"
        return "post_repair_validation_failed"
    if str(review.get("status")) not in REPAIR_ACCEPT_STATUSES:
        return "post_repair_review_blocked"
    return "review_repair_rejected"


def _attempt_delta(
    *,
    attempt: int,
    before_chapter: str,
    after_chapter: str,
    before_validation: dict[str, Any],
    after_validation: dict[str, Any],
    before_review: dict[str, Any],
    after_review: dict[str, Any],
    accepted: bool,
    error: str | None,
) -> dict[str, Any]:
    before_codes = _problem_codes(before_validation)
    after_codes = _problem_codes(after_validation)
    return {
        "attempt": attempt,
        "chars_before": len(before_chapter),
        "chars_after": len(after_chapter),
        "changed": before_chapter != after_chapter,
        "before_validation_ok": bool(before_validation.get("ok")),
        "after_validation_ok": bool(after_validation.get("ok")),
        "before_review_status": before_review.get("status"),
        "after_review_status": after_review.get("status"),
        "resolved_problem_codes": sorted(set(before_codes) - set(after_codes)),
        "new_problem_codes": sorted(set(after_codes) - set(before_codes)),
        "remaining_problem_codes": sorted(set(before_codes) & set(after_codes)),
        "accepted": accepted,
        "error": error,
    }


def _problem_codes(validation: dict[str, Any]) -> list[str]:
    return [str(problem.get("code")) for problem in validation.get("problems", []) if isinstance(problem, dict)]


def _evidence(task: dict[str, Any], index: int) -> list[dict[str, str]]:
    raw = task.get("evidence")
    if isinstance(raw, dict):
        return [{"kind": str(key), "value": str(value)} for key, value in raw.items() if value not in (None, "")]
    if isinstance(raw, list):
        result = []
        for item in raw:
            if isinstance(item, dict):
                kind = str(item.get("kind") or item.get("code") or "review")
                value = str(item.get("value") or item.get("message") or item)
            else:
                kind = "review"
                value = str(item)
            if value:
                result.append({"kind": kind, "value": value})
        return result
    return [{"kind": "review_task", "value": str(task.get("id") or index)}]


def _severity_counts(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for problem in problems:
        severity = str(problem.get("severity") or "medium")
        counts[severity] = counts.get(severity, 0) + 1
    return [{"severity": severity, "count": counts[severity]} for severity in ("critical", "high", "medium", "low") if severity in counts]


def _repair_action_counts(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for problem in problems:
        action = str(problem.get("repair_action") or "manual_review")
        counts[action] = counts.get(action, 0) + 1
    return [{"action": action, "count": counts[action]} for action in sorted(counts)]


__all__ = [
    "REPAIR_ACCEPT_STATUSES",
    "REPAIR_TRIGGER_STATUSES",
    "ReviewRepairConfig",
    "build_review_repair_plan",
    "disabled_review_repair",
    "run_review_repair_loop",
    "validate_review_repair_config",
]
