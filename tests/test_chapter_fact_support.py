import copy
import json
from pathlib import Path

import pytest

from core.engine.chapter_fact_candidate import normalize_fact_proposal
from core.engine.chapter_fact_identity import ChapterFactIdentityError
from core.engine.chapter_fact_prepare import ChapterFactPreparationError
from core.engine.chapter_fact_support import (
    SUPPORT_VERSION, ChapterFactSupportError, prepare_fact_support,
    prepare_supported_fact_changes, support_entity_directory, resolve_fact_identities,
)
from core.state.authoritative import empty_authoritative_state


def identity(key, kind, name):
    return {"local_key":key,"kind":kind,"action":"declare","review_source":"author",
        "reason":"作者确认本章首次出现的实体","name":name,"aliases":[]}


def baseline(key, kind, **fields):
    return {"local_key":key,"kind":kind,"review_source":"author","reason":"作者明确补齐此前未知的初始情况",**fields}


def support(identities=(), baselines=()):
    return {"version":SUPPORT_VERSION,"identities":list(identities),"baselines":list(baselines)}


def prepare(state, value):
    return prepare_fact_support(base_state=state,support=value,book_id="support-book",review_scope="review-one")


def test_new_entities_can_be_referenced_in_the_same_chapter_without_fake_baselines():
    state = empty_authoritative_state()
    value = support([identity("guard","character","赵原"),identity("gate","location","北门"),identity("key","item","铜钥匙")])
    text = "赵原在北门看见一把铜钥匙。"
    facts = normalize_fact_proposal({"schema_version":"1.0","facts":[{
        "local_key":"sighting","kind":"event","summary":text,"evidence_quotes":[text],"proposal":{
            "event_type":"sighting","status":"completed","occurrence_location":"local:gate",
            "onsite_participants":[{"entity_id":"local:guard","role":"actor"}],
            "remote_participants":[],"transported_items":[],
            "related_entities":[{"entity_id":"local:key","relation":"reference"}]}}]},chapter_text=text)
    facts[0]["review"]["decision"]="accepted"
    result = prepare_supported_fact_changes(facts=facts,chapter_text=text,base_state=state,support=value,
        book_id="support-book",review_scope="review-one")
    assert len(result["state_after"]["characters"]) == 1
    assert len(support_entity_directory(result["state_after"])) == 3
    assert result["state_after"]["locations"] == result["state_after"]["inventory"] == {}
    assert len(result["state_after"]["events"]) == 4
    assert result["resolved_facts"][0]["proposal"]["occurrence_location"].startswith("location_")
    assert facts[0]["proposal"]["occurrence_location"] == "local:gate"
    assert state == empty_authoritative_state()


def test_explicit_quantity_baseline_is_used_by_existing_fact_reducer():
    root = Path("tests/fixtures/chapter_fact_offline_v1")
    state = json.loads((root/"baseline.json").read_text(encoding="utf-8"))
    state["inventory"].clear()
    case = json.loads((root/"cases/missing_quantity_baseline.json").read_text(encoding="utf-8"))["material"]
    facts = normalize_fact_proposal(case["proposal"], chapter_text=case["chapter_text"])
    for fact in facts: fact["review"]["decision"]="accepted"
    # An identity may exist while this owner's stock remains unknown.
    state["inventory"]["other_owner"]={"inventory_id":"other_owner","owner_id":"char_zhou_lan","item_id":"item_beacon","quantity":1}
    before=copy.deepcopy(state)
    with pytest.raises(ChapterFactPreparationError,match="baseline_missing"):
        prepare_supported_fact_changes(facts=facts,chapter_text=case["chapter_text"],base_state=state)
    result=prepare_supported_fact_changes(facts=facts,chapter_text=case["chapter_text"],base_state=state,
        support=support(baselines=[baseline("stock","inventory",owner_id="char_lin_che",item_id="item_beacon",quantity=4)]),
        book_id="support-book",review_scope="review-one")
    own=[v for v in result["state_after"]["inventory"].values() if v["owner_id"]=="char_lin_che"]
    assert len(own)==1 and own[0]["quantity"]==5
    inventory_change=next(c for c in result["computed_changes"] if c["kind"]=="inventory")
    assert inventory_change["before"]==4 and inventory_change["after"]==5
    assert state==before


def test_all_missing_baseline_kinds_are_explicit_and_counts_are_program_computed():
    identities=[identity("a","character","甲"),identity("b","character","乙"),identity("place","location","北门"),identity("item","item","铜钥匙")]
    bases=[baseline("goal","character_state",entity_id="local:a",field="current_goal",value="巡逻"),
        baseline("position","location",entity_id="local:a",location_id="local:place"),
        baseline("stock","inventory",owner_id="local:a",item_id="local:item",quantity=0),
        baseline("alert","counter",counter_id="alert",value=3),
        baseline("team","relationship",relationship_id="team",source_entity_id="local:a",target_entity_id="local:b",relation_type="teammate",field="trust",value="陌生"),
        baseline("guards","roster",roster_id="guards",member_ids=["local:a","local:b"])]
    result=prepare(empty_authoritative_state(),support(identities,bases))
    state=result["state_after_support"]
    assert state["roster"]["guards"]["declared_count"]==2
    assert state["numeric_counters"]["alert"]["current_value"]==3
    assert state["relationships"]["team"]["trust"]=="陌生"
    assert len(state["locations"])==len(state["inventory"])==1
    assert result["publication_authorized"] is False
    # Once known, even a same-value supplement must not pretend to be new.
    for baseline_value in result["resolved_baselines"]:
        with pytest.raises(ChapterFactSupportError,match="already_known"):
            prepare(state,support(baselines=[baseline_value]))


@pytest.mark.parametrize("change", [
    {"quantity":-1},{"quantity":True},{"quantity":1.5},{"quantity":float("inf")},
    {"review_source":"model"},{"reason":""},{"before":0},{"owner_id":"unknown"},
])
def test_baseline_rejects_invented_quantities_provenance_and_unknown_identity(change):
    value=support([identity("a","character","甲"),identity("item","item","铜钥匙")],
        [{**baseline("stock","inventory",owner_id="local:a",item_id="local:item",quantity=0),**change}])
    with pytest.raises((ChapterFactSupportError,ChapterFactIdentityError)):
        prepare(empty_authoritative_state(),value)


def test_rejected_or_missing_declaration_does_not_resolve_a_local_reference():
    value=support(baselines=[baseline("position","location",entity_id="local:missing",location_id="local:place")])
    with pytest.raises(ChapterFactSupportError,match="identity_unknown"):
        prepare(empty_authoritative_state(),value)


def test_baseline_cannot_edit_identity_metadata_or_replace_relationship_endpoints():
    initial=prepare(empty_authoritative_state(),support([identity("a","character","甲"),identity("b","character","乙")]))
    state=initial["state_after_support"]
    a=initial["identity_plan"]["local_references"]["a"]["entity_id"]
    b=initial["identity_plan"]["local_references"]["b"]["entity_id"]
    with pytest.raises(ChapterFactSupportError,match="field_forbidden"):
        prepare(state,support(baselines=[baseline("identity","character_state",entity_id=a,field="source_tier",value="story_project_standard")]))
    state=prepare(state,support(baselines=[baseline("rel","relationship",relationship_id="rel",source_entity_id=a,target_entity_id=b,relation_type="friend",field="trust",value="高")]))["state_after_support"]
    with pytest.raises(ChapterFactSupportError,match="identity_conflict"):
        prepare(state,support(baselines=[baseline("rel","relationship",relationship_id="rel",source_entity_id=b,target_entity_id=a,relation_type="friend",field="status",value="好")]))


def test_identity_resolution_never_rewrites_nested_state_values():
    facts=[{"proposal":{"entity_id":"local:guard","after":{"entity_id":"local:guard","text":"local:guard"}}}]
    resolved=resolve_fact_identities(facts,{"guard":{"kind":"character","entity_id":"char_guard"}})
    assert resolved[0]["proposal"]["entity_id"]=="char_guard"
    assert resolved[0]["proposal"]["after"]==facts[0]["proposal"]["after"]


def test_ordinary_fact_event_cannot_impersonate_an_identity_declaration():
    root=Path("tests/fixtures/chapter_fact_offline_v1")
    state=json.loads((root/"baseline.json").read_text(encoding="utf-8"))
    material=json.loads((root/"cases/seven_kinds.json").read_text(encoding="utf-8"))["material"]
    proposal=copy.deepcopy(material["proposal"])
    proposal["facts"]=[proposal["facts"][0]]
    proposal["facts"][0]["proposal"]["event_type"]="reviewed_entity_declaration_v1"
    facts=normalize_fact_proposal(proposal,chapter_text=material["chapter_text"])
    facts[0]["review"]["decision"]="accepted"
    with pytest.raises(ChapterFactPreparationError,match="event_type_reserved"):
        prepare_supported_fact_changes(facts=facts,chapter_text=material["chapter_text"],base_state=state)
