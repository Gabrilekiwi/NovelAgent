from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Iterable


CANONICAL_JSON_ALGORITHM = "novelagent-canonical-json-v1"
ENVIRONMENT_FIELD_NAMES = frozenset(
    {
        "absolute_path",
        "file_path",
        "mtime",
        "mtime_ns",
        "storage_path",
    }
)


class CanonicalJSONError(ValueError):
    pass


def canonical_json_bytes(
    value: Any,
    *,
    exclude_fields: Iterable[str] = (),
    exclude_environment_fields: bool = True,
) -> bytes:
    """Return deterministic UTF-8 JSON for integrity hashing.

    Version 1 sorts object keys, emits no insignificant whitespace, preserves
    Unicode, rejects non-finite numbers, and removes explicitly excluded hash
    fields. Storage-only fields are excluded so moving an event store does not
    change its semantic identity.
    """

    excluded = frozenset(str(field) for field in exclude_fields)
    normalized = _normalized_for_hash(
        value,
        excluded=excluded,
        exclude_environment_fields=exclude_environment_fields,
    )
    try:
        rendered = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CanonicalJSONError(f"value is not canonical JSON compatible: {exc}") from exc
    return rendered.encode("utf-8")


def canonical_json_hash(
    value: Any,
    *,
    exclude_fields: Iterable[str] = (),
    exclude_environment_fields: bool = True,
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            value,
            exclude_fields=exclude_fields,
            exclude_environment_fields=exclude_environment_fields,
        )
    ).hexdigest()


def canonical_copy_for_hash(
    value: Any,
    *,
    exclude_fields: Iterable[str] = (),
    exclude_environment_fields: bool = True,
) -> Any:
    return _normalized_for_hash(
        value,
        excluded=frozenset(str(field) for field in exclude_fields),
        exclude_environment_fields=exclude_environment_fields,
    )


def _normalized_for_hash(
    value: Any,
    *,
    excluded: frozenset[str],
    exclude_environment_fields: bool,
) -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            if key in excluded:
                continue
            if exclude_environment_fields and key in ENVIRONMENT_FIELD_NAMES:
                continue
            normalized[key] = _normalized_for_hash(
                child,
                excluded=excluded,
                exclude_environment_fields=exclude_environment_fields,
            )
        return normalized
    if isinstance(value, list):
        return [
            _normalized_for_hash(
                child,
                excluded=excluded,
                exclude_environment_fields=exclude_environment_fields,
            )
            for child in value
        ]
    if isinstance(value, tuple):
        return [
            _normalized_for_hash(
                child,
                excluded=excluded,
                exclude_environment_fields=exclude_environment_fields,
            )
            for child in value
        ]
    return deepcopy(value)


__all__ = [
    "CANONICAL_JSON_ALGORITHM",
    "ENVIRONMENT_FIELD_NAMES",
    "CanonicalJSONError",
    "canonical_copy_for_hash",
    "canonical_json_bytes",
    "canonical_json_hash",
]
