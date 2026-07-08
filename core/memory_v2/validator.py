from __future__ import annotations

from typing import Any

from core.schema import SchemaValidationError, validate_schema


class MemoryV2ValidationError(ValueError):
    pass


def validate_canonical_memory(memory: Any) -> dict[str, Any]:
    if not isinstance(memory, dict):
        raise MemoryV2ValidationError("canonical memory must be a JSON object")
    try:
        return validate_schema(memory, "canonical_memory.schema.json")
    except SchemaValidationError as exc:
        raise MemoryV2ValidationError(str(exc)) from exc


__all__ = [
    "MemoryV2ValidationError",
    "validate_canonical_memory",
]
