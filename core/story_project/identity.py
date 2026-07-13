from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
import json
import uuid

from core.engine.persistence import atomic_create_json
from core.schema import SchemaValidationError, validate_schema


PROJECT_IDENTITY_SCHEMA_VERSION = "1.0"
PROJECT_IDENTITY_RELATIVE_PATH = Path(".novelagent/project.json")


class ProjectIdentityError(ValueError):
    code = "story_project_identity_invalid"


class ProjectIdentityMismatchError(ProjectIdentityError):
    code = "story_project_state_identity_mismatch"

    def __init__(self, *, expected_book_id: str, actual_book_id: str, source: str) -> None:
        self.expected_book_id = expected_book_id
        self.actual_book_id = actual_book_id
        self.source = source
        super().__init__(
            f"{self.code}: {source} belongs to book {actual_book_id!r}; "
            f"expected {expected_book_id!r}"
        )


@dataclass(frozen=True)
class ProjectIdentity:
    schema_version: str
    book_id: str
    created_at: str
    root_hint: str
    story_state_mode: str = "shadow"
    activation: dict[str, Any] | None = None
    ephemeral: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "book_id": self.book_id,
            "created_at": self.created_at,
            "root_hint": self.root_hint,
            "story_state_mode": self.story_state_mode,
            "activation": dict(self.activation) if self.activation is not None else None,
            "ephemeral": self.ephemeral,
        }


def project_identity_path(story_project_root: str | Path) -> Path:
    return Path(story_project_root) / PROJECT_IDENTITY_RELATIVE_PATH


def validate_project_identity(value: Any) -> ProjectIdentity:
    if not isinstance(value, dict):
        raise ProjectIdentityError("Project identity must be a JSON object")
    try:
        validated = validate_schema(value, "project_identity.schema.json")
    except SchemaValidationError as exc:
        raise ProjectIdentityError(str(exc)) from exc
    identity = ProjectIdentity(
        schema_version=str(validated["schema_version"]),
        book_id=str(validated["book_id"]),
        created_at=str(validated["created_at"]),
        root_hint=str(validated["root_hint"]),
        story_state_mode=str(validated["story_state_mode"]),
        activation=dict(validated["activation"]) if validated["activation"] is not None else None,
        ephemeral=bool(validated["ephemeral"]),
    )
    if identity.story_state_mode == "strict" and identity.activation is None:
        raise ProjectIdentityError("strict StoryProject identity requires activation metadata")
    if identity.activation is not None:
        report_hash = identity.activation.get("calibration_report_sha256")
        if not isinstance(report_hash, str) or len(report_hash) != 64 or any(
            character not in "0123456789abcdef" for character in report_hash
        ):
            raise ProjectIdentityError("activation calibration_report_sha256 must be lowercase SHA-256")
    return identity


def load_project_identity(story_project_root: str | Path) -> ProjectIdentity | None:
    path = project_identity_path(story_project_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ProjectIdentityError(f"Could not read project identity: {path}: {exc}") from exc
    return validate_project_identity(payload)


def create_ephemeral_project_identity(
    story_project_root: str | Path,
    *,
    now: Callable[[], datetime] | None = None,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
) -> ProjectIdentity:
    root = _validated_story_project_root(story_project_root)
    return ProjectIdentity(
        schema_version=PROJECT_IDENTITY_SCHEMA_VERSION,
        book_id=f"ephemeral:{uuid_factory()}",
        created_at=_utc_timestamp(now),
        root_hint=str(root),
        story_state_mode="shadow",
        activation=None,
        ephemeral=True,
    )


def ensure_project_identity(
    story_project_root: str | Path,
    *,
    now: Callable[[], datetime] | None = None,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    book_id: str | None = None,
) -> ProjectIdentity:
    root = _validated_story_project_root(story_project_root)
    existing = load_project_identity(root)
    if existing is not None:
        if existing.ephemeral:
            raise ProjectIdentityError("Persisted project identity cannot be ephemeral")
        if book_id is not None and existing.book_id != book_id:
            raise ProjectIdentityMismatchError(
                expected_book_id=existing.book_id,
                actual_book_id=book_id,
                source=str(project_identity_path(root)),
            )
        return existing

    identity = ProjectIdentity(
        schema_version=PROJECT_IDENTITY_SCHEMA_VERSION,
        book_id=book_id or str(uuid_factory()),
        created_at=_utc_timestamp(now),
        root_hint=str(root),
        story_state_mode="shadow",
        activation=None,
        ephemeral=False,
    )
    validate_project_identity(identity.to_dict())
    try:
        atomic_create_json(project_identity_path(root), identity.to_dict())
    except FileExistsError:
        winner = load_project_identity(root)
        if winner is None:
            raise ProjectIdentityError("Project identity creation raced but no identity can be loaded")
        return winner
    return identity


def project_identity_for_operation(
    story_project_root: str | Path,
    *,
    persist: bool,
    persistence_dir: str | Path | None = None,
) -> ProjectIdentity:
    if persist:
        return ensure_project_identity_for_runtime(
            story_project_root,
            persistence_dir=persistence_dir,
        )
    existing = load_project_identity(story_project_root)
    return existing if existing is not None else create_ephemeral_project_identity(story_project_root)


def ensure_project_identity_for_runtime(
    story_project_root: str | Path,
    *,
    persistence_dir: str | Path | None,
) -> ProjectIdentity:
    existing = load_project_identity(story_project_root)
    if existing is not None:
        return existing
    if persistence_dir is not None:
        journal_root = Path(persistence_dir)
        if journal_root.is_dir() and any(path.is_dir() for path in journal_root.iterdir()):
            raise ProjectIdentityError(
                "story_project_identity_missing_for_existing_journal: "
                "run explicit StoryProject runtime migration before assigning a new book_id"
            )
    return ensure_project_identity(story_project_root)


def assert_project_identity(
    expected: ProjectIdentity,
    actual_book_id: str | None,
    *,
    source: str,
    allow_missing_legacy: bool = False,
) -> None:
    if actual_book_id is None and allow_missing_legacy:
        return
    if actual_book_id != expected.book_id:
        raise ProjectIdentityMismatchError(
            expected_book_id=expected.book_id,
            actual_book_id=str(actual_book_id or "<missing>"),
            source=source,
        )


def _validated_story_project_root(story_project_root: str | Path) -> Path:
    root = Path(story_project_root).resolve()
    if not root.is_dir():
        raise ProjectIdentityError(f"StoryProject root is not a directory: {root}")
    return root


def _utc_timestamp(now: Callable[[], datetime] | None) -> str:
    value = now() if now is not None else datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


__all__ = [
    "PROJECT_IDENTITY_RELATIVE_PATH",
    "PROJECT_IDENTITY_SCHEMA_VERSION",
    "ProjectIdentity",
    "ProjectIdentityError",
    "ProjectIdentityMismatchError",
    "assert_project_identity",
    "create_ephemeral_project_identity",
    "ensure_project_identity",
    "ensure_project_identity_for_runtime",
    "load_project_identity",
    "project_identity_path",
    "project_identity_for_operation",
    "validate_project_identity",
]
