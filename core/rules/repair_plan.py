from __future__ import annotations

import copy
from typing import Any

from core.schema import validate_schema


SEVERITY_ORDER = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}

STATUS_ORDER = {
    "fail": 0,
    "warning": 1,
}

REPAIR_STRATEGIES = {
    "continue_previous_ending": {
        "repair_type": "strengthen_opening_continuity",
        "title": "Strengthen opening continuity",
        "instruction": "Rewrite or adjust the chapter opening so it directly continues the previous ending's location, characters, action, or unresolved event.",
        "requires_human_review": False,
    },
    "preserve_last_scene_location": {
        "repair_type": "fix_location_transition",
        "title": "Fix location transition",
        "instruction": "Add a clear transition from the previous location to the current location, or move the opening back to the previous scene location.",
        "requires_human_review": False,
    },
    "preserve_last_scene_characters": {
        "repair_type": "restore_missing_characters",
        "title": "Restore missing characters",
        "instruction": "Bring back the characters present in the previous scene during the first half of the chapter, or explain their status, action, or exit.",
        "requires_human_review": False,
    },
    "advance_current_conflict": {
        "repair_type": "advance_conflict_or_thread",
        "title": "Advance conflict or thread",
        "instruction": "Add an action, discovery, cost, choice, or complication tied to a current conflict, goal, threat, or foreshadowed thread.",
        "requires_human_review": False,
    },
    "avoid_premature_resolution": {
        "repair_type": "defer_premature_resolution",
        "title": "Defer premature resolution",
        "instruction": "Remove or weaken early reveals, complete resolutions, or final answers; keep only partial clues or staged progress.",
        "requires_human_review": False,
    },
    "prose_only_no_meta_output": {
        "repair_type": "remove_meta_output",
        "title": "Remove non-prose output",
        "instruction": "Remove analysis, summaries, JSON, Markdown headings, author notes, and model explanations; keep only chapter prose.",
        "requires_human_review": False,
    },
    "follow_target_language": {
        "repair_type": "enforce_target_language",
        "title": "Enforce target language",
        "instruction": "Rewrite content that does not match the project target language while preserving chapter prose style.",
        "requires_human_review": False,
    },
    "avoid_repetition_and_stalling": {
        "repair_type": "reduce_repetition_and_stalling",
        "title": "Reduce repetition and stalling",
        "instruction": "Remove repeated paragraphs and empty hesitation; convert repeated internal narration into concrete action, information, or state change.",
        "requires_human_review": False,
    },
    "reasonable_chapter_length": {
        "repair_type": "adjust_chapter_length",
        "title": "Adjust chapter length",
        "instruction": "If too short, add effective scene progress; if too long, compress repeated content while preserving key turns.",
        "requires_human_review": False,
    },
}

QUALITY_CHECK_REPAIR_TYPES = {
    "continues_previous_ending": "continue_previous_ending",
    "preserves_last_scene_location": "preserve_last_scene_location",
    "preserves_last_scene_characters": "preserve_last_scene_characters",
    "advances_open_threads_or_conflicts": "advance_current_conflict",
    "avoids_premature_resolution": "avoid_premature_resolution",
    "no_meta_output": "prose_only_no_meta_output",
    "language_consistency": "follow_target_language",
    "repetition_or_stalling": "avoid_repetition_and_stalling",
    "chapter_length_reasonable": "reasonable_chapter_length",
}

MANUAL_REVIEW_STRATEGY = {
    "repair_type": "manual_review",
    "title": "Manual review",
    "instruction": "No deterministic repair strategy is configured for this rule. Review the evidence and decide the repair approach manually.",
    "requires_human_review": True,
}


class RuleRepairPlanError(ValueError):
    pass


def build_rule_repair_plan(
    *,
    rule_validation_report: dict,
    chapter_text: str | None = None,
    snapshot: dict | None = None,
    max_tasks: int | None = None,
    include_warnings: bool = True,
) -> dict:
    if max_tasks is not None and max_tasks < 0:
        raise RuleRepairPlanError("max_tasks must be >= 0")

    report = validate_schema(copy.deepcopy(rule_validation_report), "rule_validation_report.schema.json")
    _ = chapter_text
    _ = copy.deepcopy(snapshot) if snapshot is not None else None

    rules_by_code = {str(rule["code"]): rule for rule in report["rules"]}
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, violation in enumerate(report["violations"]):
        status = str(violation["status"])
        if status == "warning" and not include_warnings:
            continue
        priority_key = _priority_key(status, str(violation["severity"]))
        candidates.append((priority_key, index, violation))

    ordered = sorted(candidates, key=lambda item: (item[0], item[1]))
    if max_tasks is not None:
        ordered = ordered[:max_tasks]

    tasks = [
        _task_from_violation(
            violation=violation,
            rule_result=rules_by_code.get(str(violation["rule_code"])),
            task_index=task_index,
        )
        for task_index, (_, _, violation) in enumerate(ordered, start=1)
    ]
    summary = _summary(tasks)
    plan = {
        "schema_version": "1.0",
        "status": _plan_status(summary),
        "summary": summary,
        "source_report": {
            "status": report["status"],
            "score": report["score"],
            "rule_pack_id": report["rule_pack"]["rule_pack_id"],
            "violation_count": len(report["violations"]),
        },
        "tasks": tasks,
        "metadata": {
            "created_by": "NovelAgent",
            "source": "rule-aware-repair-plan",
        },
    }
    return validate_schema(plan, "rule_repair_plan.schema.json")


def _task_from_violation(
    *,
    violation: dict[str, Any],
    rule_result: dict[str, Any] | None,
    task_index: int,
) -> dict[str, Any]:
    strategy = _strategy_for_violation(violation)
    status = str(violation["status"])
    severity = str(violation["severity"])
    blocking = status == "fail" and severity in {"critical", "high"}
    requires_human_review = bool(strategy["requires_human_review"])
    if strategy["repair_type"] == "manual_review" and status == "fail":
        requires_human_review = True

    return {
        "task_id": f"repair_{task_index:03d}",
        "rule_code": violation["rule_code"],
        "rule_status": status,
        "severity": severity,
        "category": violation["category"],
        "priority": task_index,
        "repair_type": strategy["repair_type"],
        "title": strategy["title"],
        "instruction": strategy["instruction"],
        "source_quality_check_codes": [str(code) for code in violation.get("quality_check_codes") or []],
        "evidence": _evidence_from_rule_result(rule_result),
        "requires_human_review": requires_human_review,
        "blocking": blocking,
    }


def _strategy_for_violation(violation: dict[str, Any]) -> dict[str, Any]:
    rule_code = str(violation.get("rule_code") or "")
    if rule_code in REPAIR_STRATEGIES:
        return REPAIR_STRATEGIES[rule_code]

    for quality_code in violation.get("quality_check_codes") or []:
        mapped_rule_code = QUALITY_CHECK_REPAIR_TYPES.get(str(quality_code))
        if mapped_rule_code and mapped_rule_code in REPAIR_STRATEGIES:
            return REPAIR_STRATEGIES[mapped_rule_code]
    return MANUAL_REVIEW_STRATEGY


def _evidence_from_rule_result(rule_result: dict[str, Any] | None) -> dict[str, Any]:
    if not rule_result:
        return {}
    checks = rule_result.get("matched_quality_checks")
    if not isinstance(checks, list) or not checks:
        return {}
    if len(checks) == 1 and isinstance(checks[0], dict):
        evidence = checks[0].get("evidence")
        return copy.deepcopy(evidence) if isinstance(evidence, dict) else {}
    return {
        "quality_checks": [
            {
                "code": str(check.get("code") or ""),
                "evidence": copy.deepcopy(check.get("evidence")) if isinstance(check.get("evidence"), dict) else {},
            }
            for check in checks
            if isinstance(check, dict)
        ]
    }


def _summary(tasks: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "task_count": len(tasks),
        "blocking_task_count": sum(1 for task in tasks if task["blocking"]),
        "human_review_task_count": sum(1 for task in tasks if task["requires_human_review"]),
        "fail_task_count": sum(1 for task in tasks if task["rule_status"] == "fail"),
        "warning_task_count": sum(1 for task in tasks if task["rule_status"] == "warning"),
    }


def _plan_status(summary: dict[str, int]) -> str:
    if summary["task_count"] == 0:
        return "no_repair_needed"
    if summary["blocking_task_count"] > 0:
        return "blocked"
    return "needs_repair"


def _priority_key(status: str, severity: str) -> int:
    if status not in STATUS_ORDER:
        raise RuleRepairPlanError(f"unsupported rule status for repair task: {status}")
    if severity not in SEVERITY_ORDER:
        raise RuleRepairPlanError(f"unsupported severity: {severity}")
    return STATUS_ORDER[status] * 10 + SEVERITY_ORDER[severity]


__all__ = [
    "RuleRepairPlanError",
    "build_rule_repair_plan",
]
