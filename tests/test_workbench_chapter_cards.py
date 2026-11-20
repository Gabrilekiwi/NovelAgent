from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import uuid

from fastapi.testclient import TestClient
import pytest

from core.memory_v2 import (
    apply_genesis_event,
    create_genesis_memory_batch,
    write_memory_event_batch,
)
from core.memory_v2.canonical import canonical_json_hash
from core.runtime_paths import RuntimePaths
from core.schema import SchemaValidationError, validate_schema
from core.story_project.activation import (
    activate_story_state,
    build_story_state_calibration_report,
)
from core.story_project.authority import activate_event_authority, project_identity_sha256
from core.story_project.identity import ensure_project_identity
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.paths import canonical_outline_path
from core.story_project.semantic_contracts import STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION
from core.story_project.semantic_parser import SEMANTIC_PARSER_VERSION
from workbench.app import create_app
from workbench.chapter_cards import ChapterCardConflict, ChapterCardError


REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURE = REPOSITORY / "tests" / "fixtures" / "story_project_semantics" / "cases" / "synthetic_standard" / "book"


def _qualified_report(book_id: str) -> dict:
    return build_story_state_calibration_report(
        book_id=book_id,
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
            "unsupported_structure_count": 1,
            "unsupported_structure_captured_count": 1,
            "consecutive_shadow_chapters": 10,
            "blocking_conflict_count": 0,
            "missing_provenance_fields": [],
        },
        generated_at="2026-09-03T00:00:00+00:00",
    )


def _strict_book(tmp_path: Path, *, explicit_read_set: bool = False) -> Path:
    root = tmp_path / "books" / "chapter-card-book"
    root.parent.mkdir()
    shutil.copytree(FIXTURE, root)
    outline = root / "大纲" / "细纲_第002章.md"
    if explicit_read_set:
        contract = {
            "schema_version": "1.0",
            "mode": "explicit",
            "chapter_index": 2,
            "required_state_item_ids": [
                "characters/lin-che",
                "locations/old-station",
            ],
            "required_event_item_ids": ["events/chapter-0001-ending"],
            "continuity": {
                "last_scene_location": "旧城站入口",
                "last_scene_character_ids": ["lin-che", "zhou-lan"],
                "required_opening_bridge": "从闸门后的求救声直接续接。",
            },
            "narrative_constraints": [
                {
                    "constraint_id": "signal-source",
                    "lifecycle_action": "active",
                    "instruction": "不得提前揭示失踪信号源的完整身份。",
                }
            ],
            "expected_new_entities": [
                {
                    "kind": "device",
                    "entity_id": "gate-controller",
                    "display_name": "闸门控制器",
                }
            ],
        }
        outline.write_text(
            outline.read_text(encoding="utf-8")
            + "\n```novelagent-chapter-context\n"
            + json.dumps(contract, ensure_ascii=False, indent=2)
            + "\n```\n",
            encoding="utf-8",
        )
    identity = ensure_project_identity(root)
    activate_story_state(root, _qualified_report(identity.book_id))
    return root


def _client_for_book(
    tmp_path: Path,
    root: Path,
    *,
    metadata: dict | None = None,
) -> tuple[TestClient, dict]:
    app = create_app(
        database_path=tmp_path / "workbench.db",
        runtime_root=tmp_path / "job-runtime",
        allowed_workspaces=[root.parent],
        repo_root=REPOSITORY,
        start_worker=False,
        auto_import_active=False,
    )
    record = app.state.projects.import_project(
        root,
        name="章节卡测试书",
        metadata=metadata or {},
    )
    return TestClient(app), record.to_dict()


def _tree_inventory(root: Path) -> dict[str, tuple[str, str | None]]:
    result: dict[str, tuple[str, str | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            result[relative] = ("directory", None)
        elif path.is_file():
            result[relative] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
    return result


def test_chapter_card_api_is_compact_hash_bound_and_zero_write(tmp_path: Path) -> None:
    root = _strict_book(tmp_path)
    client, project = _client_for_book(tmp_path, root)
    before = _tree_inventory(root)

    response = client.get(f"/api/projects/{project['id']}/chapters/2/card")

    assert response.status_code == 200, response.text
    card = response.json()["chapter_card"]
    assert card["record_type"] == "chapter_intent_card"
    assert card["blueprint"] == {
        "title": "第二章：闸门后的回声",
        "core_event": "林澈和周岚决定进入旧城站封闭区寻找失踪信号源。",
        "required_beats": [
            {"index": 1, "text": "林澈确认求救声来自封闭闸门后方。"},
            {"index": 2, "text": "周岚发现闸门控制器刚被人从内部使用。"},
            {"index": 3, "text": "两人约定先进入北侧控制室恢复局部供电。"},
        ],
        "ending_pressure": "控制室日志显示顾言在十分钟前刷卡进入封闭区。",
    }
    assert card["readiness"]["can_generate"] is True
    assert card["context_summary"]["scope"] == "full"
    assert card["language"]["effective"] == "zh-CN"
    assert len(card["confirmation"]["card_sha256"]) == 64
    assert before == _tree_inventory(root)
    assert not (root / ".novelagent" / "runtime" / "candidates").exists()
    serialized = json.dumps(card, ensure_ascii=False)
    assert str(root) not in serialized
    for private_field in (
        "story_project_root",
        "outline_path",
        "previous_prose",
        "tracking_files",
        "setting_files",
        "head_event_hash",
    ):
        assert private_field not in serialized


def test_explicit_read_set_is_summarized_without_exposing_internal_ids(tmp_path: Path) -> None:
    root = _strict_book(tmp_path, explicit_read_set=True)
    client, project = _client_for_book(tmp_path, root)

    card = client.get(
        f"/api/projects/{project['id']}/chapters/2/card"
    ).json()["chapter_card"]

    summary = card["context_summary"]
    assert summary["scope"] == "explicit"
    assert summary["required_state_item_count"] == 2
    assert summary["required_event_item_count"] == 1
    assert summary["continuity"] == {
        "last_scene_location": "旧城站入口",
        "last_scene_character_ids": ["lin-che", "zhou-lan"],
        "required_opening_bridge": "从闸门后的求救声直接续接。",
    }
    assert summary["narrative_constraints"][0]["instruction"] == "不得提前揭示失踪信号源的完整身份。"
    assert summary["expected_new_entities"][0]["display_name"] == "闸门控制器"
    assert card["binding"]["chapter_context_contract_sha256"]
    serialized = json.dumps(card, ensure_ascii=False)
    assert "characters/lin-che" not in serialized
    assert "events/chapter-0001-ending" not in serialized


def test_confirmation_is_stateless_and_rejects_a_stale_card_hash(tmp_path: Path) -> None:
    root = _strict_book(tmp_path)
    client, project = _client_for_book(tmp_path, root)
    client.get("/")
    before = _tree_inventory(root)
    card = client.get(
        f"/api/projects/{project['id']}/chapters/2/card"
    ).json()["chapter_card"]
    request = {
        "confirmed": True,
        "expected_card_sha256": card["confirmation"]["card_sha256"],
        "goal": "进入封闭区并确认失踪信号的来源",
        "validation_focus": ["continuity", "spatial", "logic"],
        "notes_text": "保持第三人称限知\n不要提前揭底",
    }

    confirmed = client.post(
        f"/api/projects/{project['id']}/chapters/2/card/confirm",
        headers={"origin": "http://testserver"},
        json=request,
    )

    assert confirmed.status_code == 200, confirmed.text
    confirmation = confirmed.json()["confirmation"]
    assert confirmation["chapter_intent"] == {
        "goal": "进入封闭区并确认失踪信号的来源",
        "validation_focus": ["continuity", "spatial", "logic"],
        "notes": ["保持第三人称限知", "不要提前揭底"],
    }
    assert confirmation["context_sha256"] == card["binding"]["context_sha256"]
    assert len(confirmation["confirmation_sha256"]) == 64
    assert (
        client.app.state.chapter_cards.verify_confirmation(
            project["id"],
            2,
            confirmation,
        )
        == confirmation
    )
    assert before == _tree_inventory(root)
    assert not (root / ".novelagent" / "runtime" / "candidates").exists()

    stale = client.post(
        f"/api/projects/{project['id']}/chapters/2/card/confirm",
        headers={"origin": "http://testserver"},
        json={**request, "expected_card_sha256": "0" * 64},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "chapter_card_changed"
    assert "重新载入" in stale.json()["error"]["message"]
    assert before == _tree_inventory(root)


def test_confirmation_verification_rejects_tampering_and_later_source_drift(
    tmp_path: Path,
) -> None:
    root = _strict_book(tmp_path)
    client, project = _client_for_book(tmp_path, root)
    client.get("/")
    card = client.get(
        f"/api/projects/{project['id']}/chapters/2/card"
    ).json()["chapter_card"]
    confirmation_response = client.post(
        f"/api/projects/{project['id']}/chapters/2/card/confirm",
        headers={"origin": "http://testserver"},
        json={
            "confirmed": True,
            "expected_card_sha256": card["confirmation"]["card_sha256"],
            "goal": "进入封闭区并确认信号来源",
            "validation_focus": ["continuity", "spatial", "logic"],
            "notes_text": "保持第三人称限知",
        },
    )
    assert confirmation_response.status_code == 200, confirmation_response.text
    confirmation = confirmation_response.json()["confirmation"]
    service = client.app.state.chapter_cards

    tampered = json.loads(json.dumps(confirmation))
    tampered["chapter_intent"]["goal"] = "被篡改的目标"
    with pytest.raises(ChapterCardError, match="confirmation hash") as raised:
        service.verify_confirmation(project["id"], 2, tampered)
    assert raised.value.code == "chapter_card_confirmation_invalid"

    wrong_book = json.loads(json.dumps(confirmation))
    wrong_book["book_id"] = "another-book"
    wrong_book["confirmation_sha256"] = canonical_json_hash(
        wrong_book,
        exclude_fields=("confirmation_sha256",),
        exclude_environment_fields=False,
    )
    with pytest.raises(ChapterCardConflict):
        service.verify_confirmation(project["id"], 2, wrong_book)

    outline = root / "大纲" / "细纲_第002章.md"
    outline.write_text(
        outline.read_text(encoding="utf-8") + "\n作者确认后修改了章纲。\n",
        encoding="utf-8",
    )
    after_author_edit = _tree_inventory(root)
    with pytest.raises(ChapterCardConflict):
        service.verify_confirmation(project["id"], 2, confirmation)
    assert after_author_edit == _tree_inventory(root)
    assert not (root / ".novelagent" / "runtime" / "candidates").exists()


def test_confirmation_rejects_old_hash_after_outline_source_changes(tmp_path: Path) -> None:
    root = _strict_book(tmp_path)
    client, project = _client_for_book(tmp_path, root)
    client.get("/")
    card = client.get(
        f"/api/projects/{project['id']}/chapters/2/card"
    ).json()["chapter_card"]
    outline = root / "大纲" / "细纲_第002章.md"
    outline.write_text(
        outline.read_text(encoding="utf-8").replace(
            "进入旧城站封闭区",
            "从北侧进入旧城站封闭区",
            1,
        ),
        encoding="utf-8",
    )
    after_author_edit = _tree_inventory(root)

    response = client.post(
        f"/api/projects/{project['id']}/chapters/2/card/confirm",
        headers={"origin": "http://testserver"},
        json={
            "confirmed": True,
            "expected_card_sha256": card["confirmation"]["card_sha256"],
            "goal": "进入封闭区",
            "validation_focus": ["continuity"],
            "notes_text": "",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "chapter_card_changed"
    assert after_author_edit == _tree_inventory(root)


def test_confirmation_rejects_non_text_goal_and_schema_rejects_false_confirmation(
    tmp_path: Path,
) -> None:
    root = _strict_book(tmp_path)
    client, project = _client_for_book(tmp_path, root)
    client.get("/")
    card = client.get(
        f"/api/projects/{project['id']}/chapters/2/card"
    ).json()["chapter_card"]
    request = {
        "confirmed": True,
        "expected_card_sha256": card["confirmation"]["card_sha256"],
        "goal": {"unexpected": "object"},
        "validation_focus": ["logic"],
        "notes_text": "",
    }

    response = client.post(
        f"/api/projects/{project['id']}/chapters/2/card/confirm",
        headers={"origin": "http://testserver"},
        json=request,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "chapter_card_confirmation_invalid"
    valid = client.post(
        f"/api/projects/{project['id']}/chapters/2/card/confirm",
        headers={"origin": "http://testserver"},
        json={**request, "goal": "确认闸门后信号", "confirmed": True},
    ).json()["confirmation"]
    invalid = {**valid, "confirmed": False}
    with pytest.raises(SchemaValidationError):
        validate_schema(invalid, "chapter_intent_confirmation.schema.json")
    duplicate_focus = client.post(
        f"/api/projects/{project['id']}/chapters/2/card/confirm",
        headers={"origin": "http://testserver"},
        json={
            **request,
            "goal": "确认闸门后信号",
            "validation_focus": ["logic", "logic"],
        },
    )
    assert duplicate_focus.status_code == 400
    assert duplicate_focus.json()["error"]["code"] == "chapter_card_confirmation_invalid"
    malformed_focus = client.post(
        f"/api/projects/{project['id']}/chapters/2/card/confirm",
        headers={"origin": "http://testserver"},
        json={
            **request,
            "goal": "确认闸门后信号",
            "validation_focus": [{"unexpected": "object"}],
        },
    )
    assert malformed_focus.status_code == 400
    assert malformed_focus.json()["error"]["code"] == "chapter_card_confirmation_invalid"

    invalid_card = json.loads(json.dumps(card))
    invalid_card["confirmation"]["required"] = False
    with pytest.raises(SchemaValidationError):
        validate_schema(invalid_card, "chapter_intent_card.schema.json")


def test_missing_blueprint_fields_block_confirmation_without_provider_or_candidate(
    tmp_path: Path,
) -> None:
    root = _strict_book(tmp_path)
    outline = root / "大纲" / "细纲_第002章.md"
    outline.write_text("# 第二章：尚未补完\n", encoding="utf-8")
    client, project = _client_for_book(tmp_path, root)
    client.get("/")
    before = _tree_inventory(root)

    card_response = client.get(f"/api/projects/{project['id']}/chapters/2/card")

    assert card_response.status_code == 200, card_response.text
    card = card_response.json()["chapter_card"]
    assert card["readiness"]["status"] == "needs_input"
    assert card["readiness"]["can_generate"] is False
    assert {
        "chapter_core_event_missing",
        "chapter_required_beats_missing",
        "chapter_ending_pressure_missing",
    }.issubset({issue["code"] for issue in card["readiness"]["issues"]})
    rejected = client.post(
        f"/api/projects/{project['id']}/chapters/2/card/confirm",
        headers={"origin": "http://testserver"},
        json={
            "confirmed": True,
            "expected_card_sha256": card["confirmation"]["card_sha256"],
            "goal": "不能执行",
            "validation_focus": ["continuity"],
            "notes_text": "",
        },
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "chapter_card_not_ready"
    assert before == _tree_inventory(root)
    assert not (root / ".novelagent" / "runtime" / "candidates").exists()


def test_workbench_language_mismatch_is_visible_but_does_not_change_effective_input(
    tmp_path: Path,
) -> None:
    root = _strict_book(tmp_path)
    client, project = _client_for_book(tmp_path, root, metadata={"language": "en"})

    card = client.get(
        f"/api/projects/{project['id']}/chapters/2/card"
    ).json()["chapter_card"]

    assert card["language"] == {
        "effective": "zh-CN",
        "source": "legacy_story_project_default",
        "workbench_preference": "en",
        "preference_matches_effective": False,
    }
    assert "workbench_language_preference_mismatch" in {
        issue["code"] for issue in card["readiness"]["issues"]
    }
    assert card["readiness"]["can_generate"] is True


def test_source_warnings_are_visible_and_non_blocking(tmp_path: Path) -> None:
    root = _strict_book(tmp_path)
    (root / "设定" / "超长参考.md").write_text("参考" * 25_000, encoding="utf-8")
    client, project = _client_for_book(tmp_path, root)

    response = client.get(f"/api/projects/{project['id']}/chapters/2/card")

    assert response.status_code == 200, response.text
    card = response.json()["chapter_card"]
    assert card["context_summary"]["source_warning_count"] >= 1
    warning = next(
        issue
        for issue in card["readiness"]["issues"]
        if issue["code"] == "story_project_source_warnings"
    )
    assert warning["blocking"] is False
    assert "健康检查" in warning["message"]
    assert card["readiness"]["can_generate"] is True


def test_event_authority_card_uses_replayed_language_without_building_caches(
    tmp_path: Path,
    request,
) -> None:
    short_root = REPOSITORY / ".tmp" / "wcc" / uuid.uuid4().hex[:8]
    request.addfinalizer(lambda: shutil.rmtree(short_root, ignore_errors=True))
    root = short_root / "books" / "event-authority-book"
    root.mkdir(parents=True)
    for directory in CORE_DIRECTORY_NAMES:
        (root / directory).mkdir()
    canonical_outline_path(root, 1).write_text(
        "# Chapter 1: The sealed gate\n\n"
        "core_event: enter the old station\n\n"
        "## required_beats\n\n"
        "- verify the signal behind the gate\n"
        "- restore power in the control room\n\n"
        "ending_pressure: the access log names the missing engineer\n",
        encoding="utf-8",
    )
    identity = ensure_project_identity(root, book_id="event-authority-book")
    genesis = create_genesis_memory_batch(
        book_id=identity.book_id,
        title="Event authority book",
        source_project_digest="a" * 64,
        context_digest="b" * 64,
        language="en",
        authority_epoch=1,
    )
    projection = apply_genesis_event(genesis["events"][0])
    paths = RuntimePaths.for_story_project(root)
    memory_root = paths.memory_dir / "v2"
    write_memory_event_batch(memory_root / "events", genesis)
    activate_event_authority(
        root,
        expected_identity_sha256=project_identity_sha256(root),
        head_event_hash=projection["head_event_hash"],
    )
    before = _tree_inventory(root)
    client, project = _client_for_book(
        tmp_path,
        root,
        metadata={"language": "zh-CN"},
    )

    response = client.get(f"/api/projects/{project['id']}/chapters/1/card")

    assert response.status_code == 200, response.text
    card = response.json()["chapter_card"]
    assert card["language"] == {
        "effective": "en",
        "source": "event_authority",
        "workbench_preference": "zh-CN",
        "preference_matches_effective": False,
    }
    assert card["binding"]["authority_mode"] == "event_v1"
    assert card["binding"]["authority_projection_sha256"]
    assert before == _tree_inventory(root)
    assert not (memory_root / "canonical_memory.json").exists()
    assert not (memory_root / "projections").exists()
    assert not paths.snapshot_path.exists()


def test_unactivated_legacy_project_returns_actionable_chinese_error(tmp_path: Path) -> None:
    root = tmp_path / "books" / "unactivated-book"
    root.parent.mkdir()
    shutil.copytree(FIXTURE, root)
    ensure_project_identity(root)
    client, project = _client_for_book(tmp_path, root)

    response = client.get(f"/api/projects/{project['id']}/chapters/2/card")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "chapter_card_context_not_ready"
    assert "健康检查" in error["message"]
    assert "激活" in error["message"]


def test_chapter_detail_marks_outline_only_chapter_as_draft(tmp_path: Path) -> None:
    root = _strict_book(tmp_path)
    client, project = _client_for_book(tmp_path, root)

    detail = client.get(f"/api/projects/{project['id']}/chapters/2")

    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["has_outline"] is True
    assert payload["has_prose"] is False
    assert payload["status"] == "draft"


def test_written_chapter_card_explains_already_submitted_prose_not_health(tmp_path: Path) -> None:
    root = _strict_book(tmp_path)
    client, project = _client_for_book(tmp_path, root)

    response = client.get(f"/api/projects/{project['id']}/chapters/1/card")

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "chapter_card_already_written"
    assert "正式正文" in error["message"]
    assert "健康" not in error["message"]
    assert "章纲" not in error["message"]


def test_later_outline_chapter_card_explains_previous_chapter_missing_not_health(
    tmp_path: Path,
) -> None:
    root = _strict_book(tmp_path)
    outline = root / "大纲" / "细纲_第003章.md"
    outline.write_text(
        "\n".join(
            [
                "title: 第三章",
                "core_event: 主角沿信号继续向前。",
                "required_beats:",
                "- 确认上一章尚未完成的路线",
                "- 不提前进入尚未开放的区域",
                "",
                "ending_pressure: 前方通路在看不见的地方被封上。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    client, project = _client_for_book(tmp_path, root)

    response = client.get(f"/api/projects/{project['id']}/chapters/3/card")

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "chapter_card_previous_chapter_missing"
    assert "上一章正文还没有提交" in error["message"]
    assert "生产页" in error["message"]
    assert "第 2 章" in error["message"]
    assert "健康" not in error["message"]


def test_next_unwritten_chapter_card_still_builds_when_previous_prose_exists(
    tmp_path: Path,
) -> None:
    root = _strict_book(tmp_path)
    client, project = _client_for_book(tmp_path, root)

    response = client.get(f"/api/projects/{project['id']}/chapters/2/card")

    assert response.status_code == 200, response.text
    card = response.json()["chapter_card"]
    assert card["chapter_index"] == 2
    assert card["readiness"]["can_generate"] is True
