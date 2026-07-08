from __future__ import annotations

import copy
from typing import Any

from core.memory_v2.events import create_memory_event, validate_memory_event
from core.memory_v2.patch import validate_memory_patch
from core.memory_v2.validator import validate_canonical_memory


class MemoryReducerError(ValueError):
    pass


SUPPORTED_REDUCER_OPS = {
    "append_timeline_event",
    "update_character_state",
    "update_current_state",
    "update_world",
    "upsert_character",
    "upsert_constraint",
    "upsert_location",
    "upsert_open_thread",
}


def apply_memory_patch(canonical_memory: dict[str, Any], patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    validated_memory = validate_canonical_memory(canonical_memory)
    validated_patch = validate_memory_patch(patch)
    operations = validated_patch.get("operations", [])
    if not operations:
        return copy.deepcopy(validated_memory), []

    working = copy.deepcopy(validated_memory)
    patch_source = _patch_source(validated_patch)
    patch_id = str(validated_patch["patch_id"])
    _record_source_index(working, validated_patch)

    events: list[dict[str, Any]] = []
    revision = int(working.get("revision", 1))
    for operation in operations:
        op = str(operation.get("op") or "")
        if op not in SUPPORTED_REDUCER_OPS:
            raise MemoryReducerError(f"unsupported memory operation: {op}")
        revision += 1
        event = _apply_operation(working, operation, revision, patch_source)
        working["revision"] = revision
        events.append(validate_memory_event(event))
        _record_source_resolution(working, event, patch_id)

    validate_canonical_memory(working)
    return working, events


def _apply_operation(
    memory: dict[str, Any],
    operation: dict[str, Any],
    revision: int,
    source: dict[str, Any],
) -> dict[str, Any]:
    op = str(operation["op"])
    if op == "upsert_character":
        return _upsert_map_record(memory, operation, revision, source, "characters", default_name_prefix="character")
    if op == "upsert_location":
        return _upsert_map_record(memory, operation, revision, source, "locations", default_name_prefix="location")
    if op == "upsert_constraint":
        return _upsert_list_record(memory, operation, revision, source, "constraints", default_status="active")
    if op == "append_timeline_event":
        return _upsert_list_record(memory, operation, revision, source, "timeline", default_summary=True)
    if op == "update_current_state":
        return _update_current_state(memory, operation, revision, source)
    if op == "upsert_open_thread":
        return _upsert_list_record(memory, operation, revision, source, "open_threads", default_status="open")
    if op == "update_character_state":
        return _update_character_state(memory, operation, revision, source)
    if op == "update_world":
        return _update_world(memory, operation, revision, source)
    raise MemoryReducerError(f"unsupported memory operation: {op}")


def _upsert_map_record(
    memory: dict[str, Any],
    operation: dict[str, Any],
    revision: int,
    source: dict[str, Any],
    collection: str,
    *,
    default_name_prefix: str,
) -> dict[str, Any]:
    subject_id = _operation_id(operation)
    collection_map = _ensure_dict(memory, collection)
    old_value = copy.deepcopy(collection_map.get(subject_id))
    value = _operation_value_object(operation)
    default_record = {"name": subject_id, "data": {}}
    new_value = _merge_record(default_record if old_value is None else old_value, value)
    if not isinstance(new_value.get("name"), str) or not new_value["name"].strip():
        new_value["name"] = f"{default_name_prefix}_{subject_id}"
    if not isinstance(new_value.get("data"), dict):
        new_value["data"] = {}
    collection_map[subject_id] = new_value
    field = f"{collection}.{subject_id}"
    return _event(operation, revision, source, subject_id, field, old_value, copy.deepcopy(new_value))


def _upsert_list_record(
    memory: dict[str, Any],
    operation: dict[str, Any],
    revision: int,
    source: dict[str, Any],
    collection: str,
    *,
    default_status: str | None = None,
    default_summary: bool = False,
) -> dict[str, Any]:
    subject_id = _operation_id(operation)
    records = _ensure_list(memory, collection)
    existing_index = _find_record_index(records, subject_id)
    old_value = copy.deepcopy(records[existing_index]) if existing_index is not None else None
    value = _operation_value_object(operation)
    value["id"] = subject_id
    if default_summary:
        value = _canonical_timeline_record(subject_id, value)
    if default_status is not None:
        value.setdefault("status", default_status)
    value.setdefault("data", {})
    if not isinstance(value["data"], dict):
        value["data"] = {"value": value["data"]}
    new_value = _merge_record(old_value or {}, value)
    if existing_index is None:
        records.append(new_value)
    else:
        records[existing_index] = new_value
    field = f"{collection}.{subject_id}"
    return _event(operation, revision, source, subject_id, field, old_value, copy.deepcopy(new_value))


def _canonical_timeline_record(subject_id: str, value: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(value.get("data", {})) if isinstance(value.get("data"), dict) else {}
    for key, field_value in value.items():
        if key not in {"id", "name", "chapter_index", "summary", "data"}:
            data.setdefault(key, copy.deepcopy(field_value))
    summary = value.get("summary") or value.get("event") or value.get("text") or subject_id
    record = {
        "id": subject_id,
        "summary": str(summary),
        "data": data,
    }
    if isinstance(value.get("name"), str) and value["name"].strip():
        record["name"] = value["name"]
    if isinstance(value.get("chapter_index"), int) and not isinstance(value.get("chapter_index"), bool):
        record["chapter_index"] = value["chapter_index"]
    return record


def _update_current_state(
    memory: dict[str, Any],
    operation: dict[str, Any],
    revision: int,
    source: dict[str, Any],
) -> dict[str, Any]:
    value = _operation_value_object(operation)
    current_state = _ensure_dict(memory, "current_state")
    old_value = {key: copy.deepcopy(current_state.get(key)) for key in value}
    current_state.update(copy.deepcopy(value))
    new_value = {key: copy.deepcopy(current_state.get(key)) for key in value}
    return _event(operation, revision, source, None, "current_state", old_value, new_value)


def _update_character_state(
    memory: dict[str, Any],
    operation: dict[str, Any],
    revision: int,
    source: dict[str, Any],
) -> dict[str, Any]:
    subject_id = _operation_id(operation)
    field_path = operation.get("field")
    if not isinstance(field_path, str) or not field_path.strip():
        raise MemoryReducerError("update_character_state requires a field")
    characters = _ensure_dict(memory, "characters")
    character = characters.setdefault(subject_id, {"name": subject_id, "data": {}})
    if not isinstance(character, dict):
        raise MemoryReducerError(f"character {subject_id} must be an object")
    character.setdefault("name", subject_id)
    character.setdefault("data", {})

    old_value = _get_dotted_value(character, field_path)
    _set_dotted_value(character, field_path, copy.deepcopy(operation.get("value")))
    new_value = _get_dotted_value(character, field_path)
    return _event(
        operation,
        revision,
        source,
        subject_id,
        f"characters.{subject_id}.{field_path}",
        old_value,
        copy.deepcopy(new_value),
    )


def _update_world(
    memory: dict[str, Any],
    operation: dict[str, Any],
    revision: int,
    source: dict[str, Any],
) -> dict[str, Any]:
    value = operation.get("value", {})
    if not isinstance(value, dict):
        raise MemoryReducerError("update_world value must be an object")
    world = _ensure_dict(memory, "world")
    old_value = {key: copy.deepcopy(world.get(key)) for key in value}
    world.update(copy.deepcopy(value))
    new_value = {key: copy.deepcopy(world.get(key)) for key in value}
    return _event(operation, revision, source, None, "world", old_value, new_value)


def _event(
    operation: dict[str, Any],
    revision: int,
    source: dict[str, Any],
    subject_id: str | None,
    field: str,
    old_value: Any,
    new_value: Any,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    operation_data = operation.get("data")
    if isinstance(operation_data, dict) and operation_data:
        metadata["operation_data"] = copy.deepcopy(operation_data)
    return create_memory_event(
        event_id=f"evt_{revision:06d}",
        revision=revision,
        op=str(operation["op"]),
        source=source,
        subject_id=subject_id,
        field=field,
        old_value=copy.deepcopy(old_value),
        new_value=copy.deepcopy(new_value),
        metadata=metadata,
    )


def _patch_source(patch: dict[str, Any]) -> dict[str, Any]:
    source = copy.deepcopy(patch.get("source", {}))
    source["patch_id"] = patch["patch_id"]
    return source


def _record_source_index(memory: dict[str, Any], patch: dict[str, Any]) -> None:
    source_index = _ensure_dict(memory, "source_index")
    source = copy.deepcopy(patch.get("source", {}))
    source_index[str(patch["patch_id"])] = source


def _record_source_resolution(memory: dict[str, Any], event: dict[str, Any], patch_id: str) -> None:
    field = event.get("field")
    if not isinstance(field, str) or not field:
        return
    source_resolution = _ensure_dict(memory, "source_resolution")
    source_resolution[field] = {
        "chosen_source": patch_id,
        "reason": "latest_patch_operation",
    }


def _merge_record(old_value: dict[str, Any], new_value: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(old_value)
    for key, value in new_value.items():
        if key == "data" and isinstance(merged.get("data"), dict) and isinstance(value, dict):
            merged["data"] = {**merged["data"], **copy.deepcopy(value)}
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _operation_id(operation: dict[str, Any]) -> str:
    subject_id = operation.get("id")
    if not isinstance(subject_id, str) or not subject_id.strip():
        raise MemoryReducerError(f"{operation.get('op')} requires an id")
    return subject_id.strip()


def _operation_value_object(operation: dict[str, Any]) -> dict[str, Any]:
    value = operation.get("value", {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MemoryReducerError(f"{operation.get('op')} value must be an object")
    return copy.deepcopy(value)


def _ensure_dict(memory: dict[str, Any], key: str) -> dict[str, Any]:
    value = memory.setdefault(key, {})
    if not isinstance(value, dict):
        raise MemoryReducerError(f"canonical_memory.{key} must be an object")
    return value


def _ensure_list(memory: dict[str, Any], key: str) -> list[Any]:
    value = memory.setdefault(key, [])
    if not isinstance(value, list):
        raise MemoryReducerError(f"canonical_memory.{key} must be a list")
    return value


def _find_record_index(records: list[Any], subject_id: str) -> int | None:
    for index, record in enumerate(records):
        if isinstance(record, dict) and record.get("id") == subject_id:
            return index
    return None


def _get_dotted_value(record: dict[str, Any], field_path: str) -> Any:
    current: Any = record
    for part in field_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return copy.deepcopy(current)


def _set_dotted_value(record: dict[str, Any], field_path: str, value: Any) -> None:
    current: dict[str, Any] = record
    parts = [part for part in field_path.split(".") if part]
    if not parts:
        raise MemoryReducerError("field path must not be empty")
    for part in parts[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise MemoryReducerError(f"field path segment {part} is not an object")
        current = child
    current[parts[-1]] = value


__all__ = [
    "MemoryReducerError",
    "SUPPORTED_REDUCER_OPS",
    "apply_memory_patch",
]
