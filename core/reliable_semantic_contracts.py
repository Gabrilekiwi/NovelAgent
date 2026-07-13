from __future__ import annotations

from collections.abc import Iterable, Mapping


LOCAL_COMMIT_STATES = frozenset(
    {
        "new",
        "prepared",
        "applying",
        "commit_marked",
        "publishing",
        "completed",
        "rolling_back",
        "rolled_back",
        "recovery_required",
    }
)

LOCAL_COMMIT_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "new": frozenset({"prepared"}),
    "prepared": frozenset({"applying", "rolling_back"}),
    "applying": frozenset({"commit_marked", "rolling_back"}),
    "commit_marked": frozenset({"publishing"}),
    "publishing": frozenset({"completed", "recovery_required"}),
    "completed": frozenset(),
    "rolling_back": frozenset({"rolled_back", "recovery_required"}),
    "rolled_back": frozenset(),
    "recovery_required": frozenset({"rolling_back", "publishing"}),
}

DELIVERY_STATES = frozenset(
    {
        "not_required",
        "pending",
        "delivering",
        "succeeded",
        "retryable_failed",
        "permanent_failed",
        "uncertain",
        "conflict",
        "cancelled",
    }
)

HASH_DEPENDENCIES: Mapping[str, frozenset[str]] = {
    "context_digest": frozenset({"read_set"}),
    "candidate_digest": frozenset({"context_digest"}),
    "artifact_bundle_digest": frozenset({"staged_targets"}),
    "final_run_hash": frozenset({"final_run_bytes"}),
    "commit_marker_hash": frozenset(
        {
            "manifest_digest",
            "candidate_digest",
            "artifact_bundle_digest",
            "final_run_hash",
        }
    ),
    "publication_receipt_hash": frozenset(
        {"commit_marker_hash", "final_run_hash", "artifact_bundle_digest"}
    ),
    "delivery_attempt_receipt_hash": frozenset(
        {"publication_receipt_hash", "delivery_payload_hash", "delivery_attempt"}
    ),
}


def is_valid_local_commit_transition(
    current: str,
    target: str,
    *,
    commit_marker_valid: bool | None = None,
) -> bool:
    if current not in LOCAL_COMMIT_STATES or target not in LOCAL_COMMIT_STATES:
        return False
    if target not in LOCAL_COMMIT_TRANSITIONS[current]:
        return False
    if current == "recovery_required" and target == "rolling_back":
        return commit_marker_valid is False
    if current == "recovery_required" and target == "publishing":
        return commit_marker_valid is True
    return True


def validate_hash_dependency_dag(
    dependencies: Mapping[str, Iterable[str]] = HASH_DEPENDENCIES,
) -> tuple[str, ...]:
    visiting: set[str] = set()
    visited: set[str] = set()
    order: list[str] = []

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ValueError(f"hash dependency cycle detected at {node}")
        visiting.add(node)
        for dependency in sorted(dependencies.get(node, ())):
            if dependency in dependencies:
                visit(dependency)
        visiting.remove(node)
        visited.add(node)
        order.append(node)

    for node in sorted(dependencies):
        visit(node)
    return tuple(order)


def readiness_failure_reasons(
    *,
    accepted: bool,
    committed: bool,
    project_identity_matches: bool,
    required_delivery_states: Iterable[str],
    next_context_valid: bool,
    read_set_unchanged: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not accepted:
        reasons.append("quality_not_accepted")
    if not committed:
        reasons.append("local_commit_not_verified")
    if not project_identity_matches:
        reasons.append("project_identity_mismatch")

    for state in required_delivery_states:
        if state not in DELIVERY_STATES:
            raise ValueError(f"unknown delivery state: {state}")
        if state != "succeeded":
            reasons.append(f"required_delivery_not_succeeded:{state}")

    if not next_context_valid:
        reasons.append("next_step_context_invalid")
    if not read_set_unchanged:
        reasons.append("next_step_context_drift")
    return tuple(dict.fromkeys(reasons))


def ready_for_next_step(
    *,
    accepted: bool,
    committed: bool,
    project_identity_matches: bool,
    required_delivery_states: Iterable[str],
    next_context_valid: bool,
    read_set_unchanged: bool,
) -> bool:
    return not readiness_failure_reasons(
        accepted=accepted,
        committed=committed,
        project_identity_matches=project_identity_matches,
        required_delivery_states=required_delivery_states,
        next_context_valid=next_context_valid,
        read_set_unchanged=read_set_unchanged,
    )


validate_hash_dependency_dag()


__all__ = [
    "DELIVERY_STATES",
    "HASH_DEPENDENCIES",
    "LOCAL_COMMIT_STATES",
    "LOCAL_COMMIT_TRANSITIONS",
    "is_valid_local_commit_transition",
    "readiness_failure_reasons",
    "ready_for_next_step",
    "validate_hash_dependency_dag",
]
