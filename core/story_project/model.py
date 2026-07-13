from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CORE_DIRECTORY_NAMES = ("设定", "大纲", "正文", "追踪")


@dataclass(frozen=True)
class StoryProjectRootResolution:
    requested: str
    root: Path | None
    source: str
    active_book_path: Path | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.root is not None and self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "root": str(self.root) if self.root else None,
            "source": self.source,
            "active_book_path": str(self.active_book_path) if self.active_book_path else None,
            "error": self.error,
        }


@dataclass(frozen=True)
class PathResolution:
    chapter_index: int
    path: Path | None
    candidates: tuple[Path, ...] = ()
    canonical_path: Path | None = None

    @property
    def found(self) -> bool:
        return self.path is not None

    @property
    def conflict(self) -> bool:
        return len(self.candidates) > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_index": self.chapter_index,
            "path": str(self.path) if self.path else None,
            "candidates": [str(path) for path in self.candidates],
            "canonical_path": str(self.canonical_path) if self.canonical_path else None,
            "conflict": self.conflict,
        }


@dataclass(frozen=True)
class ChapterResolution:
    requested: str
    resolved_chapter: int | None
    basis: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "resolved_chapter": self.resolved_chapter,
            "basis": list(self.basis),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ValidationProblem:
    code: str
    message: str
    blocking: bool = True
    severity: str = "high"
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "blocking": self.blocking,
            "severity": self.severity,
            "path": str(self.path) if self.path else None,
        }


@dataclass(frozen=True)
class StoryProjectValidationResult:
    enabled: bool
    root_resolution: StoryProjectRootResolution | None = None
    chapter_resolution: ChapterResolution | None = None
    outline_resolution: PathResolution | None = None
    prose_resolution: PathResolution | None = None
    core_directories: dict[str, str] = field(default_factory=dict)
    problems: tuple[ValidationProblem, ...] = ()
    warnings: tuple[ValidationProblem, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(problem.blocking for problem in self.problems)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "ok": self.ok,
            "root": self.root_resolution.to_dict() if self.root_resolution else None,
            "chapter_resolution": self.chapter_resolution.to_dict() if self.chapter_resolution else None,
            "outline_resolution": self.outline_resolution.to_dict() if self.outline_resolution else None,
            "prose_resolution": self.prose_resolution.to_dict() if self.prose_resolution else None,
            "core_directories": dict(self.core_directories),
            "problems": [problem.to_dict() for problem in self.problems],
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


@dataclass(frozen=True)
class ChapterBlueprint:
    chapter_index: int
    outline_path: Path
    title: str
    core_event: str | None = None
    required_beats: tuple[dict[str, Any], ...] = ()
    ending_pressure: str | None = None
    source_path: Path | None = None
    missing_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_index": self.chapter_index,
            "outline_path": str(self.outline_path),
            "title": self.title,
            "core_event": self.core_event,
            "required_beats": [dict(beat) for beat in self.required_beats],
            "ending_pressure": self.ending_pressure,
            "source_path": str(self.source_path or self.outline_path),
            "missing_fields": list(self.missing_fields),
        }


@dataclass(frozen=True)
class SourcePathSet:
    story_project_root: Path
    outline_path: Path
    previous_prose_path: Path | None = None
    tracking_paths: dict[str, Path] = field(default_factory=dict)
    setting_paths: dict[str, Path] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "story_project_root": str(self.story_project_root),
            "outline_path": str(self.outline_path),
            "previous_prose_path": str(self.previous_prose_path) if self.previous_prose_path else None,
            "tracking_paths": {name: str(path) for name, path in sorted(self.tracking_paths.items())},
            "setting_paths": {name: str(path) for name, path in sorted(self.setting_paths.items())},
        }


@dataclass(frozen=True)
class SourceResolutionEntry:
    field: str
    chosen_source: str
    discarded_sources: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "chosen_source": self.chosen_source,
            "discarded_sources": list(self.discarded_sources),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SourceResolution:
    precedence: tuple[str, ...] = ()
    entries: tuple[SourceResolutionEntry, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "precedence": list(self.precedence),
            "entries": [entry.to_dict() for entry in self.entries],
        }


@dataclass(frozen=True)
class StoryProjectRuntimeContext:
    story_project_root: Path
    chapter_index: int
    outline: dict[str, Any]
    previous_prose: dict[str, Any] | None
    previous_chapter_context: dict[str, Any] | None
    tracking_files: dict[str, dict[str, Any]]
    setting_files: dict[str, dict[str, Any]]
    snapshot_overlay: dict[str, Any]
    memory_context_overlay: dict[str, Any]
    chapter_blueprint: ChapterBlueprint
    source_paths: SourcePathSet
    source_resolution: SourceResolution
    project_identity: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    chapter_resolution: ChapterResolution | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "story_project_root": str(self.story_project_root),
            "chapter_index": self.chapter_index,
            "outline": dict(self.outline),
            "previous_prose": dict(self.previous_prose) if self.previous_prose else None,
            "previous_chapter_context": (
                dict(self.previous_chapter_context) if self.previous_chapter_context else None
            ),
            "tracking_files": {name: dict(value) for name, value in sorted(self.tracking_files.items())},
            "setting_files": {name: dict(value) for name, value in sorted(self.setting_files.items())},
            "snapshot_overlay": self.snapshot_overlay,
            "memory_context_overlay": self.memory_context_overlay,
            "chapter_blueprint": self.chapter_blueprint.to_dict(),
            "source_paths": self.source_paths.to_dict(),
            "source_resolution": self.source_resolution.to_dict(),
            "project_identity": dict(self.project_identity) if self.project_identity else None,
            "warnings": list(self.warnings),
            "missing_fields": list(self.missing_fields),
            "chapter_resolution": self.chapter_resolution.to_dict() if self.chapter_resolution else None,
        }
