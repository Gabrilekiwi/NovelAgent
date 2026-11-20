from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from tests.test_workbench_app import _client, _create_project, _unlock
from workbench.jobs import _compact_result, _provider_uncertain_user_message

STATIC = Path(__file__).resolve().parents[1] / "workbench" / "static"


def test_pending_ui_exposes_force_reset_after_safe_classification_contract() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    stream = (STATIC / "workbench.js").read_text(encoding="utf-8")

    assert 'data-force-reset-panel hidden' in html
    assert 'data-force-reset-form' in html
    assert 'name="force_reset" value="true"' in html
    assert "结束本章并允许新尝试" in html
    assert "不删除失败 RunRecord" in html or "不会删除证据" in html
    assert "不会改写正式正文" in html

    assert 'one("[data-force-reset-panel]")' in spa
    assert 'issue.checkpoint.action !== "reset"' in spa
    assert "forceResetAllowed" in spa
    assert "classifiedCheckpoint || uncertainWithoutRun" in spa
    assert "close-stalled-session" in spa
    assert "uncertain_without_run" in spa
    assert "reset_cleared" in spa
    assert "上次模型调用结果不确定，且没有留下完整章节运行记录" in spa
    assert "失败锁已解除，没有可复用的完整章节记录" in spa
    assert "关闭这次停住的生产任务后即可重新开写" in spa
    assert "第 2 个场景的 JSON 格式错误" not in spa
    assert "场景响应的 JSON 格式错误" in spa
    assert "释放新的 Cohort 生产位" not in html
    assert "关闭这次停住的生产任务" in html
    assert "force_reset" in stream
    assert 'form.matches("[data-force-reset-form]")' in spa


def test_project_recovery_api_queues_force_reset_without_worker(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="force-reset-ui")
    project_root = Path(project["root"])
    for chapter in range(1, 20):
        (project_root / "正文" / f"第{chapter:03d}章_测试.md").write_text(
            f"第{chapter}章正文",
            encoding="utf-8",
        )
    (project_root / "大纲" / "细纲_第020章.md").write_text(
        "# 第20章\n\n失败后需要强制重置。\n",
        encoding="utf-8",
    )

    response = client.post(
        f"/api/projects/{project['id']}/recovery",
        headers={"origin": "http://testserver"},
        json={"chapter": 20, "force_reset": True},
    )
    assert response.status_code == 202, response.text
    job = response.json()["job"]
    assert job["kind"] == "locked_chapter_recovery"
    assert job["state"] == "queued"
    assert job["config"]["force_reset"] is True
    assert client.app.state.supervisor._thread is None
    assert not (project_root / "正文" / "第020章_测试.md").exists()


def test_parent_bound_recovery_api_queues_force_reset(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="force-reset-parent")
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
                }
            }
        ),
        encoding="utf-8",
    )

    response = client.post(
        f"/api/jobs/{parent.id}/recover",
        headers={"origin": "http://testserver"},
        json={"force_reset": True},
    )
    assert response.status_code == 202, response.text
    recovery = response.json()["job"]
    assert recovery["kind"] == "locked_chapter_recovery"
    assert recovery["config"]["force_reset"] is True
    assert recovery["config"]["parent_job_id"] == parent.id



def _short_tmp(name: str) -> Path:
    root = Path(__file__).resolve().parents[1] / ".tmp" / "fr" / f"{name}_{uuid.uuid4().hex[:8]}"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    return root


def _write_unbound_uncertain_intent(project_root: Path) -> dict[str, str]:
    from datetime import datetime, timezone

    from core.model_calls import ModelCallStore

    store = ModelCallStore(
        Path(project_root) / ".novelagent" / "runtime" / "runs" / "executions"
        / "execution_unbound_pending" / "model_calls"
    )
    store.intents_dir.mkdir(parents=True, exist_ok=True)
    intent = store.create_intent(
        call_id="openai-chapter_generation-scene-0004-primary-a1",
        attempt_id="attempt-missing",
        provider="openai",
        model="gpt-test",
        stage="chapter_generation",
        budget_reservation={"reserved_input_tokens": 10, "reserved_output_tokens": 100},
        request={"messages": [{"role": "user", "content": "scene 4"}]},
        created_at=datetime(2026, 11, 10, 8, 48, 59, tzinfo=timezone.utc),
    )
    return {
        "execution_id": "execution_unbound_pending",
        "call_id": intent["call_id"],
        "attempt_id": intent["attempt_id"],
    }


def _ensure_snapshot(project_root: Path, chapter: int) -> None:
    from core.runtime_paths import RuntimePaths
    from core.state.snapshot import save_snapshot

    runtime = RuntimePaths.for_story_project(project_root)
    runtime.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    save_snapshot({"chapter_index": int(chapter)}, runtime.snapshot_path)


def test_pending_api_marks_unbound_uncertain_execution_recoverable() -> None:
    client, workspace = _client(_short_tmp("unb"))
    _unlock(client)
    project = _create_project(client, workspace, name="unbound-uncertain")
    root = Path(project["root"])
    written = _write_unbound_uncertain_intent(root)
    _ensure_snapshot(root, project["next_chapter"])

    pending = client.get(f"/api/projects/{project['id']}/pending")
    assert pending.status_code == 200, pending.text
    recovery = pending.json()["chapter_recovery"]
    assert recovery["chapter"] == project["next_chapter"]
    assert recovery["latest_run"] is None
    assert recovery["formal_prose_exists"] is False
    assert recovery["checkpoint"] is None
    assert recovery["recoverable"] is True
    assert recovery["uncertain_without_run"] is True
    assert recovery["needs_reclassification"] is True
    assert recovery["reset_cleared"] is False
    assert recovery["uncertain_call"]["execution_id"] == written["execution_id"]
    assert recovery["uncertain_call"]["attempt_id"] == written["attempt_id"]


def test_pending_api_force_reset_still_queues_for_unbound_uncertain_execution() -> None:
    client, workspace = _client(_short_tmp("unbq"))
    _unlock(client)
    project = _create_project(client, workspace, name="unbound-queue")
    root = Path(project["root"])
    _write_unbound_uncertain_intent(root)
    _ensure_snapshot(root, project["next_chapter"])

    response = client.post(
        f"/api/projects/{project['id']}/recovery",
        headers={"origin": "http://testserver"},
        json={"chapter": project["next_chapter"], "force_reset": True},
    )
    assert response.status_code == 202, response.text
    job = response.json()["job"]
    assert job["kind"] == "locked_chapter_recovery"
    assert job["config"]["force_reset"] is True


def test_pending_api_marks_reset_cleared_after_unbound_classification() -> None:
    from core.engine.locked_chapter import recover_locked_chapter
    from core.runtime_paths import RuntimePaths
    from core.story_project.identity import load_project_identity

    client, workspace = _client(_short_tmp("clr"))
    _unlock(client)
    project = _create_project(client, workspace, name="reset-cleared")
    root = Path(project["root"])
    _write_unbound_uncertain_intent(root)
    _ensure_snapshot(root, project["next_chapter"])
    runtime = RuntimePaths.for_story_project(root)
    identity = load_project_identity(root)
    result = recover_locked_chapter(
        story_project_root=root,
        run_dir=runtime.run_dir,
        snapshot_path=runtime.snapshot_path,
        expected_book_id=identity.book_id,
        language="zh-CN",
    )
    assert result["action"] == "reset"

    pending = client.get(f"/api/projects/{project['id']}/pending")
    assert pending.status_code == 200, pending.text
    recovery = pending.json()["chapter_recovery"]
    assert recovery["reset_cleared"] is True
    assert recovery["uncertain_without_run"] is False
    assert recovery["recoverable"] is False
    assert recovery["checkpoint"]["action"] == "reset"


def test_closing_paused_parent_after_reset_cleared_allows_new_session() -> None:
    from core.engine.locked_chapter import recover_locked_chapter
    from core.runtime_paths import RuntimePaths
    from core.story_project.identity import load_project_identity
    from workbench.jobs import JobError

    client, workspace = _client(_short_tmp("cls"))
    _unlock(client)
    project = _create_project(client, workspace, name="close-parent")
    root = Path(project["root"])
    jobs = client.app.state.jobs
    parent = jobs.create_job(
        project_id=project["id"],
        kind="generation_session",
        start_chapter=1,
        end_chapter=2,
        config={},
    )
    assert jobs.claim_next().id == parent.id
    jobs.mark_running(parent.id, chapter=1, command=["python", "main.py"])
    jobs.chapter_failed(
        parent.id,
        chapter=1,
        error={"code": "provider_call_uncertain", "type": "ProviderCallUncertainError"},
        uncertain=True,
    )
    _write_unbound_uncertain_intent(root)
    _ensure_snapshot(root, 1)
    runtime = RuntimePaths.for_story_project(root)
    identity = load_project_identity(root)
    result = recover_locked_chapter(
        story_project_root=root,
        run_dir=runtime.run_dir,
        snapshot_path=runtime.snapshot_path,
        expected_book_id=identity.book_id,
        language="zh-CN",
    )
    assert result["action"] == "reset"

    pending = client.get(f"/api/projects/{project['id']}/pending")
    assert pending.status_code == 200, pending.text
    payload = pending.json()
    assert payload["chapter_recovery"]["reset_cleared"] is True
    paused = [item for item in payload.get("items") or payload.get("jobs") or [] if item.get("id") == parent.id or item.get("job_id") == parent.id]
    # The parent remains paused until the author closes it; a new session must fail closed.
    refreshed = jobs.get(parent.id)
    assert refreshed.state in {"needs_recovery", "awaiting_input"}
    try:
        jobs.create_job(
            project_id=project["id"],
            kind="generation_session",
            start_chapter=1,
            end_chapter=2,
            config={"quality_policy": "strict"},
        )
    except JobError as exc:
        assert "unfinished session" in str(exc)
    else:
        raise AssertionError("new session must not start while the parent is still paused")

    cancelled = client.post(
        f"/api/jobs/{parent.id}/cancel",
        headers={"origin": "http://testserver"},
        json={},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["job"]["state"] == "cancelled"

    nxt = jobs.create_job(
        project_id=project["id"],
        kind="generation_session",
        start_chapter=1,
        end_chapter=2,
        config={"quality_policy": "strict"},
    )
    assert nxt.id != parent.id
    assert nxt.state in {"queued", "held"}


def test_compact_result_keeps_locked_chapter_error_message() -> None:
    compact = _compact_result(
        {
            "ok": False,
            "error": {
                "type": "LockedChapterRecoveryError",
                "message": "an unresolved model execution has no durable chapter run; automatic recovery stopped safely",
            },
        }
    )
    assert compact["ok"] is False
    assert compact["error"]["type"] == "LockedChapterRecoveryError"
    assert "no durable chapter run" in compact["error"]["message"]


def test_provider_uncertain_user_message_from_stderr() -> None:
    message = _provider_uncertain_user_message(
        "core.model_call_runtime.ProviderCallUncertainError: provider_call_uncertain: "
        "intent openai-chapter_generation-scene-0004-primary-a1 exists without a durable receipt"
    )
    assert message is not None
    assert "不要盲目重试" in message
    assert _provider_uncertain_user_message("ValueError: other failure") is None


def test_failed_recovery_job_can_be_retried_with_same_idempotency_key(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="force-reset-retry")
    jobs = client.app.state.jobs
    first = jobs.create_job(
        project_id=project["id"],
        kind="locked_chapter_recovery",
        start_chapter=1,
        end_chapter=1,
        config={"force_reset": True},
        idempotency_key="recover-retry-same-key",
    )
    jobs.fail_job(first.id, {"code": "locked_chapter_recovery_failed", "reason": "previous attempt"})
    second = jobs.create_job(
        project_id=project["id"],
        kind="locked_chapter_recovery",
        start_chapter=1,
        end_chapter=1,
        config={"force_reset": True},
        idempotency_key="recover-retry-same-key",
    )
    assert second.id != first.id
    assert second.state == "queued"
