from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from core.memory_v2.patch import create_memory_patch
from core.state.memory import MemoryError, normalize_memory_context
from core.state.notion_export import normalize_notion_export


SUPPORTED_V1_TYPES = {
    "character",
    "constraint",
    "location",
    "spatial_state",
    "story_state",
    "timeline_event",
    "world_state",
}


def load_v1_memory_file(path: str | Path) -> dict[str, Any] | list[Any]:
    memory_path = Path(path)
    with memory_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def import_v1_memory_file_to_patch(path: str | Path) -> dict[str, Any]:
    memory_path = Path(path)
    return import_v1_memory_to_patch(
        load_v1_memory_file(memory_path),
        source_kind="local_memory",
        source_path=str(memory_path),
    )


def import_v1_memory_to_patch(
    memory: dict[str, Any] | list[Any],
    source_kind: str = "local_memory",
    source_path: str | None = None,
    patch_id: str | None = None,
) -> dict[str, Any]:
    normalized = _normalize_v1_memory(memory)
    operations = _items_to_operations(normalized.get("items", []))
    resolved_patch_id = patch_id or _default_patch_id(source_kind)
    return create_memory_patch(
        patch_id=resolved_patch_id,
        source_kind=source_kind,
        source_path=source_path,
        operations=operations,
        metadata={"source_item_count": len(normalized.get("items", []))},
    )


def _normalize_v1_memory(memory: dict[str, Any] | list[Any]) -> dict[str, Any]:
    if _looks_like_notion_export(memory):
        return normalize_notion_export(memory)

    if isinstance(memory, dict):
        if isinstance(memory.get("items"), list):
            items = memory["items"]
        elif _looks_like_single_item(memory):
            items = [memory]
        else:
            items = []
        context = {
            "source": memory.get("source", "v1-memory"),
            "status": memory.get("status", "ready"),
            "items": [_normalize_item_shape(item, index) for index, item in enumerate(items)],
        }
        return normalize_memory_context(context)

    if isinstance(memory, list):
        return normalize_memory_context([_normalize_item_shape(item, index) for index, item in enumerate(memory)])

    raise MemoryError("v1 memory must be an object or list")


def _items_to_operations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    counters: dict[str, int] = {}
    for item in items:
        item_type = str(item.get("type", "")).strip().lower()
        if item_type == "character":
            operations.append(_character_operation(item, counters))
        elif item_type == "location":
            operations.append(_location_operation(item, counters))
        elif item_type == "constraint":
            operations.append(_constraint_operation(item, counters))
        elif item_type == "timeline_event":
            operations.append(_timeline_operation(item, counters))
        elif item_type == "world_state":
            operations.append({"op": "update_world", "value": _item_data(item)})
        elif item_type == "story_state":
            operations.extend(_story_state_operations(item, counters))
        elif item_type == "spatial_state":
            operations.extend(_spatial_state_operations(item, counters))
    return operations


def _character_operation(item: dict[str, Any], counters: dict[str, int]) -> dict[str, Any]:
    data = _item_data(item)
    name = _item_name(item, data)
    item_id = _existing_id(item) or _stable_id("char", name, "character", counters)
    return {
        "op": "upsert_character",
        "id": item_id,
        "value": {
            "name": name or item_id,
            "data": data,
        },
    }


def _location_operation(item: dict[str, Any], counters: dict[str, int]) -> dict[str, Any]:
    data = _item_data(item)
    name = _item_name(item, data)
    item_id = _existing_id(item) or _stable_id("loc", name, "location", counters)
    return {
        "op": "upsert_location",
        "id": item_id,
        "value": {
            "name": name or item_id,
            "data": data,
        },
    }


def _constraint_operation(item: dict[str, Any], counters: dict[str, int]) -> dict[str, Any]:
    data = _item_data(item)
    text = _first_string(data, "text", "rule", "summary", "value") or str(item.get("name") or "").strip()
    item_id = _existing_id(item) or _stable_id("constraint", text, "constraint", counters)
    return {
        "op": "upsert_constraint",
        "id": item_id,
        "value": {
            "text": text or item_id,
            "status": str(data.get("status") or "active"),
            "data": data,
        },
    }


def _timeline_operation(item: dict[str, Any], counters: dict[str, int]) -> dict[str, Any]:
    data = _item_data(item)
    summary = _first_string(data, "summary", "event", "text", "value") or str(item.get("name") or "").strip()
    item_id = _existing_id(item) or _stable_id("event", summary, "timeline_event", counters)
    value = _select_fields(
        data,
        "chapter_index",
        "day",
        "time",
        "summary",
        "event",
        "text",
        "characters",
        "locations",
    )
    value.setdefault("summary", summary or item_id)
    value["data"] = data
    return {
        "op": "append_timeline_event",
        "id": item_id,
        "value": value,
    }


def _story_state_operations(item: dict[str, Any], counters: dict[str, int]) -> list[dict[str, Any]]:
    data = _item_data(item)
    operations: list[dict[str, Any]] = []
    current_state = _select_fields(
        data,
        "last_chapter_ending",
        "last_scene_location",
        "last_scene_characters",
        "required_opening_bridge",
        "active_conflicts",
    )
    if current_state:
        operations.append(
            {
                "op": "update_current_state",
                "value": current_state,
                "data": {"source_type": "story_state"},
            }
        )

    for index, thread in enumerate(_as_list(data.get("open_threads")), start=1):
        thread_title = _thread_title(thread)
        thread_id = _stable_id("thread", thread_title, "open_thread", counters, fallback_index=index)
        operations.append(
            {
                "op": "upsert_open_thread",
                "id": thread_id,
                "value": {
                    "title": thread_title or thread_id,
                    "status": "open",
                    "data": thread if isinstance(thread, dict) else {},
                },
            }
        )
    return operations


def _spatial_state_operations(item: dict[str, Any], counters: dict[str, int]) -> list[dict[str, Any]]:
    data = _item_data(item)
    operations: list[dict[str, Any]] = []
    spatial_state = _select_fields(
        data,
        "connections",
        "blocked_paths",
        "last_transition",
    )
    if spatial_state:
        operations.append(
            {
                "op": "update_current_state",
                "value": {"spatial_state": spatial_state},
                "data": {"source_type": "spatial_state"},
            }
        )

    character_positions = data.get("character_positions")
    if isinstance(character_positions, dict):
        for character_name, location_name in sorted(character_positions.items(), key=lambda entry: str(entry[0])):
            character_id = _stable_id("char", str(character_name), "character", counters)
            operations.append(
                {
                    "op": "update_character_state",
                    "id": character_id,
                    "field": "state.current_location",
                    "value": location_name,
                    "data": {"source_field": "character_positions"},
                }
            )

    for location_name, location_data in _iter_location_states(data):
        location_id = _stable_id("loc", location_name, "location", counters)
        operations.append(
            {
                "op": "upsert_location",
                "id": location_id,
                "value": {
                    "name": location_name,
                    "data": location_data if isinstance(location_data, dict) else {"value": location_data},
                },
            }
        )
    return operations


def _iter_location_states(data: dict[str, Any]) -> list[tuple[str, Any]]:
    raw_locations = data.get("location_states")
    if raw_locations is None:
        raw_locations = data.get("spaces")
    if not isinstance(raw_locations, dict):
        return []
    return sorted(((str(name), value) for name, value in raw_locations.items()), key=lambda entry: entry[0])


def _normalize_item_shape(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise MemoryError(f"memory item {index} must be an object")
    item_type = item.get("type", item.get("item_type"))
    if not isinstance(item_type, str) or not item_type.strip():
        raise MemoryError(f"memory item {index} requires a type")
    normalized_type = item_type.strip().lower()
    if normalized_type not in SUPPORTED_V1_TYPES:
        raise MemoryError(f"memory item {index} has unsupported type '{item_type}'")

    data = copy.deepcopy(item.get("data", {}))
    if data is None:
        data = {}
    if not isinstance(data, dict):
        data = {"value": data}
    for key, value in item.items():
        if key in {"data", "type", "item_type", "id", "memory_id", "name", "title", "key", "source_run_id"}:
            continue
        data.setdefault(key, copy.deepcopy(value))

    normalized = {
        "type": normalized_type,
        "name": item.get("name") or item.get("title") or item.get("key"),
        "data": data,
    }
    item_id = item.get("id") or item.get("memory_id")
    if item_id:
        normalized["id"] = str(item_id)
    if item.get("source_run_id"):
        normalized["source_run_id"] = item.get("source_run_id")
    return normalized


def _looks_like_notion_export(memory: Any) -> bool:
    if isinstance(memory, dict) and isinstance(memory.get("pages"), list):
        return True
    return isinstance(memory, list) and bool(memory) and all(
        isinstance(item, dict) and "properties" in item for item in memory
    )


def _looks_like_single_item(memory: dict[str, Any]) -> bool:
    return isinstance(memory.get("type") or memory.get("item_type"), str)


def _default_patch_id(source_kind: str) -> str:
    return "patch_import_v1_default"


def _item_data(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data", {})
    return copy.deepcopy(data) if isinstance(data, dict) else {"value": data}


def _item_name(item: dict[str, Any], data: dict[str, Any]) -> str:
    return str(item.get("name") or data.get("name") or data.get("title") or data.get("key") or "").strip()


def _existing_id(item: dict[str, Any]) -> str | None:
    item_id = item.get("id") or item.get("memory_id")
    return str(item_id).strip() if item_id not in (None, "") else None


def _stable_id(
    prefix: str,
    raw_value: Any,
    fallback_prefix: str,
    counters: dict[str, int],
    *,
    fallback_index: int | None = None,
) -> str:
    slug = _slug(str(raw_value or ""))
    if slug:
        return f"{prefix}_{slug}"
    if fallback_index is None:
        counters[fallback_prefix] = counters.get(fallback_prefix, 0) + 1
        fallback_index = counters[fallback_prefix]
    return f"{fallback_prefix}_{fallback_index:04d}"


def _slug(value: str) -> str:
    chunks: list[str] = []
    previous_separator = False
    for char in value.strip().lower():
        if char.isalnum():
            chunks.append(char)
            previous_separator = False
        else:
            if not previous_separator and chunks:
                chunks.append("_")
                previous_separator = True
    return "".join(chunks).strip("_")


def _first_string(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _select_fields(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: copy.deepcopy(data[key]) for key in keys if key in data}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _thread_title(thread: Any) -> str:
    if isinstance(thread, dict):
        return str(thread.get("title") or thread.get("text") or thread.get("summary") or thread.get("name") or "").strip()
    return str(thread).strip()


__all__ = [
    "import_v1_memory_file_to_patch",
    "import_v1_memory_to_patch",
    "load_v1_memory_file",
]
