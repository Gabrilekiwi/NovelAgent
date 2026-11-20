"""Author identity/baseline supplements replayed with the seven fact kinds.

Supplements are review evidence, not a second state database. The same pure
transition is used by preparation and cold-history verification.
"""
from __future__ import annotations

import copy
import math

from core.engine.chapter_fact_identity import (
    _digest, _text, normalize_entity_directory, prepare_identity_decisions,
    resolve_entity_reference,
)
from core.state.authoritative import validate_authoritative_state
from core.state.location_mirrors import CHARACTER_LOCATION_FIELDS, validate_character_location_mirrors

SUPPORT_VERSION = "reviewed_fact_support_v1"
DECLARATION_EVENT = "reviewed_entity_declaration_v1"
_RESERVED = frozenset({"id", "character_id", "entity_id", "canonical_name", "name", "aliases", "identity",
    "relationship_id", "source_character_id", "target_character_id", "source_id", "target_id", "type", "kind",
    "source_tier", "source_event_id", "scene_index", "field", "before", "after", *CHARACTER_LOCATION_FIELDS})


class ChapterFactSupportError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _fail(code, message):
    raise ChapterFactSupportError(code, message)


def normalize_fact_support(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"version", "identities", "baselines"}
            or value["version"] != SUPPORT_VERSION
            or any(not isinstance(value[k], list) or len(value[k]) > 100 for k in ("identities", "baselines"))):
        _fail("chapter_fact_support_invalid", "support must contain versioned identity and baseline decisions")
    try:
        _digest(value)
    except (ValueError, TypeError):
        _fail("chapter_fact_support_invalid", "support contains non-JSON or non-finite values")
    return copy.deepcopy(value)


def declared_identities(state):
    result = []
    for event in state["events"].values():
        if isinstance(event, dict) and event.get("type") == DECLARATION_EVENT:
            entry = event.get("declared_identity")
            if entry is None:
                _fail("chapter_fact_support_invalid", "declared identity evidence is incomplete")
            result.extend(normalize_entity_directory([entry]))
    return result


def support_entity_directory(state):
    entries = {(e["kind"], e["entity_id"]):e for e in declared_identities(state)}
    def add(kind, entity_id, name=None, aliases=None):
        key = (kind, entity_id)
        entry = {"kind":kind, "entity_id":entity_id, "name":name or entity_id, "aliases":aliases or []}
        if key not in entries:
            entries[key] = entry
        elif kind == "character" and name and entries[key]["name"] != name:
            _fail("chapter_fact_identity_conflict", "declared character name differs from current state")
    for key, record in state["characters"].items():
        add("character", key, record.get("canonical_name"), record.get("aliases"))
    for record in state["locations"].values():
        if record.get("location_id"):
            add("location", record["location_id"])
    for record in state["inventory"].values():
        add("item", record["item_id"], record.get("item_name") or record.get("name"), record.get("aliases"))
    return normalize_entity_directory(list(entries.values()))


def _resolve_local(value, references):
    if not isinstance(value, str) or not value.startswith("local:"):
        return value
    entry = references.get(value[6:])
    if entry is None:
        _fail("chapter_fact_identity_unknown", "local identity has no accepted author declaration/selection")
    return entry["entity_id"]


def resolve_fact_identities(facts, references):
    # Only entity-reference fields are substituted. Narrative text, evidence,
    # event keys and state values must not be rewritten by identity resolution.
    ref_fields = {"entity_id", "source_entity_id", "target_entity_id", "owner_id", "item_id",
                  "occurrence_location", "from_location", "to_location", "carrier_id"}
    def visit(value):
        if isinstance(value, list):
            return [visit(v) for v in value]
        if not isinstance(value, dict):
            return value
        return {key:(copy.deepcopy(item) if key in {"after", "value"} else _resolve_local(item, references) if key in ref_fields else
                     [_resolve_local(member, references) for member in item] if key == "member_ids" else visit(item))
                for key,item in value.items()}
    result = copy.deepcopy(facts)
    for fact in result:
        fact["proposal"] = visit(fact["proposal"])
    return result


def _number(value, *, quantity=False):
    if (type(value) not in (int,float) or type(value) is float and not math.isfinite(value)
            or quantity and (type(value) is not int or value < 0)):
        _fail("chapter_fact_baseline_invalid", "baseline requires a known finite number; quantity is a nonnegative integer")
    return value


def _missing(record, field, value):
    if field in record:
        _fail("chapter_fact_baseline_already_known", "baseline supplement cannot overwrite a bound value")
    record[field] = copy.deepcopy(value)


def prepare_fact_support(*, base_state, support, book_id, review_scope):
    support = normalize_fact_support(support)
    if support is None:
        _fail("chapter_fact_support_invalid", "explicit support is required")
    base = validate_authoritative_state(copy.deepcopy(base_state))
    validate_character_location_mirrors(base)
    working = copy.deepcopy(base)
    identity = prepare_identity_decisions(directory=support_entity_directory(base), book_id=book_id,
        review_scope=review_scope, decisions=support["identities"])
    for entry in identity["declarations"]:
        event_id = "fact_identity_" + _digest({"book_id":book_id, "review_scope":review_scope, "identity":entry})
        if event_id in working["events"]:
            _fail("chapter_fact_identity_conflict", "identity declaration already exists")
        working["events"][event_id] = {"event_id":event_id, "type":DECLARATION_EVENT,
            "declared_identity":copy.deepcopy(entry), "review_source":"author", "source_tier":"chapter_event"}
        if entry["kind"] == "character":
            if entry["entity_id"] in working["characters"]:
                _fail("chapter_fact_identity_conflict", "character id is already occupied")
            working["characters"][entry["entity_id"]] = {"character_id":entry["entity_id"],
                "canonical_name":entry["name"], "aliases":copy.deepcopy(entry["aliases"]), "source_tier":"chapter_event"}
    directory = identity["directory_after"]
    def known(kind, value):
        if kind is None:
            matches = [e for e in directory if e["entity_id"] == value]
            if len(matches) != 1:
                _fail("chapter_fact_identity_unknown", "baseline requires an unambiguous declared entity")
            return
        resolve_entity_reference(directory, kind=kind, reference=value, reference_type="id")
    checked, keys = [], set()
    shapes = {
        "character_state":{"entity_id","field","value"},
        "location":{"entity_id","location_id"},
        "inventory":{"owner_id","item_id","quantity"},
        "counter":{"counter_id","value"},
        "relationship":{"relationship_id","source_entity_id","target_entity_id","relation_type","field","value"},
        "roster":{"roster_id","member_ids"},
    }
    for raw in support["baselines"]:
        if not isinstance(raw, dict):
            _fail("chapter_fact_support_invalid", "baseline decision must be an object")
        kind = _text(raw.get("kind"), "baseline kind", 50)
        if kind not in shapes or set(raw) != {"local_key","kind","review_source","reason",*shapes[kind]}:
            _fail("chapter_fact_support_invalid", "baseline decision fields differ")
        key = _text(raw["local_key"], "baseline local key", 64)
        if key in keys or raw["review_source"] != "author":
            _fail("chapter_fact_support_invalid", "baseline needs a unique key and explicit author source")
        keys.add(key)
        _text(raw["reason"], "baseline author reason", 1000)
        value = copy.deepcopy(raw)
        for field in ("entity_id","owner_id","item_id","source_entity_id","target_entity_id","location_id"):
            if field in value:
                value[field] = _resolve_local(value[field], identity["local_references"])
        if "member_ids" in value:
            if not isinstance(value["member_ids"], list):
                _fail("chapter_fact_baseline_invalid", "roster baseline needs an explicit member list")
            value["member_ids"] = [_resolve_local(m, identity["local_references"]) for m in value["member_ids"]]
        if kind in {"character_state","relationship"}:
            field = _text(value["field"], "state field", 100)
            if field in _RESERVED or field.startswith("_"):
                _fail("chapter_fact_identity_field_forbidden", "baseline cannot alter identity/authority/location metadata")
        if kind == "character_state":
            entity_id = value["entity_id"]
            known("character", entity_id)
            _missing(working["characters"][entity_id], field, value["value"])
        elif kind == "location":
            entity_id = value["entity_id"]
            known(None, entity_id)
            known("location", value["location_id"])
            record = working["locations"].setdefault(entity_id, {"entity_id":entity_id, "source_tier":"chapter_event"})
            _missing(record, "location_id", value["location_id"])
        elif kind == "inventory":
            known(None, value["owner_id"])
            known("item", value["item_id"])
            quantity = _number(value["quantity"], quantity=True)
            matches = [record for record in working["inventory"].values()
                if (record.get("owner_id"),record.get("item_id")) == (value["owner_id"],value["item_id"])]
            if len(matches) > 1:
                _fail("chapter_fact_baseline_invalid", "inventory has duplicate owner/item ledgers")
            if matches:
                _missing(matches[0], "quantity", quantity)
            else:
                record_id = "inventory_" + _digest({"book_id":book_id,"owner_id":value["owner_id"],"item_id":value["item_id"]})[:32]
                if record_id in working["inventory"]:
                    _fail("chapter_fact_baseline_invalid", "inventory identity collision")
                working["inventory"][record_id] = {"inventory_id":record_id,"owner_id":value["owner_id"],
                    "item_id":value["item_id"],"quantity":quantity,"source_tier":"chapter_event"}
        elif kind == "counter":
            counter_id = _text(value["counter_id"], "counter id")
            record = working["numeric_counters"].setdefault(counter_id, {"counter_id":counter_id,"source_tier":"chapter_event"})
            _missing(record, "current_value", _number(value["value"]))
        elif kind == "relationship":
            pair = (value["source_entity_id"], value["target_entity_id"])
            for member in pair: known("character", member)
            record_id = _text(value["relationship_id"], "relationship id")
            relation_type = _text(value["relation_type"], "relationship type")
            record = working["relationships"].setdefault(record_id, {"relationship_id":record_id,
                "source_character_id":pair[0], "target_character_id":pair[1], "type":relation_type,"source_tier":"chapter_event"})
            if (record.get("source_character_id",record.get("source_id")),record.get("target_character_id",record.get("target_id")),
                    record.get("type",record.get("kind"))) != (*pair,relation_type):
                _fail("chapter_fact_identity_conflict", "relationship baseline targets another identity")
            _missing(record, field, value["value"])
        else:
            record_id = _text(value["roster_id"], "roster id")
            if record_id in working["roster"]:
                _fail("chapter_fact_baseline_already_known", "roster baseline is already bound")
            members = value["member_ids"]
            for member in members: known("character", member)
            if len(members) != len(set(members)):
                _fail("chapter_fact_baseline_invalid", "roster baseline has duplicate members")
            working["roster"][record_id] = {"roster_id":record_id,"members":[{"member_id":m} for m in members],
                "unresolved_count":0,"declared_count":len(members),"source_tier":"chapter_event"}
        checked.append(value)
    working = validate_authoritative_state(working)
    validate_character_location_mirrors(working)
    result = {"version":SUPPORT_VERSION,"publication_authorized":False,"book_id":book_id,"review_scope":review_scope,
        "base_state_sha256":_digest(base),"support":support,"identity_plan":identity,"resolved_baselines":checked,
        "state_after_support":working}
    result["support_plan_sha256"] = _digest(result)
    return result


def prepare_supported_fact_changes(*, facts, chapter_text, base_state, support=None, book_id=None, review_scope=None):
    from core.engine.chapter_fact_prepare import prepare_confirmed_fact_changes
    if support is None:
        return prepare_confirmed_fact_changes(facts=facts, chapter_text=chapter_text, base_state=base_state)
    supplemental = prepare_fact_support(base_state=base_state, support=support, book_id=book_id, review_scope=review_scope)
    resolved = resolve_fact_identities(facts, supplemental["identity_plan"]["local_references"])
    result = prepare_confirmed_fact_changes(facts=resolved, chapter_text=chapter_text, base_state=supplemental["state_after_support"])
    return {**result, "base_state_sha256":supplemental["base_state_sha256"],
        "support_preparation":supplemental,"resolved_facts":resolved}
