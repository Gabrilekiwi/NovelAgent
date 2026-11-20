from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.schema import validate_schema
from core.state.chapter_read_set import (
    CHAPTER_CONTEXT_READ_SET_DERIVED_MODE,
    derive_chapter_context_read_set,
    normalize_chapter_context_read_set,
    parse_chapter_context_read_set,
    required_authority_item_ids,
)
from core.story_project.coverage import blueprint_to_dict
from core.story_project.mapper import _build_chapter_blueprint


def _body(*, chapter_index: int = 18) -> dict:
    return {
        "schema_version": "1.0",
        "mode": "explicit",
        "chapter_index": chapter_index,
        "required_state_item_ids": [
            "locations/fire-station-radio-room",
            "characters/lu-chen",
            "characters/lu-chen",
            "relationships/fire-station-cooperation",
        ],
        "required_event_item_ids": [
            "events/chapter-0017-beat-010",
            "events/chapter-0017-beat-008",
            "events/chapter-0017-beat-008",
        ],
        "continuity": {
            "last_scene_location": "消防站二层通信室",
            "last_scene_character_ids": ["lu-chen", "han-ye", "lu-chen"],
            "required_opening_bridge": "从上一章无线电倒计时结束处直接续接。",
        },
        "narrative_constraints": [
            {
                "constraint_id": "fs-siren",
                "lifecycle_action": "active",
                "instruction": "本章推进警报器伏笔，但不得提前揭示完整来源。",
            },
            {
                "constraint_id": "fs-siren",
                "lifecycle_action": "active",
                "instruction": "本章推进警报器伏笔，但不得提前揭示完整来源。",
            },
        ],
        "expected_new_entities": [
            {
                "kind": "creature",
                "entity_id": "sound-seeker",
                "display_name": "寻声者",
            },
            {
                "kind": "creature",
                "entity_id": "sound-seeker",
                "display_name": "寻声者",
            },
        ],
    }


def _source_hash(text: str = "outline") -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fenced(body: dict) -> str:
    return (
        "# 第18章\n\n"
        "```novelagent-chapter-context\n"
        + json.dumps(body, ensure_ascii=False, indent=2)
        + "\n```\n"
    )


def test_normalize_is_deterministic_and_deduplicates_set_semantics() -> None:
    body = _body()
    source_hash = _source_hash()

    first = normalize_chapter_context_read_set(
        body,
        chapter_index=18,
        source_outline_sha256=source_hash,
    )
    reordered = _body()
    reordered["required_state_item_ids"].reverse()
    reordered["required_event_item_ids"].reverse()
    reordered["continuity"]["last_scene_character_ids"].reverse()
    reordered["narrative_constraints"].reverse()
    reordered["expected_new_entities"].reverse()
    second = normalize_chapter_context_read_set(
        reordered,
        chapter_index=18,
        source_outline_sha256=source_hash,
    )

    assert first == second
    assert first["required_state_item_ids"] == [
        "characters/lu-chen",
        "locations/fire-station-radio-room",
        "relationships/fire-station-cooperation",
    ]
    assert first["required_event_item_ids"] == [
        "events/chapter-0017-beat-008",
        "events/chapter-0017-beat-010",
    ]
    assert first["continuity"]["last_scene_character_ids"] == [
        "han-ye",
        "lu-chen",
    ]
    assert len(first["source_outline_sha256"]) == 64
    assert len(first["contract_sha256"]) == 64
    validate_schema(first, "chapter_context_read_set.schema.json")


def test_contract_hash_excludes_only_itself_and_detects_tampering() -> None:
    normalized = normalize_chapter_context_read_set(
        _body(),
        chapter_index=18,
        source_outline_sha256=_source_hash(),
    )

    assert (
        normalize_chapter_context_read_set(
            normalized,
            chapter_index=18,
            source_outline_sha256=_source_hash(),
        )
        == normalized
    )
    tampered = json.loads(json.dumps(normalized, ensure_ascii=False))
    tampered["continuity"]["required_opening_bridge"] = "篡改后的桥接"
    with pytest.raises(ValueError, match="contract_sha256"):
        normalize_chapter_context_read_set(
            tampered,
            chapter_index=18,
            source_outline_sha256=_source_hash(),
        )


def test_parse_requires_the_exact_fence_and_legacy_outline_returns_none() -> None:
    legacy = "# 第18章\n\n这里只写自然语言细纲。"
    wrong_fence = (
        "```json\n"
        + json.dumps(_body(), ensure_ascii=False)
        + "\n```\n"
    )

    assert (
        parse_chapter_context_read_set(
            legacy,
            chapter_index=18,
            source_outline_sha256=_source_hash(legacy),
        )
        is None
    )
    assert (
        parse_chapter_context_read_set(
            wrong_fence,
            chapter_index=18,
            source_outline_sha256=_source_hash(wrong_fence),
        )
        is None
    )


def test_parse_rejects_malformed_or_duplicate_explicit_fences() -> None:
    malformed = "```novelagent-chapter-context\n{\"schema_version\":\"1.0\"}"
    with pytest.raises(ValueError, match="malformed or unclosed"):
        parse_chapter_context_read_set(
            malformed,
            chapter_index=18,
            source_outline_sha256=_source_hash(malformed),
        )

    one = _fenced(_body())
    duplicate = one + "\n" + one
    with pytest.raises(ValueError, match="at most one"):
        parse_chapter_context_read_set(
            duplicate,
            chapter_index=18,
            source_outline_sha256=_source_hash(duplicate),
        )

    duplicate_key = (
        "```novelagent-chapter-context\n"
        '{"schema_version":"1.0","schema_version":"1.0"}'
        "\n```\n"
    )
    with pytest.raises(ValueError, match="must contain one JSON object"):
        parse_chapter_context_read_set(
            duplicate_key,
            chapter_index=18,
            source_outline_sha256=_source_hash(duplicate_key),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value.update(chapter_index=17),
            "does not match requested chapter",
        ),
        (
            lambda value: value["required_state_item_ids"].append(
                "events/chapter-0017-beat-001"
            ),
            "collection must be one of",
        ),
        (
            lambda value: value["required_event_item_ids"].append(
                "characters/lu-chen"
            ),
            "collection must be one of",
        ),
        (
            lambda value: value["required_state_item_ids"].append("lu-chen"),
            "complete collection/id",
        ),
        (
            lambda value: value.update(extra="forbidden"),
            "unsupported field",
        ),
    ],
)
def test_normalize_rejects_invalid_contracts(mutation, message: str) -> None:
    body = _body()
    mutation(body)
    with pytest.raises((TypeError, ValueError), match=message):
        normalize_chapter_context_read_set(
            body,
            chapter_index=18,
            source_outline_sha256=_source_hash(),
        )


def test_conflicting_duplicate_semantic_records_are_rejected() -> None:
    body = _body()
    body["narrative_constraints"].append(
        {
            "constraint_id": "fs-siren",
            "lifecycle_action": "resolved",
            "instruction": "冲突版本。",
        }
    )
    with pytest.raises(ValueError, match="conflicting entries"):
        normalize_chapter_context_read_set(
            body,
            chapter_index=18,
            source_outline_sha256=_source_hash(),
        )


def test_required_authority_item_ids_keeps_state_then_event_partition() -> None:
    normalized = normalize_chapter_context_read_set(
        _body(),
        chapter_index=18,
        source_outline_sha256=_source_hash(),
    )

    assert required_authority_item_ids(normalized) == (
        "characters/lu-chen",
        "locations/fire-station-radio-room",
        "relationships/fire-station-cooperation",
        "events/chapter-0017-beat-008",
        "events/chapter-0017-beat-010",
    )


def test_mapper_binds_contract_to_full_utf8_outline_text() -> None:
    outline_text = (
        "# 第18章 寻声者\n\n"
        "core_event: 测试显式语义读集。\n\n"
        "## required_beats\n"
        "- 从通信室续接。\n\n"
        "ending_pressure: 尸潮逼近。\n\n"
        + _fenced(_body())
    )
    blueprint = _build_chapter_blueprint(
        chapter_index=18,
        outline_path=Path("outline-18.md"),
        outline_text=outline_text,
    )
    serialized = blueprint_to_dict(blueprint)

    assert serialized is not None
    read_set = serialized["chapter_context_read_set"]
    assert read_set["source_outline_sha256"] == hashlib.sha256(
        outline_text.encode("utf-8")
    ).hexdigest()
    assert read_set["chapter_index"] == 18


def test_mapper_keeps_legacy_blueprint_compatible_without_contract() -> None:
    outline_text = (
        "# 第18章 旧细纲\n\n"
        "core_event: 旧格式仍可运行。\n\n"
        "## required_beats\n"
        "- 保持兼容。\n\n"
        "ending_pressure: 下一章继续。\n"
    )
    blueprint = _build_chapter_blueprint(
        chapter_index=18,
        outline_path=Path("legacy-outline-18.md"),
        outline_text=outline_text,
    )

    assert blueprint.chapter_context_read_set is None
    assert blueprint.to_dict()["chapter_context_read_set"] is None


def test_derive_keeps_current_state_and_only_recent_or_open_events() -> None:
    authority = {
        "characters": {"hero": {"character_id": "hero"}, "ally": {"character_id": "ally"}},
        "locations": {"hero": {"entity_id": "hero", "location_id": "old-cabinet"}},
        "events": {
            "chapter-0050-beat-001": {"event_id": "chapter-0050-beat-001", "status": "completed"},
            "chapter-0057-beat-002": {"event_id": "chapter-0057-beat-002", "status": "completed"},
            "chapter-0058-beat-003": {"event_id": "chapter-0058-beat-003", "status": "completed"},
            "open-search": {"event_id": "open-search", "status": "ongoing", "chapter_index": 12},
        },
    }
    snapshot = {
        "story_state": {
            "last_scene_location": "old-cabinet",
            "last_scene_characters": ["hero"],
            "required_opening_bridge": "Stay at the cabinet.",
            "last_chapter_ending": "The other side started knocking.",
        },
        "spatial_state": {"character_positions": {"hero": "old-cabinet", "ally": "radio-room"}},
        "constraints": [
            {
                "constraint_id": "no-chase",
                "lifecycle_action": "enforce",
                "instruction": "Do not expand the search past the withdrawal mark.",
            }
        ],
    }
    blueprint = {
        "chapter_index": 59,
        "title": "Only the withdrawal mark",
        "core_event": "The search stops.",
        "required_beats": [{"index": 1, "text": "Approve a local review only."}],
        "ending_pressure": "The device turns away.",
    }

    read_set = derive_chapter_context_read_set(
        authority,
        chapter_index=59,
        snapshot=snapshot,
        blueprint=blueprint,
    )

    assert read_set["mode"] == CHAPTER_CONTEXT_READ_SET_DERIVED_MODE
    assert "characters/hero" in read_set["required_state_item_ids"]
    assert "characters/ally" in read_set["required_state_item_ids"]
    assert "locations/hero" in read_set["required_state_item_ids"]
    assert "events/chapter-0050-beat-001" not in read_set["required_event_item_ids"]
    assert "events/chapter-0057-beat-002" in read_set["required_event_item_ids"]
    assert "events/chapter-0058-beat-003" in read_set["required_event_item_ids"]
    assert "events/open-search" in read_set["required_event_item_ids"]
    assert read_set["continuity"]["last_scene_location"] == "old-cabinet"
    assert set(read_set["continuity"]["last_scene_character_ids"]) == {"hero", "ally"}
    assert read_set["continuity"]["required_opening_bridge"] == "Stay at the cabinet."
    assert read_set["narrative_constraints"][0]["constraint_id"] == "no-chase"
    validate_schema(read_set, "chapter_context_read_set.schema.json")


def test_parse_rejects_derived_mode_in_outline_fence() -> None:
    body = {
        "schema_version": "1.0",
        "mode": "derived",
        "chapter_index": 59,
        "required_state_item_ids": [],
        "required_event_item_ids": [],
        "continuity": {
            "last_scene_location": "here",
            "last_scene_character_ids": ["hero"],
            "required_opening_bridge": "Continue.",
        },
        "narrative_constraints": [],
        "expected_new_entities": [],
    }
    outline = "# Chapter 59\n\n```novelagent-chapter-context\n" + json.dumps(body, ensure_ascii=False) + "\n```\n"
    with pytest.raises(ValueError, match="must use mode 'explicit'"):
        parse_chapter_context_read_set(
            outline,
            chapter_index=59,
            source_outline_sha256="a" * 64,
        )


def test_derive_continuity_ignores_discourse_last_scene_location() -> None:
    authority = {
        "characters": {
            "陆沉": {"character_id": "陆沉"},
            "韩野": {"character_id": "韩野"},
            "顾岚": {"character_id": "顾岚"},
        },
        "locations": {
            "陆沉": {"entity_id": "陆沉", "location_id": "消防站侧首段递送路线"},
        },
        "events": {},
    }
    snapshot = {
        "story_state": {
            "last_scene_location": "最后",
            "last_scene_characters": ["陆沉", "韩野", "顾岚"],
            "required_opening_bridge": "最后；陆沉、韩野、顾岚",
            "last_chapter_ending": "静默的边界已经守住，真正留给他们的，仍是阀口那一分钟——任何记录都覆盖不了的一分钟。",
        },
        "spatial_state": {
            "character_positions": {
                "陆沉": "消防站侧首段递送路线",
                "韩野": "消防站侧首段递送路线",
                "顾岚": "消防站侧首段递送路线",
            },
            "last_transition": {"to": "最后", "source": "chapter_analysis"},
        },
    }
    blueprint = {
        "chapter_index": 64,
        "title": "三分钟之外",
        "core_event": "外围观察备用取水口。",
        "required_beats": [{"index": 1, "text": "不越过撤回线。"}],
        "ending_pressure": "扫描间隔并不固定。",
    }

    read_set = derive_chapter_context_read_set(
        authority,
        chapter_index=64,
        snapshot=snapshot,
        blueprint=blueprint,
    )

    assert read_set["continuity"]["last_scene_location"] == "消防站侧首段递送路线"
    assert "消防站侧首段递送路线" in read_set["continuity"]["required_opening_bridge"]
    assert "最后" not in read_set["continuity"]["required_opening_bridge"]
    validate_schema(read_set, "chapter_context_read_set.schema.json")
