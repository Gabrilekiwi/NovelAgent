from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from core.state.authoritative import empty_authoritative_state
from core.state.chapter_read_set import normalize_chapter_context_read_set
from core.state.generation_state_view import build_generation_state_view
from core.structured_context import StructuredContextError
from modules.scene_repair.envelope import (
    RepairEnvelopeError,
    generation_state_view_sha256,
    repair_validation_sha256,
)
from modules.scene_repair.plan import build_repair_plan
from modules.scene_repair.repairer import (
    RepairContext,
    _repair_with_model,
    build_repair_messages,
)


CHAPTER = "第十八章\n\n陆沉按住无线电，倒计时归零。"
RAW_AUTHORITY_SENTINEL = "RAW-AUTHORITY-MUST-NOT-REACH-REPAIR"


def _view() -> dict:
    authority = empty_authoritative_state()
    authority["characters"]["hero"] = {
        "character_id": "hero",
        "canonical_name": "陆沉",
        "condition": "稳定",
        "before": RAW_AUTHORITY_SENTINEL,
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
            }
        ],
    }


def _input_pack(view: dict) -> str:
    return (
        "# Authoritative State\n"
        + json.dumps(
            {
                "characters": {
                    "unselected": {"audit": RAW_AUTHORITY_SENTINEL}
                }
            },
            ensure_ascii=False,
        )
        + "\n\n# Generation State View\n"
        + json.dumps(view, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n\n# StoryProject Chapter Blueprint\n"
        + json.dumps(
            {
                "chapter_blueprint": {
                    "chapter_index": 18,
                    "title": "三秒握手",
                    "chapter_goal": "完成受控通信且不破坏组织边界。",
                    "core_event": "只发送一次三秒握手。",
                    "required_beats": [
                        {"index": 1, "text": "无线电倒计时归零。"}
                    ],
                    "ending_pressure": "第二个接收器苏醒。",
                    "chapter_context_read_set": {
                        "contract_sha256": view["read_set_digest"]
                    },
                }
            },
            ensure_ascii=False,
        )
        + "\n\n# Requirements\nrepair only\n"
    )


def _inputs() -> tuple[dict, dict, dict, dict, str]:
    validation = _validation()
    recovery = {
        "available": True,
        "chapter_index": 18,
        "source_run_id": "chapter_18_previous",
        "status": "rejected",
        "problem_codes": ["invalid_spatial_transition"],
        "repair_plan": {
            "risk_level": "critical",
            "repair_budget": 2,
            "attempt": 1,
        },
    }
    plan = build_repair_plan(
        validation,
        repair_budget=2,
        attempt=1,
        recovery_context=recovery,
    )
    view = _view()
    return validation, plan, recovery, view, _input_pack(view)


def _messages(
    validation: dict,
    plan: dict,
    recovery: dict,
    input_pack: str,
) -> list[dict[str, str]]:
    return build_repair_messages(
        CHAPTER,
        validation,
        input_pack,
        plan,
        recovery,
        RepairContext(language="zh-CN"),
    )


def _key_count(value, expected: str) -> int:
    if isinstance(value, dict):
        return sum(
            (1 if key == expected else 0) + _key_count(item, expected)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return sum(_key_count(item, expected) for item in value)
    return 0


def _exact_string_count(value, expected: str) -> int:
    if isinstance(value, str):
        return int(value == expected)
    if isinstance(value, dict):
        return sum(
            _exact_string_count(item, expected) for item in value.values()
        )
    if isinstance(value, list):
        return sum(_exact_string_count(item, expected) for item in value)
    return 0


def test_explicit_messages_contain_only_one_compact_repair_envelope() -> None:
    validation, plan, recovery, view, input_pack = _inputs()

    messages = _messages(validation, plan, recovery, input_pack)
    payload_text = messages[1]["content"]
    payload = json.loads(payload_text)

    assert "\n" not in payload_text
    assert payload_text == json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert payload["envelope_kind"] == "repair_envelope"
    assert _exact_string_count(payload, CHAPTER) == 1
    assert _key_count(payload, "problems") == 1
    assert _key_count(payload, "repair_plan") == 1
    assert _key_count(payload, "recovery_context") == 1
    assert _key_count(payload, "generation_state_projection") == 1
    assert _key_count(payload, "chapter_contract") == 1
    assert payload["chapter_contract"] == {
        "chapter_index": 18,
        "title": "三秒握手",
        "chapter_goal": "完成受控通信且不破坏组织边界。",
        "core_event": "只发送一次三秒握手。",
        "required_beats": [
            {"index": 1, "text": "无线电倒计时归零。"}
        ],
        "ending_pressure": "第二个接收器苏醒。",
    }
    assert "generation_state_view" not in payload
    assert payload["recovery_context"]["previous_repair_summary"] == {
        "risk_level": "critical",
        "repair_budget": 2,
        "attempt": 1,
    }
    assert "# Authoritative State" not in payload_text
    assert RAW_AUTHORITY_SENTINEL not in payload_text
    assert "context_digest_and_excerpts" not in payload
    assert "validation" not in payload
    assert messages[0]["content"].endswith(
        '{"allow_new_facts":false,"known_conflict_hint":null,"language":"zh-CN"}'
    )


def test_explicit_message_builder_is_deterministic() -> None:
    validation, plan, recovery, _view_value, input_pack = _inputs()

    first = _messages(validation, plan, recovery, input_pack)
    second = _messages(
        copy.deepcopy(validation),
        copy.deepcopy(plan),
        copy.deepcopy(recovery),
        input_pack,
    )

    assert first == second


def test_explicit_builder_honors_caller_bound_digests() -> None:
    validation, plan, recovery, view, input_pack = _inputs()
    stale_validation = repair_validation_sha256(validation)
    stale_view = generation_state_view_sha256(view)
    changed = copy.deepcopy(validation)
    changed["problems"][0]["message"] = "stale validation"

    with pytest.raises(RepairEnvelopeError) as raised:
        build_repair_messages(
            CHAPTER,
            changed,
            input_pack,
            plan,
            recovery,
            RepairContext(language="zh-CN"),
            expected_validation_sha256=stale_validation,
            expected_generation_state_view_sha256=stale_view,
        )

    assert raised.value.code == "validation_digest_mismatch"


def test_invalid_view_fails_before_completion_call() -> None:
    validation, plan, recovery, view, _input = _inputs()
    tampered = copy.deepcopy(view)
    tampered["continuity"]["last_scene_location"] = "错误地点"
    input_pack = _input_pack(tampered)
    completion = Mock()

    with (
        patch("modules.scene_repair.repairer.chat_completion", completion),
        pytest.raises(StructuredContextError),
    ):
        _repair_with_model(
            CHAPTER,
            validation,
            input_pack,
            plan,
            recovery,
            RepairContext(language="zh-CN"),
        )

    completion.assert_not_called()


def test_budget_checks_the_exact_final_messages_sent_to_completion() -> None:
    validation, plan, recovery, _view_value, input_pack = _inputs()
    captured_budget: dict = {}
    captured_completion: dict = {}

    class Budget:
        def require_input(
            self,
            text: str,
            *,
            stage: str,
            protocol_texts: tuple[str, ...],
        ) -> None:
            captured_budget.update(
                {
                    "text": text,
                    "stage": stage,
                    "protocol_texts": protocol_texts,
                }
            )

    def completion(messages, **kwargs):
        captured_completion["messages"] = messages
        return "修复后的正文"

    with (
        patch(
            "modules.scene_repair.repairer.default_context_budget",
            return_value=Budget(),
        ),
        patch(
            "modules.scene_repair.repairer._repair_max_output_tokens",
            return_value=4_000,
        ),
        patch(
            "modules.scene_repair.repairer.chat_completion",
            side_effect=completion,
        ),
        patch(
            "modules.scene_repair.repairer.validate_text_output",
            side_effect=lambda output, _contract: output,
        ),
    ):
        result = _repair_with_model(
            CHAPTER,
            validation,
            input_pack,
            plan,
            recovery,
            RepairContext(language="zh-CN"),
        )

    assert result == "修复后的正文"
    messages = captured_completion["messages"]
    assert captured_budget == {
        "text": messages[1]["content"],
        "stage": "repair",
        "protocol_texts": (messages[0]["content"],),
    }


def test_legacy_messages_remain_byte_compatible_with_previous_payload() -> None:
    validation = {
        "ok": False,
        "problems": [
            {
                "code": "chapter_too_short",
                "message": "short",
                "validator": "logic",
            }
        ],
    }
    plan = build_repair_plan(validation)
    recovery = {"available": False}
    repair_context = RepairContext(
        language="zh-CN",
        allow_new_facts=False,
        known_conflict_hint="门外有动静",
    )
    compiled = SimpleNamespace(repair=SimpleNamespace(text="COMPILED"))

    with (
        patch(
            "modules.scene_repair.repairer.compile_prompt_contexts",
            return_value=compiled,
        ),
        patch(
            "modules.scene_repair.repairer.authoritative_state_from_markdown",
            return_value={"schema_version": "1.0"},
        ),
        patch(
            "modules.scene_repair.repairer._compact_repair_context",
            return_value="COMPACT",
        ),
        patch(
            "modules.scene_repair.repairer._load_prompt",
            return_value="LEGACY PROMPT",
        ),
        patch(
            "modules.scene_repair.repairer._compact_validation",
            return_value={"legacy": "validation"},
        ),
    ):
        messages = build_repair_messages(
            CHAPTER,
            validation,
            "# Story State\n{}",
            plan,
            recovery,
            repair_context,
        )

    expected_payload = json.dumps(
        {
            "chapter": CHAPTER,
            "base_chapter_sha256": __import__(
                "core.structured_context",
                fromlist=["sha256_text"],
            ).sha256_text(CHAPTER),
            "validation": {"legacy": "validation"},
            "repair_plan": plan,
            "recovery_context": recovery,
            "repair_context": {
                "language": "zh-CN",
                "allow_new_facts": False,
                "known_conflict_hint": "门外有动静",
            },
            "context_digest_and_excerpts": "COMPACT",
            "response_contract": {
                "preferred": "RepairPatch JSON",
                "schema_version": "1.0",
                "operation": "replace",
                "model_required_hashes": [
                    "base_chapter_sha256",
                    "expected_text_sha256",
                ],
                "runtime_bound_hashes": [
                    "output_chapter_sha256",
                    "patch_sha256",
                ],
                "fallback": "complete replacement chapter prose",
            },
        },
        ensure_ascii=False,
        indent=2,
    )
    assert messages == [
        {"role": "system", "content": "LEGACY PROMPT"},
        {"role": "user", "content": expected_payload},
    ]
