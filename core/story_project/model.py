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
