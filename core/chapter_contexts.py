from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from core.path_refs import PathRef, path_ref_for
from core.schema import validate_schema


CHAPTER_CONTEXT_SCHEMA_VERSION = "1.0"
DEFAULT_GENERATION_EXCERPT_CHARS = 24_000
DEFAULT_REVIEW_TAIL_CHARS = 12_000


class ChapterContextError(ValueError):
    def __init__(self, code: str, message: str, *, risk: str = "high") -> None:
        self.code = code
        self.risk = risk
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class PreviousChapterContext:
    chapter_index: int
    source_kind: str
    path_ref: PathRef
    sha256: str
    original_chars: int
    generation_excerpt: dict[str, Any]
    review_tail: dict[str, Any]
    committed_verified: bool = True

    def to_dict(self) -> dict[str, Any]:
        return validate_schema(
            {
                "schema_version": CHAPTER_CONTEXT_SCHEMA_VERSION,
                "chapter_index": self.chapter_index,
                "source_kind": self.source_kind,
                "path_ref": self.path_ref.to_dict(),
                "sha256": self.sha256,
                "original_chars": self.original_chars,
                "generation_excerpt": dict(self.generation_excerpt),
                "review_tail": dict(self.review_tail),
                "committed_verified": self.committed_verified,
            },
            "previous_chapter_context.schema.json",
        )

    def to_legacy_dict(self, path: Path) -> dict[str, Any]:
        excerpt = self.generation_excerpt
        return {
            "path": str(path),
            "relative_path": self.path_ref.relative_path,
            "text": excerpt["text"],
            "chars": self.original_chars,
            "sha256": self.sha256,
            "truncated": excerpt["truncated"],
            "excerpt_ranges": [dict(item) for item in excerpt["ranges"]],
            "review_tail": dict(self.review_tail),
            "context_kind": "previous_chapter",
        }


@dataclass(frozen=True)
class AttemptContext:
    chapter_index: int
    run_id: str
    status: str
    sha256: str
    original_chars: int
    excerpt: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return validate_schema(
            {
                "schema_version": CHAPTER_CONTEXT_SCHEMA_VERSION,
                "chapter_index": self.chapter_index,
                "run_id": self.run_id,
                "status": self.status,
                "sha256": self.sha256,
                "original_chars": self.original_chars,
                "excerpt": dict(self.excerpt),
            },
            "attempt_context.schema.json",
        )


@dataclass(frozen=True)
class RecoveryContext:
    chapter_index: int
    source_run_id: str
    source_status: str
    sha256: str
    original_chars: int
    excerpt: dict[str, Any]
    artifact_path_ref: PathRef | None = None
    artifact_hash_verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return validate_schema(
            {
                "schema_version": CHAPTER_CONTEXT_SCHEMA_VERSION,
                "chapter_index": self.chapter_index,
                "source_run_id": self.source_run_id,
                "source_status": self.source_status,
                "sha256": self.sha256,
                "original_chars": self.original_chars,
                "excerpt": dict(self.excerpt),
                "artifact_path_ref": self.artifact_path_ref.to_dict() if self.artifact_path_ref else None,
                "artifact_hash_verified": self.artifact_hash_verified,
            },
            "recovery_context.schema.json",
        )


def resolve_story_project_previous_chapter(
    story_project_root: str | Path,
    chapter_index: int,
    *,
    generation_max_chars: int = DEFAULT_GENERATION_EXCERPT_CHARS,
    review_tail_chars: int = DEFAULT_REVIEW_TAIL_CHARS,
    fail_closed: bool = True,
) -> PreviousChapterContext | None:
    from core.story_project.paths import resolve_prose

    root = Path(story_project_root).resolve()
    _validate_chapter_index(chapter_index)
    if chapter_index == 1:
        return None
    resolution = resolve_prose(root, chapter_index - 1)
    if resolution.conflict:
        raise ChapterContextError(
            "previous_chapter_conflict",
            f"multiple prose files matched chapter {chapter_index - 1}",
        )
    if resolution.path is None:
        if fail_closed:
            raise ChapterContextError(
                "previous_chapter_missing",
                f"no prose file matched chapter {chapter_index - 1}",
            )
        return None
    raw = resolution.path.read_bytes()
    text = raw.decode("utf-8-sig")
    return _previous_context_from_text(
        text=text,
        chapter_index=chapter_index - 1,
        source_kind="story_project_prose",
        path_ref=path_ref_for(resolution.path, root_id="story_project", root=root),
        generation_max_chars=generation_max_chars,
        review_tail_chars=review_tail_chars,
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )


def resolve_committed_previous_chapter_artifact(
    *,
    chapter_index: int,
    run_dir: str | Path,
    chapter_artifact_root: str | Path,
    generation_max_chars: int = DEFAULT_GENERATION_EXCERPT_CHARS,
    review_tail_chars: int = DEFAULT_REVIEW_TAIL_CHARS,
) -> PreviousChapterContext | None:
    _validate_chapter_index(chapter_index)
    if chapter_index == 1:
        return None
    run_root = Path(run_dir)
    artifact_root = Path(chapter_artifact_root).resolve()
    valid: dict[Path, tuple[str, str]] = {}
    for run_path in sorted(run_root.glob("chapter_*.json")) if run_root.is_dir() else ():
        try:
            payload = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        run = payload.get("run") if isinstance(payload, dict) else None
        if not isinstance(run, dict):
            continue
        if run.get("committed") is not True or run.get("status") != "committed":
            continue
        if run.get("chapter_index") != chapter_index - 1:
            continue
        artifact = ((run.get("chapter") or {}).get("artifact") or {}) if isinstance(run.get("chapter"), dict) else {}
        path_value = artifact.get("path") if isinstance(artifact, dict) else None
        expected_hash = artifact.get("sha256") if isinstance(artifact, dict) else None
        if not path_value or not _is_sha256(expected_hash):
            continue
        path = Path(path_value).resolve()
        if not _is_relative_to(path, artifact_root) or not path.is_file():
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            continue
        valid[path] = (_markdown_body(raw.decode("utf-8-sig")), expected_hash)
    if not valid:
        raise ChapterContextError(
            "committed_previous_chapter_artifact_missing",
            f"no hash-verified committed artifact matched chapter {chapter_index - 1}",
        )
    if len(valid) > 1:
        raise ChapterContextError(
            "committed_previous_chapter_artifact_conflict",
            f"multiple hash-verified committed artifacts matched chapter {chapter_index - 1}",
        )
    path, (text, source_sha256) = next(iter(valid.items()))
    return _previous_context_from_text(
        text=text,
        chapter_index=chapter_index - 1,
        source_kind="committed_artifact",
        path_ref=path_ref_for(path, root_id="chapter_artifacts", root=artifact_root),
        generation_max_chars=generation_max_chars,
        review_tail_chars=review_tail_chars,
        source_sha256=source_sha256,
    )


def build_attempt_context(
    *,
    chapter_index: int,
    run_id: str,
    status: str,
    draft_text: str,
    max_chars: int = DEFAULT_GENERATION_EXCERPT_CHARS,
) -> AttemptContext:
    _validate_chapter_index(chapter_index)
    if status not in {"preview", "rejected", "failed"}:
        raise ChapterContextError("attempt_context_status_invalid", "attempt status must be preview, rejected, or failed")
    excerpt = _head_tail_excerpt(draft_text, max_chars=max_chars, policy="attempt_head_tail")
    return AttemptContext(
        chapter_index=chapter_index,
        run_id=str(run_id),
        status=status,
        sha256=_text_sha256(draft_text),
        original_chars=len(draft_text),
        excerpt=excerpt,
    )


def build_recovery_context(
    *,
    chapter_index: int,
    source_run_id: str,
    source_status: str,
    draft_text: str,
    artifact_path: str | Path | None = None,
    artifact_root: str | Path | None = None,
    expected_artifact_sha256: str | None = None,
    max_chars: int = DEFAULT_GENERATION_EXCERPT_CHARS,
) -> RecoveryContext:
    _validate_chapter_index(chapter_index)
    if source_status not in {"rejected", "failed"}:
        raise ChapterContextError("recovery_context_status_invalid", "recovery source must be rejected or failed")
    artifact_ref = None
    verified = False
    if artifact_path is not None:
        if artifact_root is None or not _is_sha256(expected_artifact_sha256):
            raise ChapterContextError("recovery_artifact_unverified", "artifact root and SHA-256 are required")
        path = Path(artifact_path).resolve()
        root = Path(artifact_root).resolve()
        if not _is_relative_to(path, root) or not path.is_file():
            raise ChapterContextError("recovery_artifact_unverified", "artifact path is missing or outside its root")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_artifact_sha256:
            raise ChapterContextError("recovery_artifact_hash_mismatch", "recovery artifact hash does not match")
        artifact_ref = path_ref_for(path, root_id="chapter_artifacts", root=root)
        verified = True
    return RecoveryContext(
        chapter_index=chapter_index,
        source_run_id=str(source_run_id),
        source_status=source_status,
        sha256=_text_sha256(draft_text),
        original_chars=len(draft_text),
        excerpt=_head_tail_excerpt(draft_text, max_chars=max_chars, policy="recovery_head_tail"),
        artifact_path_ref=artifact_ref,
        artifact_hash_verified=verified,
    )


def _previous_context_from_text(
    *,
    text: str,
    chapter_index: int,
    source_kind: str,
    path_ref: PathRef,
    generation_max_chars: int,
    review_tail_chars: int,
    source_sha256: str | None = None,
) -> PreviousChapterContext:
    return PreviousChapterContext(
        chapter_index=chapter_index,
        source_kind=source_kind,
        path_ref=path_ref,
        sha256=source_sha256 or _text_sha256(text),
        original_chars=len(text),
        generation_excerpt=_head_tail_excerpt(text, max_chars=generation_max_chars, policy="previous_chapter_10_90"),
        review_tail=_tail_excerpt(text, max_chars=review_tail_chars, policy="review_tail"),
    )


def _head_tail_excerpt(text: str, *, max_chars: int, policy: str) -> dict[str, Any]:
    _validate_max_chars(max_chars)
    if len(text) <= max_chars:
        return _excerpt(text, [(0, len(text))], policy=policy, original_chars=len(text))
    head_chars = max(1, int(max_chars * 0.1))
    tail_chars = max_chars - head_chars
    ranges = [(0, head_chars), (len(text) - tail_chars, len(text))]
    excerpt_text = text[:head_chars] + "\n\n[…中段已省略…]\n\n" + text[-tail_chars:]
    return _excerpt(excerpt_text, ranges, policy=policy, original_chars=len(text))


def _tail_excerpt(text: str, *, max_chars: int, policy: str) -> dict[str, Any]:
    _validate_max_chars(max_chars)
    start = max(0, len(text) - max_chars)
    return _excerpt(text[start:], [(start, len(text))], policy=policy, original_chars=len(text))


def _excerpt(text: str, ranges: list[tuple[int, int]], *, policy: str, original_chars: int) -> dict[str, Any]:
    return {
        "text": text,
        "policy": policy,
        "ranges": [{"start_char": start, "end_char": end} for start, end in ranges],
        "estimated_tokens": math.ceil(len(text) / 4),
        "truncated": sum(end - start for start, end in ranges) < original_chars,
    }


def _markdown_body(text: str) -> str:
    marker = "\n---\n\n"
    return text.split(marker, 1)[1].strip() if marker in text else text.strip()


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_chapter_index(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ChapterContextError("chapter_index_invalid", "chapter_index must be a positive integer")


def _validate_max_chars(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ChapterContextError("context_excerpt_limit_invalid", "excerpt limit must be a positive integer")


__all__ = [
    "AttemptContext",
    "CHAPTER_CONTEXT_SCHEMA_VERSION",
    "ChapterContextError",
    "PreviousChapterContext",
    "RecoveryContext",
    "build_attempt_context",
    "build_recovery_context",
    "resolve_committed_previous_chapter_artifact",
    "resolve_story_project_previous_chapter",
]
