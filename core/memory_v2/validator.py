from __future__ import annotations

from typing import Any

from core.schema import SchemaValidationError, validate_schema
from core.memory_v2.models import (
    CANONICAL_MEMORY_SCHEMA_VERSION,
    TYPED_CANONICAL_MEMORY_SCHEMA_VERSION,
)
from core.memory_v2.typed import TypedCanonicalMemoryError, validate_typed_canonical_memory


class MemoryV2ValidationError(ValueError):
    pass


def validate_canonical_memory(memory: Any) -> dict[str, Any]:
    if not isinstance(memory, dict):
        raise MemoryV2ValidationError("canonical memory must be a JSON object")
    schema_version = memory.get("schema_version")
    if schema_version == CANONICAL_MEMORY_SCHEMA_VERSION:
        schema_name = "canonical_memory.schema.json"
    elif schema_version == TYPED_CANONICAL_MEMORY_SCHEMA_VERSION:
        schema_name = "canonical_memory_v2_2.schema.json"
    else:
        raise MemoryV2ValidationError(f"unsupported canonical memory schema_version: {schema_version}")
    try:
        validated = validate_schema(memory, schema_name)
        if schema_version == TYPED_CANONICAL_MEMORY_SCHEMA_VERSION:
            validate_typed_canonical_memory(validated)
        return validated
    except (SchemaValidationError, TypedCanonicalMemoryError) as exc:
        raise MemoryV2ValidationError(str(exc)) from exc


__all__ = [
    "MemoryV2ValidationError",
    "validate_canonical_memory",
]
