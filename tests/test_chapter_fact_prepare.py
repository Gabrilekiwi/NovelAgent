from __future__ import annotations

import copy

import pytest

from core.engine.chapter_fact_candidate import normalize_fact_proposal
from core.engine.chapter_fact_contract import fact_contract_example
from core.engine.chapter_fact_prepare import ChapterFactPreparationError, prepare_confirmed_fact_changes
from core.state.authoritative import empty_authoritative_state


def _base():
    base = empty_authoritative_state()
    base["characters"] = {
        "char_lin_che": {"character_id": "char_lin_che", "canonical_name": "林澈", "aliases": ["林澈"], "current_goal": "赶路"},
        "char_zhou_lan": {"character_id": "char_zhou_lan", "canonical_name": "周岚", "aliases": ["周岚"]},
    }
    base["relationships"] = {"rel_lin_zhou": {"relationship_id": "rel_lin_zhou", "source_character_id": "char_lin_che",
                                               "target_character_id": "char_zhou_lan", "type": "teammate", "trust": "谨慎"}}
    base["locations"] = {
        "char_lin_che": {"entity_id": "char_lin_che", "location_id": "location_warehouse"},
        "char_zhou_lan": {"entity_id": "char_zhou_lan", "location_id": "location_old_station"},
    }
    base["roster"] = {"roster_station_guards": {"roster_id": "roster_station_guards", "name": "旧城站守卫队",
                                                "members": [{"member_id": "char_zhou_lan"}], "unresolved_count": 0, "declared_count": 1}}
    base["inventory"] = {"beacon_ledger": {"inventory_id": "beacon_ledger", "owner_id": "char_lin_che", "item_id": "item_beacon", "quantity": 0}}
    base["numeric_counters"] = {"alert_level": {"counter_id": "alert_level", "current_value": 0, "minimum": 0, "maximum": 100}}
    return base


def _review(example):
    facts = normalize_fact_proposal(example["proposal"], chapter_text=example["chapter_text"])
    for fact in facts:
        fact["review"]["decision"] = "accepted"
    return facts


def test_seven_kind_plan_computes_values_and_dependencies_without_mutating_inputs():
    example = fact_contract_example()
    facts, base = _review(example), _base()
    original = copy.deepcopy((facts, base))
    plan = prepare_confirmed_fact_changes(facts=facts, chapter_text=example["chapter_text"], base_state=base)
    assert (facts, base) == original
    by_kind = {item["kind"]: item for item in plan["computed_changes"]}
    assert len(by_kind) == 6 and len(plan["state_delta"]["events"]) == 1
    assert (by_kind["inventory"]["before"], by_kind["inventory"]["after"]) == (0, 1)
    assert (by_kind["counter"]["before"], by_kind["counter"]["after"]) == (0, 2)
    assert (by_kind["roster"]["before"], by_kind["roster"]["after"]) == (1, 2)
    assert plan["state_after"]["characters"]["char_lin_che"]["current_goal"] == "守住闸门"
    assert plan["state_after"]["relationships"]["rel_lin_zhou"]["trust"] == "互信"
    assert plan["state_after"]["locations"]["char_lin_che"]["location_id"] == "location_old_station"
    event_id = plan["state_delta"]["events"][0]["event_id"]
    assert all(change["source_event_id"] == event_id for change in plan["computed_changes"])
    assert prepare_confirmed_fact_changes(facts=list(reversed(facts)), chapter_text=example["chapter_text"], base_state=base) == plan


@pytest.mark.parametrize("collection,key", [("inventory", "beacon_ledger"), ("numeric_counters", "alert_level"),
                                            ("roster", "roster_station_guards"), ("locations", "char_lin_che")])
def test_missing_baseline_cannot_be_synthesized_from_a_fact(collection, key):
    example, base = fact_contract_example(), _base()
    # Keep identity elsewhere while removing the actual state/quantity entry.
    if collection == "inventory":
        base["locations"]["item_beacon"] = {"entity_id": "item_beacon", "location_id": "location_old_station"}
    del base[collection][key]
    with pytest.raises(ChapterFactPreparationError) as failure:
        prepare_confirmed_fact_changes(facts=_review(example), chapter_text=example["chapter_text"], base_state=base)
    assert failure.value.code == "chapter_fact_baseline_missing"


@pytest.mark.parametrize("quantity", [None, True, -1, float("nan")])
def test_unknown_or_invalid_quantity_is_not_zero(quantity):
    example, base = fact_contract_example(), _base()
    base["inventory"]["beacon_ledger"]["quantity"] = quantity
    with pytest.raises(ChapterFactPreparationError):
        prepare_confirmed_fact_changes(facts=_review(example), chapter_text=example["chapter_text"], base_state=base)


@pytest.mark.parametrize("operation,value", [("increment", 2), ("set", 0), ("set", 10)])
def test_setting_a_counter_does_not_authorize_creation_of_its_missing_baseline(operation, value):
    example, base = fact_contract_example(), _base()
    example["proposal"]["facts"][-1]["proposal"].update(operation=operation, value=value)
    base["numeric_counters"].clear()
    with pytest.raises(ChapterFactPreparationError) as failure:
        prepare_confirmed_fact_changes(facts=_review(example), chapter_text=example["chapter_text"], base_state=base)
    assert failure.value.code == "chapter_fact_baseline_missing"


@pytest.mark.parametrize("collection,key,field", [("characters", "char_lin_che", "current_goal"),
                                                ("relationships", "rel_lin_zhou", "trust")])
def test_missing_state_fields_require_a_baseline_instead_of_an_implicit_before(collection, key, field):
    example, base = fact_contract_example(), _base()
    del base[collection][key][field]
    with pytest.raises(ChapterFactPreparationError) as failure:
        prepare_confirmed_fact_changes(facts=_review(example), chapter_text=example["chapter_text"], base_state=base)
    assert failure.value.code == "chapter_fact_baseline_missing"


def _counter_sequence(*, overlap=False):
    example = fact_contract_example()
    event = copy.deepcopy(example["proposal"]["facts"][0])
    counter = copy.deepcopy(example["proposal"]["facts"][-1])
    prose = "警戒值设为10。随后警戒值增加2，最终为12。"
    event["evidence_quotes"] = [prose]
    first, second = copy.deepcopy(counter), copy.deepcopy(counter)
    first["local_key"], second["local_key"] = "counter_set", "counter_increment"
    first["proposal"].update(operation="set", value=10)
    second["proposal"].update(operation="increment", value=2)
    first["evidence_quotes"] = [prose if overlap else "警戒值设为10。"]
    second["evidence_quotes"] = [prose if overlap else "随后警戒值增加2，最终为12。"]
    return {"chapter_text": prose, "proposal": {"schema_version": "1.0", "facts": [event, second, first]}}


def test_explicit_evidence_order_overrides_model_array_order_and_advances_before_values():
    example, base = _counter_sequence(), _base()
    base["numeric_counters"]["alert_level"]["current_value"] = 3
    facts = _review(example)
    plan = prepare_confirmed_fact_changes(facts=facts, chapter_text=example["chapter_text"], base_state=base)
    assert [(c["before"], c["after"]) for c in plan["computed_changes"]] == [(3, 10), (10, 12)]
    assert plan["state_after"]["numeric_counters"]["alert_level"]["current_value"] == 12
    assert prepare_confirmed_fact_changes(facts=list(reversed(facts)), chapter_text=example["chapter_text"], base_state=base) == plan


def test_overlapping_counter_changes_are_ambiguous_even_if_array_order_is_available():
    example = _counter_sequence(overlap=True)
    with pytest.raises(ChapterFactPreparationError) as failure:
        prepare_confirmed_fact_changes(facts=_review(example), chapter_text=example["chapter_text"], base_state=_base())
    assert failure.value.code == "chapter_fact_order_ambiguous"


def test_claimed_departure_cannot_replace_actual_baseline_location():
    example, base = fact_contract_example(), _base()
    fact = next(f for f in example["proposal"]["facts"] if f["kind"] == "location")
    fact["proposal"]["from_location"] = "somewhere_else"
    with pytest.raises(ChapterFactPreparationError) as failure:
        prepare_confirmed_fact_changes(facts=_review(example), chapter_text=example["chapter_text"], base_state=base)
    assert failure.value.code == "chapter_fact_location_conflict"


def test_existing_domain_counter_bounds_still_block_the_prepared_result():
    example, base = fact_contract_example(), _base()
    base["numeric_counters"]["alert_level"]["maximum"] = 1
    with pytest.raises(ChapterFactPreparationError) as failure:
        prepare_confirmed_fact_changes(facts=_review(example), chapter_text=example["chapter_text"], base_state=base)
    assert failure.value.code == "chapter_fact_domain_conflict"
    assert "numeric_counter_out_of_range" in {item["code"] for item in failure.value.evidence}


@pytest.mark.parametrize("field", ["character_id", "canonical_name", "source_tier", "aliases"])
def test_state_fields_cannot_change_identity_or_authority_metadata(field):
    example = fact_contract_example()
    fact = next(f for f in example["proposal"]["facts"] if f["kind"] == "character_state")
    fact["proposal"]["field"] = field
    fact["proposal"]["after"] = ["冒名"] if field == "aliases" else "story_project_standard"
    with pytest.raises(ChapterFactPreparationError) as failure:
        prepare_confirmed_fact_changes(facts=_review(example), chapter_text=example["chapter_text"], base_state=_base())
    assert failure.value.code == "chapter_fact_identity_field_forbidden"


def test_evidence_changed_in_the_prose_invalidates_preparation():
    example = fact_contract_example()
    facts = _review(example)
    from core.engine.chapter_fact_candidate import ChapterFactCandidateError
    with pytest.raises(ChapterFactCandidateError, match="does not match"):
        prepare_confirmed_fact_changes(facts=facts, chapter_text=example["chapter_text"].replace("增加1枚", "增加3枚"), base_state=_base())
