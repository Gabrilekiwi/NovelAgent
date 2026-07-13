from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from core.engine.workflow import validate_workflow_plan
from core.schema import validate_schema
from core.quality_decision import build_quality_decision, quality_decision_accepted


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_timestamp(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S%fZ")


def build_run_id(chapter_index: int, started_at: datetime) -> str:
    return f"chapter_{chapter_index}_{format_timestamp(started_at)}"


def validate_run_result(result: dict[str, Any]) -> dict[str, Any]:
    validate_schema(result, "run_result.schema.json")
    validate_schema(result["run"], "run_record.schema.json")
    run_workflow_plan = result["run"].get("workflow_plan")
    if isinstance(run_workflow_plan, dict):
        validate_workflow_plan(run_workflow_plan)
    if isinstance(result.get("memory_write"), dict):
        validate_schema(result["memory_write"], "memory_writeback.schema.json")
    run_memory = result["run"].get("memory")
    if isinstance(run_memory, dict) and isinstance(run_memory.get("writeback"), dict):
        validate_schema(run_memory["writeback"], "memory_writeback.schema.json")
    return result


def build_loop_session_record(
    *,
    started_at: datetime,
    finished_at: datetime,
    requested_steps: int,
    completed_steps: int,
    stopped_reason: str,
    persist: bool,
    stop_on_rejection: bool,
    runs: list[dict[str, Any]],
    step_timings: list[dict[str, Any]] | None = None,
    book_id: str | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    run_summaries = []
    failure_reasons: list[str] = []
    for item in runs:
        summary = _loop_run_summary(item["run"])
        reasons = _loop_failure_reasons(item)
        summary["failure_reasons"] = reasons
        run_summaries.append(summary)
        for reason in reasons:
            if reason not in failure_reasons:
                failure_reasons.append(reason)
    recovery_links = []
    for item in runs:
        link = _loop_recovery_link(item["run"])
        if link is not None:
            recovery_links.append(link)
    statuses = [item["status"] for item in run_summaries]
    chapter_indexes = [item["chapter_index"] for item in run_summaries]
    if error is not None and "run_failed" not in failure_reasons:
        failure_reasons.append("run_failed")
    error_code = getattr(error, "code", None) if error is not None else None
    if isinstance(error_code, str) and error_code and error_code not in failure_reasons:
        failure_reasons.append(error_code)
    succeeded = not failure_reasons and int(completed_steps) == int(requested_steps)
    record = {
        "id": f"loop_{format_timestamp(started_at)}",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "requested_steps": int(requested_steps),
        "completed_steps": int(completed_steps),
        "stopped_reason": stopped_reason,
        "persist": bool(persist),
        "stop_on_rejection": bool(stop_on_rejection),
        "succeeded": succeeded,
        "exit_code": 0 if succeeded else 1,
        "failure_reasons": failure_reasons,
        "committed_count": statuses.count("committed"),
        "rejected_count": statuses.count("rejected"),
        "failed_count": statuses.count("failed"),
        "first_chapter_index": chapter_indexes[0] if chapter_indexes else None,
        "last_chapter_index": chapter_indexes[-1] if chapter_indexes else None,
        "last_run_id": run_summaries[-1]["id"] if run_summaries else None,
        "recovery_links": recovery_links,
        "runs": run_summaries,
        "step_timings": step_timings or [],
    }
    if book_id is not None:
        record["book_id"] = book_id
    if error is not None:
        record["error"] = {
            "type": type(error).__name__,
            "message": str(error),
        }
        if isinstance(error_code, str) and error_code:
            record["error"]["code"] = error_code
    return validate_schema(record, "loop_session.schema.json")


def build_run_record(
    *,
    started_at: datetime,
    finished_at: datetime,
    base_snapshot: dict[str, Any],
    runtime_snapshot: dict[str, Any],
    memory_context: dict[str, Any],
    decision: dict[str, Any],
    workflow: list[str],
    workflow_plan: dict[str, Any] | None = None,
    input_pack: str,
    input_pack_metadata: dict[str, Any] | None = None,
    chapter: str,
    validation: dict[str, Any],
    analysis: dict[str, Any],
    repair_attempts: int,
    committed: bool,
    workflow_trace: list[dict[str, Any]] | None = None,
    director_trace: dict[str, Any] | None = None,
    snapshot_pack: str = "",
    snapshot_audit: dict[str, Any] | None = None,
    state_update_audit: dict[str, Any] | None = None,
    chapter_pipeline: dict[str, Any] | None = None,
    quality_decision: dict[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    chapter_index = int(decision["chapter_index"])
    problems = validation.get("problems", [])
    final_quality_decision = quality_decision or build_quality_decision(
        policy="minimal",
        validation=validation,
        chapter_index=chapter_index,
    )
    accepted_value = quality_decision_accepted(final_quality_decision)
    status_value = status or ("committed" if committed else "rejected")
    record = {
        "id": build_run_id(chapter_index, started_at),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "status": status_value,
        "accepted": accepted_value,
        "quality_decision": final_quality_decision,
        "committed": committed,
        "chapter_index": chapter_index,
        "snapshot": {
            "base_chapter_index": base_snapshot.get("chapter_index"),
            "runtime_chapter_index": runtime_snapshot.get("chapter_index"),
            "next_chapter_index": runtime_snapshot.get("chapter_index", chapter_index) + 1
            if committed
            else runtime_snapshot.get("chapter_index"),
        },
        "memory": _memory_summary(memory_context),
        "recovery_context": _recovery_context_summary(memory_context),
        "snapshot_builder": _snapshot_pack_summary(snapshot_pack, snapshot_audit),
        "director": _director_audit(director_trace or _default_director_trace(started_at, finished_at)),
        "decision": {
            "goal": decision.get("goal"),
            "actions": decision.get("actions", []),
            "validation_focus": decision.get("validation_focus", []),
            "max_repair_attempts": decision.get("max_repair_attempts", 0),
        },
        "workflow": workflow,
        "workflow_plan": _workflow_plan_summary(workflow_plan),
        "input_pack": _input_pack_summary(input_pack, input_pack_metadata),
        "chapter": _chapter_summary(chapter, chapter_pipeline),
        "validation": {
            "ok": bool(validation.get("ok")),
            "problem_codes": [problem.get("code") for problem in problems],
            "problem_count": len(problems),
            "blocking_problem_count": _blocking_problem_count(validation, problems),
            "warning_count": _warning_count(validation, problems),
            "severity_counts": _validation_severity_counts(validation, problems),
            "problem_evidence": _problem_evidence_summary(problems),
            **_validation_repair_summary(validation, problems),
            **_validation_coverage_summary(validation, decision),
        },
        "analysis": _analysis_summary(analysis),
        "state_update": _state_update_audit(
            state_update_audit,
            committed=committed,
            chapter_index=chapter_index,
            next_chapter_index=record_next_chapter_index(
                runtime_snapshot=runtime_snapshot,
                chapter_index=chapter_index,
                committed=committed,
            ),
            analysis=analysis,
        ),
        "repair_attempts": repair_attempts,
        "trace": workflow_trace or [],
    }
    return validate_schema(record, "run_record.schema.json")


def build_failed_run_record(
    *,
    started_at: datetime,
    finished_at: datetime,
    base_snapshot: dict[str, Any],
    runtime_snapshot: dict[str, Any],
    memory_context: dict[str, Any],
    decision: dict[str, Any],
    workflow: list[str],
    workflow_plan: dict[str, Any] | None = None,
    input_pack: str,
    input_pack_metadata: dict[str, Any] | None = None,
    chapter: str,
    validation: dict[str, Any] | None,
    repair_attempts: int,
    workflow_trace: list[dict[str, Any]],
    error: BaseException,
    director_trace: dict[str, Any] | None = None,
    snapshot_pack: str = "",
    snapshot_audit: dict[str, Any] | None = None,
    chapter_pipeline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    chapter_index = int(decision["chapter_index"])
    validation_payload = validation or {
        "ok": False,
        "problems": [
            {
                "code": "execution_error",
                "message": str(error),
                "severity": "critical",
                "blocking": True,
                "category": "blocking",
                "repair_hint": "Inspect the failed workflow action and rerun after fixing the underlying error.",
            }
        ],
    }
    problems = validation_payload.get("problems", [])
    record = {
        "id": build_run_id(chapter_index, started_at),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "status": "failed",
        "accepted": False,
        "committed": False,
        "chapter_index": chapter_index,
        "snapshot": {
            "base_chapter_index": base_snapshot.get("chapter_index"),
            "runtime_chapter_index": runtime_snapshot.get("chapter_index"),
            "next_chapter_index": runtime_snapshot.get("chapter_index"),
        },
        "memory": _memory_summary(memory_context),
        "recovery_context": _recovery_context_summary(memory_context),
        "snapshot_builder": _snapshot_pack_summary(snapshot_pack, snapshot_audit),
        "director": _director_audit(director_trace or _default_director_trace(started_at, finished_at)),
        "decision": {
            "goal": decision.get("goal"),
            "actions": decision.get("actions", []),
            "validation_focus": decision.get("validation_focus", []),
            "max_repair_attempts": decision.get("max_repair_attempts", 0),
        },
        "workflow": workflow,
        "workflow_plan": _workflow_plan_summary(workflow_plan),
        "input_pack": _input_pack_summary(input_pack, input_pack_metadata),
        "chapter": _chapter_summary(chapter, chapter_pipeline),
        "validation": {
            "ok": False,
            "problem_codes": [problem.get("code") for problem in problems],
            "problem_count": len(problems),
            "blocking_problem_count": _blocking_problem_count(validation_payload, problems),
            "warning_count": _warning_count(validation_payload, problems),
            "severity_counts": _validation_severity_counts(validation_payload, problems),
            "problem_evidence": _problem_evidence_summary(problems),
            **_validation_repair_summary(validation_payload, problems),
            **_validation_coverage_summary(validation_payload, decision),
        },
        "analysis": {
            "validation_ok": False,
            "conflict_count": 0,
            "event_count": 0,
            "world_change_count": 0,
            "summary": "",
        },
        "state_update": _state_update_audit(
            None,
            committed=False,
            chapter_index=chapter_index,
            next_chapter_index=runtime_snapshot.get("chapter_index") or chapter_index,
            analysis=None,
        ),
        "repair_attempts": repair_attempts,
        "trace": workflow_trace,
        "error": {
            "type": type(error).__name__,
            "message": str(error),
        },
    }
    return validate_schema(record, "run_record.schema.json")


def build_director_failed_run_record(
    *,
    started_at: datetime,
    finished_at: datetime,
    base_snapshot: dict[str, Any],
    runtime_snapshot: dict[str, Any],
    memory_context: dict[str, Any],
    director_trace: dict[str, Any],
    error: BaseException,
    snapshot_pack: str = "",
    snapshot_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    chapter_index = int(runtime_snapshot.get("chapter_index") or base_snapshot.get("chapter_index") or 1)
    record = {
        "id": build_run_id(chapter_index, started_at),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "status": "failed",
        "accepted": False,
        "committed": False,
        "chapter_index": chapter_index,
        "snapshot": {
            "base_chapter_index": base_snapshot.get("chapter_index"),
            "runtime_chapter_index": runtime_snapshot.get("chapter_index"),
            "next_chapter_index": runtime_snapshot.get("chapter_index"),
        },
        "memory": _memory_summary(memory_context),
        "recovery_context": _recovery_context_summary(memory_context),
        "snapshot_builder": _snapshot_pack_summary(snapshot_pack, snapshot_audit),
        "director": _director_audit(director_trace),
        "decision": {
            "goal": None,
            "actions": [],
            "validation_focus": [],
            "max_repair_attempts": 0,
        },
        "workflow": [],
        "workflow_plan": None,
        "input_pack": {
            "chars": 0,
            "preview": "",
        },
        "chapter": {
            "chars": 0,
        },
        "validation": {
            "ok": False,
            "problem_codes": ["director_error"],
            "problem_count": 1,
            "blocking_problem_count": 1,
            "warning_count": 0,
            "severity_counts": [{"severity": "critical", "count": 1}],
            "problem_evidence": [],
            "deterministic_repair_count": 0,
            "manual_review_count": 1,
            "repair_action_counts": [{"action": "manual_review", "count": 1}],
            "requested_focus": [],
            "executed_checks": [],
            "skipped_checks": [],
        },
        "analysis": {
            "validation_ok": False,
            "conflict_count": 0,
            "event_count": 0,
            "world_change_count": 0,
            "summary": "",
        },
        "state_update": _state_update_audit(
            None,
            committed=False,
            chapter_index=chapter_index,
            next_chapter_index=runtime_snapshot.get("chapter_index") or chapter_index,
            analysis=None,
        ),
        "repair_attempts": 0,
        "trace": [],
        "error": {
            "type": type(error).__name__,
            "message": str(error),
        },
    }
    return validate_schema(record, "run_record.schema.json")


def build_workflow_failed_run_record(
    *,
    started_at: datetime,
    finished_at: datetime,
    base_snapshot: dict[str, Any],
    runtime_snapshot: dict[str, Any],
    memory_context: dict[str, Any],
    decision: dict[str, Any],
    director_trace: dict[str, Any],
    error: BaseException,
    snapshot_pack: str = "",
    snapshot_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    chapter_index = int(decision["chapter_index"])
    record = {
        "id": build_run_id(chapter_index, started_at),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "status": "failed",
        "accepted": False,
        "committed": False,
        "chapter_index": chapter_index,
        "snapshot": {
            "base_chapter_index": base_snapshot.get("chapter_index"),
            "runtime_chapter_index": runtime_snapshot.get("chapter_index"),
            "next_chapter_index": runtime_snapshot.get("chapter_index"),
        },
        "memory": _memory_summary(memory_context),
        "recovery_context": _recovery_context_summary(memory_context),
        "snapshot_builder": _snapshot_pack_summary(snapshot_pack, snapshot_audit),
        "director": _director_audit(director_trace),
        "decision": {
            "goal": decision.get("goal"),
            "actions": decision.get("actions", []),
            "validation_focus": decision.get("validation_focus", []),
            "max_repair_attempts": decision.get("max_repair_attempts", 0),
        },
        "workflow": [],
        "workflow_plan": None,
        "input_pack": {
            "chars": 0,
            "preview": "",
        },
        "chapter": {
            "chars": 0,
        },
        "validation": {
            "ok": False,
            "problem_codes": ["workflow_error"],
            "problem_count": 1,
            "blocking_problem_count": 1,
            "warning_count": 0,
            "severity_counts": [{"severity": "critical", "count": 1}],
            "problem_evidence": [],
            "deterministic_repair_count": 0,
            "manual_review_count": 1,
            "repair_action_counts": [{"action": "manual_review", "count": 1}],
            **_validation_coverage_summary({}, decision),
        },
        "analysis": {
            "validation_ok": False,
            "conflict_count": 0,
            "event_count": 0,
            "world_change_count": 0,
            "summary": "",
        },
        "state_update": _state_update_audit(
            None,
            committed=False,
            chapter_index=chapter_index,
            next_chapter_index=runtime_snapshot.get("chapter_index") or chapter_index,
            analysis=None,
        ),
        "repair_attempts": 0,
        "trace": [],
        "error": {
            "type": type(error).__name__,
            "message": str(error),
        },
    }
    return validate_schema(record, "run_record.schema.json")


def _default_director_trace(started_at: datetime, finished_at: datetime) -> dict[str, Any]:
    return _director_audit({
        "mode": "unknown",
        "source": "unknown",
        "model": None,
        "status": "completed",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": 0,
    })


def _director_audit(director_trace: dict[str, Any]) -> dict[str, Any]:
    return validate_schema(director_trace, "director_audit.schema.json")


def _snapshot_pack_summary(snapshot_pack: str, snapshot_audit: dict[str, Any] | None = None) -> dict[str, Any]:
    summary = {
        "chars": len(snapshot_pack),
        "preview": snapshot_pack[:500],
    }
    if snapshot_audit is not None:
        summary["audit"] = snapshot_audit
    return summary


def _input_pack_summary(input_pack: str, input_pack_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    summary = {
        "chars": len(input_pack),
        "preview": input_pack[:500],
    }
    if input_pack_metadata is not None:
        validate_schema(input_pack_metadata, "input_pack_metadata.schema.json")
        summary["metadata"] = input_pack_metadata
    return summary


def _chapter_summary(chapter: str, chapter_pipeline: dict[str, Any] | None = None) -> dict[str, Any]:
    summary = {
        "chars": len(chapter),
    }
    if chapter_pipeline is not None:
        summary["pipeline"] = _chapter_pipeline_summary(chapter_pipeline)
    return summary


def _chapter_pipeline_summary(chapter_pipeline: dict[str, Any]) -> dict[str, Any]:
    pipeline = validate_schema(chapter_pipeline, "chapter_pipeline.schema.json")
    summary = {
        "chapter_index": pipeline.get("chapter_index"),
        "scene_count": len(pipeline.get("scene_drafts", [])),
        "plan_goal": (pipeline.get("plan") or {}).get("goal"),
        "merged_chars": len(str(pipeline.get("merged_chapter") or "")),
        "scene_spans": pipeline.get("scene_spans", []),
        "stages": pipeline.get("stages", []),
    }
    if pipeline.get("chapter_blueprint") is not None:
        summary["chapter_blueprint"] = pipeline.get("chapter_blueprint")
    if pipeline.get("blueprint_coverage") is not None:
        summary["blueprint_coverage"] = pipeline.get("blueprint_coverage")
    return summary


def _workflow_plan_summary(workflow_plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if workflow_plan is None:
        return None
    return validate_workflow_plan(workflow_plan)


def _memory_summary(memory_context: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": memory_context.get("source"),
        "status": memory_context.get("status"),
        "item_count": len(memory_context.get("items", [])),
        **_memory_source_mapping_summary(memory_context.get("source_mappings")),
    }


def _memory_source_mapping_summary(source_mappings: Any) -> dict[str, Any]:
    mappings = [mapping for mapping in source_mappings if isinstance(mapping, dict)] if isinstance(source_mappings, list) else []
    source_counts: dict[str, int] = {}
    file_mapping_count = 0
    line_mapping_count = 0
    notion_page_mapping_count = 0
    notion_page_url_count = 0
    for mapping in mappings:
        source = str(mapping.get("source") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1
        if mapping.get("path"):
            file_mapping_count += 1
        if mapping.get("line_number") is not None:
            line_mapping_count += 1
        if mapping.get("page_id"):
            notion_page_mapping_count += 1
        if mapping.get("page_url"):
            notion_page_url_count += 1
    return {
        "source_mapping_count": len(mappings),
        "source_mapping_sources": [
            {"source": source, "count": source_counts[source]}
            for source in sorted(source_counts)
        ],
        "file_mapping_count": file_mapping_count,
        "line_mapping_count": line_mapping_count,
        "notion_page_mapping_count": notion_page_mapping_count,
        "notion_page_url_count": notion_page_url_count,
    }


def record_next_chapter_index(
    *,
    runtime_snapshot: dict[str, Any],
    chapter_index: int,
    committed: bool,
) -> int:
    return (
        int(runtime_snapshot.get("chapter_index", chapter_index) or chapter_index) + 1
        if committed
        else int(runtime_snapshot.get("chapter_index", chapter_index) or chapter_index)
    )


def _state_update_audit(
    audit: dict[str, Any] | None,
    *,
    committed: bool,
    chapter_index: int,
    next_chapter_index: int,
    analysis: dict[str, Any] | None,
) -> dict[str, Any]:
    if audit is not None:
        return validate_schema(audit, "state_update_audit.schema.json")
    fallback = {
        "applied": bool(committed),
        "chapter_index": int(chapter_index),
        "next_chapter_index": int(next_chapter_index),
        "timeline_added": 1 if committed else 0,
        "character_update_count": len(_analysis_items(analysis, "character_changes")) if committed else 0,
        "location_update_count": len(_analysis_items(analysis, "new_locations")) if committed else 0,
        "world_change_count": len(_analysis_items(analysis, "world_changes")) if committed else 0,
        "memory_update_count": 0,
        "memory_update_types": [],
        "analysis_validation_ok": bool((analysis or {}).get("validation_ok")),
    }
    return validate_schema(fallback, "state_update_audit.schema.json")


def _analysis_summary(analysis: dict[str, Any]) -> dict[str, Any]:
    validated = validate_schema(analysis, "analysis_result.schema.json")
    return {
        "validation_ok": validated.get("validation_ok"),
        "conflict_count": len(validated.get("conflicts", [])),
        "event_count": len(validated.get("events", [])),
        "world_change_count": len(validated.get("world_changes", [])),
        "summary": validated.get("summary", ""),
    }


def _analysis_items(analysis: dict[str, Any] | None, key: str) -> list[Any]:
    value = (analysis or {}).get(key)
    return value if isinstance(value, list) else []


def _blocking_problem_count(validation: dict[str, Any], problems: list[dict[str, Any]]) -> int:
    explicit = validation.get("blocking_problem_count")
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return explicit
    return sum(1 for problem in problems if problem.get("blocking", True))


def _warning_count(validation: dict[str, Any], problems: list[dict[str, Any]]) -> int:
    explicit = validation.get("warning_count")
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return explicit
    return sum(1 for problem in problems if not problem.get("blocking", True))


def _validation_severity_counts(validation: dict[str, Any], problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    explicit = validation.get("severity_counts")
    if isinstance(explicit, list):
        return explicit
    counts: dict[str, int] = {}
    order = ["critical", "high", "medium", "low"]
    for problem in problems:
        severity = str(problem.get("severity") or "critical")
        counts[severity] = counts.get(severity, 0) + 1
    return [{"severity": severity, "count": counts[severity]} for severity in order if severity in counts]


def _problem_evidence_summary(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for problem in problems:
        if not isinstance(problem, dict):
            continue
        evidence = _evidence_items(problem.get("evidence"))
        if not evidence:
            continue
        summaries.append(
            {
                "code": str(problem.get("code") or "unknown"),
                "validator": str(problem.get("validator") or "unknown"),
                "severity": str(problem.get("severity") or "critical"),
                "blocking": bool(problem.get("blocking", True)),
                "repair_action": str(problem.get("repair_action") or "manual_review"),
                "evidence": evidence,
            }
        )
    return summaries


def _evidence_items(raw_evidence: Any) -> list[dict[str, str]]:
    if not isinstance(raw_evidence, list):
        return []
    items: list[dict[str, str]] = []
    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        value = item.get("value")
        if kind is None or value is None:
            continue
        items.append({"kind": str(kind), "value": str(value)})
    return items


def _validation_repair_summary(validation: dict[str, Any], problems: list[dict[str, Any]]) -> dict[str, Any]:
    deterministic = validation.get("deterministic_repair_count")
    manual = validation.get("manual_review_count")
    action_counts = validation.get("repair_action_counts")
    if (
        isinstance(deterministic, int)
        and not isinstance(deterministic, bool)
        and isinstance(manual, int)
        and not isinstance(manual, bool)
        and isinstance(action_counts, list)
    ):
        return {
            "deterministic_repair_count": deterministic,
            "manual_review_count": manual,
            "repair_action_counts": action_counts,
        }

    counts: dict[str, int] = {}
    for problem in problems:
        action = str(problem.get("repair_action") or "manual_review")
        counts[action] = counts.get(action, 0) + 1
    return {
        "deterministic_repair_count": sum(count for action, count in counts.items() if action != "manual_review"),
        "manual_review_count": counts.get("manual_review", 0),
        "repair_action_counts": [
            {"action": action, "count": counts[action]}
            for action in sorted(counts)
        ],
    }


def _validation_coverage_summary(validation: dict[str, Any], decision: dict[str, Any] | None = None) -> dict[str, list[str]]:
    requested_focus = _known_validation_names(validation.get("requested_focus"))
    if not requested_focus and isinstance(decision, dict):
        requested_focus = _known_validation_names(decision.get("validation_focus"))
    executed_checks = _known_validation_names(validation.get("executed_checks"))
    if not executed_checks:
        checks = validation.get("checks")
        if isinstance(checks, list):
            executed_checks = _known_validation_names(
                [check.get("name") for check in checks if isinstance(check, dict)]
            )
    if not executed_checks and requested_focus:
        executed_checks = list(requested_focus)
    skipped_checks = _known_validation_names(validation.get("skipped_checks"))
    if not skipped_checks and executed_checks:
        skipped_checks = [name for name in _RULE_VALIDATION_NAMES if name not in executed_checks]
    return {
        "requested_focus": requested_focus,
        "executed_checks": executed_checks,
        "skipped_checks": skipped_checks,
    }


_RULE_VALIDATION_NAMES = ["continuity", "spatial", "logic"]
_VALIDATION_NAMES = [*_RULE_VALIDATION_NAMES, "story_project", "llm"]


def _known_validation_names(raw_names: Any) -> list[str]:
    if not isinstance(raw_names, list):
        return []
    names: list[str] = []
    for raw_name in raw_names:
        name = str(raw_name)
        if name in _VALIDATION_NAMES and name not in names:
            names.append(name)
    return names


def _recovery_context_summary(memory_context: dict[str, Any]) -> dict[str, Any]:
    last_run = memory_context.get("last_run")
    if not isinstance(last_run, dict):
        return {
            "available": False,
            "source_run_id": None,
            "source_status": None,
            "source_committed": None,
            "source_chapter_index": None,
            "source_goal": None,
            "problem_codes": [],
            "problem_count": 0,
            "blocking_problem_count": None,
            "severity_counts": [],
            "repair_attempts": 0,
            "repair_effective": None,
            "repair_stalled": False,
            "repair_introduced_new_problems": False,
            "repair_risk_level": None,
            "repair_budget": None,
            "repair_manual_review_count": 0,
            "repair_budget_exhausted": False,
        }
    repair_plan = last_run.get("repair_plan") if isinstance(last_run.get("repair_plan"), dict) else {}
    repair_budget = repair_plan.get("repair_budget")
    repair_attempt = repair_plan.get("attempt")
    problem_codes = last_run.get("problem_codes", [])
    return {
        "available": True,
        "source_run_id": last_run.get("id"),
        "source_status": last_run.get("status"),
        "source_committed": last_run.get("committed"),
        "source_chapter_index": last_run.get("chapter_index"),
        "source_goal": last_run.get("goal"),
        "problem_codes": problem_codes if isinstance(problem_codes, list) else [],
        "problem_count": last_run.get("problem_count", 0),
        "blocking_problem_count": last_run.get("blocking_problem_count"),
        "severity_counts": last_run.get("severity_counts", []),
        "repair_attempts": last_run.get("repair_attempts", 0),
        "repair_effective": last_run.get("repair_effective"),
        "repair_stalled": bool(last_run.get("repair_stalled")),
        "repair_introduced_new_problems": bool(last_run.get("repair_introduced_new_problems")),
        "repair_risk_level": repair_plan.get("risk_level"),
        "repair_budget": repair_budget,
        "repair_manual_review_count": _int_or_zero(repair_plan.get("manual_review_count")),
        "repair_budget_exhausted": _repair_budget_exhausted(repair_budget, repair_attempt),
    }


def _loop_recovery_link(run: dict[str, Any]) -> dict[str, Any] | None:
    recovery = run.get("recovery_context")
    if not isinstance(recovery, dict) or not recovery.get("available"):
        return None
    decision = run.get("decision") or {}
    return {
        "run_id": run.get("id"),
        "run_status": run.get("status"),
        "director_goal": decision.get("goal"),
        "source_run_id": recovery.get("source_run_id"),
        "source_status": recovery.get("source_status"),
        "source_chapter_index": recovery.get("source_chapter_index"),
        "source_problem_codes": recovery.get("problem_codes", []),
        "repair_stalled": recovery.get("repair_stalled", False),
        "repair_introduced_new_problems": recovery.get("repair_introduced_new_problems", False),
        "repair_risk_level": recovery.get("repair_risk_level"),
        "repair_budget_exhausted": recovery.get("repair_budget_exhausted", False),
    }


def _loop_run_summary(run: dict[str, Any]) -> dict[str, Any]:
    validation = run.get("validation") or {}
    problem_codes = validation.get("problem_codes", [])
    return {
        "id": str(run.get("id")),
        "status": str(run.get("status")),
        "accepted": bool(run.get("accepted", run.get("committed"))),
        "committed": bool(run.get("committed")),
        "chapter_index": int(run.get("chapter_index") or 1),
        "problem_codes": problem_codes if isinstance(problem_codes, list) else [],
        "requested_focus": validation.get("requested_focus", []),
        "executed_checks": validation.get("executed_checks", []),
        "skipped_checks": validation.get("skipped_checks", []),
        "problem_evidence": validation.get("problem_evidence", []),
        "repair_evidence": _latest_repair_evidence(run),
        **_loop_workflow_summary(run),
        "repair_attempts": int(run.get("repair_attempts") or 0),
    }


def _loop_failure_reasons(result: dict[str, Any]) -> list[str]:
    run = result.get("run") if isinstance(result.get("run"), dict) else {}
    reasons: list[str] = []
    status = str(run.get("status") or "")
    if status == "rejected":
        reasons.append("run_rejected")
    elif status == "failed":
        reasons.append("run_failed")

    gate = run.get("review_gate") if isinstance(run.get("review_gate"), dict) else None
    if gate and gate.get("status") in {"fail", "error"}:
        reasons.append("review_gate_failed")

    story_project = run.get("story_project") if isinstance(run.get("story_project"), dict) else {}
    writeback = story_project.get("writeback") if isinstance(story_project.get("writeback"), dict) else {}
    if writeback.get("attempted") and not writeback.get("dry_run"):
        if not writeback.get("applied") or writeback.get("partial"):
            reasons.append("story_project_writeback_failed")

    memory = run.get("memory") if isinstance(run.get("memory"), dict) else {}
    memory_writeback = memory.get("writeback") if isinstance(memory.get("writeback"), dict) else {}
    verification = memory_writeback.get("verification") if isinstance(memory_writeback.get("verification"), dict) else {}
    if verification.get("status") in {"failed", "error"}:
        reasons.append("memory_delivery_failed")
    return list(dict.fromkeys(reasons))


def _loop_workflow_summary(run: dict[str, Any]) -> dict[str, Any]:
    workflow_actions = _known_workflow_actions(run.get("workflow"))
    if not workflow_actions and isinstance(run.get("workflow_plan"), dict):
        workflow_actions = _known_workflow_actions(run["workflow_plan"].get("actions"))
    trace_actions = _known_workflow_actions(
        [event.get("action") for event in run.get("trace", []) if isinstance(event, dict)]
        if isinstance(run.get("trace"), list)
        else []
    )
    return {
        "workflow_actions": workflow_actions,
        "trace_actions": trace_actions,
        "trace_plan_aligned": _trace_plan_aligned(run, workflow_actions, trace_actions),
    }


def _trace_plan_aligned(run: dict[str, Any], workflow_actions: list[str], trace_actions: list[str]) -> bool:
    if not workflow_actions or workflow_actions != trace_actions:
        return False
    plan_steps = _workflow_plan_steps_by_action(run.get("workflow_plan"))
    if not plan_steps:
        return False
    trace = run.get("trace")
    if not isinstance(trace, list):
        return False
    for event in trace:
        if not isinstance(event, dict):
            return False
        action = event.get("action")
        plan_step = plan_steps.get(action)
        if not plan_step:
            return False
        if event.get("plan_step_index") != plan_step.get("index"):
            return False
        if event.get("plan_step_mode") != plan_step.get("mode"):
            return False
        if event.get("plan_failure_policy") != plan_step.get("failure_policy"):
            return False
    return True


def _workflow_plan_steps_by_action(workflow_plan: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(workflow_plan, dict):
        return {}
    steps = workflow_plan.get("steps")
    if not isinstance(steps, list):
        return {}
    return {
        str(step.get("action")): step
        for step in steps
        if isinstance(step, dict) and step.get("action") in _WORKFLOW_ACTIONS
    }


def _known_workflow_actions(raw_actions: Any) -> list[str]:
    if not isinstance(raw_actions, list):
        return []
    return [str(action) for action in raw_actions if str(action) in _WORKFLOW_ACTIONS]


_WORKFLOW_ACTIONS = {
    "build_snapshot",
    "pre_validate_bridge",
    "generate_chapter",
    "polish",
    "validate",
    "repair_if_needed",
    "commit_snapshot",
}


def load_latest_run_summary(run_dir: str | Path) -> dict[str, Any] | None:
    path = Path(run_dir)
    if not path.exists():
        return None

    candidates = sorted(path.glob("chapter_*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    for candidate in candidates:
        try:
            with candidate.open("r", encoding="utf-8-sig") as f:
                payload = json.load(f)
            payload = validate_run_result(payload)
        except (OSError, json.JSONDecodeError, ValueError):
            continue

        run = payload.get("run")
        if isinstance(run, dict):
            validation = run.get("validation") or {}
            decision = run.get("decision") or {}
            error = run.get("error") or {}
            director = run.get("director") or {}
            director_model_call = director.get("model_call") if isinstance(director.get("model_call"), dict) else None
            problem_codes = validation.get("problem_codes", [])
            repair_deltas = _latest_repair_deltas(run)
            repair_plan = _latest_repair_plan(run)
            return {
                "id": run.get("id"),
                "status": run.get("status"),
                "committed": run.get("committed"),
                "chapter_index": run.get("chapter_index"),
                "goal": decision.get("goal"),
                "workflow": run.get("workflow", []),
                "problem_codes": problem_codes,
                "problem_count": validation.get("problem_count", len(problem_codes) if isinstance(problem_codes, list) else 0),
                "blocking_problem_count": validation.get("blocking_problem_count"),
                "warning_count": validation.get("warning_count"),
                "severity_counts": validation.get("severity_counts", []),
                "requested_focus": validation.get("requested_focus", []),
                "executed_checks": validation.get("executed_checks", []),
                "skipped_checks": validation.get("skipped_checks", []),
                "problem_evidence": validation.get("problem_evidence", []),
                "repair_attempts": run.get("repair_attempts", 0),
                "repair_plan": repair_plan,
                "repair_evidence": _latest_repair_evidence(run),
                "repair_deltas": repair_deltas,
                "repair_effective": _repair_effective(repair_deltas),
                "repair_stalled": _repair_stalled(repair_deltas),
                "repair_introduced_new_problems": _repair_introduced_new_problems(repair_deltas),
                "error_type": error.get("type"),
                "error_message": error.get("message"),
                "director_mode": director.get("mode"),
                "director_status": director.get("status"),
                "director_model_call": director_model_call,
            }

    return None


def _latest_repair_deltas(run: dict[str, Any]) -> list[dict[str, Any]]:
    trace = run.get("trace", [])
    if not isinstance(trace, list):
        return []
    deltas: list[dict[str, Any]] = []
    for event in trace:
        if not isinstance(event, dict):
            continue
        raw_deltas = event.get("repair_deltas")
        if not isinstance(raw_deltas, list):
            continue
        for delta in raw_deltas:
            if not isinstance(delta, dict):
                continue
            deltas.append(
                {
                    "attempt": delta.get("attempt"),
                    "before_problem_count": delta.get("before_problem_count"),
                    "after_problem_count": delta.get("after_problem_count"),
                    "resolved_problem_codes": delta.get("resolved_problem_codes", []),
                    "new_problem_codes": delta.get("new_problem_codes", []),
                    "remaining_problem_codes": delta.get("remaining_problem_codes", []),
                }
            )
    return deltas


def _latest_repair_plan(run: dict[str, Any]) -> dict[str, Any] | None:
    trace = run.get("trace", [])
    if not isinstance(trace, list):
        return None
    for event in reversed(trace):
        if not isinstance(event, dict):
            continue
        repair_plan = event.get("repair_plan")
        if not isinstance(repair_plan, dict):
            continue
        return {
            "risk_level": repair_plan.get("risk_level"),
            "repair_budget": repair_plan.get("repair_budget"),
            "attempt": repair_plan.get("attempt"),
            "deterministic_step_count": repair_plan.get("deterministic_step_count"),
            "manual_review_count": repair_plan.get("manual_review_count"),
        }
    return None


def _latest_repair_evidence(run: dict[str, Any]) -> list[dict[str, Any]]:
    trace = run.get("trace", [])
    if not isinstance(trace, list):
        return []
    for event in reversed(trace):
        if not isinstance(event, dict):
            continue
        repair_plan = event.get("repair_plan")
        if not isinstance(repair_plan, dict):
            continue
        return _repair_plan_evidence_summary(repair_plan)
    return []


def _repair_plan_evidence_summary(repair_plan: dict[str, Any]) -> list[dict[str, Any]]:
    steps = repair_plan.get("steps")
    if not isinstance(steps, list):
        return []
    summaries: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        evidence = _evidence_items(step.get("evidence"))
        if not evidence:
            continue
        summaries.append(
            {
                "code": str(step.get("code") or "unknown"),
                "validator": str(step.get("validator") or "unknown"),
                "action": str(step.get("action") or "manual_review"),
                "evidence": evidence,
            }
        )
    return summaries


def _repair_budget_exhausted(repair_budget: Any, repair_attempt: Any) -> bool:
    if not isinstance(repair_budget, int) or isinstance(repair_budget, bool):
        return False
    if not isinstance(repair_attempt, int) or isinstance(repair_attempt, bool):
        return False
    return repair_budget > 0 and repair_attempt >= repair_budget


def _repair_effective(repair_deltas: list[dict[str, Any]]) -> bool | None:
    if not repair_deltas:
        return None
    last_delta = repair_deltas[-1]
    return (
        _int_or_zero(last_delta.get("after_problem_count")) == 0
        and not last_delta.get("new_problem_codes")
        and bool(last_delta.get("resolved_problem_codes"))
    )


def _repair_stalled(repair_deltas: list[dict[str, Any]]) -> bool:
    if not repair_deltas:
        return False
    last_delta = repair_deltas[-1]
    return _int_or_zero(last_delta.get("after_problem_count")) > 0 or bool(last_delta.get("remaining_problem_codes"))


def _repair_introduced_new_problems(repair_deltas: list[dict[str, Any]]) -> bool:
    return any(bool(delta.get("new_problem_codes")) for delta in repair_deltas)


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
