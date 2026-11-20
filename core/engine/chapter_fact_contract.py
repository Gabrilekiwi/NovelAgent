"""One payload contract for both model instructions and local validation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.schema import load_schema, validate_schema, validate_schema_keywords


PAYLOAD_SCHEMA = "chapter_fact_payloads.schema.json"


def fact_payload_contracts() -> dict[str, Any]:
    schema = load_schema(PAYLOAD_SCHEMA)
    validate_schema_keywords(schema, PAYLOAD_SCHEMA)
    return schema["properties"]


def fact_payload_contract(kind: str) -> dict[str, Any]:
    return fact_payload_contracts()[kind]


def validate_fact_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    validate_schema({kind: payload}, PAYLOAD_SCHEMA)
    return payload


def fact_contract_example() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "examples/chapter_fact_review/seven_kinds.json"
    return json.loads(path.read_text(encoding="utf-8"))
