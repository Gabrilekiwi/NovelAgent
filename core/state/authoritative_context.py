from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable

from core.structured_context import (
    StructuredContextError,
    rank_texts,
    sha256_text,
)


AUTHORITATIVE_CONTEXT_POLICY = "authoritative_record_relevance_v1"
AUTHORITATIVE_CONTEXT_SELECTION_KEY = "context_selection"
AUTHORITATIVE_PLAN_SECTION_MAX_CHARS = 8_000
AUTHORITATIVE_SCENE_SECTION_MAX_CHARS = 3_000
AUTHORITATIVE_REPAIR_SECTION_MAX_CHARS = 6_000
AUTHORITATIVE_METADATA_KEYS = frozenset({"schema_version", "source_precedence"})
AUTHORITATIVE_RECORD_COLLECTIONS = (
    "characters",
    "relationships",
    "roster",
    "numeric_counters",
    "inventory",
    "locations",
    "events",
)
_AUTHORITATIVE_CONTEXT_SCHEMA_VERSION = "1.0"
_AUTHORITATIVE_CHAR_COUNT_MODE = "canonical_json_codepoints_v1"
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
_CHAPTER_ID_ORDER = re.compile(r"chapter[-_](\d+)", re.IGNORECASE)
_SCENE_ID_ORDER = re.compile(r"scene[-_](\d+)", re.IGNORECASE)
_BEAT_ID_ORDER = re.compile(r"beat[-_](\d+)", re.IGNORECASE)
_EVENT_ID_ORDER = re.compile(
    r"(?:^|[-_])event[-_](\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _AuthorityRecord:
    collection: str
    record_id: str
    value: dict[str, Any]
    ordinal: int

    @property
    def item_id(self) -> str:
        return f"{self.collection}/{self.record_id}"

    @property
    def search_text(self) -> str:
        return (
            f"{self.collection} {self.record_id} "
            + _json_text(self.value, sort_keys=True)
        )


def compact_authoritative_state_section(
    section_text: str,
    *,
    max_chars: int,
    query: str = "",
    require_query_references: bool = True,
    require_open_events: bool = True,
) -> str:
    """Project raw Authoritative State at complete stable-record boundaries."""

    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise StructuredContextError(
            "structured_context_limit_invalid",
            "max_chars must be positive",
        )
    heading_match = re.match(r"(?m)^# ([^\r\n]+)\r?\n?", str(section_text or ""))
    if heading_match is None:
        heading = "# Authoritative State"
        body = str(section_text or "").strip()
    else:
        heading = heading_match.group(0).rstrip("\r\n")
        body = str(section_text or "")[heading_match.end() :].strip()
    body_limit = max_chars - len(heading) - 1
    if body_limit < 1:
        raise StructuredContextError(
            "required_authoritative_context_metadata_exceeds_budget",
            f"authoritative section heading exceeds {max_chars} characters",
        )
    try:
        value = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise StructuredContextError(
            "authoritative_context_invalid",
            "Authoritative State must contain one JSON object",
        ) from exc
    if not isinstance(value, dict):
        raise StructuredContextError(
            "authoritative_context_invalid",
            "Authoritative State must contain one JSON object",
        )
    projected = project_authoritative_state(
        value,
        max_chars=body_limit,
        query=query,
        require_query_references=require_query_references,
        require_open_events=require_open_events,
    )
    rendered = _json_text(projected, sort_keys=True)
    if len(rendered) > body_limit:
        raise StructuredContextError(
            "required_authoritative_context_metadata_exceeds_budget",
            f"authoritative projection exceeds {body_limit} characters",
        )
    return f"{heading}\n{rendered}"


def authoritative_state_from_markdown(text: str) -> dict[str, Any] | None:
    match = re.search(
        r"(?ms)^# Authoritative State[ \t]*\r?\n(.*?)(?=^# |\Z)",
        str(text or ""),
    )
    if match is None:
        return None
    try:
        value = json.loads(match.group(1).strip())
    except (TypeError, ValueError) as exc:
        raise StructuredContextError(
            "authoritative_context_invalid",
            "Authoritative State must contain one JSON object",
        ) from exc
    if not isinstance(value, dict):
        raise StructuredContextError(
            "authoritative_context_invalid",
            "Authoritative State must contain one JSON object",
        )
    return value


def compact_authoritative_state_in_markdown(
    text: str,
    *,
    max_section_chars: int,
    query: str = "",
    require_query_references: bool = True,
    require_open_events: bool = True,
    authoritative_state_source: dict[str, Any] | None = None,
) -> str:
    """Replace one Authoritative State section with one raw-source projection."""

    source = str(text or "")
    match = re.search(
        r"(?ms)^# Authoritative State[ \t]*\r?\n.*?(?=^# |\Z)",
        source,
    )
    if authoritative_state_source is not None:
        if not isinstance(authoritative_state_source, dict):
            raise StructuredContextError(
                "authoritative_context_invalid",
                "authoritative_state_source must be a JSON object",
            )
        full_section = "# Authoritative State\n" + _json_text(
            authoritative_state_source,
            sort_keys=True,
        )
        if match is None:
            source = source.rstrip() + ("\n\n" if source.strip() else "") + full_section
        else:
            suffix = source[match.end() :]
            separator = "\n\n" if suffix else ""
            source = source[: match.start()] + full_section + separator + suffix
        match = re.search(
            r"(?ms)^# Authoritative State[ \t]*\r?\n.*?(?=^# |\Z)",
            source,
        )
    if match is None:
        return source
    compacted = compact_authoritative_state_section(
        match.group(0).rstrip(),
        max_chars=max_section_chars,
        query=query,
        require_query_references=require_query_references,
        require_open_events=require_open_events,
    )
    suffix = source[match.end() :]
    separator = "\n\n" if suffix else ""
    return source[: match.start()] + compacted + separator + suffix


def project_authoritative_state(
    value: dict[str, Any],
    *,
    max_chars: int,
    query: str = "",
    required_item_ids: Iterable[str] = (),
    require_query_references: bool = True,
    require_open_events: bool = True,
) -> dict[str, Any]:
    """Return one bounded projection built directly from raw authority.

    Projections are deliberately not valid projection inputs. Consumers that
    need a different query must retain and supply the raw authoritative source.
    This prevents a second projection from silently losing records omitted by
    the first.
    """

    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise StructuredContextError(
            "structured_context_limit_invalid",
            "max_chars must be positive",
        )
    if not isinstance(value, dict):
        raise StructuredContextError(
            "authoritative_context_invalid",
            "Authoritative State must be a JSON object",
        )
    if AUTHORITATIVE_CONTEXT_SELECTION_KEY in value:
        raise StructuredContextError(
            "authoritative_context_already_projected",
            "Authoritative State projection requires the raw source; "
            "supply authoritative_state_source instead of reprojecting",
        )

    source = dict(value)
    unknown_keys = set(source) - set(AUTHORITATIVE_RECORD_COLLECTIONS) - set(
        AUTHORITATIVE_METADATA_KEYS
    )
    if unknown_keys:
        raise StructuredContextError(
            "authoritative_context_invalid",
            "Authoritative State contains unsupported keys: "
            + ", ".join(sorted(str(key) for key in unknown_keys)),
        )
    metadata = {
        key: item
        for key, item in source.items()
        if key not in AUTHORITATIVE_RECORD_COLLECTIONS
    }
    records = _authority_records(source)
    records_by_item_id = {record.item_id: record for record in records}
    records_by_record_id = _records_by_record_id(records)
    records_by_reference = _records_by_reference(records)
    normalized_source = _projected_payload(
        metadata,
        records=records,
        selected_ordinals={record.ordinal for record in records},
    )
    canonical_source = _json_text(normalized_source, sort_keys=True)
    source_sha256 = sha256_text(canonical_source)

    required_ordinals = _resolve_required_item_ids(
        required_item_ids,
        records_by_item_id=records_by_item_id,
        records_by_record_id=records_by_record_id,
    )
    if require_query_references:
        required_ordinals.update(
            _resolve_query_references(
                _explicit_query_values(str(query or "")),
                records_by_item_id=records_by_item_id,
                records_by_record_id=records_by_record_id,
                records_by_reference=records_by_reference,
            )
        )
    if require_open_events:
        required_ordinals.update(
            record.ordinal for record in records if _record_is_open_event(record)
        )
    required_event_ordinals = {
        ordinal
        for ordinal in required_ordinals
        if records[ordinal].collection == "events"
    }
    required_ordinals.update(
        _event_dependency_ordinals(
            required_event_ordinals,
            records=records,
            records_by_reference=records_by_reference,
        )
    )
    required_items = [
        record.item_id
        for record in records
        if record.ordinal in required_ordinals
    ]

    def render(selected_ordinals: set[int]) -> dict[str, Any]:
        projected = _projected_payload(
            metadata,
            records=records,
            selected_ordinals=selected_ordinals,
        )
        selected_items = [
            record.item_id
            for record in records
            if record.ordinal in selected_ordinals
        ]
        omitted_counts = {
            collection: sum(
                1
                for record in records
                if record.collection == collection
                and record.ordinal not in selected_ordinals
            )
            for collection in AUTHORITATIVE_RECORD_COLLECTIONS
        }
        sparse_omitted_counts = {
            collection: count
            for collection, count in omitted_counts.items()
            if count
        }
        manifest: dict[str, Any] = {
            "schema_version": _AUTHORITATIVE_CONTEXT_SCHEMA_VERSION,
            "policy": AUTHORITATIVE_CONTEXT_POLICY,
            "char_count_mode": _AUTHORITATIVE_CHAR_COUNT_MODE,
            "source_sha256": source_sha256,
            "original_chars": len(canonical_source),
            "original_item_count": len(records),
            "budget_chars": max_chars,
            "requirements": {
                "query_references": bool(require_query_references),
                "open_events": bool(require_open_events),
            },
            "required_items": required_items,
            "selected_items": selected_items,
            "omitted_count": len(records) - len(selected_ordinals),
            "omitted_counts_by_collection": sparse_omitted_counts,
            "query_sha256": sha256_text(str(query or "")),
        }
        projected[AUTHORITATIVE_CONTEXT_SELECTION_KEY] = manifest
        manifest["projection_sha256"] = sha256_text(
            _json_text(projected, sort_keys=True)
        )
        return projected

    def rendered_chars(selected_ordinals: set[int]) -> int:
        return len(_json_text(render(selected_ordinals), sort_keys=True))

    for ordinal in sorted(required_ordinals):
        record = records[ordinal]
        single_chars = rendered_chars({ordinal})
        if single_chars > max_chars:
            raise StructuredContextError(
                "required_authoritative_record_exceeds_budget",
                f"required record {record.item_id!r} needs {single_chars} "
                f"characters including audit metadata, exceeding the "
                f"{max_chars} character authority budget",
            )
    required_chars = rendered_chars(required_ordinals)
    if required_chars > max_chars:
        required_items = [
            record.item_id
            for record in records
            if record.ordinal in required_ordinals
        ]
        raise StructuredContextError(
            "required_authoritative_records_exceed_budget",
            f"required records need {required_chars} characters including "
            f"audit metadata, exceeding the {max_chars} character authority "
            f"budget: {', '.join(required_items)}",
        )
    if rendered_chars(set()) > max_chars:
        raise StructuredContextError(
            "required_authoritative_context_metadata_exceeds_budget",
            f"authoritative metadata exceeds {max_chars} characters",
        )

    all_ordinals = {record.ordinal for record in records}
    if rendered_chars(all_ordinals) <= max_chars:
        return render(all_ordinals)

    chosen = set(required_ordinals)
    ranking_records = _records_for_ranking(records)
    ranked = rank_texts(
        [record.search_text for record in ranking_records],
        query=str(query or ""),
        prefer_recent=False,
    )
    for index in ranked:
        record = ranking_records[index]
        if record.ordinal in chosen:
            continue
        candidate = chosen | {record.ordinal}
        if rendered_chars(candidate) <= max_chars:
            chosen = candidate
    return render(chosen)


def _authority_records(value: dict[str, Any]) -> list[_AuthorityRecord]:
    records: list[_AuthorityRecord] = []
    for collection in AUTHORITATIVE_RECORD_COLLECTIONS:
        raw = value.get(collection)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise StructuredContextError(
                "authoritative_context_invalid",
                f"Authoritative State {collection!r} must be a JSON object",
            )
        collection_records: list[tuple[str, dict[str, Any]]] = []
        for record_id, record in raw.items():
            if not isinstance(record_id, str):
                raise StructuredContextError(
                    "authoritative_context_invalid",
                    f"Authoritative State {collection!r} record ids must be strings",
                )
            normalized_id = record_id.strip()
            if not normalized_id or normalized_id != record_id:
                raise StructuredContextError(
                    "authoritative_context_invalid",
                    f"Authoritative State {collection!r} contains a noncanonical "
                    f"record id {record_id!r}",
                )
            if not isinstance(record, dict):
                raise StructuredContextError(
                    "authoritative_context_invalid",
                    f"Authoritative State record "
                    f"{collection}/{normalized_id!s} must be a JSON object",
                )
            collection_records.append((normalized_id, record))
        collection_records.sort(key=lambda item: item[0])
        for normalized_id, record in collection_records:
            records.append(
                _AuthorityRecord(
                    collection=collection,
                    record_id=normalized_id,
                    value=record,
                    ordinal=len(records),
                )
            )
    return records


def _projected_payload(
    metadata: dict[str, Any],
    *,
    records: list[_AuthorityRecord],
    selected_ordinals: set[int],
) -> dict[str, Any]:
    projected = dict(metadata)
    collections: dict[str, dict[str, Any]] = {
        collection: {} for collection in AUTHORITATIVE_RECORD_COLLECTIONS
    }
    for record in records:
        if record.ordinal in selected_ordinals:
            collections[record.collection][record.record_id] = record.value
    projected.update(collections)
    return projected


def _records_by_record_id(
    records: list[_AuthorityRecord],
) -> dict[str, list[_AuthorityRecord]]:
    by_record_id: dict[str, list[_AuthorityRecord]] = {}
    for record in records:
        by_record_id.setdefault(record.record_id, []).append(record)
    return by_record_id


def _records_by_reference(
    records: list[_AuthorityRecord],
) -> dict[str, list[_AuthorityRecord]]:
    by_reference: dict[str, list[_AuthorityRecord]] = {}
    for record in records:
        references = {record.item_id, record.record_id}
        references.update(_stable_record_references(record))
        for reference in sorted(references):
            bucket = by_reference.setdefault(reference, [])
            if record not in bucket:
                bucket.append(record)
    return by_reference


def _resolve_required_item_ids(
    required_item_ids: Iterable[str],
    *,
    records_by_item_id: dict[str, _AuthorityRecord],
    records_by_record_id: dict[str, list[_AuthorityRecord]],
) -> set[int]:
    resolved: set[int] = set()
    for raw_item_id in required_item_ids:
        item_id = str(raw_item_id).strip()
        if not item_id:
            continue
        exact = records_by_item_id.get(item_id)
        if exact is not None:
            resolved.add(exact.ordinal)
            continue
        if "/" in item_id:
            raise StructuredContextError(
                "required_authoritative_record_missing",
                f"required record {item_id!r} is absent from Authoritative State",
            )
        shorthand = records_by_record_id.get(item_id, [])
        if len(shorthand) == 1:
            resolved.add(shorthand[0].ordinal)
            continue
        if len(shorthand) > 1:
            raise StructuredContextError(
                "required_authoritative_record_ambiguous",
                f"required record id {item_id!r} matches multiple collections; "
                "use a collection/id item id",
            )
        raise StructuredContextError(
            "required_authoritative_record_missing",
            f"required record {item_id!r} is absent from Authoritative State",
        )
    return resolved


def _resolve_query_references(
    query_values: set[str],
    *,
    records_by_item_id: dict[str, _AuthorityRecord],
    records_by_record_id: dict[str, list[_AuthorityRecord]],
    records_by_reference: dict[str, list[_AuthorityRecord]],
) -> set[int]:
    resolved: set[int] = set()
    for value in query_values:
        exact = records_by_item_id.get(value)
        if exact is not None:
            resolved.add(exact.ordinal)
        for record in records_by_record_id.get(value, []):
            resolved.add(record.ordinal)
        for record in records_by_reference.get(value, []):
            resolved.add(record.ordinal)
    return resolved


def _event_dependency_ordinals(
    event_ordinals: set[int],
    *,
    records: list[_AuthorityRecord],
    records_by_reference: dict[str, list[_AuthorityRecord]],
) -> set[int]:
    """Resolve bounded event dependencies without traversing provenance loops."""

    entity_state = {
        "characters",
        "relationships",
        "roster",
        "numeric_counters",
        "inventory",
        "locations",
    }
    reverse_provenance = {"numeric_counters", "inventory"}
    direct: set[int] = set()
    for ordinal in sorted(event_ordinals):
        event = records[ordinal]
        event_ids = {event.record_id}
        event_ids.update(_strings_from_fields(event.value, ("event_id",)))
        direct.update(
            record.ordinal
            for record in records
            if record.collection in reverse_provenance
            and str(record.value.get("source_event_id") or "").strip()
            in event_ids
        )
        subject_objects = _strings_from_fields(
            event.value,
            ("subjects", "objects"),
        )
        for reference in sorted(subject_objects):
            direct.update(
                record.ordinal
                for record in records_by_reference.get(reference, [])
                if record.collection in entity_state
            )

    supporting_identity: set[int] = set()
    for ordinal in sorted(direct):
        record = records[ordinal]
        allowed_collections = (
            entity_state
            if record.collection == "characters"
            else {"characters", "locations"}
        )
        for reference in sorted(_supporting_identity_references(record)):
            supporting_identity.update(
                dependency.ordinal
                for dependency in records_by_reference.get(reference, [])
                if dependency.collection in allowed_collections
            )
    return direct | supporting_identity


def _stable_record_references(record: _AuthorityRecord) -> set[str]:
    fields_by_collection = {
        "characters": ("character_id", "canonical_name", "aliases"),
        "relationships": (
            "relationship_id",
            "source_character_id",
            "target_character_id",
            "source_id",
            "target_id",
        ),
        "numeric_counters": ("counter_id", "owner_id", "source_event_id"),
        "inventory": (
            "inventory_id",
            "item_id",
            "owner_id",
            "source_event_id",
        ),
        "locations": ("entity_id",),
        "events": ("event_id",),
    }
    if record.collection == "roster":
        return _strings_from_fields(record.value, ("roster_id",)) | (
            _roster_member_references(record.value)
        )
    return _strings_from_fields(
        record.value,
        fields_by_collection[record.collection],
    )


def _supporting_identity_references(record: _AuthorityRecord) -> set[str]:
    fields_by_collection = {
        "characters": ("character_id", "canonical_name", "aliases"),
        "relationships": (
            "source_character_id",
            "target_character_id",
            "source_id",
            "target_id",
        ),
        "numeric_counters": ("owner_id",),
        "inventory": ("owner_id",),
        "locations": ("entity_id",),
        "events": (),
    }
    if record.collection == "roster":
        return _roster_member_references(record.value)
    return _strings_from_fields(
        record.value,
        fields_by_collection[record.collection],
    )


def _roster_member_references(value: dict[str, Any]) -> set[str]:
    references = _strings_from_fields(
        value,
        ("member_ids", "character_ids"),
    )
    members = value.get("members")
    if not isinstance(members, list):
        return references
    for member in members:
        if isinstance(member, str):
            normalized = member.strip()
            if normalized:
                references.add(normalized)
        elif isinstance(member, dict):
            references.update(
                _strings_from_fields(
                    member,
                    ("member_id", "character_id", "entity_id", "id"),
                )
            )
    return references


def _strings_from_fields(
    value: dict[str, Any],
    fields: Iterable[str],
) -> set[str]:
    values: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, str):
            normalized = item.strip()
            if normalized:
                values.add(normalized)
            return
        if isinstance(item, list):
            for nested in item:
                visit(nested)

    for field in fields:
        visit(value.get(field))
    return values


def _record_is_open_event(record: _AuthorityRecord) -> bool:
    if record.collection != "events":
        return False
    return authoritative_event_is_open(record.value)


def authoritative_event_is_open(value: dict[str, Any]) -> bool:
    status = str(value.get("status") or "").strip().lower()
    # Legacy authority records omitted status and historically meant completed.
    # Any explicit non-terminal value is conservatively binding, including a
    # misspelled open status, so it cannot be silently discarded.
    return bool(status) and status not in _TERMINAL_EVENT_STATUSES


def _records_for_ranking(
    records: list[_AuthorityRecord],
) -> list[_AuthorityRecord]:
    current_state = [record for record in records if record.collection != "events"]
    events = sorted(
        (record for record in records if record.collection == "events"),
        key=_event_recency_sort_key,
    )
    return current_state + events


def _event_recency_sort_key(
    record: _AuthorityRecord,
) -> tuple[int, int, int, int, str]:
    chapter = _optional_nonnegative_int(record.value.get("chapter_index"))
    scene = _optional_nonnegative_int(record.value.get("scene_index"))
    event = _first_nonnegative_int(
        record.value.get("event_index"),
        record.value.get("beat_index"),
        record.value.get("sequence_index"),
    )
    chapter_match = _CHAPTER_ID_ORDER.search(record.record_id)
    scene_match = _SCENE_ID_ORDER.search(record.record_id)
    beat_matches = list(_BEAT_ID_ORDER.finditer(record.record_id))
    event_matches = list(_EVENT_ID_ORDER.finditer(record.record_id))
    if chapter is None and chapter_match is not None:
        chapter = int(chapter_match.group(1))
    if scene is None and scene_match is not None:
        scene = int(scene_match.group(1))
    if event is None:
        if event_matches:
            event = int(event_matches[-1].group(1))
        elif beat_matches:
            event = int(beat_matches[-1].group(1))
    if chapter is None and scene is None and event is None:
        return (1, 0, 0, 0, record.record_id)
    return (
        0,
        -(chapter if chapter is not None else 0),
        -(scene if scene is not None else 0),
        -(event if event is not None else 0),
        record.record_id,
    )


def _optional_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _first_nonnegative_int(*values: Any) -> int | None:
    for value in values:
        result = _optional_nonnegative_int(value)
        if result is not None:
            return result
    return None


def _json_text(value: Any, *, sort_keys: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=sort_keys,
        separators=(",", ":"),
        default=str,
    )


def _explicit_query_values(query: str) -> set[str]:
    source = str(query or "")
    parsed_documents: list[Any] = []
    try:
        parsed_documents.append(json.loads(source))
    except (TypeError, ValueError):
        decoder = json.JSONDecoder()
        cursor = 0
        while cursor < len(source):
            match = re.search(r"[\{\[]", source[cursor:])
            if match is None:
                break
            start = cursor + match.start()
            try:
                parsed, end = decoder.raw_decode(source, start)
            except (TypeError, ValueError):
                cursor = start + 1
                continue
            parsed_documents.append(parsed)
            cursor = end
    values: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                values.add(normalized)
            return
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)

    for parsed in parsed_documents:
        visit(parsed)
    return values


__all__ = [
    "AUTHORITATIVE_CONTEXT_POLICY",
    "AUTHORITATIVE_CONTEXT_SELECTION_KEY",
    "AUTHORITATIVE_METADATA_KEYS",
    "AUTHORITATIVE_PLAN_SECTION_MAX_CHARS",
    "AUTHORITATIVE_REPAIR_SECTION_MAX_CHARS",
    "AUTHORITATIVE_RECORD_COLLECTIONS",
    "AUTHORITATIVE_SCENE_SECTION_MAX_CHARS",
    "authoritative_event_is_open",
    "authoritative_state_from_markdown",
    "compact_authoritative_state_in_markdown",
    "compact_authoritative_state_section",
    "project_authoritative_state",
]
