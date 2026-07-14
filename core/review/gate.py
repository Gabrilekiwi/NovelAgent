from __future__ import annotations

from typing import Any

from core.schema import validate_schema


REVIEW_STATUS_RANK = {
    "pass": 0,
    "warning": 1,
    "needs_revision": 2,
    "blocked": 3,
    "error": 4,
}

GATE_THRESHOLD_RANK = {
    "off": None,
    # The legacy name remains accepted, but warning-only review output is
    # advisory.  Therefore this threshold starts blocking at needs_revision.
    "warning": 2,
    "needs_revision": 2,
    "blocked": 3,
}


def evaluate_review_gate(
    *,
    review_pipeline: dict[str, Any] | None = None,
    quality_decision: dict[str, Any] | None = None,
    threshold: str = "off",
) -> dict[str, Any]:
    # `quality_decision` remains in the signature for old callers, but is
    # deliberately ignored.  The gate must be computed from the raw review.
    del quality_decision
    if threshold not in GATE_THRESHOLD_RANK:
        raise ValueError(f"unsupported review gate threshold: {threshold}")
    if threshold == "off":
        return _result(
            enabled=False,
            threshold=threshold,
            status="disabled",
            matched=False,
            review_status=None,
            reason="review gate disabled",
            exit_code=0,
        )
    if isinstance(review_pipeline, dict) and review_pipeline.get("status") == "error":
        review_status = "error"
    elif isinstance(review_pipeline, dict):
        review_status = review_pipeline.get("status")
    else:
        return _result(
            enabled=True,
            threshold=threshold,
            status="error",
            matched=True,
            review_status=None,
            reason="review gate requires review pipeline result",
            exit_code=1,
        )

    if review_status not in REVIEW_STATUS_RANK:
        return _result(
            enabled=True,
            threshold=threshold,
            status="error",
            matched=True,
            review_status=None,
            reason="review gate requires a valid review status",
            exit_code=1,
        )

    threshold_rank = GATE_THRESHOLD_RANK[threshold]
    review_rank = REVIEW_STATUS_RANK[str(review_status)]
    matched = review_rank >= int(threshold_rank)
    status = "fail" if matched else "pass"
    relation = "meets" if matched else "does not meet"
    return _result(
        enabled=True,
        threshold=threshold,
        status=status,
        matched=matched,
        review_status=str(review_status),
        reason=f"review status {review_status} {relation} gate threshold {threshold}",
        exit_code=1 if matched else 0,
    )


def _result(
    *,
    enabled: bool,
    threshold: str,
    status: str,
    matched: bool,
    review_status: str | None,
    reason: str,
    exit_code: int,
) -> dict[str, Any]:
    return validate_schema(
        {
            "schema_version": "1.0",
            "enabled": enabled,
            "threshold": threshold,
            "status": status,
            "matched": matched,
            "review_status": review_status,
            "reason": reason,
            "exit_code": exit_code,
        },
        "review_gate_result.schema.json",
    )


__all__ = [
    "GATE_THRESHOLD_RANK",
    "REVIEW_STATUS_RANK",
    "evaluate_review_gate",
]
