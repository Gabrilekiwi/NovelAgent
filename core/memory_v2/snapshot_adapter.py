from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from core.memory_v2.storage import load_canonical_memory
from core.memory_v2.validator import validate_canonical_memory
from core.state.snapshot import normalize_snapshot, validate_snapshot
from core.story_project.semantic_contracts import validate_story_project_semantic_state


PROMOTED_CHARACTER_DATA_FIELDS = ("role", "identity", "status", "current_goal")


def canonical_memory_to_snapshot(canonical_memory: dict[str, Any]) -> dict[str, Any]:
    memory = copy.deepcopy(validate_canonical_memory(canonical_memory))

    project_profile = {
        "book_id": memory["book_id"],
        "title": memory["title"],
        "language": memory["language"],
        "style_rules": _active_style_rules(memory.get("style_rules", [])),
    }
    world_state = _world_state(memory)
    characters = _characters(memory.get("characters", {}))
    locations = _locations(memory.get("locations", {}))
    story_state = _story_state(memory)
    spatial_state = _spatial_state(memory, characters, locations)

    snapshot = {
        "chapter_index": _chapter_index(memory),
        "project_profile": project_profile,
        "world_state": world_state,
        "characters": characters,
        "locations": locations,
        "timeline": copy.deepcopy(memory.get("timeline", [])),
        "constraints": copy.deepcopy(memory.get("constraints", [])),
        "active_constraints": _active_constraints(memory.get("constraints", [])),
        "open_threads": copy.deepcopy(memory.get("open_threads", [])),
        "style_rules": copy.deepcopy(memory.get("style_rules", [])),
        "story_state": story_state,
        "spatial_state": spatial_state,
        "memory_v2": {
            "schema_version": memory["schema_version"],
            "revision": memory["revision"],
            "source_index": copy.deepcopy(memory.get("source_index", {})),
            "source_resolution": copy.deepcopy(memory.get("source_resolution", {})),
        },
    }
    if memory.get("schema_version") == "2.2":
        snapshot["memory_v2"].update(
            {
                "authority_epoch": int(memory["authority_epoch"]),
                "head_event_hash": memory.get("head_event_hash"),
                "reducer_version": "memory-reducer-2.2",
            }
        )
        for field in (
            "relationships",
            "injuries",
            "inventories",
            "resources",
            "glossary",
            "corruption",
            "story_time",
            "foreshadowing",
        ):
            snapshot[field] = copy.deepcopy(memory[field])
    return validate_snapshot(normalize_snapshot(snapshot))


def load_canonical_memory_snapshot(path: str | Path) -> dict[str, Any]:
    return canonical_memory_to_snapshot(load_canonical_memory(path))


def rebuild_semantic_snapshot(
    story_project_state: dict[str, Any],
    memory_projection: dict[str, Any],
) -> dict[str, Any]:
    """Rebuild a snapshot with StoryProject facts as the authority layer.

    Memory contributes non-conflicting projection fields. For the same field or
    record id, parsed StoryProject text wins so replayed model memory can never
    overwrite a human-maintained fact.
    """

    story = copy.deepcopy(validate_story_project_semantic_state(story_project_state))
    memory = copy.deepcopy(validate_canonical_memory(memory_projection))
    if str(story["book_id"]) != str(memory["book_id"]):
        raise ValueError("StoryProjectSemanticState and Memory projection book_id must match")

    snapshot = canonical_memory_to_snapshot(memory)
    snapshot["book_id"] = str(story["book_id"])
    snapshot["chapter_index"] = int(story["chapter_index"])
    snapshot["world_state"] = _deep_authority_merge(snapshot.get("world_state", {}), story["world_state"])
    snapshot["story_state"] = _deep_authority_merge(snapshot.get("story_state", {}), story["story_state"])
    snapshot["spatial_state"] = _deep_authority_merge(snapshot.get("spatial_state", {}), story["spatial_state"])
    snapshot["characters"] = _deep_authority_merge(snapshot.get("characters", {}), story["characters"])
    snapshot["timeline"] = _merge_records_by_id(snapshot.get("timeline", []), story["timeline"])
    snapshot["constraints"] = _merge_records_by_id(snapshot.get("constraints", []), story["constraints"])
    snapshot["active_constraints"] = _active_constraints(snapshot["constraints"])
    snapshot["foreshadowing"] = copy.deepcopy(story["foreshadowing"])
    snapshot["story_project_semantics"] = {
        "source_digest": story["source_digest"],
        "parser_version": story["parser_version"],
        "layout_profile_version": story["layout_profile_version"],
        "provenance": copy.deepcopy(story["provenance"]),
        "conflicts": copy.deepcopy(story["conflicts"]),
    }
    return validate_snapshot(normalize_snapshot(snapshot))


def _chapter_index(memory: dict[str, Any]) -> int:
    value = memory.get("current_state", {}).get("chapter_index")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    story_time = memory.get("story_time")
    typed_value = story_time.get("chapter_index") if isinstance(story_time, dict) else None
    if isinstance(typed_value, int) and not isinstance(typed_value, bool) and typed_value >= 1:
        return typed_value
    return 1


def _world_state(memory: dict[str, Any]) -> dict[str, Any]:
    world_state = copy.deepcopy(memory.get("world", {}))
    if not isinstance(world_state, dict):
        world_state = {}
    world_state.setdefault("locations", {})
    return world_state


def _characters(raw_characters: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_characters, dict):
        return {}

    characters: dict[str, dict[str, Any]] = {}
    for character_id, raw_record in raw_characters.items():
        if not isinstance(raw_record, dict):
            continue
        record = copy.deepcopy(raw_record)
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        snapshot_record: dict[str, Any] = {
            "id": str(character_id),
            "name": str(record.get("name") or character_id),
            "data": copy.deepcopy(data),
        }
        if isinstance(record.get("state"), dict):
            snapshot_record["state"] = copy.deepcopy(record["state"])
        for key in PROMOTED_CHARACTER_DATA_FIELDS:
            if key in data and key not in snapshot_record:
                snapshot_record[key] = copy.deepcopy(data[key])
        for key, value in record.items():
            if key not in {"name", "data", "state"} and key not in snapshot_record:
                snapshot_record[key] = copy.deepcopy(value)
        characters[str(character_id)] = snapshot_record
    return characters


def _locations(raw_locations: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_locations, dict):
        return {}

    locations: dict[str, dict[str, Any]] = {}
    for location_id, raw_record in raw_locations.items():
        if not isinstance(raw_record, dict):
            continue
        record = copy.deepcopy(raw_record)
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        snapshot_record: dict[str, Any] = {
            "id": str(location_id),
            "name": str(record.get("name") or location_id),
            "data": copy.deepcopy(data),
        }
        for key, value in record.items():
            if key not in {"name", "data"} and key not in snapshot_record:
                snapshot_record[key] = copy.deepcopy(value)
        locations[str(location_id)] = snapshot_record
    return locations


def _story_state(memory: dict[str, Any]) -> dict[str, Any]:
    current_state = memory.get("current_state") if isinstance(memory.get("current_state"), dict) else {}
    open_threads = memory.get("open_threads") if isinstance(memory.get("open_threads"), list) else []
    story_state = {
        "last_chapter_ending": str(current_state.get("last_chapter_ending") or ""),
        "last_scene_location": str(current_state.get("last_scene_location") or ""),
        "last_scene_characters": _string_list(current_state.get("last_scene_characters")),
        "required_opening_bridge": str(current_state.get("required_opening_bridge") or ""),
        "active_conflicts": copy.deepcopy(current_state.get("active_conflicts", [])),
        "open_threads": [_thread_title(thread) for thread in open_threads if _thread_title(thread)],
    }
    for key, value in current_state.items():
        if key not in {"spatial_state", *story_state.keys()}:
            story_state[key] = copy.deepcopy(value)
    return story_state


def _spatial_state(
    memory: dict[str, Any],
    characters: dict[str, dict[str, Any]],
    locations: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    current_state = memory.get("current_state") if isinstance(memory.get("current_state"), dict) else {}
    raw_spatial = current_state.get("spatial_state") if isinstance(current_state.get("spatial_state"), dict) else {}
    spatial_state = {
        "spaces": copy.deepcopy(raw_spatial.get("spaces", {})) if isinstance(raw_spatial.get("spaces"), dict) else {},
        "connections": copy.deepcopy(raw_spatial.get("connections", [])) if isinstance(raw_spatial.get("connections"), list) else [],
        "character_positions": (
            copy.deepcopy(raw_spatial.get("character_positions", {}))
            if isinstance(raw_spatial.get("character_positions"), dict)
            else {}
        ),
        "blocked_paths": copy.deepcopy(raw_spatial.get("blocked_paths", [])) if isinstance(raw_spatial.get("blocked_paths"), list) else [],
        "last_transition": (
            copy.deepcopy(raw_spatial.get("last_transition", {}))
            if isinstance(raw_spatial.get("last_transition"), dict)
            else {}
        ),
    }

    for character_id, character in characters.items():
        state = character.get("state")
        if not isinstance(state, dict):
            continue
        current_location = state.get("current_location")
        if current_location in (None, ""):
            continue
        position_key = character.get("name") or character_id
        spatial_state["character_positions"][str(position_key)] = current_location

    for location_id, location in locations.items():
        location_name = str(location.get("name") or location_id)
        location_data = location.get("data") if isinstance(location.get("data"), dict) else {}
        existing = spatial_state["spaces"].get(location_name)
        if isinstance(existing, dict):
            spatial_state["spaces"][location_name] = {**copy.deepcopy(existing), **copy.deepcopy(location_data)}
        else:
            spatial_state["spaces"][location_name] = copy.deepcopy(location_data)
    return spatial_state


def _active_constraints(raw_constraints: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_constraints, list):
        return []
    return [copy.deepcopy(item) for item in raw_constraints if isinstance(item, dict) and item.get("status") == "active"]


def _active_style_rules(raw_style_rules: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_style_rules, list):
        return []
    return [copy.deepcopy(item) for item in raw_style_rules if isinstance(item, dict) and item.get("status") == "active"]


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value if item] if isinstance(value, list) else []


def _thread_title(thread: Any) -> str:
    if isinstance(thread, dict):
        return str(thread.get("title") or thread.get("text") or thread.get("summary") or thread.get("name") or "").strip()
    return str(thread).strip()


def _deep_authority_merge(base: Any, authority: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(authority, dict):
        return copy.deepcopy(authority)
    merged = copy.deepcopy(base)
    for key, value in authority.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_authority_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _merge_records_by_id(memory_records: Any, authority_records: Any) -> list[dict[str, Any]]:
    memory_items = [copy.deepcopy(item) for item in memory_records if isinstance(item, dict)] if isinstance(memory_records, list) else []
    authority_items = [copy.deepcopy(item) for item in authority_records if isinstance(item, dict)] if isinstance(authority_records, list) else []
    authority_by_id = {
        str(item["id"]): item
        for item in authority_items
        if isinstance(item.get("id"), str) and item["id"]
    }
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in memory_items:
        item_id = str(item.get("id") or "")
        if item_id and item_id in authority_by_id:
            merged.append(_deep_authority_merge(item, authority_by_id[item_id]))
            seen.add(item_id)
        else:
            merged.append(item)
            if item_id:
                seen.add(item_id)
    for item in authority_items:
        item_id = str(item.get("id") or "")
        if not item_id or item_id not in seen:
            merged.append(item)
            if item_id:
                seen.add(item_id)
    return merged


__all__ = [
    "canonical_memory_to_snapshot",
    "load_canonical_memory_snapshot",
    "rebuild_semantic_snapshot",
]
