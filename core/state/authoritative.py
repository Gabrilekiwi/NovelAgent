from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any


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
    del chapter_text
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
    findings.extend(_apply_relationship_changes(after, delta.get("relationship_changes"), source_tier=source_tier))
    findings.extend(_apply_roster_changes(after, delta.get("roster_changes"), source_tier=source_tier))
    findings.extend(
        _apply_numeric_changes(
            after,
            delta.get("numeric_changes"),
            source_tier=source_tier,
            declared_events=declared_events,
        )
    )
    findings.extend(_apply_inventory_changes(after, delta.get("inventory_changes"), source_tier=source_tier))
    findings.extend(_apply_location_changes(after, delta.get("location_changes"), source_tier=source_tier))
    findings.extend(_apply_events(after, delta.get("events"), source_tier=source_tier))

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


def normalize_entity_alias(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


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
                candidate_pair = {
                    str(candidate.get("source_character_id") or candidate.get("source_id") or ""),
                    str(candidate.get("target_character_id") or candidate.get("target_id") or ""),
                }
                if candidate_pair == {source_id, target_id}:
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
        for existing_id, existing in state["relationships"].items():
            if not isinstance(existing, dict):
                continue
            pair_matches = {
                str(existing.get("source_character_id") or existing.get("source_id") or ""),
                str(existing.get("target_character_id") or existing.get("target_id") or ""),
            } == {source_id, target_id}
            existing_type = str(existing.get("type") or existing.get("kind") or "")
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
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for raw in _objects(value):
        roster_id = str(raw.get("roster_id") or raw.get("id") or "").strip()
        operation = str(raw.get("operation") or "replace").strip()
        if not roster_id:
            findings.append(_finding("invalid_roster_change", "Roster change requires roster_id.", raw))
            continue
        existing = state["roster"].get(roster_id)
        existing_members = _roster_members(existing)
        declared_member_ids = _unique_strings(raw.get("member_ids") or [])
        members = [copy.deepcopy(item) for item in raw.get("members") or [] if isinstance(item, dict)]
        member_ids = _unique_strings(
            [str(item.get("member_id") or "") for item in members if str(item.get("member_id") or "")]
        )
        if operation in {"join", "replace"} and declared_member_ids and set(declared_member_ids) != set(member_ids):
            findings.append(
                _finding(
                    "roster_count_mismatch",
                    f"Roster {roster_id} member_ids do not match member records.",
                    {"member_ids": declared_member_ids, "record_member_ids": member_ids},
                )
            )
        change_ids = declared_member_ids or member_ids
        if operation == "join":
            for member in members:
                member_id = str(member["member_id"])
                prior = existing_members.get(member_id)
                if isinstance(prior, dict) and any(
                    prior.get(field) not in (None, "")
                    and member.get(field) not in (None, "")
                    and prior.get(field) != member.get(field)
                    for field in ("character_id", "descriptor")
                ):
                    findings.append(
                        _finding(
                            "roster_member_identity_drift",
                            f"Roster member {member_id} changed identity fields.",
                            {"before": prior, "after": member},
                        )
                    )
            next_members = {**existing_members, **{str(item["member_id"]): item for item in members}}
            expected_delta = len(set(change_ids) - set(existing_members))
        elif operation in {"leave", "dead", "missing"}:
            next_members = {key: item for key, item in existing_members.items() if key not in set(change_ids)}
            expected_delta = -len(set(change_ids) & set(existing_members))
        else:
            next_members = {str(item["member_id"]): item for item in members}
            expected_delta = len(next_members) - len(existing_members)
        declared_delta = raw.get("delta")
        declared_count = raw.get("declared_count")
        computed_count = len(next_members)
        if declared_delta is not None and declared_delta != expected_delta:
            findings.append(
                _finding(
                    "roster_count_mismatch",
                    f"Roster {roster_id} declared delta does not match member records.",
                    {"declared_delta": declared_delta, "computed_delta": expected_delta},
                )
            )
        if declared_count is not None and declared_count != computed_count:
            findings.append(
                _finding(
                    "roster_count_mismatch",
                    f"Roster {roster_id} declared count does not match computed members.",
                    {"declared_count": declared_count, "computed_count": computed_count},
                )
            )
        state["roster"][roster_id] = {
            **copy.deepcopy(existing or {}),
            "roster_id": roster_id,
            "members": list(next_members.values()),
            "declared_count": computed_count,
            "computed_count": computed_count,
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
        existing = state["numeric_counters"].get(counter_id)
        current = (existing or {}).get("current_value", raw.get("previous_value"))
        previous = raw.get("previous_value", raw.get("before"))
        delta = raw.get("delta")
        expected = raw.get("expected_value", raw.get("after"))
        declared = raw.get("declared_value", expected)
        if current is not None and previous != current:
            findings.append(
                _finding(
                    "numeric_counter_stale_before",
                    f"Counter {counter_id} starts from stale value.",
                    {"expected_previous": current, "declared_previous": previous},
                )
            )
        if _number(previous) and _number(delta) and previous + delta != expected:
            findings.append(
                _finding(
                    "numeric_counter_arithmetic_mismatch",
                    f"Counter {counter_id} arithmetic is inconsistent.",
                    {"previous": previous, "delta": delta, "expected": expected},
                )
            )
        if expected != declared:
            findings.append(
                _finding(
                    "numeric_counter_arithmetic_mismatch",
                    f"Counter {counter_id} declared value differs from expected value.",
                    {"expected": expected, "declared": declared},
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
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for raw in _objects(value):
        owner_id = str(raw.get("owner_id") or "").strip()
        item_id = str(raw.get("item_id") or "").strip()
        key = str(raw.get("inventory_id") or f"{owner_id}:{item_id}").strip()
        previous = raw.get("previous_quantity", raw.get("before"))
        delta = raw.get("delta")
        declared = raw.get("declared_quantity", raw.get("after"))
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
        if current is not None and before != current:
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
        if existing is not None and existing != raw:
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
        "character_changes": list(state["characters"].values()),
        "relationship_changes": list(state["relationships"].values()),
        "roster_changes": [
            {**copy.deepcopy(item), "operation": "replace"}
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
    "empty_authoritative_state",
    "normalize_entity_alias",
    "require_authoritative_state_delta",
    "validate_authoritative_state",
    "validate_authoritative_state_delta",
]
