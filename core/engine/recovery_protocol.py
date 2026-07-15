from __future__ import annotations

from enum import Enum
import re
from typing import Any, Callable, Mapping, TypeVar

from core.memory_v2.canonical import canonical_json_hash


MARKER_RECOVERY_PROTOCOL = "commit-marker-forward-v1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_T = TypeVar("_T")


class RecoveryProtocolError(RuntimeError):
    pass


class RecoveryDecision(str, Enum):
    """Legal recovery outcomes shared by canonical and checkpoint writers."""

    ROLL_BACK = "roll_back"
    ROLL_FORWARD = "roll_forward"
    COMPLETED = "completed"


def decide_marker_recovery(
    *, marker_present: bool, completion_present: bool
) -> RecoveryDecision:
    """Use one commit boundary for every durable local write.

    Before the marker, staged work is disposable.  After the marker, recovery
    must finish publication.  A completion without its marker is unverifiable.
    """

    marker = bool(marker_present)
    completion = bool(completion_present)
    if completion and not marker:
        raise RecoveryProtocolError(
            "recovery completion exists without its durable commit marker"
        )
    if completion:
        return RecoveryDecision.COMPLETED
    if marker:
        return RecoveryDecision.ROLL_FORWARD
    return RecoveryDecision.ROLL_BACK


def build_marker_envelope(
    *,
    transaction_id: str,
    intent_hash: str,
    evidence_kind: str,
    evidence_hash: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the common tamper-evident marker written by checkpoint writers."""

    marker = {
        "schema_version": "1.0",
        "recovery_protocol": MARKER_RECOVERY_PROTOCOL,
        "transaction_id": str(transaction_id),
        "intent_hash": str(intent_hash),
        "evidence_kind": str(evidence_kind),
        "evidence_hash": str(evidence_hash),
        "metadata": dict(metadata or {}),
    }
    marker["marker_hash"] = canonical_json_hash(
        marker, exclude_fields=("marker_hash",)
    )
    return validate_marker_envelope(marker)


def validate_marker_envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RecoveryProtocolError("recovery marker must be an object")
    marker = dict(value)
    required = {
        "schema_version",
        "recovery_protocol",
        "transaction_id",
        "intent_hash",
        "evidence_kind",
        "evidence_hash",
        "metadata",
        "marker_hash",
    }
    if set(marker) != required or marker.get("schema_version") != "1.0":
        raise RecoveryProtocolError("recovery marker envelope is malformed")
    if marker.get("recovery_protocol") != MARKER_RECOVERY_PROTOCOL:
        raise RecoveryProtocolError("recovery marker protocol changed")
    if not isinstance(marker.get("transaction_id"), str) or _IDENTIFIER.fullmatch(
        marker["transaction_id"]
    ) is None:
        raise RecoveryProtocolError("recovery marker transaction id is invalid")
    if not isinstance(marker.get("evidence_kind"), str) or _IDENTIFIER.fullmatch(
        marker["evidence_kind"]
    ) is None:
        raise RecoveryProtocolError("recovery marker evidence kind is invalid")
    for field in ("intent_hash", "evidence_hash", "marker_hash"):
        if not isinstance(marker.get(field), str) or _SHA256.fullmatch(marker[field]) is None:
            raise RecoveryProtocolError(f"recovery marker {field} is invalid")
    if not isinstance(marker.get("metadata"), Mapping):
        raise RecoveryProtocolError("recovery marker metadata must be an object")
    marker["metadata"] = dict(marker["metadata"])
    expected = canonical_json_hash(marker, exclude_fields=("marker_hash",))
    if marker["marker_hash"] != expected:
        raise RecoveryProtocolError("recovery marker hash mismatch")
    return marker


def reconcile_marker_transaction(
    *,
    marker_present: bool,
    completion_present: bool,
    on_roll_back: Callable[[], _T],
    on_roll_forward: Callable[[], _T],
    on_completed: Callable[[], _T],
) -> _T:
    """Execute the common recovery transition, not only classify it."""

    decision = decide_marker_recovery(
        marker_present=marker_present,
        completion_present=completion_present,
    )
    if decision is RecoveryDecision.ROLL_BACK:
        return on_roll_back()
    if decision is RecoveryDecision.ROLL_FORWARD:
        return on_roll_forward()
    return on_completed()


__all__ = [
    "MARKER_RECOVERY_PROTOCOL",
    "RecoveryDecision",
    "RecoveryProtocolError",
    "build_marker_envelope",
    "decide_marker_recovery",
    "reconcile_marker_transaction",
    "validate_marker_envelope",
]
