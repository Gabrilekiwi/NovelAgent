from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from core.engine.locked_chapter import recover_locked_chapter
from core.engine.locked_chapter_state import active_locked_chapter_checkpoint
from core.engine.run_record import validate_run_result
from core.execution_provenance import validate_execution_provenance
from core.model_calls import ModelCallStore
from core.state.snapshot import load_snapshot
from core.story_project.identity import load_project_identity
from core.story_project.paths import infer_next_chapter, resolve_outline, resolve_prose, scan_prose_chapters
from core.story_project.runtime import build_generation_story_project_context
from core.validator.spatial import validate_bridge_preconditions
from modules.chapter_generator.pipeline import run_chapter_pipeline
from scripts.prepare_chapter20_recovery_fixture import (
    CHAPTER_INDEX,
    EXECUTION_ID,
    FixturePreparationError,
    prepare_fixture,
)
from workbench.app import SESSION_COOKIE, create_app


FIXED_NOW = datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc)
FIXED_BOOK_ID = "fixture-book-chapter-20"


@pytest.fixture
def short_workspace() -> Iterator[Path]:
    base = Path.cwd() / ".tmp"
    base.mkdir(exist_ok=True)
    temporary = base / f"c20-test-{uuid.uuid4().hex[:8]}"
    temporary.mkdir()
    try:
        yield temporary
    finally:
        shutil.rmtree(temporary)
        assert not temporary.exists()


def test_prepared_fixture_classifies_two_scenes_without_provider_calls(
    short_workspace: Path,
) -> None:
    workspace = short_workspace / "workspace"
    books = workspace / "books"
    books.mkdir(parents=True)
    target = books / "chapter20-recovery"

    summary = prepare_fixture(
        target,
        allowed_root=workspace,
        now=FIXED_NOW,
        book_id=FIXED_BOOK_ID,
    )

    assert summary == {
        "status": "ready",
        "synthetic": True,
        "purpose": "chapter20_workbench_recovery_vertical_slice",
        "target": str(target.resolve()),
        "book_id": FIXED_BOOK_ID,
        "chapter_index": 20,
        "formal_chapter_count": 19,
        "expected_scene_count": 3,
        "expected_reusable_scene_count": 2,
        "expected_next_scene_index": 3,
        "unresolved_attempt_count": 1,
        "provider_calls": 0,
        "workbench_import_required": True,
        "fixture_manifest": str(
            target.resolve() / ".novelagent" / "validation_fixture.json"
        ),
    }
    identity = load_project_identity(target)
    assert identity is not None
    assert identity.book_id == FIXED_BOOK_ID
    assert Path(identity.root_hint) == target.resolve()
    assert set(scan_prose_chapters(target)) == set(range(1, CHAPTER_INDEX))
    assert infer_next_chapter(target) == CHAPTER_INDEX
    assert resolve_prose(target, CHAPTER_INDEX).path is None
    assert resolve_outline(target, CHAPTER_INDEX).path is not None
    fixture_snapshot = load_snapshot(
        target / ".novelagent" / "runtime" / "snapshot.json"
    )
    assert fixture_snapshot["chapter_index"] == CHAPTER_INDEX
    bridge_precheck = validate_bridge_preconditions(fixture_snapshot)
    assert bridge_precheck["ok"] is True, bridge_precheck

    run_dir = target / ".novelagent" / "runtime" / "runs"
    run_path = next(run_dir.glob("chapter_20_*.json"))
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    validate_run_result(payload)
    assert payload["run"]["status"] == "failed"
    assert payload["run"]["committed"] is False
    assert payload["run"]["accepted"] is False
    execution_evidence = payload["run"]["execution_evidence"]
    provenance_path = run_dir / Path(
        execution_evidence["provenance_artifact_ref"]
    )
    provenance = validate_execution_provenance(
        json.loads(provenance_path.read_text(encoding="utf-8"))
    )
    assert provenance["provenance_hash"] == execution_evidence["provenance_hash"]
    assert provenance["model"]["provider"] == "synthetic_fixture"

    store = ModelCallStore(
        run_dir / "executions" / EXECUTION_ID / "model_calls"
    )
    assert [
        item["attempt_id"] for item in store.list_uncertain_calls()
    ] == ["attempt-scene-03"]

    database_path = workspace / "workbench.db"
    assert not database_path.exists()
    app = create_app(
        database_path=database_path,
        runtime_root=workspace / "job-runtime",
        allowed_workspaces=[workspace],
        repo_root=Path.cwd(),
        start_worker=False,
        auto_import_active=False,
    )
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert SESSION_COOKIE in client.cookies
    imported_response = client.post(
        "/api/projects/import",
        headers={"origin": "http://testserver"},
        json={"path": str(target), "name": "第20章恢复验收（合成）"},
    )
    assert imported_response.status_code == 201, imported_response.text
    imported = imported_response.json()["project"]
    assert imported["book_id"] == FIXED_BOOK_ID
    assert imported["next_chapter"] == CHAPTER_INDEX
    pending_before = client.get(
        f"/api/projects/{imported['id']}/pending"
    ).json()["chapter_recovery"]
    assert pending_before["chapter"] == CHAPTER_INDEX
    assert pending_before["recoverable"] is True
    assert pending_before["formal_prose_exists"] is False
    assert pending_before["needs_reclassification"] is True
    queued = client.post(
        f"/api/projects/{imported['id']}/recovery",
        headers={"origin": "http://testserver"},
        json={"chapter": CHAPTER_INDEX},
    )
    assert queued.status_code == 202, queued.text
    assert queued.json()["job"]["state"] == "queued"
    assert app.state.supervisor._thread is None

    result = recover_locked_chapter(
        story_project_root=target,
        run_dir=run_dir,
        snapshot_path=target / ".novelagent" / "runtime" / "snapshot.json",
        expected_book_id=FIXED_BOOK_ID,
        language="zh-CN",
        clock=lambda: FIXED_NOW,
        id_factory=lambda: "testfixture",
    )

    assert result["ok"] is True
    assert result["action"] == "resume_scenes"
    assert result["provider_calls"] == 0
    assert result["reusable_scene_count"] == 2
    assert result["expected_scene_count"] == 3
    assert result["next_scene_index"] == 3
    checkpoint = active_locked_chapter_checkpoint(
        run_dir,
        chapter_index=CHAPTER_INDEX,
        expected_book_id=FIXED_BOOK_ID,
        language="zh-CN",
    )
    assert checkpoint is not None
    assert [scene["index"] for scene in checkpoint["scenes"]] == [1, 2]

    context = build_generation_story_project_context(
        story_project=target,
        chapter=CHAPTER_INDEX,
        snapshot=load_snapshot(
            target / ".novelagent" / "runtime" / "snapshot.json"
        ),
        memory_context={
            "source": "synthetic_fixture",
            "status": "ready",
            "items": [],
        },
        project_identity=identity,
        allow_story_state_shadow_downgrade=True,
    )
    blueprint = context.chapter_blueprint.to_dict()
    assert len(blueprint["required_beats"]) == 3
    pipeline = run_chapter_pipeline(
        "# 第 20 章恢复夹具\n\n从地下站入口直接继续。",
        chapter_index=CHAPTER_INDEX,
        dry_run=True,
        language="zh-CN",
        chapter_blueprint=blueprint,
        recovered_scene_drafts=checkpoint["scenes"],
    )
    assert len(pipeline["scene_drafts"]) == 3
    assert [
        scene.get("source_attempt_id")
        for scene in pipeline["scene_drafts"][:2]
    ] == ["attempt-scene-01", "attempt-scene-02"]
    pending_after = client.get(
        f"/api/projects/{imported['id']}/pending"
    ).json()["chapter_recovery"]
    assert pending_after["needs_reclassification"] is False
    assert pending_after["checkpoint"]["reusable_scene_count"] == 2
    assert pending_after["checkpoint"]["expected_scene_count"] == 3


def test_fixture_preparation_refuses_existing_or_outside_target(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    existing = workspace / "existing"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(FixturePreparationError, match="already exists"):
        prepare_fixture(existing, allowed_root=workspace)
    assert marker.read_text(encoding="utf-8") == "keep"

    outside = tmp_path / "outside"
    with pytest.raises(FixturePreparationError, match="inside the repository"):
        prepare_fixture(outside, allowed_root=workspace)
    assert not outside.exists()
