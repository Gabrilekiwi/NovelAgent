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
from core.runtime_paths import RuntimePaths


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
) -> StoryProjectRuntimeContext:
    validation = validate_story_project(
        story_project=story_project,
        chapter=chapter,
        allow_existing_prose=overwrite,
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
    context = build_story_project_runtime_context(
        root,
        chapter_index,
        snapshot=snapshot,
        memory_context=memory_context,
        previous_chapter_fail_closed=identity.story_state_mode == "strict",
    )
    semantic_state = parse_story_project_semantic_state(
        root,
        chapter_index,
        project_identity=identity,
    )
    activation = evaluate_story_state_activation(
        identity,
        semantic_state,
        allow_shadow_downgrade=allow_story_state_shadow_downgrade,
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
    }
    memory_v2 = _load_memory_v2_context(root, identity)
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
        semantic_state=(semantic_state if activation["authoritative"] else None),
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
    )


def _load_memory_v2_context(root: Path, identity: ProjectIdentity) -> dict[str, Any]:
    memory_root = RuntimePaths.for_story_project(root).memory_dir / "v2"
    canonical_path = memory_root / "canonical_memory.json"
    if not canonical_path.exists():
        return {
            "status": "absent",
            "canonical_path": str(canonical_path),
            "event_store": str(memory_root / "events"),
            "revision": None,
            "projection_hash": None,
            "projection": None,
        }
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
