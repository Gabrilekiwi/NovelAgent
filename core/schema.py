from __future__ import annotations

import json
from pathlib import Path
from typing import Any


EMBEDDED_SCHEMA_CONTRACTS = (
    {
        "source": "director_audit.schema.json",
        "embedded_in": "run_record.schema.json",
        "path": ("properties", "director"),
    },
    {
        "source": "input_pack_metadata.schema.json",
        "embedded_in": "run_record.schema.json",
        "path": ("properties", "input_pack", "properties", "metadata"),
    },
    {
        "source": "snapshot_builder_audit.schema.json",
        "embedded_in": "run_record.schema.json",
        "path": ("properties", "snapshot_builder", "properties", "audit"),
    },
    {
        "source": "trace_event.schema.json",
        "embedded_in": "run_record.schema.json",
        "path": ("properties", "trace", "items"),
    },
    {
        "source": "workflow_plan.schema.json",
        "embedded_in": "run_record.schema.json",
        "path": ("properties", "workflow_plan"),
        "nullable": True,
    },
    {
        "source": "state_update_audit.schema.json",
        "embedded_in": "run_record.schema.json",
        "path": ("properties", "state_update"),
    },
    {
        "source": "project_identity.schema.json",
        "embedded_in": "run_record.schema.json",
        "path": ("properties", "story_project", "properties", "project_identity"),
        "nullable": True,
    },
    {
        "source": "review_gate_result.schema.json",
        "embedded_in": "run_record.schema.json",
        "path": ("properties", "review_gate"),
    },
    {
        "source": "repair_plan.schema.json",
        "embedded_in": "trace_event.schema.json",
        "path": ("properties", "repair_plan"),
    },
)

MIRRORED_SCHEMA_CONTRACTS = (
    {
        "source": "schemas/director_decision.schema.json",
        "mirror": "core/director/schema.json",
    },
)

SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "$id",
        "$schema",
        "additionalProperties",
        "description",
        "enum",
        "items",
        "maximum",
        "minimum",
        "minItems",
        "minLength",
        "properties",
        "required",
        "title",
        "type",
    }
)


class SchemaValidationError(ValueError):
    pass


def load_schema(name: str) -> dict[str, Any]:
    schema_path = Path("schemas") / name
    with schema_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def validate_schema(data: Any, schema_name: str) -> Any:
    schema = load_schema(schema_name)
    validate_schema_keywords(schema, schema_name)
    errors = _validate(data, schema, "$")
    if errors:
        raise SchemaValidationError(f"{schema_name}: " + "; ".join(errors))
    return data


def validate_schema_keywords(schema: Any, schema_name: str) -> Any:
    if not isinstance(schema, dict):
        raise SchemaValidationError(f"{schema_name}: schema must be a JSON object")
    errors = _schema_keyword_errors(schema, schema_name)
    if errors:
        raise SchemaValidationError(f"{schema_name}: " + "; ".join(errors))
    return schema


def validate_schema_consistency() -> list[dict[str, Any]]:
    checked: list[dict[str, Any]] = []
    errors: list[str] = []

    for contract in MIRRORED_SCHEMA_CONTRACTS:
        source_path = Path(str(contract["source"]))
        mirror_path = Path(str(contract["mirror"]))
        source = _load_json_file(source_path)
        mirror = _load_json_file(mirror_path)
        _collect_schema_keyword_errors(errors, source, str(source_path))
        _collect_schema_keyword_errors(errors, mirror, str(mirror_path))
        if source != mirror:
            errors.append(f"{mirror_path} differs from {source_path}")
            continue
        checked.append(
            {
                "source": str(source_path),
                "mirror": str(mirror_path),
            }
        )

    for contract in EMBEDDED_SCHEMA_CONTRACTS:
        source_name = str(contract["source"])
        embedded_name = str(contract["embedded_in"])
        path = tuple(str(part) for part in contract["path"])
        source_schema = load_schema(source_name)
        embedded_schema = load_schema(embedded_name)
        _collect_schema_keyword_errors(errors, source_schema, source_name)
        _collect_schema_keyword_errors(errors, embedded_schema, embedded_name)
        source = _standalone_contract(source_schema)
        if contract.get("nullable"):
            source = _nullable_contract(source)
        embedded = _schema_path(embedded_schema, path)
        if source != embedded:
            errors.append(f"{embedded_name}:{'.'.join(path)} differs from {source_name}")
            continue
        checked.append(
            {
                "source": source_name,
                "embedded_in": embedded_name,
                "path": ".".join(path),
            }
        )

    if errors:
        raise SchemaValidationError("schema consistency: " + "; ".join(errors))
    return checked


def _collect_schema_keyword_errors(errors: list[str], schema: Any, schema_name: str) -> None:
    try:
        validate_schema_keywords(schema, schema_name)
    except SchemaValidationError as exc:
        errors.append(str(exc))


def _load_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise SchemaValidationError(f"{path} must contain a JSON object")
    return value


def _schema_keyword_errors(schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    for key, value in schema.items():
        if key not in SUPPORTED_SCHEMA_KEYWORDS:
            errors.append(f"{path}.{key} is unsupported")

        if key == "properties":
            if not isinstance(value, dict):
                errors.append(f"{path}.properties must be an object")
                continue
            for property_name, property_schema in value.items():
                property_path = f"{path}.properties.{property_name}"
                if not isinstance(property_schema, dict):
                    errors.append(f"{property_path} must be a schema object")
                    continue
                errors.extend(_schema_keyword_errors(property_schema, property_path))
            continue

        if key == "items":
            if not isinstance(value, dict):
                errors.append(f"{path}.items must be a schema object")
                continue
            errors.extend(_schema_keyword_errors(value, f"{path}.items"))
            continue

        if key == "additionalProperties" and not isinstance(value, bool):
            errors.append(f"{path}.additionalProperties must be a boolean")

    return errors


def _validate(data: Any, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []

    if "enum" in schema and data not in schema["enum"]:
        return [f"{path} must be one of {schema['enum']}"]

    expected_type = schema.get("type")
    if expected_type and not _matches_type(data, expected_type):
        return [f"{path} must be {expected_type}"]

    primary_type = _primary_type_for_value(data, expected_type)

    if primary_type == "integer":
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and data < minimum:
            errors.append(f"{path} must be >= {minimum}")
        if maximum is not None and data > maximum:
            errors.append(f"{path} must be <= {maximum}")

    if primary_type == "string":
        min_length = schema.get("minLength")
        if min_length is not None and len(data) < min_length:
            errors.append(f"{path} length must be >= {min_length}")

    if primary_type == "object":
        required = schema.get("required", [])
        for key in required:
            if key not in data:
                errors.append(f"{path}.{key} is required")

        properties = schema.get("properties", {})
        for key, value in data.items():
            child_schema = properties.get(key)
            if child_schema is None:
                if schema.get("additionalProperties") is False:
                    errors.append(f"{path}.{key} is not allowed")
                continue
            errors.extend(_validate(value, child_schema, f"{path}.{key}"))

    if primary_type == "array":
        min_items = schema.get("minItems")
        if min_items is not None and len(data) < min_items:
            errors.append(f"{path} must contain at least {min_items} item(s)")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(data):
                errors.extend(_validate(item, item_schema, f"{path}[{index}]"))

    return errors


def _matches_type(value: Any, expected_type: str | list[str]) -> bool:
    if isinstance(expected_type, list):
        return any(_matches_type(value, item) for item in expected_type)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True


def _primary_type(expected_type: str | list[str] | None) -> str | None:
    if expected_type is None:
        return None
    if isinstance(expected_type, str):
        return expected_type
    for item in ("object", "array", "integer", "string", "boolean", "number", "null"):
        if item in expected_type:
            return item
    return None


def _primary_type_for_value(value: Any, expected_type: str | list[str] | None) -> str | None:
    if isinstance(expected_type, list):
        for item in expected_type:
            if _matches_type(value, item):
                return item
    return _primary_type(expected_type)


def _standalone_contract(schema: dict[str, Any]) -> dict[str, Any]:
    result = dict(schema)
    result.pop("$schema", None)
    result.pop("title", None)
    return result


def _nullable_contract(schema: dict[str, Any]) -> dict[str, Any]:
    result = dict(schema)
    result["type"] = ["object", "null"]
    return result


def _schema_path(schema: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = schema
    for part in path:
        if not isinstance(current, dict) or part not in current:
            raise SchemaValidationError(f"schema path missing: {'.'.join(path)}")
        current = current[part]
    return current


__all__ = [
    "EMBEDDED_SCHEMA_CONTRACTS",
    "MIRRORED_SCHEMA_CONTRACTS",
    "SchemaValidationError",
    "load_schema",
    "validate_schema",
    "validate_schema_consistency",
    "validate_schema_keywords",
]
