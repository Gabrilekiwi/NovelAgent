from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.story_project.paths import canonical_prose_path
from workbench.app import SESSION_COOKIE, create_app
from workbench.database import WorkbenchDatabase
from workbench.jobs import JobError, JobStore
from workbench.projects import ProjectRegistry


REPOSITORY = Path(__file__).resolve().parents[1]


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


def _create_project(client: TestClient, workspace: Path, name: str = "正式提交换复草稿") -> dict:
    response = client.post(
        "/api/projects",
        headers={"origin": "http://testserver"},
        json={
            "name": name,
            "genre": "科幻",
            "synopsis": "用于验证正式提交后不再把恢复草稿留在待处理。",
            "language": "zh-CN",
            "target": str(workspace / name),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["project"]


def _job_store(tmp_path: Path) -> tuple[JobStore, str, Path]:
    workspace = tmp_path / "books"
    workspace.mkdir()
    database = WorkbenchDatabase(tmp_path / "workbench.db")
    registry = ProjectRegistry(database, allowed_workspaces=[workspace])
    project = registry.create_project(workspace / "test-book", name="测试作品")
    return JobStore(database), project.id, Path(project.root)


def _running_job(store: JobStore, project_id: str, *, chapter: int = 1, end_chapter: int = 1):
    queued = store.create_job(
        project_id=project_id,
        kind="generation_session",
        start_chapter=chapter,
        end_chapter=end_chapter,
    )
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == queued.id
    return store.mark_running(
        queued.id,
        chapter=chapter,
        command=["python", "main.py", "--chapter", str(chapter)],
    )


def _write_formal_prose(project_root: Path, chapter: int, text: str = "正式提交的章节正文。\n") -> Path:
    path = canonical_prose_path(project_root, chapter, f"第{chapter}章正式正文")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_formal_commit_archives_recovery_draft_and_drops_it_from_pending(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace)
    store = client.app.state.jobs
    kept_text = "这是第 1 章已校验的恢复稿，提交后仍应保留文本。\n"
    draft = store.upsert_draft(
        project_id=project["id"],
        chapter=1,
        text=kept_text,
        source_run_id="chapter_1_source",
    )
    draft = store.mark_draft_validated(
        draft.id,
        expected_revision=draft.revision,
        validation={"ok": True, "reason": "manual_repaired_draft_provided", "status": "recovered"},
    )
    assert draft.state == "validated"

    pending_before = client.get(f"/api/projects/{project['id']}/pending")
    assert pending_before.status_code == 200, pending_before.text
    assert [item["id"] for item in pending_before.json()["drafts"]] == [draft.id]
    chapter_before = client.get(f"/api/projects/{project['id']}/chapters/1")
    assert chapter_before.status_code == 200, chapter_before.text
    assert chapter_before.json()["retained_author_draft"]["id"] == draft.id
    assert chapter_before.json()["retained_author_draft"]["state"] == "validated"

    running = _running_job(store, project["id"])
    _write_formal_prose(Path(project["root"]), 1, "第一章已经正式落盘。\n")
    committed = store.chapter_succeeded(
        running.id,
        chapter=1,
        result={
            "status": "success",
            "committed": True,
            "run": {"id": "run-1", "chapter_index": 1, "committed": True, "status": "committed"},
        },
        verification={"ok": True, "receipt": True, "readback": True},
    )
    assert committed.state == "completed"

    archived = store.get_draft(draft.id)
    assert archived.state == "archived"
    assert archived.text == kept_text
    assert archived.validation == {
        "ok": True,
        "reason": "manual_repaired_draft_provided",
        "status": "recovered",
    }

    pending_after = client.get(f"/api/projects/{project['id']}/pending")
    assert pending_after.status_code == 200, pending_after.text
    assert pending_after.json()["drafts"] == []
    chapter_after = client.get(f"/api/projects/{project['id']}/chapters/1")
    assert chapter_after.status_code == 200, chapter_after.text
    assert chapter_after.json().get("has_prose") is True
    assert "retained_author_draft" not in chapter_after.json()


def test_uncommitted_chapter_draft_is_not_archived(tmp_path: Path) -> None:
    store, project_id, project_root = _job_store(tmp_path)
    draft = store.upsert_draft(
        project_id=project_id,
        chapter=2,
        text="第 2 章尚未正式提交，恢复稿必须继续待处理。\n",
    )
    _write_formal_prose(project_root, 1, "只有第 1 章已经正式落盘。\n")

    assert store.archive_recovery_draft_for_committed_chapter(project_id, 2) is None
    assert store.archive_settled_recovery_drafts(project_id) == ()
    remaining = store.get_draft(draft.id)
    assert remaining.state == "editing"
    assert remaining.text == "第 2 章尚未正式提交，恢复稿必须继续待处理。\n"
    assert [item.id for item in store.list_actionable_drafts(project_id)] == [draft.id]


def test_pending_reconciles_historical_validated_draft_after_formal_prose_exists(
    tmp_path: Path,
) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="历史遗留恢复草稿")
    store = client.app.state.jobs
    kept_text = "这是第 60 章遗留的已校验恢复稿。\n"
    draft = store.upsert_draft(
        project_id=project["id"],
        chapter=60,
        text=kept_text,
        source_run_id="chapter_60_source",
    )
    draft = store.mark_draft_validated(
        draft.id,
        expected_revision=draft.revision,
        validation={"ok": True, "reason": "manual_repaired_draft_provided"},
    )
    _write_formal_prose(Path(project["root"]), 60, "第60章已经正式落盘。\n")

    pending = client.get(f"/api/projects/{project['id']}/pending")
    assert pending.status_code == 200, pending.text
    assert pending.json()["drafts"] == []

    archived = store.get_draft(draft.id)
    assert archived.state == "archived"
    assert archived.text == kept_text
    chapter = client.get(f"/api/projects/{project['id']}/chapters/60")
    assert chapter.status_code == 200, chapter.text
    assert chapter.json().get("has_prose") is True
    assert "retained_author_draft" not in chapter.json()


def test_draft_actions_fail_closed_after_formal_commit(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="提交后禁止再操作恢复稿")
    store = client.app.state.jobs
    draft = store.upsert_draft(
        project_id=project["id"],
        chapter=1,
        text="提交前的已校验恢复稿。\n",
    )
    draft = store.mark_draft_validated(
        draft.id,
        expected_revision=draft.revision,
        validation={"ok": True, "status": "recovered"},
    )
    running = _running_job(store, project["id"])
    parent_id = running.id
    _write_formal_prose(Path(project["root"]), 1)
    store.chapter_succeeded(
        running.id,
        chapter=1,
        result={"status": "success", "committed": True, "run": {"id": "run-1", "chapter_index": 1}},
        verification={"ok": True},
    )
    archived = store.get_draft(draft.id)
    assert archived.state == "archived"

    updated = client.put(
        f"/api/drafts/{draft.id}",
        headers={"origin": "http://testserver"},
        json={"text": "不应改写已归档恢复稿。\n", "expected_revision": archived.revision},
    )
    assert updated.status_code == 409, updated.text
    assert "archived after formal commit" in updated.json()["error"]["message"]

    validated = client.post(
        f"/api/drafts/{draft.id}/validate",
        headers={"origin": "http://testserver"},
        json={"job_id": parent_id},
    )
    assert validated.status_code == 409, validated.text

    confirmed = client.post(
        f"/api/drafts/{draft.id}/confirm",
        headers={"origin": "http://testserver"},
        json={"job_id": parent_id, "confirmed": True},
    )
    assert confirmed.status_code == 409, confirmed.text
    assert store.get_draft(draft.id).text == "提交前的已校验恢复稿。\n"

    with pytest.raises(JobError, match="already has formal committed prose"):
        store.upsert_draft(
            project_id=project["id"],
            chapter=1,
            text="不能在已正式提交的章节上再写入恢复草稿。\n",
        )


def test_restart_reconcile_archives_draft_only_when_formal_prose_exists(
    tmp_path: Path,
) -> None:
    store, project_id, project_root = _job_store(tmp_path)
    running = _running_job(store, project_id, chapter=1, end_chapter=1)
    draft = store.upsert_draft(
        project_id=project_id,
        chapter=1,
        text="重启核对前提交的恢复稿。\n",
    )
    paused = store.chapter_failed(
        running.id,
        chapter=1,
        uncertain=True,
        error={"code": "engine_result_unreadable"},
    )
    assert paused.state == "needs_recovery"

    # Fail closed: a receipt-verified restart reconcile still must not archive
    # a draft whose chapter has no formal committed prose.
    store.reconcile_committed_after_restart(
        paused.id,
        chapter=1,
        result={
            "committed": True,
            "run": {"id": "run-1", "chapter_index": 1, "committed": True, "status": "committed"},
        },
        verification={"ok": True},
    )
    assert store.get_draft(draft.id).state == "editing"
    assert store.get_draft(draft.id).text == "重启核对前提交的恢复稿。\n"

    # After formal prose exists, a later settlement archives the draft and
    # keeps the author text.
    _write_formal_prose(project_root, 1)
    archived = store.archive_recovery_draft_for_committed_chapter(project_id, 1)
    assert archived is not None
    assert archived.state == "archived"
    assert archived.text == "重启核对前提交的恢复稿。\n"
