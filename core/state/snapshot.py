from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from core.schema import SchemaValidationError, validate_schema
from core.runtime_paths import DEFAULT_SNAPSHOT_PATH


DEFAULT_SNAPSHOT: dict[str, Any] = {
    "chapter_index": 1,
    "world_state": {"locations": {}},
    "characters": {},
    "timeline": [],
    "story_state": {
        "last_chapter_ending": "",
        "last_scene_location": "",
        "last_scene_characters": [],
        "open_threads": [],
        "required_opening_bridge": "",
    },
    "spatial_state": {
        "spaces": {},
        "connections": [],
        "character_positions": {},
        "blocked_paths": [],
        "last_transition": {},
    },
}


class SnapshotError(ValueError):
    pass


def normalize_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    data = copy.deepcopy(DEFAULT_SNAPSHOT)
    if snapshot:
        data.update(copy.deepcopy(snapshot))

    raw_chapter_index = data.get("chapter_index")
    if raw_chapter_index in (None, ""):
        raw_chapter_index = 1
    try:
        data["chapter_index"] = int(raw_chapter_index)
    except (TypeError, ValueError) as exc:
        raise SnapshotError("chapter_index must be a positive integer") from exc

    if data.get("world_state") is None:
        data["world_state"] = {}
    if isinstance(data.get("world_state"), dict):
        data["world_state"].setdefault("locations", {})

    if data.get("characters") is None:
        data["characters"] = {}

    if data.get("timeline") is None:
        data["timeline"] = []
    data["story_state"] = _normalize_story_state(data.get("story_state"))
    data["spatial_state"] = _normalize_spatial_state(data.get("spatial_state"))
    validate_snapshot(data)
    return data


def validate_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []

    try:
        validate_schema(snapshot, "snapshot.schema.json")
    except SchemaValidationError as exc:
        errors.append(str(exc))

    chapter_index = snapshot.get("chapter_index")
    if not isinstance(chapter_index, int) or chapter_index < 1:
        errors.append("chapter_index must be a positive integer")

    world_state = snapshot.get("world_state")
    if not isinstance(world_state, dict):
        errors.append("world_state must be an object")
    elif not isinstance(world_state.get("locations", {}), dict):
        errors.append("world_state.locations must be an object")

    if not isinstance(snapshot.get("characters"), dict):
        errors.append("characters must be an object")

    timeline = snapshot.get("timeline")
    if not isinstance(timeline, list):
        errors.append("timeline must be a list")
    else:
        for index, entry in enumerate(timeline):
            if not isinstance(entry, dict):
                errors.append(f"timeline[{index}] must be an object")

    story_state = snapshot.get("story_state")
    if not isinstance(story_state, dict):
        errors.append("story_state must be an object")
    else:
        for field in ("last_chapter_ending", "last_scene_location", "required_opening_bridge"):
            if not isinstance(story_state.get(field), str):
                errors.append(f"story_state.{field} must be a string")
        for field in ("last_scene_characters", "open_threads"):
            if not isinstance(story_state.get(field), list):
                errors.append(f"story_state.{field} must be a list")

    spatial_state = snapshot.get("spatial_state")
    if not isinstance(spatial_state, dict):
        errors.append("spatial_state must be an object")
    else:
        if not isinstance(spatial_state.get("spaces"), dict):
            errors.append("spatial_state.spaces must be an object")
        if not isinstance(spatial_state.get("connections"), list):
            errors.append("spatial_state.connections must be a list")
        if not isinstance(spatial_state.get("character_positions"), dict):
            errors.append("spatial_state.character_positions must be an object")
        if not isinstance(spatial_state.get("blocked_paths"), list):
            errors.append("spatial_state.blocked_paths must be a list")
        if not isinstance(spatial_state.get("last_transition"), dict):
            errors.append("spatial_state.last_transition must be an object")

    if errors:
        raise SnapshotError("; ".join(errors))

    return snapshot


def load_snapshot(path: str | Path = DEFAULT_SNAPSHOT_PATH) -> dict[str, Any]:
    snapshot_path = Path(path)
    if not snapshot_path.exists():
        return normalize_snapshot({})

    with snapshot_path.open("r", encoding="utf-8") as f:
        return normalize_snapshot(json.load(f))


def save_snapshot(snapshot: dict[str, Any], path: str | Path = DEFAULT_SNAPSHOT_PATH) -> None:
    snapshot_path = Path(path)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    with snapshot_path.open("w", encoding="utf-8") as f:
        json.dump(normalize_snapshot(snapshot), f, ensure_ascii=False, indent=2)


def update_snapshot(
    snapshot: dict[str, Any],
    analysis: dict[str, Any],
    validation: dict[str, Any] | None = None,
    source_run_id: str | None = None,
) -> dict[str, Any]:
    analysis = _validate_analysis_result(analysis)
    next_snapshot = normalize_snapshot(snapshot)
    chapter_index = next_snapshot["chapter_index"]
    _apply_analysis_to_world_state(next_snapshot, analysis, chapter_index)
    _apply_analysis_to_characters(next_snapshot, analysis, chapter_index)
    next_snapshot["chapter_index"] += 1
    timeline_entry = {
        "chapter_index": chapter_index,
        "memory_id": _timeline_memory_id(chapter_index, "summary"),
        "memory_ids": _timeline_memory_ids(chapter_index, analysis),
        "name": f"chapter_{chapter_index}_summary",
        "summary": analysis.get("summary", ""),
        "events": analysis.get("events", []),
        "character_changes": analysis.get("character_changes", []),
        "world_changes": analysis.get("world_changes", []),
        "new_locations": analysis.get("new_locations", []),
        "conflicts": analysis.get("conflicts", []),
        "validation": validation or {},
    }
    if source_run_id:
        timeline_entry["source_run_id"] = source_run_id
    next_snapshot["timeline"].append(timeline_entry)
    return next_snapshot


def build_state_update_audit(
    *,
    snapshot: dict[str, Any],
    next_snapshot: dict[str, Any],
    analysis: dict[str, Any],
    memory_updates: list[dict[str, Any]] | None = None,
    applied: bool,
) -> dict[str, Any]:
    analysis = _validate_analysis_result(analysis)
    current = normalize_snapshot(snapshot)
    updated = normalize_snapshot(next_snapshot)
    updates = memory_updates or []
    audit = {
        "applied": bool(applied),
        "chapter_index": int(current.get("chapter_index") or 1),
        "next_chapter_index": int(updated.get("chapter_index") or current.get("chapter_index") or 1),
        "timeline_added": _delta_count(updated.get("timeline"), current.get("timeline")) if applied else 0,
        "character_update_count": len(_named_character_changes(analysis.get("character_changes"))),
        "location_update_count": len(_named_locations(analysis.get("new_locations"))),
        "world_change_count": len(_objects(analysis.get("world_changes"))),
        "memory_update_count": len(updates),
        "memory_update_types": _memory_update_type_counts(updates),
        "analysis_validation_ok": bool(analysis.get("validation_ok")),
    }
    return validate_schema(audit, "state_update_audit.schema.json")


def _apply_analysis_to_world_state(
    snapshot: dict[str, Any],
    analysis: dict[str, Any],
    chapter_index: int,
) -> None:
    world_state = snapshot.setdefault("world_state", {})
    locations = world_state.setdefault("locations", {})

    for location in analysis.get("new_locations", []):
        if not isinstance(location, str) or not location.strip():
            continue
        locations.setdefault(
            location.strip(),
            {
                "source": "chapter_analysis",
                "first_seen_chapter": chapter_index,
            },
        )

    world_changes = analysis.get("world_changes", [])
    if world_changes:
        world_state["last_world_changes"] = world_changes


def _apply_analysis_to_characters(
    snapshot: dict[str, Any],
    analysis: dict[str, Any],
    chapter_index: int,
) -> None:
    characters = snapshot.setdefault("characters", {})
    for change in _objects(analysis.get("character_changes")):
        name = change.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        clean_name = name.strip()
        existing = characters.get(clean_name)
        if not isinstance(existing, dict):
            existing = {}
            characters[clean_name] = existing

        for field in ("status", "current_location", "current_goal"):
            value = change.get(field)
            if isinstance(value, str) and value.strip():
                existing[field] = value.strip()

        text = change.get("text")
        if isinstance(text, str) and text.strip():
            existing["last_observation"] = text.strip()
        existing["last_seen_chapter"] = chapter_index


def _normalize_story_state(value: Any) -> dict[str, Any]:
    base = copy.deepcopy(DEFAULT_SNAPSHOT["story_state"])
    if isinstance(value, dict):
        base.update(copy.deepcopy(value))
    for field in ("last_chapter_ending", "last_scene_location", "required_opening_bridge"):
        base[field] = str(base.get(field) or "")
    for field in ("last_scene_characters", "open_threads"):
        raw_items = base.get(field)
        base[field] = [str(item) for item in raw_items if item] if isinstance(raw_items, list) else []
    return base


def _normalize_spatial_state(value: Any) -> dict[str, Any]:
    base = copy.deepcopy(DEFAULT_SNAPSHOT["spatial_state"])
    if isinstance(value, dict):
        base.update(copy.deepcopy(value))
    if not isinstance(base.get("spaces"), dict):
        base["spaces"] = {}
    if not isinstance(base.get("connections"), list):
        base["connections"] = []
    if not isinstance(base.get("character_positions"), dict):
        base["character_positions"] = {}
    if not isinstance(base.get("blocked_paths"), list):
        base["blocked_paths"] = []
    if not isinstance(base.get("last_transition"), dict):
        base["last_transition"] = {}
    return base


def _objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _validate_analysis_result(analysis: dict[str, Any]) -> dict[str, Any]:
    try:
        return validate_schema(analysis, "analysis_result.schema.json")
    except SchemaValidationError as exc:
        raise SnapshotError(str(exc)) from exc


def _named_character_changes(value: Any) -> list[dict[str, Any]]:
    return [
        change
        for change in _objects(value)
        if isinstance(change.get("name"), str) and change["name"].strip()
    ]


def _named_locations(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [location.strip() for location in value if isinstance(location, str) and location.strip()]


def _delta_count(after: Any, before: Any) -> int:
    after_count = len(after) if isinstance(after, list) else 0
    before_count = len(before) if isinstance(before, list) else 0
    return max(0, after_count - before_count)


def _memory_update_type_counts(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for update in updates:
        update_type = update.get("type")
        if isinstance(update_type, str) and update_type:
            counts[update_type] = counts.get(update_type, 0) + 1
    return [{"type": key, "count": counts[key]} for key in sorted(counts)]


def _timeline_memory_ids(chapter_index: int, analysis: dict[str, Any]) -> list[str]:
    memory_ids = [_timeline_memory_id(chapter_index, "summary")]
    event_count = len(_objects(analysis.get("events")))
    memory_ids.extend(_timeline_memory_id(chapter_index, f"event_{index}") for index in range(1, event_count + 1))
    return memory_ids


def _timeline_memory_id(chapter_index: int, name: str) -> str:
    return f"chapter_{chapter_index}:timeline_event:chapter_{chapter_index}_{name}"
