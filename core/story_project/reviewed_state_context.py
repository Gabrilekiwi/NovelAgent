"""Read-set-bound runtime view for reviewed legacy chapter state.

This module specifies a context boundary, not a book storage format. A loader
must verify its project source before constructing this view. Normal entry
points do not discover or enable the experimental D1 format automatically.
"""
from __future__ import annotations

import copy
from pathlib import PurePosixPath
import re
from typing import Any

from core.memory_v2.canonical import canonical_json_hash
from core.state.authoritative import validate_authoritative_state
from core.state.snapshot import normalize_snapshot
from core.state.location_mirrors import validate_character_location_mirrors
from core.story_project.reviewed_static_context import (
    static_character_details, static_character_mapping_diagnostics, static_spatial_fields,
)


class ReviewedStateContextError(ValueError):
    code = "reviewed_state_context_invalid"


def _hash(value: dict) -> str:
    return canonical_json_hash(value, exclude_environment_fields=False)


def build_reviewed_state_context(context: dict, *, authoritative_state: dict,
                                 source_relative_path: str, source_sha256: str,
                                 manual_sources_sha256: str, committed_through: int,
                                 static_references: list[dict] | None = None) -> dict:
    value = {
        "schema_version": "1.0", "book_id": context["project_identity"]["book_id"],
        "chapter_index": context["chapter_index"], "committed_through": committed_through,
        "source_relative_path": source_relative_path, "source_sha256": source_sha256,
        "manual_sources_sha256": manual_sources_sha256,
        "semantic_source_digest": context["semantic_state"]["source_digest"],
        "read_set_context_digest": context["read_set"]["context_digest"],
        "authoritative_state": copy.deepcopy(authoritative_state),
        "static_references": copy.deepcopy(static_references or []),
    }
    value["payload_sha256"] = _hash(value)
    return validated_reviewed_state_context({**context, "reviewed_state": value})


def validated_reviewed_state_context(context: dict | None) -> dict | None:
    if not isinstance(context, dict) or "reviewed_state" not in context:
        return None
    value = context["reviewed_state"]
    fields = {"schema_version", "book_id", "chapter_index", "committed_through",
              "source_relative_path", "source_sha256", "manual_sources_sha256",
              "semantic_source_digest", "read_set_context_digest", "authoritative_state", "payload_sha256",
              "static_references"}
    if not isinstance(value, dict) or set(value) != fields or value["schema_version"] != "1.0":
        raise ReviewedStateContextError("reviewed runtime view has an unsupported shape")
    identity = context.get("project_identity") or {}
    authority = identity.get("authority") or {}
    semantic = context.get("semantic_state") or {}
    audit = context.get("semantic_audit") or {}
    read_set = context.get("read_set") or {}
    if (context.get("story_state_mode") != "strict" or identity.get("ephemeral") is not False
            or authority.get("mode", "legacy_markdown_v1") != "legacy_markdown_v1"
            or audit.get("authoritative") is not True
            or semantic.get("book_id") != identity.get("book_id")
            or value["book_id"] != identity.get("book_id")
            or read_set.get("book_id") != value["book_id"]):
        raise ReviewedStateContextError("reviewed runtime view requires the same persisted legacy Strict identity")
    chapter = value["chapter_index"]
    if (type(chapter) is not int or type(value["committed_through"]) is not int
            or value["committed_through"] < 1 or chapter != value["committed_through"] + 1
            or any(item != chapter for item in (context.get("chapter_index"),
                                                semantic.get("chapter_index"), read_set.get("chapter_index")))):
        raise ReviewedStateContextError("reviewed runtime view is bound to another chapter")
    for name in ("source_sha256", "manual_sources_sha256", "semantic_source_digest",
                 "read_set_context_digest", "payload_sha256"):
        if not isinstance(value[name], str) or re.fullmatch(r"[0-9a-f]{64}", value[name]) is None:
            raise ReviewedStateContextError("reviewed runtime view requires SHA-256 bindings")
    if (value["semantic_source_digest"] != semantic.get("source_digest")
            or value["semantic_source_digest"] != audit.get("source_digest")
            or value["read_set_context_digest"] != read_set.get("context_digest")):
        raise ReviewedStateContextError("reviewed runtime view differs from its semantic or read-set binding")
    relative = value["source_relative_path"]
    if (not isinstance(relative, str) or "\\" in relative or ":" in relative
            or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts
            or not relative.startswith("追踪/") or not relative.endswith(".md")):
        raise ReviewedStateContextError("reviewed source must be a project tracking Markdown source")
    matches = [entry for entry in read_set.get("membership", []) if entry.get("relative_path") == relative]
    if len(matches) != 1 or matches[0].get("sha256") != value["source_sha256"]:
        raise ReviewedStateContextError("reviewed source is not pinned by the chapter read set")
    references = value["static_references"]
    if not isinstance(references, list):
        raise ReviewedStateContextError("approved static references must be a list")
    for reference in references:
        if (not isinstance(reference, dict) or set(reference) != {"relative_path", "sha256", "text"}
                or not isinstance(reference["text"], str) or not reference["text"].strip()):
            raise ReviewedStateContextError("approved static reference is invalid")
        matching = [entry for entry in read_set.get("membership", [])
                    if entry.get("relative_path") == reference["relative_path"]]
        if len(matching) != 1 or matching[0].get("sha256") != reference["sha256"]:
            raise ReviewedStateContextError("approved static reference is not pinned by the chapter read set")
    unhashed = {key: item for key, item in value.items() if key != "payload_sha256"}
    if value["payload_sha256"] != _hash(unhashed):
        raise ReviewedStateContextError("reviewed runtime payload no longer matches its hash")
    state = value["authoritative_state"]
    if not isinstance(state, dict) or any(not isinstance(state.get(key), dict) for key in
            ("characters", "relationships", "roster", "locations", "inventory", "numeric_counters", "events")):
        raise ReviewedStateContextError("reviewed runtime view requires all seven state collections")
    validate_authoritative_state(copy.deepcopy(state))
    try:
        validate_character_location_mirrors(state)
    except ValueError as exc:
        raise ReviewedStateContextError(str(exc)) from exc
    return copy.deepcopy(value)


def reviewed_state_snapshot(context: dict) -> dict:
    """Build current dynamic fields from reviewed state, keeping static semantics.

    Raw tracking prose and the initial semantic object remain on the context for
    audit/UI. They are not merged back into the current character/position views.
    Unknown scene position stays unknown; the reader does not pick a character
    or guess a scene from an arbitrary location update.
    """
    view = validated_reviewed_state_context(context)
    if view is None:
        raise ReviewedStateContextError("reviewed runtime view is required")
    state = view["authoritative_state"]
    semantic = context["semantic_state"]
    characters = copy.deepcopy(state["characters"])
    static_details = static_character_details(semantic.get("characters"), characters)
    for entity_id, details in static_details.items():
        characters[entity_id].update(details)
    positions = {}
    for entity_id, position in state["locations"].items():
        location = position.get("location_id")
        if entity_id in characters and location is not None:
            positions[entity_id] = location
            characters[entity_id]["location"] = location
    previous = context.get("previous_prose") or {}
    ending = str(previous.get("text") or "")[-1200:]
    # Pending threads are retained; chapter-bound location/decision/ending fields
    # come from current prose and structured facts, not the unchanged manual file.
    story = {key: copy.deepcopy(semantic["story_state"][key]) for key in ("pending_threads", "open_threads")
             if key in semantic.get("story_state", {})}
    story["last_chapter_ending"] = ending
    return normalize_snapshot({
        "book_id": view["book_id"], "chapter_index": view["chapter_index"],
        "project_profile": copy.deepcopy((context.get("snapshot_overlay") or {}).get("project_profile") or {}),
        "authoritative_state": state, "characters": characters,
        "spatial_state": {**static_spatial_fields(semantic.get("spatial_state")),
                          "character_positions": positions}, "story_state": story,
        "world_state": copy.deepcopy(semantic.get("world_state") or {}),
        "constraints": copy.deepcopy(semantic.get("constraints") or []),
        "foreshadowing": copy.deepcopy(semantic.get("foreshadowing") or []),
        "timeline": list(copy.deepcopy(state["events"]).values()),
        "semantic_authority": {"source": "legacy_reviewed_state", "source_sha256": view["source_sha256"],
                               "payload_sha256": view["payload_sha256"],
                               "static_character_mapping": static_character_mapping_diagnostics(
                                   semantic.get("characters"), state["characters"]),
                               "manual_sources_sha256": view["manual_sources_sha256"]},
    })


def reviewed_state_memory_context(context: dict) -> dict:
    view = validated_reviewed_state_context(context)
    if view is None:
        raise ReviewedStateContextError("reviewed runtime view is required")
    # Keep outline instructions; static parsed settings are already in snapshot.
    overlay = context.get("memory_context_overlay") or {}
    items = [copy.deepcopy(item) for item in overlay.get("items", []) if item.get("name") == "current_outline"]
    return {"source": "legacy_reviewed_state", "status": "ready", "items": items,
            "source_mappings": [], "story_project": {"enabled": True, "book_id": view["book_id"],
                                                     "chapter_index": view["chapter_index"]}}
