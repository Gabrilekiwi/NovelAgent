from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from core.path_refs import path_ref_for
from core.schema import validate_schema
from core.story_project.identity import ProjectIdentity, ProjectIdentityError, load_project_identity
from core.story_project.paths import resolve_outline, resolve_prose
from core.story_project.semantic_parser import SEMANTIC_PARSER_VERSION


STORY_PROJECT_READ_SET_SCHEMA_VERSION = "1.0"
SOURCE_DIRECTORIES = ("大纲", "正文", "追踪", "设定")


class StoryProjectSourceDriftError(ValueError):
    code = "story_project_source_drift"

    def __init__(self, phase: str, differences: list[dict[str, Any]]) -> None:
        self.phase = phase
        self.differences = differences
        summary = "; ".join(
            f"{item.get('path', item.get('field'))}: {item['code']}" for item in differences[:5]
        )
        super().__init__(f"{self.code} during {phase}: {summary}")


@dataclass(frozen=True)
class DeclaredReadSetWrite:
    relative_path: str
    after_sha256: str | None
    after_size: int | None
    action: str = "replace"

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "after_sha256": self.after_sha256,
            "after_size": self.after_size,
            "action": self.action,
        }


def capture_story_project_read_set(
    story_project_root: str | Path,
    chapter_index: int,
    *,
    project_identity: ProjectIdentity,
    parser_version: str = SEMANTIC_PARSER_VERSION,
    parse_status: str = "ok",
) -> dict[str, Any]:
    root = Path(story_project_root).resolve()
    if not root.is_dir():
        raise ValueError(f"StoryProject root is not a directory: {root}")
    if isinstance(chapter_index, bool) or not isinstance(chapter_index, int) or chapter_index < 1:
        raise ValueError("chapter_index must be a positive integer")
    entries = _source_entries(root, chapter_index)
    membership = _markdown_membership(root)
    candidates = _candidate_fingerprints(root, chapter_index)
    payload = {
        "schema_version": STORY_PROJECT_READ_SET_SCHEMA_VERSION,
        "book_id": project_identity.book_id,
        "chapter_index": chapter_index,
        "root_identity": _root_identity(root),
        "identity_revision": _identity_revision(root, project_identity),
        "parser_version": parser_version,
        "parse_status": parse_status,
        "entries": entries,
        "candidate_fingerprints": candidates,
        "membership": membership,
        "membership_fingerprint": _canonical_digest(membership),
        "context_digest": "",
    }
    payload["context_digest"] = _context_digest(payload)
    return validate_schema(payload, "story_project_read_set.schema.json")


def declared_read_set_writes(
    read_set: dict[str, Any],
    targets: Iterable[tuple[str | Path, str | None, int | None]],
) -> list[dict[str, Any]]:
    root = Path(read_set["root_identity"]["resolved_path"]).resolve()
    declared: list[dict[str, Any]] = []
    for path_value, after_sha256, after_size in targets:
        path = Path(path_value).resolve()
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if not any(relative == directory or relative.startswith(directory + "/") for directory in SOURCE_DIRECTORIES):
            continue
        action = "replace" if path.exists() else "create"
        declared.append(
            DeclaredReadSetWrite(
                relative_path=relative,
                after_sha256=after_sha256,
                after_size=after_size,
                action=action,
            ).to_dict()
        )
    return sorted(declared, key=lambda item: item["relative_path"])


def verify_story_project_read_set(
    read_set: dict[str, Any],
    *,
    declared_writes: Iterable[dict[str, Any]] = (),
    phase: str,
) -> dict[str, Any]:
    validated = validate_schema(read_set, "story_project_read_set.schema.json")
    root = Path(validated["root_identity"]["resolved_path"]).resolve()
    differences: list[dict[str, Any]] = []
    declared: dict[str, dict[str, Any]] = {}
    for raw in declared_writes:
        item = dict(raw)
        relative = str(item.get("relative_path") or "").replace("\\", "/")
        if relative in declared:
            differences.append(
                {"path": relative, "code": "duplicate_declared_write"}
            )
        declared[relative] = item
    identity_write = declared.pop(".novelagent/project.json", None)
    if _context_digest(validated) != validated["context_digest"]:
        differences.append({"field": "context_digest", "code": "read_set_digest_invalid"})
    if _root_identity(root) != validated["root_identity"]:
        differences.append({"field": "root_identity", "code": "root_identity_changed"})
    try:
        identity = load_project_identity(root)
    except ProjectIdentityError:
        identity = None
        differences.append({"field": "identity_revision", "code": "project_identity_invalid"})
    if identity is None:
        differences.append({"field": "identity_revision", "code": "project_identity_missing"})
    else:
        if identity.book_id != validated["book_id"]:
            differences.append({"field": "book_id", "code": "project_identity_changed"})
        actual_identity_revision = _identity_revision(root, identity)
        if identity_write is None:
            allowed_identity_revisions = [validated["identity_revision"]]
        elif phase in {"prepare", "pre_apply"}:
            allowed_identity_revisions = [validated["identity_revision"]]
        elif phase == "during_apply":
            allowed_identity_revisions = [
                validated["identity_revision"],
                identity_write.get("after_sha256"),
            ]
        elif phase == "pre_marker":
            allowed_identity_revisions = [identity_write.get("after_sha256")]
        else:
            raise ValueError(f"unknown read-set verification phase: {phase}")
        if actual_identity_revision not in allowed_identity_revisions:
            differences.append({"field": "identity_revision", "code": "project_identity_changed"})
        if identity_write is not None:
            authority = identity.authority or {}
            expected_epoch = (
                identity_write.get("after_authority_epoch")
                if phase == "pre_marker"
                else identity_write.get("expected_authority_epoch")
            )
            expected_head = (
                identity_write.get("after_head_event_hash")
                if phase == "pre_marker"
                else identity_write.get("expected_head_event_hash")
            )
            if (
                identity_write.get("role") != "project_identity"
                or identity_write.get("book_id") != identity.book_id
                or authority.get("authority_epoch") != expected_epoch
                or authority.get("head_event_hash") != expected_head
            ):
                differences.append(
                    {"field": "identity_authority", "code": "project_identity_changed"}
                )

    before = {item["relative_path"]: item for item in validated["membership"]}
    current_items = _markdown_membership(root)
    current = {item["relative_path"]: item for item in current_items}
    all_paths = sorted(set(before) | set(current) | set(declared))
    for relative in all_paths:
        original = before.get(relative)
        actual = current.get(relative)
        write = declared.get(relative)
        if write is None:
            if actual != original:
                differences.append({"path": relative, "code": "undeclared_source_change"})
            continue
        allowed_after = _declared_after_membership(relative, write)
        if phase in {"prepare", "pre_apply"}:
            allowed = [original]
        elif phase == "during_apply":
            allowed = [original, allowed_after]
        elif phase == "pre_marker":
            allowed = [allowed_after]
        else:
            raise ValueError(f"unknown read-set verification phase: {phase}")
        if actual not in allowed:
            differences.append({"path": relative, "code": "declared_target_drift"})

    actual_candidates = _candidate_fingerprints(root, int(validated["chapter_index"]))
    if not differences and phase in {"prepare", "pre_apply"} and actual_candidates != validated["candidate_fingerprints"]:
        differences.append({"field": "candidate_fingerprints", "code": "candidate_set_changed"})
    if differences:
        raise StoryProjectSourceDriftError(phase, differences)
    return {
        "ok": True,
        "phase": phase,
        "context_digest": validated["context_digest"],
        "membership_fingerprint": _canonical_digest(current_items),
    }


def _source_entries(root: Path, chapter_index: int) -> list[dict[str, Any]]:
    paths: list[tuple[str, Path]] = []
    outline = resolve_outline(root, chapter_index)
    if outline.path is not None:
        paths.append(("outline", outline.path))
    if chapter_index > 1:
        previous = resolve_prose(root, chapter_index - 1)
        if previous.path is not None:
            paths.append(("previous_prose", previous.path))
    for directory_name in ("追踪", "设定"):
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md"), key=lambda item: item.relative_to(root).as_posix()):
            paths.append((directory_name, path))
    entries: list[dict[str, Any]] = []
    for role, path in paths:
        raw = path.read_bytes()
        entries.append(
            {
                "role": role,
                "path_ref": path_ref_for(path, root_id="story_project", root=root).to_dict(),
                "exists": True,
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return entries


def _markdown_membership(root: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for directory_name in SOURCE_DIRECTORIES:
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md"), key=lambda item: item.relative_to(root).as_posix()):
            if not path.is_file():
                continue
            path_ref_for(path, root_id="story_project", root=root)
            raw = path.read_bytes()
            items.append(
                {
                    "relative_path": path.relative_to(root).as_posix(),
                    "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
    return items


def _candidate_fingerprints(root: Path, chapter_index: int) -> dict[str, Any]:
    outline = resolve_outline(root, chapter_index)
    previous = resolve_prose(root, chapter_index - 1) if chapter_index > 1 else None
    outline_names = [path.relative_to(root).as_posix() for path in outline.candidates]
    previous_names = [path.relative_to(root).as_posix() for path in previous.candidates] if previous else []
    return {
        "outline": {"members": outline_names, "fingerprint": _canonical_digest(outline_names)},
        "previous_prose": {"members": previous_names, "fingerprint": _canonical_digest(previous_names)},
    }


def _declared_after_membership(relative: str, write: dict[str, Any]) -> dict[str, Any] | None:
    if write.get("action") == "delete":
        return None
    digest = write.get("after_sha256")
    if not isinstance(digest, str):
        return None
    return {"relative_path": relative, "size": write.get("after_size"), "sha256": digest}


def _root_identity(root: Path) -> dict[str, Any]:
    stat = root.stat()
    return {
        "root_id": "story_project",
        "resolved_path": str(root),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
    }


def _identity_revision(root: Path, identity: ProjectIdentity) -> str:
    identity_path = root / ".novelagent" / "project.json"
    if identity_path.is_file():
        return hashlib.sha256(identity_path.read_bytes()).hexdigest()
    return _canonical_digest(identity.to_dict())


def _context_digest(payload: dict[str, Any]) -> str:
    content = dict(payload)
    content.pop("context_digest", None)
    return _canonical_digest(content)


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "DeclaredReadSetWrite",
    "STORY_PROJECT_READ_SET_SCHEMA_VERSION",
    "StoryProjectSourceDriftError",
    "capture_story_project_read_set",
    "declared_read_set_writes",
    "verify_story_project_read_set",
]
