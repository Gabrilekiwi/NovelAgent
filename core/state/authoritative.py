from __future__ import annotations

import copy
import json
import re
import unicodedata
from typing import Any

from core.schema import validate_schema
from core.state.prose_state_alignment import validate_roster_count_claims
from core.state.roster import ROSTER_INVALID_MUTATION, reduce_roster_mutation


AUTHORITATIVE_STATE_SCHEMA_VERSION = "1.0"
SOURCE_PRECEDENCE = (
    "story_project_standard",
    "chapter_event",
    "model_inference",
)
_STATE_COLLECTIONS = (
    "characters",
    "relationships",
    "roster",
    "numeric_counters",
    "inventory",
    "locations",
    "events",
)
_AUTHORITY_DELTA_COLLECTIONS = (
    "character_changes",
    "relationship_changes",
    "roster_changes",
    "numeric_changes",
    "inventory_changes",
    "location_changes",
    "events",
)
_SCENE_DELTA_KEY_MAP = {
    "characters": "character_changes",
    "relationships": "relationship_changes",
    "rosters": "roster_changes",
    "counters": "numeric_changes",
    "inventory": "inventory_changes",
    "locations": "location_changes",
}
_KNOWN_COUNTER_SPECS: dict[str, dict[str, Any]] = {
    "erosion": {
        "field_names": ("erosion", "erosion_value", "侵蚀", "侵蚀值"),
        "labels": ("侵蚀值", "侵蚀", "erosion"),
        "minimum": 0,
        "maximum": 100,
        "rule": "monotonic_non_decreasing",
    },
    "corruption": {
        "field_names": ("corruption", "corruption_value", "污染", "污染值", "腐化", "腐化值"),
        "labels": ("污染值", "腐化值", "corruption"),
        "minimum": 0,
        "maximum": 100,
        "rule": "monotonic_non_decreasing",
    },
    "infection": {
        "field_names": ("infection", "infection_value", "感染", "感染值"),
        "labels": ("感染值", "感染", "infection"),
        "minimum": 0,
        "maximum": 100,
        "rule": "monotonic_non_decreasing",
    },
}


class AuthoritativeStateError(ValueError):
    def __init__(self, report: dict[str, Any]) -> None:
        self.report = copy.deepcopy(report)
        codes = ", ".join(str(item.get("code")) for item in report.get("findings") or [])
        super().__init__(f"authoritative state delta rejected: {codes or 'unknown'}")


def empty_authoritative_state() -> dict[str, Any]:
    return {
        "schema_version": AUTHORITATIVE_STATE_SCHEMA_VERSION,
        "source_precedence": list(SOURCE_PRECEDENCE),
        "characters": {},
        "relationships": {},
        "roster": {},
        "numeric_counters": {},
        "inventory": {},
        "locations": {},
        "events": {},
    }


def validate_authoritative_state(state: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_state(state)
    report = validate_authoritative_state_delta(
        base_state=empty_authoritative_state(),
        state_delta=_full_state_delta(normalized),
        chapter_text="",
    )
    if not report["accepted"]:
        raise AuthoritativeStateError(report)
    return normalized


def validate_authoritative_state_delta(
    *,
    base_state: dict[str, Any],
    state_delta: dict[str, Any],
    chapter_text: str,
) -> dict[str, Any]:
    base = _normalize_state(base_state)
    delta = state_delta if isinstance(state_delta, dict) else {}
    after = copy.deepcopy(base)
    findings: list[dict[str, Any]] = []
    source_tier = _source_tier(delta)
    declared_events = {
        str(item.get("event_id") or ""): item
        for item in _objects(delta.get("events"))
        if str(item.get("event_id") or "")
    }

    findings.extend(
        _apply_character_changes(
            after,
            delta.get("character_changes"),
            source_tier=source_tier,
            declared_events=declared_events,
        )
    )
    findings.extend(
        _apply_relationship_changes(
            after,
            delta.get("relationship_changes"),
            source_tier=source_tier,
            declared_events=declared_events,
        )
    )
    findings.extend(
        _apply_roster_changes(
            after,
            delta.get("roster_changes"),
            source_tier=source_tier,
            declared_events=declared_events,
        )
    )
    findings.extend(
        _apply_numeric_changes(
            after,
            delta.get("numeric_changes"),
            source_tier=source_tier,
            declared_events=declared_events,
        )
    )
    findings.extend(
        _apply_inventory_changes(
            after,
            delta.get("inventory_changes"),
            source_tier=source_tier,
            declared_events=declared_events,
        )
    )
    findings.extend(_apply_location_changes(after, delta.get("location_changes"), source_tier=source_tier))
    findings.extend(_apply_events(after, delta.get("events"), source_tier=source_tier))
    findings.extend(
        _validate_chapter_counter_declarations(
            chapter_text,
            after,
            numeric_changes=delta.get("numeric_changes"),
        )
    )
    findings.extend(
        validate_roster_count_claims(
            chapter_text=chapter_text,
            state_before=base,
            state_after=after,
            roster_changes=delta.get("roster_changes"),
        )
    )

    return {
        "schema_version": AUTHORITATIVE_STATE_SCHEMA_VERSION,
        "accepted": not findings,
        "blocking": bool(findings),
        "findings": findings,
        "source_precedence": list(SOURCE_PRECEDENCE),
        "applied_source_tier": source_tier,
        "state_after": after,
    }


def require_authoritative_state_delta(report: dict[str, Any]) -> dict[str, Any]:
    if not bool(report.get("accepted")):
        raise AuthoritativeStateError(report)
    return report


def merge_authoritative_report_into_validation(
    validation: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    """Expose authority conflicts to the same QualityDecision input as validators."""

    base = copy.deepcopy(validation)
    authority_problems = [
        _authority_problem(item)
        for item in report.get("findings") or []
        if isinstance(item, dict)
    ]
    checks = [
        copy.deepcopy(item)
        for item in base.get("checks") or []
        if isinstance(item, dict)
        and item.get("name") != "authoritative_state"
    ]
    if not checks:
        legacy_problems = [
            _normalize_validation_problem(item, validation_ok=bool(base.get("ok")))
            for item in base.get("problems") or []
            if isinstance(item, dict)
        ]
        checks.append(
            {
                "name": "custom_validator",
                "ok": not any(item["blocking"] for item in legacy_problems),
                "problems": legacy_problems,
            }
        )
    checks.append(
        {
            "name": "authoritative_state",
            "ok": not authority_problems,
            "problems": authority_problems,
            "schema_version": str(
                report.get("schema_version") or AUTHORITATIVE_STATE_SCHEMA_VERSION
            ),
            "applied_source_tier": str(report.get("applied_source_tier") or ""),
        }
    )
    all_problems = [
        _normalize_validation_problem(
            problem,
            validation_ok=bool(check.get("ok")),
        )
        for check in checks
        for problem in check.get("problems") or []
        if isinstance(problem, dict)
    ]
    normalized_checks: list[dict[str, Any]] = []
    cursor = 0
    for check in checks:
        count = len(
            [item for item in check.get("problems") or [] if isinstance(item, dict)]
        )
        normalized_checks.append(
            {
                **copy.deepcopy(check),
                "problems": all_problems[cursor : cursor + count],
                "ok": not any(
                    item["blocking"]
                    for item in all_problems[cursor : cursor + count]
                ),
            }
        )
        cursor += count
    executed = [str(item) for item in base.get("executed_checks") or []]
    if "authoritative_state" not in executed:
        executed.append("authoritative_state")
    severity_order = ("critical", "high", "medium", "low")
    action_order = (
        "seed_conflict_scene",
        "expand_scene",
        "add_conflict_signal",
        "remove_forbidden_term",
        "add_required_term",
        "anchor_known_location",
        "insert_opening_bridge",
        "rewrite_spatial_transition",
        "anchor_last_scene_state",
        "repair_character_position",
        "add_transition_event",
        "flag_unknown_location",
        "add_character_location",
        "rewrite_inactive_character_action",
        "correct_chapter_index",
        "manual_review",
    )
    base.update(
        {
            "ok": not any(item["blocking"] for item in all_problems),
            "requested_focus": [
                str(item) for item in base.get("requested_focus") or []
            ],
            "executed_checks": executed,
            "skipped_checks": [
                str(item) for item in base.get("skipped_checks") or []
            ],
            "checks": normalized_checks,
            "problems": all_problems,
            "blocking_problem_count": sum(
                1 for item in all_problems if item["blocking"]
            ),
            "warning_count": sum(
                1 for item in all_problems if not item["blocking"]
            ),
            "severity_counts": [
                {
                    "severity": severity,
                    "count": sum(
                        1
                        for item in all_problems
                        if item["severity"] == severity
                    ),
                }
                for severity in severity_order
            ],
            "deterministic_repair_count": sum(
                1
                for item in all_problems
                if item["repair_action"] != "manual_review"
            ),
            "manual_review_count": sum(
                1
                for item in all_problems
                if item["repair_action"] == "manual_review"
            ),
            "repair_action_counts": [
                {
                    "action": action,
                    "count": sum(
                        1
                        for item in all_problems
                        if item["repair_action"] == action
                    ),
                }
                for action in action_order
            ],
        }
    )
    return validate_schema(base, "validation_result.schema.json")


def normalize_entity_alias(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def seed_authoritative_state_from_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Deterministically bootstrap authority ledgers from a legacy/runtime snapshot.

    Existing authoritative records always win. Legacy character facts only fill
    missing identity, alias, location, and known numeric-counter records. Mutable
    legacy facts default to model-inference precedence so a structured chapter
    event can establish a newer auditable baseline without weakening persisted
    authoritative records.
    """

    source = snapshot if isinstance(snapshot, dict) else {}
    state = _normalize_state(
        source.get("authoritative_state")
        if isinstance(source.get("authoritative_state"), dict)
        else None
    )
    _seed_top_level_numeric_counters(state, source.get("numeric_counters"))

    legacy_characters = source.get("characters")
    raw_characters = legacy_characters if isinstance(legacy_characters, dict) else {}
    for legacy_key in sorted(raw_characters, key=lambda item: str(item)):
        raw = raw_characters[legacy_key]
        if not isinstance(raw, dict):
            continue
        values = _legacy_character_values(raw)
        canonical_name = str(
            values.get("canonical_name")
            or values.get("name")
            or legacy_key
        ).strip()
        if not canonical_name:
            continue
        explicit_id = str(
            values.get("character_id")
            or values.get("stable_id")
            or values.get("id")
            or legacy_key
        ).strip()
        alias_owners = _character_alias_owners(state["characters"])
        character_id = next(
            (
                alias_owners[normalized]
                for candidate in (canonical_name, legacy_key)
                if (normalized := normalize_entity_alias(str(candidate))) in alias_owners
            ),
            explicit_id,
        )
        if not character_id:
            continue
        existing = state["characters"].get(character_id)
        record = copy.deepcopy(existing) if isinstance(existing, dict) else {}
        aliases = _unique_strings(
            [
                *(record.get("aliases") or []),
                canonical_name,
                str(legacy_key),
                *_string_values(values.get("aliases")),
                *_string_values(values.get("alias")),
            ]
        )
        record.update(
            {
                "character_id": character_id,
                "canonical_name": str(
                    record.get("canonical_name")
                    or record.get("name")
                    or canonical_name
                ),
                "aliases": aliases,
                "source_tier": str(
                    record.get("source_tier")
                    or values.get("source_tier")
                    or "model_inference"
                ),
            }
        )
        identity = (
            record.get("identity")
            or values.get("identity")
            or values.get("role")
        )
        if identity not in (None, ""):
            record["identity"] = copy.deepcopy(identity)
        for field in (
            "role",
            "status",
            "condition",
            "current_goal",
            "last_observation",
            "last_seen_chapter",
            "traits",
        ):
            if field not in record and values.get(field) not in (None, ""):
                record[field] = copy.deepcopy(values[field])
        state["characters"][character_id] = record

    positions = _snapshot_character_positions(source)
    alias_owners = _character_alias_owners(state["characters"])
    for character_id, record in state["characters"].items():
        if not isinstance(record, dict) or character_id in state["locations"]:
            continue
        legacy_key, legacy_record = _legacy_record_for_character(
            raw_characters,
            character_id,
            record,
        )
        values = _legacy_character_values(legacy_record)
        location = _position_for_character(
            positions,
            character_id=character_id,
            canonical_name=str(record.get("canonical_name") or legacy_key or character_id),
            aliases=record.get("aliases"),
        )
        if location in (None, ""):
            location = values.get("current_location") or values.get("location")
        if location not in (None, ""):
            state["locations"][character_id] = {
                "entity_id": character_id,
                "location_id": copy.deepcopy(location),
                "source_tier": str(record.get("source_tier") or "model_inference"),
            }

    _seed_character_numeric_counters(
        state,
        raw_characters,
        alias_owners=alias_owners,
    )
    return state


def adapt_scene_deltas_to_authoritative_delta(
    scene_drafts: Any,
    *,
    base_state: dict[str, Any] | None = None,
    source_tier: str = "chapter_event",
) -> dict[str, Any]:
    """Adapt ordered Scene field deltas into authority-validator changes."""

    payload, payload_source_tier, embedded_baseline = _collect_scene_authority_payload(scene_drafts)
    baseline = _normalize_state(base_state if isinstance(base_state, dict) else embedded_baseline)
    applied_source_tier = (
        payload_source_tier
        if payload_source_tier in SOURCE_PRECEDENCE
        else source_tier if source_tier in SOURCE_PRECEDENCE else "chapter_event"
    )
    result: dict[str, Any] = {
        "source_tier": applied_source_tier,
        "baseline_state": copy.deepcopy(baseline),
        **{key: [] for key in _AUTHORITY_DELTA_COLLECTIONS},
    }
    result["events"] = [copy.deepcopy(item) for item in payload["events"]]

    staged_characters = copy.deepcopy(baseline["characters"])
    canonical_hints = _character_canonical_hints(
        payload["character_changes"],
        staged_characters,
    )
    for raw in payload["character_changes"]:
        adapted = _adapt_character_field_delta(raw, staged_characters, canonical_hints)
        result["character_changes"].append(adapted)

    staged_relationships = copy.deepcopy(baseline["relationships"])
    relationship_hints = _relationship_hints(
        payload["relationship_changes"],
        staged_relationships,
    )
    for raw in payload["relationship_changes"]:
        adapted = _adapt_relationship_field_delta(raw, staged_relationships, relationship_hints)
        result["relationship_changes"].append(adapted)

    result["roster_changes"] = [
        _with_event_reference(item)
        for item in payload["roster_changes"]
    ]
    result["numeric_changes"] = [
        _adapt_numeric_field_delta(item)
        for item in payload["numeric_changes"]
    ]
    result["inventory_changes"] = [
        _adapt_inventory_field_delta(item)
        for item in payload["inventory_changes"]
    ]
    result["location_changes"] = [
        copy.deepcopy(item)
        for item in payload["location_changes"]
    ]
    return result


def _collect_scene_authority_payload(
    value: Any,
) -> tuple[dict[str, list[dict[str, Any]]], str, dict[str, Any] | None]:
    payload = {key: [] for key in _AUTHORITY_DELTA_COLLECTIONS}
    source_tier = ""
    embedded_baseline: dict[str, Any] | None = None
    scenes: Any = value
    if isinstance(value, dict):
        source_tier = str(value.get("source_tier") or "")
        baseline = value.get("baseline_state")
        embedded_baseline = baseline if isinstance(baseline, dict) else None
        if any(key in value for key in _AUTHORITY_DELTA_COLLECTIONS):
            for key in _AUTHORITY_DELTA_COLLECTIONS:
                payload[key].extend(_objects(value.get(key)))
            return payload, source_tier, embedded_baseline
        scenes = value.get("scene_drafts")
        if not isinstance(scenes, list):
            scenes = value.get("scenes")
    for scene in scenes if isinstance(scenes, list) else []:
        if not isinstance(scene, dict):
            continue
        scene_index = int(scene.get("index") or 0)
        for event in _objects(scene.get("events")):
            payload["events"].append({**event, "scene_index": scene_index})
        deltas = scene.get("deltas")
        if not isinstance(deltas, dict):
            deltas = scene.get("state_delta")
        if not isinstance(deltas, dict):
            continue
        for scene_key, authority_key in _SCENE_DELTA_KEY_MAP.items():
            for item in _objects(deltas.get(scene_key)):
                payload[authority_key].append({**item, "scene_index": scene_index})
    return payload, source_tier, embedded_baseline


def _character_canonical_hints(
    changes: list[dict[str, Any]],
    characters: dict[str, Any],
) -> dict[str, str]:
    hints = {
        str(character_id): str(
            record.get("canonical_name")
            or record.get("name")
            or character_id
        )
        for character_id, record in characters.items()
        if isinstance(record, dict)
    }
    for raw in changes:
        character_id = str(raw.get("character_id") or raw.get("id") or "").strip()
        if not character_id:
            continue
        field = str(raw.get("field") or "").strip()
        candidate = (
            raw.get("canonical_name")
            or raw.get("name")
            or (raw.get("after") if field in {"canonical_name", "name"} else None)
        )
        if candidate not in (None, ""):
            hints[character_id] = str(candidate)
    return hints


def _adapt_character_field_delta(
    raw: dict[str, Any],
    staged: dict[str, Any],
    hints: dict[str, str],
) -> dict[str, Any]:
    result = _with_event_reference(raw)
    character_id = str(result.get("character_id") or result.get("id") or "").strip()
    field = str(result.get("field") or "").strip()
    existing = staged.get(character_id)
    canonical_name = (
        result.get("canonical_name")
        or result.get("name")
        or (result.get("after") if field in {"canonical_name", "name"} else None)
        or (existing or {}).get("canonical_name")
        or (existing or {}).get("name")
        or hints.get(character_id)
        or character_id
    )
    if canonical_name not in (None, ""):
        result["canonical_name"] = str(canonical_name)
    if character_id:
        next_record = copy.deepcopy(existing) if isinstance(existing, dict) else {}
        next_record.update(
            {
                "character_id": character_id,
                "canonical_name": str(canonical_name or character_id),
            }
        )
        if field and "after" in result:
            next_record[field] = copy.deepcopy(result["after"])
        else:
            next_record.update(copy.deepcopy(result))
        staged[character_id] = next_record
    return result


def _relationship_hints(
    changes: list[dict[str, Any]],
    relationships: dict[str, Any],
) -> dict[tuple[str, str], dict[str, str]]:
    hints: dict[tuple[str, str], dict[str, str]] = {}
    for relationship_id, record in relationships.items():
        if not isinstance(record, dict):
            continue
        source_id = str(
            record.get("source_character_id") or record.get("source_id") or ""
        ).strip()
        target_id = str(
            record.get("target_character_id") or record.get("target_id") or ""
        ).strip()
        if source_id and target_id:
            hints[(source_id, target_id)] = {
                "relationship_id": str(
                    record.get("relationship_id") or relationship_id
                ),
                "type": str(record.get("type") or record.get("kind") or "relationship"),
            }
    for raw in changes:
        source_id = str(
            raw.get("source_character_id") or raw.get("source_id") or ""
        ).strip()
        target_id = str(
            raw.get("target_character_id") or raw.get("target_id") or ""
        ).strip()
        if not source_id or not target_id:
            continue
        field = str(raw.get("field") or "").strip()
        current = hints.setdefault(
            (source_id, target_id),
            {
                "relationship_id": str(
                    raw.get("relationship_id") or f"{source_id}->{target_id}"
                ),
                "type": "relationship",
            },
        )
        relation_type = (
            raw.get("type")
            or raw.get("kind")
            or (raw.get("after") if field in {"type", "kind"} else None)
        )
        if relation_type not in (None, ""):
            current["type"] = str(relation_type)
    return hints


def _adapt_relationship_field_delta(
    raw: dict[str, Any],
    staged: dict[str, Any],
    hints: dict[tuple[str, str], dict[str, str]],
) -> dict[str, Any]:
    result = _with_event_reference(raw)
    source_id = str(
        result.get("source_character_id") or result.get("source_id") or ""
    ).strip()
    target_id = str(
        result.get("target_character_id") or result.get("target_id") or ""
    ).strip()
    hint = hints.get((source_id, target_id), {})
    relationship_id = str(
        result.get("relationship_id")
        or hint.get("relationship_id")
        or f"{source_id}->{target_id}"
    )
    field = str(result.get("field") or "").strip()
    relation_type = (
        result.get("type")
        or result.get("kind")
        or (result.get("after") if field in {"type", "kind"} else None)
        or hint.get("type")
        or "relationship"
    )
    result.update(
        {
            "relationship_id": relationship_id,
            "source_character_id": source_id,
            "target_character_id": target_id,
            "type": str(relation_type),
        }
    )
    if relationship_id:
        existing = staged.get(relationship_id)
        next_record = copy.deepcopy(existing) if isinstance(existing, dict) else {}
        next_record.update(
            {
                "relationship_id": relationship_id,
                "source_character_id": source_id,
                "target_character_id": target_id,
                "type": str(relation_type),
            }
        )
        if field and "after" in result:
            next_record[field] = copy.deepcopy(result["after"])
        staged[relationship_id] = next_record
    return result


def _adapt_numeric_field_delta(raw: dict[str, Any]) -> dict[str, Any]:
    result = _with_event_reference(raw)
    if "previous_value" not in result and "before" in result:
        result["previous_value"] = copy.deepcopy(result["before"])
    if "expected_value" not in result and "after" in result:
        result["expected_value"] = copy.deepcopy(result["after"])
    if "declared_value" not in result:
        result["declared_value"] = copy.deepcopy(
            result.get("expected_value", result.get("after"))
        )
    spec = _counter_spec_for(str(result.get("counter_id") or ""))
    if spec:
        for key in ("minimum", "maximum", "rule"):
            result.setdefault(key, copy.deepcopy(spec[key]))
    return result


def _adapt_inventory_field_delta(raw: dict[str, Any]) -> dict[str, Any]:
    result = _with_event_reference(raw)
    if "previous_quantity" not in result and "before" in result:
        result["previous_quantity"] = copy.deepcopy(result["before"])
    if "declared_quantity" not in result and "after" in result:
        result["declared_quantity"] = copy.deepcopy(result["after"])
    return result


def _with_event_reference(raw: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(raw)
    if not str(result.get("source_event_id") or "").strip():
        reason_event_id = str(result.get("reason_event_id") or "").strip()
        if reason_event_id:
            result["source_event_id"] = reason_event_id
    return result


def _required_event_reference_finding(
    raw: dict[str, Any],
    *,
    state: dict[str, Any],
    declared_events: dict[str, dict[str, Any]],
    source_tier: str,
    ledger: str,
    current_delta_only: bool = False,
) -> dict[str, Any] | None:
    if source_tier == "story_project_standard":
        return None
    event_ids = [
        str(raw.get("source_event_id") or raw.get("reason_event_id") or "").strip()
    ]
    event_ids = [item for item in event_ids if item]
    if not event_ids:
        return _finding(
            "missing_authority_event_reference",
            f"{ledger} change requires a source event reference.",
            {"ledger": ledger, "change": raw},
        )
    if current_delta_only:
        missing = sorted(
            {
                event_id
                for event_id in event_ids
                if event_id not in declared_events
            }
        )
        if missing:
            return _finding(
                "invalid_authority_event_reference",
                f"{ledger} change must reference an event declared in the current delta.",
                {"ledger": ledger, "event_ids": missing, "change": raw},
            )
        change_scene_index = raw.get("scene_index")
        mismatched = sorted(
            event_id
            for event_id in event_ids
            if change_scene_index is not None
            and declared_events[event_id].get("scene_index") is not None
            and declared_events[event_id].get("scene_index") != change_scene_index
        )
        if mismatched:
            return _finding(
                "invalid_authority_event_reference",
                f"{ledger} change references an event from another Scene.",
                {
                    "ledger": ledger,
                    "event_ids": mismatched,
                    "change_scene_index": change_scene_index,
                    "event_scene_indexes": {
                        event_id: declared_events[event_id].get("scene_index")
                        for event_id in mismatched
                    },
                    "change": raw,
                },
            )
        return None
    known_events = {
        *declared_events,
        *(
            str(event_id)
            for event_id in (state.get("events") or {})
        ),
    }
    missing = sorted({event_id for event_id in event_ids if event_id not in known_events})
    if missing:
        return _finding(
            "invalid_authority_event_reference",
            f"{ledger} change references an undeclared event.",
            {"ledger": ledger, "event_ids": missing, "change": raw},
        )
    return None


def _validate_chapter_counter_declarations(
    chapter_text: str,
    state: dict[str, Any],
    *,
    numeric_changes: Any,
) -> list[dict[str, Any]]:
    text = str(chapter_text or "")
    if not text:
        return []
    findings: list[dict[str, Any]] = []
    counters = [
        record
        for record in (state.get("numeric_counters") or {}).values()
        if isinstance(record, dict)
    ]
    transition_values: dict[str, set[int | float]] = {}
    for change in _objects(numeric_changes):
        counter_id = str(change.get("counter_id") or change.get("id") or "").strip()
        if not counter_id:
            continue
        values = {
            value
            for value in (
                change.get("previous_value", change.get("before")),
                change.get("expected_value", change.get("after")),
                change.get(
                    "declared_value",
                    change.get("expected_value", change.get("after")),
                ),
            )
            if _number(value)
        }
        transition_values.setdefault(counter_id, set()).update(values)
    for record in counters:
        counter_id = str(record.get("counter_id") or record.get("id") or "").strip()
        current = record.get("current_value")
        if not counter_id or not _number(current):
            continue
        labels = [counter_id]
        explicit_label = str(record.get("label") or record.get("name") or "").strip()
        if explicit_label:
            labels.append(explicit_label)
        spec = _counter_spec_for(counter_id)
        if spec and len(counters) == 1:
            labels.extend(str(item) for item in spec["labels"])
        declared_values: list[dict[str, Any]] = []
        for label in _unique_strings(labels):
            pattern = re.compile(
                rf"{re.escape(label)}\s*(?:为|是|=|：|:)?\s*"
                r"(?:由|从)?\s*"
                r"(-?\d+(?:\.\d+)?)\s*(?:/\s*(-?\d+(?:\.\d+)?))?",
                flags=re.IGNORECASE,
            )
            for match in pattern.finditer(text):
                declared_values.append(
                    {
                        "label": label,
                        "value": _parse_number(match.group(1)),
                        "maximum": _parse_number(match.group(2)),
                        "span": [match.start(), match.end()],
                        "text": match.group(0),
                    }
                )
                transition = re.match(
                    r"\s*(?:升至|增至|提高到|变为|变成|到|→|->)\s*"
                    r"(-?\d+(?:\.\d+)?)\s*(?:/\s*(-?\d+(?:\.\d+)?))?",
                    text[match.end() : match.end() + 48],
                    flags=re.IGNORECASE,
                )
                if transition is not None:
                    declared_values.append(
                        {
                            "label": label,
                            "value": _parse_number(transition.group(1)),
                            "maximum": _parse_number(transition.group(2)),
                            "span": [
                                match.end() + transition.start(),
                                match.end() + transition.end(),
                            ],
                            "text": transition.group(0),
                        }
                    )
        mentions_by_span: dict[tuple[int, int, int | float | None], dict[str, Any]] = {
            (
                int(item["span"][0]),
                int(item["span"][1]),
                item.get("value"),
            ): item
            for item in declared_values
        }
        declared_values = list(mentions_by_span.values())
        unique_values = {
            item["value"]
            for item in declared_values
            if _number(item.get("value"))
        }
        allowed_values: set[int | float] = {current}
        allowed_values.update(transition_values.get(counter_id, set()))
        unexpected_values = unique_values - allowed_values
        if len(unique_values) > 1 and unexpected_values:
            findings.append(
                _finding(
                    "numeric_counter_mismatch",
                    f"Counter {counter_id} has contradictory values in chapter prose.",
                    {
                        "kind": "contradictory_prose_values",
                        "counter_id": counter_id,
                        "allowed_transition_values": sorted(allowed_values),
                        "mentions": declared_values,
                    },
                )
            )
        if declared_values and current not in unique_values:
            findings.append(
                _finding(
                    "numeric_counter_mismatch",
                    f"Counter {counter_id} prose omits its final authoritative value.",
                    {
                        "kind": "missing_final_prose_value",
                        "counter_id": counter_id,
                        "authoritative_value": current,
                        "mentions": declared_values,
                    },
                )
            )
        for mention in declared_values:
            if mention.get("value") not in allowed_values:
                code = (
                    "numeric_counter_rollback"
                    if _number(mention.get("value"))
                    and mention["value"] < current
                    and str(record.get("rule") or "") == "monotonic_non_decreasing"
                    else "numeric_counter_mismatch"
                )
                findings.append(
                    _finding(
                        code,
                        f"Counter {counter_id} prose value does not match authoritative state.",
                        {
                            "kind": "prose_state_mismatch",
                            "counter_id": counter_id,
                            "authoritative_value": current,
                            "prose_mention": mention,
                        },
                    )
                )
    return findings


def _seed_top_level_numeric_counters(
    state: dict[str, Any],
    value: Any,
) -> None:
    if not isinstance(value, dict):
        return
    for key in sorted(value, key=lambda item: str(item)):
        raw = value[key]
        record = copy.deepcopy(raw) if isinstance(raw, dict) else {}
        current = (
            record.get("current_value")
            if isinstance(raw, dict)
            else raw
        )
        current = _coerce_counter_value(current)
        if not _number(current):
            continue
        counter_id = str(record.get("counter_id") or key)
        existing = state["numeric_counters"].get(counter_id)
        if isinstance(existing, dict):
            continue
        spec = _counter_spec_for(counter_id)
        seeded = {
            **record,
            "counter_id": counter_id,
            "current_value": current,
            "source_tier": str(record.get("source_tier") or "model_inference"),
        }
        if spec:
            for field in ("minimum", "maximum", "rule"):
                seeded.setdefault(field, copy.deepcopy(spec[field]))
        state["numeric_counters"][counter_id] = seeded


def _seed_character_numeric_counters(
    state: dict[str, Any],
    characters: dict[str, Any],
    *,
    alias_owners: dict[str, str],
) -> None:
    for legacy_key in sorted(characters, key=lambda item: str(item)):
        raw = characters[legacy_key]
        if not isinstance(raw, dict):
            continue
        values = _legacy_character_values(raw)
        explicit_character_id = str(
            values.get("character_id")
            or values.get("stable_id")
            or values.get("id")
            or legacy_key
        ).strip()
        character_id = next(
            (
                alias_owners[normalized]
                for candidate in (
                    explicit_character_id,
                    legacy_key,
                    values.get("canonical_name"),
                    values.get("name"),
                )
                if (
                    normalized := normalize_entity_alias(str(candidate or ""))
                )
                in alias_owners
            ),
            explicit_character_id,
        )
        record = state["characters"].get(character_id)
        if not isinstance(record, dict):
            continue
        canonical_name = str(
            record.get("canonical_name") or legacy_key or character_id
        ).strip()
        for spec_id, spec in _KNOWN_COUNTER_SPECS.items():
            raw_value = next(
                (
                    values[field]
                    for field in spec["field_names"]
                    if field in values and values[field] not in (None, "")
                ),
                None,
            )
            current = _coerce_counter_value(raw_value)
            if not _number(current):
                continue
            counter_id = str(
                values.get(f"{spec_id}_counter_id")
                or (
                    f"{canonical_name}侵蚀值"
                    if spec_id == "erosion" and canonical_name
                    else f"{character_id}:{spec_id}"
                )
            )
            matching_existing = next(
                (
                    existing
                    for existing_id, existing in state["numeric_counters"].items()
                    if isinstance(existing, dict)
                    and _counter_spec_for(str(existing_id)) is spec
                    and (
                        str(existing.get("owner_id") or "") in {"", character_id}
                        or str(existing_id) == counter_id
                    )
                ),
                None,
            )
            if counter_id in state["numeric_counters"] or matching_existing is not None:
                continue
            state["numeric_counters"][counter_id] = {
                "counter_id": counter_id,
                "owner_id": character_id,
                "label": counter_id,
                "current_value": current,
                "minimum": spec["minimum"],
                "maximum": spec["maximum"],
                "rule": spec["rule"],
                "source_tier": str(record.get("source_tier") or "model_inference"),
            }


def _legacy_character_values(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("data", "state", "facts", "value"):
        nested = raw.get(key)
        if isinstance(nested, dict):
            result.update(copy.deepcopy(nested))
    result.update(
        {
            key: copy.deepcopy(value)
            for key, value in raw.items()
            if key not in {"data", "state", "facts", "value"}
        }
    )
    return result


def _snapshot_character_positions(snapshot: dict[str, Any]) -> dict[str, Any]:
    spatial = snapshot.get("spatial_state")
    if not isinstance(spatial, dict):
        return {}
    positions = spatial.get("character_positions")
    return copy.deepcopy(positions) if isinstance(positions, dict) else {}


def _legacy_record_for_character(
    characters: dict[str, Any],
    character_id: str,
    record: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    candidates = {
        normalize_entity_alias(character_id),
        normalize_entity_alias(str(record.get("canonical_name") or "")),
        *(
            normalize_entity_alias(str(alias))
            for alias in record.get("aliases") or []
        ),
    }
    for key, raw in characters.items():
        if not isinstance(raw, dict):
            continue
        values = _legacy_character_values(raw)
        names = {
            normalize_entity_alias(str(key)),
            normalize_entity_alias(str(values.get("character_id") or "")),
            normalize_entity_alias(str(values.get("canonical_name") or "")),
            normalize_entity_alias(str(values.get("name") or "")),
        }
        if (candidates & names) - {""}:
            return str(key), raw
    return "", {}


def _position_for_character(
    positions: dict[str, Any],
    *,
    character_id: str,
    canonical_name: str,
    aliases: Any,
) -> Any:
    wanted = {
        normalize_entity_alias(character_id),
        normalize_entity_alias(canonical_name),
        *(
            (
                normalize_entity_alias(str(alias))
                for alias in aliases
                if str(alias)
            )
            if isinstance(aliases, list)
            else ()
        ),
    }
    for key, value in positions.items():
        if normalize_entity_alias(str(key)) in wanted:
            return copy.deepcopy(value)
    return None


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _counter_spec_for(counter_id: str) -> dict[str, Any] | None:
    normalized = normalize_entity_alias(counter_id)
    for spec_id, spec in _KNOWN_COUNTER_SPECS.items():
        candidates = [spec_id, *spec["field_names"], *spec["labels"]]
        if any(
            normalize_entity_alias(str(candidate)) in normalized
            for candidate in candidates
            if normalize_entity_alias(str(candidate))
        ):
            return spec
    return None


def _coerce_counter_value(value: Any) -> Any:
    if _number(value):
        return value
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if match:
            return _parse_number(match.group(0))
    return value


def _parse_number(value: Any) -> int | float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _normalize_state(value: dict[str, Any] | None) -> dict[str, Any]:
    state = empty_authoritative_state()
    if not isinstance(value, dict):
        return state
    for collection in _STATE_COLLECTIONS:
        raw = value.get(collection)
        if isinstance(raw, dict):
            state[collection] = copy.deepcopy(raw)
    return state


def _apply_character_changes(
    state: dict[str, Any],
    value: Any,
    *,
    source_tier: str,
    declared_events: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    alias_owners = _character_alias_owners(state["characters"])
    for raw in _objects(value):
        character_id = str(raw.get("character_id") or raw.get("id") or "").strip()
        existing = state["characters"].get(character_id)
        canonical_name = str(
            raw.get("canonical_name")
            or raw.get("name")
            or (existing or {}).get("canonical_name")
            or (existing or {}).get("name")
            or ""
        ).strip()
        if not character_id or not canonical_name:
            findings.append(_finding("invalid_character_change", "Character change requires stable id and name.", raw))
            continue
        field = str(raw.get("field") or "").strip()
        incoming = copy.deepcopy(raw)
        if field and "after" in raw:
            if isinstance(existing, dict) and field in existing and existing[field] != raw.get("before"):
                findings.append(
                    _finding(
                        "character_state_rollback",
                        f"Character {character_id} field {field} starts from stale state.",
                        {
                            "character_id": character_id,
                            "field": field,
                            "expected_before": existing[field],
                            "declared_before": raw.get("before"),
                        },
                    )
                )
            incoming[field] = copy.deepcopy(raw["after"])
        aliases = _unique_strings([canonical_name, *(raw.get("aliases") or [])])
        for alias in aliases:
            owner = alias_owners.get(normalize_entity_alias(alias))
            if owner is not None and owner != character_id:
                findings.append(
                    _finding(
                        "character_identity_drift",
                        f"Alias {alias!r} already belongs to {owner}, not {character_id}.",
                        {"alias": alias, "existing_character_id": owner, "incoming_character_id": character_id},
                    )
                )
        if isinstance(existing, dict):
            old_name = str(existing.get("canonical_name") or existing.get("name") or "")
            old_identity = str(existing.get("identity") or "")
            new_identity = str(incoming.get("identity") or old_identity)
            if old_name and old_name != canonical_name:
                findings.append(
                    _finding(
                        "character_identity_drift",
                        f"Character {character_id} canonical name changed without authority resolution.",
                        {"before": old_name, "after": canonical_name},
                    )
                )
            if old_identity and new_identity and old_identity != new_identity:
                findings.append(
                    _finding(
                        "character_identity_drift",
                        f"Character {character_id} identity changed without authority resolution.",
                        {"before": old_identity, "after": new_identity},
                    )
                )
            old_targets = _motivation_targets(existing.get("active_motivations"))
            new_targets = _motivation_targets(incoming.get("active_motivations"))
            if (
                old_targets
                and new_targets
                and old_targets != new_targets
                and str(raw.get("source_event_id") or "") not in declared_events
            ):
                findings.append(
                    _finding(
                        "character_motivation_target_drift",
                        f"Character {character_id} motivation target changed without a declared event.",
                        {
                            "before": sorted(old_targets),
                            "after": sorted(new_targets),
                            "source_event_id": raw.get("source_event_id"),
                        },
                    )
                )
            findings.extend(
                _model_inference_conflict(
                    existing,
                    incoming,
                    source_tier=source_tier,
                    subject=f"character {character_id}",
                )
            )
        merged = {**copy.deepcopy(existing or {}), **incoming}
        merged["character_id"] = character_id
        merged["canonical_name"] = canonical_name
        merged["aliases"] = _unique_strings([*(existing or {}).get("aliases", []), *aliases])
        merged["source_tier"] = _stronger_source_tier(existing, source_tier)
        state["characters"][character_id] = merged
        for alias in merged["aliases"]:
            alias_owners.setdefault(normalize_entity_alias(alias), character_id)
    return findings


def _apply_relationship_changes(
    state: dict[str, Any],
    value: Any,
    *,
    source_tier: str,
    declared_events: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for raw in _objects(value):
        relationship_id = str(raw.get("relationship_id") or raw.get("id") or "").strip()
        source_id = str(raw.get("source_character_id") or raw.get("source_id") or "").strip()
        target_id = str(raw.get("target_character_id") or raw.get("target_id") or "").strip()
        relation_type = str(raw.get("type") or raw.get("kind") or "").strip()
        existing = state["relationships"].get(relationship_id)
        if not isinstance(existing, dict) and source_id and target_id:
            for existing_id, candidate in state["relationships"].items():
                if not isinstance(candidate, dict):
                    continue
                candidate_pair = (
                    str(candidate.get("source_character_id") or candidate.get("source_id") or ""),
                    str(candidate.get("target_character_id") or candidate.get("target_id") or ""),
                )
                if candidate_pair == (source_id, target_id):
                    relationship_id = str(existing_id)
                    existing = candidate
                    break
        source_id = source_id or str((existing or {}).get("source_character_id") or "")
        target_id = target_id or str((existing or {}).get("target_character_id") or "")
        field = str(raw.get("field") or "").strip()
        if field == "type" and "after" in raw:
            relation_type = str(raw["after"])
        relation_type = relation_type or str((existing or {}).get("type") or (existing or {}).get("kind") or "")
        if not relationship_id or not source_id or not target_id or not relation_type:
            findings.append(_finding("invalid_relationship_change", "Relationship change is incomplete.", raw))
            continue
        event_finding = _required_event_reference_finding(
            raw,
            state=state,
            declared_events=declared_events,
            source_tier=source_tier,
            ledger="relationships",
            current_delta_only=True,
        )
        if event_finding is not None:
            findings.append(event_finding)
        for existing_id, candidate in state["relationships"].items():
            if not isinstance(candidate, dict):
                continue
            pair_matches = (
                str(candidate.get("source_character_id") or candidate.get("source_id") or ""),
                str(candidate.get("target_character_id") or candidate.get("target_id") or ""),
            ) == (source_id, target_id)
            existing_type = str(candidate.get("type") or candidate.get("kind") or "")
            if pair_matches and existing_type and existing_type != relation_type:
                findings.append(
                    _finding(
                        "character_relationship_drift",
                        f"Relationship pair {source_id}/{target_id} changed from {existing_type} to {relation_type}.",
                        {
                            "existing_relationship_id": existing_id,
                            "incoming_relationship_id": relationship_id,
                            "before": existing_type,
                            "after": relation_type,
                        },
                    )
                )
        if isinstance(existing, dict):
            immutable = (
                str(existing.get("source_character_id") or existing.get("source_id") or ""),
                str(existing.get("target_character_id") or existing.get("target_id") or ""),
                str(existing.get("type") or existing.get("kind") or ""),
            )
            if immutable != (source_id, target_id, relation_type):
                findings.append(
                    _finding(
                        "character_relationship_drift",
                        f"Relationship {relationship_id} changed immutable identity fields.",
                        {"before": immutable, "after": (source_id, target_id, relation_type)},
                    )
                )
        incoming = copy.deepcopy(raw)
        if field and "after" in raw:
            if field in (existing or {}) and (existing or {}).get(field) != raw.get("before"):
                findings.append(
                    _finding(
                        "relationship_state_rollback",
                        f"Relationship {relationship_id} field {field} starts stale.",
                        {
                            "relationship_id": relationship_id,
                            "field": field,
                            "expected_before": (existing or {}).get(field),
                            "declared_before": raw.get("before"),
                        },
                    )
                )
            incoming[field] = copy.deepcopy(raw["after"])
        if isinstance(existing, dict):
            findings.extend(
                _model_inference_conflict(
                    existing,
                    incoming,
                    source_tier=source_tier,
                    subject=f"relationship {relationship_id}",
                )
            )
        state["relationships"][relationship_id] = {
            **copy.deepcopy(existing or {}),
            **incoming,
            "relationship_id": relationship_id,
            "source_character_id": source_id,
            "target_character_id": target_id,
            "type": relation_type,
            "source_tier": _stronger_source_tier(existing, source_tier),
        }
    return findings


def _apply_roster_changes(
    state: dict[str, Any],
    value: Any,
    *,
    source_tier: str,
    declared_events: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    current_events = {
        event_id: event
        for event_id, event in declared_events.items()
        if event_id not in (state.get("events") or {})
    }
    for raw in _objects(value):
        roster_id = str(raw.get("roster_id") or raw.get("id") or "").strip()
        existing = state["roster"].get(roster_id)
        transition = reduce_roster_mutation(
            state["roster"],
            raw,
            current_events=current_events,
            require_current_event=not (
                source_tier == "story_project_standard"
                and str(raw.get("operation") or "replace").strip() == "replace"
            ),
        )
        for issue in transition["issues"]:
            code = str(issue.get("code") or ROSTER_INVALID_MUTATION)
            findings.append(
                _finding(
                    (
                        "invalid_roster_change"
                        if code == ROSTER_INVALID_MUTATION
                        else code
                    ),
                    str(issue.get("message") or "Invalid roster mutation."),
                    issue.get("evidence") or {},
                )
            )
        record = transition.get("record")
        roster_id = str(transition.get("roster_id") or "")
        if roster_id and isinstance(record, dict):
            state["roster"][roster_id] = {
                **copy.deepcopy(record),
                "source_tier": _stronger_source_tier(existing, source_tier),
            }
    return findings


def _apply_numeric_changes(
    state: dict[str, Any],
    value: Any,
    *,
    source_tier: str,
    declared_events: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for raw in _objects(value):
        counter_id = str(raw.get("counter_id") or raw.get("id") or "").strip()
        if not counter_id:
            findings.append(_finding("invalid_numeric_change", "Numeric change requires counter_id.", raw))
            continue
        event_finding = _required_event_reference_finding(
            raw,
            state=state,
            declared_events=declared_events,
            source_tier=source_tier,
            ledger="numeric_counters",
        )
        if event_finding is not None:
            findings.append(event_finding)
        existing = state["numeric_counters"].get(counter_id)
        current = (existing or {}).get("current_value", raw.get("previous_value"))
        previous = raw.get("previous_value", raw.get("before"))
        delta = raw.get("delta")
        expected = raw.get("expected_value", raw.get("after"))
        declared = raw.get("declared_value", expected)
        if not all(_number(item) for item in (previous, delta, expected, declared)):
            findings.append(
                _finding(
                    "invalid_numeric_change",
                    f"Counter {counter_id} requires numeric previous, delta, expected, and declared values.",
                    raw,
                )
            )
            continue
        if (
            current is not None
            and previous != current
            and not _incoming_source_supersedes(existing, source_tier)
        ):
            findings.append(
                _finding(
                    "numeric_counter_mismatch",
                    f"Counter {counter_id} starts from stale value.",
                    {
                        "kind": "stale_previous_value",
                        "expected_previous": current,
                        "declared_previous": previous,
                    },
                )
            )
        if _number(previous) and _number(delta) and previous + delta != expected:
            findings.append(
                _finding(
                    "numeric_counter_mismatch",
                    f"Counter {counter_id} arithmetic is inconsistent.",
                    {
                        "kind": "arithmetic_mismatch",
                        "previous": previous,
                        "delta": delta,
                        "expected": expected,
                    },
                )
            )
        if expected != declared:
            findings.append(
                _finding(
                    "numeric_counter_mismatch",
                    f"Counter {counter_id} declared value differs from expected value.",
                    {
                        "kind": "declared_value_mismatch",
                        "expected": expected,
                        "declared": declared,
                    },
                )
            )
        rule = str((existing or {}).get("rule") or raw.get("rule") or "")
        rollback_event = _rollback_event(raw, state, declared_events)
        if (
            rule == "monotonic_non_decreasing"
            and _number(previous)
            and _number(declared)
            and declared < previous
            and rollback_event is None
        ):
            findings.append(
                _finding(
                    "numeric_counter_rollback",
                    f"Counter {counter_id} cannot decrease under monotonic_non_decreasing.",
                    {"previous": previous, "declared": declared},
                )
            )
        minimum = (existing or {}).get("minimum", raw.get("minimum"))
        maximum = (existing or {}).get("maximum", raw.get("maximum"))
        if _number(minimum) and _number(declared) and declared < minimum:
            findings.append(_finding("numeric_counter_out_of_range", f"Counter {counter_id} is below minimum.", raw))
        if _number(maximum) and _number(declared) and declared > maximum:
            findings.append(_finding("numeric_counter_out_of_range", f"Counter {counter_id} exceeds maximum.", raw))
        state["numeric_counters"][counter_id] = {
            **copy.deepcopy(existing or {}),
            **copy.deepcopy(raw),
            "counter_id": counter_id,
            "current_value": declared,
            "source_tier": _stronger_source_tier(existing, source_tier),
        }
    return findings


def _apply_inventory_changes(
    state: dict[str, Any],
    value: Any,
    *,
    source_tier: str,
    declared_events: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for raw in _objects(value):
        owner_id = str(raw.get("owner_id") or "").strip()
        item_id = str(raw.get("item_id") or "").strip()
        if not owner_id or not item_id:
            findings.append(
                _finding(
                    "invalid_inventory_change",
                    "Inventory change requires stable owner_id and item_id values.",
                    raw,
                )
            )
            continue
        key = str(raw.get("inventory_id") or f"{owner_id}:{item_id}").strip()
        event_finding = _required_event_reference_finding(
            raw,
            state=state,
            declared_events=declared_events,
            source_tier=source_tier,
            ledger="inventory",
        )
        if event_finding is not None:
            findings.append(event_finding)
        previous = raw.get("previous_quantity", raw.get("before"))
        delta = raw.get("delta")
        declared = raw.get("declared_quantity", raw.get("after"))
        if not all(_number(item) for item in (previous, delta, declared)):
            findings.append(
                _finding(
                    "invalid_inventory_change",
                    f"Inventory {key} requires numeric previous, delta, and declared quantities.",
                    raw,
                )
            )
            continue
        existing = state["inventory"].get(key)
        current = (existing or {}).get("quantity", previous)
        if current is not None and previous != current:
            findings.append(_finding("inventory_state_rollback", f"Inventory {key} starts stale.", raw))
        if _number(previous) and _number(delta) and previous + delta != declared:
            findings.append(_finding("inventory_arithmetic_mismatch", f"Inventory {key} arithmetic is inconsistent.", raw))
        if _number(declared) and declared < 0:
            findings.append(_finding("inventory_negative_quantity", f"Inventory {key} cannot be negative.", raw))
        state["inventory"][key] = {
            **copy.deepcopy(existing or {}),
            **copy.deepcopy(raw),
            "inventory_id": key,
            "owner_id": owner_id,
            "item_id": item_id,
            "quantity": declared,
            "source_tier": _stronger_source_tier(existing, source_tier),
        }
    return findings


def _apply_location_changes(
    state: dict[str, Any],
    value: Any,
    *,
    source_tier: str,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for raw in _objects(value):
        entity_id = str(raw.get("entity_id") or "").strip()
        before = raw.get("before", raw.get("from_location"))
        after = raw.get("after", raw.get("to_location"))
        existing = state["locations"].get(entity_id)
        current = (existing or {}).get("location_id", before)
        if not entity_id or after in (None, ""):
            findings.append(_finding("invalid_location_change", "Location change is incomplete.", raw))
            continue
        if (
            current is not None
            and before != current
            and not _incoming_source_supersedes(existing, source_tier)
        ):
            findings.append(_finding("location_state_rollback", f"Location {entity_id} starts stale.", raw))
        state["locations"][entity_id] = {
            **copy.deepcopy(existing or {}),
            **copy.deepcopy(raw),
            "entity_id": entity_id,
            "location_id": after,
            "source_tier": _stronger_source_tier(existing, source_tier),
        }
    return findings


def _apply_events(
    state: dict[str, Any],
    value: Any,
    *,
    source_tier: str,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for raw in _objects(value):
        event_id = str(raw.get("event_id") or raw.get("id") or "").strip()
        if not event_id:
            findings.append(_finding("invalid_authority_event", "Authority event requires event_id.", raw))
            continue
        existing = state["events"].get(event_id)
        existing_payload = (
            {
                key: value
                for key, value in existing.items()
                if key != "source_tier"
            }
            if isinstance(existing, dict)
            else existing
        )
        raw_payload = {
            key: value for key, value in raw.items() if key != "source_tier"
        }
        if existing is not None and existing_payload != raw_payload:
            findings.append(
                _finding(
                    "duplicate_scene_event",
                    f"Event {event_id} was redefined.",
                    {"before": existing, "after": raw},
                )
            )
        state["events"][event_id] = {
            **copy.deepcopy(raw),
            "source_tier": _stronger_source_tier(existing, source_tier),
        }
    return findings


def _full_state_delta(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_tier": "story_project_standard",
        "character_changes": list(state["characters"].values()),
        "relationship_changes": list(state["relationships"].values()),
        "roster_changes": [
            {
                **copy.deepcopy(item),
                "operation": "replace",
                "unresolved_before": 0,
                "unresolved_count": (
                    item.get("unresolved_count")
                    if _nonnegative_int(item.get("unresolved_count"))
                    else 0
                ),
                "delta": (
                    len(_roster_members(item))
                    + (
                        int(item.get("unresolved_count"))
                        if _nonnegative_int(item.get("unresolved_count"))
                        else 0
                    )
                ),
            }
            for item in state["roster"].values()
        ],
        "numeric_changes": [
            {
                **copy.deepcopy(item),
                "previous_value": item.get("current_value"),
                "delta": 0,
                "expected_value": item.get("current_value"),
                "declared_value": item.get("current_value"),
            }
            for item in state["numeric_counters"].values()
        ],
        "inventory_changes": [
            {
                **copy.deepcopy(item),
                "previous_quantity": item.get("quantity"),
                "delta": 0,
                "declared_quantity": item.get("quantity"),
            }
            for item in state["inventory"].values()
        ],
        "location_changes": [
            {
                **copy.deepcopy(item),
                "before": item.get("location_id"),
                "after": item.get("location_id"),
            }
            for item in state["locations"].values()
        ],
        "events": list(state["events"].values()),
    }


def _character_alias_owners(characters: dict[str, Any]) -> dict[str, str]:
    owners: dict[str, str] = {}
    for key, record in characters.items():
        if not isinstance(record, dict):
            continue
        character_id = str(record.get("character_id") or record.get("id") or key)
        names = [
            record.get("canonical_name"),
            record.get("name"),
            *(record.get("aliases") or []),
        ]
        for name in names:
            normalized = normalize_entity_alias(str(name or ""))
            if normalized:
                owners.setdefault(normalized, character_id)
    return owners


def _roster_members(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(item["member_id"]): copy.deepcopy(item)
        for item in value.get("members") or []
        if isinstance(item, dict) and str(item.get("member_id") or "")
    }


def _objects(value: Any) -> list[dict[str, Any]]:
    return [copy.deepcopy(item) for item in value or [] if isinstance(item, dict)] if isinstance(value, list) else []


def _unique_strings(value: Any) -> list[str]:
    result: list[str] = []
    for item in value if isinstance(value, list) else []:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonnegative_int(value: Any) -> bool:
    return _integer(value) and value >= 0


def _source_tier(delta: dict[str, Any]) -> str:
    value = str(delta.get("source_tier") or "chapter_event").strip()
    if value not in SOURCE_PRECEDENCE:
        return "model_inference"
    return value


def _motivation_targets(value: Any) -> set[str]:
    targets: set[str] = set()
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            target = item.get("target_id") or item.get("target")
        else:
            target = item
        if str(target or "").strip():
            targets.add(str(target).strip())
    return targets


def _model_inference_conflict(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    *,
    source_tier: str,
    subject: str,
) -> list[dict[str, Any]]:
    existing_tier = str(existing.get("source_tier") or "")
    if source_tier != "model_inference" or existing_tier not in SOURCE_PRECEDENCE[:-1]:
        return []
    ignored = {
        "id",
        "character_id",
        "relationship_id",
        "field",
        "before",
        "after",
        "scene_index",
        "source_tier",
        "source_event_id",
    }
    changed = {
        key: {"before": existing.get(key), "after": value}
        for key, value in incoming.items()
        if key not in ignored and key in existing and existing.get(key) != value
    }
    if not changed:
        return []
    return [
        _finding(
            "source_precedence_conflict",
            f"Model inference cannot overwrite higher-authority {subject}.",
            {
                "existing_source_tier": existing_tier,
                "incoming_source_tier": source_tier,
                "changed": changed,
            },
        )
    ]


def _rollback_event(
    raw: dict[str, Any],
    state: dict[str, Any],
    declared_events: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    event_id = str(raw.get("source_event_id") or "").strip()
    event = declared_events.get(event_id) or state.get("events", {}).get(event_id)
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type") or "").casefold()
    if any(token in event_type for token in ("purif", "cleanse", "reset", "consume", "restore")):
        return event
    return None


def _stronger_source_tier(existing: Any, incoming: str) -> str:
    existing_tier = (
        str(existing.get("source_tier") or "")
        if isinstance(existing, dict)
        else ""
    )
    if existing_tier not in SOURCE_PRECEDENCE:
        return incoming
    return min((existing_tier, incoming), key=SOURCE_PRECEDENCE.index)


def _incoming_source_supersedes(existing: Any, incoming: str) -> bool:
    existing_tier = (
        str(existing.get("source_tier") or "")
        if isinstance(existing, dict)
        else ""
    )
    if (
        existing_tier not in SOURCE_PRECEDENCE
        or incoming not in SOURCE_PRECEDENCE
    ):
        return False
    return SOURCE_PRECEDENCE.index(incoming) < SOURCE_PRECEDENCE.index(
        existing_tier
    )


def _authority_problem(finding: dict[str, Any]) -> dict[str, Any]:
    evidence = finding.get("evidence")
    evidence_text = json.dumps(
        evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return {
        "code": str(finding.get("code") or "authoritative_state_conflict"),
        "message": str(
            finding.get("message")
            or "Authoritative state validation reported a conflict."
        ),
        "validator": "authoritative_state",
        "severity": "critical",
        "blocking": True,
        "category": "blocking",
        "repair_hint": (
            "Regenerate the affected Scene with a delta whose before-state and "
            "source event match the authoritative ledger."
        ),
        "repair_action": "manual_review",
        "repair_parameters": {},
        "evidence": [
            {
                "kind": "authoritative_state_finding",
                "value": evidence_text or "{}",
            }
        ],
    }


def _normalize_validation_problem(
    problem: dict[str, Any],
    *,
    validation_ok: bool,
) -> dict[str, Any]:
    normalized = copy.deepcopy(problem)
    blocking = bool(normalized.get("blocking", not validation_ok))
    severity = str(
        normalized.get("severity") or ("critical" if blocking else "medium")
    )
    if severity not in {"critical", "high", "medium", "low"}:
        severity = "critical" if blocking else "medium"
    action = str(normalized.get("repair_action") or "manual_review")
    allowed_actions = {
        "seed_conflict_scene",
        "expand_scene",
        "add_conflict_signal",
        "remove_forbidden_term",
        "add_required_term",
        "anchor_known_location",
        "insert_opening_bridge",
        "rewrite_spatial_transition",
        "anchor_last_scene_state",
        "repair_character_position",
        "add_transition_event",
        "flag_unknown_location",
        "add_character_location",
        "rewrite_inactive_character_action",
        "correct_chapter_index",
        "manual_review",
    }
    if action not in allowed_actions:
        action = "manual_review"
    raw_evidence = normalized.get("evidence")
    evidence = [
        {
            "kind": str(item.get("kind") or "validation_evidence"),
            "value": str(item.get("value") or item),
        }
        for item in raw_evidence or []
        if isinstance(item, dict)
    ]
    normalized.update(
        {
            "code": str(
                normalized.get("code") or "custom_validation_problem"
            ),
            "message": str(
                normalized.get("message")
                or "Custom validator reported a problem."
            ),
            "validator": str(normalized.get("validator") or "custom"),
            "severity": severity,
            "blocking": blocking,
            "category": "blocking" if blocking else "warning",
            "repair_hint": str(
                normalized.get("repair_hint")
                or "Inspect the validation evidence before commit."
            ),
            "repair_action": action,
            "repair_parameters": (
                copy.deepcopy(normalized.get("repair_parameters"))
                if isinstance(normalized.get("repair_parameters"), dict)
                else {}
            ),
            "evidence": evidence,
        }
    )
    return normalized


def _finding(code: str, message: str, evidence: Any) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "blocking": True,
        "evidence": copy.deepcopy(evidence),
    }


__all__ = [
    "AUTHORITATIVE_STATE_SCHEMA_VERSION",
    "AuthoritativeStateError",
    "SOURCE_PRECEDENCE",
    "adapt_scene_deltas_to_authoritative_delta",
    "empty_authoritative_state",
    "merge_authoritative_report_into_validation",
    "normalize_entity_alias",
    "require_authoritative_state_delta",
    "seed_authoritative_state_from_snapshot",
    "validate_authoritative_state",
    "validate_authoritative_state_delta",
]
