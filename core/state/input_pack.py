from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.project_profile import normalize_project_profile
from core.schema import validate_schema


_SNAPSHOT_PROMPT_PATH = Path("prompts/snapshot_prompt.md")
INPUT_PACK_SECTIONS = [
    "chapter_index",
    "project_profile",
    "director_decision",
    "world_state",
    "story_state",
    "spatial_state",
    "characters",
    "timeline",
    "constraints",
    "runtime_memory_metadata",
    "memory_index",
    "recovery_context",
    "requirements",
]


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def build_snapshot_input_pack(
    base_snapshot: dict[str, Any],
    memory_context: dict[str, Any] | None = None,
) -> str:
    return f"""{_load_snapshot_prompt()}

# Base Snapshot
{_dump(base_snapshot)}

# Memory Context
{_dump(memory_context or {})}

# Output Contract
- Merge memory into a runtime Snapshot.
- Keep `chapter_index`, `world_state`, `characters`, and `timeline` present.
- Preserve memory identity fields such as `id`, `name`, and `source_run_id` where they matter for deduplication.
- Return structured Snapshot data, not chapter prose."""


def build_input_pack(
    snapshot: dict[str, Any],
    decision: dict[str, Any] | None = None,
    memory_context: dict[str, Any] | None = None,
    *,
    narrative_rules: str | None = None,
    story_project_context: dict[str, Any] | None = None,
) -> str:
    return f"""You are NovelAgent's chapter generation module. Generate the next chapter from the runtime Snapshot and Director decision.

# Chapter Index
{snapshot.get("chapter_index")}

# Project Profile
{_dump(normalize_project_profile(snapshot))}

# Director Decision
{_dump(decision or {})}{_narrative_rules_section(narrative_rules)}

# World State
{_dump(snapshot.get("world_state", {}))}

# Story State
{_dump(snapshot.get("story_state", {}))}

# Spatial State
{_dump(snapshot.get("spatial_state", {}))}

# Characters
{_dump(snapshot.get("characters", {}))}

# Timeline
{_dump(snapshot.get("timeline", []))}

# Constraints
{_dump(snapshot.get("constraints", []))}

# Runtime Memory Metadata
{_dump(snapshot.get("memory", {}))}

# Memory Index
{_dump(_memory_index(memory_context))}

# Recovery Context
{_dump(build_recovery_context(memory_context))}
{_story_project_section(story_project_context)}

# Requirements
- Advance the plot instead of restating setup.
- Preserve character, location, and timeline continuity from the Snapshot.
- If Project Profile sets a language, write the chapter only in that language.
- Continue directly from Story State and explain Spatial State transitions before changing locations.
- Introduce or intensify at least one concrete conflict.
- Treat Snapshot and Memory Index as read-only runtime context.
- If Recovery Context is available, address its problem codes and validation coverage gaps without contradicting the Snapshot.
- If StoryProject Chapter Blueprint is present, treat its required_beats and ending_pressure as mandatory chapter contract.
- Return only chapter prose, not analysis or JSON."""


def _narrative_rules_section(narrative_rules: str | None) -> str:
    if not isinstance(narrative_rules, str) or not narrative_rules.strip():
        return ""
    return "\n\n# 小说生成规则契约\n" + narrative_rules.strip()


def _story_project_section(story_project_context: dict[str, Any] | None) -> str:
    if not isinstance(story_project_context, dict):
        return ""
    payload = {
        "chapter_blueprint": story_project_context.get("chapter_blueprint"),
        "read_set_context_digest": (
            story_project_context.get("read_set") or {}
        ).get("context_digest"),
        "outline_source": _compact_story_source(story_project_context.get("outline"), excerpt_chars=800),
        "previous_chapter_context": story_project_context.get("previous_chapter_context"),
        "semantic_state": story_project_context.get("semantic_state"),
        "tracking_excerpts": _compact_story_sources(
            story_project_context.get("tracking_files"), excerpt_chars=2000
        ),
        "setting_excerpts": _compact_story_sources(
            story_project_context.get("setting_files"), excerpt_chars=1200
        ),
        "source_paths": story_project_context.get("source_paths"),
        "source_resolution": story_project_context.get("source_resolution"),
    }
    return "\n\n# StoryProject Chapter Blueprint\n" + _dump(payload)


def _compact_story_sources(value: Any, *, excerpt_chars: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(name): _compact_story_source(source, excerpt_chars=excerpt_chars)
        for name, source in sorted(value.items())
        if isinstance(source, dict)
    }


def _compact_story_source(value: Any, *, excerpt_chars: int) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    text = str(value.get("text") or "")
    if len(text) > excerpt_chars:
        head = max(1, excerpt_chars // 4)
        text = text[:head].rstrip() + "\n[…excerpt…]\n" + text[-(excerpt_chars - head) :].lstrip()
    return {
        "relative_path": value.get("relative_path"),
        "sha256": value.get("sha256"),
        "original_chars": value.get("chars"),
        "excerpt": text,
        "truncated": bool(value.get("truncated")) or len(str(value.get("text") or "")) > excerpt_chars,
    }


def build_input_pack_metadata(
    input_pack: str,
    snapshot: dict[str, Any],
    decision: dict[str, Any] | None = None,
    memory_context: dict[str, Any] | None = None,
    story_project_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    memory_index = _memory_index(memory_context)
    sections = list(INPUT_PACK_SECTIONS)
    if isinstance(story_project_context, dict):
        sections.append("story_project_chapter_blueprint")
    metadata = {
        "kind": "chapter_input_pack",
        "chapter_index": int(snapshot.get("chapter_index") or 1),
        "chars": len(input_pack),
        "sections": sections,
        "decision": {
            "goal": (decision or {}).get("goal"),
            "actions": list((decision or {}).get("actions") or []),
            "validation_focus": list((decision or {}).get("validation_focus") or []),
            "max_repair_attempts": int((decision or {}).get("max_repair_attempts") or 0),
        },
        "snapshot": {
            "project_profile": normalize_project_profile(snapshot),
            "world_state_keys": sorted(str(key) for key in (snapshot.get("world_state") or {}).keys()),
            "story_state_keys": sorted(str(key) for key in (snapshot.get("story_state") or {}).keys()),
            "spatial_state_keys": sorted(str(key) for key in (snapshot.get("spatial_state") or {}).keys()),
            "open_thread_count": len((snapshot.get("story_state") or {}).get("open_threads") or []),
            "space_count": len((snapshot.get("spatial_state") or {}).get("spaces") or {}),
            "connection_count": len((snapshot.get("spatial_state") or {}).get("connections") or []),
            "character_position_count": len((snapshot.get("spatial_state") or {}).get("character_positions") or {}),
            "character_count": len(snapshot.get("characters") or {}),
            "timeline_count": len(snapshot.get("timeline") or []),
            "constraint_count": len(snapshot.get("constraints") or []),
            "memory_source": (snapshot.get("memory") or {}).get("source"),
            "memory_status": (snapshot.get("memory") or {}).get("status"),
            "memory_item_count": int((snapshot.get("memory") or {}).get("item_count") or 0),
        },
        "memory_index": {
            "source": memory_index.get("source"),
            "status": memory_index.get("status"),
            "item_count": int(memory_index.get("item_count") or 0),
            "indexed_item_count": len(memory_index.get("items") or []),
            "source_mapping_count": len(memory_index.get("source_mappings") or []),
            "last_run_present": isinstance(memory_index.get("last_run"), dict),
        },
        "recovery_context": build_recovery_context_metadata(build_recovery_context(memory_context)),
    }
    if isinstance(story_project_context, dict):
        blueprint = story_project_context.get("chapter_blueprint") or {}
        metadata["story_project"] = {
            "enabled": True,
            "chapter_index": story_project_context.get("chapter_index"),
            "required_beat_count": len(blueprint.get("required_beats") or []),
            "ending_pressure_present": bool(blueprint.get("ending_pressure")),
        }
    return validate_schema(metadata, "input_pack_metadata.schema.json")


def _load_snapshot_prompt() -> str:
    return _SNAPSHOT_PROMPT_PATH.read_text(encoding="utf-8")


def _memory_index(memory_context: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(memory_context, dict):
        return {}

    items = memory_context.get("items")
    indexed_items = [_memory_item_index(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    compact: dict[str, Any] = {
        "source": memory_context.get("source"),
        "status": memory_context.get("status"),
        "item_count": len(items) if isinstance(items, list) else 0,
        "items": indexed_items,
        "source_mappings": _compact_source_mappings(memory_context.get("source_mappings")),
    }
    for key in ("note", "path", "last_run"):
        if key in memory_context:
            compact[key] = memory_context[key]
    if "snapshot_builder_audit" in memory_context:
        compact["snapshot_builder_audit"] = _compact_snapshot_builder_audit(memory_context["snapshot_builder_audit"])
    return compact


def build_recovery_context(memory_context: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(memory_context, dict) or not isinstance(memory_context.get("last_run"), dict):
        return {"available": False}
    last_run = memory_context["last_run"]
    return {
        "available": True,
        "source_run_id": last_run.get("id"),
        "status": last_run.get("status"),
        "committed": last_run.get("committed"),
        "chapter_index": last_run.get("chapter_index"),
        "goal": last_run.get("goal"),
        "workflow": _list_value(last_run.get("workflow")),
        "problem_codes": _list_value(last_run.get("problem_codes")),
        "problem_count": _int_or_none(last_run.get("problem_count")),
        "blocking_problem_count": _int_or_none(last_run.get("blocking_problem_count")),
        "warning_count": _int_or_none(last_run.get("warning_count")),
        "severity_counts": last_run.get("severity_counts", []),
        "requested_focus": _list_value(last_run.get("requested_focus")),
        "executed_checks": _list_value(last_run.get("executed_checks")),
        "skipped_checks": _list_value(last_run.get("skipped_checks")),
        "repair_attempts": _int_or_zero(last_run.get("repair_attempts")),
        "repair_plan": _compact_repair_plan(last_run.get("repair_plan")),
        "repair_deltas": _compact_repair_deltas(last_run.get("repair_deltas")),
        "error": _compact_error(last_run),
    }


def build_recovery_context_metadata(recovery_context: dict[str, Any]) -> dict[str, Any]:
    return {
        "available": bool(recovery_context.get("available")),
        "source_run_id": recovery_context.get("source_run_id"),
        "status": recovery_context.get("status"),
        "problem_count": _int_or_zero(recovery_context.get("problem_count")),
        "executed_checks": _list_value(recovery_context.get("executed_checks")),
        "skipped_checks": _list_value(recovery_context.get("skipped_checks")),
        "repair_attempts": _int_or_zero(recovery_context.get("repair_attempts")),
    }


def _compact_repair_plan(repair_plan: Any) -> dict[str, Any] | None:
    if not isinstance(repair_plan, dict):
        return None
    return {
        "risk_level": repair_plan.get("risk_level"),
        "repair_budget": repair_plan.get("repair_budget"),
        "attempt": repair_plan.get("attempt"),
        "deterministic_step_count": repair_plan.get("deterministic_step_count"),
        "manual_review_count": repair_plan.get("manual_review_count"),
    }


def _compact_repair_deltas(repair_deltas: Any) -> list[dict[str, Any]]:
    if not isinstance(repair_deltas, list):
        return []
    compact: list[dict[str, Any]] = []
    for delta in repair_deltas[-3:]:
        if not isinstance(delta, dict):
            continue
        compact.append(
            {
                "attempt": delta.get("attempt"),
                "before_problem_count": delta.get("before_problem_count"),
                "after_problem_count": delta.get("after_problem_count"),
                "resolved_problem_codes": _list_value(delta.get("resolved_problem_codes")),
                "new_problem_codes": _list_value(delta.get("new_problem_codes")),
                "remaining_problem_codes": _list_value(delta.get("remaining_problem_codes")),
            }
        )
    return compact


def _compact_error(last_run: dict[str, Any]) -> dict[str, Any] | None:
    if not last_run.get("error_type") and not last_run.get("error_message"):
        return None
    return {
        "type": last_run.get("error_type"),
        "message": last_run.get("error_message"),
    }


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _memory_item_index(item: dict[str, Any]) -> dict[str, Any]:
    indexed: dict[str, Any] = {
        "type": item.get("type"),
        "name": item.get("name"),
    }
    for key in ("id", "source_run_id"):
        if key in item:
            indexed[key] = item[key]
    return indexed


def _compact_source_mappings(source_mappings: Any) -> list[dict[str, Any]]:
    if not isinstance(source_mappings, list):
        return []
    compact: list[dict[str, Any]] = []
    for mapping in source_mappings:
        if not isinstance(mapping, dict):
            continue
        item = {
            "index": mapping.get("index"),
            "source": mapping.get("source"),
            "memory_id": mapping.get("memory_id"),
            "type": mapping.get("type"),
            "name": mapping.get("name"),
            "path": mapping.get("path"),
            "line_number": mapping.get("line_number"),
            "page_id": mapping.get("page_id"),
            "page_url": mapping.get("page_url"),
            "page_index": mapping.get("page_index"),
        }
        compact.append({key: value for key, value in item.items() if value is not None})
    return compact


def _compact_snapshot_builder_audit(audit: Any) -> dict[str, Any] | None:
    if not isinstance(audit, dict):
        return None
    return {
        "source": audit.get("source"),
        "status": audit.get("status"),
        "item_count": audit.get("item_count"),
        "applied_count": audit.get("applied_count"),
        "skipped_count": audit.get("skipped_count"),
        "deduplicated_count": audit.get("deduplicated_count"),
        "skipped_reason_counts": audit.get("skipped_reason_counts", []),
        "skipped_severity_counts": audit.get("skipped_severity_counts", []),
    }
