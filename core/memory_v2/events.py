from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.memory_v2.canonical import canonical_json_hash
from core.schema import SchemaValidationError, validate_schema


MEMORY_EVENT_SCHEMA_VERSION = "2.1"
LEGACY_MEMORY_EVENT_SCHEMA_VERSION = "2.0"
MEMORY_EVENT_CREATED_BY = "NovelAgent Memory System V2"


class MemoryEventValidationError(ValueError):
    pass


def create_memory_event(
    *,
    event_id: str,
    revision: int,
    op: str,
    source: dict[str, Any],
    subject_id: str | None = None,
    field: str | None = None,
    old_value: Any = None,
    new_value: Any = None,
    metadata: dict[str, Any] | None = None,
    schema_version: str = MEMORY_EVENT_SCHEMA_VERSION,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schema_version": schema_version,
        "event_id": event_id,
        "revision": revision,
        "op": op,
        "old_value": old_value,
        "new_value": new_value,
        "source": dict(source),
        "metadata": {
            "created_by": MEMORY_EVENT_CREATED_BY,
        },
    }
    if subject_id is not None:
        event["subject_id"] = subject_id
    if field is not None:
        event["field"] = field
    if metadata:
        event["metadata"].update(metadata)
    if schema_version == MEMORY_EVENT_SCHEMA_VERSION:
        event["event_hash"] = memory_event_hash(event)
    return validate_memory_event(event)


def validate_memory_event(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise MemoryEventValidationError("memory event must be a JSON object")
    try:
        validated = validate_schema(event, "memory_event.schema.json")
    except SchemaValidationError as exc:
        raise MemoryEventValidationError(str(exc)) from exc
    schema_version = validated.get("schema_version")
    if schema_version == MEMORY_EVENT_SCHEMA_VERSION:
        event_hash = validated.get("event_hash")
        if not _is_sha256(event_hash):
            raise MemoryEventValidationError("Memory 2.1 event_hash must be a lowercase SHA-256 digest")
        expected_hash = memory_event_hash(validated)
        if event_hash != expected_hash:
            raise MemoryEventValidationError("Memory 2.1 event_hash mismatch")
    elif schema_version == LEGACY_MEMORY_EVENT_SCHEMA_VERSION and "event_hash" in validated:
        raise MemoryEventValidationError("Memory 2.0 events must not contain event_hash")
    return validated


def memory_event_hash(event: dict[str, Any]) -> str:
    return canonical_json_hash(event, exclude_fields=("event_hash",))


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)


def append_memory_event(path: str | Path, event: dict[str, Any]) -> dict[str, Any]:
    validated = validate_memory_event(event)
    append_memory_events(path, [validated])
    return validated


def append_memory_events(path: str | Path, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validated_events = [validate_memory_event(event) for event in events]
    event_path = Path(path)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    with event_path.open("a", encoding="utf-8") as f:
        for event in validated_events:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            f.write("\n")
    return validated_events


def load_memory_events(path: str | Path) -> list[dict[str, Any]]:
    event_path = Path(path)
    if not event_path.exists():
        return []

    events: list[dict[str, Any]] = []
    with event_path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise MemoryEventValidationError(f"{event_path} line {line_number} is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise MemoryEventValidationError(f"{event_path} line {line_number} must be a JSON object")
            events.append(validate_memory_event(payload))
    return events


__all__ = [
    "MEMORY_EVENT_CREATED_BY",
    "MEMORY_EVENT_SCHEMA_VERSION",
    "LEGACY_MEMORY_EVENT_SCHEMA_VERSION",
    "MemoryEventValidationError",
    "append_memory_event",
    "append_memory_events",
    "create_memory_event",
    "load_memory_events",
    "memory_event_hash",
    "validate_memory_event",
]
