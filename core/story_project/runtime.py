from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.story_project.mapper import build_story_project_runtime_context
from core.story_project.identity import ProjectIdentity, create_ephemeral_project_identity
from core.story_project.activation import evaluate_story_state_activation
from core.story_project.semantic_parser import parse_story_project_semantic_state
from core.story_project.model import StoryProjectRuntimeContext
from core.story_project.paths import resolve_story_project_root
from core.story_project.read_set import capture_story_project_read_set
from core.story_project.validator import validate_story_project
from core.memory_v2 import load_canonical_memory, memory_projection_hash
from core.memory_v2.recovery import (
    MemoryAuthorityMismatchError,
    ensure_event_authority_caches,
)
from core.runtime_paths import RuntimePaths
from core.story_project.authority import AUTHORITY_MODE_EVENT
from core.memory_v2.canonical import canonical_json_hash
from core.story_project.semantic_parser import SEMANTIC_PARSER_VERSION


class StoryProjectSequenceDriftError(ValueError):
    code = "story_project_sequence_drift"

    def __init__(self, *, expected_chapter: int, resolved_chapter: int) -> None:
        self.expected_chapter = int(expected_chapter)
        self.resolved_chapter = int(resolved_chapter)
        super().__init__(
            f"{self.code}: expected chapter {self.expected_chapter}, "
            f"but StoryProject resolved chapter {self.resolved_chapter}"
        )


@dataclass(frozen=True)
class GenerationStoryProjectContextLoader:
    """Rebuild a generation context from one pinned StoryProject root per call."""

    story_project_root: Path
    requested_chapter: str
    project_identity: ProjectIdentity
    overwrite: bool = False
    allow_story_state_shadow_downgrade: bool = False
    outline_override: dict[str, Any] | None = None

    def __call__(
        self,
        snapshot: dict[str, Any],
        memory_context: dict[str, Any],
        chapter_hint: int | None = None,
    ) -> StoryProjectRuntimeContext:
        hint = _validated_chapter_hint(chapter_hint)
        chapter = self.requested_chapter if self.requested_chapter == "auto" or hint is None else hint
        context = build_generation_story_project_context(
            story_project=self.story_project_root,
            chapter=chapter,
            snapshot=snapshot,
            memory_context=memory_context,
            overwrite=self.overwrite,
            project_identity=self.project_identity,
            allow_story_state_shadow_downgrade=self.allow_story_state_shadow_downgrade,
            outline_override=self.outline_override,
        )
        if hint is not None and context.chapter_index != hint:
            raise StoryProjectSequenceDriftError(
                expected_chapter=hint,
                resolved_chapter=context.chapter_index,
            )
        return context


def build_generation_story_project_context(
    *,
    story_project: str | Path,
    chapter: str | int | None = "auto",
    snapshot: dict[str, Any] | None = None,
    memory_context: dict[str, Any] | None = None,
    overwrite: bool = False,
    project_identity: ProjectIdentity | None = None,
    allow_story_state_shadow_downgrade: bool = False,
    outline_override: dict[str, Any] | None = None,
) -> StoryProjectRuntimeContext:
    validation = validate_story_project(
        story_project=story_project,
        chapter=chapter,
        allow_existing_prose=overwrite,
        allow_missing_outline=outline_override is not None,
    )
    if not validation.ok:
        blocking = [problem.message for problem in validation.problems if problem.blocking]
        raise ValueError("; ".join(blocking) or "StoryProject validation failed.")
    root = validation.root_resolution.root if validation.root_resolution else None
    chapter_resolution = validation.chapter_resolution
    chapter_index = chapter_resolution.resolved_chapter if chapter_resolution else None
    if root is None or chapter_index is None:
        raise ValueError("StoryProject generation requires a resolved root and chapter.")
    identity = project_identity or create_ephemeral_project_identity(root)
    event_authority = _uses_event_authority(identity)
    context = build_story_project_runtime_context(
        root,
        chapter_index,
        snapshot=snapshot,
        memory_context=memory_context,
        previous_chapter_fail_closed=identity.story_state_mode == "strict" or event_authority,
        outline_override=outline_override,
    )
    memory_v2 = _load_memory_v2_context(root, identity)
    if event_authority and outline_override is not None:
        activation = _event_authority_activation(identity)
        read_set = capture_story_project_read_set(
            root,
            chapter_index,
            project_identity=identity,
            parser_version=SEMANTIC_PARSER_VERSION,
            parse_status="warning" if context.warnings else "ok",
        )
        semantic_state = None
        semantic_audit = {
            **activation,
            "parser_version": SEMANTIC_PARSER_VERSION,
            "semantic_schema_version": "event-outline-1.0",
            "layout_profile_version": "autonomy-checkpoint-1.0",
            "source_digest": canonical_json_hash(
                {
                    "story_project_context_digest": read_set["context_digest"],
                    "outline_checkpoint_hash": outline_override.get("checkpoint_hash"),
                    "outline_hash": outline_override.get("outline_hash"),
                }
            ),
            "provenance_count": 1,
            "blocking_conflict_count": 0,
            "warning_count": len(context.warnings),
            "parser_authoritative": False,
            "canonical_authoritative": True,
        }
    else:
        semantic_state = parse_story_project_semantic_state(
            root,
            chapter_index,
            project_identity=identity,
        )
        activation = (
            _event_authority_activation(identity)
            if event_authority
            else evaluate_story_state_activation(
                identity,
                semantic_state,
                allow_shadow_downgrade=allow_story_state_shadow_downgrade,
            )
        )
        semantic_audit = {
            **activation,
            "parser_version": semantic_state["parser_version"],
            "semantic_schema_version": semantic_state["schema_version"],
            "layout_profile_version": semantic_state["layout_profile_version"],
            "source_digest": semantic_state["source_digest"],
            "provenance_count": len(semantic_state["provenance"]),
            "blocking_conflict_count": sum(
                1 for item in semantic_state["conflicts"] if item.get("blocking")
            ),
            "warning_count": len(semantic_state["parse_warnings"]),
            "parser_authoritative": False if event_authority else bool(activation["authoritative"]),
            "canonical_authoritative": event_authority,
        }
        read_set = capture_story_project_read_set(
            root,
            chapter_index,
            project_identity=identity,
            parser_version=str(semantic_state["parser_version"]),
            parse_status=(
                "warning"
                if context.warnings or semantic_state["parse_warnings"] or activation["downgraded"]
                else "ok"
            ),
        )
    return replace(
        context,
        chapter_resolution=chapter_resolution,
        project_identity=identity.to_dict(),
        read_set=read_set,
        story_state_mode=str(activation["effective_mode"]),
        semantic_state=(
            semantic_state
            if semantic_state is not None and activation["authoritative"] and not event_authority
            else None
        ),
        semantic_audit=semantic_audit,
        memory_v2=memory_v2,
    )


def build_generation_story_project_context_loader(
    *,
    story_project: str | Path,
    chapter: str | int | None = "auto",
    overwrite: bool = False,
    workspace_root: str | Path | None = None,
    project_identity: ProjectIdentity | None = None,
    allow_story_state_shadow_downgrade: bool = False,
    outline_override: dict[str, Any] | None = None,
) -> GenerationStoryProjectContextLoader:
    resolution = resolve_story_project_root(story_project, workspace_root=workspace_root)
    if not resolution.ok or resolution.root is None:
        raise ValueError(resolution.error or "StoryProject root could not be resolved.")
    requested_chapter = _normalized_requested_chapter(chapter)
    identity = project_identity or create_ephemeral_project_identity(resolution.root)
    return GenerationStoryProjectContextLoader(
        story_project_root=resolution.root.resolve(),
        requested_chapter=requested_chapter,
        project_identity=identity,
        overwrite=bool(overwrite),
        allow_story_state_shadow_downgrade=bool(allow_story_state_shadow_downgrade),
        outline_override=outline_override,
    )


def _load_memory_v2_context(root: Path, identity: ProjectIdentity) -> dict[str, Any]:
    runtime_paths = RuntimePaths.for_story_project(root)
    memory_root = runtime_paths.memory_dir / "v2"
    canonical_path = memory_root / "canonical_memory.json"
    event_authority = _uses_event_authority(identity)
    replay_report: dict[str, Any] | None = None
    cache_status: str | None = None
    recovery_report: dict[str, Any] | None = None
    if event_authority:
        authority = identity.authority or {}
        try:
            ensured = ensure_event_authority_caches(
                memory_root,
                runtime_root=runtime_paths.runtime_dir,
                runtime_snapshot_target=runtime_paths.snapshot_path,
                expected_book_id=identity.book_id,
                expected_authority_epoch=int(authority["authority_epoch"]),
                expected_head_event_hash=str(authority["head_event_hash"]),
            )
        except MemoryAuthorityMismatchError as exc:
            mismatch_codes = {
                "book_id": "story_project_memory_v2_identity_mismatch",
                "authority_epoch": "story_project_event_authority_epoch_mismatch",
                "head_event_hash": "story_project_event_authority_head_mismatch",
            }
            raise ValueError(mismatch_codes[exc.field]) from exc
        except (OSError, ValueError) as exc:
            raise ValueError("story_project_event_authority_replay_failed") from exc
        projection = ensured["projection"]
        replay_report = ensured["replay_report"]
        cache_status = str(ensured["cache_status"])
        recovery_report = ensured.get("recovery_report")
    elif not canonical_path.exists():
        return {
            "status": "absent",
            "canonical_path": str(canonical_path),
            "event_store": str(memory_root / "events"),
            "revision": None,
            "projection_hash": None,
            "projection": None,
        }
    else:
        projection = load_canonical_memory(canonical_path)
    if str(projection["book_id"]) != identity.book_id:
        raise ValueError("story_project_memory_v2_identity_mismatch")
    return {
        "status": "ready",
        "canonical_path": str(canonical_path),
        "event_store": str(memory_root / "events"),
        "revision": int(projection["revision"]),
        "projection_hash": memory_projection_hash(projection),
        "projection": projection,
        "authority_epoch": projection.get("authority_epoch"),
        "head_event_hash": projection.get("head_event_hash"),
        "reducer_version": (
            replay_report.get("reducer_version")
            if replay_report is not None
            else "memory-reducer-2.2" if projection.get("schema_version") == "2.2" else None
        ),
        "replay_projection_hash": (
            replay_report.get("projection_hash") if replay_report is not None else None
        ),
        "cache_status": cache_status,
        "cache_recovery_hash": (
            recovery_report.get("recovery_hash")
            if isinstance(recovery_report, dict)
            else None
        ),
    }


def _uses_event_authority(identity: ProjectIdentity) -> bool:
    authority = identity.authority
    return isinstance(authority, dict) and authority.get("mode") == AUTHORITY_MODE_EVENT


def _event_authority_activation(identity: ProjectIdentity) -> dict[str, Any]:
    authority = identity.authority or {}
    return {
        "configured_mode": identity.story_state_mode,
        "effective_mode": "strict",
        "authoritative": False,
        "profile_match": None,
        "downgraded": False,
        "ready_for_next_step": True,
        "blockers": [],
        "authority_source": "memory_event_v2_2",
        "authority_epoch": authority.get("authority_epoch"),
        "head_event_hash": authority.get("head_event_hash"),
    }


def _normalized_requested_chapter(chapter: str | int | None) -> str:
    requested = "auto" if chapter is None else str(chapter)
    if requested == "auto":
        return requested
    try:
        chapter_index = int(requested)
    except ValueError as exc:
        raise ValueError("--chapter must be a positive integer or auto.") from exc
    if chapter_index < 1:
        raise ValueError("--chapter must be a positive integer or auto.")
    return str(chapter_index)


def _validated_chapter_hint(chapter_hint: int | None) -> int | None:
    if chapter_hint is None:
        return None
    if isinstance(chapter_hint, bool) or not isinstance(chapter_hint, int) or chapter_hint < 1:
        raise ValueError("chapter_hint must be a positive integer")
    return chapter_hint


__all__ = [
    "GenerationStoryProjectContextLoader",
    "StoryProjectSequenceDriftError",
    "build_generation_story_project_context",
    "build_generation_story_project_context_loader",
]
