from __future__ import annotations

import hashlib
import json
from pathlib import Path

from workbench.app import (
    _chapter_recovery_summary,
    _retained_paid_draft_preview,
)


def _chapter_text(length: int, seed: str = "候选正文") -> str:
    unit = seed
    text = (unit * ((length // len(unit)) + 1))[:length]
    assert len(text) == length
    return text


def _uncertain_payload(*, chars: int = 3500, unsettled: int = 1, committed: bool = False, accepted: bool = False, failed_action: str = "validate", mismatch_chars: bool = False) -> dict:
    text = _chapter_text(chars)
    return {
        "accepted": accepted,
        "committed": committed,
        "chapter": text,
        "run": {
            "id": "chapter_60_fixture_unsettled",
            "chapter_index": 60,
            "status": "failed",
            "accepted": accepted,
            "committed": committed,
            "error": {
                "type": "ModelCallError",
                "message": "OpenAI chat completion failed (provider_call_uncertain; ProviderCallUncertainError).",
            },
            "chapter": {"chars": chars + (1 if mismatch_chars else 0)},
            "execution_evidence": {
                "budget": {"provider_calls": 6, "unsettled_attempt_count": unsettled}
            },
            "trace": [
                {"action": "generate_chapter", "status": "completed"},
                {"action": "polish", "status": "completed"},
                {"action": failed_action, "status": "failed"},
            ],
        },
    }


def _ppr_payload() -> dict:
    text = _chapter_text(6318, seed="部分响应稿")
    return {
        "accepted": False,
        "committed": False,
        "chapter": text,
        "run": {
            "id": "chapter_14_fixture_ppr",
            "chapter_index": 14,
            "status": "failed",
            "accepted": False,
            "committed": False,
            "error": {
                "type": "ModelCallError",
                "message": "OpenAI chat completion failed (provider_partial_response; ProviderPartialResponseError).",
            },
            "chapter": {"chars": 6318},
            "execution_evidence": {
                "budget": {"provider_calls": 14, "unsettled_attempt_count": 1}
            },
            "trace": [
                {"action": "generate_chapter", "status": "completed"},
                {"action": "polish", "status": "completed"},
                {"action": "validate", "status": "completed"},
                {"action": "repair_if_needed", "status": "failed"},
            ],
        },
    }


def test_preview_discloses_unsettled_paid_draft_without_opening_confirm() -> None:
    payload = _uncertain_payload()
    preview = _retained_paid_draft_preview(payload)
    assert preview is not None
    assert preview["text"] == payload["chapter"]
    assert preview["chars"] == 3500
    assert preview["failed_action"] == "validate"
    assert preview["unsettled_attempt_count"] == 1
    assert preview["source_run_id"] == "chapter_60_fixture_unsettled"
    assert preview["sha256"] == hashlib.sha256(payload["chapter"].encode("utf-8")).hexdigest()
    assert preview["committed"] is False
    assert preview["verified_candidate"] is False
    assert preview["confirm_submit_allowed"] is False


def test_preview_discloses_provider_partial_response_paid_draft() -> None:
    payload = _ppr_payload()
    preview = _retained_paid_draft_preview(payload)
    assert preview is not None
    assert preview["chars"] == 6318
    assert preview["failed_action"] == "repair_if_needed"
    assert preview["confirm_submit_allowed"] is False
    assert preview["verified_candidate"] is False


def test_preview_fails_closed_without_usable_paid_text() -> None:
    assert _retained_paid_draft_preview(_uncertain_payload(committed=True)) is None
    assert _retained_paid_draft_preview(_uncertain_payload(accepted=True)) is None
    assert _retained_paid_draft_preview(_uncertain_payload(chars=499)) is None
    assert _retained_paid_draft_preview(_uncertain_payload(mismatch_chars=True)) is None
    assert _retained_paid_draft_preview(None) is None
    empty = _uncertain_payload()
    empty["chapter"] = ""
    assert _retained_paid_draft_preview(empty) is None


def test_recovery_summary_includes_preview_only_without_formal_prose(tmp_path: Path) -> None:
    run_dir = tmp_path / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True)
    payload = _uncertain_payload()
    (run_dir / "chapter_60_fixture_unsettled.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    summary = _chapter_recovery_summary(tmp_path, 60)
    assert summary["formal_prose_exists"] is False
    assert summary["retained_complete_draft_available"] is False
    preview = summary["retained_paid_draft_preview"]
    assert preview is not None
    assert preview["chars"] == 3500
    assert preview["text"] == payload["chapter"]
    assert preview["confirm_submit_allowed"] is False
    assert preview["verified_candidate"] is False
