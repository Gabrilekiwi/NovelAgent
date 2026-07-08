from __future__ import annotations

from copy import deepcopy
from typing import Any

from core.schema import SchemaValidationError, validate_schema


MEMORY_PATCH_SCHEMA_VERSION = "2.0"
MEMORY_PATCH_CREATED_BY = "NovelAgent Memory System V2"


class MemoryPatchValidationError(ValueError):
    pass


def create_memory_patch(
    *,
    patch_id: str,
    source_kind: str = "local_memory",
    source_path: str | None = None,
    operations: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = {"kind": source_kind}
    if source_path is not None:
        source["path"] = source_path

    patch = {
        "schema_version": MEMORY_PATCH_SCHEMA_VERSION,
        "patch_id": patch_id,
        "source": source,
        "operations": deepcopy(operations) if operations is not None else [],
        "metadata": {
            "created_by": MEMORY_PATCH_CREATED_BY,
        },
    }
    if metadata:
        patch["metadata"].update(deepcopy(metadata))
    return validate_memory_patch(patch)


def validate_memory_patch(patch: Any) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise MemoryPatchValidationError("memory patch must be a JSON object")
    try:
        return validate_schema(patch, "memory_patch.schema.json")
    except SchemaValidationError as exc:
        raise MemoryPatchValidationError(str(exc)) from exc


__all__ = [
    "MEMORY_PATCH_CREATED_BY",
    "MEMORY_PATCH_SCHEMA_VERSION",
    "MemoryPatchValidationError",
    "create_memory_patch",
    "validate_memory_patch",
]
