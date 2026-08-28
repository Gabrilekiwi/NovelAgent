from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
import json
import os
import shutil
import sqlite3
import stat
import uuid

from core.engine.persistence import atomic_create_json
from core.engine.safe_paths import FILE_ATTRIBUTE_REPARSE_POINT
from core.story_project.identity import (
    PROJECT_IDENTITY_SCHEMA_VERSION,
    ProjectIdentity,
    ensure_project_identity,
    load_project_identity,
    validate_project_identity,
)
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.paths import infer_next_chapter
from workbench.database import WorkbenchDatabase


_SENSITIVE_METADATA_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "credential",
        "credentials",
        "access_key",
        "access_token",
        "refresh_token",
        "password",
        "passwd",
        "private_key",
        "secret",
        "database_url",
        "connection_string",
        "dsn",
    }
)
_SENSITIVE_METADATA_SUFFIXES = (
    "_api_key",
    "_access_key",
    "_access_token",
    "_refresh_token",
    "_password",
    "_passwd",
    "_private_key",
    "_secret",
    "_credential",
    "_credentials",
)


class ProjectRegistryError(ValueError):
    """Raised for an unsafe or inconsistent workbench project operation."""


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    book_id: str
    root: Path
    name: str
    metadata: dict[str, Any]
    next_chapter: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "book_id": self.book_id,
            "root": str(self.root),
            "name": self.name,
            "metadata": json.loads(json.dumps(self.metadata, ensure_ascii=False)),
            "next_chapter": self.next_chapter,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ProjectRegistry:
    def __init__(
        self,
        database: WorkbenchDatabase,
        *,
        allowed_workspaces: Sequence[str | Path],
    ) -> None:
        self.database = database
        self.allowed_workspaces = tuple(
            _canonical_existing_directory(path, label="allowed workspace")
            for path in allowed_workspaces
        )
        if not self.allowed_workspaces:
            raise ProjectRegistryError("at least one allowed workspace is required")
        self.database.initialize()

    def import_project(
        self,
        root: str | Path,
        *,
        name: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProjectRecord:
        canonical_root = self.validate_project_root(root)
        _require_story_project_directories(canonical_root)
        _require_safe_identity_location(canonical_root)
        identity = load_project_identity(canonical_root)
        if identity is None:
            identity = ensure_project_identity(canonical_root)
        if identity.ephemeral:
            raise ProjectRegistryError("an ephemeral ProjectIdentity cannot be imported")
        project_name = _required_text("name", name if name is not None else canonical_root.name)
        safe_metadata = validate_public_metadata(metadata or {})
        return self._register(
            book_id=identity.book_id,
            root=canonical_root,
            name=project_name,
            metadata=safe_metadata,
        )

    def create_project(
        self,
        target: str | Path,
        *,
        name: str,
        genre: str = "",
        synopsis: str = "",
        language: str = "zh-CN",
        metadata: Mapping[str, Any] | None = None,
    ) -> ProjectRecord:
        project_name = _required_text("name", name)
        target_path = Path(target).expanduser()
        if not target_path.is_absolute():
            raise ProjectRegistryError("new project target must be an absolute path")
        canonical_target = target_path.resolve(strict=False)
        self._assert_allowed(canonical_target)
        parent = canonical_target.parent.resolve(strict=True)
        self._assert_allowed(parent)
        if not parent.is_dir():
            raise ProjectRegistryError(f"new project parent is not a directory: {parent}")
        if canonical_target == parent:
            raise ProjectRegistryError("new project target cannot be its parent directory")

        target_was_empty = False
        if canonical_target.exists():
            if not canonical_target.is_dir():
                raise ProjectRegistryError(f"new project target is not a directory: {canonical_target}")
            if any(canonical_target.iterdir()):
                raise ProjectRegistryError(f"new project target is not empty: {canonical_target}")
            target_was_empty = True

        combined_metadata = dict(metadata or {})
        combined_metadata.update(
            {
                "genre": _optional_text("genre", genre),
                "synopsis": _optional_text("synopsis", synopsis),
                "language": _required_text("language", language),
            }
        )
        safe_metadata = validate_public_metadata(combined_metadata)
        book_id = str(uuid.uuid4())
        now = _utc_now()
        identity = ProjectIdentity(
            schema_version=PROJECT_IDENTITY_SCHEMA_VERSION,
            book_id=book_id,
            created_at=now,
            root_hint=str(canonical_target),
            story_state_mode="shadow",
            activation=None,
            ephemeral=False,
        )
        validate_project_identity(identity.to_dict())

        staging = parent / f".novelagent-create-{uuid.uuid4().hex}"
        removed_empty_target = False
        published = False
        try:
            staging.mkdir(exist_ok=False)
            for directory in CORE_DIRECTORY_NAMES:
                (staging / directory).mkdir()
            (staging / ".novelagent").mkdir()
            atomic_create_json(staging / ".novelagent" / "project.json", identity.to_dict())

            if target_was_empty:
                canonical_target.rmdir()
                removed_empty_target = True
            os.replace(staging, canonical_target)
            published = True
            return self._register(
                book_id=book_id,
                root=canonical_target,
                name=project_name,
                metadata=safe_metadata,
                created_at=now,
            )
        except Exception:
            if published:
                shutil.rmtree(canonical_target, ignore_errors=True)
            elif staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if removed_empty_target and not canonical_target.exists():
                canonical_target.mkdir()
            raise

    def get(self, project_id: str) -> ProjectRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (str(project_id),)
            ).fetchone()
        if row is None:
            raise ProjectRegistryError(f"unknown workbench project: {project_id}")
        return self._record_from_row(row)

    def list_projects(self) -> tuple[ProjectRecord, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC, id"
            ).fetchall()
        return tuple(self._record_from_row(row) for row in rows)

    def validate_project_root(self, root: str | Path) -> Path:
        canonical = _canonical_existing_directory(root, label="StoryProject root")
        self._assert_allowed(canonical)
        return canonical

    def _assert_allowed(self, candidate: Path) -> None:
        if not any(_is_relative_to(candidate, workspace) for workspace in self.allowed_workspaces):
            raise ProjectRegistryError(
                f"path is outside the configured workspaces: {candidate}"
            )

    def _register(
        self,
        *,
        book_id: str,
        root: Path,
        name: str,
        metadata: Mapping[str, Any],
        created_at: str | None = None,
    ) -> ProjectRecord:
        root = root.resolve(strict=True)
        self._assert_allowed(root)
        safe_metadata = validate_public_metadata(metadata)
        metadata_json = _canonical_json(safe_metadata)
        timestamp = created_at or _utc_now()
        project_id = f"project_{uuid.uuid4().hex}"
        try:
            with self.database.transaction() as connection:
                existing = connection.execute(
                    "SELECT * FROM projects WHERE root = ? OR book_id = ?",
                    (str(root), book_id),
                ).fetchall()
                if existing:
                    if len(existing) != 1:
                        raise ProjectRegistryError(
                            "project root and ProjectIdentity resolve to different registry entries"
                        )
                    row = existing[0]
                    if row["root"] != str(root) or row["book_id"] != book_id:
                        raise ProjectRegistryError(
                            "project root or book_id is already bound to another project"
                        )
                    connection.execute(
                        "UPDATE projects SET name = ?, metadata_json = ?, updated_at = ? WHERE id = ?",
                        (name, metadata_json, timestamp, row["id"]),
                    )
                    row = connection.execute(
                        "SELECT * FROM projects WHERE id = ?", (row["id"],)
                    ).fetchone()
                else:
                    connection.execute(
                        """
                        INSERT INTO projects(
                            id, book_id, root, name, metadata_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            project_id,
                            book_id,
                            str(root),
                            name,
                            metadata_json,
                            timestamp,
                            timestamp,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM projects WHERE id = ?", (project_id,)
                    ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ProjectRegistryError(f"project registry conflict: {exc}") from exc
        if row is None:
            raise ProjectRegistryError("project registration did not produce a record")
        return self._record_from_row(row)

    def _record_from_row(self, row: sqlite3.Row) -> ProjectRecord:
        root = Path(str(row["root"])).resolve(strict=True)
        self._assert_allowed(root)
        _require_story_project_directories(root)
        _require_safe_identity_location(root)
        identity = load_project_identity(root)
        if identity is None or identity.book_id != row["book_id"]:
            raise ProjectRegistryError(
                f"registered project identity drifted for {row['id']}"
            )
        metadata = json.loads(str(row["metadata_json"]))
        safe_metadata = validate_public_metadata(metadata)
        return ProjectRecord(
            id=str(row["id"]),
            book_id=str(row["book_id"]),
            root=root,
            name=str(row["name"]),
            metadata=safe_metadata,
            next_chapter=infer_next_chapter(root),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


def validate_public_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProjectRegistryError("project metadata must be an object")

    def visit(item: Any, path: str) -> Any:
        if item is None or isinstance(item, (str, int, bool)):
            return item
        if isinstance(item, float):
            if not (float("-inf") < item < float("inf")):
                raise ProjectRegistryError(f"project metadata contains a non-finite number at {path}")
            return item
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for raw_key, nested in item.items():
                if not isinstance(raw_key, str) or not raw_key.strip():
                    raise ProjectRegistryError(f"project metadata key is invalid at {path}")
                key = raw_key.strip()
                normalized = key.casefold().replace("-", "_").replace(" ", "_")
                if normalized in _SENSITIVE_METADATA_KEYS or normalized.endswith(
                    _SENSITIVE_METADATA_SUFFIXES
                ):
                    raise ProjectRegistryError(
                        f"project metadata must not contain credentials or secrets: {path}.{key}"
                    )
                result[key] = visit(nested, f"{path}.{key}")
            return result
        if isinstance(item, (list, tuple)):
            return [visit(nested, f"{path}[{index}]") for index, nested in enumerate(item)]
        raise ProjectRegistryError(
            f"project metadata is not JSON-compatible at {path}: {type(item).__name__}"
        )

    result = visit(value, "metadata")
    encoded = _canonical_json(result)
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise ProjectRegistryError("project metadata exceeds 64 KiB")
    return result


def _canonical_existing_directory(path: str | Path, *, label: str) -> Path:
    try:
        canonical = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ProjectRegistryError(f"{label} does not exist: {path}") from exc
    try:
        info = canonical.stat()
    except OSError as exc:
        raise ProjectRegistryError(f"cannot inspect {label}: {canonical}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ProjectRegistryError(f"{label} is not a directory: {canonical}")
    return canonical


def _require_story_project_directories(root: Path) -> None:
    missing: list[str] = []
    unsafe: list[str] = []
    for name in CORE_DIRECTORY_NAMES:
        path = root / name
        try:
            info = path.lstat()
        except OSError:
            missing.append(name)
            continue
        if not stat.S_ISDIR(info.st_mode):
            missing.append(name)
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
        ):
            unsafe.append(name)
    if missing:
        raise ProjectRegistryError(
            "StoryProject is missing required directories: " + ", ".join(missing)
        )
    if unsafe:
        raise ProjectRegistryError(
            "StoryProject core directories must not be links or reparse points: "
            + ", ".join(unsafe)
        )


def _require_safe_identity_location(root: Path) -> None:
    metadata_directory = root / ".novelagent"
    if metadata_directory.is_symlink():
        raise ProjectRegistryError(
            "StoryProject .novelagent directory must not be a link or reparse point"
        )
    if metadata_directory.exists():
        resolved_directory = metadata_directory.resolve(strict=True)
        if not _is_relative_to(resolved_directory, root):
            raise ProjectRegistryError("StoryProject .novelagent directory escapes its root")
        info = metadata_directory.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ProjectRegistryError(
                "StoryProject .novelagent directory must not be a link or reparse point"
            )
    identity_path = metadata_directory / "project.json"
    if identity_path.is_symlink():
        raise ProjectRegistryError("StoryProject ProjectIdentity must be a regular local file")
    if not identity_path.exists():
        return
    resolved_identity = identity_path.resolve(strict=True)
    if not _is_relative_to(resolved_identity, root):
        raise ProjectRegistryError("StoryProject ProjectIdentity escapes its root")
    info = identity_path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ProjectRegistryError("StoryProject ProjectIdentity must be a regular local file")


def _required_text(field: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectRegistryError(f"{field} must be non-empty text")
    return value.strip()


def _optional_text(field: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ProjectRegistryError(f"{field} must be text")
    return value.strip()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ProjectRegistryError(f"value is not canonical JSON: {exc}") from exc


def _is_relative_to(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ProjectRecord",
    "ProjectRegistry",
    "ProjectRegistryError",
    "validate_public_metadata",
]
