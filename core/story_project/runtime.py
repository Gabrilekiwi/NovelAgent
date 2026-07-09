from __future__ import annotations

from pathlib import Path
from typing import Any

from core.story_project.mapper import build_story_project_runtime_context
from core.story_project.model import StoryProjectRuntimeContext
from core.story_project.validator import validate_story_project


def build_generation_story_project_context(
    *,
    story_project: str | Path,
    chapter: str | int | None = "auto",
    snapshot: dict[str, Any] | None = None,
    memory_context: dict[str, Any] | None = None,
) -> StoryProjectRuntimeContext:
    validation = validate_story_project(story_project=story_project, chapter=chapter)
    if not validation.ok:
        blocking = [problem.message for problem in validation.problems if problem.blocking]
        raise ValueError("; ".join(blocking) or "StoryProject validation failed.")
    root = validation.root_resolution.root if validation.root_resolution else None
    chapter_resolution = validation.chapter_resolution
    chapter_index = chapter_resolution.resolved_chapter if chapter_resolution else None
    if root is None or chapter_index is None:
        raise ValueError("StoryProject generation requires a resolved root and chapter.")
    return build_story_project_runtime_context(
        root,
        chapter_index,
        snapshot=snapshot,
        memory_context=memory_context,
    )


__all__ = ["build_generation_story_project_context"]
