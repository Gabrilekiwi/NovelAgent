from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.config import get_config
from core.schema import SchemaValidationError, validate_schema


class MemoryError(ValueError):
    pass


ALLOWED_MEMORY_TYPES = {
    "world_state",
    "location",
    "character",
    "constraint",
    "timeline_event",
}


def normalize_memory_context(raw_context: dict[str, Any] | list[dict[str, Any]] | None) -> dict[str, Any]:
    if raw_context is None:
        return _pending_context("memory context not provided", source="none")

    if isinstance(raw_context, list):
        context: dict[str, Any] = {
            "source": "file",
            "status": "ready",
            "items": raw_context,
        }
    elif isinstance(raw_context, dict):
        context = {
            "source": raw_context.get("source", "file"),
            "status": raw_context.get("status", "ready"),
            "items": raw_context.get("items", []),
        }
        if "note" in raw_context:
            context["note"] = raw_context["note"]
        if "source_mappings" in raw_context:
            context["source_mappings"] = raw_context["source_mappings"]
    else:
        raise MemoryError("memory context must be an object or list")

    if not isinstance(context["items"], list):
        raise MemoryError("memory items must be a list")

    normalized_items = []
    for index, item in enumerate(context["items"]):
        if not isinstance(item, dict):
            raise MemoryError(f"memory item {index} must be an object")
        item_type = item.get("type")
        if not isinstance(item_type, str) or not item_type.strip():
            raise MemoryError(f"memory item {index} requires a type")
        normalized_type = item_type.strip().lower()
        if normalized_type not in ALLOWED_MEMORY_TYPES:
            allowed = ", ".join(sorted(ALLOWED_MEMORY_TYPES))
            raise MemoryError(f"memory item {index} has unsupported type '{item_type}'. Allowed types: {allowed}")
        data = item.get("data", {})
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise MemoryError(f"memory item {index} data must be an object")
        normalized_item = {
            "type": normalized_type,
            "name": item.get("name"),
            "data": data,
        }
        if "id" in item:
            normalized_item["id"] = item.get("id")
        if "source_run_id" in item:
            normalized_item["source_run_id"] = item.get("source_run_id")
        normalized_items.append(normalized_item)

    context["items"] = normalized_items
    context["source_mappings"] = _normalize_source_mappings(
        normalized_items,
        context.get("source_mappings"),
        source=str(context.get("source") or "unknown"),
    )
    return _validate_memory_context(context)


def load_memory_context(path: str | Path | None = None, *, source: str = "auto") -> dict[str, Any]:
    source = _normalize_memory_source(source)
    if source == "notion" or (source == "auto" and path is None and _has_notion_api_config()):
        return load_notion_memory_context()

    memory_path = Path(path) if path else get_config().memory_path
    if not memory_path.exists():
        return _pending_context(f"{memory_path} not found", source="file")

    if memory_path.suffix.lower() == ".jsonl":
        return _load_jsonl_memory_context(memory_path)

    with memory_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if _looks_like_notion_export(payload):
        from core.state.notion_export import normalize_notion_export

        memory = normalize_notion_export(payload)
        memory["path"] = str(memory_path)
        for mapping in memory.get("source_mappings", []):
            if isinstance(mapping, dict):
                mapping["path"] = str(memory_path)
        return _validate_memory_context(memory)

    memory = normalize_memory_context(payload)
    memory["path"] = str(memory_path)
    memory["source_mappings"] = _default_source_mappings(
        memory["items"],
        source=str(memory.get("source") or "file"),
        path=str(memory_path),
    )
    return _validate_memory_context(memory)


def _load_jsonl_memory_context(path: Path) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    line_numbers: list[int] = []
    with path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise MemoryError(f"{path} line {line_number} is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise MemoryError(f"{path} line {line_number} must be a memory item object")
            items.append(payload)
            line_numbers.append(line_number)

    memory = normalize_memory_context(
        {
            "source": "jsonl-outbox",
            "status": "ready",
            "items": items,
        }
    )
    memory["path"] = str(path)
    memory["source_mappings"] = _default_source_mappings(
        memory["items"],
        source="jsonl-outbox",
        path=str(path),
        line_numbers=line_numbers,
    )
    return _validate_memory_context(memory)


def load_notion_memory_context(
    *,
    database_id: str | None = None,
    api_key: str | None = None,
    transport=None,
) -> dict[str, Any]:
    from api.notion_client import query_database_pages
    from core.state.notion_export import normalize_notion_export

    pages = query_database_pages(database_id=database_id, api_key=api_key, transport=transport)
    return normalize_notion_export({"pages": pages}, source="notion-api")


def _looks_like_notion_export(payload: Any) -> bool:
    if isinstance(payload, dict) and isinstance(payload.get("pages"), list):
        return True
    if isinstance(payload, list) and payload and all(isinstance(item, dict) and "properties" in item for item in payload):
        return True
    return False


def _has_notion_api_config() -> bool:
    return get_config().has_notion_api


def _normalize_memory_source(source: str) -> str:
    normalized = (source or "auto").strip().lower()
    if normalized not in {"auto", "file", "notion"}:
        raise MemoryError("memory source must be one of: auto, file, notion")
    return normalized


def _pending_context(note: str, *, source: str) -> dict[str, Any]:
    return _validate_memory_context({
        "source": source,
        "items": [],
        "status": "adapter_pending",
        "note": note,
    })


def _default_source_mappings(
    items: list[dict[str, Any]],
    *,
    source: str,
    path: str | None = None,
    line_numbers: list[int] | None = None,
) -> list[dict[str, Any]]:
    return [
        _default_source_mapping_for(
            item,
            index,
            source=source,
            path=path,
            line_number=line_numbers[index] if line_numbers and index < len(line_numbers) else None,
        )
        for index, item in enumerate(items)
    ]


def _normalize_source_mappings(
    items: list[dict[str, Any]],
    raw_mappings: Any,
    *,
    source: str,
) -> list[dict[str, Any]]:
    if raw_mappings is None:
        return _default_source_mappings(items, source=source)
    if not isinstance(raw_mappings, list):
        raise MemoryError("memory source_mappings must be a list")

    mappings_by_index: dict[int, dict[str, Any]] = {}
    for mapping_position, raw_mapping in enumerate(raw_mappings):
        if not isinstance(raw_mapping, dict):
            raise MemoryError(f"memory source_mapping {mapping_position} must be an object")
        index = raw_mapping.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise MemoryError(f"memory source_mapping {mapping_position} requires an integer index")
        if index < 0 or index >= len(items):
            raise MemoryError(f"memory source_mapping {mapping_position} index {index} is out of range")
        if index in mappings_by_index:
            raise MemoryError(f"memory source_mapping index {index} is duplicated")

        normalized_mapping = _default_source_mapping_for(items[index], index, source=source)
        normalized_mapping.update(raw_mapping)
        mappings_by_index[index] = normalized_mapping

    for index, item in enumerate(items):
        mappings_by_index.setdefault(index, _default_source_mapping_for(item, index, source=source))

    return [mappings_by_index[index] for index in sorted(mappings_by_index)]


def _default_source_mapping_for(
    item: dict[str, Any],
    index: int,
    *,
    source: str,
    path: str | None = None,
    line_number: int | None = None,
) -> dict[str, Any]:
    mapping = {
        "index": index,
        "source": source,
        "memory_id": item.get("id"),
        "type": item.get("type"),
        "name": item.get("name"),
    }
    if path is not None:
        mapping["path"] = path
    if line_number is not None:
        mapping["line_number"] = line_number
    return mapping


def _validate_memory_context(context: dict[str, Any]) -> dict[str, Any]:
    try:
        validate_schema(context, "memory_context.schema.json")
    except SchemaValidationError as exc:
        raise MemoryError(str(exc)) from exc
    return context
