from __future__ import annotations

import copy
import json

import pytest

from core.engine.chapter_fact_candidate import (
    FACT_KINDS,
    ChapterFactCandidateError,
    build_fact_extraction_messages,
    normalize_fact_proposal,
    normalize_fact_review_items,
    parse_fact_proposal,
    require_confirmed_fact_review,
)
from core.engine.chapter_fact_contract import fact_contract_example, fact_payload_contracts


def test_model_teaching_example_round_trips_all_seven_kinds_through_review():
    example = fact_contract_example()
    facts = parse_fact_proposal(json.dumps(example["proposal"], ensure_ascii=False),
                                chapter_text=example["chapter_text"])
    assert tuple(item["kind"] for item in facts) == FACT_KINDS
    assert tuple(fact_payload_contracts()) == FACT_KINDS
    for fact in facts:
        fact["review"]["decision"] = "accepted"
    assert require_confirmed_fact_review(facts, chapter_text=example["chapter_text"]) == facts

    messages = build_fact_extraction_messages(chapter_index=1, chapter_text="另一段作者正文。",
                                              authority_view={})
    user = messages[1]["content"]
    decoder = json.JSONDecoder()
    prompt_contract, _ = decoder.raw_decode(user.split("七类 proposal 契约：\n", 1)[1])
    prompt_example, _ = decoder.raw_decode(user.split("输出契约示例：\n", 1)[1])
    assert prompt_contract == fact_payload_contracts()
    assert prompt_example == example["proposal"]
    assert "独立教学样本，不是当前章节" in user


@pytest.mark.parametrize("kind", FACT_KINDS)
def test_computed_authority_fields_are_not_accepted_as_model_proposals(kind):
    example = fact_contract_example()
    target = next(item for item in example["proposal"]["facts"] if item["kind"] == kind)
    target["proposal"]["before"] = 999
    with pytest.raises(ChapterFactCandidateError, match="extra=.*before"):
        normalize_fact_proposal(example["proposal"], chapter_text=example["chapter_text"])


@pytest.mark.parametrize("kind", FACT_KINDS[1:])
def test_every_state_kind_blocks_acceptance_when_its_event_is_rejected(kind):
    example = fact_contract_example()
    example["proposal"]["facts"] = [item for item in example["proposal"]["facts"]
                                    if item["kind"] in {"event", kind}]
    facts = normalize_fact_proposal(example["proposal"], chapter_text=example["chapter_text"])
    facts[0]["review"]["decision"] = "rejected"
    facts[1]["review"]["decision"] = "accepted"
    with pytest.raises(ChapterFactCandidateError) as failure:
        require_confirmed_fact_review(facts, chapter_text=example["chapter_text"])
    assert failure.value.code == "chapter_fact_review_dependency_rejected"
    facts[1]["review"]["decision"] = "rejected"
    assert require_confirmed_fact_review(facts, chapter_text=example["chapter_text"]) == []


def test_same_inventory_change_cannot_be_counted_twice_by_widening_its_quote():
    example = fact_contract_example()
    proposal = example["proposal"]
    proposal["facts"] = [item for item in proposal["facts"] if item["kind"] in {"event", "inventory"}]
    duplicate = copy.deepcopy(proposal["facts"][1])
    duplicate["local_key"] = "inventory_duplicate"
    duplicate["summary"] = "换一个摘要和更长的引用"
    duplicate["evidence_quotes"] = ["周岚将一枚信标交给林澈，林澈的信标库存增加1枚。"]
    proposal["facts"].append(duplicate)
    with pytest.raises(ChapterFactCandidateError, match="duplicate fact"):
        normalize_fact_proposal(proposal, chapter_text=example["chapter_text"])


def test_renaming_event_and_widening_quote_does_not_create_a_second_source():
    example = fact_contract_example()
    event = copy.deepcopy(example["proposal"]["facts"][0])
    event["local_key"] = "event_duplicate"
    event["evidence_quotes"] = ["林澈从仓库抵达旧城站"]
    event["proposal"]["onsite_participants"].reverse()
    example["proposal"]["facts"].append(event)
    with pytest.raises(ChapterFactCandidateError, match="duplicate event"):
        normalize_fact_proposal(example["proposal"], chapter_text=example["chapter_text"])


def test_distinct_events_can_repeat_a_delta_but_review_edit_cannot_duplicate_one_event():
    example = fact_contract_example()
    prose = example["chapter_text"] + "随后他又收到一枚信标，库存再增加1枚。"
    first = [item for item in example["proposal"]["facts"] if item["kind"] in {"event", "inventory"}]
    second = copy.deepcopy(first)
    second[0]["local_key"] = "event_2"
    second[1]["local_key"] = "inventory_2"
    second[1]["proposal"]["source_event_key"] = "event_2"
    for item in second:
        item["evidence_quotes"] = ["随后他又收到一枚信标，库存再增加1枚。"]
    facts = normalize_fact_proposal({"schema_version": "1.0", "facts": first + second}, chapter_text=prose)
    assert len(facts) == 4
    facts[-1]["proposal"]["source_event_key"] = "event_1"
    with pytest.raises(ChapterFactCandidateError, match="duplicate fact"):
        normalize_fact_review_items(facts, chapter_text=prose)


@pytest.mark.parametrize("kind,operation", [("roster", "replace"), ("counter", "multiply")])
def test_operations_outside_the_shared_contract_are_rejected(kind, operation):
    example = fact_contract_example()
    target = next(item for item in example["proposal"]["facts"] if item["kind"] == kind)
    target["proposal"]["operation"] = operation
    with pytest.raises(ChapterFactCandidateError, match="operation must be"):
        normalize_fact_proposal(example["proposal"], chapter_text=example["chapter_text"])
