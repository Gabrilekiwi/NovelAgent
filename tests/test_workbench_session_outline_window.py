from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from core.story_project.paths import canonical_prose_path
from workbench.app import SESSION_COOKIE, create_app


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


def _create_project(client: TestClient, workspace: Path, name: str = "生产窗口测试") -> dict:
    response = client.post(
        "/api/projects",
        headers={"origin": "http://testserver"},
        json={
            "name": name,
            "genre": "科幻",
            "synopsis": "用于已批准章纲剩余范围测试。",
            "language": "zh-CN",
            "target": str(workspace / name),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["project"]


def _outline(chapter: int) -> dict:
    return {
        "chapter_index": chapter,
        "title": f"第{chapter}章测试",
        "core_event": f"主角解决第{chapter}章的核心冲突",
        "required_beats": ["建立压力", "作出选择"],
        "ending_pressure": "新的风险逼近",
    }


def _approve_batch(client: TestClient, project_id: str, start: int, end: int) -> dict:
    created = client.post(
        f"/api/projects/{project_id}/outline-batches",
        headers={"origin": "http://testserver"},
        json={"chapters": [_outline(chapter) for chapter in range(start, end + 1)]},
    ).json()["batch"]
    approved = client.post(
        f"/api/outline-batches/{created['id']}/approve",
        headers={"origin": "http://testserver"},
        json={
            "expected_revision": created["revision"],
            "expected_sha256": created["proposal_sha256"],
        },
    )
    assert approved.status_code == 200, approved.text
    return approved.json()["batch"]


def _write_formal_chapter(project_root: Path, chapter: int) -> None:
    path = canonical_prose_path(project_root, chapter, f"第{chapter}章测试")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(("林澈沿走廊前进，示意同伴停下。\n\n" * 20).strip() + "\n", encoding="utf-8")


def test_production_defaults_follow_remaining_approved_outline_chapters(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace)
    project_id = project["id"]
    batch = _approve_batch(client, project_id, 1, 10)
    _write_formal_chapter(Path(project["root"]), 1)

    production = client.get(f"/api/projects/{project_id}/production")
    assert production.status_code == 200, production.text
    defaults = production.json()["session_defaults"]
    assert defaults == {
        "start_chapter": 2,
        "chapter_count": 9,
        "end_chapter": 10,
        "max_chapter_count": 9,
        "outline_batch_id": batch["id"],
        "outline_batch_start": 1,
        "outline_batch_end": 10,
        "covered": True,
    }


def test_default_ten_chapter_session_stops_at_approved_batch_end(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace)
    project_id = project["id"]
    batch = _approve_batch(client, project_id, 1, 10)
    _write_formal_chapter(Path(project["root"]), 1)

    session = client.post(
        f"/api/projects/{project_id}/sessions",
        headers={"origin": "http://testserver"},
        json={"start_chapter": 2, "chapter_count": 10},
    )
    assert session.status_code == 202, session.text
    job = session.json()["job"]
    assert job["start_chapter"] == 2
    assert job["end_chapter"] == 10
    assert job["config"]["outline_batch_id"] == batch["id"]
    assert job["state"] == "queued"


def test_explicit_end_past_approved_batch_explains_remaining_window(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace)
    project_id = project["id"]
    _approve_batch(client, project_id, 1, 10)
    _write_formal_chapter(Path(project["root"]), 1)

    failed = client.post(
        f"/api/projects/{project_id}/sessions",
        headers={"origin": "http://testserver"},
        json={"start_chapter": 2, "end_chapter": 11},
    )
    assert failed.status_code == 400, failed.text
    message = failed.json()["error"]["message"]
    assert "只覆盖第 1—10 章" in message
    assert "可从第 2 章连续写到第 10 章（共 9 章）" in message
    assert "尚未批准" in message


def test_session_without_approved_coverage_still_fails_closed(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace)
    project_id = project["id"]
    _approve_batch(client, project_id, 1, 10)

    failed = client.post(
        f"/api/projects/{project_id}/sessions",
        headers={"origin": "http://testserver"},
        json={"start_chapter": 11, "chapter_count": 1},
    )
    assert failed.status_code == 400, failed.text
    assert failed.json()["error"]["message"] == "第 11—11 章必须先由一个已批准且冻结的章纲批次完整覆盖"


def test_launch_form_uses_session_defaults_and_explains_remaining_window() -> None:
    html = (REPOSITORY / "workbench/static/index.html").read_text(encoding="utf-8")
    script = (REPOSITORY / "workbench/static/workbench-spa.js").read_text(encoding="utf-8")
    assert 'data-session-window-note' in html
    assert "applySessionOutlineWindow" in script
    assert "approvedBatchWindow" in script
    assert "当前已批准章纲还可写第" in script
    assert "下一章还没有已批准且冻结的章纲，不能开始生产。" in script
