from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from core.delivery import DeliveryQueue, validate_delivery_job
from core.engine.persistence_v2 import verify_publication_receipt
from core.reliable_semantic_contracts import readiness_failure_reasons
from core.schema import SchemaValidationError, validate_schema


class ReadinessError(RuntimeError):
    pass


def derive_readiness_decision(
    *,
    accepted: bool,
    committed: bool,
    book_id: str,
    run_id: str,
    expected_book_id: str,
    delivery_jobs: Iterable[Mapping[str, Any]],
    next_chapter: int,
    next_step_context_preflight: Mapping[str, Any],
    current_context_digest: str,
    checked_at: str | None = None,
    receipt_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if type(accepted) is not bool or type(committed) is not bool:
        raise ReadinessError("accepted and committed must be booleans")
    if not isinstance(book_id, str) or not book_id or not isinstance(run_id, str) or not run_id:
        raise ReadinessError("book_id and run_id are required")
    if not isinstance(expected_book_id, str) or not expected_book_id:
        raise ReadinessError("expected_book_id is required")
    if not isinstance(next_chapter, int) or isinstance(next_chapter, bool) or next_chapter < 1:
        raise ReadinessError("next_chapter must be a positive integer")
    jobs = [copy.deepcopy(validate_delivery_job(dict(job))) for job in delivery_jobs]
    required_jobs = [job for job in jobs if job["policy"] == "required"]
    required_states = [str(job["state"]) for job in required_jobs]
    preflight = validate_next_step_context_preflight(dict(next_step_context_preflight))
    planned_digest = str(preflight["next_step_context_digest"])
    _require_digest("current context digest", current_context_digest)
    preflight_valid = bool(preflight["valid"])
    preflight_valid = preflight_valid and preflight["book_id"] == book_id
    preflight_valid = preflight_valid and preflight["next_chapter"] == next_chapter
    identity_matches = str(book_id) == str(expected_book_id)
    read_set_unchanged = planned_digest == str(current_context_digest)
    reasons = list(readiness_failure_reasons(
        accepted=bool(accepted),
        committed=bool(committed),
        project_identity_matches=identity_matches,
        required_delivery_states=required_states,
        next_context_valid=preflight_valid,
        read_set_unchanged=read_set_unchanged,
    ))
    receipt = copy.deepcopy(dict(receipt_evidence or {}))
    binding_reasons = _delivery_binding_failure_reasons(
        jobs,
        book_id=book_id,
        run_id=run_id,
        receipt_evidence=receipt,
    )
    reasons.extend(binding_reasons)
    reasons = list(dict.fromkeys(reasons))
    decision = {
        "schema_version": "1.0",
        "ok": not reasons,
        "reasons": list(reasons),
        "book_id": str(book_id),
        "run_id": str(run_id),
        "next_chapter": int(next_chapter),
        "next_step_context_digest": planned_digest,
        "checked_at": checked_at or datetime.now(timezone.utc).isoformat(),
        "evidence": {
            "accepted": bool(accepted),
            "committed": bool(committed),
            "project_identity_matches": identity_matches,
            "required_delivery_jobs": [
                {
                    "job_id": job["job_id"],
                    "state": job["state"],
                    "payload_hash": job["payload_hash"],
                }
                for job in required_jobs
            ],
            "next_step_context_preflight": preflight,
            "read_set_unchanged": read_set_unchanged,
            "current_context_digest": str(current_context_digest),
            "delivery_binding_valid": not binding_reasons,
            "receipt": receipt,
        },
    }
    return validate_readiness_decision(decision)


def validate_readiness_decision(decision: Any) -> dict[str, Any]:
    if not isinstance(decision, dict):
        raise ReadinessError("ReadinessDecision must be an object")
    try:
        validated = validate_schema(decision, "readiness_decision.schema.json")
    except SchemaValidationError as exc:
        raise ReadinessError(str(exc)) from exc
    digest = validated["next_step_context_digest"]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ReadinessError("ReadinessDecision next_step_context_digest must be SHA-256")
    if bool(validated["ok"]) != (not validated["reasons"]):
        raise ReadinessError("ReadinessDecision ok must be derived from an empty reasons list")
    return validated


def validate_next_step_context_preflight(preflight: Any) -> dict[str, Any]:
    if not isinstance(preflight, dict):
        raise ReadinessError("NextStepContextPreflight must be an object")
    try:
        validated = validate_schema(preflight, "next_step_context_preflight.schema.json")
    except SchemaValidationError as exc:
        raise ReadinessError(str(exc)) from exc
    _require_digest("next-step context digest", validated["next_step_context_digest"])
    checks_ok = all(validated["checks"].values())
    expected_valid = checks_ok and not validated["blocking_conflicts"]
    if validated["valid"] != expected_valid:
        raise ReadinessError("NextStepContextPreflight valid must equal its checks and conflicts")
    return validated


def assert_provider_consumes_readiness_context(
    decision: Mapping[str, Any],
    *,
    actual_context_digest: str,
) -> None:
    validated = validate_readiness_decision(dict(decision))
    if not validated["ok"]:
        raise ReadinessError(
            "provider generation is blocked by readiness: " + ", ".join(validated["reasons"])
        )
    if actual_context_digest != validated["next_step_context_digest"]:
        raise ReadinessError("provider context digest drifted after readiness preflight")


class ReadinessService:
    def __init__(
        self,
        *,
        delivery_queue: DeliveryQueue,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.delivery_queue = delivery_queue
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def evaluate(
        self,
        *,
        quality_decision: Mapping[str, Any],
        publication_receipt: Mapping[str, Any] | str | Path,
        root_map: Mapping[str, str | Path],
        expected_book_id: str,
        next_chapter: int,
        next_step_context_preflight: Mapping[str, Any],
        current_context_digest: str,
    ) -> dict[str, Any]:
        try:
            quality = validate_schema(dict(quality_decision), "quality_decision.schema.json")
        except SchemaValidationError as exc:
            raise ReadinessError(f"invalid QualityDecision: {exc}") from exc
        receipt = verify_publication_receipt(publication_receipt, root_map=root_map)
        book_id = str(receipt.get("book_id") or quality.get("book_id") or expected_book_id)
        run_id = str(receipt.get("run_id") or quality.get("run_id") or "unknown")
        jobs = self.delivery_queue.jobs_for_run(run_id)
        return derive_readiness_decision(
            accepted=bool(quality.get("accepted")),
            committed=bool(receipt.get("committed")),
            book_id=book_id,
            run_id=run_id,
            expected_book_id=expected_book_id,
            delivery_jobs=jobs,
            next_chapter=next_chapter,
            next_step_context_preflight=next_step_context_preflight,
            current_context_digest=current_context_digest,
            checked_at=self.clock().astimezone(timezone.utc).isoformat(),
            receipt_evidence=receipt,
        )


def _delivery_binding_failure_reasons(
    jobs: Iterable[Mapping[str, Any]],
    *,
    book_id: str,
    run_id: str,
    receipt_evidence: Mapping[str, Any],
) -> tuple[str, ...]:
    job_list = list(jobs)
    actual = {str(job["job_id"]): job for job in job_list}
    reasons: list[str] = []
    if len(actual) != len(job_list):
        reasons.append("duplicate_delivery_job_id")
    for job in actual.values():
        if job["book_id"] != book_id or job["run_id"] != run_id:
            reasons.append("delivery_job_scope_mismatch")
        receipt_hash = receipt_evidence.get("receipt_hash")
        if isinstance(receipt_hash, str) and receipt_hash and job["publication_receipt_hash"] != receipt_hash:
            reasons.append("delivery_receipt_binding_mismatch")

    expected_raw = receipt_evidence.get("delivery_jobs")
    if not isinstance(expected_raw, list):
        return tuple(dict.fromkeys(reasons))
    expected: dict[str, Mapping[str, Any]] = {}
    for raw in expected_raw:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("id"), str):
            reasons.append("receipt_delivery_binding_invalid")
            continue
        if raw["id"] in expected:
            reasons.append("receipt_delivery_binding_invalid")
        expected[str(raw["id"])] = raw
    if set(actual) - set(expected):
        reasons.append("delivery_job_not_in_receipt")
    if set(expected) - set(actual):
        reasons.append("receipt_delivery_job_missing")
    for job_id in set(actual).intersection(expected):
        job = actual[job_id]
        binding = expected[job_id]
        policy = binding.get("policy") if isinstance(binding.get("policy"), Mapping) else {}
        if type(policy.get("required")) is not bool or policy.get("target") not in {
            "none",
            "file",
            "notion",
        }:
            reasons.append("receipt_delivery_binding_invalid")
        expected_required = bool(policy.get("required"))
        expected_target = policy.get("target")
        if binding.get("payload_hash") != job["payload_hash"]:
            reasons.append("delivery_payload_binding_mismatch")
        if expected_required != (job["policy"] == "required"):
            reasons.append("delivery_policy_binding_mismatch")
        if isinstance(expected_target, str) and expected_target and expected_target != job["target_type"]:
            reasons.append("delivery_target_binding_mismatch")
    return tuple(dict.fromkeys(reasons))


def _require_digest(label: str, value: Any) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ReadinessError(f"{label} must be a lowercase SHA-256 digest")


__all__ = [
    "ReadinessError",
    "ReadinessService",
    "assert_provider_consumes_readiness_context",
    "derive_readiness_decision",
    "validate_next_step_context_preflight",
    "validate_readiness_decision",
]
