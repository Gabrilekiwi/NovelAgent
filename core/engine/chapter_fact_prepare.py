"""Pure preparation of reviewed facts; does not publish or choose book authority."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any

from core.engine.chapter_fact_candidate import require_confirmed_fact_review
from core.state.authoritative import (
    empty_authoritative_state,
    validate_authoritative_state,
    validate_authoritative_state_delta,
)
from core.state.roster import reduce_roster_mutation
from core.state.location_mirrors import CHARACTER_LOCATION_FIELDS, validate_character_location_mirrors


# Identity, source precedence and reducer metadata are not editable state
# fields. Identity declarations/resolution have a separate T2.2 contract.
_RESERVED_FIELDS = frozenset({
    "id", "character_id", "entity_id", "canonical_name", "name", "aliases", "identity",
    "relationship_id", "source_character_id", "target_character_id", "source_id", "target_id",
    "type", "kind", "source_tier", "source_event_id", "scene_index", "field", "before", "after",
})


class ChapterFactPreparationError(ValueError):
    def __init__(self, message: str, *, code: str, evidence: Any = None):
        self.code = code
        self.evidence = copy.deepcopy(evidence)
        super().__init__(f"{code}: {message}")


def _reject(code: str, message: str, evidence: Any = None):
    raise ChapterFactPreparationError(message, code=code, evidence=evidence)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _number(value: Any, label: str, *, integer: bool = False) -> int | float:
    valid = isinstance(value, int) and not isinstance(value, bool)
    if not integer and isinstance(value, float) and math.isfinite(value):
        valid = True
    if not valid:
        _reject("chapter_fact_baseline_invalid", f"{label} requires a known finite number")
    return value


def _record(state: dict, collection: str, key: str, *fields: str) -> dict:
    value = state[collection].get(key)
    if not isinstance(value, dict) or any(field not in value for field in fields):
        _reject("chapter_fact_baseline_missing", f"Missing bound baseline for {collection}/{key}", list(fields))
    return value


def _target(fact: dict) -> tuple[str, ...]:
    p, kind = fact["proposal"], fact["kind"]
    keys = {
        "character_state": ("entity_id", "field"), "relationship": ("relationship_id", "field"),
        "roster": ("roster_id",), "location": ("entity_id",),
        "inventory": ("owner_id", "item_id"), "counter": ("counter_id",),
    }
    return (kind, *(p[key] for key in keys[kind]))


def _span(fact: dict) -> tuple[int, int]:
    return min(s["start"] for s in fact["evidence_spans"]), max(s["end"] for s in fact["evidence_spans"])


def _known_references(state: dict) -> set[str]:
    from core.engine.chapter_fact_support import declared_identities
    known = set(state["characters"]) | set(state["locations"])
    known.update(entry["entity_id"] for entry in declared_identities(state))
    for record in state["locations"].values():
        if isinstance(record, dict) and isinstance(record.get("location_id"), str):
            known.add(record["location_id"])
    for record in state["inventory"].values():
        if isinstance(record, dict):
            known.update(value for key in ("owner_id", "item_id") if isinstance(value := record.get(key), str))
    for record in state["roster"].values():
        if isinstance(record, dict):
            known.update(member["member_id"] for member in record.get("members", [])
                         if isinstance(member, dict) and isinstance(member.get("member_id"), str))
    return known


def _require_known(known: set[str], *references: str | None):
    missing = sorted({reference for reference in references if reference is not None and reference not in known})
    if missing:
        _reject("chapter_fact_identity_unknown", "Unknown identities require an explicit baseline/declaration", missing)


def prepare_confirmed_fact_changes(*, facts: list[dict], chapter_text: str, base_state: dict) -> dict:
    """Compute a non-authoritative plan from an upstream-bound authority snapshot.

    This function cannot establish that a caller's snapshot is trusted. The
    eventual commit service must bind it to the book, revision and read set.
    Missing baselines are explicit errors; entity introduction belongs to T2.2.
    """
    accepted = require_confirmed_fact_review(facts, chapter_text=chapter_text)
    if (not isinstance(base_state, dict) or base_state.get("schema_version") != "1.0"
            or any(not isinstance(base_state.get(key), dict) for key in empty_authoritative_state()
                   if key not in {"schema_version", "source_precedence"})):
        _reject("chapter_fact_baseline_invalid", "A complete authoritative state snapshot is required")
    try:
        base = validate_authoritative_state(copy.deepcopy(base_state))
        validate_character_location_mirrors(base)
        _canonical(base)
    except (ValueError, TypeError) as exc:
        _reject("chapter_fact_baseline_invalid", "The bound baseline is invalid", str(exc))
    working = copy.deepcopy(base)
    known = _known_references(base)
    events = {}
    chapter_hash = hashlib.sha256(chapter_text.encode("utf-8")).hexdigest()
    for fact in sorted((f for f in accepted if f["kind"] == "event"), key=lambda f: (*_span(f), f["local_key"])):
        p = fact["proposal"]
        if p["event_type"] == "reviewed_entity_declaration_v1":
            _reject("chapter_fact_event_type_reserved", "Identity declarations require their explicit author decision contract")
        _require_known(known, p["occurrence_location"])
        for field in ("onsite_participants", "remote_participants", "related_entities"):
            _require_known(known, *(item["entity_id"] for item in p[field]))
        for item in p["transported_items"]:
            _require_known(known, item["item_id"], item["from_location"], item["to_location"], item["carrier_id"])
        event_id = "fact_event_" + _digest({"chapter": chapter_hash, "local_key": fact["local_key"],
                                           "proposal": p, "evidence": sorted(fact["evidence_spans"], key=lambda s: s["start"])})
        if event_id in base["events"]:
            _reject("chapter_fact_event_already_applied", "The source event is already present", event_id)
        events[fact["local_key"]] = {"event_id": event_id, "type": p["event_type"],
                                      **{k: copy.deepcopy(v) for k, v in p.items() if k != "event_type"},
                                      "evidence_spans": copy.deepcopy(fact["evidence_spans"])}
    changes = sorted((f for f in accepted if f["kind"] != "event"), key=lambda f: (*_span(f), _target(f), f["local_key"]))
    prior_by_target = {}
    for fact in changes:
        target = _target(fact)
        previous = prior_by_target.get(target)
        if previous is not None and _span(fact)[0] < _span(previous)[1]:
            _reject("chapter_fact_order_ambiguous", "Changes to one target have overlapping evidence", [previous["fact_id"], fact["fact_id"]])
        prior_by_target[target] = fact
    delta = {"source_tier": "chapter_event", "events": list(events.values()),
             **{key: [] for key in ("character_changes", "relationship_changes", "roster_changes",
                                    "location_changes", "inventory_changes", "numeric_changes")}}
    computed = []
    for fact in changes:
        p, kind = fact["proposal"], fact["kind"]
        mirrored_fields = []
        source = events[p["source_event_key"]]["event_id"]
        dependency = {"source_event_id": source}
        if kind in {"character_state", "relationship"} and p["field"] in _RESERVED_FIELDS:
            _reject("chapter_fact_identity_field_forbidden", "Identity/authority metadata cannot be changed through a state field", p["field"])
        if kind == "character_state":
            if p["field"] in CHARACTER_LOCATION_FIELDS:
                _reject("chapter_fact_location_kind_required", "Position changes require a location fact", p["field"])
            record = _record(working, "characters", p["entity_id"], p["field"], "canonical_name")
            if record["canonical_name"] != p["canonical_name"]:
                _reject("chapter_fact_identity_conflict", "Canonical character identity changed", fact["fact_id"])
            before, after = copy.deepcopy(record[p["field"]]), copy.deepcopy(p["after"])
            change = {"character_id": p["entity_id"], "canonical_name": p["canonical_name"],
                      "field": p["field"], "before": before, "after": after, **dependency}
            delta["character_changes"].append(change)
            record[p["field"]] = copy.deepcopy(after)
        elif kind == "relationship":
            record = _record(working, "relationships", p["relationship_id"], p["field"])
            pair = (record.get("source_character_id", record.get("source_id")), record.get("target_character_id", record.get("target_id")))
            _require_known(known, p["source_entity_id"], p["target_entity_id"])
            if pair != (p["source_entity_id"], p["target_entity_id"]) or record.get("type", record.get("kind")) != p["relation_type"]:
                _reject("chapter_fact_identity_conflict", "Relationship identity changed", fact["fact_id"])
            before, after = copy.deepcopy(record[p["field"]]), copy.deepcopy(p["after"])
            change = {"relationship_id": p["relationship_id"], "source_character_id": pair[0],
                      "target_character_id": pair[1], "type": p["relation_type"], "field": p["field"],
                      "before": before, "after": after, **dependency}
            delta["relationship_changes"].append(change)
            record[p["field"]] = copy.deepcopy(after)
        elif kind == "location":
            record = _record(working, "locations", p["entity_id"], "location_id")
            _require_known(known, p["entity_id"], p["to_location"])
            before, after = record["location_id"], p["to_location"]
            if not isinstance(before, str) or not before:
                _reject("chapter_fact_baseline_missing", "Current location is unknown", fact["fact_id"])
            if p["from_location"] is not None and p["from_location"] != before:
                _reject("chapter_fact_location_conflict", "Proposed departure disagrees with bound location", fact["fact_id"])
            delta["location_changes"].append({"entity_id": p["entity_id"], "before": before, "after": after, **dependency})
            record["location_id"] = after
            character = working["characters"].get(p["entity_id"], {})
            for field in CHARACTER_LOCATION_FIELDS:
                if field not in character:
                    continue
                delta["character_changes"].append({"character_id": p["entity_id"],
                    "canonical_name": character["canonical_name"], "field": field,
                    "before": before, "after": after, **dependency})
                character[field] = after
                mirrored_fields.append(field)
        elif kind == "inventory":
            matches = [(key, value) for key, value in working["inventory"].items() if isinstance(value, dict)
                       and (value.get("owner_id"), value.get("item_id")) == (p["owner_id"], p["item_id"])]
            if len(matches) != 1:
                _reject("chapter_fact_baseline_missing", "Inventory requires exactly one bound ledger entry", [p["owner_id"], p["item_id"]])
            key, record = matches[0]
            before = _number(record.get("quantity"), "inventory quantity", integer=True)
            after = before + p["delta"]
            if before < 0 or after < 0:
                _reject("chapter_fact_inventory_negative", "Inventory cannot become negative", fact["fact_id"])
            delta["inventory_changes"].append({"inventory_id": key, "owner_id": p["owner_id"], "item_id": p["item_id"],
                                                "previous_quantity": before, "delta": p["delta"], "declared_quantity": after, **dependency})
            record["quantity"] = after
        elif kind == "counter":
            record = _record(working, "numeric_counters", p["counter_id"], "current_value")
            before = _number(record["current_value"], "counter baseline")
            after = _number(before + p["value"] if p["operation"] == "increment" else p["value"], "counter result")
            change_amount = _number(after - before, "counter delta")
            delta["numeric_changes"].append({"counter_id": p["counter_id"], "previous_value": before, "delta": change_amount,
                                              "expected_value": after, "declared_value": after, **dependency})
            record["current_value"] = after
        else:
            record = _record(working, "roster", p["roster_id"], "members", "declared_count")
            _require_known(known, *p["member_ids"])
            before = _number(record["declared_count"], "roster count", integer=True)
            count_delta = len(p["member_ids"]) * (1 if p["operation"] == "add" else -1)
            after = before + count_delta
            mutation = {"roster_id": p["roster_id"], "operation": "join" if p["operation"] == "add" else "leave",
                        "member_ids": p["member_ids"], "delta": count_delta, "declared_count": after, **dependency}
            if p["operation"] == "add":
                mutation["members"] = [{"member_id": member} for member in p["member_ids"]]
            transition = reduce_roster_mutation(working["roster"], mutation,
                                                current_events={event["event_id"]: event for event in events.values()})
            if transition["issues"]:
                _reject("chapter_fact_roster_invalid", "Roster mutation rejected by the domain reducer", transition["issues"])
            delta["roster_changes"].append(mutation)
            working["roster"][p["roster_id"]] = transition["record"]
        computed.append({"fact_id": fact["fact_id"], "kind": kind, "target": list(_target(fact)),
                         "before": copy.deepcopy(before), "after": copy.deepcopy(after),
                         "source_event_id": source, "source_event_key": p["source_event_key"],
                         "evidence_spans": copy.deepcopy(fact["evidence_spans"]),
                         **({"mirrored_character_fields": mirrored_fields} if mirrored_fields else {})})
    report = validate_authoritative_state_delta(base_state=base, state_delta=delta, chapter_text=chapter_text)
    if not report["accepted"]:
        _reject("chapter_fact_domain_conflict", "The prepared changes fail existing domain rules", report["findings"])
    validate_character_location_mirrors(report["state_after"])
    return {"schema_version": "1.0", "chapter_sha256": chapter_hash, "base_state_sha256": _digest(base),
            "state_delta": delta, "computed_changes": computed, "state_after": report["state_after"],
            "event_dependencies": {key: event["event_id"] for key, event in events.items()}}
