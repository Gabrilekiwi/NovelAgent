from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from core.memory_v2.canonical import canonical_json_hash
from core.memory_v2.versions import (
    CURRENT_REDUCER_VERSION,
    LEGACY_REDUCER_VERSION,
    UnsupportedMemoryVersionError,
    require_supported_reducer_version,
)
from core.schema import SchemaValidationError, validate_schema


MEMORY_EVENT_SCHEMA_VERSION = "2.1"
LEGACY_MEMORY_EVENT_SCHEMA_VERSION = "2.0"
TYPED_MEMORY_EVENT_SCHEMA_VERSION = "2.2"
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
    before: Any = None,
    after: Any = None,
    precondition: dict[str, Any] | None = None,
    chapter_body: str | None = None,
    chapter_body_sha256: str | None = None,
    evidence_spans: list[dict[str, Any]] | None = None,
    authority_epoch: int | None = None,
    reducer_version: str | None = None,
) -> dict[str, Any]:
    if schema_version == TYPED_MEMORY_EVENT_SCHEMA_VERSION:
        return _create_typed_memory_event(
            event_id=event_id,
            revision=revision,
            op=op,
            source=source,
            subject_id=subject_id,
            field=field,
            before=before,
            after=after,
            precondition=precondition,
            chapter_body=chapter_body,
            chapter_body_sha256=chapter_body_sha256,
            evidence_spans=evidence_spans,
            authority_epoch=authority_epoch,
            reducer_version=reducer_version,
            metadata=metadata,
        )
    if schema_version not in {MEMORY_EVENT_SCHEMA_VERSION, LEGACY_MEMORY_EVENT_SCHEMA_VERSION}:
        raise MemoryEventValidationError(f"unsupported memory event schema_version: {schema_version}")
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


def validate_memory_event(event: Any, *, chapter_body: str | None = None) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise MemoryEventValidationError("memory event must be a JSON object")
    schema_version = event.get("schema_version")
    if schema_version in {MEMORY_EVENT_SCHEMA_VERSION, LEGACY_MEMORY_EVENT_SCHEMA_VERSION}:
        schema_name = "memory_event.schema.json"
    elif schema_version == TYPED_MEMORY_EVENT_SCHEMA_VERSION:
        schema_name = "memory_event_v2_2.schema.json"
    else:
        raise MemoryEventValidationError(f"unsupported memory event schema_version: {schema_version}")
    try:
        validated = validate_schema(event, schema_name)
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
    elif schema_version == TYPED_MEMORY_EVENT_SCHEMA_VERSION:
        _validate_typed_memory_event(validated, chapter_body=chapter_body)
    return validated


def upcast_memory_event(
    event: Any, *, chapter_body: str | None = None
) -> dict[str, Any]:
    """Build the deterministic in-memory read view of an immutable event.

    Memory 2.0 predates the tamper-evident ``event_hash`` envelope. Readers
    upgrade that envelope to 2.1 without rewriting source bytes. The legacy
    view intentionally stops at 2.1: a reader must not invent the prose
    evidence, preconditions, or authority binding required by typed 2.2.
    """

    validated = validate_memory_event(event, chapter_body=chapter_body)
    if validated.get("schema_version") != LEGACY_MEMORY_EVENT_SCHEMA_VERSION:
        return validated
    upgraded = copy.deepcopy(validated)
    upgraded["schema_version"] = MEMORY_EVENT_SCHEMA_VERSION
    upgraded["event_hash"] = memory_event_hash(upgraded)
    return validate_memory_event(upgraded, chapter_body=chapter_body)


def create_memory_event_context(
    *,
    chapter_body: str,
    evidence_spans: list[dict[str, Any]],
    authority_epoch: int,
) -> dict[str, Any]:
    if not isinstance(chapter_body, str):
        raise MemoryEventValidationError("chapter_body must be a string")
    spans = _validated_evidence_spans(evidence_spans, chapter_body=chapter_body)
    if isinstance(authority_epoch, bool) or not isinstance(authority_epoch, int) or authority_epoch < 1:
        raise MemoryEventValidationError("authority_epoch must be a positive integer")
    return {
        "chapter_body": chapter_body,
        "chapter_body_sha256": hashlib.sha256(chapter_body.encode("utf-8")).hexdigest(),
        "evidence_spans": spans,
        "authority_epoch": authority_epoch,
    }


def verify_memory_event_evidence(event: dict[str, Any], chapter_body: str) -> bool:
    upcast_memory_event(event, chapter_body=chapter_body)
    return True


def _create_typed_memory_event(
    *,
    event_id: str,
    revision: int,
    op: str,
    source: dict[str, Any],
    subject_id: str | None,
    field: str | None,
    before: Any,
    after: Any,
    precondition: dict[str, Any] | None,
    chapter_body: str | None,
    chapter_body_sha256: str | None,
    evidence_spans: list[dict[str, Any]] | None,
    authority_epoch: int | None,
    reducer_version: str | None,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(field, str) or not field.strip():
        raise MemoryEventValidationError("Memory 2.2 events require field")
    if not isinstance(precondition, dict):
        raise MemoryEventValidationError("Memory 2.2 events require precondition")
    if not isinstance(chapter_body, str):
        raise MemoryEventValidationError("Memory 2.2 event creation requires chapter_body")
    computed_body_hash = hashlib.sha256(chapter_body.encode("utf-8")).hexdigest()
    if chapter_body_sha256 is not None and computed_body_hash != chapter_body_sha256:
        raise MemoryEventValidationError("chapter_body_sha256 does not match chapter_body")
    spans = _validated_evidence_spans(evidence_spans, chapter_body=chapter_body)
    try:
        reducer = require_supported_reducer_version(reducer_version or CURRENT_REDUCER_VERSION)
    except UnsupportedMemoryVersionError as exc:
        raise MemoryEventValidationError(str(exc)) from exc
    if reducer != CURRENT_REDUCER_VERSION:
        raise MemoryEventValidationError("Memory 2.2 events require memory-reducer-2.2")
    if isinstance(authority_epoch, bool) or not isinstance(authority_epoch, int) or authority_epoch < 1:
        raise MemoryEventValidationError("Memory 2.2 events require a positive authority_epoch")

    event: dict[str, Any] = {
        "schema_version": TYPED_MEMORY_EVENT_SCHEMA_VERSION,
        "event_id": event_id,
        "revision": revision,
        "op": op,
        "field": field,
        "before": before,
        "after": after,
        "precondition": dict(precondition),
        "chapter_body_sha256": computed_body_hash,
        "evidence_spans": spans,
        "authority_epoch": authority_epoch,
        "reducer_version": reducer,
        "source": dict(source),
        "metadata": {"created_by": MEMORY_EVENT_CREATED_BY},
    }
    if subject_id is not None:
        event["subject_id"] = subject_id
    if metadata:
        event["metadata"].update(metadata)
    event["event_hash"] = memory_event_hash(event)
    return validate_memory_event(event, chapter_body=chapter_body)


def _validate_typed_memory_event(event: dict[str, Any], *, chapter_body: str | None) -> None:
    try:
        reducer = require_supported_reducer_version(event.get("reducer_version"))
    except UnsupportedMemoryVersionError as exc:
        raise MemoryEventValidationError(str(exc)) from exc
    if reducer != CURRENT_REDUCER_VERSION:
        raise MemoryEventValidationError("Memory 2.2 events require memory-reducer-2.2")
    source = event.get("source")
    if not isinstance(source, dict):
        raise MemoryEventValidationError("Memory 2.2 source must be an object")
    for field_name in ("kind", "patch_id"):
        value = source.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise MemoryEventValidationError(f"Memory 2.2 source.{field_name} must be a non-empty string")
    for field_name in ("event_hash", "chapter_body_sha256"):
        if not _is_sha256(event.get(field_name)):
            raise MemoryEventValidationError(f"Memory 2.2 {field_name} must be a lowercase SHA-256 digest")
    precondition = event.get("precondition")
    if not isinstance(precondition, dict):
        raise MemoryEventValidationError("Memory 2.2 precondition must be an object")
    if precondition.get("expected_revision") != int(event["revision"]) - 1:
        raise MemoryEventValidationError("Memory 2.2 precondition expected_revision mismatch")
    expected_head = precondition.get("expected_head_event_hash")
    if expected_head is not None and not _is_sha256(expected_head):
        raise MemoryEventValidationError("expected_head_event_hash must be null or a lowercase SHA-256 digest")
    expected_field_hash = precondition.get("expected_field_hash")
    if not _is_sha256(expected_field_hash):
        raise MemoryEventValidationError("expected_field_hash must be a lowercase SHA-256 digest")
    if expected_field_hash != canonical_json_hash(event.get("before")):
        raise MemoryEventValidationError("Memory 2.2 precondition expected_field_hash mismatch")
    _validated_evidence_spans(event.get("evidence_spans"), chapter_body=chapter_body)
    if chapter_body is not None:
        body_hash = hashlib.sha256(chapter_body.encode("utf-8")).hexdigest()
        if body_hash != event["chapter_body_sha256"]:
            raise MemoryEventValidationError("Memory 2.2 chapter_body_sha256 mismatch")
    if event["event_hash"] != memory_event_hash(event):
        raise MemoryEventValidationError("Memory 2.2 event_hash mismatch")


def _validated_evidence_spans(value: Any, *, chapter_body: str | None) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise MemoryEventValidationError("Memory 2.2 evidence_spans must contain at least one span")
    spans: list[dict[str, Any]] = []
    for index, raw_span in enumerate(value):
        if not isinstance(raw_span, dict):
            raise MemoryEventValidationError(f"evidence_spans[{index}] must be an object")
        keys = set(raw_span)
        if keys == {"start", "end", "quote"}:
            start_field = "start"
            end_field = "end"
        elif keys == {"start_char", "end_char", "quote"}:
            # Input-only compatibility with existing chapter validators. Memory
            # events always persist the explicit 2.2 start/end envelope.
            start_field = "start_char"
            end_field = "end_char"
        else:
            raise MemoryEventValidationError(
                f"evidence_spans[{index}] must contain only start, end, and quote"
            )
        start = raw_span.get(start_field)
        end = raw_span.get(end_field)
        quote = raw_span.get("quote")
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise MemoryEventValidationError(f"evidence_spans[{index}].start is invalid")
        if isinstance(end, bool) or not isinstance(end, int) or end <= start:
            raise MemoryEventValidationError(f"evidence_spans[{index}].end is invalid")
        if not isinstance(quote, str) or not quote:
            raise MemoryEventValidationError(f"evidence_spans[{index}].quote must be non-empty")
        if len(quote) != end - start:
            raise MemoryEventValidationError(f"evidence_spans[{index}] quote length does not match offsets")
        if chapter_body is not None:
            if end > len(chapter_body) or chapter_body[start:end] != quote:
                raise MemoryEventValidationError(f"evidence_spans[{index}] does not match chapter_body")
        spans.append({"start": start, "end": end, "quote": quote})
    return spans


def memory_event_hash(event: dict[str, Any]) -> str:
    return canonical_json_hash(event, exclude_fields=("event_hash",))


def reducer_version_for_event(event: dict[str, Any]) -> str:
    validated = upcast_memory_event(event)
    if validated.get("schema_version") in {LEGACY_MEMORY_EVENT_SCHEMA_VERSION, MEMORY_EVENT_SCHEMA_VERSION}:
        return LEGACY_REDUCER_VERSION
    return str(validated["reducer_version"])


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
            events.append(upcast_memory_event(payload))
    return events


__all__ = [
    "MEMORY_EVENT_CREATED_BY",
    "MEMORY_EVENT_SCHEMA_VERSION",
    "LEGACY_MEMORY_EVENT_SCHEMA_VERSION",
    "TYPED_MEMORY_EVENT_SCHEMA_VERSION",
    "MemoryEventValidationError",
    "append_memory_event",
    "append_memory_events",
    "create_memory_event",
    "create_memory_event_context",
    "load_memory_events",
    "memory_event_hash",
    "reducer_version_for_event",
    "upcast_memory_event",
    "verify_memory_event_evidence",
    "validate_memory_event",
]
