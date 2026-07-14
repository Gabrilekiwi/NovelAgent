from __future__ import annotations

import math
from typing import Any


TYPED_COLLECTIONS = (
    "characters",
    "locations",
    "relationships",
    "injuries",
    "inventories",
    "resources",
    "glossary",
    "corruption",
    "foreshadowing",
)

CHARACTER_STATUSES = frozenset({"active", "missing", "dead", "unknown"})
LOCATION_STATUSES = frozenset({"active", "destroyed", "inaccessible", "unknown"})
RELATIONSHIP_STATUSES = frozenset({"active", "strained", "broken", "unknown"})
INJURY_SEVERITIES = frozenset({"minor", "moderate", "severe", "critical"})
INJURY_STATUSES = frozenset({"active", "healing", "healed", "permanent"})
INVENTORY_STATUSES = frozenset({"held", "lost", "consumed", "destroyed"})
RESOURCE_STATUSES = frozenset({"available", "reserved", "depleted", "destroyed"})
GLOSSARY_STATUSES = frozenset({"active", "deprecated"})
CORRUPTION_STATUSES = frozenset({"stable", "rising", "falling", "cleansed"})
FORESHADOWING_STATUSES = frozenset({"seeded", "developing", "ripe", "resolved", "abandoned"})


class TypedCanonicalMemoryError(ValueError):
    pass


def validate_typed_canonical_memory(memory: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    _validate_hash_or_none(errors, "head_event_hash", memory.get("head_event_hash"))

    characters = _mapping(memory, "characters", errors)
    locations = _mapping(memory, "locations", errors)
    _validate_named_records(characters, "characters", CHARACTER_STATUSES, errors)
    _validate_named_records(locations, "locations", LOCATION_STATUSES, errors)

    relationships = _mapping(memory, "relationships", errors)
    for record_id, record in _records(relationships, "relationships", errors):
        _record_identity(record_id, record, "relationships", errors)
        _required_string(record, "source_character_id", f"relationships.{record_id}", errors)
        _required_string(record, "target_character_id", f"relationships.{record_id}", errors)
        _required_string(record, "kind", f"relationships.{record_id}", errors)
        _status(record, "status", RELATIONSHIP_STATUSES, f"relationships.{record_id}", errors)
        _data(record, f"relationships.{record_id}", errors)
        source_id = record.get("source_character_id")
        target_id = record.get("target_character_id")
        if isinstance(source_id, str) and source_id not in characters:
            errors.append(f"relationships.{record_id}.source_character_id references unknown character")
        if isinstance(target_id, str) and target_id not in characters:
            errors.append(f"relationships.{record_id}.target_character_id references unknown character")
        if source_id == target_id and isinstance(source_id, str):
            errors.append(f"relationships.{record_id} cannot relate a character to itself")

    injuries = _mapping(memory, "injuries", errors)
    for record_id, record in _records(injuries, "injuries", errors):
        _record_identity(record_id, record, "injuries", errors)
        _required_string(record, "character_id", f"injuries.{record_id}", errors)
        _required_string(record, "description", f"injuries.{record_id}", errors)
        _status(record, "severity", INJURY_SEVERITIES, f"injuries.{record_id}", errors)
        _status(record, "status", INJURY_STATUSES, f"injuries.{record_id}", errors)
        _data(record, f"injuries.{record_id}", errors)
        character_id = record.get("character_id")
        if isinstance(character_id, str) and character_id not in characters:
            errors.append(f"injuries.{record_id}.character_id references unknown character")

    inventories = _mapping(memory, "inventories", errors)
    valid_owners = set(characters) | set(locations) | {"world"}
    for record_id, record in _records(inventories, "inventories", errors):
        _record_identity(record_id, record, "inventories", errors)
        _required_string(record, "owner_id", f"inventories.{record_id}", errors)
        _data(record, f"inventories.{record_id}", errors)
        owner_id = record.get("owner_id")
        if isinstance(owner_id, str) and owner_id not in valid_owners:
            errors.append(f"inventories.{record_id}.owner_id references unknown owner")
        items = record.get("items")
        if not isinstance(items, dict):
            errors.append(f"inventories.{record_id}.items must be an object")
        else:
            for item_id, item in items.items():
                path = f"inventories.{record_id}.items.{item_id}"
                if not isinstance(item_id, str) or not item_id:
                    errors.append(f"{path} id must be a non-empty string")
                if not isinstance(item, dict):
                    errors.append(f"{path} must be an object")
                    continue
                _required_string(item, "name", path, errors)
                _nonnegative_number(item, "quantity", path, errors)
                _status(item, "status", INVENTORY_STATUSES, path, errors)

    resources = _mapping(memory, "resources", errors)
    for record_id, record in _records(resources, "resources", errors):
        _record_identity(record_id, record, "resources", errors)
        _required_string(record, "name", f"resources.{record_id}", errors)
        _nonnegative_number(record, "quantity", f"resources.{record_id}", errors)
        _required_string(record, "unit", f"resources.{record_id}", errors)
        _status(record, "status", RESOURCE_STATUSES, f"resources.{record_id}", errors)
        _data(record, f"resources.{record_id}", errors)

    glossary = _mapping(memory, "glossary", errors)
    for record_id, record in _records(glossary, "glossary", errors):
        _record_identity(record_id, record, "glossary", errors)
        _required_string(record, "term", f"glossary.{record_id}", errors)
        _required_string(record, "definition", f"glossary.{record_id}", errors)
        _status(record, "status", GLOSSARY_STATUSES, f"glossary.{record_id}", errors)
        _data(record, f"glossary.{record_id}", errors)

    corruption = _mapping(memory, "corruption", errors)
    valid_subjects = valid_owners
    for record_id, record in _records(corruption, "corruption", errors):
        _record_identity(record_id, record, "corruption", errors)
        _required_string(record, "subject_id", f"corruption.{record_id}", errors)
        _bounded_number(record, "level", 0, 100, f"corruption.{record_id}", errors)
        _status(record, "status", CORRUPTION_STATUSES, f"corruption.{record_id}", errors)
        _data(record, f"corruption.{record_id}", errors)
        subject_id = record.get("subject_id")
        if isinstance(subject_id, str) and subject_id not in valid_subjects:
            errors.append(f"corruption.{record_id}.subject_id references unknown subject")

    foreshadowing = _mapping(memory, "foreshadowing", errors)
    revision = memory.get("revision")
    for record_id, record in _records(foreshadowing, "foreshadowing", errors):
        path = f"foreshadowing.{record_id}"
        _record_identity(record_id, record, "foreshadowing", errors)
        _required_string(record, "description", path, errors)
        _status(record, "status", FORESHADOWING_STATUSES, path, errors)
        _nonnegative_integer(record, "introduced_revision", path, errors)
        _data(record, path, errors)
        introduced = record.get("introduced_revision")
        if isinstance(introduced, int) and not isinstance(introduced, bool) and isinstance(revision, int):
            if introduced > revision:
                errors.append(f"{path}.introduced_revision cannot be in the future")
        resolved = record.get("resolved_revision")
        status = record.get("status")
        if status in {"resolved", "abandoned"}:
            if isinstance(resolved, bool) or not isinstance(resolved, int) or resolved < 1:
                errors.append(f"{path}.resolved_revision is required for terminal lifecycle status")
            elif isinstance(introduced, int) and resolved < introduced:
                errors.append(f"{path}.resolved_revision cannot precede introduced_revision")
            elif isinstance(revision, int) and resolved > revision:
                errors.append(f"{path}.resolved_revision cannot be in the future")
        elif resolved is not None:
            errors.append(f"{path}.resolved_revision must be null before terminal lifecycle status")

    story_time = memory.get("story_time")
    if isinstance(story_time, dict):
        for key in ("elapsed_minutes", "chapter_index", "scene_index"):
            _nonnegative_integer(story_time, key, "story_time", errors)

    _validate_timeline(memory.get("timeline"), errors)
    if errors:
        raise TypedCanonicalMemoryError("typed canonical memory: " + "; ".join(errors))
    return memory


def _mapping(memory: dict[str, Any], field: str, errors: list[str]) -> dict[str, Any]:
    value = memory.get(field)
    if not isinstance(value, dict):
        errors.append(f"{field} must be an object")
        return {}
    return value


def _records(mapping: dict[str, Any], collection: str, errors: list[str]):
    for record_id, record in mapping.items():
        if not isinstance(record_id, str) or not record_id:
            errors.append(f"{collection} keys must be non-empty strings")
        if not isinstance(record, dict):
            errors.append(f"{collection}.{record_id} must be an object")
            continue
        yield str(record_id), record


def _validate_named_records(
    mapping: dict[str, Any], collection: str, statuses: frozenset[str], errors: list[str]
) -> None:
    for record_id, record in _records(mapping, collection, errors):
        path = f"{collection}.{record_id}"
        _record_identity(record_id, record, collection, errors)
        _required_string(record, "name", path, errors)
        _status(record, "status", statuses, path, errors)
        _data(record, path, errors)


def _record_identity(record_id: str, record: dict[str, Any], collection: str, errors: list[str]) -> None:
    if record.get("id") != record_id:
        errors.append(f"{collection}.{record_id}.id must equal its mapping key")


def _required_string(record: dict[str, Any], field: str, path: str, errors: list[str]) -> None:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}.{field} must be a non-empty string")


def _status(
    record: dict[str, Any], field: str, allowed: frozenset[str], path: str, errors: list[str]
) -> None:
    value = record.get(field)
    if value not in allowed:
        errors.append(f"{path}.{field} must be one of {sorted(allowed)}")


def _data(record: dict[str, Any], path: str, errors: list[str]) -> None:
    if not isinstance(record.get("data"), dict):
        errors.append(f"{path}.data must be an object")


def _nonnegative_integer(record: dict[str, Any], field: str, path: str, errors: list[str]) -> None:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        errors.append(f"{path}.{field} must be a non-negative integer")


def _nonnegative_number(record: dict[str, Any], field: str, path: str, errors: list[str]) -> None:
    value = record.get(field)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        errors.append(f"{path}.{field} must be a non-negative number")


def _bounded_number(
    record: dict[str, Any], field: str, minimum: int, maximum: int, path: str, errors: list[str]
) -> None:
    value = record.get(field)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        errors.append(f"{path}.{field} must be between {minimum} and {maximum}")


def _validate_hash_or_none(errors: list[str], field: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        errors.append(f"{field} must be null or a lowercase SHA-256 digest")


def _validate_timeline(value: Any, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append("timeline must be an array")
        return
    seen: set[str] = set()
    for index, record in enumerate(value):
        path = f"timeline[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{path} must be an object")
            continue
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id:
            errors.append(f"{path}.id must be a non-empty string")
        elif record_id in seen:
            errors.append(f"{path}.id is duplicated")
        else:
            seen.add(record_id)
        _required_string(record, "summary", path, errors)
        _data(record, path, errors)
        if "chapter_index" in record:
            _nonnegative_integer(record, "chapter_index", path, errors)


__all__ = [
    "CHARACTER_STATUSES",
    "CORRUPTION_STATUSES",
    "FORESHADOWING_STATUSES",
    "GLOSSARY_STATUSES",
    "INJURY_SEVERITIES",
    "INJURY_STATUSES",
    "INVENTORY_STATUSES",
    "LOCATION_STATUSES",
    "RELATIONSHIP_STATUSES",
    "RESOURCE_STATUSES",
    "TYPED_COLLECTIONS",
    "TypedCanonicalMemoryError",
    "validate_typed_canonical_memory",
]
