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
MIGRATION_SHADOW_CANDIDATE_SCHEMA_VERSION = "1.0"
MIGRATION_PREVIEW_SCHEMA_VERSION = "1.0"
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
_RUNTIME_SOURCE_DIRECTORIES = (
    "runs",
    "persistence",
    "receipts",
    "deliveries",
    "memory",
    "chapters",
    "reviews",
)
_RUNTIME_SOURCE_FILES = ("snapshot.json", "memory.json")
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
    authority = identity.authority or {}
    if authority.get("mode") != "legacy_markdown_v1":
        raise MigrationV2Error(
            "migration_event_authority_already_active",
            "event authority is already active; migration preview cannot authorize a downgrade or replay",
        )
    identity_path = project_identity_path(root)
    expected_identity_sha256 = _file_sha256(identity_path)
    sources = _capture_sources(root)
    if not any(item["role"] == "project_identity" for item in sources):
        raise MigrationV2Error("migration_identity_missing", "ProjectIdentity was not captured")
    source_digest = canonical_json_hash(sources)
    conflicts = _source_conflicts(sources)
    shadow_candidate = _build_shadow_semantic_candidate(
        root,
        identity=identity,
        sources=sources,
        source_digest=source_digest,
    )
    if _capture_sources(root) != sources:
        raise MigrationPlanStaleError(
            "StoryProject source bytes or membership changed while building migration preview"
        )
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
        "shadow_candidate": shadow_candidate,
        "shadow_candidate_hash": canonical_json_hash(shadow_candidate),
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
    candidate_present = "shadow_candidate" in plan
    candidate_hash_present = "shadow_candidate_hash" in plan
    if candidate_present != candidate_hash_present:
        raise MigrationV2Error(
            "migration_shadow_candidate_invalid",
            "shadow_candidate and shadow_candidate_hash must be present together",
        )
    if candidate_present:
        candidate = _validate_shadow_semantic_candidate(
            plan["shadow_candidate"],
            book_id=plan["book_id"],
            source_digest=plan["source_digest"],
        )
        _sha256("shadow_candidate_hash", plan["shadow_candidate_hash"])
        if candidate != plan["shadow_candidate"] or canonical_json_hash(candidate) != plan["shadow_candidate_hash"]:
            raise MigrationV2Error(
                "migration_shadow_candidate_digest_mismatch",
                "shadow semantic candidate was modified",
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
    if "shadow_candidate" in validated:
        current_candidate = _build_shadow_semantic_candidate(
            root,
            identity=identity,
            sources=current_sources,
            source_digest=validated["source_digest"],
        )
        if _capture_sources(root) != current_sources:
            raise MigrationPlanStaleError(
                "StoryProject source bytes or membership changed while checking semantic candidate"
            )
        if current_candidate != validated["shadow_candidate"]:
            raise MigrationPlanStaleError("StoryProject shadow semantic candidate changed")
    return validated


def build_migration_preview(
    story_project_root: str | Path,
    *,
    created_at: str,
) -> dict[str, Any]:
    """Build a read-only migration preview; never approve, execute, or activate it."""

    plan = build_migration_plan(story_project_root, created_at=created_at)
    candidate = copy.deepcopy(plan["shadow_candidate"])
    preview = {
        "schema_version": MIGRATION_PREVIEW_SCHEMA_VERSION,
        "mode": "read_only_shadow",
        "read_only": True,
        "authoritative": False,
        "affects_generation": False,
        "affects_source": False,
        "approval_created": False,
        "execution_performed": False,
        "activation_performed": False,
        "book_id": plan["book_id"],
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "shadow_candidate_hash": plan["shadow_candidate_hash"],
        "source_conflicts": copy.deepcopy(plan["conflicts"]),
        "semantic_conflicts": copy.deepcopy(candidate["conflicts"]),
        "warnings": copy.deepcopy(candidate["warnings"]),
        "unsupported": copy.deepcopy(candidate["unsupported"]),
        "required_user_decisions": copy.deepcopy(candidate["required_user_decisions"]),
        "plan": plan,
    }
    preview["preview_hash"] = canonical_json_hash(preview)
    return preview


def assert_migration_source_snapshot_current(
    plan: Mapping[str, Any],
    story_project_root: str | Path,
    *,
    expected_identity_sha256: str | None = None,
    ignored_relative_prefixes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Recheck frozen migration inputs during an atomic identity transition.

    Migration bootstrap writes its Memory 2.2 files before the commit marker and
    replaces ProjectIdentity last.  This narrower CAS check permits only those
    explicitly named transition effects while keeping every pre-existing source
    byte and membership entry frozen through the marker boundary.
    """

    validated = validate_migration_plan(dict(plan))
    root = _story_root(story_project_root)
    prefixes = tuple(_safe_relative_prefix(item) for item in ignored_relative_prefixes)
    current = _capture_sources(root)

    expected_items = [
        item
        for item in validated["sources"]
        if not _matches_relative_prefix(item["relative_path"], prefixes)
    ]
    current_items = [
        item
        for item in current
        if not _matches_relative_prefix(item["relative_path"], prefixes)
    ]
    if expected_identity_sha256 is not None:
        expected_after = _sha256("expected_identity_sha256", expected_identity_sha256)
        identity_items = [item for item in current_items if item["role"] == "project_identity"]
        if len(identity_items) != 1 or identity_items[0]["sha256"] != expected_after:
            raise MigrationPlanStaleError("ProjectIdentity did not match the atomic transition target")
        expected_items = [item for item in expected_items if item["role"] != "project_identity"]
        current_items = [item for item in current_items if item["role"] != "project_identity"]

    if current_items != expected_items:
        raise MigrationPlanStaleError(
            "StoryProject source bytes or membership changed during migration bootstrap"
        )
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


def _build_shadow_semantic_candidate(
    root: Path,
    *,
    identity: Any,
    sources: list[dict[str, Any]],
    source_digest: str,
) -> dict[str, Any]:
    from core.story_project.semantic_parser import (
        StoryProjectSemanticParseError,
        parse_story_project_semantic_state,
    )

    chapter_index, target_basis, outline_source = _shadow_candidate_target(sources)
    state: dict[str, Any] | None = None
    if chapter_index is not None and outline_source is not None:
        try:
            state = parse_story_project_semantic_state(
                root,
                chapter_index,
                project_identity=identity,
            )
        except (StoryProjectSemanticParseError, OSError, UnicodeError, ValueError):
            state = None

    if state is None:
        conflicts = [
            {
                "id": "migration-semantic-candidate-unavailable",
                "field_path": "semantic_state",
                "code": "semantic_candidate_unavailable",
                "blocking": True,
                "sources": [outline_source] if outline_source is not None else [],
                "message": "Frozen sources could not produce a complete semantic candidate.",
            }
        ]
        warnings = [
            {
                "code": "semantic_candidate_requires_manual_review",
                "source_path": outline_source or "大纲",
                "message": "No semantic values are authoritative until the required user decisions are approved.",
            }
        ]
        unsupported = [
            {
                "code": "semantic_candidate_unavailable",
                "source_paths": [outline_source] if outline_source is not None else [],
                "authoritative": False,
            }
        ]
    else:
        conflicts = copy.deepcopy(state["conflicts"])
        warnings = copy.deepcopy(state["parse_warnings"])
        unsupported = copy.deepcopy(state["unsupported_excerpts"])

    candidate = {
        "schema_version": MIGRATION_SHADOW_CANDIDATE_SCHEMA_VERSION,
        "mode": "shadow",
        "authoritative": False,
        "read_only": True,
        "affects_generation": False,
        "affects_source": False,
        "book_id": identity.book_id,
        "source_digest": source_digest,
        "chapter_index": chapter_index,
        "target_basis": target_basis,
        "outline_source": outline_source,
        "state": copy.deepcopy(state),
        "state_hash": canonical_json_hash(state) if state is not None else None,
        "conflicts": conflicts,
        "warnings": warnings,
        "unsupported": unsupported,
        "required_user_decisions": _required_user_decision_items(state),
    }
    return _validate_shadow_semantic_candidate(
        candidate,
        book_id=identity.book_id,
        source_digest=source_digest,
    )


def _shadow_candidate_target(
    sources: list[dict[str, Any]],
) -> tuple[int | None, str, str | None]:
    published = sorted(
        {
            int(item["chapter_index"])
            for item in sources
            if item["role"] == "published_prose" and item["chapter_index"] is not None
        }
    )
    outlines: dict[int, list[str]] = {}
    for item in sources:
        if item["role"] != "chapter_outline" or item["chapter_index"] is None:
            continue
        outlines.setdefault(int(item["chapter_index"]), []).append(item["relative_path"])

    if published:
        next_chapter = max(published) + 1
        if len(outlines.get(next_chapter, [])) == 1:
            return next_chapter, "next_unpublished_chapter", outlines[next_chapter][0]
        latest = max(published)
        if len(outlines.get(latest, [])) == 1:
            return latest, "latest_published_chapter_fallback", outlines[latest][0]
    unique_outline_chapters = sorted(chapter for chapter, paths in outlines.items() if len(paths) == 1)
    if unique_outline_chapters:
        chapter = unique_outline_chapters[-1]
        return chapter, "latest_unique_outline_fallback", outlines[chapter][0]
    return None, "no_unique_chapter_outline", None


def _required_user_decision_items(state: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    prefixes = {
        "timeline_elapsed_minutes": ("timeline",),
        "chapter_10_character_state": ("characters", "spatial_state.character_positions"),
        "open_foreshadowing": ("foreshadowing",),
        "inventory": ("world_state.inventory",),
        "lexicon": ("world_state.lexicon",),
        "corruption": ("world_state.corruption",),
    }
    provenance = list((state or {}).get("provenance") or [])
    items: list[dict[str, Any]] = []
    for topic in REQUIRED_MIGRATION_DECISIONS:
        evidence_paths = sorted(
            {
                str(item["source_path"])
                for item in provenance
                if isinstance(item, Mapping)
                and isinstance(item.get("source_path"), str)
                and any(
                    str(item.get("field_path") or "") == prefix
                    or str(item.get("field_path") or "").startswith(prefix + ".")
                    or str(item.get("field_path") or "").startswith(prefix + "[")
                    for prefix in prefixes[topic]
                )
            }
        )
        items.append(
            {
                "topic": topic,
                "status": "user_decision_required",
                "required": True,
                "candidate_evidence_available": bool(evidence_paths),
                "evidence_paths": evidence_paths,
            }
        )
    return items


def _validate_shadow_semantic_candidate(
    value: Any,
    *,
    book_id: str,
    source_digest: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MigrationV2Error(
            "migration_shadow_candidate_invalid", "shadow semantic candidate must be an object"
        )
    candidate = copy.deepcopy(dict(value))
    expected_keys = {
        "schema_version",
        "mode",
        "authoritative",
        "read_only",
        "affects_generation",
        "affects_source",
        "book_id",
        "source_digest",
        "chapter_index",
        "target_basis",
        "outline_source",
        "state",
        "state_hash",
        "conflicts",
        "warnings",
        "unsupported",
        "required_user_decisions",
    }
    if set(candidate) != expected_keys:
        raise MigrationV2Error(
            "migration_shadow_candidate_invalid", "shadow semantic candidate fields are incomplete"
        )
    if candidate["schema_version"] != MIGRATION_SHADOW_CANDIDATE_SCHEMA_VERSION:
        raise MigrationV2Error(
            "migration_shadow_candidate_invalid", "unsupported shadow semantic candidate version"
        )
    expected_flags = {
        "mode": "shadow",
        "authoritative": False,
        "read_only": True,
        "affects_generation": False,
        "affects_source": False,
    }
    if any(candidate[field] != expected for field, expected in expected_flags.items()):
        raise MigrationV2Error(
            "migration_shadow_candidate_invalid", "shadow semantic candidate authority flags changed"
        )
    if candidate["book_id"] != book_id or candidate["source_digest"] != source_digest:
        raise MigrationV2Error(
            "migration_shadow_candidate_invalid", "shadow semantic candidate is not bound to frozen inputs"
        )
    _sha256("shadow_candidate.source_digest", candidate["source_digest"])
    chapter_index = candidate["chapter_index"]
    if chapter_index is not None and (
        isinstance(chapter_index, bool) or not isinstance(chapter_index, int) or chapter_index < 1
    ):
        raise MigrationV2Error(
            "migration_shadow_candidate_invalid", "candidate chapter_index must be positive or null"
        )
    if candidate["target_basis"] not in {
        "next_unpublished_chapter",
        "latest_published_chapter_fallback",
        "latest_unique_outline_fallback",
        "no_unique_chapter_outline",
    }:
        raise MigrationV2Error(
            "migration_shadow_candidate_invalid", "candidate target basis is unsupported"
        )
    if candidate["outline_source"] is not None:
        candidate["outline_source"] = _safe_relative_path(candidate["outline_source"])
    state = candidate["state"]
    if state is None:
        if candidate["state_hash"] is not None:
            raise MigrationV2Error(
                "migration_shadow_candidate_invalid", "unavailable candidate cannot have a state hash"
            )
    else:
        from core.story_project.semantic_contracts import validate_story_project_semantic_state

        state = validate_story_project_semantic_state(state)
        if state["book_id"] != book_id or state["chapter_index"] != chapter_index:
            raise MigrationV2Error(
                "migration_shadow_candidate_invalid", "semantic state identity differs from its candidate"
            )
        _sha256("shadow_candidate.state_hash", candidate["state_hash"])
        if canonical_json_hash(state) != candidate["state_hash"]:
            raise MigrationV2Error(
                "migration_shadow_candidate_invalid", "semantic state hash differs"
            )
        if (
            candidate["conflicts"] != state["conflicts"]
            or candidate["warnings"] != state["parse_warnings"]
            or candidate["unsupported"] != state["unsupported_excerpts"]
        ):
            raise MigrationV2Error(
                "migration_shadow_candidate_invalid", "semantic diagnostics are not derived from state"
            )
        candidate["state"] = state
    for field in ("conflicts", "warnings", "unsupported"):
        if not isinstance(candidate[field], list):
            raise MigrationV2Error(
                "migration_shadow_candidate_invalid", f"candidate {field} must be an array"
            )
    expected_decisions = _required_user_decision_items(state)
    if candidate["required_user_decisions"] != expected_decisions:
        raise MigrationV2Error(
            "migration_shadow_candidate_invalid", "required user decisions are not derived"
        )
    try:
        return json.loads(json.dumps(candidate, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise MigrationV2Error("migration_shadow_candidate_invalid", str(exc)) from exc


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
            if path.name == ".gitkeep":
                continue
            _assert_safe_source_path(path, root)
            resolved_role = role
            chapter: int | None = None
            if role == "published_prose":
                chapter = prose_chapter_index(path)
                if chapter is None:
                    resolved_role = "unclassified_prose"
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
        for child_name in _RUNTIME_SOURCE_DIRECTORIES:
            base = runtime / child_name
            if not base.exists():
                continue
            for path in sorted(base.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
                if not path.is_file() or path.name.endswith(".lock"):
                    continue
                _assert_safe_source_path(path, root)
                entries.append(
                    _source_entry(
                        root,
                        path,
                        role=f"legacy_{child_name}",
                        chapter_index=None,
                    )
                )
        for child_name in _RUNTIME_SOURCE_FILES:
            path = runtime / child_name
            if not path.is_file():
                continue
            _assert_safe_source_path(path, root)
            entries.append(
                _source_entry(
                    root,
                    path,
                    role="legacy_runtime",
                    chapter_index=None,
                )
            )
    return sorted(entries, key=lambda item: item["relative_path"])


def _source_entry(root: Path, path: Path, *, role: str, chapter_index: int | None) -> dict[str, Any]:
    if role == "published_prose":
        evidence_class = "occurred_event"
    elif role == "unclassified_prose":
        evidence_class = "unknown"
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
        chapter = prose_chapter_index(path)
        if chapter is None:
            role, evidence = "unclassified_prose", "unknown"
        else:
            role, evidence = "published_prose", "occurred_event"
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
    elif (
        len(parts) >= 4
        and parts[:2] == (".novelagent", "runtime")
        and parts[2] in _RUNTIME_SOURCE_DIRECTORIES
    ):
        role, evidence, chapter = f"legacy_{parts[2]}", "legacy_artifact", None
    elif relative_path in {
        f".novelagent/runtime/{name}" for name in _RUNTIME_SOURCE_FILES
    }:
        role, evidence, chapter = "legacy_runtime", "legacy_artifact", None
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
    for item in sources:
        if item["role"] != "unclassified_prose":
            continue
        relative_path = item["relative_path"]
        conflicts.append(
            {
                "code": "unclassified_chapter_source",
                "conflict_id": (
                    "unclassified_chapter_source:"
                    + hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:16]
                ),
                "role": "published_prose",
                "chapter_index": None,
                "paths": [relative_path],
                "requires_decision": True,
                "resolution_kind": "exclude_unclassified_source",
            }
        )
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
        expected_codes = {_conflict_resolution_key(item) for item in conflicts}
        if set(resolutions) != expected_codes or any(
            not isinstance(resolutions[key], str) or not resolutions[key].strip() for key in resolutions
        ):
            raise MigrationV2Error(
                "migration_conflict_resolution_invalid", "conflict resolution keys or values are incomplete"
            )
        conflict_paths = {
            _conflict_resolution_key(item): set(item["paths"])
            for item in conflicts
        }
        for key, chosen in resolutions.items():
            normalized = _safe_relative_path(chosen)
            if normalized not in conflict_paths[key]:
                raise MigrationV2Error(
                    "migration_conflict_resolution_invalid",
                    f"conflict resolution must select a reported source path: {key}",
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


def _conflict_resolution_key(conflict: Mapping[str, Any]) -> str:
    explicit = conflict.get("conflict_id")
    if isinstance(explicit, str) and explicit:
        return explicit
    return f"{conflict['code']}:{conflict['role']}:{conflict['chapter_index']}"


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


def _safe_relative_prefix(value: Any) -> str:
    normalized = _safe_relative_path(value)
    return normalized.rstrip("/")


def _matches_relative_prefix(relative_path: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        relative_path == prefix or relative_path.startswith(prefix + "/")
        for prefix in prefixes
    )


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
    "MIGRATION_PREVIEW_SCHEMA_VERSION",
    "MIGRATION_SHADOW_CANDIDATE_SCHEMA_VERSION",
    "REQUIRED_MIGRATION_DECISIONS",
    "MigrationPlanStaleError",
    "MigrationV2Error",
    "assert_migration_plan_current",
    "assert_migration_source_snapshot_current",
    "build_migration_approval",
    "build_migration_plan",
    "build_migration_preview",
    "validate_migration_approval",
    "validate_migration_plan",
]
