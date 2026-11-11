from __future__ import annotations

from pathlib import Path
import hashlib
import json
from datetime import datetime, timezone

from core.memory_v2.canonical import canonical_json_hash
from core.story_project.identity import load_project_identity
from tests.test_workbench_app import _client, _create_project, _unlock
from workbench.recovery_guidance import (
    ATTENTION_CONFIRM_DRAFT,
    ATTENTION_NEEDS_RECOVERY,
    ATTENTION_VALIDATE_DRAFT,
    GUIDANCE_CONFIRM_DRAFT,
    GUIDANCE_VALIDATE_DRAFT,
    attention_label_for_project,
    durable_polished_draft_available,
)


def _polished_recovery(**overrides) -> dict:
    payload = {
        "formal_prose_exists": False,
        "uncertain_without_run": False,
        "checkpoint": {
            "action": "repair_draft",
            "reason": "durable_polished_draft_available",
            "has_complete_draft": True,
        },
    }
    payload.update(overrides)
    return payload


def test_durable_polished_draft_available_requires_classified_complete_draft() -> None:
    assert durable_polished_draft_available(_polished_recovery()) is True
    assert durable_polished_draft_available(_polished_recovery(uncertain_without_run=True)) is False
    assert durable_polished_draft_available(_polished_recovery(formal_prose_exists=True)) is False
    assert durable_polished_draft_available(
        _polished_recovery(checkpoint={"action": "reset", "reason": "durable_polished_draft_available", "has_complete_draft": True})
    ) is False
    assert durable_polished_draft_available(
        _polished_recovery(checkpoint={"action": "repair_draft", "reason": "other", "has_complete_draft": True})
    ) is False
    assert durable_polished_draft_available(None) is False


def test_attention_label_guides_validation_when_polished_draft_is_ready() -> None:
    recovery = _polished_recovery()
    assert attention_label_for_project("needs_recovery", recovery=recovery) == ATTENTION_VALIDATE_DRAFT
    assert (
        attention_label_for_project("needs_recovery", recovery=recovery, draft_state="validated")
        == ATTENTION_CONFIRM_DRAFT
    )
    assert attention_label_for_project("needs_recovery") == ATTENTION_NEEDS_RECOVERY
    assert "结束失败尝试" in GUIDANCE_VALIDATE_DRAFT or "新开一章" in GUIDANCE_VALIDATE_DRAFT
    assert "确认提交" in GUIDANCE_CONFIRM_DRAFT


def test_attention_for_polished_draft(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="pg")
    jobs = client.app.state.jobs
    parent = jobs.create_job(
        project_id=project["id"],
        kind="generation_session",
        start_chapter=int(project["next_chapter"]),
        end_chapter=int(project["next_chapter"]),
        config={},
    )
    assert jobs.claim_next().id == parent.id
    jobs.mark_running(parent.id, chapter=int(project["next_chapter"]), command=["python", "main.py"])
    jobs.chapter_failed(
        parent.id,
        chapter=int(project["next_chapter"]),
        error={
            "code": "engine_result_unreadable",
            "error_type": "ProviderCallUncertainError",
            "message": "上次模型调用结果不确定，没有完整回执。不要盲目重试；请先结束本章失败尝试，再开始新的一次写作。",
        },
        uncertain=True,
    )

    before = client.get(f"/api/projects/{project['id']}")
    assert before.status_code == 200, before.text
    assert before.json()["project"]["attention_label"] == ATTENTION_NEEDS_RECOVERY

    resolutions = (
        Path(project["root"])
        / ".novelagent"
        / "runtime"
        / "runs"
        / "locked_chapter_resolutions"
    )
    resolutions.mkdir(parents=True)
    identity = load_project_identity(Path(project["root"]))
    draft_text = "完整润色稿\n"
    draft_sha = hashlib.sha256(draft_text.encode("utf-8")).hexdigest()
    created_at = datetime(2026, 11, 11, 5, 0, tzinfo=timezone.utc).isoformat()
    resolution_id = "resolution_20261111T050000Z_ab"
    payload = {
        "schema_version": "1.2",
        "id": resolution_id,
        "created_at": created_at,
        "book_id": str(identity.book_id),
        "chapter_index": int(project["next_chapter"]),
        "action": "repair_draft",
        "reason": "durable_polished_draft_available",
        "source_run_id": "chapter_ready",
        "scenes": [],
        "expected_scene_count": 1,
        "complete_draft": {
            "text": draft_text,
            "sha256": draft_sha,
            "problem_codes": [],
            "source_attempt_id": "attempt-test",
            "source_stage": "chapter_generation",
        },
        "resolved_execution_ids": ["execution_test"],
        "resolved_attempt_ids": ["attempt-test"],
        "discarded_run_ids": [],
    }
    payload["resolution_hash"] = canonical_json_hash(payload, exclude_fields=("resolution_hash",))
    (resolutions / f"{resolution_id}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    jobs.upsert_draft(
        project_id=project["id"],
        chapter=int(project["next_chapter"]),
        text="完整润色稿\n",
        source_run_id="chapter_ready",
    )

    after = client.get(f"/api/projects/{project['id']}")
    assert after.status_code == 200, after.text
    assert after.json()["project"]["attention_label"] == ATTENTION_VALIDATE_DRAFT

    pending = client.get(f"/api/projects/{project['id']}/pending")
    assert pending.status_code == 200, pending.text
    recovery = pending.json()["chapter_recovery"]
    assert recovery["durable_polished_draft_available"] is True
    assert recovery["checkpoint"]["reason"] == "durable_polished_draft_available"
    assert recovery["checkpoint"]["has_complete_draft"] is True


def test_pending_ui_contract_guides_confirming_existing_draft_not_starting_over() -> None:
    static = Path(__file__).resolve().parents[1] / "workbench" / "static"
    spa = (static / "workbench-spa.js").read_text(encoding="utf-8")
    assert "durablePolishedDraftAvailable" in spa
    assert "polishedDraftGuidance" in spa
    assert "durable_polished_draft_available" in spa
    assert "请确认提交已有草稿继续本章" in spa
    assert "请先验证或确认已有草稿" in spa
    assert "!durablePolishedDraftAvailable(issue)" in spa
    start = spa.index("const providerUncertainCopy")
    chunk = spa[start:start + 1200]
    assert "durablePolishedDraftAvailable(issue)" in chunk
    assert "polishedDraftGuidance(issue)" in chunk
