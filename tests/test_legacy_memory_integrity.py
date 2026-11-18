from __future__ import annotations

import pytest

from core.engine.story_project_context import StoryProjectContextError, StoryProjectContextService
from core.quality.chapter_quality import evaluate_chapter_quality
from core.state.snapshot import normalize_snapshot
from core.state.story_state_context import project_story_state_for_model
from core.story_project.identity import ProjectIdentity
from core.story_project.activation import semantic_fields_without_provenance
from core.story_project.managed_block import parse_managed_block
from core.story_project.semantic_parser import parse_story_project_semantic_state
from core.story_project.writer import (
    StoryProjectWriteTarget, StoryProjectWritebackPlan, StoryProjectWritebackConfig,
    _managed_tracking_content, run_story_project_writeback,
)


def book(tmp_path):
    for name in ("正文", "大纲", "追踪", "设定"):
        (tmp_path / name).mkdir()
    for chapter in (1, 2, 3):
        (tmp_path / "大纲" / f"细纲_第{chapter:03d}章.md").write_text(f"# 第{chapter}章\n核心事件：继续取水。", encoding="utf-8")
    (tmp_path / "追踪/角色状态.md").write_text("# 角色状态\n\n## 陆沉\n\n| 字段 | 值 |\n|---|---|\n| 位置 | 地下商超 |\n", encoding="utf-8")
    return ProjectIdentity("1.0", "memory-integrity-test", "2026-11-17T00:00:00Z", str(tmp_path), ephemeral=True)


def write_memory(root, identity, chapter, prose, changes):
    (root / "正文" / f"第{chapter:03d}章.md").write_text(prose, encoding="utf-8")
    target = StoryProjectWriteTarget("character_state", root / "追踪/角色状态.md")
    plan = StoryProjectWritebackPlan(True, False, False, root, chapter, "取水",
                                   story_state_mode="strict", project_identity=identity.to_dict())
    target.path.write_bytes(_managed_tracking_content(target.path.read_bytes(), target=target,
        run={"id": f"run-{chapter}"}, plan=plan, analysis={"character_changes": changes}, chapter_text=prose))
    return parse_managed_block(target.path.read_bytes()).projection


def authority(root, identity, chapter):
    semantic = parse_story_project_semantic_state(root, chapter, project_identity=identity)
    assert semantic_fields_without_provenance(semantic) == []
    return StoryProjectContextService.apply_authority({"story_state_mode": "strict", "semantic_state": semantic},
                                                      normalize_snapshot({"chapter_index": chapter}))


def test_actual_write_read_cycle_preserves_source_and_unifies_character(tmp_path):
    identity = book(tmp_path)
    first = write_memory(tmp_path, identity, 1, "陆沉返回消防站。", [
        {"name": "陆沉", "current_location": "消防站", "text": "陆沉返回消防站。"}])
    second = write_memory(tmp_path, identity, 2, "三分钟的窗口仍不足以开阀。", [])
    assert second["chapter"] == 2
    assert second["field_sources"]["characters.陆沉"] == first["field_sources"]["characters.陆沉"]
    assert second["field_sources"]["characters.陆沉"]["chapter"] == 1
    snapshot = authority(tmp_path, identity, 3)
    assert len(snapshot["characters"]) == 1
    assert snapshot["spatial_state"]["character_positions"]["陆沉"] == "消防站"
    assert next(iter(snapshot["characters"].values()))["location"] == "消防站"
    # A previous source chapter edited after writeback must invalidate its facts,
    # even when the current chapter and projection hash have not changed.
    (tmp_path / "正文/第001章.md").write_text("陆沉返回医院。", encoding="utf-8")
    with pytest.raises(StoryProjectContextError, match="memory_source_changed"):
        authority(tmp_path, identity, 3)


def test_new_character_locations_have_provenance(tmp_path):
    identity = book(tmp_path)
    write_memory(tmp_path, identity, 1, "陆沉和顾岚返回消防站。", [
        {"name": name, "current_location": "消防站", "text": "陆沉和顾岚返回消防站。"}
        for name in ("陆沉", "顾岚")])
    snapshot = authority(tmp_path, identity, 2)
    assert snapshot["spatial_state"]["character_positions"] == {"陆沉": "消防站", "顾岚": "消防站"}


def test_public_writeback_rejects_invalid_memory_before_writing_prose(tmp_path):
    identity = book(tmp_path)
    context = {"story_project_root": str(tmp_path), "chapter_index": 1,
               "story_state_mode": "strict", "project_identity": identity.to_dict(),
               "chapter_blueprint": {"chapter_index": 1, "title": "取水"}}
    run = {"id": "run-1", "chapter_index": 1, "committed": True}
    plan, result = run_story_project_writeback(context=context, run=run,
        chapter_text="最后，陆沉看向取水图。", validation={"ok": True},
        analysis={"story_state": {"last_scene_location": "最后"}},
        config=StoryProjectWritebackConfig(mode="apply"))
    assert "memory_invalid_scene_location" in plan.blocked_reasons
    assert not result.applied
    assert not list((tmp_path / "正文").iterdir())


def test_empty_extraction_does_not_erase_unresolved_threads(tmp_path):
    identity = book(tmp_path)
    path = tmp_path / "追踪/上下文.md"
    path.write_text("# 当前上下文\n", encoding="utf-8")
    target = StoryProjectWriteTarget("context", path)
    for chapter, threads in [(1, ["阀口缺口仍待复核"]), (2, [])]:
        prose = "阀口缺口仍待复核。"
        (tmp_path / "正文" / f"第{chapter:03d}章.md").write_text(prose, encoding="utf-8")
        plan = StoryProjectWritebackPlan(True, False, False, tmp_path, chapter, "取水",
                                       story_state_mode="strict", project_identity=identity.to_dict())
        path.write_bytes(_managed_tracking_content(path.read_bytes(), target=target, plan=plan,
            run={"id": f"run-{chapter}"}, chapter_text=prose, analysis={"story_state": {"open_threads": threads}}))
    projection = parse_managed_block(path.read_bytes()).projection
    assert projection["values"]["story_state.open_threads"] == ["阀口缺口仍待复核"]
    assert projection["field_sources"]["story_state.open_threads"]["chapter"] == 1
    assert authority(tmp_path, identity, 3)["story_state"]["open_threads"] == ["阀口缺口仍待复核"]


def test_manual_change_after_writeback_is_not_silently_overwritten(tmp_path):
    identity = book(tmp_path)
    write_memory(tmp_path, identity, 1, "陆沉返回消防站。", [
        {"name": "陆沉", "current_location": "消防站", "text": "陆沉返回消防站。"}])
    path = tmp_path / "追踪/角色状态.md"
    path.write_text(path.read_text(encoding="utf-8").replace("地下商超", "医院"), encoding="utf-8")
    with pytest.raises(StoryProjectContextError, match="memory_character_identity_conflict"):
        authority(tmp_path, identity, 2)


def test_unbound_old_quote_and_invalid_location_cannot_be_generation_authority(tmp_path):
    identity = book(tmp_path)
    projection = write_memory(tmp_path, identity, 1, "陆沉看向旧取水图。", [
        {"name": "陆沉", "current_location": "外侧", "text": "顾岚把两处最低数写进公开账。"}])
    from core.story_project.managed_block import managed_payload_sha256, write_managed_block
    projection.pop("field_sources")
    projection["payload_sha256"] = managed_payload_sha256(projection)
    path = tmp_path / "追踪/角色状态.md"
    path.write_bytes(write_managed_block(path.read_bytes(), projection))
    with pytest.raises(StoryProjectContextError, match="memory_source_unbound"):
        authority(tmp_path, identity, 2)
    with pytest.raises(StoryProjectContextError, match="memory_invalid_scene_location"):
        StoryProjectContextService.apply_authority({"story_state_mode": "strict", "semantic_state": {
            "parse_warnings": [{"code": "memory_invalid_scene_location", "message": "最后 is not a place"}]}}, {})


def test_pending_threads_survive_prompt_projection_and_quality_checks():
    state = {"open_threads": [], "pending_threads": ["阀口不足一分钟，不能解决"]}
    projected = project_story_state_for_model(state)
    assert projected["pending_threads"] == state["pending_threads"]
    assert projected["open_threads"] == []
    report = evaluate_chapter_quality(chapter_text="顾岚继续测量阀口。", snapshot=normalize_snapshot({"chapter_index": 64, "story_state": state}))
    checks = {check["code"]: check for check in report["checks"]}
    assert checks["advances_open_threads_or_conflicts"]["status"] != "skip"
    assert checks["avoids_premature_resolution"]["status"] != "skip"


def test_cast_names_do_not_prove_continuity_and_invalid_location_is_a_memory_issue():
    state = {"last_scene_location": "最后", "last_scene_characters": ["陆沉", "韩野", "顾岚"],
             "required_opening_bridge": "最后；陆沉、韩野、顾岚", "last_chapter_ending": "必须再争取一分钟。"}
    report = evaluate_chapter_quality(chapter_text="韩野守着公开账，陆沉和顾岚开始复算。", snapshot=normalize_snapshot({"chapter_index": 64, "story_state": state}))
    checks = {check["code"]: check for check in report["checks"]}
    assert checks["continues_previous_ending"]["status"] != "pass"
    assert checks["preserves_last_scene_location"]["evidence"]["memory_issue"] == "invalid_scene_location"
