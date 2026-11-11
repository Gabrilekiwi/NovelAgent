from __future__ import annotations

from workbench.jobs import _compact_result, _review_status
from workbench.review_status import resolve_review_status
from workbench.app import _compact_run


def _quality_reject_result(*, severity: str = "needs_revision", status: str = "needs_revision") -> dict:
    return {
        "accepted": False,
        "committed": False,
        "quality_decision": {
            "accepted": False,
            "status": status,
            "max_severity": severity,
            "findings": [],
        },
        "run": {
            "id": "chapter_52_quality_reject",
            "chapter_index": 52,
            "status": "rejected",
            "accepted": False,
            "committed": False,
            "quality_decision": {
                "accepted": False,
                "status": status,
                "max_severity": severity,
                "findings": [],
            },
        },
        "validation": {
            "ok": False,
            "blocking_problem_count": 1,
            "problems": [
                {
                    "code": "missing_last_scene_continuity",
                    "message": "Chapter opening does not anchor the last scene state",
                    "severity": "blocking",
                    "blocking": True,
                    "repair_action": "manual_review",
                }
            ],
        },
    }


def test_resolve_review_status_prefers_review_pipeline_on_success() -> None:
    payload = {
        "review_pipeline": {"status": "pass"},
        "quality_decision": {"accepted": True, "status": "pass", "max_severity": "info"},
        "run": {"review_pipeline": {"status": "pass"}},
    }
    assert resolve_review_status(payload) == "pass"
    assert _review_status(payload) == "pass"


def test_resolve_review_status_uses_quality_decision_when_review_pipeline_skipped() -> None:
    payload = _quality_reject_result()
    assert "review_pipeline" not in payload
    assert resolve_review_status(payload) == "needs_revision"
    assert _review_status(payload) == "needs_revision"
    compact = _compact_result(payload)
    assert compact["review_status"] == "needs_revision"


def test_resolve_review_status_maps_blocking_quality_decision() -> None:
    payload = _quality_reject_result(severity="blocking", status="blocked")
    assert resolve_review_status(payload) == "blocked"
    assert _compact_result(payload)["review_status"] == "blocked"
    assert _compact_run(payload)["review_status"] == "blocked"


def test_compact_run_records_needs_revision_without_review_pipeline() -> None:
    compact = _compact_run(_quality_reject_result())
    assert compact["review_status"] == "needs_revision"
    assert compact["status"] == "rejected"
    assert compact["committed"] is False


def test_resolve_review_status_unknown_without_review_or_quality_decision() -> None:
    payload = {
        "accepted": False,
        "run": {
            "id": "chapter_60_uncertain",
            "status": "failed",
            "error": {"type": "ModelCallError"},
        },
    }
    assert resolve_review_status(payload) == "unknown"
    assert _compact_result(payload)["review_status"] == "unknown"
    assert _compact_run(payload)["review_status"] == "unknown"
