from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os

import pytest
from fastapi.testclient import TestClient

from core.story_project.activation import build_story_state_calibration_report
from core.story_project.identity import load_project_identity
from core.story_project.paths import canonical_prose_path
from core.story_project.semantic_contracts import STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION
from core.story_project.semantic_parser import SEMANTIC_PARSER_VERSION
from workbench.app import SESSION_COOKIE, create_app


REPOSITORY = Path(__file__).resolve().parents[1]


def test_windows_launcher_keeps_cmd_safe_crlf_and_forwards_arguments() -> None:
    raw = (REPOSITORY / "启动创作工作台.bat").read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")
    script = raw.decode("ascii")
    assert "%NOVELAGENT_PYTHON% run_workbench.py %*" in script
    assert 'cd /d "%~dp0"' in script


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    workspace = tmp_path / "books"
    workspace.mkdir()
    app = create_app(
        database_path=tmp_path / "workbench.db",
        runtime_root=tmp_path / "job-runtime",
        allowed_workspaces=[workspace],
        repo_root=REPOSITORY,
        start_worker=False,
        auto_import_active=False,
    )
    return TestClient(app), workspace


def _unlock(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert SESSION_COOKIE in client.cookies
    set_cookie = response.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie


def _create_project(client: TestClient, workspace: Path, name: str = "测试长篇") -> dict:
    response = client.post(
        "/api/projects",
        headers={"origin": "http://testserver"},
        json={
            "name": name,
            "genre": "科幻",
            "synopsis": "用于工作台自动测试。",
            "language": "zh-CN",
            "target": str(workspace / name),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["project"]


def _outline(chapter: int, *, title: str | None = None) -> dict:
    return {
        "chapter_index": chapter,
        "title": title or f"第{chapter}章测试",
        "core_event": f"主角解决第{chapter}章的核心冲突",
        "required_beats": ["建立压力", "作出选择"],
        "ending_pressure": "新的风险逼近",
    }


def test_local_security_boundary_requires_safe_host_origin_and_startup_cookie(
    tmp_path: Path,
) -> None:
    client, workspace = _client(tmp_path)

    assert client.get("/api/bootstrap", headers={"host": "attacker.example"}).status_code == 400
    denied = client.post(
        "/api/projects",
        json={"name": "无令牌", "target": str(workspace / "denied")},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "session_token_required"

    _unlock(client)
    cross_site = client.post(
        "/api/projects",
        headers={"origin": "https://attacker.example"},
        json={"name": "跨站", "target": str(workspace / "cross-site")},
    )
    assert cross_site.status_code == 403
    assert cross_site.json()["error"]["code"] == "origin_not_allowed"

    created = _create_project(client, workspace)
    assert Path(created["root"]).is_dir()
    bootstrap_text = client.get("/api/bootstrap").text
    assert client.app.state.session_token not in bootstrap_text


def test_project_and_outline_apis_enforce_revision_and_sha_cas(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace)
    project_id = project["id"]

    proposed = [_outline(1), _outline(2)]
    created_response = client.post(
        f"/api/projects/{project_id}/outline-batches",
        headers={"origin": "http://testserver"},
        json={"chapters": proposed},
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()["batch"]
    assert created["revision"] == 1
    assert created["status"] == "draft"
    assert created_response.json()["provider_calls"] == 0

    edited = [_outline(1, title="修改后的第一章"), _outline(2)]
    stale = client.put(
        f"/api/outline-batches/{created['id']}",
        headers={"origin": "http://testserver"},
        json={
            "chapters": edited,
            "expected_revision": 1,
            "expected_sha256": "0" * 64,
        },
    )
    assert stale.status_code == 409

    updated_response = client.put(
        f"/api/outline-batches/{created['id']}",
        headers={"origin": "http://testserver"},
        json={
            "chapters": edited,
            "expected_revision": created["revision"],
            "expected_sha256": created["proposal_sha256"],
        },
    )
    assert updated_response.status_code == 200, updated_response.text
    updated = updated_response.json()["batch"]
    assert updated["revision"] == 2
    assert updated["items"][0]["title"] == "修改后的第一章"

    stale_approval = client.post(
        f"/api/outline-batches/{created['id']}/approve",
        headers={"origin": "http://testserver"},
        json={
            "expected_revision": created["revision"],
            "expected_sha256": created["proposal_sha256"],
        },
    )
    assert stale_approval.status_code == 409

    approved_response = client.post(
        f"/api/outline-batches/{created['id']}/approve",
        headers={"origin": "http://testserver"},
        json={
            "expected_revision": updated["revision"],
            "expected_sha256": updated["proposal_sha256"],
        },
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()["batch"]
    assert approved["status"] == "approved"
    assert approved["approval_receipt_sha256"]
    assert (Path(project["root"]) / "大纲" / "细纲_第001章.md").is_file()

    immutable = client.put(
        f"/api/outline-batches/{created['id']}",
        headers={"origin": "http://testserver"},
        json={
            "chapters": edited,
            "expected_revision": approved["revision"],
            "expected_sha256": approved["proposal_sha256"],
        },
    )
    assert immutable.status_code == 409


def test_job_poll_and_sse_replay_events_from_last_event_id(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace)
    store = client.app.state.jobs
    job = store.create_job(
        project_id=project["id"],
        kind="generation_session",
        start_chapter=1,
        end_chapter=1,
    )
    custom_id = store.append_event(job.id, "test.progress", {"stage": "drafting"})
    store.request_cancel(job.id)

    poll = client.get(f"/api/jobs/{job.id}?after_event_id={custom_id}")
    assert poll.status_code == 200
    assert poll.json()["job"]["state"] == "cancelled"
    assert [item["event"] for item in poll.json()["events"]] == ["job.cancel_requested"]

    stream = client.get(
        f"/api/jobs/{job.id}/events",
        headers={"last-event-id": str(custom_id)},
    )
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "event: job.cancel_requested" in stream.text
    assert "event: job.queued" not in stream.text
    assert "event: test.progress" not in stream.text


def test_cancel_paused_job_releases_generation_session_slot(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="close-paused-session")
    store = client.app.state.jobs
    paused = store.create_job(
        project_id=project["id"],
        kind="generation_session",
        start_chapter=1,
        end_chapter=10,
    )
    store.chapter_failed(
        paused.id,
        chapter=1,
        error={"code": "scene_boundary_state_mismatch"},
        uncertain=False,
    )
    assert store.get(paused.id).state == "awaiting_input"

    cancelled = client.post(
        f"/api/jobs/{paused.id}/cancel",
        headers={"origin": "http://testserver"},
    )

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["job"]["state"] == "cancelled"
    replacement = store.create_job(
        project_id=project["id"],
        kind="generation_session",
        start_chapter=1,
        end_chapter=10,
    )
    assert replacement.id != paused.id
    assert replacement.state == "queued"


def test_job_api_exposes_reconciled_pre_provider_retry_readiness(
    tmp_path: Path,
) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="context-budget-api")
    jobs = client.app.state.jobs
    parent = jobs.create_job(
        project_id=project["id"],
        kind="generation_session",
        start_chapter=1,
        end_chapter=3,
    )
    assert jobs.claim_next().id == parent.id
    jobs.mark_running(
        parent.id,
        chapter=1,
        command=["python", "main.py", "--chapter", "1"],
    )
    error_message = (
        "story_project_context_budget_exceeded: scene input requires "
        "33212 tokens; hard limit is 32000"
    )
    result = {
        "chapter": "",
        "accepted": False,
        "committed": False,
        "run": {
            "id": "run-context-budget-api",
            "chapter_index": 1,
            "status": "failed",
            "accepted": False,
            "committed": False,
            "chapter": {"chars": 0},
            "error": {"type": "ContextBudgetError", "message": error_message},
            "execution_evidence": {
                "budget": {"provider_calls": 0, "unsettled_attempt_count": 0}
            },
            "persistence": None,
            "story_project": {
                "writeback": {"attempted": False, "applied": False}
            },
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    run_dir = Path(project["root"]) / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True)
    (run_dir / "chapter_1_run-context-budget-api.json").write_text(
        json.dumps(result), encoding="utf-8"
    )
    jobs.chapter_failed(
        parent.id,
        chapter=1,
        error={
            "code": "engine_result_unreadable",
            "exit_code": 1,
            "stderr_tail": f"ContextBudgetError: {error_message}",
        },
        uncertain=False,
    )

    client.app.state.supervisor._reconcile_pre_provider_context_failures()
    response = client.get(f"/api/jobs/{parent.id}")

    assert response.status_code == 200, response.text
    payload = response.json()["job"]
    assert payload["id"] == parent.id
    assert payload["state"] == "awaiting_input"
    assert payload["next_chapter"] == 1
    assert payload["error"]["code"] == "pre_provider_context_budget_exceeded"
    assert payload["error"]["details"]["provider_calls"] == 0
    assert payload["resume_ready"] is True

    production = client.get(f"/api/projects/{project['id']}/production")
    assert production.status_code == 200, production.text
    production_parent = next(
        item for item in production.json()["jobs"] if item["id"] == parent.id
    )
    assert production_parent["resume_ready"] is True

    resumed = client.post(
        f"/api/jobs/{parent.id}/resume",
        headers={"origin": "http://testserver"},
        json={"confirmed": True},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["job"]["id"] == parent.id
    assert resumed.json()["job"]["state"] == "queued"


def test_job_api_exposes_repair_convergence_as_recovery_required(
    tmp_path: Path,
) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="repair-convergence-api")
    jobs = client.app.state.jobs
    parent = jobs.create_job(
        project_id=project["id"],
        kind="generation_session",
        start_chapter=1,
        end_chapter=3,
    )
    assert jobs.claim_next().id == parent.id
    jobs.mark_running(parent.id, chapter=1, command=["python", "main.py"])
    details = {
        "failure_origin": "chapter_repair_convergence",
        "run_id": "run-repair-api",
        "provider_calls": 2,
        "settled_provider_calls": 2,
        "unsettled_attempt_count": 0,
        "candidate_chars": 1200,
        "formal_prose_exists": False,
        "accepted": False,
        "committed": False,
        "writeback_attempted": False,
        "writeback_applied": False,
        "original_problem_count": 6,
        "final_problem_count": 2,
        "convergence_reason": "new_more_severe_problem",
        "new_problem_codes": ["missing_opening_bridge"],
        "automatic_retry": False,
        "safe_to_retry": False,
        "recovery_required": True,
    }
    jobs.chapter_failed(
        parent.id,
        chapter=1,
        result={
            "accepted": False,
            "committed": False,
            "run": {
                "id": "run-repair-api",
                "chapter_index": 1,
                "status": "failed",
                "chapter": {"chars": 1200},
                "execution_evidence": {
                    "budget": {
                        "provider_calls": 2,
                        "unsettled_attempt_count": 0,
                    }
                },
            },
        },
        error={
            "code": "chapter_repair_not_converging",
            "details": details,
            "automatic_retry": False,
            "safe_to_retry": False,
            "recovery_required": True,
        },
        uncertain=False,
    )

    response = client.get(f"/api/jobs/{parent.id}")
    assert response.status_code == 200, response.text
    payload = response.json()["job"]
    assert payload["state"] == "awaiting_input"
    assert payload["resume_ready"] is False
    assert payload["error"]["code"] == "chapter_repair_not_converging"
    assert payload["error"]["details"] == details
    assert payload["result"]["provider_calls"] == 2
    assert payload["result"]["unsettled_attempt_count"] == 0
    stream = client.get(f"/api/jobs/{parent.id}/events")
    assert stream.status_code == 200
    assert "event: chapter.repair_not_converging" in stream.text


def test_sse_closes_after_job_enters_manual_recovery_pause(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="sse-recovery-pause")
    store = client.app.state.jobs
    job = store.create_job(
        project_id=project["id"],
        kind="generation_session",
        start_chapter=1,
        end_chapter=1,
    )
    store.chapter_failed(
        job.id,
        chapter=1,
        error={"code": "provider_state_uncertain"},
        uncertain=True,
    )

    stream = client.get(f"/api/jobs/{job.id}/events")

    assert stream.status_code == 200
    assert "event: chapter.needs_recovery" in stream.text
    assert client.app.state.jobs.get(job.id).state == "needs_recovery"


def test_recovery_summary_marks_missing_current_and_stale_checkpoints(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="recovery-summary")
    run_dir = Path(project["root"]) / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True)

    def write_run(run_id: str, *, mtime_ns: int) -> None:
        path = run_dir / f"chapter_20_{run_id}.json"
        path.write_text(
            json.dumps(
                {
                    "run": {
                        "id": run_id,
                        "chapter_index": 20,
                        "committed": False,
                        "status": "failed",
                    }
                }
            ),
            encoding="utf-8",
        )
        os.utime(path, ns=(mtime_ns, mtime_ns))

    def recovery_summary() -> dict:
        response = client.get(f"/api/projects/{project['id']}/pending")
        assert response.status_code == 200, response.text
        return response.json()["chapter_20_recovery"]

    write_run("run-original", mtime_ns=1_000_000_000)
    missing = recovery_summary()
    assert missing["latest_run"]["id"] == "run-original"
    assert missing["recoverable"] is True
    assert missing["checkpoint"] is None
    assert missing["checkpoint_matches_latest_run"] is False
    assert missing["needs_reclassification"] is True

    resolution_dir = run_dir / "locked_chapter_resolutions"
    resolution_dir.mkdir()
    (resolution_dir / "resolution_current.json").write_text(
        json.dumps(
            {
                "chapter_index": 20,
                "action": "resume_scenes",
                "reason": "preserve trusted scene prefix",
                "source_run_id": "run-original",
                "expected_scene_count": 3,
                "scenes": [{"scene_index": 1}, {"scene_index": 2}],
            }
        ),
        encoding="utf-8",
    )

    current = recovery_summary()
    assert current["checkpoint"]["source_run_id"] == "run-original"
    assert current["checkpoint"]["reusable_scene_count"] == 2
    assert current["checkpoint"]["expected_scene_count"] == 3
    assert current["checkpoint_matches_latest_run"] is True
    assert current["needs_reclassification"] is False

    write_run("run-newer-failure", mtime_ns=3_000_000_000)
    stale = recovery_summary()
    assert stale["latest_run"]["id"] == "run-newer-failure"
    assert stale["checkpoint"]["source_run_id"] == "run-original"
    assert stale["checkpoint_matches_latest_run"] is False
    assert stale["needs_reclassification"] is True

    (resolution_dir / "resolution_reset.json").write_text(
        json.dumps(
            {
                "chapter_index": 20,
                "action": "reset",
                "reason": "operator_requested_reset",
                "source_run_id": "run-newer-failure",
                "expected_scene_count": 3,
                "scenes": [],
            }
        ),
        encoding="utf-8",
    )

    reset = recovery_summary()
    assert reset["latest_run"]["id"] == "run-newer-failure"
    assert reset["checkpoint"]["action"] == "reset"
    assert reset["recoverable"] is False
    assert reset["needs_reclassification"] is False


def test_existing_chapter_20_recovery_is_only_queued_without_worker_or_model(
    tmp_path: Path,
) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="第20章恢复测试")
    project_root = Path(project["root"])
    for chapter in range(1, 20):
        (project_root / "正文" / f"第{chapter:03d}章_测试.md").write_text(
            f"第{chapter}章正文",
            encoding="utf-8",
        )
    (project_root / "大纲" / "细纲_第020章.md").write_text(
        "# 第20章\n\n恢复中断的第三场景。\n",
        encoding="utf-8",
    )

    response = client.post(
        f"/api/projects/{project['id']}/recovery",
        headers={"origin": "http://testserver"},
        json={"chapter": 20},
    )
    assert response.status_code == 202, response.text
    job = response.json()["job"]
    assert job["kind"] == "locked_chapter_recovery"
    assert job["state"] == "queued"
    assert job["start_chapter"] == job["end_chapter"] == 20
    assert client.app.state.supervisor._thread is None
    assert not (project_root / "正文" / "第020章_测试.md").exists()


def test_pending_exposes_recovery_for_the_paused_session_chapter(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="dynamic-recovery-chapter")
    jobs = client.app.state.jobs
    parent = jobs.create_job(
        project_id=project["id"],
        kind="generation_session",
        start_chapter=21,
        end_chapter=30,
        config={},
    )
    assert jobs.claim_next().id == parent.id
    jobs.mark_running(parent.id, chapter=21, command=["python", "main.py"])
    jobs.chapter_failed(
        parent.id,
        chapter=21,
        error={"code": "engine_result_unreadable"},
        uncertain=False,
    )
    run_dir = Path(project["root"]) / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True)
    (run_dir / "chapter_21_failed.json").write_text(
        json.dumps(
            {
                "run": {
                    "id": "chapter-21-failed",
                    "chapter_index": 21,
                    "committed": False,
                    "status": "failed",
                    "error": {
                        "type": "ValueError",
                        "message": "Scene response was not valid structured JSON",
                    },
                    "execution_evidence": {
                        "budget": {
                            "provider_calls": 2,
                            "unsettled_attempt_count": 0,
                            "total_input_tokens": 300,
                            "total_output_tokens": 40,
                            "estimated_cost": 0.1,
                        }
                    },
                    "chapter": {"chars": 0},
                },
                "validation": {
                    "ok": False,
                    "blocking_problem_count": 1,
                    "manual_review_count": 1,
                    "problems": [
                        {
                            "code": "llm_plot_logic_trigger_contradiction",
                            "message": "车辆滑动与触发条件相互矛盾。",
                            "severity": "high",
                            "blocking": True,
                            "repair_action": "manual_review",
                            "repair_hint": "明确触发的是车门、座椅或后窗锁扣。",
                            "evidence": [
                                {"kind": "chapter_span", "value": "车一动就会触发"}
                            ],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    resolution_dir = run_dir / "locked_chapter_resolutions"
    resolution_dir.mkdir()
    (resolution_dir / "resolution_chapter_21.json").write_text(
        json.dumps(
            {
                "chapter_index": 21,
                "action": "resume_scenes",
                "reason": "contiguous_scene_prefix_available",
                "source_run_id": "chapter-21-failed",
                "expected_scene_count": 4,
                "scenes": [{"index": 1}],
            }
        ),
        encoding="utf-8",
    )

    pending = client.get(f"/api/projects/{project['id']}/pending")

    assert pending.status_code == 200, pending.text
    payload = pending.json()
    assert payload["chapter_recovery"]["chapter"] == 21
    assert payload["chapter_recovery"]["checkpoint"]["reusable_scene_count"] == 1
    assert payload["chapter_recovery"]["checkpoint_matches_latest_run"] is True
    assert payload["chapter_recovery"]["latest_run"]["error_type"] == "ValueError"
    assert payload["chapter_recovery"]["latest_run"]["provider_calls"] == 2
    assert payload["chapter_recovery"]["latest_run"]["unsettled_attempt_count"] == 0
    assert payload["chapter_recovery"]["latest_run"]["chapter_chars"] == 0
    validation = payload["chapter_recovery"]["latest_run"]["validation"]
    assert validation["ok"] is False
    assert validation["blocking_problem_count"] == 1
    assert validation["problems"] == [
        {
            "code": "llm_plot_logic_trigger_contradiction",
            "message": "车辆滑动与触发条件相互矛盾。",
            "severity": "high",
            "blocking": True,
            "repair_action": "manual_review",
            "repair_hint": "明确触发的是车门、座椅或后窗锁扣。",
            "evidence": [{"kind": "chapter_span", "value": "车一动就会触发"}],
        }
    ]
    assert payload["chapter_20_recovery"]["chapter"] == 20


def test_pending_preserves_author_edit_and_exposes_verified_source_for_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="recovery-source-conflict")
    project_root = Path(project["root"])
    jobs = client.app.state.jobs
    parent = jobs.create_job(
        project_id=project["id"],
        kind="generation_session",
        start_chapter=22,
        end_chapter=30,
        config={},
    )
    assert jobs.claim_next().id == parent.id
    jobs.mark_running(parent.id, chapter=22, command=["python", "main.py"])
    jobs.chapter_failed(
        parent.id,
        chapter=22,
        error={"code": "chapter_not_safe_to_continue"},
        uncertain=False,
    )

    run_id = "chapter-22-rejected"
    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True)
    (run_dir / "chapter_22_rejected.json").write_text(
        json.dumps(
            {
                "run": {
                    "id": run_id,
                    "chapter_index": 22,
                    "committed": False,
                    "accepted": False,
                    "status": "rejected",
                }
            }
        ),
        encoding="utf-8",
    )
    source_text = "newer verified recovery source"
    source_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    workbench_text = source_text + "\n"
    incoming_sha256 = hashlib.sha256(workbench_text.encode("utf-8")).hexdigest()
    resolution_dir = run_dir / "locked_chapter_resolutions"
    resolution_dir.mkdir()
    checkpoint = resolution_dir / "resolution_chapter_22_final.json"
    checkpoint.write_text(
        json.dumps(
            {
                "chapter_index": 22,
                "action": "repair_draft",
                "reason": "durable_repaired_draft_available",
                "source_run_id": run_id,
                "complete_draft": {
                    "text": source_text,
                    "sha256": source_sha256,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "workbench.app.verified_final_recovery_draft",
        lambda **_kwargs: {"text": source_text, "sha256": source_sha256},
    )
    draft = jobs.upsert_draft(
        project_id=project["id"],
        chapter=22,
        text="older recovery source",
        source_run_id=run_id,
        source_path="older-checkpoint.json",
    )

    untouched = client.get(f"/api/projects/{project['id']}/pending")
    assert untouched.status_code == 200, untouched.text
    assert "draft_source_conflict" not in untouched.json()["chapter_recovery"]

    edited = jobs.update_draft(
        draft.id,
        text="author edited recovery draft",
        expected_revision=draft.revision,
    )
    pending = client.get(f"/api/projects/{project['id']}/pending")
    assert pending.status_code == 200, pending.text
    conflict = pending.json()["chapter_recovery"]["draft_source_conflict"]
    assert conflict == {
        "code": "draft_source_upgrade_conflict",
        "chapter": 22,
        "draft_id": draft.id,
        "current_sha256": edited.sha256,
        "base_sha256": draft.base_sha256,
        "incoming_sha256": incoming_sha256,
        "source_sha256": source_sha256,
        "incoming_text": workbench_text,
        "automatic_overwrite": False,
    }
    assert jobs.get_draft(draft.id) == edited


def test_parent_bound_recovery_is_idempotent_per_failure_but_not_reused_after_a_second_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="recovery-failure-generation")
    project_root = Path(project["root"])
    jobs = client.app.state.jobs
    parent = jobs.create_job(
        project_id=project["id"],
        kind="generation_session",
        start_chapter=21,
        end_chapter=30,
        config={},
    )
    assert jobs.claim_next().id == parent.id
    jobs.mark_running(parent.id, chapter=21, command=["python", "main.py"])
    first_failure = jobs.chapter_failed(
        parent.id,
        chapter=21,
        error={"code": "chapter_not_safe_to_continue"},
        uncertain=False,
    )

    run_dir = project_root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "workbench.app.verified_final_recovery_draft",
        lambda *, run_id, **_kwargs: {
            "sha256": hashlib.sha256(run_id.encode("utf-8")).hexdigest(),
        },
    )

    def write_run(filename: str, run_id: str, *, mtime_ns: int) -> None:
        path = run_dir / filename
        path.write_text(
            json.dumps(
                {
                    "run": {
                        "id": run_id,
                        "chapter_index": 21,
                        "committed": False,
                        "status": "failed",
                    }
                }
            ),
            encoding="utf-8",
        )
        os.utime(path, ns=(mtime_ns, mtime_ns))

    write_run("chapter_21_first.json", "run-first", mtime_ns=1_000_000_000)
    endpoint = f"/api/jobs/{parent.id}/recover"
    headers = {"origin": "http://testserver"}
    first_response = client.post(endpoint, headers=headers, json={})
    repeated_response = client.post(endpoint, headers=headers, json={})
    assert first_response.status_code == repeated_response.status_code == 202
    first_recovery = first_response.json()["job"]
    assert repeated_response.json()["job"]["id"] == first_recovery["id"]
    assert first_recovery["config"]["parent_failure_revision"] == first_failure.revision
    assert first_recovery["config"]["source_run_id"] == "run-first"
    assert first_recovery["config"]["source_draft_sha256"] == hashlib.sha256(
        b"run-first"
    ).hexdigest()

    assert jobs.claim_next().id == first_recovery["id"]
    checkpoint = run_dir / "locked_chapter_resolutions" / "resolution_first.json"
    checkpoint.parent.mkdir()
    checkpoint.write_text(
        json.dumps(
            {
                "chapter_index": 21,
                "source_run_id": "run-first",
                "action": "resume_scenes",
                "scenes": [{"index": 1}],
            }
        ),
        encoding="utf-8",
    )
    jobs.recovery_succeeded(
        first_recovery["id"],
        {"ok": True, "checkpoint_path": str(checkpoint), "run": {"chapter_index": 21}},
    )
    assert jobs.resume_ready(parent.id) is True

    resumed = jobs.resume(parent.id, confirmed=True)
    assert resumed.state == "queued"
    assert jobs.claim_next().id == parent.id
    jobs.mark_running(parent.id, chapter=21, command=["python", "main.py"])
    second_failure = jobs.chapter_failed(
        parent.id,
        chapter=21,
        error={"code": "chapter_not_safe_to_continue"},
        uncertain=False,
    )
    write_run("chapter_21_second.json", "run-second", mtime_ns=2_000_000_000)

    assert second_failure.revision > first_failure.revision
    assert jobs.resume_ready(parent.id) is False
    second_response = client.post(endpoint, headers=headers, json={})
    repeated_second_response = client.post(endpoint, headers=headers, json={})
    assert second_response.status_code == repeated_second_response.status_code == 202
    second_recovery = second_response.json()["job"]
    assert second_recovery["id"] != first_recovery["id"]
    assert repeated_second_response.json()["job"]["id"] == second_recovery["id"]
    assert second_recovery["config"]["parent_failure_revision"] == second_failure.revision
    assert second_recovery["config"]["source_run_id"] == "run-second"
    assert second_recovery["config"]["source_draft_sha256"] == hashlib.sha256(
        b"run-second"
    ).hexdigest()


def test_session_pause_flag_bad_numeric_input_and_current_text_export(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="产品入口测试")
    project_id = project["id"]
    project_root = Path(project["root"])

    bad = client.post(
        f"/api/projects/{project_id}/outline-batches",
        headers={"origin": "http://testserver"},
        json={"direction": "未来计划", "chapter_count": "不是数字"},
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "value_error"

    created = client.post(
        f"/api/projects/{project_id}/outline-batches",
        headers={"origin": "http://testserver"},
        json={"chapters": [_outline(1)]},
    ).json()["batch"]
    approved_response = client.post(
        f"/api/outline-batches/{created['id']}/approve",
        headers={"origin": "http://testserver"},
        json={
            "expected_revision": created["revision"],
            "expected_sha256": created["proposal_sha256"],
        },
    )
    assert approved_response.status_code == 200, approved_response.text
    session = client.post(
        f"/api/projects/{project_id}/sessions",
        headers={"origin": "http://testserver"},
        json={"start_chapter": 1, "end_chapter": 1, "pause_after_chapter": True},
    )
    assert session.status_code == 202, session.text
    assert session.json()["job"]["pause_after_chapter"] is True
    assert session.json()["job"]["state"] == "queued"

    formal = canonical_prose_path(project_root, 1, "原稿")
    original = ("林澈沿走廊前进，示意同伴停下。\n\n" * 30).strip() + "\n"
    formal.write_text(original, encoding="utf-8")
    revised = original.replace("示意同伴停下", "抬手示意同伴停步")
    client.app.state.amendments.provider = lambda messages, call_id, evidence_root: json.dumps(
        {
            "equivalent": True,
            "confidence": 0.99,
            "critical_changes": [],
            "uncertain": False,
            "summary": "仅措辞润色。",
        },
        ensure_ascii=False,
    )
    amendment = client.post(
        "/api/chapters/1/amendments",
        headers={"origin": "http://testserver"},
        json={
            "project_id": project_id,
            "text": revised,
            "expected_base_sha256": hashlib.sha256(formal.read_bytes()).hexdigest(),
        },
    )
    assert amendment.status_code == 201, amendment.text
    assert amendment.json()["amendment"]["status"] == "active"

    exported = client.get(f"/api/projects/{project_id}/export")
    assert exported.status_code == 200
    assert "attachment" in exported.headers["content-disposition"]
    assert "抬手示意同伴停步" in exported.text
    assert "示意同伴停下" not in exported.text
    detail = client.get(f"/api/projects/{project_id}").json()
    assert detail["overview"]["word_count"] > 0
    chapters = client.get(f"/api/projects/{project_id}/chapters").json()["items"]
    assert chapters[0]["word_count"] == detail["overview"]["word_count"]


def test_pending_resume_requeues_the_same_uncertain_session_without_creating_another_job(
    tmp_path: Path,
) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="恢复原任务")
    jobs = client.app.state.jobs
    parent = jobs.create_job(
        project_id=project["id"],
        kind="generation_session",
        start_chapter=20,
        end_chapter=20,
    )
    assert jobs.claim_next().id == parent.id
    jobs.mark_running(
        parent.id,
        chapter=20,
        command=["python", "main.py", "--chapter", "20"],
    )
    paused = jobs.chapter_failed(
        parent.id,
        chapter=20,
        error={"code": "provider_call_uncertain"},
        uncertain=True,
    )
    assert paused.state == "needs_recovery"
    assert jobs.resume_ready(parent.id) is False

    recovery = jobs.create_job(
        project_id=project["id"],
        kind="locked_chapter_recovery",
        start_chapter=20,
        end_chapter=20,
        config={"parent_job_id": parent.id},
    )
    assert jobs.claim_next().id == recovery.id
    checkpoint = Path(project["root"]) / ".novelagent" / "runtime" / "locked_chapter" / "chapter_0020" / "checkpoint.json"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(
        json.dumps({"chapter_index": 20, "reusable_scene_count": 4}),
        encoding="utf-8",
    )
    jobs.recovery_succeeded(
        recovery.id,
        {
            "ok": True,
            "status": "recovered",
            "checkpoint_path": str(checkpoint),
            "run": {"chapter_index": 20},
        },
    )
    assert jobs.resume_ready(parent.id) is True
    pending = client.get(f"/api/projects/{project['id']}/pending").json()
    pending_parent = next(item for item in pending["jobs"] if item["id"] == parent.id)
    assert pending_parent["resume_ready"] is True

    before = jobs.list_jobs(project_id=project["id"], limit=100)
    response = client.post(
        f"/api/jobs/{parent.id}/resume",
        headers={"origin": "http://testserver"},
        json={"confirmed": True},
    )
    assert response.status_code == 200, response.text
    resumed = response.json()["job"]
    assert resumed["id"] == parent.id
    assert resumed["state"] == "queued"
    assert resumed["manual_confirmation_required"] is False
    assert resumed["error"] is None
    after = jobs.list_jobs(project_id=project["id"], limit=100)
    assert {item.id for item in after} == {item.id for item in before}
    assert sum(item.kind == "generation_session" for item in after) == 1
    assert any(
        event["event"] == "job.author_confirmed_resume"
        for event in jobs.events_after(parent.id)
    )
    events = jobs.events_after(parent.id)
    production = client.get(f"/api/projects/{project['id']}/production").json()
    production_parent = next(item for item in production["jobs"] if item["id"] == parent.id)
    assert production_parent["last_event_id"] == events[-1]["id"]
    project_after_resume = client.get(f"/api/projects/{project['id']}").json()["project"]
    assert project_after_resume["pending_count"] == 0
    assert project_after_resume["has_attention"] is False
    pending_after_resume = client.get(f"/api/projects/{project['id']}/pending").json()
    assert pending_after_resume["jobs"] == []
    assert pending_after_resume["chapter_20_recovery"]["recoverable"] is False
    assert pending_after_resume["chapter_20_recovery"]["active_job_id"] == parent.id
    assert client.app.state.supervisor._thread is None


def test_history_detail_reads_scoped_run_record_and_rejects_invalid_lookup(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="历史详情测试")
    other_project = _create_project(client, workspace, name="历史详情隔离测试")
    run_id = "chapter_1_history_detail"
    run_dir = Path(project["root"]) / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True)
    (run_dir / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run": {
                    "id": run_id,
                    "chapter_index": 1,
                    "status": "failed",
                    "committed": False,
                    "started_at": "2026-08-31T14:00:00+08:00",
                    "finished_at": "2026-08-31T14:01:00+08:00",
                    "repair_attempts": 0,
                    "persistence": {"state": "failed"},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    detail = client.get(
        f"/api/projects/{project['id']}/history/{run_id}?source=run"
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["source"] == "run"
    assert body["run"]["id"] == run_id
    assert body["run"]["chapter_index"] == 1
    assert body["verification"] is None

    cross_project = client.get(
        f"/api/projects/{other_project['id']}/history/{run_id}?source=run"
    )
    assert cross_project.status_code == 404
    assert client.get(f"/api/projects/{project['id']}/history/missing?source=run").status_code == 404
    assert client.get(f"/api/projects/{project['id']}/history/{run_id}?source=other").status_code == 422


def test_health_accepts_browser_selected_report_and_keeps_strict_gate_fail_closed(
    tmp_path: Path,
) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="健康门测试")
    project_id = project["id"]
    batch = client.post(
        f"/api/projects/{project_id}/outline-batches",
        headers={"origin": "http://testserver"},
        json={"chapters": [_outline(1)]},
    ).json()["batch"]
    approved = client.post(
        f"/api/outline-batches/{batch['id']}/approve",
        headers={"origin": "http://testserver"},
        json={
            "expected_revision": batch["revision"],
            "expected_sha256": batch["proposal_sha256"],
        },
    )
    assert approved.status_code == 200, approved.text
    identity = load_project_identity(Path(project["root"]))
    assert identity is not None

    health = client.get(f"/api/projects/{project_id}/health")
    assert health.status_code == 200, health.text
    before = health.json()
    assert before["production_eligible"] is False
    assert [item["label"] for item in before["checks"]] == [
        "StoryProject 结构预检",
        "Shadow 语义报告",
        "目标书校准门",
        "Strict 显式激活",
    ]

    report = build_story_state_calibration_report(
        book_id=identity.book_id,
        parser_version=SEMANTIC_PARSER_VERSION,
        semantic_schema_version=STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION,
        target_layout_profile_version="canonical-zh-1",
        evidence={
            "target_sample_count": 1,
            "format_variant_count": 2,
            "managed_round_trip_rate": 1.0,
            "required_field_exact_match_rate": 1.0,
            "authoritative_precision": 1.0,
            "supported_optional_recall": 0.95,
            "unsupported_structure_count": 2,
            "unsupported_structure_captured_count": 2,
            "consecutive_shadow_chapters": 10,
            "blocking_conflict_count": 0,
            "missing_provenance_fields": [],
        },
    )
    tampered = json.loads(json.dumps(report))
    tampered["evidence"]["authoritative_precision"] = 0.5
    rejected = client.post(
        f"/api/projects/{project_id}/health/activate",
        headers={"origin": "http://testserver"},
        json={"confirmed": True, "calibration_report": tampered},
    )
    assert rejected.status_code == 400
    assert "hash" in rejected.json()["error"]["message"]
    assert client.get(f"/api/projects/{project_id}/health").json()["production_eligible"] is False

    activated = client.post(
        f"/api/projects/{project_id}/health/activate",
        headers={"origin": "http://testserver"},
        json={"confirmed": True, "calibration_report": report},
    )
    assert activated.status_code == 200, activated.text
    payload = activated.json()
    assert payload["identity"]["story_state_mode"] == "strict"
    assert payload["health"]["production_eligible"] is True
    assert payload["health"]["score"] == 100, payload["health"]["shadow_error"]
    assert all(item["state"] == "pass" for item in payload["health"]["checks"])
