from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any, Mapping

from core.schema import SchemaValidationError, validate_schema


KNOWN_ROOT_IDS = frozenset(
    {
        "story_project",
        "runtime",
        "snapshot",
        "chapter_artifacts",
        "delivery_store",
    }
)


class PathRefError(ValueError):
    pass


@dataclass(frozen=True)
class PathRef:
    root_id: str
    relative_path: str
    original_path_hint: str | None = None
    root_uuid: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "root_id": self.root_id,
            "relative_path": self.relative_path,
        }
        if self.original_path_hint is not None:
            payload["original_path_hint"] = self.original_path_hint
        if self.root_uuid is not None:
            payload["root_uuid"] = self.root_uuid
        return validate_schema(payload, "path_ref.schema.json")


def validate_path_ref(value: Any) -> PathRef:
    if isinstance(value, PathRef):
        value = {
            "root_id": value.root_id,
            "relative_path": value.relative_path,
            **(
                {"original_path_hint": value.original_path_hint}
                if value.original_path_hint is not None
                else {}
            ),
            **({"root_uuid": value.root_uuid} if value.root_uuid is not None else {}),
        }
    if not isinstance(value, dict):
        raise PathRefError("PathRef must be a JSON object")
    try:
        payload = validate_schema(value, "path_ref.schema.json")
    except SchemaValidationError as exc:
        raise PathRefError(str(exc)) from exc
    path_ref = PathRef(
        root_id=str(payload["root_id"]),
        relative_path=str(payload["relative_path"]),
        original_path_hint=(
            str(payload["original_path_hint"])
            if payload.get("original_path_hint") is not None
            else None
        ),
        root_uuid=(str(payload["root_uuid"]) if payload.get("root_uuid") is not None else None),
    )
    _validate_relative_path(path_ref.relative_path)
    _validate_root_id(path_ref.root_id)
    if path_ref.root_uuid is not None and not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        path_ref.root_uuid,
    ):
        raise PathRefError("PathRef root_uuid must be a canonical lowercase UUID")
    return path_ref


def resolve_path_ref(path_ref: PathRef | Mapping[str, Any], root_map: Mapping[str, str | Path]) -> Path:
    ref = validate_path_ref(path_ref)
    if ref.root_id not in root_map:
        raise PathRefError(f"PathRef root is not mapped: {ref.root_id}")
    root = _local_root(root_map[ref.root_id])
    candidate = (root / Path(ref.relative_path)).resolve(strict=False)
    if not _is_relative_to(candidate, root):
        raise PathRefError(f"PathRef escapes root {ref.root_id}: {ref.relative_path}")
    return candidate


def path_ref_for(
    path: str | Path,
    *,
    root_id: str,
    root: str | Path,
    original_path_hint: str | None = None,
    root_uuid: str | None = None,
) -> PathRef:
    _validate_root_id(root_id)
    root_path = _local_root(root)
    candidate = Path(path).resolve(strict=False)
    if not _is_relative_to(candidate, root_path):
        raise PathRefError(f"Path is outside root {root_id}: {candidate}")
    relative = candidate.relative_to(root_path).as_posix()
    if not relative or relative == ".":
        raise PathRefError("PathRef must identify a child of its root")
    return validate_path_ref(
        PathRef(
            root_id=root_id,
            relative_path=relative,
            original_path_hint=original_path_hint,
            root_uuid=root_uuid,
        )
    )


def _validate_root_id(root_id: str) -> None:
    if root_id in KNOWN_ROOT_IDS:
        return
    if root_id.startswith("external:") and len(root_id) > len("external:"):
        return
    raise PathRefError(f"Unknown PathRef root id: {root_id}")


def _validate_relative_path(value: str) -> None:
    if not value or value.strip() != value:
        raise PathRefError("PathRef relative_path must be non-empty and trimmed")
    normalized = value.replace("\\", "/")
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise PathRefError("PathRef relative_path cannot contain empty, dot, or parent segments")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise PathRefError("PathRef relative_path must be relative")


def _local_root(value: str | Path) -> Path:
    text = str(value)
    if text.startswith("\\\\") or text.startswith("//"):
        raise PathRefError(f"UNC/network roots are not supported for real writeback: {text}")
    root = Path(value).resolve(strict=False)
    return root


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


__all__ = [
    "KNOWN_ROOT_IDS",
    "PathRef",
    "PathRefError",
    "path_ref_for",
    "resolve_path_ref",
    "validate_path_ref",
]
