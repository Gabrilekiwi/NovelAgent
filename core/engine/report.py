from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.schema import validate_schema
from core.engine.run_record import validate_run_result
from core.runtime_paths import DEFAULT_RUN_DIR


def build_run_report(run_dir: str | Path = DEFAULT_RUN_DIR, *, limit: int | None = 5) -> dict[str, Any]:
    path = Path(run_dir)
    runs: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    skipped_sessions: list[dict[str, str]] = []

    if path.exists() and not path.is_dir():
        return {
            "run_dir": str(path),
            "total": 0,
            "loaded": 0,
            "loop_session_total": 0,
            "loop_session_loaded": 0,
            "skipped": [{"path": str(path), "error": "run_dir is not a directory"}],
            "skipped_loop_sessions": [],
            "latest": None,
            "latest_loop_session": None,
            "status_counts": {},
            "problem_counts": {},
            "runs": [],
            "loop_sessions": [],
        }

    candidates = _run_files(path)
    for candidate in candidates:
        try:
            run_result = _load_run_result(candidate)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            skipped.append({"path": str(candidate), "error": str(exc)})
            continue
        runs.append(_summarize_run(run_result, candidate))

    session_candidates = _loop_session_files(path)
    for candidate in session_candidates:
        try:
            session = _load_loop_session(candidate)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            skipped_sessions.append({"path": str(candidate), "error": str(exc)})
            continue
        sessions.append(_summarize_loop_session(session, candidate))

    runs.sort(key=_run_sort_key, reverse=True)
    sessions.sort(key=_run_sort_key, reverse=True)
    limited_runs = runs if limit is None else runs[: max(0, limit)]
    limited_sessions = sessions if limit is None else sessions[: max(0, limit)]
    return {
        "run_dir": str(path),
        "total": len(candidates),
        "loaded": len(runs),
        "loop_session_total": len(session_candidates),
        "loop_session_loaded": len(sessions),
        "skipped": skipped,
        "skipped_loop_sessions": skipped_sessions,
        "latest": limited_runs[0] if limited_runs else None,
        "latest_loop_session": limited_sessions[0] if limited_sessions else None,
        "status_counts": _status_counts(runs),
        "problem_counts": _problem_counts(runs),
        "runs": limited_runs,
        "loop_sessions": limited_sessions,
    }


def _run_files(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(path.glob("chapter_*.json"), key=lambda item: item.stat().st_mtime, reverse=True)


def _loop_session_files(path: Path) -> list[Path]:
    session_dir = path / "loop_sessions"
    if not session_dir.exists():
        return []
    return sorted(session_dir.glob("loop_*.json"), key=lambda item: item.stat().st_mtime, reverse=True)


def _load_run_result(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as f:
        payload = json.load(f)
    return validate_run_result(payload)


def _load_loop_session(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as f:
        session = json.load(f)
    if not isinstance(session, dict):
        raise ValueError("missing loop session object")
    return validate_schema(session, "loop_session.schema.json")


def _run_sort_key(run: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(run.get("finished_at") or ""),
        str(run.get("started_at") or ""),
        str(run.get("id") or ""),
    )


def _summarize_loop_session(session: dict[str, Any], path: Path) -> dict[str, Any]:
    error = session.get("error") or {}
    return {
        "id": session.get("id"),
        "path": str(path),
        "started_at": session.get("started_at"),
        "finished_at": session.get("finished_at"),
        "requested_steps": session.get("requested_steps"),
        "completed_steps": session.get("completed_steps"),
        "stopped_reason": session.get("stopped_reason"),
        "persist": session.get("persist"),
        "stop_on_rejection": session.get("stop_on_rejection"),
        "committed_count": session.get("committed_count"),
        "rejected_count": session.get("rejected_count"),
        "failed_count": session.get("failed_count"),
        "first_chapter_index": session.get("first_chapter_index"),
        "last_chapter_index": session.get("last_chapter_index"),
        "last_run_id": session.get("last_run_id"),
        "run_ids": [run.get("id") for run in session.get("runs", []) if isinstance(run, dict)],
        "run_summaries": _loop_session_run_summaries(session.get("runs")),
        "step_timings": _loop_step_timings(session.get("step_timings")),
        "artifact": _loop_session_artifact_summary(session, path),
        "recovery_link_count": len(session.get("recovery_links", [])) if isinstance(session.get("recovery_links"), list) else 0,
        "recovery_links": session.get("recovery_links", []) if isinstance(session.get("recovery_links"), list) else [],
        "error": {
            "type": error.get("type"),
            "message": error.get("message"),
        }
        if isinstance(error, dict) and error
        else None,
    }


def _loop_step_timings(step_timings: Any) -> list[dict[str, Any]]:
    if not isinstance(step_timings, list):
        return []
    summary: list[dict[str, Any]] = []
    for item in step_timings:
        if not isinstance(item, dict):
            continue
        summary.append(
            {
                "step": item.get("step"),
                "status": item.get("status"),
                "duration_ms": item.get("duration_ms"),
                "run_id": item.get("run_id"),
                "chapter_index": item.get("chapter_index"),
                "committed": item.get("committed"),
                "error_type": item.get("error_type"),
            }
        )
    return summary


def _summarize_run(run_result: dict[str, Any], path: Path) -> dict[str, Any]:
    run = run_result["run"]
    validation = run.get("validation") or {}
    full_validation = run_result.get("validation") if isinstance(run_result.get("validation"), dict) else None
    decision = run.get("decision") or {}
    director = run.get("director") or {}
    analysis = run.get("analysis") or {}
    error = run.get("error") or {}

    return {
        "id": run.get("id"),
        "path": str(path),
        "status": run.get("status"),
        "committed": run.get("committed"),
        "chapter_index": run.get("chapter_index"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "goal": decision.get("goal"),
        "workflow": run.get("workflow", []),
        "workflow_plan": _workflow_plan_summary(run.get("workflow_plan")),
        "director": {
            "mode": director.get("mode"),
            "model": director.get("model"),
            "status": director.get("status"),
            "duration_ms": director.get("duration_ms"),
            "model_call": director.get("model_call") if isinstance(director.get("model_call"), dict) else None,
        },
        "validation": {
            "ok": validation.get("ok"),
            "problem_codes": validation.get("problem_codes", []),
            "problem_count": validation.get("problem_count", 0),
            "blocking_problem_count": validation.get("blocking_problem_count", validation.get("problem_count", 0)),
            "warning_count": validation.get("warning_count", 0),
            "severity_counts": validation.get("severity_counts", []),
            "deterministic_repair_count": validation.get("deterministic_repair_count", 0),
            "manual_review_count": validation.get("manual_review_count", 0),
            "repair_action_counts": validation.get("repair_action_counts", []),
            "problem_evidence": _problem_evidence_summary(validation, full_validation),
            "requested_focus": validation.get("requested_focus", []),
            "executed_checks": validation.get("executed_checks", []),
            "skipped_checks": validation.get("skipped_checks", []),
        },
        "analysis": {
            "summary": analysis.get("summary", ""),
            "conflict_count": analysis.get("conflict_count", 0),
            "event_count": analysis.get("event_count", 0),
            "world_change_count": analysis.get("world_change_count", 0),
        },
        "memory": _memory_summary(run.get("memory")),
        "recovery_context": _recovery_context_summary(run.get("recovery_context")),
        "state_update": _state_update_summary(run.get("state_update")),
        "state_builder": _state_builder_summary(run.get("snapshot_builder", {})),
        "repair_attempts": run.get("repair_attempts", 0),
        "trace": _trace_summary(run.get("trace", [])),
        "artifacts": _artifact_summary(run),
        "error": {
            "type": error.get("type"),
            "message": error.get("message"),
        }
        if error
        else None,
    }


def _trace_summary(trace: Any) -> list[dict[str, Any]]:
    if not isinstance(trace, list):
        return []
    return [
        {
            "action": event.get("action"),
            "status": event.get("status"),
            "plan_step_index": event.get("plan_step_index"),
            "plan_step_mode": event.get("plan_step_mode"),
            "plan_failure_policy": event.get("plan_failure_policy"),
            "model_stage": event.get("model_stage"),
            "model_provider": event.get("model_provider"),
            "model_name": event.get("model_name"),
            "model_invocation": event.get("model_invocation"),
            "validation_ok": event.get("validation_ok"),
            "problem_count": event.get("problem_count"),
            "repair_attempts": event.get("repair_attempts", 0),
            "skipped": event.get("skipped") if "skipped" in event else None,
            "skip_reason": event.get("skip_reason"),
            "repair_actions": _repair_actions(event.get("repair_plan")),
            "repair_validators": _repair_validators(event.get("repair_plan")),
            "repair_evidence": _repair_evidence_summary(event.get("repair_plan")),
            "repair_risk_level": _repair_plan_field(event.get("repair_plan"), "risk_level"),
            "repair_budget": _repair_plan_field(event.get("repair_plan"), "repair_budget"),
            "repair_manual_review_count": _repair_plan_field(event.get("repair_plan"), "manual_review_count"),
            "repair_failure_modes": _repair_recovery_list(event.get("repair_plan"), "failure_modes"),
            "repair_repeated_problem_codes": _repair_recovery_list(event.get("repair_plan"), "repeated_problem_codes"),
            "repair_unresolved_problem_codes": _repair_recovery_list(event.get("repair_plan"), "unresolved_problem_codes"),
            "repair_new_problem_codes": _repair_recovery_list(event.get("repair_plan"), "new_problem_codes"),
            "repair_deltas": _repair_deltas(event.get("repair_deltas")),
            "model_call": event.get("model_call") if isinstance(event.get("model_call"), dict) else None,
            "error_type": event.get("error_type"),
        }
        for event in trace
        if isinstance(event, dict)
    ]


def _loop_session_run_summaries(runs: Any) -> list[dict[str, Any]]:
    if not isinstance(runs, list):
        return []
    summaries: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        summaries.append(
            {
                "id": run.get("id"),
                "status": run.get("status"),
                "committed": run.get("committed"),
                "chapter_index": run.get("chapter_index"),
                "problem_codes": run.get("problem_codes", []),
                "problem_evidence": run.get("problem_evidence", []),
                "requested_focus": run.get("requested_focus", []),
                "executed_checks": run.get("executed_checks", []),
                "skipped_checks": run.get("skipped_checks", []),
                "workflow_actions": run.get("workflow_actions", []),
                "trace_actions": run.get("trace_actions", []),
                "trace_plan_aligned": run.get("trace_plan_aligned"),
                "repair_attempts": run.get("repair_attempts", 0),
                "repair_evidence": run.get("repair_evidence", []),
            }
        )
    return summaries


def _workflow_plan_summary(workflow_plan: Any) -> dict[str, Any] | None:
    if not isinstance(workflow_plan, dict):
        return None
    steps = workflow_plan.get("steps", [])
    return {
        "recovery": workflow_plan.get("recovery"),
        "step_count": len(steps) if isinstance(steps, list) else 0,
        "required_step_count": _step_mode_count(steps, "required"),
        "optional_step_count": _step_mode_count(steps, "optional"),
        "conditional_step_count": _step_mode_count(steps, "conditional"),
        "validation_focus": workflow_plan.get("validation_focus", []),
        "max_repair_attempts": workflow_plan.get("max_repair_attempts", 0),
    }


def _step_mode_count(steps: Any, mode: str) -> int:
    if not isinstance(steps, list):
        return 0
    return sum(1 for step in steps if isinstance(step, dict) and step.get("mode") == mode)


def _memory_summary(memory: Any) -> dict[str, Any]:
    if not isinstance(memory, dict):
        return {
            "source": None,
            "status": None,
            "item_count": None,
            "source_mapping_count": 0,
            "source_mapping_sources": [],
            "file_mapping_count": 0,
            "line_mapping_count": 0,
            "notion_page_mapping_count": 0,
            "notion_page_url_count": 0,
            "writeback": None,
        }
    writeback = memory.get("writeback")
    return {
        "source": memory.get("source"),
        "status": memory.get("status"),
        "item_count": memory.get("item_count"),
        "source_mapping_count": memory.get("source_mapping_count", 0),
        "source_mapping_sources": memory.get("source_mapping_sources", []),
        "file_mapping_count": memory.get("file_mapping_count", 0),
        "line_mapping_count": memory.get("line_mapping_count", 0),
        "notion_page_mapping_count": memory.get("notion_page_mapping_count", 0),
        "notion_page_url_count": memory.get("notion_page_url_count", 0),
        "writeback": _writeback_summary(writeback),
    }


def _recovery_context_summary(recovery_context: Any) -> dict[str, Any]:
    if not isinstance(recovery_context, dict):
        return {"available": False}
    return {
        "available": bool(recovery_context.get("available")),
        "source_run_id": recovery_context.get("source_run_id"),
        "source_status": recovery_context.get("source_status"),
        "source_chapter_index": recovery_context.get("source_chapter_index"),
        "problem_codes": recovery_context.get("problem_codes", []),
        "repair_stalled": recovery_context.get("repair_stalled"),
        "repair_introduced_new_problems": recovery_context.get("repair_introduced_new_problems"),
        "repair_risk_level": recovery_context.get("repair_risk_level"),
        "repair_budget": recovery_context.get("repair_budget"),
        "repair_manual_review_count": recovery_context.get("repair_manual_review_count"),
        "repair_budget_exhausted": recovery_context.get("repair_budget_exhausted"),
    }


def _writeback_summary(writeback: Any) -> dict[str, Any] | None:
    if not isinstance(writeback, dict):
        return None
    mappings = writeback.get("item_mappings", [])
    return {
        "target": writeback.get("target"),
        "written": writeback.get("written", 0),
        "skipped": writeback.get("skipped", 0),
        "status_counts": _mapping_counts(mappings, "status"),
        "type_counts": _mapping_counts(mappings, "type"),
        "gate": writeback.get("gate") if isinstance(writeback.get("gate"), dict) else None,
        "verification": _writeback_verification(writeback.get("verification")),
        "item_mappings": _writeback_mappings(mappings),
    }


def _mapping_counts(mappings: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(mappings, list):
        return []
    counts: dict[str, int] = {}
    for mapping in mappings:
        if not isinstance(mapping, dict):
            continue
        value = mapping.get(field)
        if value:
            text = str(value)
            counts[text] = counts.get(text, 0) + 1
    return [{field: value, "count": counts[value]} for value in sorted(counts)]


def _writeback_verification(verification: Any) -> dict[str, Any] | None:
    if not isinstance(verification, dict):
        return None
    return {
        "status": verification.get("status"),
        "target": verification.get("target"),
        "checked": verification.get("checked"),
        "passed": verification.get("passed"),
        "failed": verification.get("failed"),
        "reason": verification.get("reason"),
    }


def _writeback_mappings(mappings: Any) -> list[dict[str, Any]]:
    if not isinstance(mappings, list):
        return []
    result: list[dict[str, Any]] = []
    for mapping in mappings:
        if not isinstance(mapping, dict):
            continue
        result.append(
            {
                "memory_id": mapping.get("memory_id"),
                "type": mapping.get("type"),
                "name": mapping.get("name"),
                "target": mapping.get("target"),
                "status": mapping.get("status"),
                "page_id": mapping.get("page_id"),
                "page_url": mapping.get("page_url"),
                "database_id": mapping.get("database_id"),
                "property_names": mapping.get("property_names", []),
                "path": mapping.get("path"),
                "line_number": mapping.get("line_number"),
            }
        )
    return result


def _state_update_summary(state_update: Any) -> dict[str, Any]:
    if not isinstance(state_update, dict):
        return {
            "applied": None,
            "timeline_added": None,
            "memory_update_count": None,
        }
    return {
        "applied": state_update.get("applied"),
        "timeline_added": state_update.get("timeline_added"),
        "memory_update_count": state_update.get("memory_update_count"),
        "next_chapter_index": state_update.get("next_chapter_index"),
    }


def _repair_actions(repair_plan: Any) -> list[str]:
    if not isinstance(repair_plan, dict):
        return []
    actions = repair_plan.get("actions", [])
    return [str(action) for action in actions] if isinstance(actions, list) else []


def _repair_validators(repair_plan: Any) -> list[str]:
    if not isinstance(repair_plan, dict):
        return []
    steps = repair_plan.get("steps")
    if not isinstance(steps, list):
        return []
    validators: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        validator = step.get("validator")
        if validator and str(validator) not in validators:
            validators.append(str(validator))
    return validators


def _repair_evidence_summary(repair_plan: Any) -> list[dict[str, Any]]:
    if not isinstance(repair_plan, dict):
        return []
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


def _problem_evidence_summary(compact_validation: Any, full_validation: Any = None) -> list[dict[str, Any]]:
    if isinstance(compact_validation, dict):
        compact = compact_validation.get("problem_evidence")
        if isinstance(compact, list):
            return [item for item in compact if isinstance(item, dict)]

    problems = full_validation.get("problems") if isinstance(full_validation, dict) else None
    if not isinstance(problems, list):
        return []
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


def _repair_plan_field(repair_plan: Any, field: str) -> Any:
    if not isinstance(repair_plan, dict):
        return None
    return repair_plan.get(field)


def _repair_recovery_list(repair_plan: Any, field: str) -> list[str]:
    if not isinstance(repair_plan, dict):
        return []
    recovery = repair_plan.get("recovery")
    if not isinstance(recovery, dict):
        return []
    value = recovery.get(field)
    return [str(item) for item in value] if isinstance(value, list) else []


def _repair_deltas(repair_deltas: Any) -> list[dict[str, Any]]:
    if not isinstance(repair_deltas, list):
        return []
    deltas: list[dict[str, Any]] = []
    for delta in repair_deltas:
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


def _state_builder_summary(snapshot_builder: Any) -> dict[str, Any]:
    audit = snapshot_builder.get("audit") if isinstance(snapshot_builder, dict) else None
    if not isinstance(audit, dict):
        return {
            "item_count": None,
            "applied_count": None,
            "skipped_count": None,
            "deduplicated_count": None,
            "applied_type_counts": [],
            "skipped_type_counts": [],
            "skipped_blocking_count": 0,
            "applied_source_mapping_count": 0,
            "skipped_source_mapping_count": 0,
            "skipped_reason_counts": [],
            "skipped_severity_counts": [],
        }
    return {
        "item_count": audit.get("item_count"),
        "applied_count": audit.get("applied_count"),
        "skipped_count": audit.get("skipped_count"),
        "deduplicated_count": audit.get("deduplicated_count"),
        "applied_type_counts": audit.get("applied_type_counts", _type_count_entries(audit.get("applied_items"))),
        "skipped_type_counts": audit.get("skipped_type_counts", _type_count_entries(audit.get("skipped_items"))),
        "skipped_blocking_count": audit.get("skipped_blocking_count", _blocking_count(audit.get("skipped_items"))),
        "applied_source_mapping_count": _source_mapped_count(audit.get("applied_items")),
        "skipped_source_mapping_count": _source_mapped_count(audit.get("skipped_items")),
        "skipped_reason_counts": audit.get("skipped_reason_counts", []),
        "skipped_severity_counts": audit.get("skipped_severity_counts", []),
    }


def _source_mapped_count(items: Any) -> int:
    if not isinstance(items, list):
        return 0
    return sum(1 for item in items if isinstance(item, dict) and isinstance(item.get("source_mapping"), dict))


def _type_count_entries(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type is None:
            continue
        counts[str(item_type)] = counts.get(str(item_type), 0) + 1
    return [{"type": name, "count": counts[name]} for name in sorted(counts)]


def _blocking_count(items: Any) -> int:
    if not isinstance(items, list):
        return 0
    return sum(1 for item in items if isinstance(item, dict) and item.get("blocking") is True)


def _artifact_summary(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    artifacts: dict[str, dict[str, Any]] = {}
    for name, section_name in (
        ("snapshot_pack", "snapshot_builder"),
        ("input_pack", "input_pack"),
        ("chapter", "chapter"),
    ):
        section = run.get(section_name) or {}
        artifact = section.get("artifact") if isinstance(section, dict) else None
        artifact_path = artifact.get("path") if isinstance(artifact, dict) else None
        artifacts[name] = {
            "path": artifact_path,
            "exists": Path(artifact_path).exists() if artifact_path else False,
        }
    chapter = run.get("chapter") if isinstance(run.get("chapter"), dict) else {}
    pipeline = chapter.get("pipeline") if isinstance(chapter, dict) else None
    if isinstance(pipeline, dict):
        artifacts["chapter_pipeline"] = _pipeline_artifact_summary(pipeline.get("artifacts"))
    return artifacts


def _pipeline_artifact_summary(raw_artifacts: Any) -> dict[str, Any]:
    if not isinstance(raw_artifacts, dict):
        return {"exists": False}
    summary: dict[str, Any] = {}
    for key in ("plan", "merged_chapter", "validation_report", "repair_deltas"):
        artifact = raw_artifacts.get(key)
        if isinstance(artifact, dict):
            path = artifact.get("path")
            summary[key] = {
                "path": path,
                "exists": Path(path).exists() if path else False,
            }
    scene_artifacts = raw_artifacts.get("scene_drafts")
    if isinstance(scene_artifacts, list):
        summary["scene_drafts"] = [
            {
                "path": artifact.get("path"),
                "exists": Path(artifact.get("path")).exists() if artifact.get("path") else False,
            }
            for artifact in scene_artifacts
            if isinstance(artifact, dict)
        ]
    summary["exists"] = all(
        item.get("exists")
        for key, item in summary.items()
        if key != "scene_drafts" and isinstance(item, dict)
    ) and all(item.get("exists") for item in summary.get("scene_drafts", []))
    return summary


def _loop_session_artifact_summary(session: dict[str, Any], path: Path) -> dict[str, Any]:
    artifact = session.get("artifact")
    artifact_path = artifact.get("path") if isinstance(artifact, dict) else None
    if not artifact_path:
        artifact_path = str(path)
    return {
        "path": artifact_path,
        "format": artifact.get("format") if isinstance(artifact, dict) else "json",
        "exists": Path(artifact_path).exists(),
    }


def _status_counts(runs: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for run in runs:
        status = str(run.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _problem_counts(runs: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for run in runs:
        validation = run.get("validation") or {}
        problem_codes = validation.get("problem_codes", [])
        if not isinstance(problem_codes, list):
            continue
        for code in problem_codes:
            key = str(code or "unknown")
            counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = ["build_run_report"]
