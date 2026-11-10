from __future__ import annotations

import json
from pathlib import Path

from tests.test_workbench_app import _client, _create_project, _unlock

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
