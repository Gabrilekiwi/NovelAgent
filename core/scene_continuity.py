from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json
import re
from typing import Any, Iterable

from core.state.prose_state_alignment import validate_roster_count_claims
from core.state.roster import (
    ROSTER_INVALID_MUTATION,
    project_roster_for_generation,
    reduce_roster_mutation,
)


SCENE_CONTINUITY_SCHEMA_VERSION = "1.0"
SCENE_STATE_GENERATION_PROJECTION_POLICY = "scene_state_generation_v1"
_DELTA_KINDS = ("characters", "relationships", "rosters", "locations", "inventory", "counters")
_COMPLETED_EVENT_SUMMARY_LIMIT = 24
_OPEN_ACTION_SUMMARY_LIMIT = 12


class SceneBoundaryValidationError(ValueError):
    def __init__(self, report: dict[str, Any]) -> None:
        self.report = copy.deepcopy(report)
        findings = [item for item in report.get("findings") or [] if isinstance(item, dict)]
        counts = Counter(str(item.get("code") or "unknown") for item in findings)
        codes = ", ".join(
            f"{code} x{count}" if count > 1 else code
            for code, count in counts.items()
        )
        first = findings[0] if findings else {}
        evidence = json.dumps(
            first.get("evidence") or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(evidence) > 400:
            evidence = evidence[:397] + "..."
        detail = (
            f"; first={first.get('code')}: {first.get('message')}; evidence={evidence}"
            if first
            else ""
        )
        super().__init__(
            f"scene boundary validation failed: {codes or 'unknown'}{detail}"
        )


def scene_delta_response_schema() -> dict[str, list[dict[str, Any]]]:
    """Return the exact prompt-facing delta shape consumed by this module."""

    return {
        "characters": [
            {
                "character_id": "stable character id",
                "field": "one changed character state field",
                "before": "exact current value or null when absent",
                "after": "new value",
                "reason": "why this scene changed the field",
            }
        ],
        "relationships": [
            {
                "source_id": "stable source character id",
                "target_id": "stable target character id",
                "field": "one changed relationship field",
                "before": "exact current value or null when absent",
                "after": "new value",
                "reason": "why this scene changed the relationship",
                "source_event_id": "event id declared by this scene",
            }
        ],
        "rosters": [
            {
                "roster_id": "stable roster id",
                "name": "canonical name",
                "aliases": ["exact alias"],
                "operation": "join|leave|dead|missing|resolve|replace",
                "member_ids": ["stable member id"],
                "members": [
                    {
                        "member_id": "same stable member id",
                        "character_id": None,
                        "descriptor": "stable descriptor",
                        "status": "active|left|dead|missing",
                    }
                ],
                "delta": 1,
                "declared_count": 1,
                "unresolved_before": None,
                "unresolved_delta": None,
                "unresolved_count": None,
                "reason_event_id": "event id declared by this Scene",
            }
        ],
        "locations": [
            {
                "entity_id": "stable character or entity id",
                "before": "exact current location or null when absent",
                "after": "new location",
                "reason": "how the location changed in this scene",
            }
        ],
        "inventory": [
            {
                "owner_id": "stable owner id",
                "item_id": "stable item id",
                "before": 2,
                "delta": -1,
                "after": 1,
                "reason": "why the quantity changed",
                "source_event_id": "source event id",
            }
        ],
        "counters": [
            {
                "counter_id": "stable counter id",
                "before": 6,
                "delta": 0,
                "after": 6,
                "reason": "why the counter changed",
                "source_event_id": "source event id",
            }
        ],
    }


def empty_scene_state() -> dict[str, Any]:
    return {
        "schema_version": SCENE_CONTINUITY_SCHEMA_VERSION,
        "characters": {},
        "relationships": {},
        "rosters": {},
        "locations": {},
        "inventories": {},
        "counters": {},
        "completed_event_ids": [],
        "completed_events": [],
        "current_location": "",
        "characters_present": [],
        "open_action": "",
        "open_actions": [],
    }


def validate_scene_transition(
    *,
    scene_index: int,
    state_before: dict[str, Any],
    events: Iterable[dict[str, Any]],
    deltas: dict[str, Any] | None,
    prose: str = "",
    required_event_ids: Iterable[str] = (),
    forbidden_event_ids: Iterable[str] = (),
    planned_events: Iterable[dict[str, Any]] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    before = _normalize_state(state_before)
    normalized_events = [_normalize_event(item) for item in events if isinstance(item, dict)]
    normalized_deltas = _normalize_deltas(deltas)
    findings: list[dict[str, Any]] = []
    required = {str(item) for item in required_event_ids if str(item)}
    forbidden = {str(item) for item in forbidden_event_ids if str(item)}
    planned_by_id = {
        str(item.get("event_id")): _normalize_event(item)
        for item in planned_events
        if isinstance(item, dict) and str(item.get("event_id") or "")
    }
    declared_ids = [str(item["event_id"]) for item in normalized_events]
    completed_ids = {str(item) for item in before["completed_event_ids"]}
    # Exact event ids remain authoritative for the complete local history.
    # Semantic signatures are only a recent-window heuristic: recurring real
    # actions may legitimately share actors, objects, type, and location.
    prior_events = [
        item
        for item in before["completed_events"][-_COMPLETED_EVENT_SUMMARY_LIMIT:]
        if isinstance(item, dict)
    ]
    prior_open_actions = [
        item for item in before["open_actions"] if isinstance(item, dict)
    ]
    seen_event_ids: set[str] = set()
    seen_event_signatures: set[str] = set()

    for event_id in sorted(required - set(declared_ids)):
        findings.append(
            _finding(
                "missing_planned_event",
                f"Scene {scene_index} omitted planned event {event_id}.",
                {"event_id": event_id},
            )
        )
    for event in normalized_events:
        event_id = str(event["event_id"])
        signature = _event_signature(event)
        findings.extend(
            _validate_event_location(
                scene_index=scene_index,
                state_before=before,
                event=event,
                location_deltas=normalized_deltas["locations"],
            )
        )
        if event_id in seen_event_ids:
            findings.append(
                _finding(
                    "duplicate_scene_event_id",
                    f"Scene {scene_index} declared event id {event_id} more than once.",
                    {"event_id": event_id},
                )
            )
        seen_event_ids.add(event_id)
        if (
            str(event.get("status") or "") == "completed"
            and _event_has_identity_anchor(event)
            and signature in seen_event_signatures
        ):
            findings.append(
                _finding(
                    "duplicate_scene_event",
                    f"Scene {scene_index} repeated the same completed event within the scene.",
                    {"event_id": event_id, "signature": signature},
                )
            )
        seen_event_signatures.add(signature)
        if required and event_id not in required:
            findings.append(
                _finding(
                    "unplanned_scene_event",
                    f"Scene {scene_index} declared event {event_id} outside its planned scope.",
                    {"event_id": event_id, "required_event_ids": sorted(required)},
                )
            )
        planned = planned_by_id.get(event_id)
        if planned is not None:
            if event["type"] != planned["type"]:
                findings.append(
                    _finding(
                        "planned_event_type_mismatch",
                        f"Scene {scene_index} changed the planned type for event {event_id}.",
                        {
                            "event_id": event_id,
                            "planned_type": planned["type"],
                            "declared_type": event["type"],
                        },
                    )
                )
            for field in ("subjects", "objects", "location"):
                planned_value = planned[field]
                declared_value = event[field]
                if planned_value and planned_value != declared_value:
                    findings.append(
                        _finding(
                            "planned_event_scope_mismatch",
                            f"Scene {scene_index} changed planned {field} for event {event_id}.",
                            {
                                "event_id": event_id,
                                "field": field,
                                "planned": planned_value,
                                "declared": declared_value,
                            },
                        )
                    )
        if event_id in forbidden or event_id in completed_ids:
            findings.append(
                _finding(
                    "repeated_completed_event_id",
                    f"Scene {scene_index} repeated completed event id {event_id}.",
                    {"event_id": event_id},
                )
            )
        if str(event.get("status") or "") == "completed":
            for prior in prior_events:
                if (
                    not _is_plan_bookkeeping_event(event)
                    and not _is_plan_bookkeeping_event(prior)
                    and _event_has_identity_anchor(event)
                    and _event_has_identity_anchor(prior)
                    and _event_signature(prior) == _event_signature(event)
                ):
                    findings.append(
                        _finding(
                            "duplicate_scene_event",
                            f"Scene {scene_index} repeated a completed event under a new id.",
                            {
                                "event_id": event_id,
                                "prior_event_id": prior.get("event_id"),
                                "signature": _event_signature(event),
                            },
                        )
                    )
                    break
        if str(event.get("status") or "") in {"started", "ongoing"}:
            action_signature = _event_action_signature(event)
            for prior in prior_open_actions:
                if (
                    str(prior.get("event_id") or "") != event_id
                    and _event_has_identity_anchor(event)
                    and _event_has_identity_anchor(prior)
                    and _event_action_signature(prior) == action_signature
                ):
                    findings.append(
                        _finding(
                            "open_action_restarted",
                            f"Scene {scene_index} restarted an already open action under a new id.",
                            {
                                "event_id": event_id,
                                "prior_event_id": prior.get("event_id"),
                                "signature": signature,
                            },
                        )
                    )
                    break

    after = copy.deepcopy(before)
    findings.extend(_apply_character_deltas(after, normalized_deltas["characters"]))
    findings.extend(
        _apply_relationship_deltas(
            after,
            normalized_deltas["relationships"],
            current_event_ids=set(declared_ids),
            scene_index=scene_index,
        )
    )
    findings.extend(
        _apply_roster_deltas(
            after,
            normalized_deltas["rosters"],
            current_events={
                str(event["event_id"]): event
                for event in normalized_events
            },
        )
    )
    findings.extend(_apply_location_deltas(after, normalized_deltas["locations"]))
    findings.extend(_apply_numeric_deltas(after, normalized_deltas["inventory"], inventory=True))
    findings.extend(_apply_numeric_deltas(after, normalized_deltas["counters"], inventory=False))
    findings.extend(
        validate_roster_count_claims(
            chapter_text=prose,
            state_before=before,
            state_after=after,
            roster_changes=normalized_deltas["rosters"],
        )
    )

    for event in normalized_events:
        event_id = str(event["event_id"])
        status = str(event.get("status") or "")
        signature = _event_signature(event)
        if status in {"started", "ongoing"}:
            after["open_actions"] = [
                item
                for item in after["open_actions"]
                if str(item.get("event_id") or "") != event_id
            ]
            after["open_actions"].append(event)
        elif status in {
            "completed",
            "resolved",
            "interrupted",
            "cancelled",
            "canceled",
            "failed",
        }:
            after["open_actions"] = [
                item
                for item in after["open_actions"]
                if (
                    str(item.get("event_id") or "") != event_id
                    and _event_action_signature(item)
                    != _event_action_signature(event)
                )
            ]
        if status == "completed":
            if event_id not in after["completed_event_ids"]:
                after["completed_event_ids"].append(event_id)
            if not any(
                _event_signature(item) == signature
                for item in after["completed_events"]
            ):
                after["completed_events"].append(event)

    locations = [
        str(event.get("location") or "").strip()
        for event in normalized_events
        if str(event.get("location") or "").strip()
    ]
    if locations:
        after["current_location"] = locations[-1]
        after["characters_present"] = sorted(
            {
                str(subject)
                for event in normalized_events
                if str(event.get("location") or "").strip() == locations[-1]
                for subject in event.get("subjects") or []
                if str(subject).strip()
            }
        )
    after["open_action"] = (
        str(after["open_actions"][0].get("event_id") or "")
        if after["open_actions"]
        else ""
    )

    report = {
        "schema_version": SCENE_CONTINUITY_SCHEMA_VERSION,
        "scene_index": int(scene_index),
        "accepted": not findings,
        "findings": findings,
        "required_event_ids": sorted(required),
        "forbidden_event_ids": sorted(forbidden),
        "declared_event_ids": declared_ids,
        "planned_event_ids": sorted(planned_by_id),
        "state_before_sha256": scene_state_hash(before),
        "state_after_sha256": scene_state_hash(after),
    }
    return report, after


def require_scene_transition(report: dict[str, Any]) -> dict[str, Any]:
    if not bool(report.get("accepted")):
        raise SceneBoundaryValidationError(report)
    return report


def scene_state_summary(state: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_state(state)
    completed_event_ids = list(normalized["completed_event_ids"])
    return {
        "schema_version": normalized["schema_version"],
        "characters": copy.deepcopy(normalized["characters"]),
        "relationships": copy.deepcopy(normalized["relationships"]),
        "rosters": copy.deepcopy(normalized["rosters"]),
        "locations": copy.deepcopy(normalized["locations"]),
        "inventories": copy.deepcopy(normalized["inventories"]),
        "counters": copy.deepcopy(normalized["counters"]),
        "completed_event_ids": completed_event_ids[-_COMPLETED_EVENT_SUMMARY_LIMIT:],
        "completed_event_ids_count": len(completed_event_ids),
        "completed_event_ids_sha256": _history_hash(completed_event_ids),
        "completed_event_ids_truncated": (
            len(completed_event_ids) > _COMPLETED_EVENT_SUMMARY_LIMIT
        ),
        "completed_events": copy.deepcopy(
            normalized["completed_events"][-_COMPLETED_EVENT_SUMMARY_LIMIT:]
        ),
        "current_location": normalized["current_location"],
        "characters_present": list(normalized["characters_present"]),
        "open_action": normalized["open_action"],
        "open_actions": copy.deepcopy(
            normalized["open_actions"][-_OPEN_ACTION_SUMMARY_LIMIT:]
        ),
    }


def scene_state_generation_projection(state: dict[str, Any]) -> dict[str, Any]:
    """Return a prompt-only Scene state while retaining full local audit state."""

    normalized = _normalize_state(state)
    projected = scene_state_summary(normalized)
    projected["rosters"] = {
        roster_id: project_roster_for_generation(record)
        for roster_id, record in projected["rosters"].items()
        if isinstance(record, dict)
    }
    projected["context_projection"] = {
        "policy": SCENE_STATE_GENERATION_PROJECTION_POLICY,
        "source_kind": "normalized_scene_state",
        "source_sha256": scene_state_hash(normalized),
        "projection_sha256": _state_hash(projected),
    }
    return projected


def scene_state_hash(state: dict[str, Any] | None) -> str:
    """Hash the complete normalized Scene state used by boundary validation."""

    return _state_hash(_normalize_state(state))


def normalize_scene_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """Return the complete internal Scene state without prompt-size truncation."""

    return _normalize_state(state)


def _normalize_state(value: dict[str, Any] | None) -> dict[str, Any]:
    base = empty_scene_state()
    if not isinstance(value, dict):
        return base
    for key in ("characters", "relationships", "rosters", "locations", "inventories", "counters"):
        if isinstance(value.get(key), dict):
            base[key] = copy.deepcopy(value[key])
    for key in (
        "completed_event_ids",
        "completed_events",
        "characters_present",
        "open_actions",
    ):
        if isinstance(value.get(key), list):
            base[key] = copy.deepcopy(value[key])
    for key in ("current_location", "open_action"):
        if isinstance(value.get(key), str):
            base[key] = str(value[key])
    return base


def _validate_event_location(
    *,
    scene_index: int,
    state_before: dict[str, Any],
    event: dict[str, Any],
    location_deltas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    event_location = str(event.get("location") or "").strip()
    if not event_location:
        return []
    transitions = {
        str(item.get("entity_id") or "").strip(): item
        for item in location_deltas
        if isinstance(item, dict) and str(item.get("entity_id") or "").strip()
    }
    findings: list[dict[str, Any]] = []
    for entity_id in _unique_strings(
        [*(event.get("subjects") or []), *(event.get("objects") or [])]
    ):
        current = state_before["locations"].get(entity_id)
        if current in (None, "") or current == event_location:
            continue
        transition = transitions.get(entity_id)
        if (
            isinstance(transition, dict)
            and transition.get("before") == current
            and transition.get("after") == event_location
        ):
            continue
        findings.append(
            _finding(
                "scene_boundary_state_mismatch",
                f"Scene {scene_index} places {entity_id} at {event_location} without a matching location delta.",
                {
                    "event_id": event.get("event_id"),
                    "entity_id": entity_id,
                    "expected_location": current,
                    "event_location": event_location,
                },
            )
        )
    return findings


def _normalize_event(value: dict[str, Any]) -> dict[str, Any]:
    event_id = str(value.get("event_id") or "").strip()
    event_type = str(value.get("type") or "").strip()
    status = str(value.get("status") or "completed").strip()
    if not event_id or not event_type:
        raise ValueError("scene events require non-empty event_id and type")
    return {
        "event_id": event_id,
        "type": event_type,
        "subjects": _string_list(value.get("subjects")),
        "objects": _string_list(value.get("objects")),
        "location": str(value.get("location") or ""),
        "status": status,
    }


def _normalize_deltas(value: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    source = value if isinstance(value, dict) else {}
    return {
        key: [copy.deepcopy(item) for item in source.get(key) or [] if isinstance(item, dict)]
        for key in _DELTA_KINDS
    }


def _apply_character_deltas(
    state: dict[str, Any],
    deltas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for delta in deltas:
        character_id = str(delta.get("character_id") or "").strip()
        field = str(delta.get("field") or "").strip()
        if not character_id or not field or "after" not in delta:
            findings.append(_finding("invalid_character_delta", "Character delta is incomplete.", delta))
            continue
        fields = state["characters"].setdefault(character_id, {})
        findings.extend(_check_before_value("character_state_rollback", fields, field, delta))
        fields[field] = copy.deepcopy(delta["after"])
    return findings


def _apply_relationship_deltas(
    state: dict[str, Any],
    deltas: list[dict[str, Any]],
    *,
    current_event_ids: set[str],
    scene_index: int,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for delta in deltas:
        source_id = str(delta.get("source_id") or "").strip()
        target_id = str(delta.get("target_id") or "").strip()
        field = str(delta.get("field") or "status").strip()
        source_event_id = str(
            delta.get("source_event_id")
            or delta.get("reason_event_id")
            or ""
        ).strip()
        if not source_id or not target_id or "after" not in delta:
            findings.append(_finding("invalid_relationship_delta", "Relationship delta is incomplete.", delta))
            continue
        if not source_event_id:
            findings.append(
                _finding(
                    "missing_authority_event_reference",
                    "Relationship delta requires a source event declared by the same Scene.",
                    {
                        "ledger": "relationships",
                        "scene_index": int(scene_index),
                        "source_id": source_id,
                        "target_id": target_id,
                        "field": field,
                        "source_event_id": "",
                    },
                )
            )
        elif source_event_id not in current_event_ids:
            findings.append(
                _finding(
                    "invalid_authority_event_reference",
                    "Relationship delta references an event outside the current Scene.",
                    {
                        "ledger": "relationships",
                        "scene_index": int(scene_index),
                        "source_id": source_id,
                        "target_id": target_id,
                        "field": field,
                        "source_event_id": source_event_id,
                        "current_event_ids": sorted(current_event_ids),
                    },
                )
            )
        relation_key = f"{source_id}->{target_id}"
        fields = state["relationships"].setdefault(relation_key, {})
        findings.extend(_check_before_value("relationship_state_rollback", fields, field, delta))
        fields[field] = copy.deepcopy(delta["after"])
    return findings


def _apply_roster_deltas(
    state: dict[str, Any],
    deltas: list[dict[str, Any]],
    *,
    current_events: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for delta in deltas:
        transition = reduce_roster_mutation(
            state["rosters"],
            delta,
            current_events=current_events,
            require_current_event=True,
        )
        for issue in transition["issues"]:
            code = str(issue.get("code") or ROSTER_INVALID_MUTATION)
            findings.append(
                _finding(
                    (
                        "invalid_roster_delta"
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
            state["rosters"][roster_id] = copy.deepcopy(record)
    return findings


def _apply_location_deltas(
    state: dict[str, Any],
    deltas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for delta in deltas:
        entity_id = str(delta.get("entity_id") or "").strip()
        if not entity_id or "after" not in delta:
            findings.append(_finding("invalid_location_delta", "Location delta is incomplete.", delta))
            continue
        current = state["locations"].get(entity_id)
        declared_before = delta.get("before")
        if current is not None and declared_before != current:
            findings.append(
                _finding(
                    "location_state_rollback",
                    f"Location transition for {entity_id} starts from stale state.",
                    {"entity_id": entity_id, "expected_before": current, "declared_before": declared_before},
                )
            )
        state["locations"][entity_id] = copy.deepcopy(delta["after"])
    return findings


def _apply_numeric_deltas(
    state: dict[str, Any],
    deltas: list[dict[str, Any]],
    *,
    inventory: bool,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    store_name = "inventories" if inventory else "counters"
    error_code = "inventory_state_rollback" if inventory else "counter_state_rollback"
    for item in deltas:
        if inventory:
            owner_id = str(item.get("owner_id") or "").strip()
            item_id = str(item.get("item_id") or "").strip()
            key = f"{owner_id}:{item_id}" if owner_id and item_id else ""
        else:
            key = str(item.get("counter_id") or "").strip()
        before = item.get("before")
        after = item.get("after")
        change = item.get("delta")
        if (
            not key
            or not _is_number(before)
            or not _is_number(after)
            or not _is_number(change)
        ):
            findings.append(_finding(f"invalid_{store_name}_delta", f"{store_name} delta is incomplete.", item))
            continue
        current = state[store_name].get(key)
        if current is not None and current != before:
            findings.append(
                _finding(
                    error_code,
                    f"{store_name} transition for {key} starts from stale state.",
                    {"key": key, "expected_before": current, "declared_before": before},
                )
            )
        if before + change != after:
            findings.append(
                _finding(
                    f"{store_name}_delta_arithmetic_mismatch",
                    f"{store_name} transition for {key} has inconsistent arithmetic.",
                    {"key": key, "before": before, "delta": change, "after": after},
                )
            )
        state[store_name][key] = after
    return findings


def _check_before_value(
    code: str,
    current: dict[str, Any],
    field: str,
    delta: dict[str, Any],
) -> list[dict[str, Any]]:
    if field not in current or current[field] == delta.get("before"):
        return []
    return [
        _finding(
            code,
            f"Delta for {field} starts from stale state.",
            {
                "field": field,
                "expected_before": current[field],
                "declared_before": delta.get("before"),
            },
        )
    ]


def _event_signature(event: dict[str, Any]) -> str:
    payload = {
        "type": str(event.get("type") or ""),
        "subjects": sorted(_string_list(event.get("subjects"))),
        "objects": sorted(_string_list(event.get("objects"))),
        "location": str(event.get("location") or ""),
        "status": str(event.get("status") or ""),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_action_signature(event: dict[str, Any]) -> str:
    payload = {
        "type": str(event.get("type") or ""),
        "subjects": sorted(_string_list(event.get("subjects"))),
        "objects": sorted(_string_list(event.get("objects"))),
        "location": str(event.get("location") or ""),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _is_plan_bookkeeping_event(event: dict[str, Any]) -> bool:
    return bool(
        re.fullmatch(
            r"required_beat_\d+_completed",
            str(event.get("type") or ""),
            flags=re.IGNORECASE,
        )
    )


def _event_has_identity_anchor(event: dict[str, Any]) -> bool:
    return bool(
        _string_list(event.get("subjects"))
        or _string_list(event.get("objects"))
        or str(event.get("location") or "")
    )


def _state_hash(state: dict[str, Any]) -> str:
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _history_hash(value: list[Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _finding(code: str, message: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "blocking": True,
        "evidence": copy.deepcopy(evidence),
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _unique_strings(value: Any) -> list[str]:
    return list(dict.fromkeys(_string_list(value)))


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


__all__ = [
    "SCENE_CONTINUITY_SCHEMA_VERSION",
    "SCENE_STATE_GENERATION_PROJECTION_POLICY",
    "SceneBoundaryValidationError",
    "empty_scene_state",
    "normalize_scene_state",
    "require_scene_transition",
    "scene_delta_response_schema",
    "scene_state_generation_projection",
    "scene_state_hash",
    "scene_state_summary",
    "validate_scene_transition",
]
