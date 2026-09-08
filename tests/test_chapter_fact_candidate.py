from __future__ import annotations

import copy
import json

import pytest

from core.engine.chapter_fact_candidate import (
    ChapterFactCandidateError,
    build_fact_extraction_messages,
    normalize_fact_proposal,
    normalize_fact_review_items,
    parse_fact_proposal,
    require_confirmed_fact_review,
)


PROSE = (
    "林澈留在北侧控制室，通过无线电向远在地面的周岚报告闸门状态。"
    "周岚远程确认日志时间，并让运输车把铅盒从仓库送往旧城站。"
    "运输车抵达旧城站后，库存中的铅盒增加一件，警戒值上升两点。"
)


def _event_fact() -> dict:
    return {
        "local_key": "event_remote_report",
        "kind": "event",
        "summary": "林澈在控制室与地面的周岚远程协作。",
        "evidence_quotes": [
            "林澈留在北侧控制室，通过无线电向远在地面的周岚报告闸门状态。"
        ],
        "proposal": {
            "event_type": "remote_coordination",
            "status": "completed",
            "occurrence_location": "location_control_room_north",
            "onsite_participants": [
                {"entity_id": "char_lin_che", "role": "actor"}
            ],
            "remote_participants": [
                {
                    "entity_id": "char_zhou_lan",
                    "role": "coordinator",
                    "channel": "radio",
                }
            ],
            "transported_items": [
                {
                    "item_id": "item_lead_box",
                    "from_location": "location_warehouse",
                    "to_location": "location_old_station",
                    "carrier_id": "vehicle_transport_1",
                }
            ],
            "related_entities": [
                {"entity_id": "gate_1", "relation": "topic"}
            ],
        },
    }


def _proposal(*facts: dict) -> dict:
    return {"schema_version": "1.0", "facts": list(facts)}


def test_event_roles_are_explicit_and_do_not_collapse_remote_or_logical_entities() -> None:
    [fact] = normalize_fact_proposal(
        _proposal(_event_fact()),
        chapter_text=PROSE,
    )

    assert fact["fact_id"].startswith("fact_")
    assert fact["review"] == {
        "origin": "model",
        "decision": "pending",
        "author_edited": False,
        "reason": None,
    }
    event = fact["proposal"]
    assert event["occurrence_location"] == "location_control_room_north"
    assert event["onsite_participants"] == [
        {"entity_id": "char_lin_che", "role": "actor"}
    ]
    assert event["remote_participants"] == [
        {
            "entity_id": "char_zhou_lan",
            "role": "coordinator",
            "channel": "radio",
        }
    ]
    assert event["transported_items"][0]["item_id"] == "item_lead_box"
    assert event["related_entities"] == [
        {"entity_id": "gate_1", "relation": "topic"}
    ]
    assert not ({"subjects", "objects", "location"} & set(event))
    assert fact["evidence_spans"][0]["quote"] == _event_fact()["evidence_quotes"][0]
    assert PROSE[
        fact["evidence_spans"][0]["start"] : fact["evidence_spans"][0]["end"]
    ] == fact["evidence_spans"][0]["quote"]


def test_legacy_subject_object_location_event_contract_is_rejected() -> None:
    event = _event_fact()
    event["proposal"] = {
        "event_type": "remote_coordination",
        "status": "completed",
        "subjects": ["char_lin_che", "char_zhou_lan"],
        "objects": ["item_lead_box"],
        "location": "location_control_room_north",
    }

    with pytest.raises(ChapterFactCandidateError, match="proposal fields differ"):
        normalize_fact_proposal(_proposal(event), chapter_text=PROSE)


def test_event_cannot_mark_one_entity_as_both_onsite_and_remote() -> None:
    event = _event_fact()
    event["proposal"]["remote_participants"][0]["entity_id"] = "char_lin_che"

    with pytest.raises(ChapterFactCandidateError, match="both onsite and remote"):
        normalize_fact_proposal(_proposal(event), chapter_text=PROSE)


def test_state_fact_kinds_require_and_resolve_same_proposal_event_keys() -> None:
    event = _event_fact()
    facts = [
        event,
        {
            "local_key": "character_1",
            "kind": "character_state",
            "summary": "林澈更新当前目标。",
            "evidence_quotes": ["报告闸门状态"],
            "proposal": {
                "entity_id": "char_lin_che",
                "canonical_name": "林澈",
                "field": "current_goal",
                "after": "确认闸门状态",
                "source_event_key": "event_remote_report",
            },
        },
        {
            "local_key": "relationship_1",
            "kind": "relationship",
            "summary": "两人完成一次远程协作。",
            "evidence_quotes": ["周岚远程确认日志时间"],
            "proposal": {
                "relationship_id": "rel_lin_zhou",
                "source_entity_id": "char_lin_che",
                "target_entity_id": "char_zhou_lan",
                "relation_type": "teammate",
                "field": "last_coordination",
                "after": "remote",
                "source_event_key": "event_remote_report",
            },
        },
        {
            "local_key": "roster_1",
            "kind": "roster",
            "summary": "运输车加入旧城站运输队列。",
            "evidence_quotes": ["运输车把铅盒从仓库送往旧城站"],
            "proposal": {
                "roster_id": "roster_station_transport",
                "operation": "add",
                "member_ids": ["vehicle_transport_1"],
                "source_event_key": "event_remote_report",
            },
        },
        {
            "local_key": "location_1",
            "kind": "location",
            "summary": "铅盒被运输到旧城站。",
            "evidence_quotes": ["运输车抵达旧城站后"],
            "proposal": {
                "entity_id": "item_lead_box",
                "from_location": "location_warehouse",
                "to_location": "location_old_station",
                "source_event_key": "event_remote_report",
            },
        },
        {
            "local_key": "inventory_1",
            "kind": "inventory",
            "summary": "旧城站铅盒库存增加一件。",
            "evidence_quotes": ["库存中的铅盒增加一件"],
            "proposal": {
                "owner_id": "location_old_station",
                "item_id": "item_lead_box",
                "delta": 1,
                "source_event_key": "event_remote_report",
            },
        },
        {
            "local_key": "counter_1",
            "kind": "counter",
            "summary": "警戒值增加两点。",
            "evidence_quotes": ["警戒值上升两点"],
            "proposal": {
                "counter_id": "alert_level",
                "operation": "increment",
                "value": 2,
                "source_event_key": "event_remote_report",
            },
        },
    ]

    normalized = normalize_fact_proposal(_proposal(*facts), chapter_text=PROSE)

    assert [item["kind"] for item in normalized] == [
        "event",
        "character_state",
        "relationship",
        "roster",
        "location",
        "inventory",
        "counter",
    ]
    assert all(item["review"]["decision"] == "pending" for item in normalized)


def test_unknown_source_event_key_is_rejected_before_review() -> None:
    inventory = {
        "local_key": "inventory_1",
        "kind": "inventory",
        "summary": "库存增加。",
        "evidence_quotes": ["库存中的铅盒增加一件"],
        "proposal": {
            "owner_id": "location_old_station",
            "item_id": "item_lead_box",
            "delta": 1,
            "source_event_key": "event_missing",
        },
    }

    with pytest.raises(ChapterFactCandidateError, match="same proposal"):
        normalize_fact_proposal(_proposal(inventory), chapter_text=PROSE)


def test_every_non_event_change_requires_a_source_event_key() -> None:
    character = {
        "local_key": "character_1",
        "kind": "character_state",
        "summary": "林澈更新目标。",
        "evidence_quotes": ["报告闸门状态"],
        "proposal": {
            "entity_id": "char_lin_che",
            "canonical_name": "林澈",
            "field": "current_goal",
            "after": "确认闸门状态",
            "source_event_key": None,
        },
    }

    with pytest.raises(ChapterFactCandidateError, match="source_event_key is required"):
        normalize_fact_proposal(_proposal(character), chapter_text=PROSE)


def test_evidence_quote_must_match_exactly_once() -> None:
    missing = _event_fact()
    missing["evidence_quotes"] = ["正文中不存在的句子"]
    with pytest.raises(ChapterFactCandidateError, match="does not occur"):
        normalize_fact_proposal(_proposal(missing), chapter_text=PROSE)

    repeated_prose = "警报响起。警报响起。"
    repeated = _event_fact()
    repeated["evidence_quotes"] = ["警报响起"]
    with pytest.raises(ChapterFactCandidateError, match="ambiguous"):
        normalize_fact_proposal(_proposal(repeated), chapter_text=repeated_prose)


def test_parser_accepts_json_fence_but_still_applies_closed_contract() -> None:
    raw = "```json\n" + json.dumps(_proposal(_event_fact()), ensure_ascii=False) + "\n```"

    [fact] = parse_fact_proposal(raw, chapter_text=PROSE)

    assert fact["kind"] == "event"


def test_review_requires_explicit_decisions_and_accepted_event_dependencies() -> None:
    event = _event_fact()
    inventory = {
        "local_key": "inventory_1",
        "kind": "inventory",
        "summary": "库存增加。",
        "evidence_quotes": ["库存中的铅盒增加一件"],
        "proposal": {
            "owner_id": "location_old_station",
            "item_id": "item_lead_box",
            "delta": 1,
            "source_event_key": "event_remote_report",
        },
    }
    review = normalize_fact_proposal(_proposal(event, inventory), chapter_text=PROSE)
    with pytest.raises(ChapterFactCandidateError, match="explicit author decision"):
        require_confirmed_fact_review(review, chapter_text=PROSE)

    rejected_dependency = copy.deepcopy(review)
    rejected_dependency[0]["review"]["decision"] = "rejected"
    rejected_dependency[0]["review"]["reason"] = "作者删除"
    rejected_dependency[1]["review"]["decision"] = "accepted"
    with pytest.raises(ChapterFactCandidateError, match="depends on a rejected event"):
        require_confirmed_fact_review(rejected_dependency, chapter_text=PROSE)

    accepted = copy.deepcopy(review)
    accepted[0]["review"]["decision"] = "accepted"
    accepted[1]["review"]["decision"] = "rejected"
    accepted[1]["review"]["reason"] = "正文并未建立正式库存归属"
    [only_event] = require_confirmed_fact_review(accepted, chapter_text=PROSE)
    assert only_event["local_key"] == "event_remote_report"


def test_review_edit_preserves_fact_identity_and_revalidates_evidence() -> None:
    [fact] = normalize_fact_proposal(_proposal(_event_fact()), chapter_text=PROSE)
    edited = copy.deepcopy(fact)
    edited["summary"] = "作者修订后的远程协作事实"
    edited["review"] = {
        "origin": "model",
        "decision": "accepted",
        "author_edited": True,
        "reason": "作者核对正文后修订",
    }

    [normalized] = normalize_fact_review_items([edited], chapter_text=PROSE)

    assert normalized["fact_id"] == fact["fact_id"]
    assert normalized["summary"] == "作者修订后的远程协作事实"
    assert normalized["review"]["author_edited"] is True

    edited["evidence_spans"][0]["end"] -= 1
    with pytest.raises(ChapterFactCandidateError, match="does not match"):
        normalize_fact_review_items([edited], chapter_text=PROSE)


def test_fact_extraction_prompt_limits_model_to_proposals_and_role_semantics() -> None:
    messages = build_fact_extraction_messages(
        chapter_index=27,
        chapter_text=PROSE,
        authority_view={"characters": ["char_lin_che", "char_zhou_lan"]},
    )
    combined = "\n".join(item["content"] for item in messages)

    assert "没有写入权威状态的权限" in combined
    assert "只有 onsite_participants" in combined
    assert "remote_participants" in combined
    assert "transported_items" in combined
    assert "related_entities" in combined
    assert "绝不使用旧 subjects、objects、location 字段" in combined
    assert "若无可核实变化，返回空 facts" in combined


def test_empty_fact_proposal_is_valid_and_does_not_force_state_changes() -> None:
    assert normalize_fact_proposal(_proposal(), chapter_text=PROSE) == []
    assert require_confirmed_fact_review([], chapter_text=PROSE) == []
