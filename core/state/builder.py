from __future__ import annotations

import copy
from typing import Any

from core.schema import validate_schema
from core.state.memory import normalize_memory_context
from core.state.snapshot import normalize_snapshot


def build_snapshot_state(
    base_snapshot: dict[str, Any],
    memory_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return build_snapshot_state_with_audit(base_snapshot, memory_context)["snapshot"]


def build_snapshot_state_with_audit(
    base_snapshot: dict[str, Any],
    memory_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = normalize_snapshot(base_snapshot)
    memory = normalize_memory_context(memory_context)
    state = copy.deepcopy(snapshot)
    audit: dict[str, Any] = {
        "source": memory.get("source", "unknown"),
        "status": memory.get("status", "unknown"),
        "item_count": len(memory.get("items", [])),
        "applied_count": 0,
        "skipped_count": 0,
        "deduplicated_count": 0,
        "applied_type_counts": [],
        "skipped_type_counts": [],
        "skipped_reason_counts": [],
        "skipped_severity_counts": [],
        "skipped_blocking_count": 0,
        "applied_items": [],
        "skipped_items": [],
    }

    for index, item in enumerate(memory.get("items", [])):
        item_type = item["type"]
        data = copy.deepcopy(item.get("data", {}))
        source_mapping = _source_mapping_for(memory, index)

        if item_type == "world_state":
            _merge_world_state(state, data)
            _record_applied(audit, index, item, "merge_world_state", "world_state", source_mapping)
        elif item_type == "location":
            name = _apply_named_item(state["world_state"].setdefault("locations", {}), item, data)
            if name:
                _record_applied(audit, index, item, "upsert_location", f"world_state.locations.{name}", source_mapping)
            else:
                _record_skipped(audit, index, item, "missing_name", source_mapping)
        elif item_type == "character":
            name = _apply_named_item(state.setdefault("characters", {}), item, data)
            if name:
                _record_applied(audit, index, item, "upsert_character", f"characters.{name}", source_mapping)
            else:
                _record_skipped(audit, index, item, "missing_name", source_mapping)
        elif item_type == "constraint":
            key = _item_key(item, data)
            if _append_unique(state.setdefault("constraints", []), data, key):
                _record_applied(audit, index, item, "append_constraint", "constraints", source_mapping)
            else:
                _record_skipped(audit, index, item, "duplicate_memory", source_mapping, deduplicated=True)
        elif item_type == "timeline_event":
            key = _item_key(item, data)
            if _append_unique(state.setdefault("timeline", []), _timeline_entry(item, data), key):
                _record_applied(audit, index, item, "append_timeline_event", "timeline", source_mapping)
            else:
                _record_skipped(audit, index, item, "duplicate_memory", source_mapping, deduplicated=True)

    state.setdefault("memory", {})
    state["memory"]["source"] = memory.get("source", "unknown")
    state["memory"]["status"] = memory.get("status", "unknown")
    state["memory"]["item_count"] = len(memory.get("items", []))
    _finalize_skipped_counts(audit)
    validate_schema(audit, "snapshot_builder_audit.schema.json")
    return {
        "snapshot": normalize_snapshot(state),
        "audit": audit,
    }


def _merge_world_state(snapshot: dict[str, Any], data: dict[str, Any]) -> None:
    world_state = snapshot.setdefault("world_state", {})
    for key, value in data.items():
        if key == "locations" and isinstance(value, dict):
            world_state.setdefault("locations", {}).update(value)
        else:
            world_state[key] = value


def _apply_named_item(target: dict[str, Any], item: dict[str, Any], data: dict[str, Any]) -> str | None:
    name = item.get("name") or data.get("name")
    if not name:
        return None
    clean_data = {key: value for key, value in data.items() if key != "name"}
    target[str(name)] = clean_data
    return str(name)


def _timeline_entry(item: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    entry = copy.deepcopy(data)
    if item.get("id") and "memory_id" not in entry:
        entry["memory_id"] = item["id"]
    if item.get("name") and "name" not in entry:
        entry["name"] = item["name"]
    if item.get("source_run_id") and "source_run_id" not in entry:
        entry["source_run_id"] = item["source_run_id"]
    return entry


def _append_unique(target: list[Any], value: dict[str, Any], key: str) -> bool:
    existing_keys: set[str] = set()
    for item in target:
        if isinstance(item, dict):
            existing_keys.update(_entry_keys(item))
    if key in existing_keys:
        return False
    target.append(value)
    return True


def _item_key(item: dict[str, Any], data: dict[str, Any]) -> str:
    item_id = item.get("id") or data.get("memory_id")
    if item_id:
        return f"id:{item_id}"
    item_type = item.get("type") or data.get("type") or "item"
    name = item.get("name") or data.get("name")
    if name:
        return f"{item_type}:name:{name}"
    chapter_index = data.get("chapter_index")
    text = data.get("summary") or data.get("text") or data.get("rule")
    return f"{item_type}:chapter:{chapter_index}:text:{text}"


def _entry_key(entry: dict[str, Any]) -> str:
    return _entry_keys(entry)[0]


def _entry_keys(entry: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    memory_id = entry.get("memory_id") or entry.get("id")
    if memory_id:
        keys.append(f"id:{memory_id}")
    memory_ids = entry.get("memory_ids")
    if isinstance(memory_ids, list):
        keys.extend(f"id:{memory_id}" for memory_id in memory_ids if memory_id)
    entry_type = entry.get("type") or _infer_entry_type(entry)
    name = entry.get("name")
    if name:
        keys.append(f"{entry_type}:name:{name}")
    chapter_index = entry.get("chapter_index")
    text = entry.get("summary") or entry.get("text") or entry.get("rule")
    keys.append(f"{entry_type}:chapter:{chapter_index}:text:{text}")
    return keys


def _infer_entry_type(entry: dict[str, Any]) -> str:
    if "rule" in entry:
        return "constraint"
    if "summary" in entry or "text" in entry:
        return "timeline_event"
    return "item"


def _record_applied(
    audit: dict[str, Any],
    index: int,
    item: dict[str, Any],
    operation: str,
    target: str,
    source_mapping: dict[str, Any] | None,
) -> None:
    audit["applied_count"] += 1
    audit["applied_items"].append(
        _audit_item(index, item, operation=operation, target=target, source_mapping=source_mapping)
    )


def _record_skipped(
    audit: dict[str, Any],
    index: int,
    item: dict[str, Any],
    reason_code: str,
    source_mapping: dict[str, Any] | None,
    *,
    deduplicated: bool = False,
) -> None:
    audit["skipped_count"] += 1
    if deduplicated:
        audit["deduplicated_count"] += 1
    issue = _skip_issue(reason_code)
    audit["skipped_items"].append(
        _audit_item(
            index,
            item,
            reason=issue["reason"],
            reason_code=reason_code,
            severity=issue["severity"],
            category=issue["category"],
            blocking=issue["blocking"],
            source_mapping=source_mapping,
        )
    )


def _audit_item(
    index: int,
    item: dict[str, Any],
    *,
    operation: str | None = None,
    target: str | None = None,
    reason: str | None = None,
    reason_code: str | None = None,
    severity: str | None = None,
    category: str | None = None,
    blocking: bool | None = None,
    source_mapping: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "index": index,
        "type": item.get("type"),
        "name": item.get("name"),
        "id": item.get("id"),
    }
    if operation:
        result["operation"] = operation
    if target:
        result["target"] = target
    if reason:
        result["reason"] = reason
    if reason_code:
        result["reason_code"] = reason_code
    if severity:
        result["severity"] = severity
    if category:
        result["category"] = category
    if blocking is not None:
        result["blocking"] = blocking
    if source_mapping is not None:
        result["source_mapping"] = source_mapping
    return result


def _source_mapping_for(memory: dict[str, Any], index: int) -> dict[str, Any] | None:
    mappings = memory.get("source_mappings")
    if not isinstance(mappings, list):
        return None
    for mapping in mappings:
        if isinstance(mapping, dict) and mapping.get("index") == index:
            return _compact_source_mapping(mapping)
    return None


def _compact_source_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "index",
        "source",
        "memory_id",
        "type",
        "name",
        "path",
        "line_number",
        "page_id",
        "page_url",
        "page_index",
    )
    return {key: mapping[key] for key in allowed if key in mapping}


def _skip_issue(reason_code: str) -> dict[str, Any]:
    issues = {
        "missing_name": {
            "reason": "missing name",
            "severity": "medium",
            "category": "memory_quality",
            "blocking": False,
        },
        "duplicate_memory": {
            "reason": "duplicate",
            "severity": "low",
            "category": "deduplication",
            "blocking": False,
        },
    }
    return issues.get(
        reason_code,
        {
            "reason": reason_code.replace("_", " "),
            "severity": "medium",
            "category": "memory_quality",
            "blocking": False,
        },
    )


def _finalize_skipped_counts(audit: dict[str, Any]) -> None:
    applied_type_counts: dict[str, int] = {}
    skipped_type_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    severity_counts: dict[str, int] = {}
    skipped_blocking_count = 0
    for item in audit.get("applied_items", []):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type is not None:
            applied_type_counts[str(item_type)] = applied_type_counts.get(str(item_type), 0) + 1
    for item in audit.get("skipped_items", []):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        reason_code = item.get("reason_code")
        severity = item.get("severity")
        if item_type is not None:
            skipped_type_counts[str(item_type)] = skipped_type_counts.get(str(item_type), 0) + 1
        if reason_code:
            reason_counts[str(reason_code)] = reason_counts.get(str(reason_code), 0) + 1
        if severity:
            severity_counts[str(severity)] = severity_counts.get(str(severity), 0) + 1
        if item.get("blocking") is True:
            skipped_blocking_count += 1
    audit["applied_type_counts"] = _count_entries(applied_type_counts, "type")
    audit["skipped_type_counts"] = _count_entries(skipped_type_counts, "type")
    audit["skipped_reason_counts"] = _count_entries(reason_counts, "reason_code")
    audit["skipped_severity_counts"] = _count_entries(severity_counts, "severity")
    audit["skipped_blocking_count"] = skipped_blocking_count


def _count_entries(counts: dict[str, int], key: str) -> list[dict[str, Any]]:
    return [{key: name, "count": counts[name]} for name in sorted(counts)]
