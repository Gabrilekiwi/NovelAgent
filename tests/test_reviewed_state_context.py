from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder, CHAPTER_CANDIDATE_CONTEXT_VERSION
from core.engine.chapter_fact_context import build_chapter_fact_context, ChapterFactContextError
from core.engine.story_project_context import StoryProjectContextService
from core.memory_v2.canonical import canonical_json_hash
from core.runtime_paths import RuntimePaths
from core.state.builder import build_snapshot_state_with_audit
from core.state.input_pack import build_input_pack
from core.story_project.read_set import StoryProjectSourceDriftError
from core.story_project.reviewed_state_context import ReviewedStateContextError, validated_reviewed_state_context
from core.story_project.reviewed_static_context import static_character_details, static_character_mapping_diagnostics
from scripts.experiments.d1_reviewed_state import (
    load_runtime_view, SourceRuleError, RELATIVE_PATH, describe_source_conflict, resolve_source_conflict,
)
from scripts.experiments.run_d1_source_experiment import prepare_isolated_book


@pytest.fixture
def case():
    prepared = prepare_isolated_book()
    trusted = ChapterCandidateContextBuilder(prepared["root"], reviewed_state_loader=load_runtime_view).build(3)
    return {**prepared, "trusted": trusted}


def rebind(trusted, context):
    value = canonical_json_hash({"context_builder_version": CHAPTER_CANDIDATE_CONTEXT_VERSION,
        "runtime_cache_policy": "bypass", "story_project_context": context,
        "snapshot": trusted.snapshot, "memory_context": trusted.memory_context}, exclude_environment_fields=False)
    return replace(trusted, story_project_context=context, context_sha256=value)


def test_generation_and_fact_entrypoints_read_identical_seven_collections(case):
    trusted = case["trusted"]
    context = trusted.story_project_context
    expected = case["plan"]["prepared"]["state_after"]
    snapshot = StoryProjectContextService.apply_authority(context, trusted.snapshot)
    assert snapshot["authoritative_state"] == expected
    refs = " ".join(key for records in expected.values() if isinstance(records, dict) for key in records)
    view = build_chapter_fact_context(trusted, refs + " 林澈 周岚").to_dict()
    sources = {source: value for detail in view["current_state_details"] for source, value in detail["current_state"].items()}
    for collection in ("characters", "relationships", "roster", "locations", "inventory", "numeric_counters", "events"):
        for key, value in expected[collection].items():
            assert sources[f"reviewed_state.authoritative_state.{collection}.{key}"] == value
    assert not any(source.startswith("semantic_state.") for source in sources)
    assert view["authority_binding"]["reviewed_source_sha256"] == context["reviewed_state"]["source_sha256"]
    assert view["authority_binding"]["mode"] == "legacy_markdown_v1"


def test_old_name_position_and_goal_cannot_return_through_memory_or_prompt(case):
    trusted = case["trusted"]
    context = copy.deepcopy(trusted.story_project_context)
    context["semantic_state"]["spatial_state"]["character_positions"]["林澈"] = "STALE_NAME_POSITION"
    for record in context["semantic_state"]["characters"].values():
        record["current_goal"] = "STALE_SEMANTIC_GOAL"
    context["semantic_state"]["story_state"]["current_location"] = "STALE_SCENE_POSITION"
    context["tracking_files"]["上下文.md"]["text"] = "STALE_RAW_TRACKING"
    context["setting_files"] = {"old.md": {"text": "STALE_RAW_SETTING"}}
    dirty = {**trusted.snapshot, "book_id": case["identity"].book_id,
             "authoritative_state": {"characters": {"poison": {"current_goal": "STALE_CACHE_GOAL"}}}}
    snapshot, memory = StoryProjectContextService.apply_context(context, dirty,
        {"source": "cache", "items": [{"type": "character", "name": "林澈", "data": {"location": "STALE_MEMORY_POSITION"}}]},
        snapshot_path=RuntimePaths.for_story_project(case["root"]).snapshot_path,
        allow_legacy_snapshot_adoption=False)
    built = build_snapshot_state_with_audit(snapshot, memory)
    snapshot = StoryProjectContextService.apply_authority(context, built["snapshot"])
    prompt = build_input_pack(snapshot, memory_context=memory, story_project_context=context)
    assert "STALE_" not in prompt
    assert snapshot["characters"]["char_lin_che"]["current_goal"] == "守住闸门"
    assert snapshot["spatial_state"]["character_positions"]["char_lin_che"] == "location_old_station"
    assert "林澈" not in snapshot["spatial_state"]["character_positions"]
    view = build_chapter_fact_context(rebind(trusted, context), "林澈 周岚").text
    assert "STALE_" not in view
    assert "STALE_RAW_TRACKING" in context["tracking_files"]["上下文.md"]["text"]  # Audit content retained.


def test_current_reader_retains_parsed_static_constraints_and_pending_threads(case):
    context = case["trusted"].story_project_context
    result = StoryProjectContextService.apply_authority(context, {})
    semantic = context["semantic_state"]
    assert result["world_state"] == semantic["world_state"]
    assert result["constraints"] == semantic["constraints"]
    assert result["foreshadowing"] == semantic["foreshadowing"]
    assert result["story_state"]["pending_threads"] == semantic["story_state"]["pending_threads"]
    assert result["story_state"]["last_chapter_ending"] == context["previous_prose"]["text"][-1200:]


def test_static_details_spaces_foreshadowing_and_approved_text_reach_final_prompt(case):
    context = copy.deepcopy(case["trusted"].story_project_context)
    semantic = context["semantic_state"]
    lin = next(record for record in semantic["characters"].values() if record.get("name") == "林澈")
    lin["background"] = "STATIC_BACKGROUND_MARKER"
    semantic["spatial_state"].update(spaces={"station": {"description": "STATIC_SPACE_MARKER"}},
                                      connections=[{"description": "STATIC_CONNECTION_MARKER"}])
    semantic["foreshadowing"].append({"id": "static-thread", "description": "STATIC_THREAD_MARKER"})
    snapshot = StoryProjectContextService.apply_authority(context, {})
    prompt = build_input_pack(snapshot, story_project_context=context)
    for marker in ("STATIC_BACKGROUND_MARKER", "STATIC_SPACE_MARKER", "STATIC_CONNECTION_MARKER",
                   "STATIC_THREAD_MARKER", "D1_STATIC_RULE"):
        assert marker in prompt
    assert "STATIC_BACKGROUND_MARKER" not in str(context["reviewed_state"]["authoritative_state"])


def test_static_identity_mapping_rejects_ambiguity_and_reports_unmapped_fields():
    semantic = {"a": {"name": "林澈", "background": "history", "location": "OLD"},
                "b": {"name": "另一个人", "background": "unmapped"}}
    reviewed = {"stable-lin": {"canonical_name": "林澈", "background": None}}
    assert static_character_details(semantic, reviewed) == {"stable-lin": {"background": None}}
    assert static_character_mapping_diagnostics(semantic, reviewed)["unmapped_semantic_static_fields"]
    semantic["b"]["aliases"] = ["林澈"]
    with pytest.raises(ValueError, match="ambiguous"):
        static_character_details(semantic, reviewed)


def test_source_edit_to_approved_static_reference_requires_new_reference_review(case):
    source = case["root"] / "设定/D1静态约束.md"
    source.write_text("新的静态规则", encoding="utf-8")
    conflict = describe_source_conflict(root=case["root"], document=case["document"], book_id=case["identity"].book_id)
    decision = {"conflict": conflict, "action": "keep_reviewed_current", "review_source": "author",
                "reason": "不能将保留当前状态冒充重新确认改变的静态规则"}
    with pytest.raises(SourceRuleError, match="static_reference_changed"):
        resolve_source_conflict(root=case["root"], document=case["document"], book_id=case["identity"].book_id, decision=decision)


def test_redundant_legacy_character_location_moves_with_ledger(case):
    current = case["trusted"].story_project_context["reviewed_state"]["authoritative_state"]
    assert current["characters"]["char_lin_che"]["location"] == current["locations"]["char_lin_che"]["location_id"]
    assert current["characters"]["char_lin_che"]["source_tier"] == "story_project_standard"
    location_change = next(item for item in case["plan"]["prepared"]["computed_changes"] if item["kind"] == "location")
    assert location_change["mirrored_character_fields"] == ["location"]


def test_contradictory_character_location_cannot_enter_runtime(case):
    context = copy.deepcopy(case["trusted"].story_project_context)
    view = context["reviewed_state"]
    view["authoritative_state"]["characters"]["char_lin_che"]["location"] = "OLD_LOCATION"
    view["payload_sha256"] = canonical_json_hash({key: value for key, value in view.items()
        if key != "payload_sha256"}, exclude_environment_fields=False)
    with pytest.raises(ReviewedStateContextError, match="mirror_conflict"):
        StoryProjectContextService.apply_authority(context, {})


@pytest.mark.parametrize("field,value", [
    ("book_id", "another-book"), ("chapter_index", 7), ("committed_through", 1),
    ("source_sha256", "1" * 64), ("manual_sources_sha256", "2" * 64),
    ("semantic_source_digest", "3" * 64), ("read_set_context_digest", "4" * 64),
    ("source_relative_path", "../other.md"),
])
def test_forged_view_bindings_fail_before_generation_and_fact_index(case, field, value):
    trusted = case["trusted"]
    context = copy.deepcopy(trusted.story_project_context)
    context["reviewed_state"][field] = value
    with pytest.raises(ReviewedStateContextError):
        StoryProjectContextService.apply_authority(context, trusted.snapshot)
    with pytest.raises(ChapterFactContextError):
        build_chapter_fact_context(rebind(trusted, context), "林澈")


@pytest.mark.parametrize("field,value", [("book_id", "other"), ("source_sha256", "1" * 64),
    ("semantic_source_digest", "2" * 64), ("read_set_context_digest", "3" * 64),
    ("source_relative_path", "追踪/不存在.md"), ("chapter_index", 4)])
def test_source_identity_binding_checks_are_independent_of_payload_checksum(case, field, value):
    context = copy.deepcopy(case["trusted"].story_project_context)
    view = context["reviewed_state"]
    view[field] = value
    view["payload_sha256"] = canonical_json_hash({key: value for key, value in view.items()
        if key != "payload_sha256"}, exclude_environment_fields=False)
    with pytest.raises(ReviewedStateContextError):
        StoryProjectContextService.apply_authority(context, {})


def test_remote_event_role_does_not_move_participant(case):
    context = copy.deepcopy(case["trusted"].story_project_context)
    view = context["reviewed_state"]
    state = view["authoritative_state"]
    event = next(iter(state["events"].values()))
    event["onsite_participants"] = [entry for entry in event["onsite_participants"] if entry["entity_id"] != "char_zhou_lan"]
    event["remote_participants"] = [{"entity_id": "char_zhou_lan", "role": "observer", "channel": "radio"}]
    state["locations"]["char_zhou_lan"]["location_id"] = "location_warehouse"
    view["payload_sha256"] = canonical_json_hash({key: value for key, value in view.items()
        if key != "payload_sha256"}, exclude_environment_fields=False)
    snapshot = StoryProjectContextService.apply_authority(context, {})
    assert snapshot["spatial_state"]["character_positions"]["char_zhou_lan"] == "location_warehouse"
    facts = build_chapter_fact_context(rebind(case["trusted"], context), "周岚").to_dict()
    detail = next(item for item in facts["current_state_details"] if item["entity_id"] == "char_zhou_lan")
    represented_event = next(value for key, value in detail["current_state"].items() if ".events." in key)
    assert represented_event["remote_participants"] == event["remote_participants"]
    assert all(participant["entity_id"] != "char_zhou_lan" for participant in represented_event["onsite_participants"])


def test_source_loader_cannot_adopt_state_during_source_drift(case):
    root = case["root"]
    def drifting_loader(context):
        view = load_runtime_view(context)
        source = root / RELATIVE_PATH
        source.write_bytes(source.read_bytes() + b"\nsource drift\n")
        return view
    with pytest.raises(StoryProjectSourceDriftError):
        ChapterCandidateContextBuilder(root, reviewed_state_loader=drifting_loader).build(3)


def test_manual_reedit_is_rejected_by_actual_builder(case):
    case["manual"].write_bytes(case["manual"].read_bytes() + "\n人工来源又修改了。\n".encode())
    with pytest.raises(SourceRuleError, match="manual_source_changed"):
        ChapterCandidateContextBuilder(case["root"], reviewed_state_loader=load_runtime_view).build(3)


def test_experiment_is_not_discovered_by_default_builder(case):
    context = ChapterCandidateContextBuilder(case["root"]).build(3).story_project_context
    assert "reviewed_state" not in context
    assert validated_reviewed_state_context(context) is None


@pytest.mark.parametrize("change", ["shadow", "event", "ephemeral"])
def test_reviewed_view_cannot_change_authority_mode(case, change):
    context = copy.deepcopy(case["trusted"].story_project_context)
    if change == "shadow":
        context["story_state_mode"] = "shadow"
    elif change == "event":
        context["project_identity"]["authority"] = {"mode": "event_v1"}
    else:
        context["project_identity"]["ephemeral"] = True
    with pytest.raises(ReviewedStateContextError):
        StoryProjectContextService.apply_authority(context, {})
    if change == "event":
        with pytest.raises(ChapterFactContextError, match="cannot be adopted by event authority"):
            build_chapter_fact_context(rebind(case["trusted"], context), "林澈")


def test_incomplete_seven_kind_view_rejects_even_with_recomputed_hash(case):
    context = copy.deepcopy(case["trusted"].story_project_context)
    view = context["reviewed_state"]
    del view["authoritative_state"]["inventory"]
    view["payload_sha256"] = canonical_json_hash({key: value for key, value in view.items()
        if key != "payload_sha256"}, exclude_environment_fields=False)
    with pytest.raises(ReviewedStateContextError, match="all seven"):
        StoryProjectContextService.apply_authority(context, {})
