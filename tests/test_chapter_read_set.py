from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.schema import validate_schema
from core.state.chapter_read_set import (
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
