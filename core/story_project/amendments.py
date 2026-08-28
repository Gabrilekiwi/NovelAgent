from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
import hashlib
import json
import re
import stat


AMENDMENT_SCHEMA_VERSION = "1.0"
AMENDMENT_ROOT = Path(".novelagent/amendments")
_ID = re.compile(r"^am_[0-9a-f]{24,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AmendmentIntegrityError(ValueError):
    """An active amendment pointer or immutable version failed verification."""


@dataclass(frozen=True)
class ActiveAmendment:
    amendment_id: str
    chapter_index: int
    path: Path
    relative_path: str
    sha256: str
    base_sha256: str
    activated_at: str


def amendment_chapter_root(story_project_root: str | Path, chapter_index: int) -> Path:
    _validate_chapter(chapter_index)
    return Path(story_project_root).resolve() / AMENDMENT_ROOT / f"chapter_{chapter_index:04d}"


def active_amendment_pointer_path(story_project_root: str | Path, chapter_index: int) -> Path:
    return amendment_chapter_root(story_project_root, chapter_index) / "current.json"


def resolve_active_amendment(
    story_project_root: str | Path,
    chapter_index: int,
) -> ActiveAmendment | None:
    root = Path(story_project_root).resolve()
    pointer_path = active_amendment_pointer_path(root, chapter_index)
    if not pointer_path.exists():
        return None
    pointer_path = _safe_regular_file(root, pointer_path, "active amendment pointer")
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AmendmentIntegrityError(f"active amendment pointer is unreadable: {pointer_path}") from exc
    if not isinstance(pointer, dict) or pointer.get("schema_version") != AMENDMENT_SCHEMA_VERSION:
        raise AmendmentIntegrityError("active amendment pointer schema is invalid")
    amendment_id = pointer.get("amendment_id")
    content_sha = pointer.get("content_sha256")
    base_sha = pointer.get("base_sha256")
    formal_prose_sha = pointer.get("formal_prose_sha256")
    relative = pointer.get("content_path")
    activated_at = pointer.get("activated_at")
    if not isinstance(amendment_id, str) or _ID.fullmatch(amendment_id) is None:
        raise AmendmentIntegrityError("active amendment id is invalid")
    if not isinstance(content_sha, str) or _SHA256.fullmatch(content_sha) is None:
        raise AmendmentIntegrityError("active amendment content hash is invalid")
    if not isinstance(base_sha, str) or _SHA256.fullmatch(base_sha) is None:
        raise AmendmentIntegrityError("active amendment base hash is invalid")
    if not isinstance(formal_prose_sha, str) or _SHA256.fullmatch(formal_prose_sha) is None:
        raise AmendmentIntegrityError("active amendment formal prose hash is invalid")
    if not isinstance(relative, str) or not isinstance(activated_at, str) or not activated_at:
        raise AmendmentIntegrityError("active amendment pointer fields are incomplete")
    pure = PurePosixPath(relative)
    expected = PurePosixPath(
        AMENDMENT_ROOT.as_posix(),
        f"chapter_{chapter_index:04d}",
        "versions",
        f"{amendment_id}.md",
    )
    if pure != expected:
        raise AmendmentIntegrityError("active amendment content path is not canonical")
    path = _safe_regular_file(
        root, root / Path(*pure.parts), "active amendment content"
    )
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != content_sha:
        raise AmendmentIntegrityError("active amendment content hash mismatch")
    manifest_path = _safe_regular_file(
        root, path.with_suffix(".json"), "active amendment manifest"
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AmendmentIntegrityError("active amendment manifest is unreadable") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != AMENDMENT_SCHEMA_VERSION
        or manifest.get("id") != amendment_id
        or manifest.get("chapter_index") != chapter_index
        or manifest.get("content_sha256") != content_sha
        or manifest.get("base_sha256") != base_sha
        or manifest.get("formal_prose_sha256") != formal_prose_sha
        or manifest.get("semantic_equivalent") is not True
    ):
        raise AmendmentIntegrityError("active amendment manifest linkage is invalid")
    from core.story_project.paths import resolve_prose

    formal = resolve_prose(root, chapter_index)
    if formal.conflict or formal.path is None:
        raise AmendmentIntegrityError("active amendment formal prose is missing or ambiguous")
    formal_path = _safe_regular_file(root, formal.path, "formal prose")
    if hashlib.sha256(formal_path.read_bytes()).hexdigest() != formal_prose_sha:
        raise AmendmentIntegrityError("formal prose changed after amendment activation")
    return ActiveAmendment(
        amendment_id=amendment_id,
        chapter_index=chapter_index,
        path=path,
        relative_path=pure.as_posix(),
        sha256=content_sha,
        base_sha256=base_sha,
        activated_at=activated_at,
    )


def read_current_chapter_text(
    story_project_root: str | Path,
    chapter_index: int,
) -> tuple[str, Path, str, str]:
    """Return current reading text, path, hash, and source kind.

    The formal prose remains immutable. A hash-verified active amendment is an
    overlay used by reading/export/generation context only.
    """

    root = Path(story_project_root).resolve(strict=True)
    active = resolve_active_amendment(root, chapter_index)
    if active is not None:
        raw = active.path.read_bytes()
        return raw.decode("utf-8-sig"), active.path, active.sha256, "amendment_current"
    from core.story_project.paths import resolve_prose

    resolution = resolve_prose(story_project_root, chapter_index)
    if resolution.conflict:
        raise AmendmentIntegrityError(f"multiple formal prose files matched chapter {chapter_index}")
    if resolution.path is None:
        raise AmendmentIntegrityError(f"no formal prose matched chapter {chapter_index}")
    formal_path = _safe_regular_file(root, resolution.path, "formal prose")
    raw = formal_path.read_bytes()
    return (
        raw.decode("utf-8-sig"),
        formal_path,
        hashlib.sha256(raw).hexdigest(),
        "story_project_prose",
    )


def _validate_chapter(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("chapter_index must be a positive integer")


def _safe_regular_file(root: Path, path: Path, label: str) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise AmendmentIntegrityError(f"{label} escapes or is unreadable") from exc
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    attributes = int(getattr(info, "st_file_attributes", 0))
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or bool(attributes & reparse_flag)
    ):
        raise AmendmentIntegrityError(f"{label} must be a regular non-link file")
    return resolved


__all__ = [
    "AMENDMENT_ROOT",
    "AMENDMENT_SCHEMA_VERSION",
    "ActiveAmendment",
    "AmendmentIntegrityError",
    "active_amendment_pointer_path",
    "amendment_chapter_root",
    "read_current_chapter_text",
    "resolve_active_amendment",
]
