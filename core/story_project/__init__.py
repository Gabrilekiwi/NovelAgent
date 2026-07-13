from __future__ import annotations

from core.story_project.model import (
    ChapterResolution,
    ChapterBlueprint,
    PathResolution,
    SourcePathSet,
    SourceResolution,
    SourceResolutionEntry,
    StoryProjectRootResolution,
    StoryProjectRuntimeContext,
    StoryProjectValidationResult,
    ValidationProblem,
)
from core.story_project.mapper import build_story_project_runtime_context
from core.story_project.oh_story_detection import (
    detect_oh_story_compatibility,
    failed_oh_story_compatibility_report,
)
from core.story_project.paths import (
    canonical_outline_path,
    canonical_prose_path,
    infer_next_chapter,
    read_active_book_path,
    resolve_outline,
    resolve_prose,
    resolve_story_project_root,
)
from core.story_project.semantic_contracts import (
    STORY_PROJECT_SEMANTIC_FIXTURE_SCHEMA_VERSION,
    STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION,
    validate_story_project_semantic_fixture_manifest,
    validate_story_project_semantic_state,
)
from core.story_project.validator import validate_story_project
from core.story_project.writer import (
    StoryProjectWritebackConfig,
    build_story_project_writeback_plan,
    run_story_project_writeback,
)

__all__ = [
    "ChapterResolution",
    "ChapterBlueprint",
    "PathResolution",
    "SourcePathSet",
    "SourceResolution",
    "SourceResolutionEntry",
    "StoryProjectRootResolution",
    "StoryProjectRuntimeContext",
    "STORY_PROJECT_SEMANTIC_FIXTURE_SCHEMA_VERSION",
    "STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION",
    "StoryProjectValidationResult",
    "StoryProjectWritebackConfig",
    "ValidationProblem",
    "build_story_project_runtime_context",
    "build_story_project_writeback_plan",
    "canonical_outline_path",
    "canonical_prose_path",
    "detect_oh_story_compatibility",
    "failed_oh_story_compatibility_report",
    "infer_next_chapter",
    "read_active_book_path",
    "resolve_outline",
    "resolve_prose",
    "resolve_story_project_root",
    "run_story_project_writeback",
    "validate_story_project",
    "validate_story_project_semantic_fixture_manifest",
    "validate_story_project_semantic_state",
]
