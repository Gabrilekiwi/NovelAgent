"""An author can finish local review/publication while old cost stays unknown."""
import copy
from pathlib import Path

from core.engine.chapter_fact_contract import fact_contract_example
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.story_project.paths import infer_next_chapter, resolve_prose
from tests.test_workbench_continuation import prepare, fresh_api, case


def test_manual_review_and_publication_keep_unknown_reserve_without_new_calls(fresh_api):
    c=fresh_api; factory,source,path,body,_=prepare(c); client=c['client']
    ready=client.post(path+'/continuation',json=body).json()['candidate']['continuation']
    response=client.post(path+'/continuation/run',json=dict(author_confirmed=True,expected_grant_sha256=ready['grant_sha256']))
    assert response.status_code==200,response.text
    draft=response.json()['candidate']['draft']
    example=fact_contract_example()
    blueprint=ReviewedChapterCandidateContextBuilder(c['root']).build(2).story_project_context['chapter_blueprint']
    good=Path('tests/fixtures/chapter_quality/good_chapter.md').read_text(encoding='utf-8').replace('林雪','林澈').replace('陈岚','周禾')
    text=example['chapter_text']+'\n\n'+good+'\n\n'+'。'.join(b['text'] for b in blueprint['required_beats'])+'。\n\n'+blueprint['ending_pressure']+'。'
    saved=client.put(f"/api/candidate-drafts/{draft['id']}",json=dict(text=text,expected_revision=draft['revision']))
    assert saved.status_code==200,saved.text
    draft=saved.json()['draft']
    ledger=c['root']/'.novelagent/runtime/budgets/c2/ledger.json'; before=ledger.read_bytes()
    review_path=f"/api/candidate-drafts/{draft['id']}/fact-reviews"
    data=dict(expected_draft_revision=draft['revision'],expected_draft_sha256=draft['sha256'],proposals=[],author_confirmed=True)
    stale=client.post(review_path,json={**data,'expected_draft_revision':1})
    assert stale.status_code==409
    response=client.post(review_path,json=data);assert response.status_code==201,response.text
    review=response.json()['fact_review'];assert review['facts']==[] and review['state']=='editing'
    assert client.post(review_path,json=data).json()['fact_review']['id']==review['id']
    facts=copy.deepcopy([f for f in example['proposal']['facts'] if f['kind'] in {'event','counter'}])
    # Author-added suggestions go through the same normalizer and decision store.
    suggestions=client.post(review_path,json={**data,'proposals':facts})
    assert suggestions.status_code==409  # Do not silently replace an existing review.
    from core.engine.chapter_fact_candidate import normalize_fact_proposal
    facts=normalize_fact_proposal({'schema_version':'1.0','facts':facts},chapter_text=text,origin='author')
    for fact in facts:fact['review'].update(decision='accepted',reason='测试作者逐项核对正文证据')
    updated=client.put(f"/api/fact-reviews/{review['id']}",json=dict(facts=facts,expected_revision=review['revision']))
    assert updated.status_code==200,updated.text
    review=updated.json()['fact_review']
    confirmed=client.post(f"/api/fact-reviews/{review['id']}/confirm",json=dict(expected_revision=review['revision'],
        expected_facts_sha256=review['facts_sha256'],expected_draft_revision=draft['revision'],expected_draft_sha256=draft['sha256'],author_confirmed=True))
    assert confirmed.status_code==200,confirmed.text
    review=confirmed.json()['fact_review'];assert review['publication_url']
    route=f"/api/projects/{c['project'].id}/chapters/2/publication"
    payload=dict(review_id=review['id'],expected_draft_revision=draft['revision'],expected_review_revision=review['revision'],
        expected_draft_sha256=draft['sha256'],expected_facts_sha256=review['facts_sha256'])
    headers={'Idempotency-Key':'manual-review-with-unknown-cost'}
    preview=client.post(route+'-preview',json=payload,headers=headers)
    assert preview.status_code==200,preview.text
    assert preview.json()['publication_authorized'] is False and infer_next_chapter(c['root'])==2
    published=client.post(route,json={**payload,'author_confirmed':True},headers=headers)
    assert published.status_code==200,published.text
    assert published.json()['publication']['publication_verified'] is True
    assert resolve_prose(c['root'],2).path.read_text(encoding='utf-8')==text+'\n'
    assert infer_next_chapter(c['root'])==3
    assert client.post(route,json={**payload,'author_confirmed':True},headers=headers).json()==published.json()
    assert factory.calls==2 and ledger.read_bytes()==before
    usage=client.get(c['path']).json()['usage']
    assert len(usage['unresolved_attempts'])==1 and usage['charged']['calls']==2
