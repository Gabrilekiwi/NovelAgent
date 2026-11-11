from __future__ import annotations

from typing import Any, Mapping

from core.quality_decision import quality_decision_review_status

KNOWN_REVIEW_STATUSES = frozenset({"pass", "warning", "needs_revision", "blocked", "error"})
_SEVERITY_TO_STATUS = {
    "info": "pass",
    "warning": "warning",
    "needs_revision": "needs_revision",
    "blocking": "blocked",
}


def resolve_review_status(payload: Mapping[str, Any] | None) -> str:
    """Return the author-visible review status for a run or compact result.

    Successful commits attach review_pipeline.status because runtime review
    only runs after the quality gate already accepted. When the quality gate
    rejects first, the executor intentionally skips that review pipeline and
    leaves quality_decision as the authoritative outcome. Consumers that only
    read review_pipeline then recorded unknown on the failure path.
    """

    if not isinstance(payload, Mapping):
        return "unknown"

    explicit = payload.get("review_status")
    if explicit in KNOWN_REVIEW_STATUSES:
        return str(explicit)

    run = payload.get("run") if isinstance(payload.get("run"), Mapping) else {}
    review = payload.get("review_pipeline")
    if not isinstance(review, Mapping) and isinstance(run, Mapping):
        review = run.get("review_pipeline")
    status = review.get("status") if isinstance(review, Mapping) else None
    if status in KNOWN_REVIEW_STATUSES:
        return str(status)

    decision = payload.get("quality_decision")
    if not isinstance(decision, Mapping) and isinstance(run, Mapping):
        decision = run.get("quality_decision")
    if not isinstance(decision, Mapping):
        return "unknown"

    decision_status = decision.get("status")
    if decision_status in KNOWN_REVIEW_STATUSES:
        return str(decision_status)

    severity = decision.get("max_severity")
    if severity in _SEVERITY_TO_STATUS:
        return _SEVERITY_TO_STATUS[severity]

    try:
        return quality_decision_review_status(dict(decision))
    except (TypeError, ValueError, KeyError):
        return "unknown"
