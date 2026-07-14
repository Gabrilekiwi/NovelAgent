from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from core.memory_v2.canonical import canonical_json_hash
from core.schema import SchemaValidationError, validate_schema


_STAGES = frozenset({"outline", "scene_plan", "draft", "polish", "validator", "repair"})
_RECEIPT_STATUSES = frozenset(
    {"succeeded", "failed", "provider_call_uncertain", "budget_rejected", "cancelled"}
)


class StageControlError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def derive_outline_readiness(
    *,
    book_id: str,
    expected_book_id: str,
    requested_chapter: int,
    canonical_next_chapter: int,
    authority_epoch: int,
    authority_head_event_hash: str | None,
    context_digest: str,
    book_lease_held: bool,
    required_delivery_allows_progress: bool,
    sources_current: bool,
    outline_exists: bool,
    checked_at: str | None = None,
) -> dict[str, Any]:
    """Authorize outline planning without requiring an outline to exist already."""

    resolved_book_id = _required_text("book_id", book_id)
    identity_matches = resolved_book_id == _required_text("expected_book_id", expected_book_id)
    chapter_matches = _chapter("requested_chapter", requested_chapter) == _chapter(
        "canonical_next_chapter", canonical_next_chapter
    )
    evidence = {
        "project_identity_matches": identity_matches,
        "canonical_next_chapter_matches": chapter_matches,
        "book_lease_held": _boolean("book_lease_held", book_lease_held),
        "required_delivery_allows_progress": _boolean(
            "required_delivery_allows_progress", required_delivery_allows_progress
        ),
        "sources_current": _boolean("sources_current", sources_current),
        # Deliberately evidence-only: a missing outline is valid at this stage.
        "outline_exists": _boolean("outline_exists", outline_exists),
    }
    reasons: list[str] = []
    for field, code in (
        ("project_identity_matches", "project_identity_mismatch"),
        ("canonical_next_chapter_matches", "requested_chapter_not_canonical_next"),
        ("book_lease_held", "book_lease_missing"),
        ("required_delivery_allows_progress", "required_delivery_blocked"),
        ("sources_current", "outline_source_drift"),
    ):
        if not evidence[field]:
            reasons.append(code)
    decision = {
        "schema_version": "1.0",
        "kind": "outline",
        "ok": not reasons,
        "reasons": reasons,
        "book_id": resolved_book_id,
        "chapter_index": requested_chapter,
        "authority": _authority(authority_epoch, authority_head_event_hash),
        "context_digest": _sha256("context_digest", context_digest),
        "checked_at": checked_at or _now(),
        "evidence": evidence,
    }
    return validate_outline_readiness(decision)


def validate_outline_readiness(value: Any) -> dict[str, Any]:
    decision = _validate_mapping(value, "outline_readiness.schema.json", "OutlineReadiness")
    _required_text("book_id", decision["book_id"])
    _chapter("chapter_index", decision["chapter_index"])
    _validate_authority_mapping(decision["authority"])
    _sha256("context_digest", decision["context_digest"])
    expected_reasons = []
    for field, code in (
        ("project_identity_matches", "project_identity_mismatch"),
        ("canonical_next_chapter_matches", "requested_chapter_not_canonical_next"),
        ("book_lease_held", "book_lease_missing"),
        ("required_delivery_allows_progress", "required_delivery_blocked"),
        ("sources_current", "outline_source_drift"),
    ):
        if not decision["evidence"][field]:
            expected_reasons.append(code)
    if decision["reasons"] != expected_reasons or decision["ok"] != (not expected_reasons):
        raise StageControlError("outline_readiness_not_derived", "readiness must match its evidence")
    return decision


def build_stage_authorization(
    *,
    stage: str,
    book_id: str,
    session_id: str,
    plan_id: str,
    chapter_index: int,
    authority_epoch: int,
    authority_head_event_hash: str | None,
    input_digest: str,
    previous_stage_receipt_hash: str | None,
    provider_profile: str,
    max_output_tokens: int,
    issued_at: str | None = None,
) -> dict[str, Any]:
    authorization = {
        "schema_version": "1.0",
        "stage": _stage(stage),
        "book_id": _required_text("book_id", book_id),
        "session_id": _required_text("session_id", session_id),
        "plan_id": _required_text("plan_id", plan_id),
        "chapter_index": _chapter("chapter_index", chapter_index),
        "authority": _authority(authority_epoch, authority_head_event_hash),
        "input_digest": _sha256("input_digest", input_digest),
        "previous_stage_receipt_hash": _optional_sha256(
            "previous_stage_receipt_hash", previous_stage_receipt_hash
        ),
        "provider_profile": _required_text("provider_profile", provider_profile),
        "max_output_tokens": _positive_int("max_output_tokens", max_output_tokens),
        "issued_at": issued_at or _now(),
    }
    authorization["authorization_hash"] = canonical_json_hash(authorization)
    return validate_stage_authorization(authorization)


def validate_stage_authorization(value: Any) -> dict[str, Any]:
    authorization = _validate_mapping(value, "stage_authorization.schema.json", "StageAuthorization")
    _stage(authorization["stage"])
    for field in ("book_id", "session_id", "plan_id", "provider_profile"):
        _required_text(field, authorization[field])
    _chapter("chapter_index", authorization["chapter_index"])
    _validate_authority_mapping(authorization["authority"])
    _sha256("input_digest", authorization["input_digest"])
    _optional_sha256("previous_stage_receipt_hash", authorization["previous_stage_receipt_hash"])
    _positive_int("max_output_tokens", authorization["max_output_tokens"])
    _sha256("authorization_hash", authorization["authorization_hash"])
    expected_hash = canonical_json_hash(authorization, exclude_fields=("authorization_hash",))
    if authorization["authorization_hash"] != expected_hash:
        raise StageControlError("stage_authorization_hash_mismatch", "authorization content was modified")
    return authorization


def assert_stage_authorized(
    authorization: Mapping[str, Any],
    *,
    stage: str,
    book_id: str,
    session_id: str,
    plan_id: str,
    chapter_index: int,
    authority_epoch: int,
    authority_head_event_hash: str | None,
    input_digest: str,
    previous_stage_receipt_hash: str | None,
    provider_profile: str,
    requested_max_output_tokens: int,
) -> dict[str, Any]:
    validated = validate_stage_authorization(dict(authorization))
    expected = {
        "stage": _stage(stage),
        "book_id": _required_text("book_id", book_id),
        "session_id": _required_text("session_id", session_id),
        "plan_id": _required_text("plan_id", plan_id),
        "chapter_index": _chapter("chapter_index", chapter_index),
        "authority": _authority(authority_epoch, authority_head_event_hash),
        "input_digest": _sha256("input_digest", input_digest),
        "previous_stage_receipt_hash": _optional_sha256(
            "previous_stage_receipt_hash", previous_stage_receipt_hash
        ),
        "provider_profile": _required_text("provider_profile", provider_profile),
    }
    for field, actual in expected.items():
        if validated[field] != actual:
            raise StageControlError("stage_authorization_drift", f"{field} changed after authorization")
    requested = _positive_int("requested_max_output_tokens", requested_max_output_tokens)
    if requested > validated["max_output_tokens"]:
        raise StageControlError(
            "stage_authorization_budget_escalation",
            "requested output budget exceeds the authorized profile",
        )
    return validated


def build_stage_receipt(
    authorization: Mapping[str, Any],
    *,
    status: str,
    output_digest: str | None,
    model_call_receipt_hash: str | None,
    created_at: str | None = None,
) -> dict[str, Any]:
    authorized = validate_stage_authorization(dict(authorization))
    resolved_status = str(status)
    if resolved_status not in _RECEIPT_STATUSES:
        raise StageControlError("stage_receipt_status_invalid", f"unsupported status: {resolved_status}")
    resolved_output = _optional_sha256("output_digest", output_digest)
    if resolved_status == "succeeded" and resolved_output is None:
        raise StageControlError("stage_receipt_output_missing", "a successful stage requires an output digest")
    if resolved_status != "succeeded" and resolved_output is not None:
        raise StageControlError("stage_receipt_failed_output_present", "an unsuccessful stage cannot publish output")
    receipt = {
        "schema_version": "1.0",
        "authorization_hash": authorized["authorization_hash"],
        "stage": authorized["stage"],
        "status": resolved_status,
        "book_id": authorized["book_id"],
        "session_id": authorized["session_id"],
        "plan_id": authorized["plan_id"],
        "chapter_index": authorized["chapter_index"],
        "authority": copy.deepcopy(authorized["authority"]),
        "input_digest": authorized["input_digest"],
        "output_digest": resolved_output,
        "model_call_receipt_hash": _optional_sha256(
            "model_call_receipt_hash", model_call_receipt_hash
        ),
        "previous_stage_receipt_hash": authorized["previous_stage_receipt_hash"],
        "created_at": created_at or _now(),
    }
    receipt["receipt_hash"] = canonical_json_hash(receipt)
    return validate_stage_receipt(receipt)


def validate_stage_receipt(value: Any) -> dict[str, Any]:
    receipt = _validate_mapping(value, "stage_receipt.schema.json", "StageReceipt")
    _stage(receipt["stage"])
    if receipt["status"] not in _RECEIPT_STATUSES:
        raise StageControlError("stage_receipt_status_invalid", "unsupported receipt status")
    for field in ("book_id", "session_id", "plan_id"):
        _required_text(field, receipt[field])
    _chapter("chapter_index", receipt["chapter_index"])
    _validate_authority_mapping(receipt["authority"])
    for field in (
        "receipt_hash",
        "authorization_hash",
        "input_digest",
    ):
        _sha256(field, receipt[field])
    for field in ("output_digest", "model_call_receipt_hash", "previous_stage_receipt_hash"):
        _optional_sha256(field, receipt[field])
    expected_hash = canonical_json_hash(receipt, exclude_fields=("receipt_hash",))
    if receipt["receipt_hash"] != expected_hash:
        raise StageControlError("stage_receipt_hash_mismatch", "receipt content was modified")
    succeeded = receipt["status"] == "succeeded"
    if succeeded != (receipt["output_digest"] is not None):
        raise StageControlError(
            "stage_receipt_output_status_mismatch",
            "only a successful stage may carry an output digest",
        )
    return receipt


def assert_receipt_matches_authorization(
    receipt: Mapping[str, Any], authorization: Mapping[str, Any]
) -> None:
    validated_receipt = validate_stage_receipt(dict(receipt))
    validated_authorization = validate_stage_authorization(dict(authorization))
    if validated_receipt["authorization_hash"] != validated_authorization["authorization_hash"]:
        raise StageControlError("stage_receipt_authorization_mismatch", "receipt belongs to another authorization")
    for field in (
        "stage",
        "book_id",
        "session_id",
        "plan_id",
        "chapter_index",
        "authority",
        "input_digest",
        "previous_stage_receipt_hash",
    ):
        if validated_receipt[field] != validated_authorization[field]:
            raise StageControlError(
                "stage_receipt_authorization_mismatch", f"receipt {field} is not authorized"
            )


def derive_draft_readiness(
    *,
    outline_stage_receipt: Mapping[str, Any],
    book_id: str,
    session_id: str,
    plan_id: str,
    chapter_index: int,
    authority_epoch: int,
    authority_head_event_hash: str | None,
    current_outline_input_digest: str,
    current_outline_hash: str,
    checked_at: str | None = None,
) -> dict[str, Any]:
    receipt = validate_stage_receipt(dict(outline_stage_receipt))
    current_authority = _authority(authority_epoch, authority_head_event_hash)
    evidence = {
        "outline_receipt_valid": receipt["stage"] == "outline" and receipt["status"] == "succeeded",
        "book_matches": receipt["book_id"] == _required_text("book_id", book_id),
        "outline_hash_matches": receipt["output_digest"] == _sha256(
            "current_outline_hash", current_outline_hash
        ),
        "authority_matches": receipt["authority"] == current_authority,
        "context_matches": receipt["input_digest"] == _sha256(
            "current_outline_input_digest", current_outline_input_digest
        ),
        "session_matches": receipt["session_id"] == _required_text("session_id", session_id),
        "plan_matches": receipt["plan_id"] == _required_text("plan_id", plan_id),
        "chapter_matches": receipt["chapter_index"] == _chapter("chapter_index", chapter_index),
    }
    reasons: list[str] = []
    for field, code in (
        ("outline_receipt_valid", "outline_stage_receipt_not_succeeded"),
        ("book_matches", "outline_book_mismatch"),
        ("outline_hash_matches", "outline_hash_drift"),
        ("authority_matches", "outline_authority_stale"),
        ("context_matches", "outline_input_drift"),
        ("session_matches", "outline_session_mismatch"),
        ("plan_matches", "outline_plan_mismatch"),
        ("chapter_matches", "outline_chapter_mismatch"),
    ):
        if not evidence[field]:
            reasons.append(code)
    decision = {
        "schema_version": "1.0",
        "kind": "draft",
        "ok": not reasons,
        "reasons": reasons,
        "book_id": _required_text("book_id", book_id),
        "session_id": _required_text("session_id", session_id),
        "plan_id": _required_text("plan_id", plan_id),
        "chapter_index": _chapter("chapter_index", chapter_index),
        "authority": current_authority,
        "context_digest": _sha256("current_outline_input_digest", current_outline_input_digest),
        "outline_hash": _sha256("current_outline_hash", current_outline_hash),
        "outline_stage_receipt_hash": receipt["receipt_hash"],
        "checked_at": checked_at or _now(),
        "evidence": evidence,
    }
    return validate_draft_readiness(decision)


def validate_draft_readiness(value: Any) -> dict[str, Any]:
    decision = _validate_mapping(value, "draft_readiness.schema.json", "DraftReadiness")
    for field in ("book_id", "session_id", "plan_id"):
        _required_text(field, decision[field])
    _chapter("chapter_index", decision["chapter_index"])
    _validate_authority_mapping(decision["authority"])
    for field in ("context_digest", "outline_hash", "outline_stage_receipt_hash"):
        _sha256(field, decision[field])
    expected_reasons = []
    for field, code in (
        ("outline_receipt_valid", "outline_stage_receipt_not_succeeded"),
        ("book_matches", "outline_book_mismatch"),
        ("outline_hash_matches", "outline_hash_drift"),
        ("authority_matches", "outline_authority_stale"),
        ("context_matches", "outline_input_drift"),
        ("session_matches", "outline_session_mismatch"),
        ("plan_matches", "outline_plan_mismatch"),
        ("chapter_matches", "outline_chapter_mismatch"),
    ):
        if not decision["evidence"][field]:
            expected_reasons.append(code)
    if decision["reasons"] != expected_reasons or decision["ok"] != (not expected_reasons):
        raise StageControlError("draft_readiness_not_derived", "readiness must match its evidence")
    return decision


def validate_stage_receipt_chain(receipts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    previous_hash: str | None = None
    scope: tuple[str, str, str] | None = None
    previous_chapter: int | None = None
    for index, raw in enumerate(receipts):
        receipt = validate_stage_receipt(dict(raw))
        if receipt["previous_stage_receipt_hash"] != previous_hash:
            raise StageControlError(
                "stage_receipt_chain_broken", f"receipt {index} does not reference the preceding receipt"
            )
        current_scope = (receipt["book_id"], receipt["session_id"], receipt["plan_id"])
        if scope is None:
            scope = current_scope
        elif current_scope != scope:
            raise StageControlError("stage_receipt_scope_changed", "receipt chain changed book/session/plan")
        chapter = int(receipt["chapter_index"])
        if previous_chapter is not None and chapter != previous_chapter:
            raise StageControlError(
                "stage_receipt_chapter_changed",
                "a model-stage receipt chain is scoped to one chapter; publication receipts advance chapters",
            )
        previous_hash = receipt["receipt_hash"]
        previous_chapter = chapter
        validated.append(receipt)
    return validated


def _validate_mapping(value: Any, schema: str, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StageControlError("stage_contract_invalid", f"{label} must be an object")
    try:
        return validate_schema(copy.deepcopy(dict(value)), schema)
    except SchemaValidationError as exc:
        raise StageControlError("stage_contract_invalid", f"invalid {label}: {exc}") from exc


def _authority(epoch: int, head_event_hash: str | None) -> dict[str, Any]:
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise StageControlError("stage_authority_invalid", "authority epoch must be a non-negative integer")
    return {
        "epoch": epoch,
        "head_event_hash": _optional_sha256("authority_head_event_hash", head_event_hash),
    }


def _validate_authority_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"epoch", "head_event_hash"}:
        raise StageControlError("stage_authority_invalid", "authority fields are invalid")
    return _authority(value["epoch"], value["head_event_hash"])


def _stage(value: str) -> str:
    resolved = str(value)
    if resolved not in _STAGES:
        raise StageControlError("stage_invalid", f"unsupported stage: {resolved}")
    return resolved


def _chapter(label: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StageControlError("stage_chapter_invalid", f"{label} must be a positive integer")
    return value


def _positive_int(label: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StageControlError("stage_limit_invalid", f"{label} must be a positive integer")
    return value


def _boolean(label: str, value: bool) -> bool:
    if type(value) is not bool:
        raise StageControlError("stage_boolean_invalid", f"{label} must be a boolean")
    return value


def _required_text(label: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StageControlError("stage_text_invalid", f"{label} is required")
    return value.strip()


def _sha256(label: str, value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise StageControlError("stage_digest_invalid", f"{label} must be a lowercase SHA-256 digest")
    return value


def _optional_sha256(label: str, value: Any) -> str | None:
    return None if value is None else _sha256(label, value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "StageControlError",
    "assert_receipt_matches_authorization",
    "assert_stage_authorized",
    "build_stage_authorization",
    "build_stage_receipt",
    "derive_draft_readiness",
    "derive_outline_readiness",
    "validate_draft_readiness",
    "validate_outline_readiness",
    "validate_stage_authorization",
    "validate_stage_receipt",
    "validate_stage_receipt_chain",
]
