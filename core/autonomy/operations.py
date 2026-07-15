from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

from core.autonomy.common import (
    AutonomyContractError,
    atomic_append_json,
    canonical_hash,
    load_json_object,
    parse_utc,
    positive_int,
    required_text,
    safe_id,
    sha256_digest,
    validate_mapping,
)
from core.engine.recovery_protocol import (
    MARKER_RECOVERY_PROTOCOL,
    RecoveryDecision,
    RecoveryProtocolError,
    build_marker_envelope,
    decide_marker_recovery,
    reconcile_marker_transaction,
    validate_marker_envelope,
)


class AutonomyOperationError(AutonomyContractError):
    pass


_T = TypeVar("_T")


def build_operation_intent(
    *,
    operation_type: str,
    session_id: str,
    book_id: str,
    plan_id: str,
    plan_hash: str,
    expected_state: str,
    expected_event_hash: str | None,
    expected_lease_hash: str | None,
    target_event_type: str | None,
    reason: str | None,
    lease_ttl_seconds: int | None,
    attempt: int,
    created_at: str,
) -> dict[str, Any]:
    key_payload = {
        "operation_type": str(operation_type),
        "session_id": safe_id("session_id", session_id),
        "book_id": safe_id("book_id", book_id),
        "plan_id": safe_id("plan_id", plan_id),
        "plan_hash": sha256_digest("plan_hash", plan_hash),
        "expected_state": str(expected_state),
        "expected_event_hash": sha256_digest(
            "expected_event_hash", expected_event_hash, optional=True
        ),
        "expected_lease_hash": sha256_digest(
            "expected_lease_hash", expected_lease_hash, optional=True
        ),
        "target_event_type": (
            str(target_event_type) if target_event_type is not None else None
        ),
        "reason": required_text("reason", reason) if reason is not None else None,
        "lease_ttl_seconds": (
            positive_int("lease_ttl_seconds", lease_ttl_seconds)
            if lease_ttl_seconds is not None
            else None
        ),
    }
    operation_key = canonical_hash(key_payload)
    ordinal = positive_int("attempt", attempt)
    operation_id = (
        f"op_{canonical_hash({'operation_key': operation_key, 'attempt': ordinal})[:24]}"
    )
    intent = {
        "schema_version": "1.1",
        "recovery_protocol": MARKER_RECOVERY_PROTOCOL,
        "operation_id": operation_id,
        "operation_key": operation_key,
        "attempt": ordinal,
        **key_payload,
        "created_at": required_text("created_at", created_at),
    }
    intent["intent_hash"] = canonical_hash(intent, exclude_fields=("intent_hash",))
    return validate_operation_intent(intent)


def validate_operation_intent(value: Any) -> dict[str, Any]:
    intent = validate_mapping(
        value, "autonomy_operation_intent.schema.json", "AutonomyOperationIntent"
    )
    for field in ("operation_id", "session_id", "book_id", "plan_id"):
        safe_id(field, intent[field])
    if intent["schema_version"] == "1.1":
        if intent.get("recovery_protocol") != MARKER_RECOVERY_PROTOCOL:
            raise AutonomyOperationError(
                "autonomy_operation_recovery_protocol_invalid",
                "new operation intents must use the shared marker recovery protocol",
            )
    elif "recovery_protocol" in intent:
        raise AutonomyOperationError(
            "autonomy_operation_recovery_protocol_invalid",
            "legacy operation intents cannot claim a newer recovery protocol",
        )
    for field in ("operation_key", "plan_hash", "intent_hash"):
        sha256_digest(field, intent[field])
    sha256_digest("expected_event_hash", intent["expected_event_hash"], optional=True)
    sha256_digest("expected_lease_hash", intent["expected_lease_hash"], optional=True)
    positive_int("attempt", intent["attempt"])
    if intent["lease_ttl_seconds"] is not None:
        positive_int("lease_ttl_seconds", intent["lease_ttl_seconds"])
    parse_utc(intent["created_at"])
    combination = (
        intent["operation_type"],
        intent["expected_state"],
        intent["target_event_type"],
    )
    allowed = {
        ("execute", "absent", "started"),
        ("execute", "active", None),
        ("resume", "active", None),
        ("resume", "cancelled", "resumed"),
        ("cancel", "active", "cancelled"),
        ("abandon", "active", "abandoned"),
        ("abandon", "cancelled", "abandoned"),
        ("complete", "active", "completed"),
    }
    if combination not in allowed:
        raise AutonomyOperationError(
            "autonomy_operation_transition_invalid",
            "operation type, pre-state, and target event are inconsistent",
        )
    source_guarded = intent["operation_type"] in {"execute", "resume"}
    if source_guarded != (intent["lease_ttl_seconds"] is not None):
        raise AutonomyOperationError(
            "autonomy_operation_lease_policy_invalid",
            "only source-guarded operations carry a lease TTL",
        )
    if (intent["expected_state"] == "absent") != (
        intent["expected_event_hash"] is None
    ):
        raise AutonomyOperationError(
            "autonomy_operation_event_precondition_invalid",
            "only an absent session may omit its expected event hash",
        )
    key_payload = {
        field: intent[field]
        for field in (
            "operation_type",
            "session_id",
            "book_id",
            "plan_id",
            "plan_hash",
            "expected_state",
            "expected_event_hash",
            "expected_lease_hash",
            "target_event_type",
            "reason",
            "lease_ttl_seconds",
        )
    }
    if intent["operation_key"] != canonical_hash(key_payload):
        raise AutonomyOperationError(
            "autonomy_operation_key_mismatch", "operation preconditions were modified"
        )
    expected_id = (
        "op_"
        + canonical_hash(
            {"operation_key": intent["operation_key"], "attempt": intent["attempt"]}
        )[:24]
    )
    if intent["operation_id"] != expected_id:
        raise AutonomyOperationError(
            "autonomy_operation_id_mismatch", "operation id is not deterministic"
        )
    if intent["intent_hash"] != canonical_hash(intent, exclude_fields=("intent_hash",)):
        raise AutonomyOperationError(
            "autonomy_operation_intent_hash_mismatch", "operation intent was modified"
        )
    return intent


def build_source_verification(
    intent: Mapping[str, Any],
    *,
    source_snapshot_hash: str,
    lease_hash: str,
    verified_at: str,
) -> dict[str, Any]:
    operation = validate_operation_intent(intent)
    verification = {
        "schema_version": "1.0",
        "operation_id": operation["operation_id"],
        "intent_hash": operation["intent_hash"],
        "source_snapshot_hash": sha256_digest(
            "source_snapshot_hash", source_snapshot_hash
        ),
        "lease_hash": sha256_digest("lease_hash", lease_hash),
        "verified_at": required_text("verified_at", verified_at),
    }
    verification["verification_hash"] = canonical_hash(
        verification, exclude_fields=("verification_hash",)
    )
    return validate_source_verification(verification)


def validate_source_verification(value: Any) -> dict[str, Any]:
    verification = validate_mapping(
        value,
        "autonomy_operation_source_verified.schema.json",
        "AutonomyOperationSourceVerified",
    )
    safe_id("operation_id", verification["operation_id"])
    for field in (
        "intent_hash",
        "source_snapshot_hash",
        "lease_hash",
        "verification_hash",
    ):
        sha256_digest(field, verification[field])
    parse_utc(verification["verified_at"])
    if verification["verification_hash"] != canonical_hash(
        verification, exclude_fields=("verification_hash",)
    ):
        raise AutonomyOperationError(
            "autonomy_operation_verification_hash_mismatch",
            "source verification marker was modified",
        )
    return verification


def build_operation_result(
    intent: Mapping[str, Any],
    *,
    outcome: str,
    event_hash: str | None,
    lease_hash: str | None,
    completed_at: str,
) -> dict[str, Any]:
    operation = validate_operation_intent(intent)
    result = {
        "schema_version": "1.0",
        "operation_id": operation["operation_id"],
        "intent_hash": operation["intent_hash"],
        "outcome": str(outcome),
        "event_hash": sha256_digest("event_hash", event_hash, optional=True),
        "lease_hash": sha256_digest("lease_hash", lease_hash, optional=True),
        "completed_at": required_text("completed_at", completed_at),
    }
    result["result_hash"] = canonical_hash(result, exclude_fields=("result_hash",))
    return validate_operation_result(result)


def validate_operation_result(value: Any) -> dict[str, Any]:
    result = validate_mapping(
        value, "autonomy_operation_result.schema.json", "AutonomyOperationResult"
    )
    safe_id("operation_id", result["operation_id"])
    for field in ("intent_hash", "result_hash"):
        sha256_digest(field, result[field])
    sha256_digest("event_hash", result["event_hash"], optional=True)
    sha256_digest("lease_hash", result["lease_hash"], optional=True)
    parse_utc(result["completed_at"])
    if (result["outcome"] == "completed") != (result["event_hash"] is not None):
        raise AutonomyOperationError(
            "autonomy_operation_result_invalid",
            "completed operations require an event hash and rollbacks must omit it",
        )
    if result["result_hash"] != canonical_hash(result, exclude_fields=("result_hash",)):
        raise AutonomyOperationError(
            "autonomy_operation_result_hash_mismatch", "operation result was modified"
        )
    return result


def build_operation_commit_marker(
    intent: Mapping[str, Any], *, evidence_kind: str, evidence_hash: str
) -> dict[str, Any]:
    operation = validate_operation_intent(intent)
    if operation["schema_version"] != "1.1":
        raise AutonomyOperationError(
            "autonomy_operation_recovery_protocol_invalid",
            "legacy operation intents do not write shared commit markers",
        )
    if evidence_kind not in {"source_verified", "terminal_preconditions"}:
        raise AutonomyOperationError(
            "autonomy_operation_marker_invalid", "unknown operation marker evidence"
        )
    return build_marker_envelope(
        transaction_id=operation["operation_id"],
        intent_hash=operation["intent_hash"],
        evidence_kind=evidence_kind,
        evidence_hash=sha256_digest("evidence_hash", evidence_hash),
        metadata={"operation_type": operation["operation_type"]},
    )


def validate_operation_commit_marker(value: Any) -> dict[str, Any]:
    try:
        marker = validate_marker_envelope(value)
    except RecoveryProtocolError as exc:
        raise AutonomyOperationError(
            "autonomy_operation_marker_invalid", str(exc)
        ) from exc
    if marker.get("evidence_kind") not in {
        "source_verified",
        "terminal_preconditions",
    }:
        raise AutonomyOperationError(
            "autonomy_operation_marker_invalid", "operation marker evidence kind changed"
        )
    metadata = marker.get("metadata")
    if not isinstance(metadata, Mapping) or set(metadata) != {"operation_type"}:
        raise AutonomyOperationError(
            "autonomy_operation_marker_invalid", "operation marker metadata is malformed"
        )
    return marker


class AutonomyOperationStore:
    """Append-only recovery journal for cross-artifact autonomy transitions."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def begin(self, **kwargs: Any) -> dict[str, Any]:
        prototype = build_operation_intent(attempt=1, **kwargs)
        matching = [
            item
            for item in self.list_intents()
            if item["operation_key"] == prototype["operation_key"]
        ]
        pending = [item for item in matching if self.result(item) is None]
        if len(pending) > 1:
            raise AutonomyOperationError(
                "autonomy_operation_multiple_pending",
                "more than one operation attempt has the same preconditions",
            )
        if pending:
            return pending[0]
        attempt = max((int(item["attempt"]) for item in matching), default=0) + 1
        intent = build_operation_intent(attempt=attempt, **kwargs)
        atomic_append_json(self._directory(intent) / "intent.json", intent)
        return intent

    def list_intents(self) -> list[dict[str, Any]]:
        intents: list[dict[str, Any]] = []
        operations_root = self.root / "operations"
        if not operations_root.exists():
            return intents
        for directory in sorted(
            path for path in operations_root.iterdir() if path.is_dir()
        ):
            intent_path = directory / "intent.json"
            if not intent_path.is_file():
                raise AutonomyOperationError(
                    "autonomy_operation_intent_missing",
                    f"operation directory has no intent: {directory.name}",
                )
            intent = validate_operation_intent(load_json_object(intent_path))
            if directory.name != intent["operation_id"]:
                raise AutonomyOperationError(
                    "autonomy_operation_path_mismatch",
                    "operation directory does not match its deterministic id",
                )
            result = self.result(intent)
            if result is not None and result["intent_hash"] != intent["intent_hash"]:
                raise AutonomyOperationError(
                    "autonomy_operation_result_scope_mismatch",
                    "operation result belongs to another intent",
                )
            verification = self.source_verification(intent)
            if (
                verification is not None
                and verification["intent_hash"] != intent["intent_hash"]
            ):
                raise AutonomyOperationError(
                    "autonomy_operation_verification_scope_mismatch",
                    "source marker belongs to another intent",
                )
            if verification is not None and intent["operation_type"] not in {
                "execute",
                "resume",
            }:
                raise AutonomyOperationError(
                    "autonomy_operation_verification_invalid",
                    "terminal operations cannot carry source verification markers",
                )
            if (
                result is not None
                and result["outcome"] == "rolled_back"
                and intent["schema_version"] == "1.0"
                and intent["operation_type"] not in {"execute", "resume"}
            ):
                raise AutonomyOperationError(
                    "autonomy_operation_result_invalid",
                    "legacy terminal operation intents cannot be rolled back",
                )
            intents.append(intent)
        return intents

    def pending(self) -> list[dict[str, Any]]:
        return [intent for intent in self.list_intents() if self.result(intent) is None]

    def mark_source_verified(
        self,
        intent: Mapping[str, Any],
        *,
        source_snapshot_hash: str,
        lease_hash: str,
        verified_at: str,
    ) -> dict[str, Any]:
        marker = build_source_verification(
            intent,
            source_snapshot_hash=source_snapshot_hash,
            lease_hash=lease_hash,
            verified_at=verified_at,
        )
        directory = self._directory(intent)
        atomic_append_json(directory / "source-verified.json", marker)
        operation = validate_operation_intent(intent)
        if operation["schema_version"] == "1.1":
            commit_marker = build_operation_commit_marker(
                operation,
                evidence_kind="source_verified",
                evidence_hash=marker["verification_hash"],
            )
            atomic_append_json(directory / "commit.marker", commit_marker)
        return marker

    def mark_terminal_ready(self, intent: Mapping[str, Any]) -> dict[str, Any] | None:
        operation = validate_operation_intent(intent)
        if operation["operation_type"] in {"execute", "resume"}:
            raise AutonomyOperationError(
                "autonomy_operation_marker_invalid",
                "source-guarded operations require source verification evidence",
            )
        if operation["schema_version"] == "1.0":
            return None
        marker = build_operation_commit_marker(
            operation,
            evidence_kind="terminal_preconditions",
            evidence_hash=operation["intent_hash"],
        )
        atomic_append_json(self._directory(operation) / "commit.marker", marker)
        return marker

    def source_verification(
        self, intent: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        operation = validate_operation_intent(intent)
        path = self._directory(operation) / "source-verified.json"
        if operation["schema_version"] == "1.1":
            commit = self.commit_marker(operation)
            if commit is None:
                return None
            if commit["evidence_kind"] != "source_verified":
                return None
        if not path.is_file():
            return None
        marker = validate_source_verification(load_json_object(path))
        if marker["operation_id"] != operation["operation_id"]:
            raise AutonomyOperationError(
                "autonomy_operation_verification_scope_mismatch",
                "source marker belongs to another operation",
            )
        if operation["schema_version"] == "1.1" and commit[
            "evidence_hash"
        ] != marker["verification_hash"]:
            raise AutonomyOperationError(
                "autonomy_operation_marker_evidence_mismatch",
                "commit marker does not bind the source verification evidence",
            )
        return marker

    def commit_marker(self, intent: Mapping[str, Any]) -> dict[str, Any] | None:
        operation = validate_operation_intent(intent)
        path = self._directory(operation) / "commit.marker"
        if not path.is_file():
            return None
        marker = validate_operation_commit_marker(load_json_object(path))
        if (
            marker["transaction_id"] != operation["operation_id"]
            or marker["intent_hash"] != operation["intent_hash"]
            or marker["metadata"].get("operation_type")
            != operation["operation_type"]
        ):
            raise AutonomyOperationError(
                "autonomy_operation_marker_scope_mismatch",
                "commit marker belongs to another operation intent",
            )
        return marker

    def recovery_decision(self, intent: Mapping[str, Any]) -> RecoveryDecision:
        operation = validate_operation_intent(intent)
        result = self._load_result(operation)
        return self._recovery_decision_for(operation, result)

    def _recovery_decision_for(
        self,
        operation: Mapping[str, Any],
        result: Mapping[str, Any] | None,
    ) -> RecoveryDecision:
        marker_present = self._marker_present(operation)
        if result is not None and result["outcome"] == "rolled_back":
            if marker_present:
                raise AutonomyOperationError(
                    "autonomy_operation_result_invalid",
                    "rolled-back operation unexpectedly has a commit marker",
                )
            return RecoveryDecision.ROLL_BACK
        try:
            return decide_marker_recovery(
                marker_present=marker_present,
                completion_present=result is not None,
            )
        except RecoveryProtocolError as exc:
            raise AutonomyOperationError(
                "autonomy_operation_result_invalid", str(exc)
            ) from exc

    def reconcile_pending(
        self,
        intent: Mapping[str, Any],
        *,
        on_roll_back: Callable[[], _T],
        on_roll_forward: Callable[[], _T],
    ) -> _T:
        operation = validate_operation_intent(intent)
        if self.result(operation) is not None:
            raise AutonomyOperationError(
                "autonomy_operation_not_pending",
                "only a pending operation can enter recovery",
            )
        return reconcile_marker_transaction(
            marker_present=self._marker_present(operation),
            completion_present=False,
            on_roll_back=on_roll_back,
            on_roll_forward=on_roll_forward,
            on_completed=lambda: self._reject_pending_completion(),
        )

    @staticmethod
    def _reject_pending_completion() -> Any:
        raise AutonomyOperationError(
            "autonomy_operation_not_pending",
            "pending operation unexpectedly entered completed recovery",
        )

    def _marker_present(self, operation: Mapping[str, Any]) -> bool:
        if operation["schema_version"] == "1.1":
            marker = self.commit_marker(operation)
            marker_present = marker is not None
            if marker is not None:
                source_guarded = operation["operation_type"] in {"execute", "resume"}
                expected_kind = (
                    "source_verified" if source_guarded else "terminal_preconditions"
                )
                expected_evidence = (
                    None if source_guarded else operation["intent_hash"]
                )
                if marker["evidence_kind"] != expected_kind or (
                    expected_evidence is not None
                    and marker["evidence_hash"] != expected_evidence
                ):
                    raise AutonomyOperationError(
                        "autonomy_operation_marker_evidence_mismatch",
                        "commit marker carries the wrong transition evidence",
                    )
                if source_guarded and self.source_verification(operation) is None:
                    raise AutonomyOperationError(
                        "autonomy_operation_marker_evidence_missing",
                        "source operation marker has no bound verification evidence",
                    )
            return marker_present
        if operation["operation_type"] in {"execute", "resume"}:
            marker_present = (
                self._directory(operation) / "source-verified.json"
            ).is_file()
            return marker_present
        # v1.0 terminal operations mutated immediately after their intent;
        # retain that historical roll-forward interpretation on read.
        return True

    def finish(
        self,
        intent: Mapping[str, Any],
        *,
        outcome: str,
        event_hash: str | None,
        lease_hash: str | None,
        completed_at: str,
    ) -> dict[str, Any]:
        operation = validate_operation_intent(intent)
        candidate = build_operation_result(
            operation,
            outcome=outcome,
            event_hash=event_hash,
            lease_hash=lease_hash,
            completed_at=completed_at,
        )
        existing = self.result(operation)
        if existing is not None:
            if existing != candidate:
                raise AutonomyOperationError(
                    "autonomy_operation_result_conflict",
                    "completed operation replay produced different result bytes",
                )
            return existing
        decision = self.recovery_decision(operation)
        if outcome == "completed" and decision is not RecoveryDecision.ROLL_FORWARD:
            raise AutonomyOperationError(
                "autonomy_operation_marker_missing",
                "operation cannot complete before its shared commit marker",
            )
        if outcome == "rolled_back" and decision is not RecoveryDecision.ROLL_BACK:
            raise AutonomyOperationError(
                "autonomy_operation_rollback_forbidden",
                "operation cannot roll back after its shared commit marker",
            )
        result = candidate
        atomic_append_json(self._directory(intent) / "result.json", result)
        return result

    def result(self, intent: Mapping[str, Any]) -> dict[str, Any] | None:
        operation = validate_operation_intent(intent)
        result = self._load_result(operation)
        if result is not None and operation["schema_version"] == "1.1":
            self._recovery_decision_for(operation, result)
        return result

    def _load_result(
        self, operation: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        path = self._directory(operation) / "result.json"
        if not path.is_file():
            return None
        result = validate_operation_result(load_json_object(path))
        if (
            result["operation_id"] != operation["operation_id"]
            or result["intent_hash"] != operation["intent_hash"]
        ):
            raise AutonomyOperationError(
                "autonomy_operation_result_scope_mismatch",
                "result belongs to another operation",
            )
        return result

    def _directory(self, intent: Mapping[str, Any]) -> Path:
        operation = validate_operation_intent(intent)
        return self.root / "operations" / operation["operation_id"]


__all__ = [
    "AutonomyOperationError",
    "AutonomyOperationStore",
    "build_operation_intent",
    "build_operation_result",
    "build_operation_commit_marker",
    "build_source_verification",
    "validate_operation_intent",
    "validate_operation_result",
    "validate_operation_commit_marker",
    "validate_source_verification",
]
