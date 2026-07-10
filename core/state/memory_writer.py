from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from api.notion_client import create_database_page, query_database_pages
from core.schema import validate_schema
from core.state.notion_export import normalize_notion_export
from core.runtime_paths import DEFAULT_MEMORY_OUTBOX as DEFAULT_MEMORY_OUTBOX_PATH


MemoryWriter = Callable[[list[dict[str, Any]]], dict[str, Any]]
DEFAULT_MEMORY_OUTBOX = str(DEFAULT_MEMORY_OUTBOX_PATH)


class FileMemoryWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def __call__(self, updates: list[dict[str, Any]]) -> dict[str, Any]:
        validate_memory_updates(updates)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing_items = _load_existing_items(self.path)
        next_line = _line_count(self.path) + 1
        written = 0
        skipped = 0
        mappings: list[dict[str, Any]] = []
        with self.path.open("a", encoding="utf-8") as f:
            for update in updates:
                update_id = update.get("id")
                if update_id and update_id in existing_items:
                    skipped += 1
                    existing = existing_items[str(update_id)]
                    status = "skipped_duplicate" if existing == update else "duplicate_conflict"
                    mappings.append(_writeback_mapping(update, status=status, target="file", path=str(self.path)))
                    continue
                f.write(json.dumps(update, ensure_ascii=False, sort_keys=True))
                f.write("\n")
                written += 1
                mappings.append(
                    _writeback_mapping(
                        update,
                        status="written",
                        target="file",
                        path=str(self.path),
                        line_number=next_line,
                    )
                )
                next_line += 1
                if update_id:
                    existing_items[str(update_id)] = update
            f.flush()
            os.fsync(f.fileno())

        verification = _verify_file_writeback(self.path, mappings)
        return validate_memory_writeback_result({
            "target": "file",
            "path": str(self.path),
            "written": written,
            "skipped": skipped,
            "item_mappings": mappings,
            "verification": verification,
        })


class NotionMemoryWriter:
    def __init__(
        self,
        *,
        database_id: str | None = None,
        api_key: str | None = None,
        transport=None,
        verify_remote_readback: bool = False,
        dedupe_existing: bool = False,
    ) -> None:
        self.database_id = database_id
        self.api_key = api_key
        self.transport = transport
        self.verify_remote_readback = bool(verify_remote_readback)
        self.dedupe_existing = bool(dedupe_existing)

    def __call__(self, updates: list[dict[str, Any]]) -> dict[str, Any]:
        validate_memory_updates(updates)
        existing_remote = _load_remote_memory_index(
            database_id=self.database_id,
            api_key=self.api_key,
            transport=self.transport,
        ) if self.dedupe_existing else {}
        pages = []
        skipped = 0
        mappings: list[dict[str, Any]] = []
        for update in updates:
            update_id = str(update.get("id") or "")
            if update_id and update_id in existing_remote:
                skipped += 1
                remote_mapping = existing_remote[update_id]
                mappings.append(
                    _writeback_mapping(
                        update,
                        status="skipped_duplicate",
                        target="notion",
                        database_id=self.database_id,
                        page_id=remote_mapping.get("page_id"),
                        page_url=remote_mapping.get("page_url"),
                    )
                )
                continue
            properties = memory_item_to_notion_properties(update)
            page = create_database_page(
                database_id=self.database_id,
                api_key=self.api_key,
                transport=self.transport,
                properties=properties,
            )
            pages.append(page)
            mappings.append(
                _writeback_mapping(
                    update,
                    status="written",
                    target="notion",
                    database_id=self.database_id,
                    page_id=str(page.get("id")) if isinstance(page, dict) and page.get("id") else None,
                    page_url=str(page.get("url")) if isinstance(page, dict) and page.get("url") else None,
                    property_names=sorted(properties.keys()),
                )
            )

        verification = _verify_notion_writeback_response(
            mappings,
            verify_remote_readback=self.verify_remote_readback,
            database_id=self.database_id,
            api_key=self.api_key,
            transport=self.transport,
        )
        return validate_memory_writeback_result({
            "target": "notion",
            "written": len(pages),
            "skipped": skipped,
            "page_ids": [page.get("id") for page in pages if isinstance(page, dict) and page.get("id")],
            "item_mappings": mappings,
            "verification": verification,
        })


def memory_item_to_notion_properties(update: dict[str, Any]) -> dict[str, Any]:
    return {
        "Memory ID": {"rich_text": [{"text": {"content": str(update.get("id") or "")}}]},
        "Type": {"select": {"name": str(update.get("type") or "memory")}},
        "Name": {"title": [{"text": {"content": str(update.get("name") or "untitled")}}]},
        "Data": {"rich_text": [{"text": {"content": json.dumps(update.get("data", {}), ensure_ascii=False)}}]},
    }


def write_memory_updates(
    updates: list[dict[str, Any]],
    writer: MemoryWriter | None,
) -> dict[str, Any]:
    if not updates:
        return validate_memory_writeback_result({
            "target": None,
            "written": 0,
            "item_mappings": [],
            "verification": {"status": "not_applicable", "target": None, "reason": "no_updates"},
        })
    validate_memory_updates(updates)
    if writer is None:
        return validate_memory_writeback_result({
            "target": None,
            "written": 0,
            "skipped": True,
            "item_mappings": [
                _writeback_mapping(update, status="skipped_no_writer", target=None)
                for update in updates
            ],
            "verification": {"status": "not_applicable", "target": None, "reason": "no_writer"},
        })
    return writer(updates)


def build_memory_writer(
    *,
    mode: str = "none",
    outbox_path: str | Path | None = None,
    notion_readback: bool = False,
) -> MemoryWriter | None:
    effective_mode = resolve_memory_writeback_mode(mode=mode, outbox_path=outbox_path)
    if effective_mode == "none":
        return None
    if effective_mode == "file":
        return FileMemoryWriter(outbox_path or DEFAULT_MEMORY_OUTBOX)
    if effective_mode == "notion":
        return NotionMemoryWriter(verify_remote_readback=notion_readback, dedupe_existing=True)
    raise ValueError(f"Unknown memory writeback mode: {effective_mode}")


def resolve_memory_writeback_mode(*, mode: str = "none", outbox_path: str | Path | None = None) -> str:
    if outbox_path and mode == "none":
        return "file"
    if mode not in {"none", "file", "notion"}:
        raise ValueError(f"Unknown memory writeback mode: {mode}")
    return mode


def validate_memory_updates(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validate_schema(
        {
            "source": "writeback",
            "status": "ready",
            "items": updates,
        },
        "memory_context.schema.json",
    )
    return updates


def validate_memory_writeback_result(result: dict[str, Any]) -> dict[str, Any]:
    return validate_schema(result, "memory_writeback.schema.json")


def _load_existing_items(path: Path) -> dict[str, dict[str, Any] | None]:
    if not path.exists():
        return {}
    items: dict[str, dict[str, Any] | None] = {}
    with path.open("r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("id"):
                item_id = str(payload["id"])
                if item_id in items and items[item_id] != payload:
                    items[item_id] = None
                else:
                    items[item_id] = payload
    return items


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8-sig") as f:
        return sum(1 for line in f if line.strip())


def _writeback_mapping(
    update: dict[str, Any],
    *,
    status: str,
    target: str | None,
    database_id: str | None = None,
    path: str | None = None,
    line_number: int | None = None,
    page_id: str | None = None,
    page_url: str | None = None,
    property_names: list[str] | None = None,
) -> dict[str, Any]:
    mapping: dict[str, Any] = {
        "memory_id": update.get("id"),
        "type": update.get("type"),
        "name": update.get("name"),
        "target": target,
        "status": status,
    }
    if path is not None:
        mapping["path"] = path
    if line_number is not None:
        mapping["line_number"] = line_number
    if page_id is not None:
        mapping["page_id"] = page_id
    if page_url is not None:
        mapping["page_url"] = page_url
    if database_id is not None:
        mapping["database_id"] = database_id
    if property_names is not None:
        mapping["property_names"] = property_names
    return mapping


def _verify_notion_writeback_response(
    mappings: list[dict[str, Any]],
    *,
    verify_remote_readback: bool = False,
    database_id: str | None = None,
    api_key: str | None = None,
    transport=None,
) -> dict[str, Any]:
    if verify_remote_readback:
        return _verify_notion_remote_readback(
            mappings,
            database_id=database_id,
            api_key=api_key,
            transport=transport,
        )

    written_mappings = [mapping for mapping in mappings if mapping.get("status") == "written"]
    if not written_mappings:
        return {
            "status": "not_applicable",
            "target": "notion",
            "checked": 0,
            "passed": 0,
            "failed": 0,
            "failures": [],
            "reason": "no_written_items",
        }

    failures: list[dict[str, Any]] = []
    for mapping in written_mappings:
        if not mapping.get("page_id"):
            failures.append(
                {
                    "memory_id": mapping.get("memory_id"),
                    "reason": "missing_page_id",
                }
            )

    checked = len(written_mappings)
    failed = len(failures)
    return {
        "status": "response_recorded" if failed == 0 else "response_incomplete",
        "target": "notion",
        "checked": checked,
        "passed": checked - failed,
        "failed": failed,
        "failures": failures,
        "reason": "remote_readback_not_configured",
    }


def _load_remote_memory_index(
    *,
    database_id: str | None,
    api_key: str | None,
    transport,
) -> dict[str, dict[str, Any]]:
    pages = query_database_pages(database_id=database_id, api_key=api_key, transport=transport)
    context = normalize_notion_export(pages, source="notion-api-readback")
    mappings = context.get("source_mappings", [])
    remote_index: dict[str, dict[str, Any]] = {}
    if not isinstance(mappings, list):
        return remote_index
    for mapping in mappings:
        if not isinstance(mapping, dict) or not mapping.get("memory_id"):
            continue
        remote_index[str(mapping["memory_id"])] = mapping
    return remote_index


def _verify_notion_remote_readback(
    mappings: list[dict[str, Any]],
    *,
    database_id: str | None,
    api_key: str | None,
    transport,
) -> dict[str, Any]:
    written_mappings = [mapping for mapping in mappings if mapping.get("status") == "written"]
    checked = len(written_mappings)
    if not written_mappings:
        return {
            "status": "not_applicable",
            "target": "notion",
            "checked": 0,
            "passed": 0,
            "failed": 0,
            "failures": [],
            "reason": "no_written_items",
        }

    try:
        pages = query_database_pages(database_id=database_id, api_key=api_key, transport=transport)
        context = normalize_notion_export(pages, source="notion-api-readback")
    except Exception as exc:  # noqa: BLE001 - writeback records verification failures instead of hiding writes.
        return {
            "status": "readback_failed",
            "target": "notion",
            "checked": checked,
            "passed": 0,
            "failed": checked,
            "failures": [
                {
                    "reason": "remote_readback_error",
                    "message": str(exc),
                }
            ],
            "reason": "remote_readback_error",
        }

    remote_items = {
        str(item.get("id")): item
        for item in context.get("items", [])
        if isinstance(item, dict) and item.get("id")
    }
    failures: list[dict[str, Any]] = []
    for mapping in written_mappings:
        memory_id = mapping.get("memory_id")
        if not memory_id:
            failures.append({"reason": "missing_memory_id"})
            continue
        remote = remote_items.get(str(memory_id))
        if not remote:
            failures.append({"memory_id": memory_id, "reason": "missing_remote_memory"})
            continue
        mismatches = [
            field
            for field, remote_field in (("type", "type"), ("name", "name"))
            if mapping.get(field) != remote.get(remote_field)
        ]
        if mismatches:
            failures.append(
                {
                    "memory_id": memory_id,
                    "reason": "field_mismatch",
                    "fields": mismatches,
                }
            )

    failed = len(failures)
    return {
        "status": "verified" if failed == 0 else "failed",
        "target": "notion",
        "checked": checked,
        "passed": checked - failed,
        "failed": failed,
        "failures": failures,
        "reason": "remote_readback",
    }


def _verify_file_writeback(path: Path, mappings: list[dict[str, Any]]) -> dict[str, Any]:
    conflict_mappings = [mapping for mapping in mappings if mapping.get("status") == "duplicate_conflict"]
    written_mappings = [
        mapping
        for mapping in mappings
        if mapping.get("status") == "written" and isinstance(mapping.get("line_number"), int)
    ]
    if not written_mappings and not conflict_mappings:
        return {
            "status": "not_applicable",
            "target": "file",
            "checked": 0,
            "passed": 0,
            "failed": 0,
            "failures": [],
            "reason": "no_written_items",
        }

    lines = _read_non_empty_lines(path)
    failures: list[dict[str, Any]] = [
        {"memory_id": mapping.get("memory_id"), "reason": "duplicate_payload_conflict"}
        for mapping in conflict_mappings
    ]
    for mapping in written_mappings:
        line_number = int(mapping["line_number"])
        payload = _payload_at_line(lines, line_number)
        if not isinstance(payload, dict):
            failures.append(
                {
                    "line_number": line_number,
                    "memory_id": mapping.get("memory_id"),
                    "reason": "line_missing_or_invalid",
                }
            )
            continue
        mismatches = [
            field
            for field, payload_field in (("memory_id", "id"), ("type", "type"), ("name", "name"))
            if mapping.get(field) != payload.get(payload_field)
        ]
        if mismatches:
            failures.append(
                {
                    "line_number": line_number,
                    "memory_id": mapping.get("memory_id"),
                    "reason": "field_mismatch",
                    "fields": mismatches,
                }
            )

    failed = len(failures)
    checked = len(written_mappings) + len(conflict_mappings)
    return {
        "status": "verified" if failed == 0 else "failed",
        "target": "file",
        "checked": checked,
        "passed": checked - failed,
        "failed": failed,
        "failures": failures,
    }


def _read_non_empty_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig") as f:
        return [line.strip() for line in f if line.strip()]


def _payload_at_line(lines: list[str], line_number: int) -> dict[str, Any] | None:
    index = line_number - 1
    if index < 0 or index >= len(lines):
        return None
    try:
        payload = json.loads(lines[index])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


__all__ = [
    "FileMemoryWriter",
    "MemoryWriter",
    "NotionMemoryWriter",
    "DEFAULT_MEMORY_OUTBOX",
    "build_memory_writer",
    "memory_item_to_notion_properties",
    "resolve_memory_writeback_mode",
    "validate_memory_writeback_result",
    "validate_memory_updates",
    "write_memory_updates",
]
