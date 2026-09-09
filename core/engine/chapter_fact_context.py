from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable, Mapping

from core.engine.chapter_candidate_context import (
    CHAPTER_CANDIDATE_CONTEXT_VERSION,
    TrustedChapterCandidateContext,
)
from core.memory_v2.canonical import canonical_json_hash
from core.memory_v2.event_store import memory_projection_hash
from core.memory_v2.validator import validate_canonical_memory
from core.state.authoritative import validate_authoritative_state
from core.story_project.authority import AUTHORITY_MODE_EVENT, AUTHORITY_MODE_LEGACY
from core.story_project.reviewed_state_context import validated_reviewed_state_context
from core.story_project.runtime import RUNTIME_CACHE_POLICY_BYPASS
from core.story_project.semantic_contracts import validate_story_project_semantic_state


CHAPTER_FACT_CONTEXT_SCHEMA_VERSION = "1.0"
CHAPTER_FACT_CONTEXT_KIND = "chapter_fact_authority_view"
CHAPTER_FACT_CONTEXT_POLICY = "global_entity_directory_prose_match_v1"
CHAPTER_FACT_CONTEXT_MAX_CHARS = 100_000
CHAPTER_FACT_CONTEXT_MAX_PROSE_CHARS = 200_000

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASCII_REFERENCE = re.compile(r"^[A-Za-z0-9_.:\-/ ]+$")
_ENTITY_KIND_ORDER = {
    "character": 0,
    "location": 1,
    "item": 2,
    "inventory": 3,
    "relationship": 4,
    "roster": 5,
    "counter": 6,
    "injury": 7,
    "resource": 8,
    "glossary": 9,
    "foreshadowing": 10,
    "entity": 11,
    "event": 12,
}


class ChapterFactContextError(ValueError):
    code = "chapter_fact_context_invalid"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(f"{self.code}: {message}")


@dataclass(frozen=True)
class ChapterFactContextView:
    """Immutable-by-interface, canonical JSON view used for fact extraction."""

    _value: dict[str, Any] = field(repr=False)
    text: str
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._value)

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass
class _Entity:
    entity_id: str
    kind: str
    names: list[tuple[int, str]] = field(default_factory=list)
    aliases: set[str] = field(default_factory=set)
    current_state: dict[str, Any] = field(default_factory=dict)

    def add_identity(
        self,
        *,
        name: Any = None,
        aliases: Any = None,
        priority: int = 0,
    ) -> None:
        normalized_name = _optional_identity_text(name)
        if normalized_name is not None:
            self.names.append((priority, normalized_name))
            self.aliases.add(normalized_name)
        for alias in _identity_strings(aliases):
            self.aliases.add(alias)

    def add_state(self, source: str, value: Any) -> None:
        checked_source = _required_text(source, "state source", maximum=500)
        copied = _json_copy(value, f"current state {checked_source}")
        existing = self.current_state.get(checked_source)
        if existing is not None and existing != copied:
            raise ChapterFactContextError(
                f"conflicting current state source {checked_source!r} for {self.entity_id!r}"
            )
        self.current_state[checked_source] = copied

    def primary_name(self) -> str:
        if not self.names:
            return self.entity_id
        return sorted(
            self.names,
            key=lambda item: (-item[0], _normalized_reference(item[1]), item[1]),
        )[0][1]


class _EntityIndex:
    def __init__(self) -> None:
        self._entities: dict[tuple[str, str], _Entity] = {}

    def register(
        self,
        kind: str,
        entity_id: Any,
        *,
        name: Any = None,
        aliases: Any = None,
        priority: int = 0,
        source: str | None = None,
        state: Any = None,
    ) -> _Entity:
        checked_kind = _required_text(kind, "entity kind", maximum=50)
        checked_id = _required_text(entity_id, "entity_id", maximum=300)
        key = (checked_kind, checked_id)
        entity = self._entities.setdefault(
            key,
            _Entity(entity_id=checked_id, kind=checked_kind),
        )
        entity.add_identity(name=name, aliases=aliases, priority=priority)
        if source is not None:
            entity.add_state(source, state)
        return entity

    def attach_by_id(self, entity_id: Any, source: str, state: Any) -> None:
        checked_id = _optional_identity_text(entity_id)
        if checked_id is None:
            return
        matches = [
            entity
            for (_kind, candidate_id), entity in self._entities.items()
            if candidate_id == checked_id
        ]
        if not matches:
            matches = [self.register("entity", checked_id, name=checked_id)]
        for entity in matches:
            entity.add_state(source, state)

    def sorted_entities(self) -> list[_Entity]:
        return sorted(
            self._entities.values(),
            key=lambda entity: (
                _ENTITY_KIND_ORDER.get(entity.kind, 99),
                _normalized_reference(entity.entity_id),
                entity.entity_id,
                entity.kind,
            ),
        )


def build_chapter_fact_context(
    trusted_context: TrustedChapterCandidateContext,
    chapter_prose: str,
    *,
    max_chars: int = CHAPTER_FACT_CONTEXT_MAX_CHARS,
) -> ChapterFactContextView:
    """Build a deterministic, read-only authority view for fact extraction.

    The chapter prose is used only as a relevance query.  It never contributes
    authority values.  Legacy projects read their activated semantic state;
    event-authority projects read the replayed ``memory_v2.projection`` already
    pinned by ``TrustedChapterCandidateContext``.
    """

    limit = _validated_budget(max_chars)
    prose = _validated_prose(chapter_prose)
    context = validate_trusted_chapter_context(trusted_context)
    identity = context["project_identity"]
    authority = identity.get("authority")
    authority_mode = (
        str(authority.get("mode"))
        if isinstance(authority, dict)
        else AUTHORITY_MODE_LEGACY
    )

    index = _EntityIndex()
    if authority_mode == AUTHORITY_MODE_EVENT:
        if "reviewed_state" in context:
            raise ChapterFactContextError("a reviewed legacy view cannot be adopted by event authority",
                                          code="chapter_fact_context_authority_unsupported")
        source_metadata = _index_event_authority(
            index,
            context=context,
            trusted_context=trusted_context,
        )
    elif authority_mode == AUTHORITY_MODE_LEGACY:
        source_metadata = _index_legacy_authority(
            index,
            context=context,
            trusted_context=trusted_context,
        )
    else:
        raise ChapterFactContextError(
            f"unsupported authority mode {authority_mode!r}",
            code="chapter_fact_context_authority_unsupported",
        )

    entities = index.sorted_entities()
    directory = [_directory_entry(entity) for entity in entities]
    matched = [
        (entity, matches)
        for entity in entities
        if (matches := _matched_references(entity, prose))
    ]
    details = [_detail_entry(entity, matches) for entity, matches in matched]
    authority_binding = _authority_binding(
        trusted_context,
        identity=identity,
        authority_mode=authority_mode,
        source_metadata=source_metadata,
    )
    payload: dict[str, Any] = {
        "schema_version": CHAPTER_FACT_CONTEXT_SCHEMA_VERSION,
        "view_kind": CHAPTER_FACT_CONTEXT_KIND,
        "policy": CHAPTER_FACT_CONTEXT_POLICY,
        "chapter_index": trusted_context.chapter_index,
        "chapter_prose_sha256": _sha256_text(prose),
        "authority_binding": authority_binding,
        "entity_directory": directory,
        "matched_entity_refs": [
            {"entity_id": entity.entity_id, "kind": entity.kind}
            for entity, _matches in matched
        ],
        "current_state_details": details,
        "selection": {
            "authority_source": source_metadata["authority_source"],
            "directory_count": len(directory),
            "matched_count": len(details),
            "details_complete": True,
            "prose_is_authority": False,
        },
    }
    payload["view_sha256"] = canonical_json_hash(
        payload,
        exclude_environment_fields=False,
    )
    text = _canonical_json(payload)
    if len(text) > limit:
        raise ChapterFactContextError(
            f"complete fact authority view requires {len(text)} characters; budget is {limit}",
            code="chapter_fact_context_budget_exceeded",
        )
    return ChapterFactContextView(
        _value=copy.deepcopy(payload),
        text=text,
        sha256=str(payload["view_sha256"]),
    )


def validate_trusted_chapter_context(
    value: TrustedChapterCandidateContext,
) -> dict[str, Any]:
    """Verify the input envelope independently of prompt indexing/budget."""
    if not isinstance(value, TrustedChapterCandidateContext):
        raise ChapterFactContextError(
            "trusted_context must be a TrustedChapterCandidateContext; raw or unknown contexts are rejected"
        )
    if isinstance(value.chapter_index, bool) or not isinstance(value.chapter_index, int) or value.chapter_index < 1:
        raise ChapterFactContextError("trusted context chapter_index must be positive")
    _require_sha256(value.context_sha256, "context_sha256")
    context = _json_object(value.story_project_context, "story_project_context")
    snapshot = _json_object(value.snapshot, "snapshot")
    memory_context = _json_object(value.memory_context, "memory_context")
    expected_context_sha256 = canonical_json_hash(
        {
            "context_builder_version": CHAPTER_CANDIDATE_CONTEXT_VERSION,
            "runtime_cache_policy": RUNTIME_CACHE_POLICY_BYPASS,
            "story_project_context": context,
            "snapshot": snapshot,
            "memory_context": memory_context,
        },
        exclude_environment_fields=False,
    )
    if expected_context_sha256 != value.context_sha256:
        raise ChapterFactContextError(
            "trusted context payload no longer matches context_sha256",
            code="chapter_fact_context_binding_mismatch",
        )
    if context.get("chapter_index") != value.chapter_index:
        raise ChapterFactContextError(
            "StoryProject context is bound to another chapter",
            code="chapter_fact_context_binding_mismatch",
        )
    if context.get("story_state_mode") != "strict":
        raise ChapterFactContextError(
            "fact extraction requires an activated strict authority context",
            code="chapter_fact_context_not_strict",
        )
    identity = context.get("project_identity")
    if not isinstance(identity, dict):
        raise ChapterFactContextError("StoryProject context has no project identity")
    _required_text(identity.get("book_id"), "project_identity.book_id", maximum=500)
    if identity.get("ephemeral") is not False:
        raise ChapterFactContextError("fact extraction requires a persisted project identity")
    return context


def _index_legacy_authority(
    index: _EntityIndex,
    *,
    context: Mapping[str, Any],
    trusted_context: TrustedChapterCandidateContext,
) -> dict[str, Any]:
    if trusted_context.authority_projection_sha256 is not None:
        raise ChapterFactContextError(
            "legacy context unexpectedly contains an event projection binding",
            code="chapter_fact_context_binding_mismatch",
        )
    memory_v2 = context.get("memory_v2")
    if not isinstance(memory_v2, dict) or any(
        memory_v2.get(field) is not None for field in ("projection", "projection_hash")
    ):
        raise ChapterFactContextError(
            "legacy authority must not adopt a memory_v2 projection",
            code="chapter_fact_context_binding_mismatch",
        )
    audit = context.get("semantic_audit")
    if not isinstance(audit, dict) or audit.get("authoritative") is not True:
        raise ChapterFactContextError(
            "legacy semantic state is not authoritative",
            code="chapter_fact_context_not_strict",
        )
    try:
        semantic = validate_story_project_semantic_state(context.get("semantic_state"))
    except (TypeError, ValueError) as exc:
        raise ChapterFactContextError(
            f"legacy semantic authority is invalid: {exc}"
        ) from exc
    identity = context["project_identity"]
    if (
        semantic.get("book_id") != identity.get("book_id")
        or semantic.get("chapter_index") != trusted_context.chapter_index
    ):
        raise ChapterFactContextError(
            "legacy semantic state belongs to another book or chapter",
            code="chapter_fact_context_binding_mismatch",
        )
    if audit.get("source_digest") != semantic.get("source_digest"):
        raise ChapterFactContextError(
            "legacy semantic audit does not bind the semantic source digest",
            code="chapter_fact_context_binding_mismatch",
        )

    try:
        reviewed = validated_reviewed_state_context(context)
    except (TypeError, ValueError) as exc:
        raise ChapterFactContextError(
            f"reviewed state authority is invalid: {exc}",
            code="chapter_fact_context_binding_mismatch",
        ) from exc
    if reviewed is not None:
        authoritative = reviewed["authoritative_state"]
        source_prefix = "reviewed_state.authoritative_state"
        _index_authoritative_state(index, authoritative, source_prefix=source_prefix)
        _index_reviewed_events(index, authoritative, source_prefix=source_prefix)
        return {
            "authority_source": "legacy_reviewed_state",
            "semantic_source_digest": str(semantic["source_digest"]),
            "semantic_schema_version": str(semantic["schema_version"]),
            "semantic_parser_version": str(semantic["parser_version"]),
            "projection_sha256": None,
            "reviewed_source_sha256": reviewed["source_sha256"],
            "reviewed_payload_sha256": reviewed["payload_sha256"],
            "reviewed_manual_sources_sha256": reviewed["manual_sources_sha256"],
        }

    positions = _mapping((semantic.get("spatial_state") or {}).get("character_positions"))
    for character_id, raw in _sorted_mapping(semantic.get("characters")):
        record = _object_or_empty(raw)
        name = record.get("name") or record.get("canonical_name") or character_id
        entity = index.register(
            "character",
            character_id,
            name=name,
            aliases=record.get("aliases"),
            priority=100,
            source=f"semantic_state.characters.{character_id}",
            state=record,
        )
        position = positions.get(str(name), positions.get(character_id, record.get("location")))
        if position not in (None, ""):
            entity.add_state("semantic_state.spatial_state.current_location", position)
            index.register(
                "location",
                position,
                name=position,
                priority=20,
            )

    world_locations = _mapping((semantic.get("world_state") or {}).get("locations"))
    for location_id, raw in _sorted_mapping(world_locations):
        index.register(
            "location",
            location_id,
            name=location_id,
            priority=50,
            source=f"semantic_state.world_state.locations.{location_id}",
            state=raw,
        )
        if isinstance(raw, dict):
            for child_id, child_state in _sorted_mapping(raw):
                index.register(
                    "location",
                    child_id,
                    name=child_id,
                    priority=10,
                    source=(
                        f"semantic_state.world_state.locations.{location_id}.{child_id}"
                    ),
                    state=child_state,
                )

    for _character, location in _sorted_mapping(positions):
        if location not in (None, ""):
            index.register("location", location, name=location, priority=20)
    current_location = (semantic.get("story_state") or {}).get("current_location")
    if current_location not in (None, ""):
        index.register(
            "location",
            current_location,
            name=current_location,
            priority=20,
            source="semantic_state.story_state.current_location",
            state=current_location,
        )
    return {
        "authority_source": "legacy_semantic_state",
        "semantic_source_digest": str(semantic["source_digest"]),
        "semantic_schema_version": str(semantic["schema_version"]),
        "semantic_parser_version": str(semantic["parser_version"]),
        "projection_sha256": None,
    }


def _index_event_authority(
    index: _EntityIndex,
    *,
    context: Mapping[str, Any],
    trusted_context: TrustedChapterCandidateContext,
) -> dict[str, Any]:
    identity = context["project_identity"]
    authority = identity.get("authority")
    memory_v2 = context.get("memory_v2")
    if not isinstance(authority, dict) or not isinstance(memory_v2, dict):
        raise ChapterFactContextError("event authority metadata is incomplete")
    projection = memory_v2.get("projection")
    try:
        checked_projection = validate_canonical_memory(projection)
    except (TypeError, ValueError) as exc:
        raise ChapterFactContextError(
            f"event authority projection is invalid: {exc}"
        ) from exc
    actual_hash = memory_projection_hash(checked_projection)
    expected_hash = trusted_context.authority_projection_sha256
    if (
        not isinstance(expected_hash, str)
        or actual_hash != expected_hash
        or memory_v2.get("projection_hash") != expected_hash
    ):
        raise ChapterFactContextError(
            "event authority projection does not match its trusted binding",
            code="chapter_fact_context_binding_mismatch",
        )
    if (
        checked_projection.get("book_id") != identity.get("book_id")
        or checked_projection.get("authority_epoch") != authority.get("authority_epoch")
        or checked_projection.get("head_event_hash") != authority.get("head_event_hash")
    ):
        raise ChapterFactContextError(
            "event projection differs from ProjectIdentity authority",
            code="chapter_fact_context_binding_mismatch",
        )

    _index_typed_projection(index, checked_projection)
    authoritative = checked_projection.get("authoritative_state")
    if isinstance(authoritative, dict):
        try:
            authoritative = validate_authoritative_state(authoritative)
        except ValueError as exc:
            raise ChapterFactContextError(
                f"event authoritative_state is invalid: {exc}"
            ) from exc
        _index_authoritative_state(index, authoritative)
    return {
        "authority_source": "memory_v2_projection",
        "semantic_source_digest": None,
        "semantic_schema_version": None,
        "semantic_parser_version": None,
        "projection_sha256": actual_hash,
        "projection_schema_version": str(checked_projection["schema_version"]),
        "projection_revision": int(checked_projection["revision"]),
    }


def _index_typed_projection(index: _EntityIndex, projection: Mapping[str, Any]) -> None:
    for character_id, raw in _sorted_mapping(projection.get("characters")):
        record = _object_or_empty(raw)
        index.register(
            "character",
            character_id,
            name=record.get("name") or character_id,
            aliases=_record_aliases(record),
            priority=60,
            source=f"memory_v2.projection.characters.{character_id}",
            state=record,
        )
    for location_id, raw in _sorted_mapping(projection.get("locations")):
        record = _object_or_empty(raw)
        index.register(
            "location",
            location_id,
            name=record.get("name") or location_id,
            aliases=_record_aliases(record),
            priority=60,
            source=f"memory_v2.projection.locations.{location_id}",
            state=record,
        )
    for relationship_id, raw in _sorted_mapping(projection.get("relationships")):
        record = _object_or_empty(raw)
        source = f"memory_v2.projection.relationships.{relationship_id}"
        index.register(
            "relationship",
            relationship_id,
            name=record.get("name") or record.get("kind") or relationship_id,
            aliases=_record_aliases(record),
            priority=50,
            source=source,
            state=record,
        )
        index.attach_by_id(record.get("source_character_id"), source, record)
        index.attach_by_id(record.get("target_character_id"), source, record)
    for injury_id, raw in _sorted_mapping(projection.get("injuries")):
        record = _object_or_empty(raw)
        source = f"memory_v2.projection.injuries.{injury_id}"
        index.register(
            "injury",
            injury_id,
            name=record.get("description") or injury_id,
            priority=40,
            source=source,
            state=record,
        )
        index.attach_by_id(record.get("character_id"), source, record)
    for inventory_id, raw in _sorted_mapping(projection.get("inventories")):
        record = _object_or_empty(raw)
        source = f"memory_v2.projection.inventories.{inventory_id}"
        index.register(
            "inventory",
            inventory_id,
            name=record.get("name") or inventory_id,
            aliases=_record_aliases(record),
            priority=40,
            source=source,
            state=record,
        )
        index.attach_by_id(record.get("owner_id"), source, record)
        for item_id, item in _sorted_mapping(record.get("items")):
            item_record = _object_or_empty(item)
            index.register(
                "item",
                item_id,
                name=item_record.get("name") or item_id,
                aliases=_record_aliases(item_record),
                priority=60,
                source=f"{source}.items.{item_id}",
                state={
                    "inventory_id": inventory_id,
                    "owner_id": record.get("owner_id"),
                    **item_record,
                },
            )
    for resource_id, raw in _sorted_mapping(projection.get("resources")):
        record = _object_or_empty(raw)
        index.register(
            "resource",
            resource_id,
            name=record.get("name") or resource_id,
            aliases=_record_aliases(record),
            priority=40,
            source=f"memory_v2.projection.resources.{resource_id}",
            state=record,
        )
    for glossary_id, raw in _sorted_mapping(projection.get("glossary")):
        record = _object_or_empty(raw)
        index.register(
            "glossary",
            glossary_id,
            name=record.get("term") or glossary_id,
            aliases=_record_aliases(record),
            priority=40,
            source=f"memory_v2.projection.glossary.{glossary_id}",
            state=record,
        )
    for counter_id, raw in _sorted_mapping(projection.get("corruption")):
        record = _object_or_empty(raw)
        source = f"memory_v2.projection.corruption.{counter_id}"
        index.register(
            "counter",
            counter_id,
            name=record.get("name") or counter_id,
            aliases=_record_aliases(record),
            priority=30,
            source=source,
            state=record,
        )
        index.attach_by_id(record.get("subject_id"), source, record)
    for thread_id, raw in _sorted_mapping(projection.get("foreshadowing")):
        record = _object_or_empty(raw)
        index.register(
            "foreshadowing",
            thread_id,
            name=record.get("description") or thread_id,
            aliases=_record_aliases(record),
            priority=30,
            source=f"memory_v2.projection.foreshadowing.{thread_id}",
            state=record,
        )

    current = _mapping(projection.get("current_state"))
    spatial = _mapping(current.get("spatial_state"))
    for character_id, location_id in _sorted_mapping(spatial.get("character_positions")):
        index.attach_by_id(
            character_id,
            "memory_v2.projection.current_state.spatial_state.character_positions",
            location_id,
        )
        if location_id not in (None, ""):
            index.register("location", location_id, name=location_id, priority=20)


def _index_authoritative_state(
    index: _EntityIndex,
    state: Mapping[str, Any],
    *,
    source_prefix: str = "memory_v2.projection.authoritative_state",
) -> None:
    for character_id, raw in _sorted_mapping(state.get("characters")):
        record = _object_or_empty(raw)
        index.register(
            "character",
            character_id,
            name=record.get("canonical_name") or record.get("name") or character_id,
            aliases=record.get("aliases"),
            priority=100,
            source=f"{source_prefix}.characters.{character_id}",
            state=record,
        )
    for relationship_id, raw in _sorted_mapping(state.get("relationships")):
        record = _object_or_empty(raw)
        source = f"{source_prefix}.relationships.{relationship_id}"
        index.register(
            "relationship",
            relationship_id,
            name=record.get("name") or record.get("type") or relationship_id,
            aliases=record.get("aliases"),
            priority=100,
            source=source,
            state=record,
        )
        index.attach_by_id(
            record.get("source_character_id") or record.get("source_id"), source, record
        )
        index.attach_by_id(
            record.get("target_character_id") or record.get("target_id"), source, record
        )
    for roster_id, raw in _sorted_mapping(state.get("roster")):
        record = _object_or_empty(raw)
        source = f"{source_prefix}.roster.{roster_id}"
        index.register(
            "roster",
            roster_id,
            name=record.get("name") or record.get("roster_name") or roster_id,
            aliases=record.get("aliases"),
            priority=100,
            source=source,
            state=record,
        )
        for member_id in _roster_member_ids(record):
            index.attach_by_id(member_id, source, record)
    for counter_id, raw in _sorted_mapping(state.get("numeric_counters")):
        record = _object_or_empty(raw)
        source = f"{source_prefix}.numeric_counters.{counter_id}"
        index.register(
            "counter",
            counter_id,
            name=record.get("name") or record.get("label") or counter_id,
            aliases=record.get("aliases"),
            priority=100,
            source=source,
            state=record,
        )
        index.attach_by_id(record.get("owner_id"), source, record)
    for inventory_id, raw in _sorted_mapping(state.get("inventory")):
        record = _object_or_empty(raw)
        item_id = record.get("item_id") or inventory_id
        source = f"{source_prefix}.inventory.{inventory_id}"
        index.register(
            "item",
            item_id,
            name=record.get("name") or record.get("item_name") or item_id,
            aliases=record.get("aliases"),
            priority=100,
            source=source,
            state=record,
        )
        index.attach_by_id(record.get("owner_id"), source, record)
    for entity_id, raw in _sorted_mapping(state.get("locations")):
        record = _object_or_empty(raw)
        source = f"{source_prefix}.locations.{entity_id}"
        index.attach_by_id(entity_id, source, record)
        location_id = record.get("location_id")
        if location_id not in (None, ""):
            index.register("location", location_id, name=location_id, priority=80)


def _index_reviewed_events(
    index: _EntityIndex,
    state: Mapping[str, Any],
    *,
    source_prefix: str,
) -> None:
    """Expose reviewed event roles without inferring presence or movement from them."""
    for event_id, raw in _sorted_mapping(state.get("events")):
        record = _object_or_empty(raw)
        source = f"{source_prefix}.events.{event_id}"
        index.register("event", event_id, name=event_id, source=source, state=record)
        location_id = record.get("occurrence_location")
        if location_id not in (None, ""):
            index.register("location", location_id, name=location_id, priority=80)
            index.attach_by_id(location_id, source, record)
        for field_name in ("onsite_participants", "remote_participants", "related_entities"):
            participants = record.get(field_name)
            if not isinstance(participants, list):
                continue
            for participant in participants:
                if isinstance(participant, dict):
                    index.attach_by_id(participant.get("entity_id"), source, record)
        transported_items = record.get("transported_items")
        if not isinstance(transported_items, list):
            continue
        for item in transported_items:
            if not isinstance(item, dict):
                continue
            for field_name in ("item_id", "carrier_id"):
                index.attach_by_id(item.get(field_name), source, record)
            for field_name in ("from_location", "to_location"):
                location_id = item.get(field_name)
                if location_id not in (None, ""):
                    index.register("location", location_id, name=location_id, priority=80)
                    index.attach_by_id(location_id, source, record)


def _authority_binding(
    trusted_context: TrustedChapterCandidateContext,
    *,
    identity: Mapping[str, Any],
    authority_mode: str,
    source_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    authority = identity.get("authority") if isinstance(identity.get("authority"), dict) else {}
    binding = {
        "book_id": str(identity["book_id"]),
        "mode": authority_mode,
        "authority_epoch": int(authority.get("authority_epoch") or 0),
        "head_event_hash": authority.get("head_event_hash"),
        "context_sha256": trusted_context.context_sha256,
        "authority_projection_sha256": trusted_context.authority_projection_sha256,
        "semantic_source_digest": source_metadata.get("semantic_source_digest"),
        "projection_revision": source_metadata.get("projection_revision"),
    }
    for field_name in (
        "reviewed_source_sha256",
        "reviewed_payload_sha256",
        "reviewed_manual_sources_sha256",
    ):
        if field_name in source_metadata:
            binding[field_name] = source_metadata[field_name]
    return binding


def _directory_entry(entity: _Entity) -> dict[str, Any]:
    name = entity.primary_name()
    aliases = _sorted_identity_strings(
        alias for alias in entity.aliases if _normalized_reference(alias) != _normalized_reference(name)
    )
    return {
        "entity_id": entity.entity_id,
        "kind": entity.kind,
        "name": name,
        "aliases": aliases,
    }


def _detail_entry(entity: _Entity, matches: list[str]) -> dict[str, Any]:
    state = {
        source: copy.deepcopy(entity.current_state[source])
        for source in sorted(entity.current_state)
    }
    return {
        "entity_id": entity.entity_id,
        "kind": entity.kind,
        "name": entity.primary_name(),
        "matched_references": matches,
        "current_state": state,
        "current_state_sha256": canonical_json_hash(
            state,
            exclude_environment_fields=False,
        ),
    }


def _matched_references(entity: _Entity, prose: str) -> list[str]:
    candidates = {entity.entity_id, entity.primary_name(), *entity.aliases}
    return _sorted_identity_strings(
        value for value in candidates if _reference_occurs(value, prose)
    )


def _reference_occurs(reference: str, prose: str) -> bool:
    needle = unicodedata.normalize("NFKC", reference).casefold().strip()
    haystack = unicodedata.normalize("NFKC", prose).casefold()
    if not needle:
        return False
    if _ASCII_REFERENCE.fullmatch(needle):
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])"
        return re.search(pattern, haystack) is not None
    return needle in haystack


def _record_aliases(record: Mapping[str, Any]) -> list[str]:
    aliases: list[str] = []
    aliases.extend(_identity_strings(record.get("aliases")))
    data = record.get("data")
    if isinstance(data, dict):
        aliases.extend(_identity_strings(data.get("aliases")))
    return _sorted_identity_strings(aliases)


def _roster_member_ids(record: Mapping[str, Any]) -> list[str]:
    result = list(_identity_strings(record.get("member_ids")))
    result.extend(_identity_strings(record.get("character_ids")))
    members = record.get("members")
    if isinstance(members, list):
        for member in members:
            if isinstance(member, str):
                result.extend(_identity_strings(member))
            elif isinstance(member, dict):
                for field_name in ("member_id", "character_id", "entity_id", "id"):
                    result.extend(_identity_strings(member.get(field_name)))
    return _sorted_identity_strings(result)


def _identity_strings(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    for item in values:
        text = _optional_identity_text(item)
        if text is not None:
            result.append(text)
    return result


def _sorted_identity_strings(values: Iterable[str]) -> list[str]:
    unique: dict[str, str] = {}
    for value in values:
        text = _optional_identity_text(value)
        if text is None:
            continue
        normalized = _normalized_reference(text)
        winner = unique.get(normalized)
        if winner is None or text < winner:
            unique[normalized] = text
    return [unique[key] for key in sorted(unique, key=lambda item: (item, unique[item]))]


def _optional_identity_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > 300:
        raise ChapterFactContextError("entity id/name/alias exceeds 300 characters")
    return text


def _normalized_reference(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold().strip()


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _sorted_mapping(value: Any) -> list[tuple[str, Any]]:
    if not isinstance(value, dict):
        return []
    return sorted(
        ((str(key), child) for key, child in value.items()),
        key=lambda item: (_normalized_reference(item[0]), item[0]),
    )


def _object_or_empty(value: Any) -> dict[str, Any]:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _validated_prose(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChapterFactContextError("chapter_prose must contain prose")
    if len(value) > CHAPTER_FACT_CONTEXT_MAX_PROSE_CHARS:
        raise ChapterFactContextError(
            f"chapter_prose exceeds {CHAPTER_FACT_CONTEXT_MAX_PROSE_CHARS} characters"
        )
    return value


def _validated_budget(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ChapterFactContextError("max_chars must be a positive integer")
    if value > CHAPTER_FACT_CONTEXT_MAX_CHARS:
        raise ChapterFactContextError(
            f"max_chars cannot exceed {CHAPTER_FACT_CONTEXT_MAX_CHARS}",
            code="chapter_fact_context_budget_invalid",
        )
    return value


def _json_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ChapterFactContextError(f"{label} must be a JSON object")
    copied = _json_copy(value, label)
    if not isinstance(copied, dict):
        raise ChapterFactContextError(f"{label} must be a JSON object")
    return copied


def _json_copy(value: Any, label: str) -> Any:
    try:
        return json.loads(_canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ChapterFactContextError(f"{label} must be finite JSON") from exc


def _required_text(value: Any, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ChapterFactContextError(
            f"{label} must contain 1 to {maximum} characters"
        )
    return value.strip()


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ChapterFactContextError(f"{label} must be lowercase SHA-256")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "CHAPTER_FACT_CONTEXT_KIND",
    "CHAPTER_FACT_CONTEXT_MAX_CHARS",
    "CHAPTER_FACT_CONTEXT_MAX_PROSE_CHARS",
    "CHAPTER_FACT_CONTEXT_POLICY",
    "CHAPTER_FACT_CONTEXT_SCHEMA_VERSION",
    "ChapterFactContextError",
    "ChapterFactContextView",
    "build_chapter_fact_context",
    "validate_trusted_chapter_context",
]
