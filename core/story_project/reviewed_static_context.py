"""Bounded static context helpers; these do not establish complete book mapping.

Only explicitly declared names/aliases establish a character correspondence.
Dictionary keys are identifiers, not fallback aliases. Source verification and
author confirmation remain the responsibility of the reviewed-state loader.
"""
from __future__ import annotations

import copy
from typing import Any, Mapping
import unicodedata


STATIC_CHARACTER_FIELDS = (
    "identity", "background", "personality", "appearance", "age", "gender",
    "occupation", "description",
)
STATIC_SPATIAL_FIELDS = ("spaces", "connections")


def static_character_details(
    semantic_characters: Mapping[str, Any] | None,
    reviewed_characters: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Return static fields by reviewed ID, supplementing only unique matches.

    Existing reviewed fields win even when their value is empty or null: an
    explicit cleared value must not be replaced by an old manual value. The
    result contains no dynamic character fields and never adds a new character.
    """
    semantic = _records(semantic_characters, "semantic_characters")
    reviewed = _records(reviewed_characters, "reviewed_characters")
    report = _mapping_report(semantic, reviewed)
    if report["ambiguous_matches"]:
        identities = ", ".join(item["reviewed_id"] for item in report["ambiguous_matches"])
        raise ValueError(f"static character name/alias mapping is ambiguous: {identities}")
    matched = {item["reviewed_id"]: item["semantic_id"] for item in report["matched"]}
    result = {}
    for reviewed_id, current in sorted(reviewed.items()):
        prior = semantic.get(matched.get(reviewed_id), {})
        details = {
            field: copy.deepcopy(current[field] if field in current else prior[field])
            for field in STATIC_CHARACTER_FIELDS
            if field in current or field in prior
        }
        if details:
            result[reviewed_id] = details
    return result


def static_character_mapping_diagnostics(
    semantic_characters: Mapping[str, Any] | None,
    reviewed_characters: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Describe correspondence and unmapped allowlisted fields without guessing.

    Unlike ``static_character_details``, this report exposes ambiguous matches
    without raising. It reports only this whitelist and does not claim that all
    character descriptions or other book settings have been represented.
    """
    return _mapping_report(
        _records(semantic_characters, "semantic_characters"),
        _records(reviewed_characters, "reviewed_characters"),
    )


def static_spatial_fields(spatial: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy only supported space descriptions and connections, never positions."""
    if spatial is None:
        return {}
    if not isinstance(spatial, Mapping):
        raise ValueError("spatial must be a mapping")
    return {field: copy.deepcopy(spatial[field]) for field in STATIC_SPATIAL_FIELDS if field in spatial}


def _records(value: Mapping[str, Any] | None, label: str) -> dict[str, Mapping[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    records = {}
    for key, record in value.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(record, Mapping):
            raise ValueError(f"{label} requires non-empty IDs and record mappings")
        records[key] = record
    return records


def _declared_names(record: Mapping[str, Any]) -> set[str]:
    aliases = record.get("aliases")
    values = [record.get("canonical_name"), record.get("name")]
    if isinstance(aliases, str):
        values.append(aliases)
    elif isinstance(aliases, (list, tuple)):
        values.extend(aliases)
    return {
        unicodedata.normalize("NFKC", value).casefold().strip()
        for value in values if isinstance(value, str) and value.strip()
    }


def _mapping_report(
    semantic: Mapping[str, Mapping[str, Any]],
    reviewed: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    semantic_names = {key: _declared_names(record) for key, record in semantic.items()}
    reviewed_names = {key: _declared_names(record) for key, record in reviewed.items()}
    candidates = {
        reviewed_id: sorted(semantic_id for semantic_id, names in semantic_names.items() if names & refs)
        for reviewed_id, refs in sorted(reviewed_names.items())
    }
    reverse = {
        semantic_id: sorted(reviewed_id for reviewed_id, ids in candidates.items() if semantic_id in ids)
        for semantic_id in semantic
    }
    matched, ambiguous, unmatched = [], [], []
    for reviewed_id, ids in candidates.items():
        if not ids:
            unmatched.append(reviewed_id)
            continue
        competing = sorted({other for semantic_id in ids for other in reverse[semantic_id]
                            if other != reviewed_id})
        if len(ids) != 1 or competing:
            ambiguous.append({"reviewed_id": reviewed_id, "candidate_semantic_ids": ids,
                              "competing_reviewed_ids": competing})
            continue
        prior = semantic[ids[0]]
        current = reviewed[reviewed_id]
        matched.append({
            "reviewed_id": reviewed_id, "semantic_id": ids[0],
            "copied_fields": [field for field in STATIC_CHARACTER_FIELDS if field in prior and field not in current],
            "reviewed_preferred_fields": [field for field in STATIC_CHARACTER_FIELDS if field in current],
        })
    matched_semantic = {item["semantic_id"] for item in matched}
    unmapped = []
    for semantic_id, record in sorted(semantic.items()):
        fields = [field for field in STATIC_CHARACTER_FIELDS if field in record]
        if not fields or semantic_id in matched_semantic:
            continue
        reason = ("ambiguous_name_alias" if reverse[semantic_id]
                  else "no_declared_name_alias" if not semantic_names[semantic_id]
                  else "no_reviewed_match")
        unmapped.append({"semantic_id": semantic_id, "fields": fields, "reason": reason,
                         "candidate_reviewed_ids": reverse[semantic_id]})
    return {
        "policy": "declared_name_alias_unique_v1",
        "supported_fields": list(STATIC_CHARACTER_FIELDS),
        "matched": matched,
        "unmatched_reviewed_ids": unmatched,
        "ambiguous_matches": ambiguous,
        "unmapped_semantic_static_fields": unmapped,
    }


__all__ = [
    "STATIC_CHARACTER_FIELDS", "STATIC_SPATIAL_FIELDS",
    "static_character_details", "static_character_mapping_diagnostics", "static_spatial_fields",
]
