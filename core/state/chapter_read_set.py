from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Callable

from core.schema import validate_schema


CHAPTER_CONTEXT_READ_SET_SCHEMA_VERSION = "1.0"
CHAPTER_CONTEXT_READ_SET_MODE = "explicit"

_STATE_COLLECTIONS = frozenset(
    {
        "characters",
        "relationships",
        "roster",
        "numeric_counters",
        "inventory",
        "locations",
    }
)
_CONTRACT_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "chapter_index",
        "required_state_item_ids",
        "required_event_item_ids",
        "continuity",
        "narrative_constraints",
        "expected_new_entities",
        "source_outline_sha256",
        "contract_sha256",
    }
)
_CONTINUITY_FIELDS = frozenset(
    {
        "last_scene_location",
        "last_scene_character_ids",
        "required_opening_bridge",
    }
)
_NARRATIVE_CONSTRAINT_FIELDS = frozenset(
    {
        "constraint_id",
        "lifecycle_action",
        "instruction",
    }
)
_EXPECTED_NEW_ENTITY_FIELDS = frozenset(
    {
        "kind",
        "entity_id",
        "display_name",
    }
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_STABLE_ID_PATTERN = re.compile(r"^[^\s/]+$")
_QUALIFIED_ITEM_ID_PATTERN = re.compile(r"^([^/\s]+)/([^/\s]+)$")
_FENCED_CONTRACT_PATTERN = re.compile(
    r"^[ \t]*```novelagent-chapter-context[ \t]*\r?\n"
    r"(?P<body>.*?)"
    r"\r?\n[ \t]*```[ \t]*(?=\r?$)",
    re.MULTILINE | re.DOTALL,
)
_FENCE_OPENING_PATTERN = re.compile(
    r"^[ \t]*```novelagent-chapter-context[ \t]*(?=\r?$)",
    re.MULTILINE,
)


def normalize_chapter_context_read_set(
    value: Any,
    *,
    chapter_index: int,
    source_outline_sha256: str,
) -> dict[str, Any]:
    """Normalize and bind one explicit chapter authority read-set contract.

    The contract hash covers the normalized contract body plus its source
    outline hash, but deliberately excludes ``contract_sha256`` itself.
    """

    expected_chapter_index = _positive_integer(chapter_index, "chapter_index")
    expected_source_hash = _sha256(source_outline_sha256, "source_outline_sha256")
    source = _object(value, "chapter context read set")
    unknown = set(source) - _CONTRACT_FIELDS
    if unknown:
        raise ValueError(
            "chapter context read set contains unsupported field(s): "
            + ", ".join(sorted(str(item) for item in unknown))
        )

    schema_version = _required_text(source, "schema_version")
    if schema_version != CHAPTER_CONTEXT_READ_SET_SCHEMA_VERSION:
        raise ValueError(
            "chapter context read set schema_version must be "
            f"{CHAPTER_CONTEXT_READ_SET_SCHEMA_VERSION!r}"
        )
    mode = _required_text(source, "mode")
    if mode != CHAPTER_CONTEXT_READ_SET_MODE:
        raise ValueError(
            f"chapter context read set mode must be {CHAPTER_CONTEXT_READ_SET_MODE!r}"
        )
    declared_chapter_index = _positive_integer(
        _required(source, "chapter_index"),
        "chapter context read set chapter_index",
    )
    if declared_chapter_index != expected_chapter_index:
        raise ValueError(
            "chapter context read set chapter_index does not match requested "
            f"chapter {expected_chapter_index}"
        )

    declared_source_hash = source.get("source_outline_sha256")
    if declared_source_hash is not None and _sha256(
        declared_source_hash,
        "chapter context read set source_outline_sha256",
    ) != expected_source_hash:
        raise ValueError(
            "chapter context read set source_outline_sha256 does not match "
            "the full outline text"
        )

    normalized: dict[str, Any] = {
        "schema_version": CHAPTER_CONTEXT_READ_SET_SCHEMA_VERSION,
        "mode": CHAPTER_CONTEXT_READ_SET_MODE,
        "chapter_index": expected_chapter_index,
        "required_state_item_ids": _normalized_item_ids(
            _required(source, "required_state_item_ids"),
            field="required_state_item_ids",
            allowed_collections=_STATE_COLLECTIONS,
        ),
        "required_event_item_ids": _normalized_item_ids(
            _required(source, "required_event_item_ids"),
            field="required_event_item_ids",
            allowed_collections=frozenset({"events"}),
        ),
        "continuity": _normalize_continuity(_required(source, "continuity")),
        "narrative_constraints": _normalize_narrative_constraints(
            _required(source, "narrative_constraints")
        ),
        "expected_new_entities": _normalize_expected_new_entities(
            _required(source, "expected_new_entities")
        ),
        "source_outline_sha256": expected_source_hash,
    }
    contract_sha256 = _canonical_sha256(normalized)
    declared_contract_hash = source.get("contract_sha256")
    if declared_contract_hash is not None and _sha256(
        declared_contract_hash,
        "chapter context read set contract_sha256",
    ) != contract_sha256:
        raise ValueError(
            "chapter context read set contract_sha256 does not match its "
            "canonical contract body"
        )
    normalized["contract_sha256"] = contract_sha256
    return validate_schema(
        normalized,
        "chapter_context_read_set.schema.json",
    )


def parse_chapter_context_read_set(
    outline_text: str,
    *,
    chapter_index: int,
    source_outline_sha256: str,
) -> dict[str, Any] | None:
    """Parse the one explicitly tagged JSON contract in a chapter outline."""

    if not isinstance(outline_text, str):
        raise TypeError("outline_text must be a string")
    matches = list(_FENCED_CONTRACT_PATTERN.finditer(outline_text))
    if not matches:
        if _FENCE_OPENING_PATTERN.search(outline_text):
            raise ValueError(
                "novelagent-chapter-context fenced block is malformed or unclosed"
            )
        return None
    if len(matches) != 1:
        raise ValueError(
            "chapter outline must contain at most one "
            "novelagent-chapter-context fenced block"
        )
    body = matches[0].group("body").strip()
    if not body:
        raise ValueError("novelagent-chapter-context fenced block is empty")

    def reject_duplicate_keys(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(
                    "novelagent-chapter-context contains duplicate JSON key "
                    f"{key!r}"
                )
            result[key] = item
        return result

    try:
        value = json.loads(body, object_pairs_hook=reject_duplicate_keys)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "novelagent-chapter-context fenced block must contain one JSON object"
        ) from exc
    return normalize_chapter_context_read_set(
        value,
        chapter_index=chapter_index,
        source_outline_sha256=source_outline_sha256,
    )


def required_authority_item_ids(read_set: Any) -> tuple[str, ...]:
    """Return the exact authority record IDs declared by a normalized read set."""

    source = _object(read_set, "chapter context read set")
    state_ids = _normalized_item_ids(
        _required(source, "required_state_item_ids"),
        field="required_state_item_ids",
        allowed_collections=_STATE_COLLECTIONS,
    )
    event_ids = _normalized_item_ids(
        _required(source, "required_event_item_ids"),
        field="required_event_item_ids",
        allowed_collections=frozenset({"events"}),
    )
    return tuple([*state_ids, *event_ids])


def _normalize_continuity(value: Any) -> dict[str, Any]:
    source = _strict_object(value, "continuity", _CONTINUITY_FIELDS)
    return {
        "last_scene_location": _text(
            _required(source, "last_scene_location"),
            "continuity.last_scene_location",
            allow_empty=True,
        ),
        "last_scene_character_ids": _normalized_string_set(
            _required(source, "last_scene_character_ids"),
            field="continuity.last_scene_character_ids",
            validator=_stable_id,
        ),
        "required_opening_bridge": _text(
            _required(source, "required_opening_bridge"),
            "continuity.required_opening_bridge",
            allow_empty=True,
        ),
    }


def _normalize_narrative_constraints(value: Any) -> list[dict[str, str]]:
    items = _array(value, "narrative_constraints")
    by_id: dict[str, dict[str, str]] = {}
    for index, raw in enumerate(items):
        field = f"narrative_constraints[{index}]"
        source = _strict_object(raw, field, _NARRATIVE_CONSTRAINT_FIELDS)
        normalized = {
            "constraint_id": _stable_id(
                _required(source, "constraint_id"),
                f"{field}.constraint_id",
            ),
            "lifecycle_action": _stable_id(
                _required(source, "lifecycle_action"),
                f"{field}.lifecycle_action",
            ),
            "instruction": _text(
                _required(source, "instruction"),
                f"{field}.instruction",
            ),
        }
        constraint_id = normalized["constraint_id"]
        existing = by_id.get(constraint_id)
        if existing is not None and existing != normalized:
            raise ValueError(
                "narrative_constraints contains conflicting entries for "
                f"constraint_id {constraint_id!r}"
            )
        by_id[constraint_id] = normalized
    return [by_id[key] for key in sorted(by_id)]


def _normalize_expected_new_entities(value: Any) -> list[dict[str, str]]:
    items = _array(value, "expected_new_entities")
    by_identity: dict[tuple[str, str], dict[str, str]] = {}
    for index, raw in enumerate(items):
        field = f"expected_new_entities[{index}]"
        source = _strict_object(raw, field, _EXPECTED_NEW_ENTITY_FIELDS)
        normalized = {
            "kind": _stable_id(
                _required(source, "kind"),
                f"{field}.kind",
            ),
            "entity_id": _stable_id(
                _required(source, "entity_id"),
                f"{field}.entity_id",
            ),
            "display_name": _text(
                _required(source, "display_name"),
                f"{field}.display_name",
            ),
        }
        identity = (normalized["kind"], normalized["entity_id"])
        existing = by_identity.get(identity)
        if existing is not None and existing != normalized:
            raise ValueError(
                "expected_new_entities contains conflicting entries for "
                f"{identity[0]}/{identity[1]}"
            )
        by_identity[identity] = normalized
    return [by_identity[key] for key in sorted(by_identity)]


def _normalized_item_ids(
    value: Any,
    *,
    field: str,
    allowed_collections: frozenset[str],
) -> list[str]:
    def validate_item(raw: Any, item_field: str) -> str:
        item_id = _text(raw, item_field)
        match = _QUALIFIED_ITEM_ID_PATTERN.fullmatch(item_id)
        if match is None:
            raise ValueError(f"{item_field} must be a complete collection/id")
        collection = match.group(1)
        if collection not in allowed_collections:
            allowed = ", ".join(sorted(allowed_collections))
            raise ValueError(
                f"{item_field} collection must be one of: {allowed}"
            )
        return item_id

    return _normalized_string_set(
        value,
        field=field,
        validator=validate_item,
    )


def _normalized_string_set(
    value: Any,
    *,
    field: str,
    validator: Callable[[Any, str], str],
) -> list[str]:
    items = _array(value, field)
    normalized = {
        validator(raw, f"{field}[{index}]")
        for index, raw in enumerate(items)
    }
    return sorted(normalized)


def _strict_object(
    value: Any,
    field: str,
    expected_fields: frozenset[str],
) -> dict[str, Any]:
    source = _object(value, field)
    unknown = set(source) - expected_fields
    if unknown:
        raise ValueError(
            f"{field} contains unsupported field(s): "
            + ", ".join(sorted(str(item) for item in unknown))
        )
    missing = expected_fields - set(source)
    if missing:
        raise ValueError(
            f"{field} is missing required field(s): "
            + ", ".join(sorted(missing))
        )
    return source


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    return copy.deepcopy(value)


def _array(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be an array")
    return list(value)


def _required(source: dict[str, Any], field: str) -> Any:
    if field not in source:
        raise ValueError(f"chapter context read set field {field!r} is required")
    return source[field]


def _required_text(source: dict[str, Any], field: str) -> str:
    return _text(_required(source, field), field)


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def _stable_id(value: Any, field: str) -> str:
    normalized = _text(value, field)
    if _STABLE_ID_PATTERN.fullmatch(normalized) is None:
        raise ValueError(
            f"{field} must be a stable ID without whitespace or slash characters"
        )
    return normalized


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TypeError(f"{field} must be a positive integer")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase sha256 digest")
    return value


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "CHAPTER_CONTEXT_READ_SET_MODE",
    "CHAPTER_CONTEXT_READ_SET_SCHEMA_VERSION",
    "normalize_chapter_context_read_set",
    "parse_chapter_context_read_set",
    "required_authority_item_ids",
]
