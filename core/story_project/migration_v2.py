from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePath
import re
from typing import Any, Mapping

from core.memory_v2.canonical import canonical_json_hash
from core.schema import SchemaValidationError, validate_schema
from core.story_project.identity import load_project_identity, project_identity_path
from core.story_project.mapper import SETTING_DIR_NAME, TRACKING_DIR_NAME
from core.story_project.paths import (
    OUTLINE_DIR_NAME,
    PROSE_DIR_NAME,
    outline_chapter_index,
    prose_chapter_index,
)


MIGRATION_PLAN_SCHEMA_VERSION = "2.0"
MIGRATION_APPROVAL_SCHEMA_VERSION = "2.0"
REQUIRED_MIGRATION_DECISIONS = (
    "timeline_elapsed_minutes",
    "chapter_10_character_state",
    "open_foreshadowing",
    "inventory",
    "lexicon",
    "corruption",
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_FORBIDDEN_DECISION_KEYS = frozenset(
    {"api_key", "apikey", "authorization", "credential", "credentials", "environment", "password", "secret", "token"}
)


class MigrationV2Error(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class MigrationPlanStaleError(MigrationV2Error):
    def __init__(self, message: str) -> None:
        super().__init__("migration_plan_stale", message)


def build_migration_plan(
    story_project_root: str | Path,
    *,
    created_at: str,
) -> dict[str, Any]:
    root = _story_root(story_project_root)
    identity = load_project_identity(root)
    if identity is None or identity.ephemeral:
        raise MigrationV2Error("migration_identity_missing", "a stable ProjectIdentity is required")
    identity_path = project_identity_path(root)
    expected_identity_sha256 = _file_sha256(identity_path)
    sources = _capture_sources(root)
    if not any(item["role"] == "project_identity" for item in sources):
        raise MigrationV2Error("migration_identity_missing", "ProjectIdentity was not captured")
    source_digest = canonical_json_hash(sources)
    conflicts = _source_conflicts(sources)
    summary = {
        "source_count": len(sources),
        "published_prose_count": sum(item["role"] == "published_prose" for item in sources),
        "occurred_event_evidence_count": sum(item["evidence_class"] == "occurred_event" for item in sources),
        "static_constraint_evidence_count": sum(item["evidence_class"] == "static_constraint" for item in sources),
        "unknown_evidence_count": sum(item["evidence_class"] == "unknown" for item in sources),
        "legacy_artifact_count": sum(item["evidence_class"] == "legacy_artifact" for item in sources),
        "conflict_count": len(conflicts),
    }
    identity_fields = {
        "book_id": identity.book_id,
        "expected_identity_sha256": expected_identity_sha256,
        "source_digest": source_digest,
    }
    plan_id = f"migration-{canonical_json_hash(identity_fields)[:20]}"
    plan = {
        "schema_version": MIGRATION_PLAN_SCHEMA_VERSION,
        "plan_id": plan_id,
        "book_id": identity.book_id,
        "expected_identity_sha256": expected_identity_sha256,
        "source_digest": source_digest,
        "sources": sources,
        "conflicts": conflicts,
        "required_decisions": list(REQUIRED_MIGRATION_DECISIONS),
        "evidence_summary": summary,
        "created_at": _required_text("created_at", created_at),
    }
    plan["plan_hash"] = canonical_json_hash(plan)
    return validate_migration_plan(plan)


def validate_migration_plan(value: Any) -> dict[str, Any]:
    plan = _schema_mapping(value, "migration_plan_v2.schema.json", "MigrationPlan")
    for field in ("plan_hash", "expected_identity_sha256", "source_digest"):
        _sha256(field, plan[field])
    _safe_id("plan_id", plan["plan_id"])
    _required_text("book_id", plan["book_id"])
    if plan["required_decisions"] != list(REQUIRED_MIGRATION_DECISIONS):
        raise MigrationV2Error(
            "migration_decision_contract_invalid", "required migration decisions were modified"
        )
    sources = plan["sources"]
    if sources != sorted(sources, key=lambda item: item["relative_path"]):
        raise MigrationV2Error("migration_source_order_invalid", "sources must use stable path order")
    seen: set[str] = set()
    for item in sources:
        relative = _safe_relative_path(item["relative_path"])
        if relative in seen:
            raise MigrationV2Error("migration_source_duplicate", f"duplicate source: {relative}")
        seen.add(relative)
        _sha256("source.sha256", item["sha256"])
        expected_role, expected_evidence, expected_chapter = _source_classification(relative)
        if (
            item["role"] != expected_role
            or item["evidence_class"] != expected_evidence
            or item["chapter_index"] != expected_chapter
        ):
            raise MigrationV2Error(
                "migration_source_classification_invalid",
                f"source classification is not derived: {relative}",
            )
        if item["chapter_index"] is not None and (
            isinstance(item["chapter_index"], bool) or int(item["chapter_index"]) < 1
        ):
            raise MigrationV2Error("migration_source_chapter_invalid", relative)
    if canonical_json_hash(sources) != plan["source_digest"]:
        raise MigrationV2Error("migration_source_digest_mismatch", "source inventory was modified")
    identity_entries = [item for item in sources if item["role"] == "project_identity"]
    if len(identity_entries) != 1 or identity_entries[0]["sha256"] != plan["expected_identity_sha256"]:
        raise MigrationV2Error(
            "migration_identity_digest_mismatch", "ProjectIdentity source does not match its CAS digest"
        )
    expected_conflicts = _source_conflicts(sources)
    if plan["conflicts"] != expected_conflicts:
        raise MigrationV2Error("migration_conflicts_not_derived", "conflicts must be derived from sources")
    expected_summary = {
        "source_count": len(sources),
        "published_prose_count": sum(item["role"] == "published_prose" for item in sources),
        "occurred_event_evidence_count": sum(item["evidence_class"] == "occurred_event" for item in sources),
        "static_constraint_evidence_count": sum(item["evidence_class"] == "static_constraint" for item in sources),
        "unknown_evidence_count": sum(item["evidence_class"] == "unknown" for item in sources),
        "legacy_artifact_count": sum(item["evidence_class"] == "legacy_artifact" for item in sources),
        "conflict_count": len(plan["conflicts"]),
    }
    if plan["evidence_summary"] != expected_summary:
        raise MigrationV2Error("migration_evidence_summary_mismatch", "evidence summary is not derived")
    expected_plan_id = f"migration-{canonical_json_hash({'book_id': plan['book_id'], 'expected_identity_sha256': plan['expected_identity_sha256'], 'source_digest': plan['source_digest']})[:20]}"
    if plan["plan_id"] != expected_plan_id:
        raise MigrationV2Error("migration_plan_id_mismatch", "plan_id is not derived from frozen inputs")
    expected_hash = canonical_json_hash(plan, exclude_fields=("plan_hash",))
    if plan["plan_hash"] != expected_hash:
        raise MigrationV2Error("migration_plan_hash_mismatch", "MigrationPlan content was modified")
    return plan


def assert_migration_plan_current(
    plan: Mapping[str, Any],
    story_project_root: str | Path,
) -> dict[str, Any]:
    validated = validate_migration_plan(dict(plan))
    root = _story_root(story_project_root)
    identity_file = project_identity_path(root)
    if not identity_file.is_file():
        raise MigrationPlanStaleError("ProjectIdentity disappeared")
    actual_identity_hash = _file_sha256(identity_file)
    if actual_identity_hash != validated["expected_identity_sha256"]:
        raise MigrationPlanStaleError("ProjectIdentity bytes changed")
    identity = load_project_identity(root)
    if identity is None or identity.book_id != validated["book_id"]:
        raise MigrationPlanStaleError("ProjectIdentity book_id changed")
    try:
        current_sources = _capture_sources(root)
    except (MigrationV2Error, OSError) as exc:
        raise MigrationPlanStaleError(f"StoryProject sources cannot be recaptured: {exc}") from exc
    if current_sources != validated["sources"]:
        raise MigrationPlanStaleError("StoryProject source bytes or membership changed")
    if canonical_json_hash(current_sources) != validated["source_digest"]:
        raise MigrationPlanStaleError("StoryProject source digest changed")
    return validated


def build_migration_approval(
    plan: Mapping[str, Any],
    *,
    decisions: Mapping[str, Any],
    approver_id: str,
    approved_at: str,
) -> dict[str, Any]:
    validated_plan = validate_migration_plan(dict(plan))
    resolved_decisions = _validate_decisions(decisions, conflicts=validated_plan["conflicts"])
    decision_digest = canonical_json_hash(resolved_decisions)
    approval_id = f"approval-{canonical_json_hash({'plan_hash': validated_plan['plan_hash'], 'decision_digest': decision_digest})[:20]}"
    approval = {
        "schema_version": MIGRATION_APPROVAL_SCHEMA_VERSION,
        "approval_id": approval_id,
        "plan_id": validated_plan["plan_id"],
        "plan_hash": validated_plan["plan_hash"],
        "book_id": validated_plan["book_id"],
        "source_digest": validated_plan["source_digest"],
        "decisions": resolved_decisions,
        "decision_digest": decision_digest,
        "approver_id": _safe_id("approver_id", approver_id),
        "approved_at": _required_text("approved_at", approved_at),
    }
    approval["approval_hash"] = canonical_json_hash(approval)
    return validate_migration_approval(approval, plan=validated_plan)


def validate_migration_approval(
    value: Any,
    *,
    plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    approval = _schema_mapping(value, "migration_approval_v2.schema.json", "MigrationApproval")
    _safe_id("approval_id", approval["approval_id"])
    _safe_id("plan_id", approval["plan_id"])
    _safe_id("approver_id", approval["approver_id"])
    for field in ("approval_hash", "plan_hash", "source_digest", "decision_digest"):
        _sha256(field, approval[field])
    decisions = _validate_decisions(
        approval["decisions"],
        conflicts=(validate_migration_plan(dict(plan))["conflicts"] if plan is not None else None),
    )
    if decisions != approval["decisions"] or canonical_json_hash(decisions) != approval["decision_digest"]:
        raise MigrationV2Error("migration_decision_digest_mismatch", "approval decisions were modified")
    if plan is not None:
        validated_plan = validate_migration_plan(dict(plan))
        for field in ("plan_id", "plan_hash", "book_id", "source_digest"):
            if approval[field] != validated_plan[field]:
                raise MigrationV2Error("migration_approval_plan_mismatch", f"approval {field} differs")
    expected_approval_id = f"approval-{canonical_json_hash({'plan_hash': approval['plan_hash'], 'decision_digest': approval['decision_digest']})[:20]}"
    if approval["approval_id"] != expected_approval_id:
        raise MigrationV2Error("migration_approval_id_mismatch", "approval_id is not derived")
    expected_hash = canonical_json_hash(approval, exclude_fields=("approval_hash",))
    if approval["approval_hash"] != expected_hash:
        raise MigrationV2Error("migration_approval_hash_mismatch", "MigrationApproval content was modified")
    return approval


def _capture_sources(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for directory, role in (
        (PROSE_DIR_NAME, "published_prose"),
        (OUTLINE_DIR_NAME, "outline"),
        (SETTING_DIR_NAME, "explicit_setting"),
        (TRACKING_DIR_NAME, "tracking_projection"),
    ):
        base = root / directory
        if not base.exists():
            continue
        _assert_safe_source_path(base, root)
        for path in sorted(base.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
            if not path.is_file():
                continue
            _assert_safe_source_path(path, root)
            resolved_role = role
            chapter: int | None = None
            if role == "published_prose":
                chapter = prose_chapter_index(path)
            elif role == "outline":
                chapter = outline_chapter_index(path)
                resolved_role = "chapter_outline" if chapter is not None else "master_outline"
            entries.append(_source_entry(root, path, role=resolved_role, chapter_index=chapter))

    identity = project_identity_path(root)
    if identity.is_file():
        _assert_safe_source_path(identity, root)
        entries.append(_source_entry(root, identity, role="project_identity", chapter_index=None))

    runtime = root / ".novelagent" / "runtime"
    if runtime.is_dir():
        _assert_safe_source_path(runtime, root)
        for child_name in ("runs", "persistence", "receipts", "memory"):
            base = runtime / child_name
            if not base.exists():
                continue
            for path in sorted(base.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
                if not path.is_file():
                    continue
                _assert_safe_source_path(path, root)
                entries.append(_source_entry(root, path, role=f"legacy_{child_name}", chapter_index=None))
    return sorted(entries, key=lambda item: item["relative_path"])


def _source_entry(root: Path, path: Path, *, role: str, chapter_index: int | None) -> dict[str, Any]:
    if role == "published_prose":
        evidence_class = "occurred_event"
    elif role == "explicit_setting":
        evidence_class = "static_constraint"
    elif role == "project_identity":
        evidence_class = "identity"
    elif role.startswith("legacy_"):
        evidence_class = "legacy_artifact"
    else:
        evidence_class = "unknown"
    content = path.read_bytes()
    return {
        "relative_path": path.relative_to(root).as_posix(),
        "role": role,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "evidence_class": evidence_class,
        "chapter_index": chapter_index,
    }


def _source_classification(relative_path: str) -> tuple[str, str, int | None]:
    path = Path(relative_path)
    parts = PurePath(relative_path).parts
    if not parts:
        raise MigrationV2Error("migration_source_classification_invalid", relative_path)
    if parts[0] == PROSE_DIR_NAME:
        role = "published_prose"
        chapter = prose_chapter_index(path)
        evidence = "occurred_event"
    elif parts[0] == OUTLINE_DIR_NAME:
        chapter = outline_chapter_index(path)
        role = "chapter_outline" if chapter is not None else "master_outline"
        evidence = "unknown"
    elif parts[0] == SETTING_DIR_NAME:
        role, evidence, chapter = "explicit_setting", "static_constraint", None
    elif parts[0] == TRACKING_DIR_NAME:
        role, evidence, chapter = "tracking_projection", "unknown", None
    elif relative_path == ".novelagent/project.json":
        role, evidence, chapter = "project_identity", "identity", None
    elif len(parts) >= 4 and parts[:2] == (".novelagent", "runtime") and parts[2] in {
        "runs",
        "persistence",
        "receipts",
        "memory",
    }:
        role, evidence, chapter = f"legacy_{parts[2]}", "legacy_artifact", None
    else:
        raise MigrationV2Error(
            "migration_source_classification_invalid", f"unrecognized controlled source: {relative_path}"
        )
    return role, evidence, chapter


def _source_conflicts(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_role_chapter: dict[tuple[str, int], list[str]] = {}
    for item in sources:
        chapter = item["chapter_index"]
        if chapter is None or item["role"] not in {"published_prose", "chapter_outline"}:
            continue
        by_role_chapter.setdefault((item["role"], chapter), []).append(item["relative_path"])
    conflicts = []
    for (role, chapter), paths in sorted(by_role_chapter.items()):
        if len(paths) > 1:
            conflicts.append(
                {
                    "code": "duplicate_chapter_source",
                    "role": role,
                    "chapter_index": chapter,
                    "paths": paths,
                    "requires_decision": True,
                }
            )
    return conflicts


def _validate_decisions(
    value: Mapping[str, Any],
    *,
    conflicts: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MigrationV2Error("migration_decisions_invalid", "decisions must be an object")
    decisions = copy.deepcopy(dict(value))
    expected = set(REQUIRED_MIGRATION_DECISIONS)
    allowed = expected | {"conflict_resolutions"}
    if set(decisions) - allowed or expected - set(decisions):
        raise MigrationV2Error(
            "migration_decisions_invalid",
            f"decisions must contain exactly the required topics plus optional conflict_resolutions",
        )
    elapsed = decisions["timeline_elapsed_minutes"]
    if isinstance(elapsed, bool) or not isinstance(elapsed, int) or elapsed < 0:
        raise MigrationV2Error("migration_timeline_decision_invalid", "elapsed minutes must be non-negative")
    for field in ("chapter_10_character_state", "inventory", "lexicon", "corruption"):
        if not isinstance(decisions[field], dict):
            raise MigrationV2Error("migration_decisions_invalid", f"{field} must be an object")
    if not isinstance(decisions["open_foreshadowing"], list):
        raise MigrationV2Error("migration_decisions_invalid", "open_foreshadowing must be an array")
    if conflicts:
        resolutions = decisions.get("conflict_resolutions")
        if not isinstance(resolutions, dict):
            raise MigrationV2Error(
                "migration_conflict_resolution_missing", "all reported conflicts require explicit resolutions"
            )
        expected_codes = {
            f"{item['code']}:{item['role']}:{item['chapter_index']}" for item in conflicts
        }
        if set(resolutions) != expected_codes or any(
            not isinstance(resolutions[key], str) or not resolutions[key].strip() for key in resolutions
        ):
            raise MigrationV2Error(
                "migration_conflict_resolution_invalid", "conflict resolution keys or values are incomplete"
            )
    elif conflicts is not None and "conflict_resolutions" in decisions and decisions["conflict_resolutions"] not in ({}, None):
        raise MigrationV2Error(
            "migration_conflict_resolution_invalid", "no conflict resolutions are expected for this plan"
        )
    _assert_safe_decision_value(decisions)
    try:
        return json.loads(json.dumps(decisions, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise MigrationV2Error("migration_decisions_invalid", str(exc)) from exc


def _assert_safe_source_path(path: Path, root: Path) -> None:
    try:
        path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise MigrationV2Error("migration_source_escape", f"source escapes StoryProject: {path.name}") from exc
    current = path
    while current != root.parent:
        if current.is_symlink() or _is_reparse_point(current):
            raise MigrationV2Error("migration_source_link_forbidden", f"linked source is forbidden: {current.name}")
        if current == root:
            break
        current = current.parent


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError as exc:
        raise MigrationV2Error("migration_source_unreadable", f"cannot inspect source: {path.name}: {exc}") from exc
    return bool(attributes & getattr(os.stat_result, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)) if os.name == "nt" else False


def _assert_safe_decision_value(value: Any, *, path: str = "$", depth: int = 0) -> None:
    if depth > 16:
        raise MigrationV2Error("migration_decisions_invalid", "decision nesting is too deep")
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _FORBIDDEN_DECISION_KEYS or any(
                token in normalized for token in ("password", "secret", "credential", "api_key")
            ):
                raise MigrationV2Error(
                    "migration_decision_credential_forbidden", f"credential-like field is forbidden: {path}.{key}"
                )
            _assert_safe_decision_value(child, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_safe_decision_value(child, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, str):
        stripped = value.strip()
        if re.match(r"^(?:[A-Za-z]:[\\/]|[/\\]{1,2})", stripped):
            raise MigrationV2Error(
                "migration_decision_path_forbidden", f"absolute path is forbidden: {path}"
            )
        if stripped.lower().startswith(("bearer ", "sk-", "-----begin private key")):
            raise MigrationV2Error(
                "migration_decision_credential_forbidden", f"credential-like value is forbidden: {path}"
            )


def _story_root(value: str | Path) -> Path:
    root = Path(value).resolve()
    if not root.is_dir():
        raise MigrationV2Error("migration_story_project_invalid", "StoryProject root is not a directory")
    return root


def _schema_mapping(value: Any, schema: str, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MigrationV2Error("migration_contract_invalid", f"{label} must be an object")
    try:
        return validate_schema(copy.deepcopy(dict(value)), schema)
    except SchemaValidationError as exc:
        raise MigrationV2Error("migration_contract_invalid", f"invalid {label}: {exc}") from exc


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise MigrationV2Error("migration_source_path_invalid", "source path must be relative text")
    normalized = value.replace("\\", "/")
    path = PurePath(normalized)
    if path.is_absolute() or path.drive or normalized.startswith("//") or any(
        part in {"", ".", ".."} for part in normalized.split("/")
    ):
        raise MigrationV2Error("migration_source_path_invalid", "source path is unsafe")
    return normalized


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise MigrationV2Error("migration_source_unreadable", f"cannot read {path.name}: {exc}") from exc


def _sha256(label: str, value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise MigrationV2Error("migration_digest_invalid", f"{label} must be lowercase SHA-256")
    return value


def _safe_id(label: str, value: Any) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise MigrationV2Error("migration_id_invalid", f"{label} is not a safe identifier")
    return value


def _required_text(label: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MigrationV2Error("migration_text_invalid", f"{label} is required")
    return value.strip()


__all__ = [
    "MIGRATION_APPROVAL_SCHEMA_VERSION",
    "MIGRATION_PLAN_SCHEMA_VERSION",
    "REQUIRED_MIGRATION_DECISIONS",
    "MigrationPlanStaleError",
    "MigrationV2Error",
    "assert_migration_plan_current",
    "build_migration_approval",
    "build_migration_plan",
    "validate_migration_approval",
    "validate_migration_plan",
]
