from __future__ import annotations

import copy
import json
from typing import Any

from core.memory_v2.canonical import canonical_json_hash
from core.memory_v2.event_store import memory_projection_hash
from core.memory_v2.snapshot_adapter import canonical_memory_to_snapshot
from core.memory_v2.validator import validate_canonical_memory
from core.memory_v2.versions import CURRENT_REDUCER_VERSION
from core.schema import SchemaValidationError, validate_schema


MEMORY_PROJECTION_SCHEMA_VERSION = "2.2"
MEMORY_PROJECTION_RENDERER_VERSION = "memory-projection-renderer-2.2"


class MemoryProjectionError(ValueError):
    pass


def render_snapshot_projection(canonical_memory: dict[str, Any]) -> dict[str, Any]:
    memory = copy.deepcopy(validate_canonical_memory(canonical_memory))
    if memory.get("schema_version") != "2.2":
        raise MemoryProjectionError("Memory 2.2 snapshot renderer requires CanonicalMemory 2.2")
    return canonical_memory_to_snapshot(memory)


def render_tracking_projection(canonical_memory: dict[str, Any]) -> dict[str, str]:
    memory = copy.deepcopy(validate_canonical_memory(canonical_memory))
    if memory.get("schema_version") != "2.2":
        raise MemoryProjectionError("Memory 2.2 tracking renderer requires CanonicalMemory 2.2")
    marker = (
        f"<!-- NovelAgent:memory-projection revision={memory['revision']} "
        f"head={memory.get('head_event_hash') or 'genesis'} -->"
    )
    return {
        "追踪/上下文.md": _render_context(memory, marker),
        "追踪/角色状态.md": _render_characters(memory, marker),
        "追踪/伏笔.md": _render_foreshadowing(memory, marker),
        "追踪/时间线.md": _render_timeline(memory, marker),
    }


def create_memory_projection_receipt(
    canonical_memory: dict[str, Any],
    *,
    projection_kind: str,
    artifact: Any,
) -> dict[str, Any]:
    memory = validate_canonical_memory(canonical_memory)
    if memory.get("schema_version") != "2.2":
        raise MemoryProjectionError("Memory 2.2 projection receipt requires CanonicalMemory 2.2")
    if projection_kind not in {"snapshot", "tracking"}:
        raise MemoryProjectionError(f"unsupported projection kind: {projection_kind}")
    receipt: dict[str, Any] = {
        "schema_version": MEMORY_PROJECTION_SCHEMA_VERSION,
        "renderer_version": MEMORY_PROJECTION_RENDERER_VERSION,
        "reducer_version": CURRENT_REDUCER_VERSION,
        "projection_kind": projection_kind,
        "book_id": str(memory["book_id"]),
        "authority_epoch": int(memory["authority_epoch"]),
        "revision": int(memory["revision"]),
        "head_event_hash": memory.get("head_event_hash"),
        "source_projection_hash": memory_projection_hash(memory),
        "artifact_hash": canonical_json_hash(artifact),
    }
    receipt["receipt_hash"] = canonical_json_hash(receipt, exclude_fields=("receipt_hash",))
    return validate_memory_projection_receipt(receipt, canonical_memory=memory, artifact=artifact)


def validate_memory_projection_receipt(
    receipt: Any,
    *,
    canonical_memory: dict[str, Any] | None = None,
    artifact: Any = None,
) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise MemoryProjectionError("memory projection receipt must be a JSON object")
    try:
        validated = validate_schema(receipt, "memory_projection_receipt.schema.json")
    except SchemaValidationError as exc:
        raise MemoryProjectionError(str(exc)) from exc
    for field in ("head_event_hash", "source_projection_hash", "artifact_hash", "receipt_hash"):
        value = validated.get(field)
        if field == "head_event_hash" and value is None:
            continue
        if not _is_sha256(value):
            raise MemoryProjectionError(f"{field} must be a lowercase SHA-256 digest")
    if validated["renderer_version"] != MEMORY_PROJECTION_RENDERER_VERSION:
        raise MemoryProjectionError("unsupported memory projection renderer_version")
    if validated["reducer_version"] != CURRENT_REDUCER_VERSION:
        raise MemoryProjectionError("projection receipt reducer_version mismatch")
    if validated["receipt_hash"] != canonical_json_hash(validated, exclude_fields=("receipt_hash",)):
        raise MemoryProjectionError("memory projection receipt hash mismatch")
    if canonical_memory is not None:
        memory = validate_canonical_memory(canonical_memory)
        expected_identity = (
            str(memory["book_id"]),
            int(memory["authority_epoch"]),
            int(memory["revision"]),
            memory.get("head_event_hash"),
        )
        actual_identity = (
            str(validated["book_id"]),
            int(validated["authority_epoch"]),
            int(validated["revision"]),
            validated.get("head_event_hash"),
        )
        if actual_identity != expected_identity:
            raise MemoryProjectionError("projection receipt canonical identity mismatch")
        if validated["source_projection_hash"] != memory_projection_hash(memory):
            raise MemoryProjectionError("projection receipt source hash mismatch")
    if artifact is not None and validated["artifact_hash"] != canonical_json_hash(artifact):
        raise MemoryProjectionError("projection receipt artifact hash mismatch")
    return validated


def rebuild_memory_projections(canonical_memory: dict[str, Any]) -> dict[str, Any]:
    memory = copy.deepcopy(validate_canonical_memory(canonical_memory))
    snapshot = render_snapshot_projection(memory)
    tracking = render_tracking_projection(memory)
    return {
        "snapshot": snapshot,
        "snapshot_receipt": create_memory_projection_receipt(
            memory,
            projection_kind="snapshot",
            artifact=snapshot,
        ),
        "tracking": tracking,
        "tracking_receipt": create_memory_projection_receipt(
            memory,
            projection_kind="tracking",
            artifact=tracking,
        ),
    }


def _render_context(memory: dict[str, Any], marker: str) -> str:
    payload = {
        "story_time": memory["story_time"],
        "current_state": memory["current_state"],
        "resources": memory["resources"],
        "corruption": memory["corruption"],
    }
    return _json_document("上下文", marker, payload)


def _render_characters(memory: dict[str, Any], marker: str) -> str:
    payload = {
        "characters": memory["characters"],
        "relationships": memory["relationships"],
        "injuries": memory["injuries"],
        "inventories": memory["inventories"],
    }
    return _json_document("角色状态", marker, payload)


def _render_foreshadowing(memory: dict[str, Any], marker: str) -> str:
    lines = ["# 伏笔", "", marker, "", "| ID | 内容 | 状态 | 引入修订 | 解决修订 |", "|---|---|---|---:|---:|"]
    for record_id in sorted(memory["foreshadowing"]):
        record = memory["foreshadowing"][record_id]
        description = _table_text(record.get("description", ""))
        resolved = record.get("resolved_revision")
        lines.append(
            f"| {_table_text(record_id)} | {description} | {_table_text(record.get('status', ''))} | "
            f"{record.get('introduced_revision', '')} | {'' if resolved is None else resolved} |"
        )
    return "\n".join(lines) + "\n"


def _render_timeline(memory: dict[str, Any], marker: str) -> str:
    lines = ["# 时间线", "", marker, "", "| ID | 章节 | 摘要 |", "|---|---:|---|"]
    timeline = sorted(memory["timeline"], key=lambda item: (int(item.get("chapter_index", 0)), str(item["id"])))
    for record in timeline:
        lines.append(
            f"| {_table_text(record['id'])} | {record.get('chapter_index', '')} | "
            f"{_table_text(record.get('summary', ''))} |"
        )
    return "\n".join(lines) + "\n"


def _json_document(title: str, marker: str, payload: dict[str, Any]) -> str:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    return f"# {title}\n\n{marker}\n\n```json\n{rendered}\n```\n"


def _table_text(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


__all__ = [
    "MEMORY_PROJECTION_RENDERER_VERSION",
    "MEMORY_PROJECTION_SCHEMA_VERSION",
    "MemoryProjectionError",
    "create_memory_projection_receipt",
    "rebuild_memory_projections",
    "render_snapshot_projection",
    "render_tracking_projection",
    "validate_memory_projection_receipt",
]
