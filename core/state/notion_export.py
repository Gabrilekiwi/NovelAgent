from __future__ import annotations

import json
from typing import Any

from core.state.memory import MemoryError, normalize_memory_context


def normalize_notion_export(
    raw_export: dict[str, Any] | list[dict[str, Any]],
    *,
    source: str = "notion-export",
) -> dict[str, Any]:
    pages = raw_export if isinstance(raw_export, list) else raw_export.get("pages")
    if not isinstance(pages, list):
        raise MemoryError("notion export requires a pages list")

    items: list[dict[str, Any]] = []
    source_mappings: list[dict[str, Any]] = []
    for index, page in enumerate(pages):
        if not isinstance(page, dict):
            raise MemoryError(f"notion page {index} must be an object")
        item = _page_to_memory_item(page, index)
        if item:
            items.append(item)
            source_mappings.append(_page_source_mapping(page, item, index, source))

    return normalize_memory_context(
        {
            "source": source,
            "status": "ready",
            "items": items,
            "source_mappings": source_mappings,
        }
    )


def _page_to_memory_item(page: dict[str, Any], index: int) -> dict[str, Any] | None:
    properties = page.get("properties") if isinstance(page.get("properties"), dict) else page
    item_type = _read_property(properties, "type") or _read_property(properties, "Type")
    if not item_type:
        raise MemoryError(f"notion page {index} requires a type property")

    name = _read_property(properties, "name") or _read_property(properties, "Name") or page.get("title")
    data = _read_data(properties)
    if "name" in data and not name:
        name = data["name"]
    item_id = (
        _read_property(properties, "id")
        or _read_property(properties, "ID")
        or _read_property(properties, "memory_id")
        or _read_property(properties, "Memory ID")
    )

    item = {
        "type": str(item_type),
        "name": name,
        "data": data,
    }
    if item_id:
        item["id"] = str(item_id)
    return item


def _page_source_mapping(page: dict[str, Any], item: dict[str, Any], index: int, source: str) -> dict[str, Any]:
    mapping: dict[str, Any] = {
        "index": index,
        "source": source,
        "memory_id": item.get("id"),
        "type": item.get("type"),
        "name": item.get("name"),
        "page_index": index,
    }
    if page.get("id"):
        mapping["page_id"] = str(page["id"])
    if page.get("url"):
        mapping["page_url"] = str(page["url"])
    return mapping


def _read_data(properties: dict[str, Any]) -> dict[str, Any]:
    raw_data = properties.get("data") or properties.get("Data")
    if isinstance(raw_data, dict):
        unwrapped = _unwrap_notion_value(raw_data)
        if isinstance(unwrapped, str):
            return _parse_data_json(unwrapped)
        if isinstance(unwrapped, dict):
            return unwrapped
        return {"value": unwrapped}
    if isinstance(raw_data, str):
        return _parse_data_json(raw_data)

    data: dict[str, Any] = {}
    for key, value in properties.items():
        if key.lower() in {"type", "name", "data", "id", "memory id", "memory_id"}:
            continue
        clean_value = _unwrap_notion_value(value)
        if clean_value not in (None, ""):
            data[_snake_case(key)] = clean_value
    return data


def _parse_data_json(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {"value": value}
    return payload if isinstance(payload, dict) else {"value": payload}


def _read_property(properties: dict[str, Any], key: str) -> Any:
    if key not in properties:
        return None
    return _unwrap_notion_value(properties[key])


def _unwrap_notion_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_unwrap_notion_value(item) for item in value]

    if not isinstance(value, dict):
        return value

    if "rich_text" in value and isinstance(value["rich_text"], list):
        return "".join(str(_unwrap_notion_value(part)) for part in value["rich_text"])
    if "title" in value and isinstance(value["title"], list):
        return "".join(str(_unwrap_notion_value(part)) for part in value["title"])
    if "multi_select" in value and isinstance(value["multi_select"], list):
        return [_unwrap_notion_value(item) for item in value["multi_select"]]
    if "select" in value:
        return _unwrap_notion_value(value["select"])
    if "status" in value:
        return _unwrap_notion_value(value["status"])
    if "date" in value:
        return _unwrap_date(value["date"])
    if "people" in value and isinstance(value["people"], list):
        return [_unwrap_person(item) for item in value["people"]]
    if "relation" in value and isinstance(value["relation"], list):
        return [_unwrap_relation(item) for item in value["relation"]]
    if "files" in value and isinstance(value["files"], list):
        return [_unwrap_file(item) for item in value["files"]]
    for scalar_key in ("url", "email", "phone_number", "created_time", "last_edited_time"):
        if scalar_key in value:
            return value[scalar_key]
    for user_key in ("created_by", "last_edited_by"):
        if user_key in value:
            return _unwrap_person(value[user_key])
    if "plain_text" in value:
        return value["plain_text"]
    if "name" in value:
        return value["name"]
    if "number" in value:
        return value["number"]
    if "checkbox" in value:
        return value["checkbox"]

    value_type = value.get("type")
    if value_type and value_type in value:
        return _unwrap_notion_value(value[value_type])

    return value


def _unwrap_date(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return {
        key: value[key]
        for key in ("start", "end", "time_zone")
        if value.get(key) not in (None, "")
    }


def _unwrap_person(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return value.get("name") or value.get("id") or _unwrap_notion_value(value)


def _unwrap_relation(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return value.get("id") or _unwrap_notion_value(value)


def _unwrap_file(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    file_payload = value.get("file") if isinstance(value.get("file"), dict) else None
    external_payload = value.get("external") if isinstance(value.get("external"), dict) else None
    return {
        key: unwrapped
        for key, unwrapped in {
            "name": value.get("name"),
            "url": (file_payload or external_payload or {}).get("url"),
        }.items()
        if unwrapped not in (None, "")
    }


def _snake_case(value: str) -> str:
    chars = []
    previous_lower = False
    for char in value.strip():
        if char in {" ", "-", "."}:
            chars.append("_")
            previous_lower = False
        elif char.isupper() and previous_lower:
            chars.extend(["_", char.lower()])
            previous_lower = False
        else:
            chars.append(char.lower())
            previous_lower = char.islower() or char.isdigit()
    return "".join(chars).strip("_")
