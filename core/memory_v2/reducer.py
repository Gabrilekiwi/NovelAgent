from __future__ import annotations

import copy
import hashlib
from types import MappingProxyType
from typing import Any

from core.memory_v2.canonical import canonical_json_hash
from core.memory_v2.events import (
    TYPED_MEMORY_EVENT_SCHEMA_VERSION,
    create_memory_event,
    upcast_memory_event,
    validate_memory_event,
)
from core.memory_v2.models import TYPED_CANONICAL_MEMORY_SCHEMA_VERSION
from core.memory_v2.patch import validate_memory_patch
from core.memory_v2.typed import TYPED_COLLECTIONS
from core.memory_v2.validator import validate_canonical_memory
from core.memory_v2.versions import (
    CURRENT_REDUCER_VERSION,
    LEGACY_REDUCER_VERSION,
    UnsupportedMemoryVersionError,
    require_supported_reducer_version,
)


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

TYPED_REDUCER_OPS = SUPPORTED_REDUCER_OPS | {
    "delete_record",
    "update_story_time",
    "upsert_corruption",
    "upsert_foreshadowing",
    "upsert_glossary_entry",
    "upsert_injury",
    "upsert_inventory",
    "upsert_relationship",
    "upsert_resource",
}


def apply_memory_patch(
    canonical_memory: dict[str, Any],
    patch: dict[str, Any],
    *,
    reducer_version: str | None = None,
    event_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    inferred_version = (
        CURRENT_REDUCER_VERSION
        if canonical_memory.get("schema_version") == TYPED_CANONICAL_MEMORY_SCHEMA_VERSION
        else LEGACY_REDUCER_VERSION
    )
    reducer = resolve_memory_reducer(reducer_version or inferred_version)
    return reducer(canonical_memory, patch, event_context=event_context)


def _apply_memory_patch_v21(
    canonical_memory: dict[str, Any],
    patch: dict[str, Any],
    *,
    event_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    del event_context
    validated_memory = validate_canonical_memory(canonical_memory)
    if validated_memory.get("schema_version") != "2.0":
        raise MemoryReducerError("memory-reducer-2.1 requires CanonicalMemory 2.0")
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


def _apply_memory_patch_v22(
    canonical_memory: dict[str, Any],
    patch: dict[str, Any],
    *,
    event_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    validated_memory = validate_canonical_memory(canonical_memory)
    if validated_memory.get("schema_version") != "2.2":
        raise MemoryReducerError("memory-reducer-2.2 requires CanonicalMemory 2.2")
    validated_patch = validate_memory_patch(patch)
    operations = validated_patch.get("operations", [])
    if not operations:
        return copy.deepcopy(validated_memory), []
    context = _validate_event_context(event_context, validated_memory)

    working = copy.deepcopy(validated_memory)
    patch_source = _patch_source(validated_patch)
    patch_id = str(validated_patch["patch_id"])
    _record_source_index(working, validated_patch)
    events: list[dict[str, Any]] = []

    for operation in operations:
        op = str(operation.get("op") or "")
        if op not in TYPED_REDUCER_OPS:
            raise MemoryReducerError(f"unsupported memory operation for {CURRENT_REDUCER_VERSION}: {op}")
        previous_revision = int(working["revision"])
        previous_head = working.get("head_event_hash")
        revision = previous_revision + 1
        subject_id, field, before, after = _mutate_operation_v22(working, operation, revision)
        metadata: dict[str, Any] = {}
        operation_data = operation.get("data")
        if isinstance(operation_data, dict) and operation_data:
            metadata["operation_data"] = copy.deepcopy(operation_data)
        event = create_memory_event(
            event_id=f"evt_{revision:06d}",
            revision=revision,
            op=op,
            source=patch_source,
            subject_id=subject_id,
            field=field,
            schema_version=TYPED_MEMORY_EVENT_SCHEMA_VERSION,
            before=copy.deepcopy(before),
            after=copy.deepcopy(after),
            precondition={
                "expected_revision": previous_revision,
                "expected_head_event_hash": previous_head,
                "expected_field_hash": canonical_json_hash(before),
            },
            chapter_body=context.get("chapter_body"),
            chapter_body_sha256=context["chapter_body_sha256"],
            evidence_spans=context["evidence_spans"],
            authority_epoch=int(context["authority_epoch"]),
            reducer_version=CURRENT_REDUCER_VERSION,
            metadata=metadata,
        )
        working["revision"] = revision
        working["head_event_hash"] = event["event_hash"]
        events.append(event)
        _record_source_resolution(working, event, patch_id)
        validate_canonical_memory(working)

    return working, events


def apply_memory_events(
    canonical_memory: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    reducer_version: str,
) -> dict[str, Any]:
    """Replay immutable events with the reducer implementation frozen by version."""

    version = _validated_reducer_version(reducer_version)
    if version != CURRENT_REDUCER_VERSION:
        raise MemoryReducerError("direct event replay is available only for Memory 2.2 events")
    working = copy.deepcopy(validate_canonical_memory(canonical_memory))
    if working.get("schema_version") != "2.2":
        raise MemoryReducerError("memory-reducer-2.2 requires CanonicalMemory 2.2")
    for raw_event in events:
        event = upcast_memory_event(raw_event)
        if event.get("schema_version") != "2.2" or event.get("reducer_version") != version:
            raise MemoryReducerError("event schema/reducer does not match memory-reducer-2.2")
        if int(event["authority_epoch"]) != int(working["authority_epoch"]):
            raise MemoryReducerError("event authority_epoch does not match canonical memory")
        precondition = event["precondition"]
        if int(precondition["expected_revision"]) != int(working["revision"]):
            raise MemoryReducerError("event expected_revision does not match canonical memory")
        if precondition["expected_head_event_hash"] != working.get("head_event_hash"):
            raise MemoryReducerError("event expected_head_event_hash does not match canonical memory")
        source = event.get("source")
        if isinstance(source, dict) and isinstance(source.get("patch_id"), str):
            patch_id = str(source["patch_id"])
            source_index = _ensure_dict(working, "source_index")
            indexed_source = copy.deepcopy(source)
            indexed_source.pop("patch_id", None)
            source_index.setdefault(patch_id, indexed_source)
        actual_before = _read_event_field(working, str(event["field"]))
        if actual_before != event["before"]:
            raise MemoryReducerError("event before value does not match canonical memory")
        if canonical_json_hash(actual_before) != precondition["expected_field_hash"]:
            raise MemoryReducerError("event expected_field_hash does not match canonical memory")
        _write_event_field(working, str(event["field"]), copy.deepcopy(event["after"]))
        working["revision"] = int(event["revision"])
        working["head_event_hash"] = str(event["event_hash"])
        if isinstance(source, dict) and isinstance(source.get("patch_id"), str):
            _record_source_resolution(working, event, str(source["patch_id"]))
        validate_canonical_memory(working)
    return working


def apply_genesis_event(event: dict[str, Any]) -> dict[str, Any]:
    validated = validate_memory_event(event)
    if validated.get("schema_version") != "2.2" or validated.get("op") != "genesis":
        raise MemoryReducerError("genesis requires a Memory 2.2 genesis event")
    if validated.get("field") != "$" or validated.get("before") is not None:
        raise MemoryReducerError("genesis event must replace the empty root")
    precondition = validated["precondition"]
    if precondition != {
        "expected_revision": 0,
        "expected_head_event_hash": None,
        "expected_field_hash": canonical_json_hash(None),
    }:
        raise MemoryReducerError("genesis event has an invalid precondition")
    projection = copy.deepcopy(validate_canonical_memory(validated["after"]))
    if projection.get("schema_version") != "2.2" or int(projection["revision"]) != 1:
        raise MemoryReducerError("genesis event after value must be CanonicalMemory 2.2 revision 1")
    if projection.get("head_event_hash") is not None:
        raise MemoryReducerError("genesis event after value must have a null pre-genesis head")
    if int(projection["authority_epoch"]) != int(validated["authority_epoch"]):
        raise MemoryReducerError("genesis authority_epoch mismatch")
    projection["head_event_hash"] = str(validated["event_hash"])
    return validate_canonical_memory(projection)


def resolve_memory_reducer(reducer_version: str):
    version = _validated_reducer_version(reducer_version)
    reducer = REDUCER_REGISTRY.get(version)
    if reducer is None:
        raise MemoryReducerError(f"no frozen reducer registered for {version}")
    return reducer


def _validated_reducer_version(value: object) -> str:
    try:
        return require_supported_reducer_version(value)
    except UnsupportedMemoryVersionError as exc:
        raise MemoryReducerError(str(exc)) from exc


def _validate_event_context(
    event_context: dict[str, Any] | None,
    memory: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(event_context, dict):
        raise MemoryReducerError("memory-reducer-2.2 requires event_context")
    context = copy.deepcopy(event_context)
    context_epoch = context.get("authority_epoch")
    if isinstance(context_epoch, bool) or not isinstance(context_epoch, int):
        raise MemoryReducerError("event_context authority_epoch must be a positive integer")
    if context_epoch != int(memory["authority_epoch"]):
        raise MemoryReducerError("event_context authority_epoch does not match canonical memory")
    body = context.get("chapter_body")
    body_hash = context.get("chapter_body_sha256")
    if not isinstance(body, str):
        raise MemoryReducerError("event_context requires chapter_body so evidence offsets can be verified")
    if not isinstance(context.get("evidence_spans"), list) or not context["evidence_spans"]:
        raise MemoryReducerError("event_context requires evidence_spans")
    computed_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if body_hash is not None and body_hash != computed_hash:
        raise MemoryReducerError("event_context chapter_body_sha256 mismatch")
    context["chapter_body_sha256"] = computed_hash
    return context


def _mutate_operation_v22(
    memory: dict[str, Any],
    operation: dict[str, Any],
    revision: int,
) -> tuple[str | None, str, Any, Any]:
    op = str(operation["op"])
    collection_by_op = {
        "upsert_character": "characters",
        "upsert_location": "locations",
        "upsert_relationship": "relationships",
        "upsert_injury": "injuries",
        "upsert_inventory": "inventories",
        "upsert_resource": "resources",
        "upsert_glossary_entry": "glossary",
        "upsert_corruption": "corruption",
        "upsert_foreshadowing": "foreshadowing",
    }
    if op in collection_by_op:
        collection = collection_by_op[op]
        record_id = _operation_id(operation)
        records = _ensure_dict(memory, collection)
        before = copy.deepcopy(records.get(record_id))
        value = _operation_value_object(operation)
        defaults = _typed_record_defaults(collection, record_id, revision)
        after = _merge_record(defaults if before is None else before, value)
        after["id"] = record_id
        after.setdefault("data", {})
        records[record_id] = copy.deepcopy(after)
        return record_id, f"{collection}.{record_id}", before, after
    if op == "delete_record":
        collection = operation.get("field")
        if not isinstance(collection, str) or collection not in TYPED_COLLECTIONS:
            raise MemoryReducerError("delete_record field must name a typed collection")
        record_id = _operation_id(operation)
        records = _ensure_dict(memory, collection)
        if record_id not in records:
            raise MemoryReducerError(f"cannot delete missing {collection}.{record_id}")
        before = copy.deepcopy(records.pop(record_id))
        return record_id, f"{collection}.{record_id}", before, None
    if op == "update_story_time":
        before = copy.deepcopy(memory["story_time"])
        value = _operation_value_object(operation)
        after = {**before, **value}
        memory["story_time"] = copy.deepcopy(after)
        return None, "story_time", before, after
    if op == "update_world":
        value = _operation_value_object(operation)
        world = _ensure_dict(memory, "world")
        before = copy.deepcopy(world)
        world.update(copy.deepcopy(value))
        after = copy.deepcopy(world)
        return None, "world", before, after
    if op == "update_current_state":
        value = _operation_value_object(operation)
        current = _ensure_dict(memory, "current_state")
        before = copy.deepcopy(current)
        current.update(copy.deepcopy(value))
        after = copy.deepcopy(current)
        return None, "current_state", before, after
    if op == "update_character_state":
        character_id = _operation_id(operation)
        field_path = operation.get("field")
        if not isinstance(field_path, str) or not field_path.strip():
            raise MemoryReducerError("update_character_state requires a field")
        characters = _ensure_dict(memory, "characters")
        character = characters.setdefault(
            character_id,
            _typed_record_defaults("characters", character_id, revision),
        )
        before = _get_dotted_value(character, field_path)
        _set_dotted_value(character, field_path, copy.deepcopy(operation.get("value")))
        after = _get_dotted_value(character, field_path)
        return character_id, f"characters.{character_id}.{field_path}", before, after
    if op == "append_timeline_event":
        event_id = _operation_id(operation)
        timeline = _ensure_list(memory, "timeline")
        index = _find_record_index(timeline, event_id)
        before = copy.deepcopy(timeline[index]) if index is not None else None
        value = _canonical_timeline_record(event_id, _operation_value_object(operation))
        after = _merge_record(before or {}, value)
        if index is None:
            timeline.append(copy.deepcopy(after))
        else:
            timeline[index] = copy.deepcopy(after)
        return event_id, f"timeline.{event_id}", before, after
    if op in {"upsert_constraint", "upsert_open_thread"}:
        collection = "constraints" if op == "upsert_constraint" else "open_threads"
        record_id = _operation_id(operation)
        records = _ensure_list(memory, collection)
        index = _find_record_index(records, record_id)
        before = copy.deepcopy(records[index]) if index is not None else None
        value = _operation_value_object(operation)
        value["id"] = record_id
        value.setdefault("status", "active" if collection == "constraints" else "open")
        value.setdefault("data", {})
        after = _merge_record(before or {}, value)
        if index is None:
            records.append(copy.deepcopy(after))
        else:
            records[index] = copy.deepcopy(after)
        return record_id, f"{collection}.{record_id}", before, after
    raise MemoryReducerError(f"unsupported memory operation for {CURRENT_REDUCER_VERSION}: {op}")


def _typed_record_defaults(collection: str, record_id: str, revision: int) -> dict[str, Any]:
    defaults: dict[str, dict[str, Any]] = {
        "characters": {"id": record_id, "name": record_id, "status": "unknown", "data": {}},
        "locations": {"id": record_id, "name": record_id, "status": "unknown", "data": {}},
        "relationships": {"id": record_id, "kind": "unknown", "status": "unknown", "data": {}},
        "injuries": {
            "id": record_id,
            "description": record_id,
            "severity": "minor",
            "status": "active",
            "data": {},
        },
        "inventories": {"id": record_id, "owner_id": "world", "items": {}, "data": {}},
        "resources": {
            "id": record_id,
            "name": record_id,
            "quantity": 0,
            "unit": "unit",
            "status": "available",
            "data": {},
        },
        "glossary": {"id": record_id, "term": record_id, "definition": record_id, "status": "active", "data": {}},
        "corruption": {"id": record_id, "subject_id": "world", "level": 0, "status": "stable", "data": {}},
        "foreshadowing": {
            "id": record_id,
            "description": record_id,
            "status": "seeded",
            "introduced_revision": revision,
            "resolved_revision": None,
            "data": {},
        },
    }
    return copy.deepcopy(defaults[collection])


def _read_event_field(memory: dict[str, Any], field: str) -> Any:
    if field == "$":
        return copy.deepcopy(memory)
    parts = field.split(".")
    if len(parts) == 2 and parts[0] in {"timeline", "constraints", "open_threads"}:
        records = memory.get(parts[0], [])
        index = _find_record_index(records if isinstance(records, list) else [], parts[1])
        return copy.deepcopy(records[index]) if index is not None else None
    return _get_dotted_value(memory, field)


def _write_event_field(memory: dict[str, Any], field: str, value: Any) -> None:
    if field == "$":
        raise MemoryReducerError("root replacement is allowed only for genesis")
    parts = field.split(".")
    if len(parts) == 2 and parts[0] in {"timeline", "constraints", "open_threads"}:
        records = _ensure_list(memory, parts[0])
        index = _find_record_index(records, parts[1])
        if value is None:
            if index is not None:
                records.pop(index)
        elif index is None:
            records.append(value)
        else:
            records[index] = value
        return
    current: dict[str, Any] = memory
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            raise MemoryReducerError(f"event field path segment {part} is not an object")
        current = child
    if value is None and len(parts) == 2 and parts[0] in TYPED_COLLECTIONS:
        current.pop(parts[-1], None)
    else:
        current[parts[-1]] = value


REDUCER_REGISTRY = MappingProxyType(
    {
        LEGACY_REDUCER_VERSION: _apply_memory_patch_v21,
        CURRENT_REDUCER_VERSION: _apply_memory_patch_v22,
    }
)


__all__ = [
    "CURRENT_REDUCER_VERSION",
    "LEGACY_REDUCER_VERSION",
    "MemoryReducerError",
    "REDUCER_REGISTRY",
    "SUPPORTED_REDUCER_OPS",
    "TYPED_REDUCER_OPS",
    "apply_genesis_event",
    "apply_memory_events",
    "apply_memory_patch",
    "resolve_memory_reducer",
]
