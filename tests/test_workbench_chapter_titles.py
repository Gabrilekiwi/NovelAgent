from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

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


def _create_project(client: TestClient, workspace: Path, name: str = "标题列表项目") -> dict:
    response = client.post(
        "/api/projects",
        headers={"origin": "http://testserver"},
        json={
            "name": name,
            "genre": "科幻",
            "synopsis": "用于工作台章节标题测试。",
            "language": "zh-CN",
            "target": str(workspace / name),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["project"]


def _write_canonical_outline(root: Path, chapter: int, title: str) -> Path:
    path = root / "大纲" / f"细纲_第{chapter:03d}章.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            f"# 第{chapter}章 {title}\n\n"
            "核心事件：用于标题提取的核心事件。\n\n"
            "## 必写节拍\n\n"
            "- 建立压力\n"
            "- 作出选择\n\n"
            "结尾压力：新的风险逼近\n"
        ),
        encoding="utf-8",
    )
    return path


def test_chapter_list_and_detail_use_outline_heading_not_canonical_filename(
    tmp_path: Path,
) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace)
    root = Path(project["root"])
    _write_canonical_outline(root, 1, "已有正文的标题")
    (root / "正文").mkdir(parents=True, exist_ok=True)
    (root / "正文" / "第001章_第1章 已有正文的标题.md").write_text(
        "正文只用来证明列表不应改回文件名。\n",
        encoding="utf-8",
    )
    _write_canonical_outline(root, 2, "三分钟之外")

    items = client.get(f"/api/projects/{project['id']}/chapters").json()["items"]
    titles = {item["chapter"]: item["title"] for item in items}
    assert titles[1] == "已有正文的标题"
    assert titles[2] == "三分钟之外"
    assert titles[2] != "细纲_第002章"

    detail = client.get(f"/api/projects/{project['id']}/chapters/2").json()
    assert detail["title"] == "三分钟之外"

    overview = client.get(f"/api/projects/{project['id']}").json()["overview"]
    assert overview["next_chapter"] == 2
    assert overview["next_outline_title"] == "三分钟之外"


def test_chapter_list_does_not_present_canonical_filename_as_title(
    tmp_path: Path,
) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="无标题细纲")
    root = Path(project["root"])
    path = root / "大纲" / "细纲_第001章.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("核心事件：只有正文没有标题行。\n", encoding="utf-8")

    items = client.get(f"/api/projects/{project['id']}/chapters").json()["items"]
    assert items[0]["chapter"] == 1
    assert items[0]["title"] == "未命名章节"
    assert "细纲_" not in items[0]["title"]
