from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from core.schema import SchemaValidationError, validate_schema


REQUIRED_ACTIONS = {"generate_chapter", "validate"}
ALLOWED_ACTIONS = {
    "build_snapshot",
    "pre_validate_bridge",
    "generate_chapter",
    "polish",
    "validate",
    "repair_if_needed",
    "commit_snapshot",
}
ALLOWED_VALIDATION_FOCUS = {"continuity", "spatial", "logic"}


class DirectorDecisionError(ValueError):
    pass


@dataclass(frozen=True)
class DirectorDecision:
    chapter_index: int
    goal: str
    actions: list[str]
    validation_focus: list[str]
    max_repair_attempts: int = 1
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_decision(decision: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []

    try:
        validate_schema(decision, "director_decision.schema.json")
    except SchemaValidationError as exc:
        errors.append(str(exc))

    chapter_index = decision.get("chapter_index")
    if not isinstance(chapter_index, int) or chapter_index < 1:
        errors.append("chapter_index must be a positive integer")

    goal = decision.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        errors.append("goal must be a non-empty string")

    actions = decision.get("actions")
    if not isinstance(actions, list) or not actions:
        errors.append("actions must be a non-empty list")
        action_set: set[str] = set()
    else:
        action_set = {str(action) for action in actions}
        unknown_actions = sorted(action_set - ALLOWED_ACTIONS)
        missing_actions = sorted(REQUIRED_ACTIONS - action_set)
        if unknown_actions:
            errors.append(f"unknown actions: {unknown_actions}")
        if missing_actions:
            errors.append(f"missing required actions: {missing_actions}")

    validation_focus = decision.get("validation_focus")
    if not isinstance(validation_focus, list):
        errors.append("validation_focus must be a list")
    else:
        unknown_focus = sorted({str(item) for item in validation_focus} - ALLOWED_VALIDATION_FOCUS)
        if unknown_focus:
            errors.append(f"unknown validation focus: {unknown_focus}")

    max_repair_attempts = decision.get("max_repair_attempts")
    if not isinstance(max_repair_attempts, int) or not 0 <= max_repair_attempts <= 5:
        errors.append("max_repair_attempts must be an integer between 0 and 5")

    notes = decision.get("notes")
    if not isinstance(notes, list):
        errors.append("notes must be a list")

    if errors:
        raise DirectorDecisionError("; ".join(errors))

    return decision


def decide_next_step(
    snapshot: dict[str, Any],
    memory_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    chapter_index = int(snapshot.get("chapter_index", 1))
    timeline = snapshot.get("timeline") or []
    world_state = snapshot.get("world_state") or {}

    notes = [
        "Use snapshot as the runtime source of truth.",
        "Keep Notion memory read-only during this execution step.",
    ]
    if memory_context and memory_context.get("items"):
        notes.append(f"Memory items available: {len(memory_context['items'])}.")
    constraints = snapshot.get("constraints") or []
    if constraints:
        notes.append(f"Active constraints: {len(constraints)}.")
    if world_state.get("infection_level"):
        notes.append(f"Current infection level: {world_state['infection_level']}.")

    last_run = (memory_context or {}).get("last_run") if isinstance(memory_context, dict) else None
    last_problem_codes: list[str] = []
    max_repair_attempts = 1
    validation_focus = ["continuity", "spatial", "logic"]
    goal = "continue_existing_arc" if timeline else "establish_story_baseline"
    actions = ["generate_chapter", "polish", "validate", "repair_if_needed"]
    if _needs_bridge_workflow(snapshot):
        actions = ["build_snapshot", "pre_validate_bridge", "generate_chapter", "polish", "validate", "repair_if_needed", "commit_snapshot"]
    snapshot_audit = (memory_context or {}).get("snapshot_builder_audit") if isinstance(memory_context, dict) else None
    skipped_reason_counts = _reason_counts((snapshot_audit or {}).get("skipped_reason_counts") if isinstance(snapshot_audit, dict) else None)
    skipped_severity_counts = _severity_counts(
        (snapshot_audit or {}).get("skipped_severity_counts") if isinstance(snapshot_audit, dict) else None
    )
    skipped_type_counts = _type_counts((snapshot_audit or {}).get("skipped_type_counts") if isinstance(snapshot_audit, dict) else None)
    skipped_blocking_count = _int_value(
        (snapshot_audit or {}).get("skipped_blocking_count") if isinstance(snapshot_audit, dict) else None,
        default=0,
    )
    memory_quality_risk = _has_memory_quality_risk(skipped_reason_counts, skipped_severity_counts)
    if isinstance(snapshot_audit, dict) and _int_value(snapshot_audit.get("skipped_count"), default=0) > 0:
        notes.append(
            "Snapshot Builder skipped memory: "
            f"reasons {_format_reason_counts(skipped_reason_counts)}; "
            f"severity counts: {_format_severity_counts(skipped_severity_counts)}; "
            f"types {_format_type_counts(skipped_type_counts)}; "
            f"blocking={skipped_blocking_count}."
        )

    if isinstance(last_run, dict) and last_run.get("status") in {"rejected", "failed"}:
        raw_codes = last_run.get("problem_codes") or []
        last_problem_codes = [str(code) for code in raw_codes if code]
        blocking_problem_count = _int_value(last_run.get("blocking_problem_count"), default=len(last_problem_codes))
        severity_counts = _severity_counts(last_run.get("severity_counts"))
        max_repair_attempts = _recovery_repair_budget(blocking_problem_count, severity_counts)
        goal = "recover_from_failed_run" if last_run.get("status") == "failed" else "recover_from_rejected_run"
        actions = ["generate_chapter", "validate", "repair_if_needed"]
        if _needs_bridge_workflow(snapshot):
            actions = ["build_snapshot", "pre_validate_bridge", "generate_chapter", "validate", "repair_if_needed", "commit_snapshot"]
        notes.append(f"Previous run {last_run.get('status')} with problem codes: {', '.join(last_problem_codes) or 'unknown'}.")
        notes.append(
            "Previous validation blocking problems: "
            f"{blocking_problem_count}; severity counts: {_format_severity_counts(severity_counts)}."
        )
        if last_run.get("error_type"):
            notes.append(f"Previous error: {last_run.get('error_type')}: {last_run.get('error_message') or 'no message'}.")
        if last_run.get("workflow"):
            notes.append(f"Previous workflow: {', '.join(str(action) for action in last_run.get('workflow') or [])}.")
        notes.append("Skip polish during recovery so fact repair runs before prose refinement.")
        validation_focus = _focus_from_problem_codes(last_problem_codes)
        executed_checks = _validation_names(last_run.get("executed_checks"))
        skipped_checks = _validation_names(last_run.get("skipped_checks"))
        if executed_checks or skipped_checks:
            notes.append(
                "Previous validation coverage: "
                f"executed={_format_code_list(executed_checks)}; "
                f"skipped={_format_code_list(skipped_checks)}."
            )
        if skipped_checks:
            validation_focus = _merge_focus(skipped_checks, validation_focus)
            max_repair_attempts = max(max_repair_attempts, 2)
            notes.append("Prioritize validation checks skipped by the previous run before committing recovery.")
        repair_deltas = _repair_deltas(last_run.get("repair_deltas"))
        if repair_deltas:
            notes.append(
                "Previous repair delta: "
                f"{_format_repair_delta(repair_deltas[-1])}."
            )
        repair_plan = last_run.get("repair_plan") if isinstance(last_run.get("repair_plan"), dict) else {}
        repair_risk_level = str(repair_plan.get("risk_level") or "")
        repair_manual_review_count = _int_value(repair_plan.get("manual_review_count"), default=0)
        repair_budget = _int_value(repair_plan.get("repair_budget"), default=0)
        repair_attempt = _int_value(repair_plan.get("attempt"), default=0)
        if repair_plan:
            notes.append(
                "Previous repair plan: "
                f"risk={repair_risk_level or 'unknown'}; "
                f"budget={repair_budget or 'unknown'}; "
                f"attempt={repair_attempt or 'unknown'}; "
                f"manual_review={repair_manual_review_count}."
            )
        if repair_risk_level == "critical":
            max_repair_attempts = max(max_repair_attempts, 3)
            notes.append("Previous repair plan carried critical risk; keep recovery budget elevated.")
        if repair_manual_review_count > 0:
            max_repair_attempts = max(max_repair_attempts, 3)
            validation_focus = _merge_focus(["continuity", "spatial", "logic"], validation_focus)
            notes.append("Previous repair plan required manual review; run full validation focus before polish.")
        if repair_budget > 0 and repair_attempt >= repair_budget and not bool(last_run.get("committed")):
            max_repair_attempts = max(max_repair_attempts, min(5, repair_budget + 1))
            notes.append("Previous repair consumed its budget without commit; increase recovery budget.")
        if bool(last_run.get("repair_introduced_new_problems")):
            max_repair_attempts = max(max_repair_attempts, 3)
            validation_focus = _merge_focus(
                _focus_from_problem_codes(_repair_delta_problem_codes(repair_deltas) + last_problem_codes),
                validation_focus,
            )
            notes.append("Previous repair introduced new validation problems; use stronger recovery before polish.")
        elif bool(last_run.get("repair_stalled")):
            max_repair_attempts = max(max_repair_attempts, 3)
            validation_focus = _merge_focus(
                _focus_from_problem_codes(_repair_delta_problem_codes(repair_deltas) + last_problem_codes),
                validation_focus,
            )
            notes.append("Previous repair did not resolve all validation problems; increase repair budget.")

    if memory_quality_risk:
        validation_focus = _merge_focus(_focus_from_memory_quality(skipped_reason_counts), validation_focus)
        max_repair_attempts = max(max_repair_attempts, 2)
        if "polish" in actions:
            actions = ["generate_chapter", "validate", "repair_if_needed"]
            if _needs_bridge_workflow(snapshot):
                actions = ["build_snapshot", "pre_validate_bridge", "generate_chapter", "validate", "repair_if_needed", "commit_snapshot"]
            goal = "resolve_memory_quality_risk"
            notes.append("Skip polish until memory quality risks are validated and scene repair can run first.")
        else:
            notes.append("Current memory quality risk keeps repair budget elevated during recovery.")

    decision = DirectorDecision(
        chapter_index=chapter_index,
        goal=goal,
        actions=actions,
        validation_focus=validation_focus,
        max_repair_attempts=max_repair_attempts,
        notes=notes,
    )
    return validate_decision(decision.to_dict())


def _focus_from_problem_codes(problem_codes: list[str]) -> list[str]:
    focus: list[str] = []
    mapping = {
        "chapter_index_mismatch": "continuity",
        "inactive_character_action": "continuity",
        "no_known_location": "spatial",
        "character_unknown_location": "spatial",
        "character_location_not_mentioned": "spatial",
        "missing_opening_bridge": "spatial",
        "unexplained_location_shift": "spatial",
        "invalid_spatial_transition": "spatial",
        "missing_last_scene_continuity": "spatial",
        "character_position_conflict": "spatial",
        "missing_conflict_marker": "logic",
        "chapter_too_short": "logic",
        "forbidden_constraint_term": "logic",
        "missing_required_constraint_term": "logic",
        "execution_error": "logic",
        "director_error": "logic",
        "workflow_error": "logic",
    }
    for code in problem_codes:
        item = mapping.get(code)
        if item and item not in focus:
            focus.append(item)
    for item in ["continuity", "spatial", "logic"]:
        if item not in focus:
            focus.append(item)
    return focus


def _needs_bridge_workflow(snapshot: dict[str, Any]) -> bool:
    story_state = snapshot.get("story_state") if isinstance(snapshot.get("story_state"), dict) else {}
    spatial_state = snapshot.get("spatial_state") if isinstance(snapshot.get("spatial_state"), dict) else {}
    return bool(
        str(story_state.get("required_opening_bridge") or "").strip()
        or str(story_state.get("last_scene_location") or "").strip()
        or spatial_state.get("connections")
        or spatial_state.get("character_positions")
    )


def _int_value(value: Any, *, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _severity_counts(raw_counts: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(raw_counts, list):
        return counts
    for item in raw_counts:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "")
        count = item.get("count")
        if severity and isinstance(count, int) and not isinstance(count, bool):
            counts[severity] = count
    return counts


def _reason_counts(raw_counts: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(raw_counts, list):
        return counts
    for item in raw_counts:
        if not isinstance(item, dict):
            continue
        reason_code = str(item.get("reason_code") or "")
        count = item.get("count")
        if reason_code and isinstance(count, int) and not isinstance(count, bool):
            counts[reason_code] = count
    return counts


def _type_counts(raw_counts: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(raw_counts, list):
        return counts
    for item in raw_counts:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        count = item.get("count")
        if item_type and isinstance(count, int) and not isinstance(count, bool):
            counts[item_type] = count
    return counts


def _repair_deltas(raw_deltas: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_deltas, list):
        return []
    return [delta for delta in raw_deltas if isinstance(delta, dict)]


def _repair_delta_problem_codes(repair_deltas: list[dict[str, Any]]) -> list[str]:
    codes: list[str] = []
    for delta in repair_deltas:
        for key in ("new_problem_codes", "remaining_problem_codes", "after_problem_codes"):
            raw_codes = delta.get(key)
            if not isinstance(raw_codes, list):
                continue
            for code in raw_codes:
                if code and str(code) not in codes:
                    codes.append(str(code))
    return codes


def _format_severity_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "unknown"
    ordered = ["critical", "high", "medium", "low"]
    return ", ".join(f"{severity}={counts[severity]}" for severity in ordered if severity in counts)


def _format_reason_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "unknown"
    return ", ".join(f"{reason}={counts[reason]}" for reason in sorted(counts))


def _format_type_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "unknown"
    return ", ".join(f"{item_type}={counts[item_type]}" for item_type in sorted(counts))


def _format_repair_delta(delta: dict[str, Any]) -> str:
    before = _int_value(delta.get("before_problem_count"), default=0)
    after = _int_value(delta.get("after_problem_count"), default=0)
    resolved = _format_code_list(delta.get("resolved_problem_codes"))
    new = _format_code_list(delta.get("new_problem_codes"))
    remaining = _format_code_list(delta.get("remaining_problem_codes"))
    return f"problems {before}->{after}; resolved={resolved}; new={new}; remaining={remaining}"


def _format_code_list(raw_codes: Any) -> str:
    if not isinstance(raw_codes, list) or not raw_codes:
        return "none"
    return ",".join(str(code) for code in raw_codes if code)


def _validation_names(raw_names: Any) -> list[str]:
    if not isinstance(raw_names, list):
        return []
    names: list[str] = []
    for raw_name in raw_names:
        name = str(raw_name)
        if name in ALLOWED_VALIDATION_FOCUS and name not in names:
            names.append(name)
    return names


def _has_memory_quality_risk(reason_counts: dict[str, int], severity_counts: dict[str, int]) -> bool:
    if severity_counts.get("critical", 0) or severity_counts.get("high", 0) or severity_counts.get("medium", 0):
        return True
    return reason_counts.get("missing_name", 0) > 0


def _focus_from_memory_quality(reason_counts: dict[str, int]) -> list[str]:
    focus: list[str] = []
    if reason_counts.get("missing_name", 0) > 0:
        focus.extend(["continuity", "spatial"])
    if not focus:
        focus.append("logic")
    for item in ["continuity", "spatial", "logic"]:
        if item not in focus:
            focus.append(item)
    return focus


def _merge_focus(primary: list[str], secondary: list[str]) -> list[str]:
    merged: list[str] = []
    for item in [*primary, *secondary]:
        if item in ALLOWED_VALIDATION_FOCUS and item not in merged:
            merged.append(item)
    return merged


def _recovery_repair_budget(blocking_problem_count: int, severity_counts: dict[str, int]) -> int:
    if severity_counts.get("critical", 0) > 0 or blocking_problem_count >= 3:
        return 3
    return 2
