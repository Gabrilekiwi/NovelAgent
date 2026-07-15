from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.memory_v2 import canonical_memory_to_snapshot
from core.memory_v2.history_revision import (
    HistoricalRevisionError,
    assert_event_authority_reconciliation_ready,
)
from core.runtime_paths import RuntimePaths
from core.state.snapshot import normalize_snapshot
from core.story_project.identity import (
    assert_project_identity,
    create_ephemeral_project_identity,
    ensure_project_identity_for_runtime,
    validate_project_identity,
)
from core.story_project.read_set import capture_story_project_read_set


StoryProjectContextLoader = Callable[[dict[str, Any], dict[str, Any], int | None], Any]


class StoryProjectContextError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class NormalizedStoryProjectContext:
    context: Any
    last_project_identity: dict[str, Any] | None
    allow_legacy_snapshot_adoption: bool


@dataclass(frozen=True)
class PreparedStoryProjectIdentity:
    expected_book_id: str | None
    last_project_identity: dict[str, Any] | None
    allow_legacy_snapshot_adoption: bool


class StoryProjectContextService:
    """Owns StoryProject context loading, identity normalization, and authority mapping."""

    @staticmethod
    def context_dict(active_context: Any, configured_context: Any) -> dict[str, Any] | None:
        context = active_context if active_context is not None else configured_context
        if context is None:
            return None
        if hasattr(context, "to_dict"):
            return context.to_dict()
        return context if isinstance(context, dict) else None

    @staticmethod
    def load(
        *,
        configured_context: Any,
        loader: StoryProjectContextLoader | None,
        snapshot: dict[str, Any],
        memory_context: dict[str, Any],
        chapter_hint: int | None,
    ) -> Any:
        if loader is None:
            return configured_context
        context = loader(snapshot, memory_context, chapter_hint)
        payload = context.to_dict() if hasattr(context, "to_dict") else context
        if not isinstance(payload, dict):
            raise StoryProjectContextError(
                "story_project_context_invalid",
                "context loader must return a mapping or an object with to_dict()",
            )
        chapter_index = payload.get("chapter_index")
        if isinstance(chapter_index, bool) or not isinstance(chapter_index, int) or chapter_index < 1:
            raise StoryProjectContextError(
                "story_project_context_invalid",
                "context loader returned an invalid chapter_index",
            )
        if chapter_hint is not None and chapter_index != chapter_hint:
            raise StoryProjectContextError(
                "story_project_sequence_drift",
                f"expected chapter {chapter_hint}, but context resolved chapter {chapter_index}",
            )
        return context

    @staticmethod
    def normalize_identity(
        context: Any,
        *,
        persist: bool,
        persistence_dir: Path,
        allow_legacy_snapshot_adoption: bool,
    ) -> NormalizedStoryProjectContext:
        if context is None:
            return NormalizedStoryProjectContext(
                context=None,
                last_project_identity=None,
                allow_legacy_snapshot_adoption=allow_legacy_snapshot_adoption,
            )
        if hasattr(context, "to_dict"):
            payload = context.to_dict()
        elif isinstance(context, dict):
            payload = dict(context)
        else:
            return NormalizedStoryProjectContext(
                context=context,
                last_project_identity=None,
                allow_legacy_snapshot_adoption=allow_legacy_snapshot_adoption,
            )
        root = payload.get("story_project_root")
        if not root:
            return NormalizedStoryProjectContext(
                context=payload,
                last_project_identity=None,
                allow_legacy_snapshot_adoption=allow_legacy_snapshot_adoption,
            )
        current_payload = payload.get("project_identity")
        current = validate_project_identity(current_payload) if isinstance(current_payload, dict) else None
        allow_legacy = allow_legacy_snapshot_adoption
        if not persist:
            allow_legacy = current is None or current.ephemeral
        if persist:
            stable = ensure_project_identity_for_runtime(root, persistence_dir=persistence_dir)
            if current is not None and not current.ephemeral:
                assert_project_identity(stable, current.book_id, source="StoryProject runtime context")
            payload["project_identity"] = stable.to_dict()
            existing_read_set = payload.get("read_set") if isinstance(payload.get("read_set"), dict) else {}
            payload["read_set"] = capture_story_project_read_set(
                root,
                int(payload["chapter_index"]),
                project_identity=stable,
                parser_version=str(existing_read_set.get("parser_version") or "shadow-1.0"),
                parse_status=str(existing_read_set.get("parse_status") or "ok"),
            )
        elif current is None:
            payload["project_identity"] = create_ephemeral_project_identity(root).to_dict()
        return NormalizedStoryProjectContext(
            context=payload,
            last_project_identity=dict(payload["project_identity"]),
            allow_legacy_snapshot_adoption=allow_legacy,
        )

    @staticmethod
    def apply_context(
        context: dict[str, Any] | None,
        snapshot: dict[str, Any],
        memory_context: dict[str, Any],
        *,
        snapshot_path: Path,
        allow_legacy_snapshot_adoption: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not context:
            return snapshot, memory_context
        identity_payload = context.get("project_identity")
        identity = validate_project_identity(identity_payload) if isinstance(identity_payload, dict) else None
        if identity is not None:
            root = context.get("story_project_root")
            internal_snapshot = RuntimePaths.for_story_project(root).snapshot_path.resolve() if root is not None else None
            assert_project_identity(
                identity,
                str(snapshot.get("book_id")) if snapshot.get("book_id") is not None else None,
                source=str(snapshot_path),
                allow_missing_legacy=(
                    allow_legacy_snapshot_adoption
                    or (internal_snapshot is not None and snapshot_path.resolve() == internal_snapshot)
                ),
            )
        event_authority = bool(
            identity is not None
            and isinstance(identity.authority, dict)
            and identity.authority.get("mode") == "event_v1"
        )
        if event_authority:
            if context.get("story_state_mode") != "strict":
                raise StoryProjectContextError(
                    "event_authority_context_mode_invalid",
                    "event-authority StoryProject context must use strict runtime isolation",
                )
            return (
                StoryProjectContextService.apply_authority(context, snapshot),
                _event_authority_memory_context(context, identity),
            )
        merged_snapshot = _deep_merge_dict(snapshot, context.get("snapshot_overlay"))
        if identity is not None:
            merged_snapshot["book_id"] = identity.book_id
        merged_memory = dict(memory_context)
        overlay = context.get("memory_context_overlay")
        if isinstance(overlay, dict):
            base_items = list(merged_memory.get("items") or [])
            overlay_items = list(overlay.get("items") or [])
            base_mappings = list(merged_memory.get("source_mappings") or [])
            adjusted_mappings: list[dict[str, Any]] = []
            for mapping in overlay.get("source_mappings") or []:
                if not isinstance(mapping, dict):
                    continue
                adjusted = dict(mapping)
                if isinstance(adjusted.get("index"), int):
                    adjusted["index"] = int(adjusted["index"]) + len(base_items)
                adjusted_mappings.append(adjusted)
            merged_memory["items"] = [*base_items, *overlay_items]
            merged_memory["source_mappings"] = [*base_mappings, *adjusted_mappings]
            merged_memory["story_project"] = {
                "enabled": True,
                "chapter_index": context.get("chapter_index"),
                "book_id": identity.book_id if identity is not None else None,
            }
        return merged_snapshot, merged_memory

    @staticmethod
    def apply_authority(context: dict[str, Any] | None, snapshot: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(context, dict) or context.get("story_state_mode") != "strict":
            return snapshot
        identity = context.get("project_identity")
        authority = identity.get("authority") if isinstance(identity, dict) else None
        if isinstance(authority, dict) and authority.get("mode") == "event_v1":
            memory_v2 = context.get("memory_v2")
            projection = memory_v2.get("projection") if isinstance(memory_v2, dict) else None
            if not isinstance(projection, dict) or projection.get("schema_version") != "2.2":
                raise StoryProjectContextError(
                    "event_authority_projection_missing",
                    "event-authoritative StoryProject context requires CanonicalMemory 2.2",
                )
            if projection.get("book_id") != identity.get("book_id"):
                raise StoryProjectContextError(
                    "event_authority_identity_mismatch",
                    "CanonicalMemory belongs to another StoryProject",
                )
            if projection.get("authority_epoch") != authority.get("authority_epoch"):
                raise StoryProjectContextError(
                    "event_authority_epoch_mismatch",
                    "CanonicalMemory authority epoch differs from ProjectIdentity",
                )
            if projection.get("head_event_hash") != authority.get("head_event_hash"):
                raise StoryProjectContextError(
                    "event_authority_head_mismatch",
                    "CanonicalMemory head differs from ProjectIdentity",
                )
            try:
                assert_event_authority_reconciliation_ready(projection)
            except HistoricalRevisionError as exc:
                raise StoryProjectContextError(exc.code, str(exc)) from exc
            canonical = canonical_memory_to_snapshot(projection)
            canonical["book_id"] = identity["book_id"]
            canonical["semantic_authority"] = {
                "source": "memory_event_v2_2",
                "reducer_version": "memory-reducer-2.2",
                "authority_epoch": authority["authority_epoch"],
                "head_event_hash": authority["head_event_hash"],
                "parser_authoritative": False,
            }
            return normalize_snapshot(canonical)
        merged = dict(snapshot)
        memory_v2 = context.get("memory_v2")
        projection = memory_v2.get("projection") if isinstance(memory_v2, dict) else None
        if isinstance(projection, dict):
            merged = _deep_merge_dict(canonical_memory_to_snapshot(projection), merged)
        semantic = context.get("semantic_state")
        if not isinstance(semantic, dict):
            raise StoryProjectContextError(
                "strict_semantic_state_missing",
                "strict StoryProject context is missing authoritative semantic state",
            )
        for field in (
            "story_state",
            "world_state",
            "spatial_state",
            "characters",
            "timeline",
            "constraints",
            "foreshadowing",
        ):
            merged[field] = copy.deepcopy(semantic.get(field))
        merged["semantic_authority"] = {
            "source": "story_project",
            "parser_version": semantic.get("parser_version"),
            "layout_profile_version": semantic.get("layout_profile_version"),
            "source_digest": semantic.get("source_digest"),
            "provenance": copy.deepcopy(semantic.get("provenance") or []),
        }
        return normalize_snapshot(merged)
    @staticmethod
    def require_strict_writeback(
        context: dict[str, Any] | None,
        *,
        persist: bool,
        writeback_mode: str,
    ) -> None:
        if persist and isinstance(context, dict) and context.get("story_state_mode") == "strict" and writeback_mode != "apply":
            raise StoryProjectContextError(
                "strict_story_state_requires_apply_writeback",
                "persistent strict StoryProject execution requires --story-project-writeback",
            )

    @staticmethod
    def require_apply_persistence(*, enabled: bool, persist: bool, writeback_mode: str) -> None:
        if enabled and writeback_mode == "apply" and not persist:
            raise StoryProjectContextError(
                "story_project_apply_requires_persistence",
                "StoryProject apply writeback requires persist=True",
            )

    @staticmethod
    def chapter_blueprint(context: dict[str, Any] | None) -> dict[str, Any] | None:
        if not context:
            return None
        blueprint = context.get("chapter_blueprint")
        return blueprint if isinstance(blueprint, dict) else None

    @staticmethod
    def prepare_identity_for_persistence(
        *,
        configured_context: Any,
        loader: StoryProjectContextLoader | None,
        persistence_dir: Path,
    ) -> PreparedStoryProjectIdentity:
        root = None
        identity_payload = None
        if isinstance(configured_context, dict):
            root = configured_context.get("story_project_root")
            identity_payload = configured_context.get("project_identity")
        elif configured_context is not None and hasattr(configured_context, "to_dict"):
            payload = configured_context.to_dict()
            root = payload.get("story_project_root")
            identity_payload = payload.get("project_identity")
        if root is None and loader is not None:
            root = getattr(loader, "story_project_root", None)
            loader_identity = getattr(loader, "project_identity", None)
            if loader_identity is not None and hasattr(loader_identity, "to_dict"):
                identity_payload = loader_identity.to_dict()
        if root is None:
            return PreparedStoryProjectIdentity(None, None, False)
        stable = ensure_project_identity_for_runtime(root, persistence_dir=persistence_dir)
        current = validate_project_identity(identity_payload) if isinstance(identity_payload, dict) else None
        allow_legacy = current is None or current.ephemeral
        if current is not None and not current.ephemeral:
            assert_project_identity(stable, current.book_id, source="StoryProject executor configuration")
        return PreparedStoryProjectIdentity(stable.book_id, stable.to_dict(), allow_legacy)


def _event_authority_memory_context(
    context: dict[str, Any], identity: Any
) -> dict[str, Any]:
    """Expose only the current outline; legacy memory and run history are audit-only."""

    overlay = context.get("memory_context_overlay")
    raw_items = overlay.get("items") if isinstance(overlay, dict) else None
    items: list[dict[str, Any]] = []
    old_to_new: dict[int, int] = {}
    if isinstance(raw_items, list):
        for old_index, item in enumerate(raw_items):
            if (
                not isinstance(item, dict)
                or item.get("source") != "story_project"
                or item.get("name") != "current_outline"
            ):
                continue
            old_to_new[old_index] = len(items)
            items.append(copy.deepcopy(item))

    mappings: list[dict[str, Any]] = []
    raw_mappings = overlay.get("source_mappings") if isinstance(overlay, dict) else None
    if isinstance(raw_mappings, list):
        for mapping in raw_mappings:
            if not isinstance(mapping, dict) or mapping.get("index") not in old_to_new:
                continue
            copied = copy.deepcopy(mapping)
            copied["index"] = old_to_new[mapping["index"]]
            mappings.append(copied)

    return {
        "source": "story_project_event_v1",
        "status": "ready",
        "items": items,
        "source_mappings": mappings,
        "story_project": {
            "enabled": True,
            "chapter_index": context.get("chapter_index"),
            "book_id": identity.book_id,
        },
        "legacy_context_mode": "audit_only",
    }


def _deep_merge_dict(base: dict[str, Any], overlay: Any) -> dict[str, Any]:
    if not isinstance(overlay, dict):
        return dict(base)
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


__all__ = [
    "NormalizedStoryProjectContext",
    "PreparedStoryProjectIdentity",
    "StoryProjectContextError",
    "StoryProjectContextLoader",
    "StoryProjectContextService",
]
