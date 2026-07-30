from __future__ import annotations

import copy
import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from core.state.authoritative_context import (
    AUTHORITATIVE_CONTEXT_SELECTION_KEY,
    AUTHORITATIVE_RECORD_COLLECTIONS,
    project_authoritative_state,
)
from core.state.chapter_read_set import normalize_chapter_context_read_set
from core.structured_context import StructuredContextError, sha256_text


GENERATION_STATE_VIEW_SCHEMA_VERSION = "1.0"
GENERATION_STATE_VIEW_KIND = "chapter_generation_state"
GENERATION_STATE_VIEW_HEADING = "Generation State View"
GENERATION_STATE_VIEW_POLICY = "chapter_authority_working_set_v1"
SCENE_GENERATION_STATE_REFERENCE_SCHEMA_VERSION = "1.0"
SCENE_GENERATION_STATE_REFERENCE_KIND = "scene_generation_state_reference"
SCENE_GENERATION_STATE_REFERENCE_HEADING = "Generation State View Reference"
SCENE_GENERATION_STATE_REFERENCE_POLICY = (
    "scene_generation_state_reference_v1"
)
PLAN_GENERATION_STATE_PROJECTION_SCHEMA_VERSION = "1.0"
PLAN_GENERATION_STATE_PROJECTION_KIND = "plan_generation_state_projection"
PLAN_GENERATION_STATE_PROJECTION_HEADING = "Generation State View Projection"
PLAN_GENERATION_STATE_PROJECTION_POLICY = "plan_generation_state_projection_v1"

_CURRENT_STATE_COLLECTIONS = tuple(
    collection
    for collection in AUTHORITATIVE_RECORD_COLLECTIONS
    if collection != "events"
)
_TRANSITION_AND_AUDIT_FIELDS = frozenset(
    {
        "after",
        "baseline_evidence",
        "baseline_source",
        "before",
        "declared_quantity",
        "declared_value",
        "delta",
        "expected_value",
        "from_location",
        "introduced_chapter",
        "introduced_event_id",
        "migration_id",
        "previous_quantity",
        "previous_value",
        "reason",
        "scene_index",
        "source_event_id",
        "to_location",
    }
)
_CHARACTER_LEGACY_PROJECTION_FIELDS = frozenset(
    {
        # Chapter goals are planning inputs, not durable character state.  The
        # legacy snapshot did not reliably advance this field on every commit.
        "current_goal",
        # Location has one authoritative owner in the typed ``locations``
        # collection.  Keeping a second copy on a character can reintroduce the
        # exact stale-location rollback the working-set view is meant to stop.
        "current_location",
        "location_id",
        # These are append-log/rendering metadata from the legacy projection,
        # not the character's current point-in-time state.
        "last_observation",
        "last_seen_chapter",
    }
)
_TERMINAL_EVENT_STATUSES = frozenset(
    {
        "completed",
        "resolved",
        "interrupted",
        "cancelled",
        "canceled",
        "failed",
        "abandoned",
        "closed",
        "expired",
    }
)
_CONFIRMED_LOCATION_CERTAINTIES = frozenset({"confirmed", "current"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def build_generation_state_view(
    authoritative_state: dict[str, Any],
    chapter_context_read_set: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a typed, auditable model view from an explicit chapter read set.

    The complete authoritative ledger remains the local source of truth. This
    view contains only explicitly requested current-state records and explicitly
    requested historical events.
    """

    if not isinstance(authoritative_state, dict):
        raise _invalid("authoritative_state must be one JSON object")
    normalized_read_set = _normalize_read_set(chapter_context_read_set)
    _validate_expected_new_entities(
        normalized_read_set["expected_new_entities"],
        authoritative_state=authoritative_state,
    )

    required_state_ids = normalized_read_set["required_state_item_ids"]
    required_event_ids = normalized_read_set["required_event_item_ids"]
    _validate_typed_item_ids(
        required_state_ids,
        allowed_collections=set(_CURRENT_STATE_COLLECTIONS),
        field_name="required_state_item_ids",
    )
    _validate_typed_item_ids(
        required_event_ids,
        allowed_collections={"events"},
        field_name="required_event_item_ids",
    )

    source_chars = len(_canonical_json(authoritative_state))
    projection_budget = max(100_000, source_chars + 50_000)
    state_projection = project_authoritative_state(
        authoritative_state,
        max_chars=projection_budget,
        required_item_ids=required_state_ids,
        selection_mode="required_only",
        require_query_references=False,
        require_open_events=False,
    )
    event_projection = project_authoritative_state(
        authoritative_state,
        max_chars=projection_budget,
        required_item_ids=required_event_ids,
        selection_mode="required_only",
        require_query_references=False,
        require_open_events=False,
    )

    state_manifest = state_projection[AUTHORITATIVE_CONTEXT_SELECTION_KEY]
    event_manifest = event_projection[AUTHORITATIVE_CONTEXT_SELECTION_KEY]
    source_authority_sha256 = str(state_manifest.get("source_sha256") or "")
    if source_authority_sha256 != str(event_manifest.get("source_sha256") or ""):
        raise _invalid("authority projections do not bind the same raw source")

    current_state: dict[str, dict[str, Any]] = {
        collection: {} for collection in _CURRENT_STATE_COLLECTIONS
    }
    for item_id in required_state_ids:
        collection, record_id = _split_item_id(item_id)
        raw_record = (state_projection.get(collection) or {}).get(record_id)
        if not isinstance(raw_record, dict):
            raise _invalid(f"selected current-state record {item_id!r} is missing")
        current_state[collection][record_id] = _current_record_view(
            raw_record,
            collection=collection,
        )

    required_events: dict[str, dict[str, Any]] = {}
    for item_id in required_event_ids:
        _collection, record_id = _split_item_id(item_id)
        raw_event = (event_projection.get("events") or {}).get(record_id)
        if not isinstance(raw_event, dict):
            raise _invalid(f"selected historical event {item_id!r} is missing")
        required_events[record_id] = copy.deepcopy(raw_event)

    selected_item_ids = [*required_state_ids, *required_event_ids]
    read_set_digest = normalized_read_set["contract_sha256"]
    view: dict[str, Any] = {
        "schema_version": GENERATION_STATE_VIEW_SCHEMA_VERSION,
        "view_kind": GENERATION_STATE_VIEW_KIND,
        "policy": GENERATION_STATE_VIEW_POLICY,
        "chapter_index": normalized_read_set["chapter_index"],
        "source_authority_sha256": source_authority_sha256,
        "read_set_digest": read_set_digest,
        "selected_item_ids_sha256": sha256_text(
            _canonical_json(selected_item_ids)
        ),
        "selected_state_item_ids": list(required_state_ids),
        "selected_event_item_ids": list(required_event_ids),
        "chapter_context_read_set": normalized_read_set,
        "current_state": current_state,
        "required_events": required_events,
        "continuity": copy.deepcopy(normalized_read_set["continuity"]),
        "narrative_constraints": copy.deepcopy(
            normalized_read_set["narrative_constraints"]
        ),
        "expected_new_entities": copy.deepcopy(
            normalized_read_set["expected_new_entities"]
        ),
    }
    view["projection_sha256"] = sha256_text(_canonical_json(view))
    return _validated_view(view)


def generation_state_view_from_markdown(
    text: str,
) -> dict[str, Any] | None:
    """Parse and verify one ``# Generation State View`` JSON section."""

    match = re.search(
        rf"(?ms)^# {re.escape(GENERATION_STATE_VIEW_HEADING)}[ \t]*\r?\n"
        r"(.*?)(?=^# |\Z)",
        str(text or ""),
    )
    if match is None:
        return None
    try:
        value = json.loads(match.group(1).strip())
    except (TypeError, ValueError) as exc:
        raise _invalid("Generation State View must contain one JSON object") from exc
    return _validated_view(value)


def build_scene_generation_state_reference(
    view: dict[str, Any],
) -> dict[str, Any]:
    """Build the lossless Scene-stage reference to one complete generation view.

    Scene prompts receive the changing point-in-time values through
    ``current_scene_state``. Repeating the same values in the shared context
    wastes budget and, worse, can let an older rendering override the dynamic
    Scene state. This reference keeps the authority/read-set hashes, historical
    events, narrative constraints, and scalar-state metadata while binding the
    omitted current values to the dedicated payload field.
    """

    checked = _validated_view(view)
    current = checked["current_state"]
    scalar_metadata: dict[str, dict[str, Any]] = {
        "locations": _scalar_record_metadata(
            current["locations"],
            value_fields={"location_id"},
        ),
        "inventories": _scalar_record_metadata(
            current["inventory"],
            value_fields={"quantity"},
        ),
        "counters": _scalar_record_metadata(
            current["numeric_counters"],
            value_fields={"current_value"},
        ),
    }
    reference: dict[str, Any] = {
        "schema_version": SCENE_GENERATION_STATE_REFERENCE_SCHEMA_VERSION,
        "view_kind": SCENE_GENERATION_STATE_REFERENCE_KIND,
        "policy": SCENE_GENERATION_STATE_REFERENCE_POLICY,
        "chapter_index": checked["chapter_index"],
        "source_authority_sha256": checked["source_authority_sha256"],
        "source_generation_state_view_sha256": checked["projection_sha256"],
        "read_set_digest": checked["read_set_digest"],
        "selected_item_ids_sha256": checked["selected_item_ids_sha256"],
        "current_state_binding": {
            "payload_field": "current_scene_state",
            "projection_policy": "sanitized_dynamic_overlay_v1",
            "collection_counts": {
                "characters": len(current["characters"]),
                "relationships": len(current["relationships"]),
                "rosters": len(current["roster"]),
                "locations": len(current["locations"]),
                "inventories": len(current["inventory"]),
                "counters": len(current["numeric_counters"]),
            },
            "scalar_state_metadata": scalar_metadata,
        },
        "required_events": copy.deepcopy(checked["required_events"]),
        "continuity": copy.deepcopy(checked["continuity"]),
        "narrative_constraints": copy.deepcopy(
            checked["narrative_constraints"]
        ),
        "expected_new_entities": copy.deepcopy(
            checked["expected_new_entities"]
        ),
    }
    reference["reference_sha256"] = sha256_text(_canonical_json(reference))
    return _validated_scene_generation_state_reference(reference)


def build_plan_generation_state_projection(
    view: dict[str, Any],
) -> dict[str, Any]:
    """Build the non-duplicated model projection used by Plan and Repair."""

    checked = _validated_view(view)
    projection: dict[str, Any] = {
        "schema_version": PLAN_GENERATION_STATE_PROJECTION_SCHEMA_VERSION,
        "view_kind": PLAN_GENERATION_STATE_PROJECTION_KIND,
        "policy": PLAN_GENERATION_STATE_PROJECTION_POLICY,
        "chapter_index": checked["chapter_index"],
        "source_authority_sha256": checked["source_authority_sha256"],
        "source_generation_state_view_sha256": checked["projection_sha256"],
        "read_set_digest": checked["read_set_digest"],
        "selected_item_ids_sha256": checked["selected_item_ids_sha256"],
        "current_state": copy.deepcopy(checked["current_state"]),
        "required_events": copy.deepcopy(checked["required_events"]),
        "continuity": copy.deepcopy(checked["continuity"]),
        "narrative_constraints": copy.deepcopy(
            checked["narrative_constraints"]
        ),
        "expected_new_entities": copy.deepcopy(
            checked["expected_new_entities"]
        ),
    }
    projection["projection_sha256"] = sha256_text(
        _canonical_json(projection)
    )
    return _validated_plan_generation_state_projection(projection)


def validate_plan_generation_state_projection(
    value: Any,
) -> dict[str, Any]:
    """Validate a serialized non-duplicated Plan/Repair state projection."""

    return _validated_plan_generation_state_projection(value)


def scene_generation_state_reference_from_markdown(
    text: str,
) -> dict[str, Any] | None:
    """Parse and verify one Scene-stage generation-state reference."""

    match = re.search(
        rf"(?ms)^# {re.escape(SCENE_GENERATION_STATE_REFERENCE_HEADING)}"
        r"[ \t]*\r?\n(.*?)(?=^# |\Z)",
        str(text or ""),
    )
    if match is None:
        return None
    try:
        value = json.loads(match.group(1).strip())
    except (TypeError, ValueError) as exc:
        raise _invalid(
            "Scene Generation State Reference must contain one JSON object"
        ) from exc
    return _validated_scene_generation_state_reference(value)


def apply_generation_state_view_to_snapshot(
    snapshot: dict[str, Any],
    view: dict[str, Any],
) -> dict[str, Any]:
    """Apply explicit continuity/location facts to a snapshot copy only."""

    if not isinstance(snapshot, dict):
        raise _invalid("snapshot must be one JSON object")
    checked = _validated_view(view)
    result = copy.deepcopy(snapshot)

    story_state = result.setdefault("story_state", {})
    if not isinstance(story_state, dict):
        raise _invalid("snapshot.story_state must be one JSON object")
    continuity = checked["continuity"]
    story_state["last_scene_location"] = copy.deepcopy(
        continuity["last_scene_location"]
    )
    story_state["last_scene_characters"] = copy.deepcopy(
        continuity["last_scene_character_ids"]
    )
    story_state["required_opening_bridge"] = copy.deepcopy(
        continuity["required_opening_bridge"]
    )

    characters = result.setdefault("characters", {})
    if not isinstance(characters, dict):
        raise _invalid("snapshot.characters must be one JSON object")
    spatial_state = result.setdefault("spatial_state", {})
    if not isinstance(spatial_state, dict):
        raise _invalid("snapshot.spatial_state must be one JSON object")
    positions = spatial_state.setdefault("character_positions", {})
    spaces = spatial_state.setdefault("spaces", {})
    if not isinstance(positions, dict) or not isinstance(spaces, dict):
        raise _invalid(
            "snapshot spatial positions and spaces must be JSON objects"
        )
    world_state = result.setdefault("world_state", {})
    if not isinstance(world_state, dict):
        raise _invalid("snapshot.world_state must be one JSON object")
    known_locations = world_state.setdefault("locations", {})
    if not isinstance(known_locations, dict):
        raise _invalid("snapshot.world_state.locations must be one JSON object")

    current_state = checked["current_state"]
    selected_characters = current_state["characters"]
    selected_locations = current_state["locations"]
    _overlay_snapshot_records(
        characters,
        selected_characters,
        field_name="characters",
        excluded_fields={"current_location", "location_id"},
    )
    for collection in (
        "relationships",
        "roster",
        "numeric_counters",
        "inventory",
        "locations",
    ):
        target = result.setdefault(collection, {})
        if not isinstance(target, dict):
            raise _invalid(f"snapshot.{collection} must be one JSON object")
        _overlay_snapshot_records(
            target,
            current_state[collection],
            field_name=collection,
        )

    location_entities = set(selected_locations)
    for record_id, raw_record in selected_locations.items():
        record = raw_record if isinstance(raw_record, dict) else {}
        entity_id = str(record.get("entity_id") or record_id).strip()
        location_id = str(record.get("location_id") or "").strip()
        if not entity_id or not location_id:
            raise _invalid(
                f"selected location record {record_id!r} is incomplete"
            )
        known_locations.setdefault(location_id, {})
        spaces.setdefault(location_id, {})
        if not _location_is_confirmed(record):
            # An unverified report is useful evidence, but it must not inherit a
            # different legacy character/spatial position and accidentally turn
            # that stale cache into an exact Scene boundary.
            positions.pop(entity_id, None)
            existing_character = characters.get(entity_id)
            if existing_character is not None:
                if not isinstance(existing_character, dict):
                    raise _invalid(
                        f"snapshot character {entity_id!r} must be one JSON object"
                    )
                existing_character.pop("current_location", None)
                existing_character.pop("location_id", None)
            continue
        positions[entity_id] = location_id
        if entity_id in selected_characters or entity_id in characters:
            target = characters.setdefault(entity_id, {})
            if not isinstance(target, dict):
                raise _invalid(
                    f"snapshot character {entity_id!r} must be one JSON object"
                )
            target["current_location"] = location_id

    for character_id, raw_record in selected_characters.items():
        if character_id in location_entities:
            continue
        record = raw_record if isinstance(raw_record, dict) else {}
        location_id = str(
            record.get("current_location") or record.get("location_id") or ""
        ).strip()
        if not location_id:
            continue
        target = characters.setdefault(character_id, {})
        if not isinstance(target, dict):
            raise _invalid(
                f"snapshot character {character_id!r} must be one JSON object"
            )
        target["current_location"] = location_id
        positions[character_id] = location_id
        known_locations.setdefault(location_id, {})
        spaces.setdefault(location_id, {})

    return result


def filter_scene_state_for_generation(
    scene_state: dict[str, Any],
    view: dict[str, Any],
    *,
    active_event_ids: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return the sanitized dynamic Scene state for one explicit working set."""

    if not isinstance(scene_state, dict):
        raise _invalid("scene_state must be one JSON object")
    checked = _validated_view(view)
    source = copy.deepcopy(scene_state)
    current = checked["current_state"]
    expected_ids = {
        str(item.get("entity_id") or "").strip()
        for item in checked["expected_new_entities"]
        if isinstance(item, dict)
    }
    expected_ids.discard("")

    character_ids = set(current["characters"]) | expected_ids
    relationship_ids = set(current["relationships"])
    for relationship_id, record in current["relationships"].items():
        if not isinstance(record, dict):
            continue
        source_id = str(
            record.get("source_character_id") or record.get("source_id") or ""
        ).strip()
        target_id = str(
            record.get("target_character_id") or record.get("target_id") or ""
        ).strip()
        if source_id and target_id:
            relationship_ids.add(f"{source_id}->{target_id}")

    projected: dict[str, Any] = {
        "schema_version": str(source.get("schema_version") or "1.0"),
    }
    source_characters = _scene_record_collection(source, "characters")
    projected["characters"] = {}
    for character_id in sorted(character_ids):
        baseline = current["characters"].get(character_id)
        dynamic = source_characters.get(character_id)
        if baseline is None and dynamic is None:
            continue
        projected["characters"][character_id] = _merge_current_scene_record(
            baseline,
            dynamic,
            collection="characters",
            item_id=character_id,
        )

    source_relationships = _scene_record_collection(
        source,
        "relationships",
    )
    projected["relationships"] = {}
    for relationship_id, baseline in sorted(
        current["relationships"].items()
    ):
        prompt_id = _relationship_scene_key(relationship_id, baseline)
        dynamic = source_relationships.get(prompt_id)
        if dynamic is None:
            dynamic = source_relationships.get(relationship_id)
        projected["relationships"][prompt_id] = _merge_current_scene_record(
            baseline,
            dynamic,
            collection="relationships",
            item_id=prompt_id,
        )

    source_rosters = _scene_record_collection(source, "rosters")
    projected["rosters"] = {}
    for roster_id, baseline in sorted(current["roster"].items()):
        projected["rosters"][roster_id] = _merge_current_scene_record(
            baseline,
            source_rosters.get(roster_id),
            collection="roster",
            item_id=roster_id,
        )

    source_locations = _scene_record_collection(source, "locations")
    projected["locations"] = {}
    for entity_id, baseline in sorted(current["locations"].items()):
        baseline_location = str(
            baseline.get("location_id") if isinstance(baseline, dict) else ""
        ).strip()
        dynamic_present = entity_id in source_locations
        dynamic_location = str(source_locations.get(entity_id) or "").strip()
        if _location_is_confirmed(baseline):
            location_id = (
                dynamic_location if dynamic_present else baseline_location
            )
        else:
            # A self-reported/unverified ledger entry is evidence, not an exact
            # Scene boundary. It becomes exact only after a later Scene moves
            # the entity away from the unverified baseline value.
            location_id = (
                dynamic_location
                if dynamic_present
                and dynamic_location
                and dynamic_location != baseline_location
                else ""
            )
        if location_id:
            projected["locations"][entity_id] = location_id
    for entity_id in sorted(expected_ids):
        if entity_id not in source_locations:
            continue
        location_id = str(source_locations[entity_id] or "").strip()
        if location_id:
            projected["locations"][entity_id] = location_id

    projected["inventories"] = _current_numeric_scene_values(
        source,
        scene_key="inventories",
        records=current["inventory"],
        value_field="quantity",
    )
    projected["counters"] = _current_numeric_scene_values(
        source,
        scene_key="counters",
        records=current["numeric_counters"],
        value_field="current_value",
    )

    event_order = list(checked["selected_event_item_ids"])
    required_event_ids = [_split_item_id(item_id)[1] for item_id in event_order]
    source_events = {
        str(item.get("event_id") or ""): item
        for item in source.get("completed_events") or []
        if isinstance(item, dict) and str(item.get("event_id") or "").strip()
    }
    completed_ids_source = {
        str(item).strip()
        for item in source.get("completed_event_ids") or []
        if str(item).strip()
    }
    completed_event_ids: list[str] = []
    for event_id in required_event_ids:
        event = source_events.get(event_id)
        if event is None:
            event = checked["required_events"].get(event_id)
        if not isinstance(event, dict):
            raise _invalid(f"required historical event {event_id!r} is missing")
        status = str(event.get("status") or "completed").strip().casefold()
        if event_id not in completed_ids_source and status not in _TERMINAL_EVENT_STATUSES:
            continue
        completed_event_ids.append(event_id)
    active_ids = {
        str(event_id).strip()
        for event_id in active_event_ids
        if str(event_id).strip()
    }
    for event_id in source.get("completed_event_ids") or []:
        normalized = str(event_id).strip()
        if (
            normalized
            and normalized in active_ids
            and normalized not in completed_event_ids
        ):
            completed_event_ids.append(normalized)
    projected["completed_event_ids"] = completed_event_ids
    # Full event records already appear exactly once in the Scene generation
    # state reference. The local state retains all records for deterministic
    # duplicate/boundary validation.
    projected["completed_events"] = []

    aliases = set(character_ids)
    for record in current["characters"].values():
        if not isinstance(record, dict):
            continue
        for value in (
            record.get("canonical_name"),
            record.get("name"),
            *(record.get("aliases") or []),
        ):
            normalized = str(value or "").strip()
            if normalized:
                aliases.add(normalized)
    projected["characters_present"] = [
        copy.deepcopy(item)
        for item in source.get("characters_present") or []
        if str(item).strip() in aliases
    ]
    projected["current_location"] = str(source.get("current_location") or "")
    allowed_action_ids = set(required_event_ids) | active_ids
    source_open_action = str(source.get("open_action") or "").strip()
    projected["open_action"] = (
        source_open_action
        if source_open_action in allowed_action_ids
        else ""
    )
    projected["open_actions"] = [
        copy.deepcopy(item)
        for item in source.get("open_actions") or []
        if _value_references_any(
            item,
            allowed_values=(
                aliases
                | relationship_ids
                | set(required_event_ids)
                | active_ids
                | set(current["roster"])
            ),
        )
    ]
    return projected


def project_snapshot_for_generation(
    snapshot: dict[str, Any],
    view: dict[str, Any],
) -> dict[str, Any]:
    """Return a bounded model snapshot aligned to the explicit working set."""

    if not isinstance(snapshot, dict):
        raise _invalid("snapshot must be one JSON object")
    checked = _validated_view(view)
    chapter_index = snapshot.get("chapter_index")
    if chapter_index != checked["chapter_index"]:
        raise _invalid(
            "snapshot.chapter_index does not match Generation State View"
        )
    aligned = apply_generation_state_view_to_snapshot(snapshot, checked)
    current = checked["current_state"]

    characters: dict[str, dict[str, Any]] = {}
    for character_id, raw_record in current["characters"].items():
        record = copy.deepcopy(raw_record)
        aligned_character = (aligned.get("characters") or {}).get(character_id)
        if isinstance(aligned_character, dict):
            current_location = str(
                aligned_character.get("current_location") or ""
            ).strip()
            if current_location:
                record["current_location"] = current_location
        characters[character_id] = record

    relevant_location_ids = {
        str(record.get("location_id") or "").strip()
        for record in current["locations"].values()
        if isinstance(record, dict)
    }
    relevant_location_ids.add(
        str(checked["continuity"].get("last_scene_location") or "").strip()
    )
    relevant_location_ids.discard("")
    world_state_source = (
        aligned.get("world_state")
        if isinstance(aligned.get("world_state"), dict)
        else {}
    )
    world_locations_source = world_state_source.get("locations")
    world_locations = (
        world_locations_source if isinstance(world_locations_source, dict) else {}
    )
    world_state = {
        **_bounded_world_scalars(world_state_source),
        "locations": {
            location_id: copy.deepcopy(world_locations.get(location_id) or {})
            for location_id in sorted(relevant_location_ids)
        },
    }

    spatial_source = (
        aligned.get("spatial_state")
        if isinstance(aligned.get("spatial_state"), dict)
        else {}
    )
    spatial_state = _project_spatial_state(
        spatial_source,
        character_ids=set(current["characters"]),
        location_entity_ids={
            entity_id
            for entity_id, record in current["locations"].items()
            if isinstance(record, dict) and _location_is_confirmed(record)
        },
        relevant_location_ids=relevant_location_ids,
    )
    project_profile_source = (
        snapshot.get("project_profile")
        if isinstance(snapshot.get("project_profile"), dict)
        else {}
    )
    project_profile = {
        "language": str(
            project_profile_source.get("language")
            or project_profile_source.get("language_code")
            or ""
        ).strip(),
        "known_characters": _working_character_names(
            current["characters"],
            continuity=checked["continuity"],
        ),
        "known_locations": sorted(relevant_location_ids),
    }
    timeline = snapshot.get("timeline")
    timeline_items = timeline if isinstance(timeline, list) else []
    story_project = (
        snapshot.get("story_project")
        if isinstance(snapshot.get("story_project"), dict)
        else {}
    )
    return {
        **(
            {"book_id": copy.deepcopy(snapshot.get("book_id"))}
            if snapshot.get("book_id") not in (None, "")
            else {}
        ),
        "chapter_index": checked["chapter_index"],
        "project_profile": project_profile,
        "world_state": world_state,
        "story_state": {
            "last_scene_location": checked["continuity"][
                "last_scene_location"
            ],
            "last_scene_characters": list(
                checked["continuity"]["last_scene_character_ids"]
            ),
            "required_opening_bridge": checked["continuity"][
                "required_opening_bridge"
            ],
        },
        "spatial_state": spatial_state,
        "characters": characters,
        "relationships": copy.deepcopy(current["relationships"]),
        "roster": copy.deepcopy(current["roster"]),
        "numeric_counters": copy.deepcopy(current["numeric_counters"]),
        "inventory": copy.deepcopy(current["inventory"]),
        "locations": copy.deepcopy(current["locations"]),
        "required_events": copy.deepcopy(checked["required_events"]),
        "constraints": copy.deepcopy(checked["narrative_constraints"]),
        "expected_new_entities": copy.deepcopy(
            checked["expected_new_entities"]
        ),
        "authority_summary": {
            "source_authority_sha256": checked["source_authority_sha256"],
            "selected_state_item_ids": list(
                checked["selected_state_item_ids"]
            ),
            "selected_event_item_ids": list(
                checked["selected_event_item_ids"]
            ),
        },
        "timeline_summary": {
            "entry_count": len(timeline_items),
            "source_sha256": sha256_text(_canonical_json(timeline_items)),
        },
        "generation_state_view": {
            "schema_version": checked["schema_version"],
            "chapter_index": checked["chapter_index"],
            "read_set_digest": checked["read_set_digest"],
            "source_authority_sha256": checked[
                "source_authority_sha256"
            ],
            "projection_sha256": checked["projection_sha256"],
        },
        **(
            {
                "story_project": {
                    "chapter_index": story_project.get("chapter_index")
                }
            }
            if story_project
            else {}
        ),
    }


def _normalize_read_set(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid("chapter_context_read_set must be one JSON object")
    source = copy.deepcopy(dict(value))
    try:
        return normalize_chapter_context_read_set(
            source,
            chapter_index=source.get("chapter_index"),
            source_outline_sha256=source.get("source_outline_sha256"),
        )
    except (TypeError, ValueError) as exc:
        raise _invalid(str(exc)) from exc


def _validate_typed_item_ids(
    item_ids: list[str],
    *,
    allowed_collections: set[str],
    field_name: str,
) -> None:
    for item_id in item_ids:
        collection, _record_id = _split_item_id(item_id)
        if collection not in allowed_collections:
            raise _invalid(
                f"{field_name} item {item_id!r} uses the wrong collection"
            )


def _split_item_id(item_id: str) -> tuple[str, str]:
    collection, separator, record_id = str(item_id or "").partition("/")
    if (
        not separator
        or not collection
        or not record_id
        or collection.strip() != collection
        or record_id.strip() != record_id
    ):
        raise _invalid(
            f"authority item id {item_id!r} must use complete collection/id syntax"
        )
    return collection, record_id


def _validate_expected_new_entities(
    expected: list[dict[str, Any]],
    *,
    authoritative_state: dict[str, Any],
) -> None:
    existing_refs: set[str] = set()
    for collection in AUTHORITATIVE_RECORD_COLLECTIONS:
        records = authoritative_state.get(collection)
        if records is None:
            continue
        if not isinstance(records, dict):
            raise _invalid(
                f"authoritative_state.{collection} must be one JSON object"
            )
        for record_id, raw in records.items():
            existing_refs.add(_identity_key(record_id))
            existing_refs.add(_identity_key(f"{collection}/{record_id}"))
            if not isinstance(raw, dict):
                continue
            for field in (
                "canonical_name",
                "name",
                "location_id",
                "entity_id",
                "character_id",
            ):
                value = str(raw.get(field) or "").strip()
                if value:
                    existing_refs.add(_identity_key(value))
            for alias in raw.get("aliases") or []:
                normalized = str(alias or "").strip()
                if normalized:
                    existing_refs.add(_identity_key(normalized))
    for item in expected:
        identifiers = {
            _identity_key(item["entity_id"]),
            _identity_key(item["display_name"]),
        }
        collisions = sorted(identifier for identifier in identifiers if identifier in existing_refs)
        if collisions:
            raise _invalid(
                f"expected new entity {item['entity_id']!r} collides with "
                "existing authority identity"
            )


def _current_record_view(
    value: dict[str, Any],
    *,
    collection: str,
) -> dict[str, Any]:
    projected = _without_transition_audit(value)
    if not isinstance(projected, dict):
        raise _invalid("current-state authority records must be JSON objects")
    if collection == "characters":
        projected = {
            key: item
            for key, item in projected.items()
            if key not in _CHARACTER_LEGACY_PROJECTION_FIELDS
        }
    return projected


def _scalar_record_metadata(
    records: dict[str, Any],
    *,
    value_fields: set[str],
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for record_id, raw_record in sorted(records.items()):
        if not isinstance(raw_record, dict):
            raise _invalid(
                f"current-state record {record_id!r} must be one JSON object"
            )
        record = {
            key: copy.deepcopy(value)
            for key, value in raw_record.items()
            if key not in value_fields
        }
        if record:
            metadata[record_id] = record
    return metadata


def _scene_record_collection(
    source: dict[str, Any],
    scene_key: str,
) -> dict[str, Any]:
    records = source.get(scene_key)
    if records is None:
        return {}
    if not isinstance(records, dict):
        raise _invalid(f"scene_state.{scene_key} must be one JSON object")
    return records


def _merge_current_scene_record(
    baseline: Any,
    dynamic: Any,
    *,
    collection: str,
    item_id: str,
) -> dict[str, Any]:
    if baseline is not None and not isinstance(baseline, dict):
        raise _invalid(
            f"Generation State View {collection}/{item_id} must be one object"
        )
    if dynamic is not None and not isinstance(dynamic, dict):
        raise _invalid(
            f"scene_state {collection}/{item_id} must be one JSON object"
        )
    base_record = (
        _current_record_view(baseline, collection=collection)
        if isinstance(baseline, dict)
        else {}
    )
    dynamic_record = (
        _current_record_view(dynamic, collection=collection)
        if isinstance(dynamic, dict)
        else {}
    )
    return {
        **base_record,
        **dynamic_record,
    }


def _relationship_scene_key(
    relationship_id: str,
    record: Any,
) -> str:
    if not isinstance(record, dict):
        return str(relationship_id)
    source_id = str(
        record.get("source_character_id") or record.get("source_id") or ""
    ).strip()
    target_id = str(
        record.get("target_character_id") or record.get("target_id") or ""
    ).strip()
    return (
        f"{source_id}->{target_id}"
        if source_id and target_id
        else str(relationship_id)
    )


def _current_numeric_scene_values(
    source: dict[str, Any],
    *,
    scene_key: str,
    records: dict[str, Any],
    value_field: str,
) -> dict[str, int | float]:
    dynamic_values = _scene_record_collection(source, scene_key)
    projected: dict[str, int | float] = {}
    for record_id, record in sorted(records.items()):
        raw_value = (
            dynamic_values[record_id]
            if record_id in dynamic_values
            else (
                record.get(value_field)
                if isinstance(record, dict)
                else None
            )
        )
        if (
            isinstance(raw_value, bool)
            or not isinstance(raw_value, (int, float))
        ):
            raise _invalid(
                f"scene_state.{scene_key}.{record_id} must be numeric"
            )
        projected[record_id] = raw_value
    return projected


def _without_transition_audit(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _without_transition_audit(item)
            for key, item in value.items()
            if str(key) not in _TRANSITION_AND_AUDIT_FIELDS
        }
    if isinstance(value, list):
        return [_without_transition_audit(item) for item in value]
    return copy.deepcopy(value)


def _validated_scene_generation_state_reference(
    value: Any,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(
            "Scene Generation State Reference must be one JSON object"
        )
    reference = copy.deepcopy(value)
    if (
        reference.get("schema_version")
        != SCENE_GENERATION_STATE_REFERENCE_SCHEMA_VERSION
    ):
        raise _invalid(
            "Scene Generation State Reference schema_version is unsupported"
        )
    if reference.get("view_kind") != SCENE_GENERATION_STATE_REFERENCE_KIND:
        raise _invalid(
            "Scene Generation State Reference view_kind is unsupported"
        )
    if reference.get("policy") != SCENE_GENERATION_STATE_REFERENCE_POLICY:
        raise _invalid(
            "Scene Generation State Reference policy is unsupported"
        )
    if (
        isinstance(reference.get("chapter_index"), bool)
        or not isinstance(reference.get("chapter_index"), int)
        or reference["chapter_index"] < 1
    ):
        raise _invalid(
            "Scene Generation State Reference chapter_index is invalid"
        )
    for field in (
        "source_authority_sha256",
        "source_generation_state_view_sha256",
        "read_set_digest",
        "selected_item_ids_sha256",
    ):
        if not _SHA256.fullmatch(
            str(reference.get(field) or "").casefold()
        ):
            raise _invalid(
                f"Scene Generation State Reference {field} is invalid"
            )
    binding = reference.get("current_state_binding")
    if not isinstance(binding, dict):
        raise _invalid(
            "Scene Generation State Reference current_state_binding is invalid"
        )
    if binding.get("payload_field") != "current_scene_state":
        raise _invalid(
            "Scene Generation State Reference payload binding is invalid"
        )
    if binding.get("projection_policy") != "sanitized_dynamic_overlay_v1":
        raise _invalid(
            "Scene Generation State Reference projection policy is invalid"
        )
    counts = binding.get("collection_counts")
    expected_count_keys = {
        "characters",
        "relationships",
        "rosters",
        "locations",
        "inventories",
        "counters",
    }
    if (
        not isinstance(counts, dict)
        or set(counts) != expected_count_keys
        or any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or item < 0
            for item in counts.values()
        )
    ):
        raise _invalid(
            "Scene Generation State Reference collection counts are invalid"
        )
    scalar_metadata = binding.get("scalar_state_metadata")
    if (
        not isinstance(scalar_metadata, dict)
        or set(scalar_metadata)
        != {"locations", "inventories", "counters"}
        or any(
            not isinstance(records, dict)
            for records in scalar_metadata.values()
        )
    ):
        raise _invalid(
            "Scene Generation State Reference scalar metadata is invalid"
        )
    _reject_audit_fields(
        scalar_metadata,
        path="scene_generation_state_reference.scalar_state_metadata",
    )
    events = reference.get("required_events")
    if not isinstance(events, dict):
        raise _invalid(
            "Scene Generation State Reference required_events is invalid"
        )
    for event_id, event in events.items():
        if not isinstance(event_id, str) or not isinstance(event, dict):
            raise _invalid(
                "Scene Generation State Reference events require stable ids"
            )
        if str(event.get("event_id") or event_id).strip() != event_id:
            raise _invalid(
                f"Scene Generation State Reference event {event_id!r} "
                "has a mismatched event_id"
            )
    if not isinstance(reference.get("continuity"), dict):
        raise _invalid(
            "Scene Generation State Reference continuity is invalid"
        )
    if not isinstance(reference.get("narrative_constraints"), list):
        raise _invalid(
            "Scene Generation State Reference constraints are invalid"
        )
    if not isinstance(reference.get("expected_new_entities"), list):
        raise _invalid(
            "Scene Generation State Reference expected entities are invalid"
        )
    reference_sha = str(reference.get("reference_sha256") or "").casefold()
    if not _SHA256.fullmatch(reference_sha):
        raise _invalid(
            "Scene Generation State Reference hash is invalid"
        )
    hash_input = copy.deepcopy(reference)
    hash_input.pop("reference_sha256", None)
    if reference_sha != sha256_text(_canonical_json(hash_input)):
        raise _invalid(
            "Scene Generation State Reference hash does not match"
        )
    reference["reference_sha256"] = reference_sha
    return reference


def _validated_plan_generation_state_projection(
    value: Any,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(
            "Plan Generation State Projection must be one JSON object"
        )
    projection = copy.deepcopy(value)
    if (
        projection.get("schema_version")
        != PLAN_GENERATION_STATE_PROJECTION_SCHEMA_VERSION
    ):
        raise _invalid(
            "Plan Generation State Projection schema_version is unsupported"
        )
    if projection.get("view_kind") != PLAN_GENERATION_STATE_PROJECTION_KIND:
        raise _invalid(
            "Plan Generation State Projection view_kind is unsupported"
        )
    if projection.get("policy") != PLAN_GENERATION_STATE_PROJECTION_POLICY:
        raise _invalid(
            "Plan Generation State Projection policy is unsupported"
        )
    if (
        isinstance(projection.get("chapter_index"), bool)
        or not isinstance(projection.get("chapter_index"), int)
        or projection["chapter_index"] < 1
    ):
        raise _invalid(
            "Plan Generation State Projection chapter_index is invalid"
        )
    for field in (
        "source_authority_sha256",
        "source_generation_state_view_sha256",
        "read_set_digest",
        "selected_item_ids_sha256",
    ):
        if not _SHA256.fullmatch(
            str(projection.get(field) or "").casefold()
        ):
            raise _invalid(
                f"Plan Generation State Projection {field} is invalid"
            )
    current = projection.get("current_state")
    if (
        not isinstance(current, dict)
        or set(current) != set(_CURRENT_STATE_COLLECTIONS)
        or any(not isinstance(records, dict) for records in current.values())
    ):
        raise _invalid(
            "Plan Generation State Projection current_state is invalid"
        )
    _reject_audit_fields(
        current,
        path="plan_generation_state_projection.current_state",
    )
    events = projection.get("required_events")
    if not isinstance(events, dict):
        raise _invalid(
            "Plan Generation State Projection required_events is invalid"
        )
    for event_id, event in events.items():
        if not isinstance(event_id, str) or not isinstance(event, dict):
            raise _invalid(
                "Plan Generation State Projection events require stable ids"
            )
        if str(event.get("event_id") or event_id).strip() != event_id:
            raise _invalid(
                f"Plan Generation State Projection event {event_id!r} "
                "has a mismatched event_id"
            )
    if not isinstance(projection.get("continuity"), dict):
        raise _invalid(
            "Plan Generation State Projection continuity is invalid"
        )
    if not isinstance(projection.get("narrative_constraints"), list):
        raise _invalid(
            "Plan Generation State Projection constraints are invalid"
        )
    if not isinstance(projection.get("expected_new_entities"), list):
        raise _invalid(
            "Plan Generation State Projection expected entities are invalid"
        )
    projection_sha = str(
        projection.get("projection_sha256") or ""
    ).casefold()
    if not _SHA256.fullmatch(projection_sha):
        raise _invalid("Plan Generation State Projection hash is invalid")
    hash_input = copy.deepcopy(projection)
    hash_input.pop("projection_sha256", None)
    if projection_sha != sha256_text(_canonical_json(hash_input)):
        raise _invalid(
            "Plan Generation State Projection hash does not match"
        )
    projection["projection_sha256"] = projection_sha
    return projection


def _validated_view(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid("Generation State View must be one JSON object")
    view = copy.deepcopy(value)
    if view.get("schema_version") != GENERATION_STATE_VIEW_SCHEMA_VERSION:
        raise _invalid("Generation State View schema_version is unsupported")
    if view.get("view_kind") != GENERATION_STATE_VIEW_KIND:
        raise _invalid("Generation State View view_kind is unsupported")
    if view.get("policy") != GENERATION_STATE_VIEW_POLICY:
        raise _invalid("Generation State View policy is unsupported")
    source_sha = str(view.get("source_authority_sha256") or "").casefold()
    if not _SHA256.fullmatch(source_sha):
        raise _invalid("Generation State View source authority hash is invalid")

    read_set = _normalize_read_set(view.get("chapter_context_read_set") or {})
    if view.get("chapter_context_read_set") != read_set:
        raise _invalid(
            "Generation State View chapter_context_read_set is not canonical"
        )
    if view.get("chapter_index") != read_set["chapter_index"]:
        raise _invalid("Generation State View chapter_index does not match")
    expected_read_set_digest = read_set["contract_sha256"]
    if view.get("read_set_digest") != expected_read_set_digest:
        raise _invalid("Generation State View read_set_digest does not match")
    if view.get("continuity") != read_set["continuity"]:
        raise _invalid("Generation State View continuity does not match its read set")
    if (
        view.get("narrative_constraints")
        != read_set["narrative_constraints"]
    ):
        raise _invalid(
            "Generation State View narrative constraints do not match its read set"
        )
    if view.get("expected_new_entities") != read_set["expected_new_entities"]:
        raise _invalid(
            "Generation State View expected new entities do not match its read set"
        )

    current = view.get("current_state")
    if not isinstance(current, dict) or set(current) != set(
        _CURRENT_STATE_COLLECTIONS
    ):
        raise _invalid(
            "Generation State View current_state collections are incomplete"
        )
    selected_state_ids: list[str] = []
    for collection in _CURRENT_STATE_COLLECTIONS:
        records = current.get(collection)
        if not isinstance(records, dict):
            raise _invalid(
                f"Generation State View current_state.{collection} must be "
                "one JSON object"
            )
        for record_id, record in sorted(records.items()):
            if not isinstance(record_id, str) or not record_id.strip():
                raise _invalid("Generation State View record ids must be strings")
            if not isinstance(record, dict):
                raise _invalid(
                    f"Generation State View record {collection}/{record_id} "
                    "must be one JSON object"
                )
            _reject_audit_fields(record, path=f"{collection}/{record_id}")
            selected_state_ids.append(f"{collection}/{record_id}")
    selected_state_ids.sort()

    events = view.get("required_events")
    if not isinstance(events, dict):
        raise _invalid("Generation State View required_events must be one object")
    for event_id, event in events.items():
        if (
            not isinstance(event_id, str)
            or not event_id.strip()
            or not isinstance(event, dict)
        ):
            raise _invalid(
                "Generation State View historical events require stable string "
                "ids and JSON-object records"
            )
        declared_event_id = str(event.get("event_id") or event_id).strip()
        if declared_event_id != event_id:
            raise _invalid(
                f"Generation State View event {event_id!r} has a mismatched "
                "event_id"
            )
    selected_event_ids = sorted(f"events/{event_id}" for event_id in events)
    if selected_state_ids != read_set["required_state_item_ids"]:
        raise _invalid(
            "Generation State View current state does not match its read set"
        )
    if selected_event_ids != read_set["required_event_item_ids"]:
        raise _invalid(
            "Generation State View historical events do not match its read set"
        )
    if view.get("selected_state_item_ids") != selected_state_ids:
        raise _invalid("Generation State View selected state ids do not match")
    if view.get("selected_event_item_ids") != selected_event_ids:
        raise _invalid("Generation State View selected event ids do not match")

    selected_item_ids = [*selected_state_ids, *selected_event_ids]
    if view.get("selected_item_ids_sha256") != sha256_text(
        _canonical_json(selected_item_ids)
    ):
        raise _invalid("Generation State View selected item hash does not match")

    projection_sha = str(view.get("projection_sha256") or "").casefold()
    if not _SHA256.fullmatch(projection_sha):
        raise _invalid("Generation State View projection hash is invalid")
    hash_input = copy.deepcopy(view)
    hash_input.pop("projection_sha256", None)
    if projection_sha != sha256_text(_canonical_json(hash_input)):
        raise _invalid("Generation State View projection hash does not match")
    view["source_authority_sha256"] = source_sha
    return view


def _reject_audit_fields(value: Any, *, path: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in _TRANSITION_AND_AUDIT_FIELDS:
                raise _invalid(
                    f"Generation State View current record {path!r} exposes "
                    f"audit field {key!r}"
                )
            _reject_audit_fields(item, path=path)
    elif isinstance(value, list):
        for item in value:
            _reject_audit_fields(item, path=path)


def _location_is_confirmed(record: dict[str, Any]) -> bool:
    raw = record.get("certainty")
    if raw in (None, ""):
        return True
    return str(raw).strip().casefold() in _CONFIRMED_LOCATION_CERTAINTIES


def _bounded_world_scalars(value: dict[str, Any]) -> dict[str, Any]:
    excluded = {
        "last_world_changes",
        "locations",
        "path",
        "relative_path",
        "source",
        "summary",
        "text",
        "truncated",
    }
    projected: dict[str, Any] = {}
    for key, item in value.items():
        if key in excluded:
            continue
        if isinstance(item, (bool, int, float)) or item is None:
            projected[str(key)] = copy.deepcopy(item)
        elif isinstance(item, str) and len(item) <= 1_000:
            projected[str(key)] = item
    return projected


def _project_spatial_state(
    value: dict[str, Any],
    *,
    character_ids: set[str],
    location_entity_ids: set[str],
    relevant_location_ids: set[str],
) -> dict[str, Any]:
    spaces = value.get("spaces") if isinstance(value.get("spaces"), dict) else {}
    positions = (
        value.get("character_positions")
        if isinstance(value.get("character_positions"), dict)
        else {}
    )
    allowed_position_ids = character_ids | location_entity_ids
    connections = [
        copy.deepcopy(item)
        for item in value.get("connections") or []
        if _connection_within_locations(
            item,
            relevant_location_ids=relevant_location_ids,
        )
    ]
    blocked_paths = [
        copy.deepcopy(item)
        for item in value.get("blocked_paths") or []
        if _connection_within_locations(
            item,
            relevant_location_ids=relevant_location_ids,
        )
    ]
    last_transition = value.get("last_transition")
    return {
        "spaces": {
            location_id: copy.deepcopy(spaces.get(location_id) or {})
            for location_id in sorted(relevant_location_ids)
        },
        "connections": connections,
        "character_positions": {
            entity_id: copy.deepcopy(positions[entity_id])
            for entity_id in sorted(allowed_position_ids)
            if entity_id in positions
        },
        "blocked_paths": blocked_paths,
        "last_transition": (
            copy.deepcopy(last_transition)
            if _connection_within_locations(
                last_transition,
                relevant_location_ids=relevant_location_ids,
            )
            else {}
        ),
    }


def _connection_within_locations(
    value: Any,
    *,
    relevant_location_ids: set[str],
) -> bool:
    if not isinstance(value, dict):
        return False
    endpoints = {
        str(value.get(key) or "").strip()
        for key in ("from", "to", "source", "target")
        if str(value.get(key) or "").strip()
    }
    return bool(endpoints) and endpoints.issubset(relevant_location_ids)


def _working_character_names(
    characters: dict[str, Any],
    *,
    continuity: dict[str, Any],
) -> list[str]:
    values = {
        str(item).strip()
        for item in continuity.get("last_scene_character_ids") or []
        if str(item).strip()
    }
    for character_id, record in characters.items():
        values.add(str(character_id))
        if not isinstance(record, dict):
            continue
        for raw in (
            record.get("canonical_name"),
            record.get("name"),
            *(record.get("aliases") or []),
        ):
            normalized = str(raw or "").strip()
            if normalized:
                values.add(normalized)
    return sorted(values)


def _overlay_snapshot_records(
    target: dict[str, Any],
    records: dict[str, Any],
    *,
    field_name: str,
    excluded_fields: set[str] | None = None,
) -> None:
    for record_id, raw_record in records.items():
        if not isinstance(raw_record, dict):
            raise _invalid(
                f"Generation State View {field_name}/{record_id} must be "
                "one JSON object"
            )
        existing = target.get(record_id)
        if existing is not None and not isinstance(existing, dict):
            raise _invalid(
                f"snapshot.{field_name}.{record_id} must be one JSON object"
            )
        incoming = {
            key: copy.deepcopy(value)
            for key, value in raw_record.items()
            if key not in (excluded_fields or set())
        }
        target[record_id] = {
            **copy.deepcopy(existing or {}),
            **incoming,
        }


def _value_references_any(value: Any, *, allowed_values: set[str]) -> bool:
    if not allowed_values:
        return False
    if isinstance(value, str):
        return value.strip() in allowed_values
    if isinstance(value, dict):
        return any(
            _value_references_any(item, allowed_values=allowed_values)
            for item in value.values()
        )
    if isinstance(value, list):
        return any(
            _value_references_any(item, allowed_values=allowed_values)
            for item in value
        )
    return False


def _identity_key(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise _invalid("Generation State View values must be JSON serializable") from exc


def _invalid(message: str) -> StructuredContextError:
    return StructuredContextError("generation_state_view_invalid", message)


__all__ = [
    "GENERATION_STATE_VIEW_HEADING",
    "GENERATION_STATE_VIEW_KIND",
    "GENERATION_STATE_VIEW_POLICY",
    "GENERATION_STATE_VIEW_SCHEMA_VERSION",
    "PLAN_GENERATION_STATE_PROJECTION_HEADING",
    "PLAN_GENERATION_STATE_PROJECTION_KIND",
    "PLAN_GENERATION_STATE_PROJECTION_POLICY",
    "PLAN_GENERATION_STATE_PROJECTION_SCHEMA_VERSION",
    "SCENE_GENERATION_STATE_REFERENCE_HEADING",
    "SCENE_GENERATION_STATE_REFERENCE_KIND",
    "SCENE_GENERATION_STATE_REFERENCE_POLICY",
    "SCENE_GENERATION_STATE_REFERENCE_SCHEMA_VERSION",
    "apply_generation_state_view_to_snapshot",
    "build_plan_generation_state_projection",
    "build_scene_generation_state_reference",
    "build_generation_state_view",
    "filter_scene_state_for_generation",
    "generation_state_view_from_markdown",
    "project_snapshot_for_generation",
    "scene_generation_state_reference_from_markdown",
    "validate_plan_generation_state_projection",
]
