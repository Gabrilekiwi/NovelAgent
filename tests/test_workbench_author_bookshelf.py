
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from core.story_project.model import CORE_DIRECTORY_NAMES
from workbench.app import create_app
from workbench.project_catalog import (
    annotate_related_editions,
    edition_stem,
    is_engineering_fixture_root,
    resolve_active_book,
    select_author_bookshelf,
    should_auto_import_book,
)


class _Record:
    def __init__(self, root: Path, project_id: str) -> None:
        self.root = root
        self.id = project_id


def _story_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for directory in CORE_DIRECTORY_NAMES:
        (root / directory).mkdir(exist_ok=True)
    return root


def _client(repo: Path, *, auto_import_active: bool) -> TestClient:
    app = create_app(
        database_path=repo / "data" / "workbench.db",
        runtime_root=repo / "data" / "jobs",
        allowed_workspaces=[repo],
        repo_root=repo,
        start_worker=False,
        auto_import_active=auto_import_active,
    )
    return TestClient(app)


def test_fixture_detection_uses_validation_prefix_not_a_book_title(tmp_path: Path) -> None:
    assert is_engineering_fixture_root(tmp_path / "_validation_chapter20_recovery")
    assert is_engineering_fixture_root(tmp_path / "_validation_chapter20_recovery_rerun")
    assert not is_engineering_fixture_root(tmp_path / "尸潮之下_我能提取进化词条_重写V2")
    assert not is_engineering_fixture_root(tmp_path / "日常长篇")
    official = tmp_path / "books" / "日常长篇"
    official.mkdir(parents=True)
    fixture = tmp_path / "books" / "_validation_chapter20_recovery"
    fixture.mkdir()
    (tmp_path / ".active-book").write_text("books/日常长篇", encoding="utf-8")
    records = (
        _Record(fixture, "project_fixture"),
        _Record(official, "project_official"),
    )
    visible = select_author_bookshelf(records, repository=tmp_path)
    assert [record.id for record in visible] == ["project_official"]
    with_fixtures = select_author_bookshelf(records, repository=tmp_path, include_fixtures=True)
    assert [record.id for record in with_fixtures] == ["project_official", "project_fixture"]
    assert should_auto_import_book(official, active_book=official) is True
    assert should_auto_import_book(fixture, active_book=official) is False
    assert resolve_active_book(tmp_path) == official.resolve()


def test_author_bookshelf_hides_auto_imported_validation_books(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    books = repo / "books"
    official = _story_project(books / "续写作品")
    earlier = _story_project(books / "旧版作品")
    fixture = _story_project(books / "_validation_chapter20_recovery")
    leftover = _story_project(books / "_validation_chapter20_recovery_rerun")
    (repo / ".active-book").write_text("books/续写作品", encoding="utf-8")
    (repo / "data").mkdir()

    client = _client(repo, auto_import_active=True)
    listing = client.get("/api/projects")
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    names = [item["name"] for item in items]
    roots = [Path(item["root"]).name for item in items]
    assert names[0] == "续写作品"
    assert roots == ["续写作品", "旧版作品"]
    assert all(item["engineering_fixture"] is False for item in items)
    assert "_validation_chapter20_recovery" not in roots
    assert "_validation_chapter20_recovery_rerun" not in roots

    # Fixtures remain on disk for tests/engineering, but startup does not register them.
    engineering = client.get("/api/projects", params={"include_fixtures": "true"})
    assert engineering.status_code == 200, engineering.text
    engineering_roots = {Path(item["root"]).name for item in engineering.json()["items"]}
    assert engineering_roots == {"续写作品", "旧版作品"}
    assert official.is_dir() and earlier.is_dir() and fixture.is_dir() and leftover.is_dir()


def test_explicit_fixture_import_stays_addressable_but_off_the_author_shelf(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    books = repo / "books"
    official = _story_project(books / "续写作品")
    fixture = _story_project(books / "_validation_chapter20_recovery")
    (repo / ".active-book").write_text("books/续写作品", encoding="utf-8")
    (repo / "data").mkdir()

    client = _client(repo, auto_import_active=False)
    unlocked = client.get("/")
    assert unlocked.status_code == 200
    official_item = client.post(
        "/api/projects/import",
        headers={"origin": "http://testserver"},
        json={"path": str(official)},
    )
    assert official_item.status_code == 201, official_item.text
    official_id = official_item.json()["project"]["id"]
    fixture_item = client.post(
        "/api/projects/import",
        headers={"origin": "http://testserver"},
        json={"path": str(fixture)},
    )
    assert fixture_item.status_code == 201, fixture_item.text
    fixture_payload = fixture_item.json()["project"]
    fixture_id = fixture_payload["id"]
    assert fixture_payload["engineering_fixture"] is True

    listing = client.get("/api/projects")
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert [item["id"] for item in items] == [official_id]
    assert items[0]["engineering_fixture"] is False

    detail = client.get(f"/api/projects/{fixture_id}")
    assert detail.status_code == 200, detail.text
    project = detail.json()["project"]
    assert project["id"] == fixture_id
    assert project["engineering_fixture"] is True
    assert Path(project["root"]).name == "_validation_chapter20_recovery"

    official_again = client.get(f"/api/projects/{official_id}")
    assert official_again.status_code == 200, official_again.text
    assert official_again.json()["project"]["id"] == official_id

    engineering = client.get("/api/projects", params={"include_fixtures": "true"})
    assert engineering.status_code == 200, engineering.text
    engineering_ids = [item["id"] for item in engineering.json()["items"]]
    assert official_id in engineering_ids
    assert fixture_id in engineering_ids


def test_edition_stem_groups_rewrite_and_version_suffixes() -> None:
    assert edition_stem("日常长篇") == "日常长篇"
    assert edition_stem("日常长篇_重写V2") == "日常长篇"
    assert edition_stem(Path("/tmp/日常长篇_V2")) == "日常长篇"
    assert edition_stem("日常长篇_重写") == "日常长篇"
    assert edition_stem("另一本作品") == "另一本作品"


def test_related_editions_label_previous_book_without_hardcoded_title(tmp_path: Path) -> None:
    official = tmp_path / "books" / "日常长篇_重写V2"
    earlier = tmp_path / "books" / "日常长篇"
    other = tmp_path / "books" / "另一本作品"
    official.mkdir(parents=True)
    earlier.mkdir()
    other.mkdir()
    (tmp_path / ".active-book").write_text("books/日常长篇_重写V2", encoding="utf-8")
    items = annotate_related_editions(
        [
            {"id": "project_official", "root": str(official), "title": "日常长篇_重写V2"},
            {"id": "project_earlier", "root": str(earlier), "title": "日常长篇"},
            {"id": "project_other", "root": str(other), "title": "另一本作品"},
        ],
        repository=tmp_path,
    )
    by_id = {item["id"]: item for item in items}
    assert by_id["project_official"]["is_active_book"] is True
    assert by_id["project_official"]["edition_role"] == "current"
    assert by_id["project_official"]["edition_label"] == "当前作品"
    assert by_id["project_earlier"]["is_active_book"] is False
    assert by_id["project_earlier"]["edition_role"] == "previous"
    assert by_id["project_earlier"]["edition_label"] == "上一版"
    assert by_id["project_earlier"]["current_edition_id"] == "project_official"
    assert by_id["project_earlier"]["current_edition_title"] == "日常长篇_重写V2"
    assert by_id["project_other"]["edition_role"] is None
    assert by_id["project_other"]["edition_label"] is None
    assert by_id["project_other"]["is_active_book"] is False


def test_author_bookshelf_marks_similarly_named_previous_edition(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    books = repo / "books"
    official = _story_project(books / "日常长篇_重写V2")
    earlier = _story_project(books / "日常长篇")
    fixture = _story_project(books / "_validation_chapter20_recovery")
    (repo / ".active-book").write_text("books/日常长篇_重写V2", encoding="utf-8")
    (repo / "data").mkdir()

    client = _client(repo, auto_import_active=True)
    listing = client.get("/api/projects")
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert [Path(item["root"]).name for item in items] == ["日常长篇_重写V2", "日常长篇"]
    official_item, earlier_item = items
    assert official_item["is_active_book"] is True
    assert official_item["edition_role"] == "current"
    assert official_item["edition_label"] == "当前作品"
    assert earlier_item["is_active_book"] is False
    assert earlier_item["edition_role"] == "previous"
    assert earlier_item["edition_label"] == "上一版"
    assert earlier_item["current_edition_id"] == official_item["id"]
    assert earlier_item["current_edition_title"] == official_item["title"]
    assert official.is_dir() and earlier.is_dir() and fixture.is_dir()

    earlier_detail = client.get(f"/api/projects/{earlier_item['id']}")
    assert earlier_detail.status_code == 200, earlier_detail.text
    earlier_project = earlier_detail.json()["project"]
    assert earlier_project["edition_role"] == "previous"
    assert earlier_project["edition_label"] == "上一版"
    assert earlier_project["current_edition_id"] == official_item["id"]
    assert earlier_project["current_edition_title"] == official_item["title"]

    official_detail = client.get(f"/api/projects/{official_item['id']}")
    assert official_detail.status_code == 200, official_detail.text
    official_project = official_detail.json()["project"]
    assert official_project["edition_role"] == "current"
    assert official_project["edition_label"] == "当前作品"
    assert official_project["is_active_book"] is True
