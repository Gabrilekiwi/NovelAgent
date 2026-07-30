from __future__ import annotations

import copy
import json

import pytest

from core.state.authoritative import empty_authoritative_state
from core.state.chapter_read_set import normalize_chapter_context_read_set
from core.state.generation_state_view import build_generation_state_view
from core.state.generation_state_view import (
    build_plan_generation_state_projection,
)
from modules.scene_repair.envelope import (
    RepairEnvelopeError,
    build_repair_envelope,
    generation_state_view_sha256,
    repair_validation_sha256,
    validate_repair_envelope,
)
from modules.scene_repair.plan import build_repair_plan


CHAPTER = "第十八章\n\n陆沉按住无线电，倒计时归零。"
CHAPTER_CONTRACT = {
    "chapter_index": 18,
    "title": "三秒握手",
    "chapter_goal": "完成一次受控通信并守住组织边界。",
    "required_beats": [{"index": 1, "text": "无线电倒计时归零。"}],
}


def _view() -> dict:
    authority = empty_authoritative_state()
    authority["characters"]["hero"] = {
        "character_id": "hero",
        "canonical_name": "陆沉",
        "condition": "稳定",
    }
    authority["events"]["chapter-0017-beat-010"] = {
        "event_id": "chapter-0017-beat-010",
        "type": "chapter_close",
        "subjects": ["hero"],
        "objects": [],
        "location": "消防站二层通信室",
        "status": "completed",
        "detail": "无线电倒计时开始。",
    }
    read_set = normalize_chapter_context_read_set(
        {
            "schema_version": "1.0",
            "mode": "explicit",
            "chapter_index": 18,
            "required_state_item_ids": ["characters/hero"],
            "required_event_item_ids": [
                "events/chapter-0017-beat-010"
            ],
            "continuity": {
                "last_scene_location": "消防站二层通信室",
                "last_scene_character_ids": ["hero"],
                "required_opening_bridge": "从无线电倒计时归零处续接。",
            },
            "narrative_constraints": [
                {
                    "constraint_id": "fs-siren",
                    "lifecycle_action": "active",
                    "instruction": "推进警报器伏笔。",
                }
            ],
            "expected_new_entities": [],
        },
        chapter_index=18,
        source_outline_sha256="a" * 64,
    )
    return build_generation_state_view(authority, read_set)


def _validation() -> dict:
    return {
        "ok": False,
        "problems": [
            {
                "code": "invalid_spatial_transition",
                "message": "陆沉的位置跳变缺少路径。",
                "validator": "spatial",
                "severity": "critical",
                "blocking": True,
                "category": "blocking",
                "repair_hint": "补出通信室到车库的连续移动。",
                "repair_action": "add_transition_event",
                "repair_parameters": {
                    "expected": "消防站二层通信室",
                    "actual": "消防站车库",
                },
                "evidence": [
                    {
                        "kind": "expected_location",
                        "value": "消防站二层通信室",
                    }
                ],
            },
            {
                "code": "missing_required_constraint_term",
                "message": "警报器伏笔未推进。",
                "validator": "logic",
                "severity": "high",
                "blocking": True,
                "category": "blocking",
                "repair_hint": "保留来源悬念并推进警报器线索。",
                "repair_action": "add_required_term",
                "repair_parameters": {"term": "警报器"},
                "evidence": [
                    {"kind": "missing_required_term", "value": "警报器"}
                ],
            },
        ],
    }


def _inputs() -> tuple[dict, dict, dict, dict]:
    validation = _validation()
    recovery = {
        "available": True,
        "chapter_index": 18,
        "source_run_id": "chapter_18_previous",
        "status": "rejected",
        "problem_codes": ["invalid_spatial_transition"],
    }
    plan = build_repair_plan(
        validation,
        repair_budget=2,
        attempt=1,
        recovery_context=recovery,
    )
    return validation, plan, recovery, _view()


def _build(
    validation: dict,
    plan: dict,
    recovery: dict,
    view: dict,
) -> dict:
    return build_repair_envelope(
        CHAPTER,
        validation,
        plan,
        chapter_index=18,
        recovery_context=recovery,
        generation_state_view=view,
        chapter_contract=CHAPTER_CONTRACT,
        expected_validation_sha256=repair_validation_sha256(validation),
        expected_generation_state_view_sha256=generation_state_view_sha256(
            view
        ),
    )


def test_builder_produces_one_canonical_problem_source_and_id_references() -> None:
    validation, plan, recovery, view = _inputs()

    envelope = _build(validation, plan, recovery, view)

    assert [item["problem_id"] for item in envelope["problems"]] == [
        "p001",
        "p002",
    ]
    assert sorted(
        step["problem_id"] for step in envelope["repair_plan"]["steps"]
    ) == ["p001", "p002"]
    for step in envelope["repair_plan"]["steps"]:
        assert "message" not in step
        assert "evidence" not in step
        assert "code" not in step
        assert "repair_hint" not in step
    assert "recovery" not in envelope["repair_plan"]
    without_chapter = copy.deepcopy(envelope)
    assert without_chapter.pop("chapter") == CHAPTER
    assert CHAPTER not in json.dumps(
        without_chapter,
        ensure_ascii=False,
        sort_keys=True,
    )
    assert envelope["chapter_contract"] == CHAPTER_CONTRACT
    assert envelope["generation_state_projection"] == (
        build_plan_generation_state_projection(view)
    )
    assert "generation_state_view" not in envelope
    assert (
        envelope["source_generation_state_view_projection_sha256"]
        == view["projection_sha256"]
    )
    assert validate_repair_envelope(envelope) == envelope


def test_builder_is_byte_deterministic_for_identical_inputs() -> None:
    validation, plan, recovery, view = _inputs()

    first = _build(validation, plan, recovery, view)
    second = _build(
        copy.deepcopy(validation),
        copy.deepcopy(plan),
        copy.deepcopy(recovery),
        copy.deepcopy(view),
    )

    assert json.dumps(
        first,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) == json.dumps(
        second,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert first["envelope_sha256"] == second["envelope_sha256"]


def test_problem_ids_are_stable_when_validation_input_order_changes() -> None:
    validation, _plan, recovery, view = _inputs()
    first = _build(
        validation,
        build_repair_plan(validation, repair_budget=2, attempt=1),
        recovery,
        view,
    )
    reordered = copy.deepcopy(validation)
    reordered["problems"].reverse()
    second = _build(
        reordered,
        build_repair_plan(reordered, repair_budget=2, attempt=1),
        recovery,
        view,
    )

    first_ids = {
        problem["code"]: problem["problem_id"]
        for problem in first["problems"]
    }
    second_ids = {
        problem["code"]: problem["problem_id"]
        for problem in second["problems"]
    }
    assert first_ids == second_ids
    assert first["repair_plan"] == second["repair_plan"]


def test_exact_duplicate_problem_and_step_are_emitted_once() -> None:
    validation, _plan, recovery, view = _inputs()
    validation["problems"].append(copy.deepcopy(validation["problems"][0]))
    plan = build_repair_plan(validation, repair_budget=2, attempt=1)

    envelope = _build(validation, plan, recovery, view)

    assert len(envelope["problems"]) == 2
    assert len(envelope["repair_plan"]["steps"]) == 2


def test_builder_fails_closed_on_stale_validation_digest() -> None:
    validation, plan, recovery, view = _inputs()
    stale = repair_validation_sha256(validation)
    validation["problems"][0]["message"] = "已经改变"

    with pytest.raises(RepairEnvelopeError) as raised:
        build_repair_envelope(
            CHAPTER,
            validation,
            plan,
            chapter_index=18,
            recovery_context=recovery,
            generation_state_view=view,
            chapter_contract=CHAPTER_CONTRACT,
            expected_validation_sha256=stale,
            expected_generation_state_view_sha256=generation_state_view_sha256(
                view
            ),
        )

    assert raised.value.code == "validation_digest_mismatch"


def test_builder_fails_closed_on_stale_or_internally_tampered_view() -> None:
    validation, plan, recovery, view = _inputs()
    stale = generation_state_view_sha256(view)
    tampered = copy.deepcopy(view)
    tampered["continuity"]["last_scene_location"] = "错误地点"

    with pytest.raises(RepairEnvelopeError) as raised:
        build_repair_envelope(
            CHAPTER,
            validation,
            plan,
            chapter_index=18,
            recovery_context=recovery,
            generation_state_view=tampered,
            chapter_contract=CHAPTER_CONTRACT,
            expected_validation_sha256=repair_validation_sha256(validation),
            expected_generation_state_view_sha256=stale,
        )

    assert raised.value.code == "generation_state_view_projection_mismatch"


def test_builder_rejects_invalid_or_missing_problem_references() -> None:
    validation, plan, recovery, view = _inputs()
    plan["steps"][0]["index"] = 99

    with pytest.raises(RepairEnvelopeError) as raised:
        _build(validation, plan, recovery, view)

    assert raised.value.code == "repair_plan_problem_reference_invalid"


def test_builder_rejects_stale_problem_text_duplicated_in_plan() -> None:
    validation, plan, recovery, view = _inputs()
    plan["steps"][0]["message"] = "stale"

    with pytest.raises(RepairEnvelopeError) as raised:
        _build(validation, plan, recovery, view)

    assert raised.value.code == "repair_plan_validation_mismatch"


def test_validator_detects_post_build_hash_and_reference_tampering() -> None:
    validation, plan, recovery, view = _inputs()
    envelope = _build(validation, plan, recovery, view)
    tampered = copy.deepcopy(envelope)
    tampered["repair_plan"]["steps"][0]["problem_id"] = "p999"

    with pytest.raises(RepairEnvelopeError) as raised:
        validate_repair_envelope(tampered)

    assert raised.value.code == "repair_plan_problem_reference_invalid"


def test_recovery_context_is_present_exactly_once_when_unavailable() -> None:
    validation, plan, _recovery, view = _inputs()

    envelope = build_repair_envelope(
        CHAPTER,
        validation,
        plan,
        chapter_index=18,
        recovery_context=None,
        generation_state_view=view,
        chapter_contract=CHAPTER_CONTRACT,
        expected_validation_sha256=repair_validation_sha256(validation),
        expected_generation_state_view_sha256=generation_state_view_sha256(
            view
        ),
    )

    assert envelope["recovery_context"] == {"available": False}
    assert "recovery" not in envelope["repair_plan"]
