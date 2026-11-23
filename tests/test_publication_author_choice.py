"""Author decisions bind to one real reviewed draft and survive cold receipt verification."""
from copy import deepcopy
import json

from core.story_project.paths import infer_next_chapter
from core.story_project.reviewed_chapter_transition import offered_author_quality_choice
from tests.test_chapter_publication_api import case, ready, service_case, open_workbench, send


def draft_without_outline(c):
    # Keep the reviewed prose and drop the outline text this fixture appends verbatim.
    text = c['text'].rsplit('\n\n', 2)[0]
    draft = c['drafts'].update(c['draft'].id, text=text, expected_revision=c['draft'].revision)
    cas = dict(expected_draft_revision=draft.revision, expected_draft_sha256=draft.sha256)
    review = c['reviews'].create(draft.id, **cas, proposals=[], author_confirmed=True)
    review = c['reviews'].confirm(review['id'], **cas, expected_revision=review['revision'],
        expected_facts_sha256=review['facts_sha256'], author_confirmed=True)
    return {**c, 'route': {**c['route'], **cas, 'review_id':review['id'],
        'expected_review_revision':review['revision'], 'expected_facts_sha256':review['facts_sha256']}}


def test_selective_quality_consent_and_cold_receipt(service_case):
    with open_workbench(draft_without_outline(service_case)) as c:
        preview = send(c, '-preview').json()
        offer = preview['offered_author_quality_choice']
        assert offer and len(offer['finding_ids']) >= 2, preview['quality']['local_policy_decision']['blocking_finding_ids']
        # Same semantic evidence across independent checks, despite volatile report metadata.
        assert send(c, '-preview').json()['offered_author_quality_choice'] == offer
        assert send(c, '', commit=True).status_code == 409
        for change in ({'finding_ids':offer['finding_ids'][:-1]}, {'finding_ids':['qf:v1:'+'0'*64]}, {'review_sha256':'a'*64}):
            body = {**c['body'], 'author_confirmed':True, 'author_quality_choice':{**offer, **change}}
            assert send(c, '', body=body).status_code == 409
        body = {**c['body'], 'author_quality_choice':offer}
        assert send(c, '-preview', body=body).status_code == 422
        assert send(c, '', body={**body, 'author_confirmed':False}).status_code == 422
        assert infer_next_chapter(c['root']) == 2
        q = preview['quality']
        for field, value in [('review_gate',{'status':'fail'}),('authoritative_state_validation',{'accepted':False}),
                ('final_artifact_integrity',{'accepted':False}),('pending_gates',['llm_validator']),('quality_coverage_complete',False)]:
            assert offered_author_quality_choice({**q, field:value}) is None
        assert offered_author_quality_choice({**q, 'binding':{**q['binding'],'acceptance_kind':'policy'}}) is None
        changed = deepcopy(q)
        next(f for f in changed['local_policy_decision']['findings'] if f['id'] in offer['finding_ids'])['codes'].append('unknown_gate')
        assert offered_author_quality_choice(changed) is None
        response = send(c, '', body={**body,'author_confirmed':True})
        assert response.status_code == 200, response.text
        publication = response.json()['publication']
        assert publication['publication_verified'] and publication['request']['request']['author_quality_choice'] == offer
        archived = c['root']/'.novelagent/runtime/reviewed/quality'/f"{publication['run_id']}.json"
        assert json.loads(archived.read_text('utf8'))['local_quality_passed'] is False
        assert send(c, '-status', body=body).json()['publication'] == publication
        assert send(c, '', body={**body,'author_confirmed':True}).json()['publication'] == publication
        assert send(c, '-status').status_code == 409 and infer_next_chapter(c['root']) == 3


def test_an_edited_and_reconfirmed_draft_needs_a_new_choice(service_case):
    c = draft_without_outline(service_case)
    with open_workbench(c) as original:
        old = send(original, '-preview').json()['offered_author_quality_choice']
        assert old
        current = c['drafts'].get(c['draft'].id)
        draft = c['drafts'].update(current.id, text=current.text + '\n钟声停了。', expected_revision=current.revision)
        cas = dict(expected_draft_revision=draft.revision, expected_draft_sha256=draft.sha256)
        review = c['reviews'].create(draft.id, **cas, proposals=[], author_confirmed=True)
        review = c['reviews'].confirm(review['id'], **cas, expected_revision=review['revision'],
            expected_facts_sha256=review['facts_sha256'], author_confirmed=True)
        base = {**original['body'], **cas, 'review_id':review['id'], 'expected_review_revision':review['revision'],
            'expected_facts_sha256':review['facts_sha256']}
        assert send(original, '', body={**original['body'],'author_confirmed':True,'author_quality_choice':old}).status_code == 409
        fresh = send(original, '-preview', body=base).json()['offered_author_quality_choice']
        assert fresh and fresh['review_sha256'] != old['review_sha256']
        assert send(original, '', body={**base,'author_confirmed':True,'author_quality_choice':old}).status_code == 409
        assert infer_next_chapter(c['root']) == 2
