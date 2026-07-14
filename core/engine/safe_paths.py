from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.path_refs import PathRef, PathRefError, validate_path_ref


FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


class SafePathError(RuntimeError):
    """Raised when a persistence path cannot be proven to stay on a safe root."""


class UnsafePathComponentError(SafePathError):
    pass


class PathGuardMismatchError(SafePathError):
    pass


@dataclass(frozen=True)
class RootBinding:
    root_id: str
    root_uuid: str
    path: Path


@dataclass(frozen=True)
class SafeResolvedPath:
    path_ref: PathRef
    path: Path
    guard: dict[str, Any]


class SafePathResolver:
    """Resolve PathRefs without following links and retain a TOCTOU guard.

    The guard intentionally binds directory identities rather than only their
    names. Revalidation immediately before a replace catches a parent swapped
    after prepare, including a swap to another otherwise ordinary directory.
    """

    def __init__(self, bindings: Mapping[str, RootBinding | Mapping[str, Any]]) -> None:
        normalized: dict[str, RootBinding] = {}
        for root_id, raw in bindings.items():
            if isinstance(raw, RootBinding):
                binding = raw
            elif isinstance(raw, Mapping):
                binding = RootBinding(
                    root_id=str(root_id),
                    root_uuid=str(raw["root_uuid"]),
                    path=Path(str(raw["path"])).absolute(),
                )
            else:
                raise SafePathError(f"invalid root binding: {root_id}")
            if binding.root_id != str(root_id):
                raise SafePathError(f"root binding id mismatch: {root_id}")
            path = _absolute_local_path(binding.path)
            _assert_directory_lineage(path, label=f"root {root_id}")
            normalized[str(root_id)] = RootBinding(
                root_id=str(root_id), root_uuid=binding.root_uuid, path=path
            )
        if not normalized:
            raise SafePathError("at least one safe root binding is required")
        self._bindings = normalized

    @property
    def bindings(self) -> dict[str, RootBinding]:
        return dict(self._bindings)

    def bind(self, value: PathRef | Mapping[str, Any]) -> PathRef:
        ref = validate_path_ref(value)
        binding = self._binding(ref)
        if ref.root_uuid is not None and ref.root_uuid != binding.root_uuid:
            raise PathGuardMismatchError(
                f"PathRef root UUID mismatch for {ref.root_id}: "
                f"expected={binding.root_uuid} actual={ref.root_uuid}"
            )
        return validate_path_ref(
            PathRef(
                root_id=ref.root_id,
                relative_path=ref.relative_path,
                original_path_hint=ref.original_path_hint,
                root_uuid=binding.root_uuid,
            )
        )

    def resolve(
        self,
        value: PathRef | Mapping[str, Any],
        *,
        expected_guard: Mapping[str, Any] | None = None,
        allow_guard_extension: bool = False,
    ) -> SafeResolvedPath:
        ref = self.bind(value)
        binding = self._binding(ref)
        parts = tuple(ref.relative_path.replace("\\", "/").split("/"))
        candidate = binding.path.joinpath(*parts)
        _assert_lexically_within(candidate, binding.path)
        lineage = _directory_lineage(binding.path, parts[:-1])
        if candidate.exists() or os.path.lexists(candidate):
            _assert_not_link_or_reparse(candidate)
        guard = {
            "root_id": ref.root_id,
            "root_uuid": binding.root_uuid,
            "relative_path": ref.relative_path,
            "directories": lineage,
        }
        if expected_guard is not None:
            _compare_guard(
                expected_guard,
                guard,
                allow_extension=allow_guard_extension,
            )
        return SafeResolvedPath(path_ref=ref, path=candidate, guard=guard)

    def ensure_parent(
        self,
        value: PathRef | Mapping[str, Any],
        *,
        expected_guard: Mapping[str, Any] | None = None,
    ) -> SafeResolvedPath:
        resolved = self.resolve(value, expected_guard=expected_guard)
        binding = self._binding(resolved.path_ref)
        current = binding.path
        parts = tuple(resolved.path_ref.relative_path.replace("\\", "/").split("/"))
        for part in parts[:-1]:
            current = current / part
            if os.path.lexists(current):
                _assert_existing_directory(current, label="PathRef parent")
            else:
                current.mkdir()
                _fsync_directory(current.parent)
                _assert_existing_directory(current, label="created PathRef parent")
        # Preserve the original guarded prefix while allowing directories that
        # this method just created to extend it.
        return self.resolve(
            resolved.path_ref,
            expected_guard=expected_guard,
            allow_guard_extension=True,
        )

    def _binding(self, ref: PathRef) -> RootBinding:
        try:
            return self._bindings[ref.root_id]
        except KeyError as exc:
            raise PathRefError(f"PathRef root is not registered: {ref.root_id}") from exc


def assert_safe_local_tree(root: str | Path) -> Path:
    """Validate an internal transaction directory without following links."""

    path = _absolute_local_path(root)
    _assert_directory_lineage(
        path,
        label="transaction root",
        allow_missing_leaf=True,
    )
    return path


def _absolute_local_path(path: str | Path) -> Path:
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        raise SafePathError(f"UNC/network paths are not supported: {text}")
    return Path(path).absolute()


def _assert_lexically_within(path: Path, root: Path) -> None:
    try:
        common = os.path.commonpath((os.path.normcase(str(path)), os.path.normcase(str(root))))
    except ValueError as exc:
        raise SafePathError(f"path/root drive mismatch: {path}") from exc
    if common != os.path.normcase(str(root)):
        raise SafePathError(f"PathRef escapes root: {path}")


def _directory_lineage(root: Path, parent_parts: tuple[str, ...]) -> list[dict[str, Any]]:
    result = [_identity(root, relative_path=".")]
    current = root
    relative: list[str] = []
    for part in parent_parts:
        current = current / part
        relative.append(part)
        if not os.path.lexists(current):
            break
        _assert_existing_directory(current, label="PathRef parent")
        result.append(_identity(current, relative_path="/".join(relative)))
    return result


def _identity(path: Path, *, relative_path: str) -> dict[str, Any]:
    info = os.lstat(path)
    return {
        "relative_path": relative_path,
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "mode": int(stat.S_IFMT(info.st_mode)),
        "reparse": bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT),
    }


def _compare_guard(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    allow_extension: bool,
) -> None:
    for field in ("root_id", "root_uuid", "relative_path"):
        if expected.get(field) != actual.get(field):
            raise PathGuardMismatchError(f"safe path guard {field} changed")
    expected_dirs = expected.get("directories")
    actual_dirs = actual.get("directories")
    if not isinstance(expected_dirs, list) or not isinstance(actual_dirs, list):
        raise PathGuardMismatchError("safe path guard directory lineage is invalid")
    lengths_valid = (
        len(actual_dirs) >= len(expected_dirs)
        if allow_extension
        else len(actual_dirs) == len(expected_dirs)
    )
    if not lengths_valid or actual_dirs[: len(expected_dirs)] != expected_dirs:
        raise PathGuardMismatchError("safe path parent identity changed after prepare")


def _assert_existing_directory(path: Path, *, label: str) -> None:
    _assert_not_link_or_reparse(path)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafePathComponentError(f"{label} is not a directory: {path}")


def _assert_directory_lineage(
    path: Path,
    *,
    label: str,
    allow_missing_leaf: bool = False,
) -> None:
    anchor = Path(path.anchor)
    current = anchor
    if os.path.lexists(current):
        _assert_existing_directory(current, label=f"{label} anchor")
    relative_parts = path.parts[1:] if path.anchor else path.parts
    missing = False
    for part in relative_parts:
        current = current / part
        if not os.path.lexists(current):
            missing = True
            break
        _assert_existing_directory(current, label=label)
    if missing and not allow_missing_leaf:
        raise UnsafePathComponentError(f"{label} does not exist: {path}")


def _assert_not_link_or_reparse(path: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise UnsafePathComponentError(f"cannot inspect path component: {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise UnsafePathComponentError(f"symbolic links are forbidden in persistence paths: {path}")
    if getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        raise UnsafePathComponentError(f"Windows reparse points/junctions are forbidden: {path}")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "FILE_ATTRIBUTE_REPARSE_POINT",
    "PathGuardMismatchError",
    "RootBinding",
    "SafePathError",
    "SafePathResolver",
    "SafeResolvedPath",
    "UnsafePathComponentError",
    "assert_safe_local_tree",
]
