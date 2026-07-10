from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.story_project.mapper import build_story_project_runtime_context
from core.story_project.model import StoryProjectRuntimeContext
from core.story_project.paths import resolve_story_project_root
from core.story_project.validator import validate_story_project


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
    overwrite: bool = False

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
    return replace(
        build_story_project_runtime_context(
            root,
            chapter_index,
            snapshot=snapshot,
            memory_context=memory_context,
        ),
        chapter_resolution=chapter_resolution,
    )


def build_generation_story_project_context_loader(
    *,
    story_project: str | Path,
    chapter: str | int | None = "auto",
    overwrite: bool = False,
    workspace_root: str | Path | None = None,
) -> GenerationStoryProjectContextLoader:
    resolution = resolve_story_project_root(story_project, workspace_root=workspace_root)
    if not resolution.ok or resolution.root is None:
        raise ValueError(resolution.error or "StoryProject root could not be resolved.")
    requested_chapter = _normalized_requested_chapter(chapter)
    return GenerationStoryProjectContextLoader(
        story_project_root=resolution.root.resolve(),
        requested_chapter=requested_chapter,
        overwrite=bool(overwrite),
    )


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
