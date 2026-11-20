import copy
import json
import subprocess
import sys

import pytest

from core.story_project.paths import infer_next_chapter
from core.story_project.reviewed_state_document import RELATIVE_PATH
from tests.test_chapter_publication_api import api_case
from tests.test_chapter_publication_service import case, ready, service_case, PowerLoss
from tests.test_chapter_fact_support import identity, baseline, support


def editable_review(c):
    text=c["text"]+"\n赵原在北门找到铜钥匙，铜钥匙库存增加1把。"
    draft=c["drafts"].update(c["draft"].id,text=text,expected_revision=c["draft"].revision)
    quote="赵原在北门找到铜钥匙，铜钥匙库存增加1把。"
    proposals=copy.deepcopy([f for f in c["example"]["proposal"]["facts"] if f["kind"] in {"event","counter"}])
    proposals.extend([
        {"local_key":"found_key","kind":"event","summary":quote,"evidence_quotes":[quote],"proposal":{
            "event_type":"found_key","status":"completed","occurrence_location":"local:gate",
            "onsite_participants":[{"entity_id":"local:guard","role":"actor"}],"remote_participants":[],
            "transported_items":[],"related_entities":[{"entity_id":"local:key","relation":"reference"}]}},
        {"local_key":"key_stock","kind":"inventory","summary":"铜钥匙库存增加1把","evidence_quotes":["铜钥匙库存增加1把"],
            "proposal":{"owner_id":"local:guard","item_id":"local:key","delta":1,"source_event_key":"found_key"}}])
    review=c["reviews"].create(draft.id,expected_draft_revision=draft.revision,expected_draft_sha256=draft.sha256,
        proposals=proposals,author_confirmed=True)
    for fact in review["facts"]: fact["review"]["decision"]="accepted"
    review=c["reviews"].update(review["id"],facts=review["facts"],expected_revision=review["revision"])
    return draft,review


def supplements():
    return support([identity("guard","character","赵原"),identity("gate","location","北门"),identity("key","item","铜钥匙")],
        [baseline("key_stock","inventory",owner_id="local:guard",item_id="local:key",quantity=0)])


def save_support(c, review, value=None):
    return c["client"].put(f"/api/fact-reviews/{review['id']}/support", json={
        "support":supplements() if value is None else value,"expected_revision":review["revision"],"author_confirmed":True})


def confirm(c,draft,review):
    return c["client"].post(f"/api/fact-reviews/{review['id']}/confirm",json={"expected_revision":review["revision"],
        "expected_facts_sha256":review["facts_sha256"],"expected_draft_revision":draft.revision,
        "expected_draft_sha256":draft.sha256,"author_confirmed":True})


@pytest.mark.parametrize("fault", [None, "after_apply_target", "after_commit_marker"])
def test_author_support_is_cas_bound_published_and_cold_read_by_the_next_chapter(api_case, fault):
    c=api_case
    draft,review=editable_review(c)
    formal_before=(c["root"]/RELATIVE_PATH).read_bytes()
    original_facts=copy.deepcopy(review["facts"])
    saved=save_support(c,review)
    assert saved.status_code==200,saved.text
    saved=saved.json()["fact_review"]
    assert saved["revision"]==review["revision"]+1 and saved["facts"]==original_facts
    assert saved["support_preview"]["publication_authorized"] is False
    assert (c["root"]/RELATIVE_PATH).read_bytes()==formal_before
    assert save_support(c,review).status_code==409
    assert confirm(c,draft,review).status_code==409
    response=confirm(c,draft,saved)
    assert response.status_code==200,response.text
    confirmed=response.json()["fact_review"]
    body={"review_id":confirmed["id"],"expected_draft_revision":draft.revision,
        "expected_review_revision":confirmed["revision"],"expected_draft_sha256":draft.sha256,
        "expected_facts_sha256":confirmed["facts_sha256"]}
    headers={"Idempotency-Key":"identity-support-commit"}
    preview=c["client"].post(c["path"]+"/publication-preview",json=body,headers=headers)
    assert preview.status_code==200,preview.text
    assert preview.json()["quality"]["local_quality_passed"],preview.json()["quality"]
    if fault is not None:
        def crash(event,index,path):
            if event==fault and (fault!="after_apply_target" or index==0):
                raise PowerLoss()
        publication=c["app"].state.chapter_publication
        publication.persistence_fault_injector=crash
        with pytest.raises(PowerLoss):
            publication.commit(project_id=c["project"].id, chapter=2, **body,
                idempotency_key=headers["Idempotency-Key"])
        publication.persistence_fault_injector=None
        recovered=c["client"].post(c["path"]+"/publication-status",json=body,headers=headers)
        assert recovered.status_code==200,recovered.text
        if fault=="after_apply_target":
            assert recovered.json()["publication"]["publication_verified"] is False
            assert (c["root"]/RELATIVE_PATH).read_bytes()==formal_before
            assert infer_next_chapter(c["root"])==2
            headers={"Idempotency-Key":"identity-support-after-rollback"}
    published=c["client"].post(c["path"]+"/publication",json={**body,"author_confirmed":True},headers=headers)
    assert published.status_code==200,published.text
    assert published.json()["publication"]["publication_verified"]
    assert infer_next_chapter(c["root"])==3 and c["calls"]==[1]
    assert c["client"].post(c["path"]+"/publication-status",json=body,headers=headers).json()==published.json()
    code="""import json,sys
from pathlib import Path
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.engine.chapter_fact_context import build_chapter_fact_context
c=ReviewedChapterCandidateContextBuilder(Path(sys.argv[1])).build(3)
print(json.dumps({'state':c.story_project_context['reviewed_state']['authoritative_state'],
 'directory':build_chapter_fact_context(c,'赵原带着铜钥匙离开北门。').to_dict()['entity_directory']},ensure_ascii=True))
"""
    child=subprocess.run([sys.executable,"-B","-c",code,str(c["root"])],check=True,capture_output=True,text=True,timeout=60)
    cold=json.loads(child.stdout)
    new={e["name"]:e for e in cold["directory"] if e["name"] in {"赵原","北门","铜钥匙"}}
    assert set(new)=={"赵原","北门","铜钥匙"}
    stock=[v for v in cold["state"]["inventory"].values() if v["owner_id"]==new["赵原"]["entity_id"]]
    assert len(stock)==1 and stock[0]["quantity"]==1 and stock[0]["item_id"]==new["铜钥匙"]["entity_id"]


def test_support_requires_ownership_and_author_intent_and_expires_with_prose(api_case):
    c=api_case
    draft,review=editable_review(c)
    path=f"/api/fact-reviews/{review['id']}/support"
    body={"support":supplements(),"expected_revision":review["revision"],"author_confirmed":False}
    assert c["client"].put(path,json=body).status_code==409
    assert c["client"].put(path,json={**body,"author_confirmed":True,"quality_passed":True}).status_code==400
    publication=c["app"].state.chapter_publication
    machine=publication.machine_store
    publication.machine_store=c["root"].parent/"not-enrolled-support"
    unowned=save_support(c,review)
    assert unowned.status_code==409 and unowned.json()["error"]["code"]=="book_ownership_invalid"
    assert c["reviews"].reviews.get(review["id"]).support is None
    publication.machine_store=machine
    marker=c["root"]/".novelagent/runtime/reviewed/p/registry/recovery_required/keep.json"
    marker.parent.mkdir(parents=True,exist_ok=True)
    marker.write_text("retain this evidence",encoding="utf-8")
    assert save_support(c,review).status_code==409
    assert c["reviews"].reviews.get(review["id"]).support is None
    assert marker.read_text(encoding="utf-8")=="retain this evidence"
    # Remove only this test-injected marker, then create the actual author decision.
    marker.unlink()
    saved=save_support(c,review)
    assert saved.status_code==200,saved.text
    saved=saved.json()["fact_review"]
    c["drafts"].update(draft.id,text=draft.text+"作者改稿。",expected_revision=draft.revision)
    assert confirm(c,draft,saved).status_code==409
    assert c["reviews"].reviews.get(review["id"]).state=="editing"
    assert infer_next_chapter(c["root"])==2


def test_confirmed_support_tampering_is_not_hidden_by_unchanged_fact_hash(api_case):
    c=api_case
    draft,review=editable_review(c)
    saved=save_support(c,review).json()["fact_review"]
    confirmed=confirm(c,draft,saved)
    assert confirmed.status_code==200,confirmed.text
    altered=supplements()
    altered["baselines"][0]["quantity"]=8
    with c["db"].transaction() as connection:
        connection.execute("UPDATE chapter_fact_reviews SET support_json=? WHERE id=?",(json.dumps(altered),review["id"]))
    response=c["client"].get(f"/api/fact-reviews/{review['id']}")
    assert response.status_code==500
    assert infer_next_chapter(c["root"])==2
