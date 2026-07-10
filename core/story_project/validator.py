from __future__ import annotations

from pathlib import Path

from core.story_project.model import (
    CORE_DIRECTORY_NAMES,
    ChapterResolution,
    StoryProjectValidationResult,
    ValidationProblem,
)
from core.story_project.paths import (
    infer_next_chapter,
    resolve_outline,
    resolve_prose,
    resolve_story_project_root,
    scan_prose_chapters,
)


def validate_story_project(
    *,
    story_project: str | Path,
    chapter: str | int | None = "auto",
    workspace_root: str | Path | None = None,
    allow_existing_prose: bool = False,
) -> StoryProjectValidationResult:
    root_resolution = resolve_story_project_root(story_project, workspace_root=workspace_root)
    problems: list[ValidationProblem] = []
    warnings: list[ValidationProblem] = []
    core_directories: dict[str, str] = {}

    if root_resolution.error:
        problems.append(
            ValidationProblem(
                code="story_project_root_invalid",
                message=root_resolution.error,
                path=root_resolution.root,
            )
        )
        return StoryProjectValidationResult(
            enabled=True,
            root_resolution=root_resolution,
            problems=tuple(problems),
            warnings=tuple(warnings),
        )

    story_project_root = root_resolution.root
    if story_project_root is None:
        problems.append(
            ValidationProblem(
                code="story_project_root_missing",
                message="StoryProject root could not be resolved.",
            )
        )
        return StoryProjectValidationResult(
            enabled=True,
            root_resolution=root_resolution,
            problems=tuple(problems),
            warnings=tuple(warnings),
        )

    for directory_name in CORE_DIRECTORY_NAMES:
        path = story_project_root / directory_name
        core_directories[directory_name] = str(path)
        if not path.is_dir():
            problems.append(
                ValidationProblem(
                    code="missing_core_directory",
                    message=f"Missing required StoryProject directory: {directory_name}/",
                    path=path,
                )
            )

    chapter_resolution = _resolve_chapter(
        story_project_root,
        chapter=chapter,
        problems=problems,
    )
    outline_resolution = None
    prose_resolution = None

    if not any(problem.code == "missing_core_directory" for problem in problems):
        _check_duplicate_prose_files(story_project_root, problems)
        if chapter_resolution.resolved_chapter is not None:
            outline_resolution = resolve_outline(story_project_root, chapter_resolution.resolved_chapter)
            if outline_resolution.conflict:
                problems.append(
                    ValidationProblem(
                        code="outline_chapter_conflict",
                        message=(
                            f"Multiple outline files matched chapter "
                            f"{chapter_resolution.resolved_chapter}."
                        ),
                        path=story_project_root / "大纲",
                    )
                )
            elif not outline_resolution.found:
                problems.append(
                    ValidationProblem(
                        code="outline_chapter_missing",
                        message=(
                            f"No compatible outline file matched chapter "
                            f"{chapter_resolution.resolved_chapter}."
                        ),
                        path=outline_resolution.canonical_path,
                    )
                )

            prose_resolution = resolve_prose(story_project_root, chapter_resolution.resolved_chapter)
            if prose_resolution.conflict:
                problems.append(
                    ValidationProblem(
                        code="prose_chapter_conflict",
                        message=f"Multiple prose files matched chapter {chapter_resolution.resolved_chapter}.",
                        path=story_project_root / "正文",
                    )
                )
            elif prose_resolution.found and not allow_existing_prose:
                problems.append(
                    ValidationProblem(
                        code="target_prose_exists",
                        message=f"Target prose file already exists for chapter {chapter_resolution.resolved_chapter}.",
                        path=prose_resolution.path,
                    )
                )

    return StoryProjectValidationResult(
        enabled=True,
        root_resolution=root_resolution,
        chapter_resolution=chapter_resolution,
        outline_resolution=outline_resolution,
        prose_resolution=prose_resolution,
        core_directories=core_directories,
        problems=tuple(problems),
        warnings=tuple(warnings),
    )


def _resolve_chapter(
    story_project_root: Path,
    *,
    chapter: str | int | None,
    problems: list[ValidationProblem],
) -> ChapterResolution:
    requested = "auto" if chapter is None else str(chapter)
    if requested == "auto":
        return ChapterResolution(
            requested=requested,
            resolved_chapter=infer_next_chapter(story_project_root),
            basis=("正文/",),
            warnings=(),
        )
    try:
        chapter_index = int(requested)
    except ValueError:
        problems.append(
            ValidationProblem(
                code="invalid_chapter",
                message="--chapter must be a positive integer or auto.",
            )
        )
        return ChapterResolution(requested=requested, resolved_chapter=None)
    if chapter_index < 1:
        problems.append(
            ValidationProblem(
                code="invalid_chapter",
                message="--chapter must be a positive integer or auto.",
            )
        )
        return ChapterResolution(requested=requested, resolved_chapter=None)
    return ChapterResolution(requested=requested, resolved_chapter=chapter_index, basis=("cli",))


def _check_duplicate_prose_files(story_project_root: Path, problems: list[ValidationProblem]) -> None:
    for chapter_index, paths in scan_prose_chapters(story_project_root).items():
        if len(paths) > 1:
            problems.append(
                ValidationProblem(
                    code="prose_chapter_conflict",
                    message=f"Multiple prose files matched chapter {chapter_index}.",
                    path=story_project_root / "正文",
                )
            )
