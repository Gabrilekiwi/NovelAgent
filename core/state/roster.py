from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any


ROSTER_INVALID_MUTATION = "invalid_roster_mutation"
ROSTER_GENERATION_PROJECTION_POLICY = "roster_generation_state_v1"
ROSTER_GENERATION_OMITTED_FIELDS = frozenset(
    {
        "baseline_evidence",
        "introduced_chapter",
        "introduced_event_id",
        "baseline_source",
        "migration_id",
    }
)
_ROSTER_OPERATIONS = frozenset(
    {"join", "leave", "dead", "missing", "replace", "resolve"}
)
_REMOVAL_OPERATIONS = frozenset({"leave", "dead", "missing"})
_BASELINE_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_AUDIT_FIELDS = (
    "introduced_chapter",
    "introduced_event_id",
    "baseline_source",
    "migration_id",
)


def project_roster_for_generation(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the model-facing roster state without immutable audit payloads.

    Local reducers and persisted snapshots retain the complete record. Generation
    only needs identity, members, aggregate counts, and precedence state; the
    omitted migration provenance remains bound by the raw authority source hash.
    """

    if not isinstance(record, Mapping):
        return {}
    return {
        key: copy.deepcopy(value)
        for key, value in record.items()
        if key not in ROSTER_GENERATION_OMITTED_FIELDS
    }


def reduce_roster_mutation(
    rosters: Mapping[str, Any],
    mutation: Mapping[str, Any],
    *,
    current_events: Mapping[str, Any] | Iterable[str] = (),
    require_current_event: bool = True,
) -> dict[str, Any]:
    """Apply one roster mutation without mutating the supplied roster mapping.

    The returned issues use layer-neutral codes. Callers may translate
    ``invalid_roster_mutation`` to their public Scene/Authority compatibility
    code, but the transition and all other issue codes are shared.
    """

    raw = copy.deepcopy(dict(mutation)) if isinstance(mutation, Mapping) else {}
    roster_id = str(raw.get("roster_id") or raw.get("id") or "").strip()
    operation = str(raw.get("operation") or "replace").strip()
    issues: list[dict[str, Any]] = []
    if not roster_id:
        issues.append(
            _issue(
                ROSTER_INVALID_MUTATION,
                "Roster mutation requires roster_id.",
                {"mutation": raw},
            )
        )
        return {
            "roster_id": "",
            "record": None,
            "issues": issues,
            "expected_delta": 0,
            "computed_count": 0,
        }
    if operation not in _ROSTER_OPERATIONS:
        issues.append(
            _issue(
                ROSTER_INVALID_MUTATION,
                f"Roster {roster_id} uses unsupported operation {operation!r}.",
                {"roster_id": roster_id, "operation": operation},
            )
        )
        return {
            "roster_id": roster_id,
            "record": None,
            "issues": issues,
            "expected_delta": 0,
            "computed_count": 0,
        }

    event_index = _current_event_index(current_events)
    valid_current_event, event_issues = _validate_event_references(
        roster_id=roster_id,
        mutation=raw,
        event_index=event_index,
        require_current_event=require_current_event,
    )
    issues.extend(event_issues)

    existing_raw = rosters.get(roster_id)
    existing = copy.deepcopy(existing_raw) if isinstance(existing_raw, dict) else {}
    is_initial = not isinstance(existing_raw, dict)
    existing_members, existing_member_issues = _member_map(
        existing.get("members"),
        roster_id=roster_id,
        source="existing",
    )
    issues.extend(existing_member_issues)
    existing_unresolved_raw = existing.get("unresolved_count", 0)
    if not _nonnegative_int(existing_unresolved_raw):
        issues.append(
            _issue(
                ROSTER_INVALID_MUTATION,
                f"Roster {roster_id} has an invalid unresolved_count baseline.",
                {
                    "roster_id": roster_id,
                    "unresolved_count": existing_unresolved_raw,
                },
            )
        )
        existing_unresolved = 0
    else:
        existing_unresolved = int(existing_unresolved_raw)

    canonical_name, aliases, identity_issues = _resolve_identity(
        rosters,
        roster_id=roster_id,
        existing=existing,
        incoming=raw,
        is_initial=is_initial,
    )
    issues.extend(identity_issues)
    audit_metadata, audit_issues = _resolve_audit_metadata(
        roster_id=roster_id,
        existing=existing,
        incoming=raw,
        is_initial=is_initial,
    )
    issues.extend(audit_issues)

    declared_member_ids, declared_id_issues = _member_id_list(
        raw.get("member_ids"),
        roster_id=roster_id,
    )
    issues.extend(declared_id_issues)
    incoming_members, incoming_member_issues = _member_map(
        raw.get("members"),
        roster_id=roster_id,
        source="incoming",
    )
    issues.extend(incoming_member_issues)
    incoming_member_ids = list(incoming_members)
    if (
        operation in {"join", "replace", "resolve"}
        and declared_member_ids
        and set(declared_member_ids) != set(incoming_member_ids)
    ):
        issues.append(
            _issue(
                "roster_count_mismatch",
                f"Roster {roster_id} member_ids do not match member records.",
                {
                    "roster_id": roster_id,
                    "member_ids": declared_member_ids,
                    "record_member_ids": incoming_member_ids,
                },
            )
        )

    change_ids = set(declared_member_ids or incoming_member_ids)
    duplicate_join_ids = (
        sorted(change_ids.intersection(existing_members))
        if operation in {"join", "resolve"}
        else []
    )
    if duplicate_join_ids:
        verb = "resolve" if operation == "resolve" else "join"
        issues.append(
            _issue(
                "roster_member_already_exists",
                f"Roster {roster_id} cannot {verb} members that already exist.",
                {
                    "roster_id": roster_id,
                    "operation": operation,
                    "member_ids": duplicate_join_ids,
                    "existing_members": {
                        member_id: copy.deepcopy(existing_members[member_id])
                        for member_id in duplicate_join_ids
                    },
                    "incoming_members": {
                        member_id: copy.deepcopy(incoming_members.get(member_id))
                        for member_id in duplicate_join_ids
                    },
                },
            )
        )
    missing_removal_ids = (
        sorted(change_ids.difference(existing_members))
        if operation in _REMOVAL_OPERATIONS
        else []
    )
    if missing_removal_ids:
        issues.append(
            _issue(
                "roster_member_not_found",
                f"Roster {roster_id} cannot {operation} members that do not exist.",
                {
                    "roster_id": roster_id,
                    "operation": operation,
                    "member_ids": missing_removal_ids,
                },
            )
        )

    for member_id, member in incoming_members.items():
        prior = existing_members.get(member_id)
        if (
            operation == "replace"
            and isinstance(prior, dict)
            and _member_identity_changed(prior, member)
        ):
            issues.append(
                _issue(
                    "roster_member_identity_drift",
                    f"Roster member {member_id} changed identity fields.",
                    {
                        "roster_id": roster_id,
                        "member_id": member_id,
                        "before": prior,
                        "after": member,
                    },
                )
            )
            incoming_members[member_id] = copy.deepcopy(prior)

    if operation in {"join", "resolve"}:
        next_members = copy.deepcopy(existing_members)
        for member_id, member in incoming_members.items():
            if member_id not in existing_members:
                next_members[member_id] = copy.deepcopy(member)
    elif operation in _REMOVAL_OPERATIONS:
        next_members = {
            member_id: copy.deepcopy(member)
            for member_id, member in existing_members.items()
            if member_id not in change_ids
        }
    else:
        next_members = copy.deepcopy(incoming_members)

    next_unresolved, unresolved_issues = _resolve_unresolved_count(
        roster_id=roster_id,
        operation=operation,
        existing_unresolved=existing_unresolved,
        mutation=raw,
        resolved_member_count=(
            len(set(incoming_members).difference(existing_members))
            if operation == "resolve"
            else 0
        ),
    )
    issues.extend(unresolved_issues)

    existing_aliases = _unique_strings(_string_values(existing.get("aliases")))
    if operation == "replace" and not is_initial:
        replacement_differences: dict[str, Any] = {}
        if next_members != existing_members:
            replacement_differences["members"] = {
                "before": list(existing_members.values()),
                "after": list(next_members.values()),
            }
        if next_unresolved != existing_unresolved:
            replacement_differences["unresolved_count"] = {
                "before": existing_unresolved,
                "after": next_unresolved,
            }
        if canonical_name != _existing_name(existing):
            replacement_differences["name"] = {
                "before": _existing_name(existing),
                "after": canonical_name,
            }
        if aliases != existing_aliases:
            replacement_differences["aliases"] = {
                "before": existing_aliases,
                "after": aliases,
            }
        if replacement_differences:
            issues.append(
                _issue(
                    "roster_replace_not_idempotent",
                    f"Existing roster {roster_id} may only be replaced idempotently.",
                    {
                        "roster_id": roster_id,
                        "differences": replacement_differences,
                    },
                )
            )
        # Existing-roster replace is an assertion, not a writer. Preserve the
        # canonical stored ordering and immutable metadata even when the echo
        # is semantically identical but serialized in a different order.
        next_members = copy.deepcopy(existing_members)
        next_unresolved = existing_unresolved
        canonical_name = _existing_name(existing)
        aliases = existing_aliases
        audit_metadata = _existing_audit_metadata(existing)

    stable_member_delta = len(next_members) - len(existing_members)
    unresolved_delta = next_unresolved - existing_unresolved
    expected_delta = stable_member_delta + unresolved_delta
    computed_count = len(next_members) + next_unresolved
    declared_delta = raw.get("delta")
    declared_count = raw.get("declared_count")
    if not _integer(declared_delta):
        issues.append(
            _issue(
                ROSTER_INVALID_MUTATION,
                f"Roster {roster_id} requires an integer delta.",
                {"roster_id": roster_id, "delta": declared_delta},
            )
        )
    elif int(declared_delta) != expected_delta:
        issues.append(
            _issue(
                "roster_count_mismatch",
                f"Roster {roster_id} declared delta is inconsistent.",
                {
                    "roster_id": roster_id,
                    "declared_delta": declared_delta,
                    "computed_delta": expected_delta,
                },
            )
        )
    if not _nonnegative_int(declared_count):
        issues.append(
            _issue(
                ROSTER_INVALID_MUTATION,
                f"Roster {roster_id} requires a non-negative integer declared_count.",
                {"roster_id": roster_id, "declared_count": declared_count},
            )
        )
    elif int(declared_count) != computed_count:
        issues.append(
            _issue(
                "roster_count_mismatch",
                f"Roster {roster_id} declared count is inconsistent.",
                {
                    "roster_id": roster_id,
                    "declared_count": declared_count,
                    "computed_count": computed_count,
                    "stable_member_count": len(next_members),
                    "unresolved_count": next_unresolved,
                },
            )
        )

    if (
        is_initial
        and next_unresolved > 0
        and not valid_current_event
        and not _has_audited_baseline(audit_metadata)
    ):
        issues.append(
            _issue(
                "missing_roster_baseline_evidence",
                f"Aggregate roster {roster_id} requires an auditable baseline.",
                {
                    "roster_id": roster_id,
                    "unresolved_count": next_unresolved,
                    "required_baseline_evidence": [
                        "source_kind",
                        "source_path",
                        "sha256",
                    ],
                    "required_introduced_chapter": "positive integer",
                },
            )
        )

    record = {
        **copy.deepcopy(existing),
        "roster_id": roster_id,
        **({"name": canonical_name} if canonical_name else {}),
        "aliases": aliases,
        **audit_metadata,
        "members": list(next_members.values()),
        "unresolved_count": next_unresolved,
        "declared_count": computed_count,
        "computed_count": computed_count,
    }
    return {
        "roster_id": roster_id,
        "record": record,
        "issues": issues,
        "expected_delta": expected_delta,
        "computed_count": computed_count,
    }


def _resolve_unresolved_count(
    *,
    roster_id: str,
    operation: str,
    existing_unresolved: int,
    mutation: dict[str, Any],
    resolved_member_count: int,
) -> tuple[int, list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    raw_before = mutation.get("unresolved_before")
    raw_delta = mutation.get("unresolved_delta")
    raw_count = mutation.get("unresolved_count")
    has_before = raw_before is not None
    has_delta = raw_delta is not None
    has_count = raw_count is not None

    if operation == "replace":
        if has_delta:
            issues.append(
                _issue(
                    ROSTER_INVALID_MUTATION,
                    f"Roster {roster_id} replace must use unresolved_count, not unresolved_delta.",
                    {"roster_id": roster_id, "unresolved_delta": raw_delta},
                )
            )
        if not has_count and existing_unresolved == 0:
            return 0, issues
        if not _nonnegative_int(raw_before):
            issues.append(
                _issue(
                    ROSTER_INVALID_MUTATION,
                    f"Roster {roster_id} aggregate replace requires unresolved_before.",
                    {"roster_id": roster_id, "unresolved_before": raw_before},
                )
            )
        elif int(raw_before) != existing_unresolved:
            issues.append(
                _issue(
                    "roster_state_rollback",
                    f"Roster {roster_id} aggregate replace starts from stale unresolved_count.",
                    {
                        "roster_id": roster_id,
                        "expected_before": existing_unresolved,
                        "declared_before": raw_before,
                    },
                )
            )
        if not _nonnegative_int(raw_count):
            issues.append(
                _issue(
                    ROSTER_INVALID_MUTATION,
                    f"Roster {roster_id} replace requires a non-negative unresolved_count.",
                    {"roster_id": roster_id, "unresolved_count": raw_count},
                )
            )
            return existing_unresolved, issues
        if (
            not _nonnegative_int(raw_before)
            or int(raw_before) != existing_unresolved
            or has_delta
        ):
            return existing_unresolved, issues
        return int(raw_count), issues

    if has_count:
        issues.append(
            _issue(
                ROSTER_INVALID_MUTATION,
                f"Roster {roster_id} {operation} must not set unresolved_count.",
                {"roster_id": roster_id, "unresolved_count": raw_count},
            )
        )
    if operation == "resolve":
        if resolved_member_count < 1:
            issues.append(
                _issue(
                    "roster_resolution_arithmetic_mismatch",
                    f"Roster {roster_id} resolve requires at least one new stable member.",
                    {
                        "roster_id": roster_id,
                        "resolved_member_count": resolved_member_count,
                    },
                )
            )
        if not has_before or not has_delta:
            issues.append(
                _issue(
                    ROSTER_INVALID_MUTATION,
                    f"Roster {roster_id} resolve requires unresolved_before and unresolved_delta.",
                    {
                        "roster_id": roster_id,
                        "unresolved_before": raw_before,
                        "unresolved_delta": raw_delta,
                    },
                )
            )
            return existing_unresolved, issues
        before_matches = (
            _nonnegative_int(raw_before)
            and int(raw_before) == existing_unresolved
        )
        if not before_matches:
            issues.append(
                _issue(
                    "roster_state_rollback",
                    f"Roster {roster_id} resolve starts from stale unresolved_count.",
                    {
                        "roster_id": roster_id,
                        "expected_before": existing_unresolved,
                        "declared_before": raw_before,
                    },
                )
            )
        if not _integer(raw_delta):
            issues.append(
                _issue(
                    ROSTER_INVALID_MUTATION,
                    f"Roster {roster_id} unresolved_delta must be an integer.",
                    {"roster_id": roster_id, "unresolved_delta": raw_delta},
                )
            )
            return existing_unresolved, issues
        expected_unresolved_delta = -resolved_member_count
        if int(raw_delta) != expected_unresolved_delta:
            issues.append(
                _issue(
                    "roster_resolution_arithmetic_mismatch",
                    f"Roster {roster_id} resolve must trade one unresolved person for each new stable member.",
                    {
                        "roster_id": roster_id,
                        "resolved_member_count": resolved_member_count,
                        "declared_unresolved_delta": raw_delta,
                        "expected_unresolved_delta": expected_unresolved_delta,
                    },
                )
            )
        if (
            not before_matches
            or resolved_member_count < 1
            or int(raw_delta) != expected_unresolved_delta
        ):
            return existing_unresolved, issues
        next_unresolved = existing_unresolved + int(raw_delta)
        if next_unresolved < 0:
            issues.append(
                _issue(
                    "roster_count_mismatch",
                    f"Roster {roster_id} cannot resolve more anonymous people than remain.",
                    {
                        "roster_id": roster_id,
                        "unresolved_before": existing_unresolved,
                        "resolved_member_count": resolved_member_count,
                    },
                )
            )
            return existing_unresolved, issues
        return next_unresolved, issues

    if not has_before and not has_delta:
        return existing_unresolved, issues
    if not has_before or not has_delta:
        issues.append(
            _issue(
                ROSTER_INVALID_MUTATION,
                f"Roster {roster_id} aggregate change requires unresolved_before and unresolved_delta.",
                {
                    "roster_id": roster_id,
                    "unresolved_before": raw_before,
                    "unresolved_delta": raw_delta,
                },
            )
        )
        return existing_unresolved, issues
    before_matches = (
        _nonnegative_int(raw_before)
        and int(raw_before) == existing_unresolved
    )
    if not before_matches:
        issues.append(
            _issue(
                "roster_state_rollback",
                f"Roster {roster_id} aggregate change starts from stale unresolved_count.",
                {
                    "roster_id": roster_id,
                    "expected_before": existing_unresolved,
                    "declared_before": raw_before,
                },
            )
        )
    if not _integer(raw_delta):
        issues.append(
            _issue(
                ROSTER_INVALID_MUTATION,
                f"Roster {roster_id} unresolved_delta must be an integer.",
                {"roster_id": roster_id, "unresolved_delta": raw_delta},
            )
        )
        return existing_unresolved, issues
    aggregate_delta = int(raw_delta)
    sign_invalid = (operation == "join" and aggregate_delta < 0) or (
        operation in _REMOVAL_OPERATIONS and aggregate_delta > 0
    )
    if sign_invalid:
        issues.append(
            _issue(
                ROSTER_INVALID_MUTATION,
                f"Roster {roster_id} unresolved_delta has the wrong sign for {operation}.",
                {
                    "roster_id": roster_id,
                    "operation": operation,
                    "unresolved_delta": aggregate_delta,
                },
            )
        )
    if not before_matches or sign_invalid:
        return existing_unresolved, issues
    next_unresolved = existing_unresolved + aggregate_delta
    if next_unresolved < 0:
        issues.append(
            _issue(
                "roster_count_mismatch",
                f"Roster {roster_id} unresolved_count cannot become negative.",
                {
                    "roster_id": roster_id,
                    "unresolved_before": existing_unresolved,
                    "unresolved_delta": aggregate_delta,
                },
            )
        )
        return existing_unresolved, issues
    return next_unresolved, issues


def _resolve_identity(
    rosters: Mapping[str, Any],
    *,
    roster_id: str,
    existing: dict[str, Any],
    incoming: dict[str, Any],
    is_initial: bool,
) -> tuple[str, list[str], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    existing_name = _existing_name(existing)
    incoming_name = str(
        incoming.get("name") or incoming.get("roster_name") or ""
    ).strip()
    canonical_name = incoming_name if is_initial else existing_name
    incoming_declares_name = "name" in incoming or "roster_name" in incoming
    if (
        not is_initial
        and incoming_declares_name
        and incoming_name != existing_name
    ):
        issues.append(
            _issue(
                "roster_identity_drift",
                f"Roster {roster_id} canonical name changed.",
                {
                    "roster_id": roster_id,
                    "before": existing_name,
                    "after": incoming_name,
                },
            )
        )
    existing_aliases = _unique_strings(_string_values(existing.get("aliases")))
    incoming_aliases = _unique_strings(_string_values(incoming.get("aliases")))
    aliases = incoming_aliases if is_initial else existing_aliases
    if (
        not is_initial
        and "aliases" in incoming
        and incoming_aliases != existing_aliases
    ):
        issues.append(
            _issue(
                "roster_identity_drift",
                f"Roster {roster_id} aliases changed.",
                {
                    "roster_id": roster_id,
                    "before": existing_aliases,
                    "after": incoming_aliases,
                },
            )
        )
    owners = _roster_alias_owners(rosters)
    for alias in [roster_id, canonical_name, *aliases]:
        normalized = normalize_roster_alias(alias)
        owner = owners.get(normalized)
        if normalized and owner is not None and owner != roster_id:
            issues.append(
                _issue(
                    "roster_alias_conflict",
                    f"Roster alias {alias!r} already belongs to {owner}.",
                    {
                        "alias": alias,
                        "existing_roster_id": owner,
                        "incoming_roster_id": roster_id,
                    },
                )
            )
    return canonical_name, aliases, issues


def _resolve_audit_metadata(
    *,
    roster_id: str,
    existing: dict[str, Any],
    incoming: dict[str, Any],
    is_initial: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    existing_evidence, existing_evidence_valid = _canonical_baseline_evidence(
        existing.get("baseline_evidence")
    )
    incoming_evidence, incoming_evidence_valid = _canonical_baseline_evidence(
        incoming.get("baseline_evidence")
    )
    if existing.get("baseline_evidence") is not None and not existing_evidence_valid:
        issues.append(
            _issue(
                ROSTER_INVALID_MUTATION,
                f"Roster {roster_id} has invalid baseline_evidence.",
                {
                    "roster_id": roster_id,
                    "baseline_evidence": existing.get("baseline_evidence"),
                },
            )
        )
    if incoming.get("baseline_evidence") is not None and not incoming_evidence_valid:
        issues.append(
            _issue(
                ROSTER_INVALID_MUTATION,
                f"Roster {roster_id} baseline_evidence is incomplete or invalid.",
                {
                    "roster_id": roster_id,
                    "baseline_evidence": incoming.get("baseline_evidence"),
                },
            )
        )
    if is_initial:
        if incoming_evidence_valid:
            metadata["baseline_evidence"] = incoming_evidence
    elif incoming.get("baseline_evidence") is not None:
        if not existing_evidence_valid or incoming_evidence != existing_evidence:
            issues.append(
                _issue(
                    "roster_baseline_evidence_drift",
                    f"Roster {roster_id} baseline evidence changed.",
                    {
                        "roster_id": roster_id,
                        "before": existing.get("baseline_evidence"),
                        "after": incoming.get("baseline_evidence"),
                    },
                )
            )
    if existing_evidence_valid:
        metadata["baseline_evidence"] = existing_evidence

    for field in _AUDIT_FIELDS:
        existing_value = existing.get(field)
        incoming_value = incoming.get(field)
        selected = existing_value if not is_initial else incoming_value
        if not is_initial and incoming_value is not None and incoming_value != existing_value:
            issues.append(
                _issue(
                    "roster_audit_metadata_drift",
                    f"Roster {roster_id} audit field {field} changed.",
                    {
                        "roster_id": roster_id,
                        "field": field,
                        "before": existing_value,
                        "after": incoming_value,
                    },
                )
            )
        if selected is not None:
            metadata[field] = copy.deepcopy(selected)
    introduced_chapter = metadata.get("introduced_chapter")
    if introduced_chapter is not None and (
        not _integer(introduced_chapter) or int(introduced_chapter) < 1
    ):
        issues.append(
            _issue(
                ROSTER_INVALID_MUTATION,
                f"Roster {roster_id} introduced_chapter must be a positive integer.",
                {
                    "roster_id": roster_id,
                    "introduced_chapter": introduced_chapter,
                },
            )
        )
        metadata.pop("introduced_chapter", None)
    return metadata, issues


def _validate_event_references(
    *,
    roster_id: str,
    mutation: dict[str, Any],
    event_index: dict[str, int | None],
    require_current_event: bool,
) -> tuple[bool, list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    primary = str(
        mutation.get("source_event_id") or mutation.get("reason_event_id") or ""
    ).strip()
    referenced = [primary] if primary else []
    for member in mutation.get("members") or []:
        if not isinstance(member, dict):
            continue
        member_event = str(
            member.get("source_event_id")
            or member.get("reason_event_id")
            or member.get("resolved_event_id")
            or member.get("joined_event_id")
            or ""
        ).strip()
        if member_event:
            referenced.append(member_event)
    if require_current_event and not primary:
        issues.append(
            _issue(
                "missing_authority_event_reference",
                f"Roster {roster_id} mutation requires a current Scene event reference.",
                {"ledger": "roster", "roster_id": roster_id, "mutation": mutation},
            )
        )
    mutation_scene_index = mutation.get("scene_index")
    invalid = sorted(
        {
            event_id
            for event_id in referenced
            if event_id not in event_index
            or (
                mutation_scene_index is not None
                and event_index.get(event_id) is not None
                and event_index[event_id] != mutation_scene_index
            )
        }
    )
    if invalid:
        issues.append(
            _issue(
                "invalid_authority_event_reference",
                f"Roster {roster_id} mutation must reference an event from the current Scene.",
                {
                    "ledger": "roster",
                    "roster_id": roster_id,
                    "event_ids": invalid,
                    "scene_index": mutation_scene_index,
                    "current_event_ids": sorted(event_index),
                    "mutation": mutation,
                },
            )
        )
    return bool(primary and primary in event_index and primary not in invalid), issues


def _current_event_index(
    current_events: Mapping[str, Any] | Iterable[str],
) -> dict[str, int | None]:
    if isinstance(current_events, Mapping):
        result: dict[str, int | None] = {}
        for event_id, raw in current_events.items():
            if not str(event_id).strip():
                continue
            scene_index = raw.get("scene_index") if isinstance(raw, dict) else None
            result[str(event_id)] = (
                int(scene_index)
                if _integer(scene_index)
                else None
            )
        return result
    return {
        str(event_id): None
        for event_id in current_events
        if str(event_id).strip()
    }


def _member_map(
    value: Any,
    *,
    roster_id: str,
    source: str,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    if value is None:
        return {}, issues
    if not isinstance(value, list):
        return {}, [
            _issue(
                ROSTER_INVALID_MUTATION,
                f"Roster {roster_id} members must be an array.",
                {"roster_id": roster_id, "source": source, "members": value},
            )
        ]
    members: dict[str, dict[str, Any]] = {}
    for raw in value:
        if not isinstance(raw, dict):
            issues.append(
                _issue(
                    ROSTER_INVALID_MUTATION,
                    f"Roster {roster_id} member records must be objects.",
                    {"roster_id": roster_id, "source": source, "member": raw},
                )
            )
            continue
        member_id = str(raw.get("member_id") or "").strip()
        if not member_id:
            issues.append(
                _issue(
                    ROSTER_INVALID_MUTATION,
                    f"Roster {roster_id} member records require stable member_id values.",
                    {"roster_id": roster_id, "source": source, "member": raw},
                )
            )
            continue
        if member_id in members:
            issues.append(
                _issue(
                    ROSTER_INVALID_MUTATION,
                    f"Roster {roster_id} repeats member_id {member_id}.",
                    {"roster_id": roster_id, "source": source, "member_id": member_id},
                )
            )
            continue
        members[member_id] = copy.deepcopy(raw)
        members[member_id]["member_id"] = member_id
    return members, issues


def _member_id_list(
    value: Any,
    *,
    roster_id: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], [
            _issue(
                ROSTER_INVALID_MUTATION,
                f"Roster {roster_id} member_ids must be an array.",
                {"roster_id": roster_id, "member_ids": value},
            )
        ]
    values = [str(item).strip() for item in value if str(item).strip()]
    if len(values) != len(set(values)):
        return list(dict.fromkeys(values)), [
            _issue(
                ROSTER_INVALID_MUTATION,
                f"Roster {roster_id} member_ids must be unique.",
                {"roster_id": roster_id, "member_ids": values},
            )
        ]
    return values, []


def _member_identity_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return any(
        before.get(field) not in (None, "")
        and after.get(field) not in (None, "")
        and before.get(field) != after.get(field)
        for field in ("character_id", "descriptor")
    )


def _canonical_baseline_evidence(value: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, dict):
        return {}, False
    source_kind = str(value.get("source_kind") or "").strip()
    source_path = str(value.get("source_path") or "").strip()
    sha256 = str(value.get("sha256") or "").strip()
    if not source_kind or not source_path or _BASELINE_SHA256.fullmatch(sha256) is None:
        return {}, False
    return {
        **copy.deepcopy(value),
        "source_kind": source_kind,
        "source_path": source_path,
        "sha256": sha256.lower(),
    }, True


def _has_audited_baseline(metadata: dict[str, Any]) -> bool:
    _, valid = _canonical_baseline_evidence(metadata.get("baseline_evidence"))
    return valid and _integer(metadata.get("introduced_chapter")) and int(
        metadata["introduced_chapter"]
    ) > 0


def _existing_audit_metadata(existing: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    evidence, valid = _canonical_baseline_evidence(existing.get("baseline_evidence"))
    if valid:
        result["baseline_evidence"] = evidence
    for field in _AUDIT_FIELDS:
        if existing.get(field) is not None:
            result[field] = copy.deepcopy(existing[field])
    return result


def _existing_name(existing: dict[str, Any]) -> str:
    return str(existing.get("name") or existing.get("roster_name") or "").strip()


def _roster_alias_owners(rosters: Mapping[str, Any]) -> dict[str, str]:
    owners: dict[str, str] = {}
    for key, raw in rosters.items():
        if not isinstance(raw, dict):
            continue
        roster_id = str(raw.get("roster_id") or key).strip()
        for value in (
            roster_id,
            raw.get("name"),
            raw.get("roster_name"),
            *_string_values(raw.get("aliases")),
        ):
            normalized = normalize_roster_alias(value)
            if normalized:
                owners.setdefault(normalized, roster_id)
    return owners


def normalize_roster_alias(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _unique_strings(value: Any) -> list[str]:
    result: list[str] = []
    for item in value if isinstance(value, list) else []:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonnegative_int(value: Any) -> bool:
    return _integer(value) and value >= 0


def _issue(code: str, message: str, evidence: Any) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "evidence": copy.deepcopy(evidence),
    }


__all__ = [
    "ROSTER_GENERATION_OMITTED_FIELDS",
    "ROSTER_GENERATION_PROJECTION_POLICY",
    "ROSTER_INVALID_MUTATION",
    "normalize_roster_alias",
    "project_roster_for_generation",
    "reduce_roster_mutation",
]
