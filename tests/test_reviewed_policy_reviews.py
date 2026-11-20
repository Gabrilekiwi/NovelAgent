"""Actual stored grant/Candidate/extraction and portable typed policy evidence."""
import copy,json,socket,subprocess,sys
from dataclasses import replace
from pathlib import Path

import pytest

from core.engine.chapter_commit_prepare import prepare_reviewed_chapter,prepare_policy_reviewed_chapter,ChapterCommitPreparationError
from core.engine.chapter_commit_request import chapter_commit_request
from core.engine.chapter_fact_context import build_chapter_fact_context
from core.engine.chapter_fact_contract import fact_contract_example
from core.engine.chapter_fact_extraction import ChapterFactExtractionExecutor
from core.engine.reviewed_candidate import ReviewedChapterCandidateExecutor
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.engine.reviewed_chapter_commit import build_reviewed_chapter_transaction
from core.engine.reviewed_policy_review import validate_policy_fact_review,verify_policy_preparation,build_policy_fact_review,PolicyReviewError
from core.story_project.reviewed_state_document import digest,RELATIVE_PATH,prepare_chapter_document,decode_document,verify_transition
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_chapter_transition import verify_quality_binding,ReviewedChapterTransitionError
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_candidate_context import author_confirmation
from tests.test_workbench_chapter_candidates import _fixed_provenance
from workbench.reviewed_policy_reviews import ReviewedPolicyReviewService


def seeded(*,include_bridge=True):
    patch=pytest.MonkeyPatch()
    c=batch_case.__wrapped__(case.__wrapped__(),patch)
    preview=c['service'].preview(**c['route'])
    grant=c['service'].authorize(**c['route'],expected_manifest_sha256=preview['manifest']['manifest_sha256'],author_confirmed=True)
    project=c['project'];projects=c['publication'].projects;root=c['root']
    trusted=ReviewedChapterCandidateContextBuilder(root).build(2)
    example=fact_contract_example();proposal=copy.deepcopy(example['proposal'])
    proposal['facts']=[f for f in proposal['facts'] if f['kind'] in {'event','counter'}]
    good=Path('tests/fixtures/chapter_quality/good_chapter.md').read_text(encoding='utf-8').replace('林雪','林澈').replace('陈岚','周禾')
    blueprint=trusted.story_project_context['chapter_blueprint']
    opening='林澈左臂的旧伤再次裂开，闸门后的声音准确叫出了他的名字。周岚扶住他，继续检查封闭闸门旁的电路。\n\n' if include_bridge else ''
    text=opening+good+'\n\n'+example['chapter_text']+'\n\n'+'。'.join(b['text'] for b in blueprint['required_beats'])+'。\n\n'+blueprint['ending_pressure']+'。'
    # The fixture explicitly requests manual generation/extraction to create
    # actual immutable input evidence. The policy service never invokes their
    # author-only entry points or manufactures these confirmations.
    candidate=ReviewedChapterCandidateExecutor(story_project_root=root,generator=lambda _:text,
        execution_kind='deterministic',provenance_factory=_fixed_provenance).execute(chapter_index=2,
            confirmation=author_confirmation(projects,project),idempotency_key='manual-fixture-input')
    draft=c['publication'].preparations.drafts.create_from_candidate(project_id=project.id,chapter=2,
        candidate_id=candidate['candidate_id'],source_sha256=candidate['prose']['sha256'],
        source_artifact_ref=candidate['prose']['artifact_ref'],text=text)
    intent=ReviewedChapterCandidateExecutor(story_project_root=root).store.load_intent(candidate['candidate_id'])
    view=build_chapter_fact_context(trusted,text)
    executor=ChapterFactExtractionExecutor(story_project_root=root,provider=lambda *args:json.dumps(proposal,ensure_ascii=False),
        execution_kind='deterministic',provenance_factory=_fixed_provenance)
    extraction=executor.execute(book_id=project.book_id,chapter_index=2,candidate_binding={'candidate_id':draft.candidate_id,'intent_hash':intent['intent_hash']},
        draft_binding={'draft_id':draft.id,'revision':draft.revision,'sha256':draft.sha256},
        context_sha256=trusted.context_sha256,authority_binding=intent['authority'],chapter_text=text,
        authority_view=view.to_dict(),idempotency_key='manual-fixture-extraction',author_confirmed=True)
    assert extraction['status']=='completed'
    service=ReviewedPolicyReviewService(c['publication'])
    route=dict(project_id=project.id,batch_id=grant['grant']['manifest']['batch_id'],draft_id=draft.id,
        extraction_id=extraction['extraction_id'],expected_draft_revision=draft.revision,
        expected_draft_sha256=draft.sha256,expected_extraction_record_hash=extraction['record_hash'])
    result=service.prepare(**route)
    return {**c,'patch':patch,'policy_service':service,'policy_route':route,'result':result,'draft':draft,'trusted':trusted}


@pytest.fixture(scope='module')
def policy_case():
    c=seeded()
    try:yield c
    finally:c['patch'].undo()


def test_owned_policy_review_uses_actual_extraction_and_never_records_author_confirmation(policy_case):
    c=policy_case;r=c['result'];review=r['policy_review'];p=r['preparation']
    assert review['review_source']=='policy' and review['author_confirmed'] is False
    assert review['state']=='accepted_for_quality_check' and not review['publication_authorized']
    assert p['binding']['acceptance_kind']=='policy' and 'confirmation_sha256' not in p['binding']
    assert verify_policy_preparation(p)==review and validate_policy_fact_review(review)==review
    assert not r['execution_permit'] and not r['publication_authorized']
    assert c['publication'].preparations.drafts.get(c['draft'].id)==c['draft']
    with c['db'].connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM chapter_fact_reviews').fetchone()[0]==0
    assert not (c['root']/'.novelagent/runtime/reviewed/intents').exists()


def test_manual_and_policy_prepare_identical_domain_changes_and_portable_history_preserves_sources(policy_case):
    c=policy_case;p=c['result']['preparation'];review=copy.deepcopy(c['result']['policy_review'])
    # Explicitly synthetic author record for comparing the two pure adapters.
    review.update(record_type='chapter_fact_review',schema_version='1.0',state='confirmed',review_source='author',
        confirmation_sha256='a'*64)
    manual=prepare_reviewed_chapter(trusted_context=c['trusted'],project_id=c['project'].id,draft=c['draft'].to_dict(),
        review=review,idempotency_key='manual-comparison')
    assert manual['fact_preparation']==p['fact_preparation'] and manual['final_prose']==p['final_prose']
    before=(c['root']/RELATIVE_PATH).read_bytes()
    document=prepare_chapter_document(document=before,book_id=c['project'].book_id,preparation=p,run_id='policy-comparison')
    author_document=prepare_chapter_document(document=before,book_id=c['project'].book_id,preparation=manual,run_id='author-comparison')
    automatic=decode_document(document,book_id=c['project'].book_id);author=decode_document(author_document,book_id=c['project'].book_id)
    assert automatic['current_state']==author['current_state']
    assert automatic['last_transition']['acceptance_kind']=='policy'
    assert automatic['last_transition']['policy_review']['review_source']=='policy'
    assert 'confirmation_sha256' not in automatic['last_transition']
    assert author['last_transition']['acceptance_kind']=='author' and 'policy_review' not in author['last_transition']
    verify_transition(document=document,previous_document=before,book_id=c['project'].book_id,chapter_prose=p['final_prose'].encode('utf-8'))


def test_author_only_adapter_still_rejects_policy_evidence(policy_case):
    c=policy_case
    with pytest.raises(ChapterCommitPreparationError):prepare_reviewed_chapter(trusted_context=c['trusted'],
        project_id=c['project'].id,draft=c['draft'].to_dict(),review=c['result']['policy_review'],idempotency_key='fake-author')


@pytest.mark.parametrize('kind',['author','facts','grant','extraction','boolean'])
def test_rehashed_policy_evidence_cannot_change_its_source_and_exact_binding(policy_case,kind):
    r=copy.deepcopy(policy_case['result']['policy_review'])
    if kind=='author':r['author_confirmed']=True
    elif kind=='facts':r['facts'][0]['review']['decision']='rejected'
    elif kind=='grant':r['batch_grant']['manifest']['chapters']=[99]
    elif kind=='extraction':r['extraction']['record_hash']='forged'
    else:r['revision']=True
    r['record_sha256']=digest({k:v for k,v in r.items() if k!='record_sha256'})
    with pytest.raises((ValueError,TypeError)):validate_policy_fact_review(r)


@pytest.mark.parametrize('key,value',[('expected_draft_revision',True),('expected_draft_sha256','f'*64),('expected_extraction_record_hash','f'*64)])
def test_owned_service_rejects_stale_cas_without_changing_author_draft(policy_case,key,value):
    c=policy_case
    with pytest.raises(PolicyReviewError):c['policy_service'].prepare(**{**c['policy_route'],key:value})
    assert c['publication'].preparations.drafts.get(c['draft'].id)==c['draft']


def test_policy_preparation_cannot_be_rehashed_as_an_author_confirmation(policy_case):
    p=copy.deepcopy(policy_case['result']['preparation']);p['binding']['acceptance_kind']='author'
    p['binding']['confirmation_sha256']='a'*64
    p['request_sha256']=digest({'version':p['preparation_version'],'binding':p['binding']})
    p['preparation_sha256']=digest({k:v for k,v in p.items() if k!='preparation_sha256'})
    c=policy_case
    with pytest.raises(ValueError):prepare_chapter_document(document=(c['root']/RELATIVE_PATH).read_bytes(),
        book_id=c['project'].book_id,preparation=p,run_id='must-not-publish')


@pytest.mark.parametrize('kind',['boolean_policy','unknown_policy','wrong_chapters'])
def test_embedded_policy_and_finite_range_cannot_be_rehashed_into_a_different_grant(policy_case,kind):
    r=copy.deepcopy(policy_case['result']['policy_review']);g=r['batch_grant'];m=g['manifest']
    if kind=='boolean_policy':m['action_policy']['max_repair_cycles']=True
    elif kind=='unknown_policy':m['action_policy']['schema_version']='unsupported_future_policy'
    else:m['chapters']=[2,3,4]
    m['manifest_sha256']=digest({k:v for k,v in m.items() if k!='manifest_sha256'})
    g['grant_sha256']=digest({k:v for k,v in g.items() if k!='grant_sha256'})
    r['record_sha256']=digest({k:v for k,v in r.items() if k!='record_sha256'})
    with pytest.raises(ValueError):validate_policy_fact_review(r)


def test_historical_policy_codec_uses_frozen_versions_while_new_reviews_reject_obsolete_grant(policy_case,monkeypatch):
    c=policy_case;r=c['result'];review=r['policy_review'];original=copy.deepcopy(r)
    from core import quality_decision
    monkeypatch.setitem(quality_decision.QUALITY_POLICIES,'standard',replace(quality_decision.QUALITY_POLICIES['standard'],version='future'))
    monkeypatch.setattr('core.engine.reviewed_policy_review.reviewed_quality_action_policy',lambda:{'schema_version':'future'})
    monkeypatch.setattr('core.quality.reviewed_disposition._POLICY',{'schema_version':'future'})
    assert validate_policy_fact_review(review)==review
    verify_quality_binding(r['quality'],r['preparation'])
    facts=copy.deepcopy(review['facts'])
    for f in facts:f['review']['decision']='pending'
    with pytest.raises(ValueError):build_policy_fact_review(grant=review['batch_grant'],project_id=review['project_id'],
        book_id=review['book_id'],chapter=review['chapter'],draft=c['draft'].to_dict(),
        candidate_intent=review['candidate_intent_hash'],context_sha256=review['context_sha256'],
        authority=review['authority'],extraction=review['extraction'],model_facts=facts)
    assert r==original


def test_grant_is_rechecked_after_quality_before_saving_policy_result(policy_case,monkeypatch):
    c=policy_case;service=c['policy_service'];original=service.batches._require_current_start;calls=[]
    def grant(context,batch_id):
        calls.append(1)
        value=original(context,batch_id)
        if len(calls)==2:value['grant']['grant_sha256']='f'*64
        return value
    monkeypatch.setattr(service.batches,'_require_current_start',grant)
    before=c['publication'].preparations.drafts.get(c['draft'].id)
    with pytest.raises(PolicyReviewError):service.prepare(**c['policy_route'])
    assert len(calls)==2 and c['publication'].preparations.drafts.get(c['draft'].id)==before


def test_policy_quality_cannot_inherit_manual_warning_override():
    c=seeded(include_bridge=False)
    try:
        r=c['result']
        assert r['quality']['local_quality_passed'] is True
        assert r['quality_disposition']['automatic_action']=='pause'
        with pytest.raises(ReviewedChapterTransitionError):verify_quality_binding(r['quality'],r['preparation'])
    finally:c['patch'].undo()


@pytest.mark.parametrize('phase,committed',[('after_apply_target',False),('after_commit_marker',True)])
def test_real_policy_transaction_process_death_reconciles_without_reviewer_or_provider(phase,committed):
    c=seeded()
    try:
        p=c['result']['preparation'];b=p['binding'];root=c['root'];project=c['project']
        route=dict(project_id=project.id,chapter=2,review_id=b['review_id'],expected_draft_revision=b['draft_revision'],
            expected_review_revision=b['review_revision'],expected_draft_sha256=b['draft_sha256'],
            expected_facts_sha256=b['facts_sha256'],idempotency_key='policy-'+b['review_id'])
        request=chapter_commit_request(book_id=project.book_id,request=route)
        # Stage the actual typed transaction under original book ownership.
        with c['publication']._owned_project(project.id):
            tx,args=build_reviewed_chapter_transaction(trusted_context=c['trusted'],chapter_preparation=p,
                request=request,run_id='policy-crash-synthetic')
            tx.prepare(**args)
        spec={'root':str(root),'database':str(c['db'].path),'machine':str(c['machine']),
            'project_id':project.id,'book_id':project.book_id,'phase':phase,'request_sha256':p['request_sha256']}
        path=root.parent/'policy-crash.fixture.json';path.write_text(json.dumps(spec),encoding='utf-8')
        child=r'''import json,os,socket,sys
from pathlib import Path
from core.engine.persistence_v2 import PersistenceV2Transaction
from tests.test_reviewed_batches import cold_service
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
service=cold_service({**spec,'config':{}}).publication
def stop(event,*unused):
    if event==spec['phase']:os._exit(78)
with service._owned_project(spec['project_id']):
    root=Path(spec['root'])
    tx=PersistenceV2Transaction(transaction_root=root/'.novelagent/runtime/reviewed/p',run_id='policy-crash-synthetic',
        book_id=spec['book_id'],root_map={'story_project':root,'runtime':root/'.novelagent/runtime'},fault_injector=stop)
    tx.commit()
'''
        process=subprocess.run([sys.executable,'-B','-c',child,str(path)],capture_output=True,text=True,timeout=50)
        assert process.returncode==78,process.stderr
        from core.engine.persistence_v2 import reconcile_pending_persistence_v2
        with c['publication']._owned_project(project.id):
            reconcile_pending_persistence_v2(root/'.novelagent/runtime/reviewed/p',expected_book_id=project.book_id)
        current=load_verified_reviewed_identity(root)['history']['value']
        assert current['committed_through']==(2 if committed else 1)
        if committed:
            assert current['last_transition']['acceptance_kind']=='policy'
            assert current['current_state']==p['fact_preparation']['state_after']
        assert c['publication'].preparations.drafts.get(c['draft'].id)==c['draft']
    finally:c['patch'].undo()


def test_actual_policy_transaction_and_cold_history_use_same_domain_reducer():
    c=seeded()
    try:
        p=c['result']['preparation'];project=c['project'];b=p['binding']
        assert c['result']['quality_disposition']['automatic_action']=='continue',c['result']['quality_disposition']
        route=dict(project_id=project.id,chapter=2,review_id=b['review_id'],expected_draft_revision=b['draft_revision'],
            expected_review_revision=b['review_revision'],expected_draft_sha256=b['draft_sha256'],
            expected_facts_sha256=b['facts_sha256'],idempotency_key='policy-'+b['review_id'])
        request=chapter_commit_request(book_id=project.book_id,request=route)
        with c['publication']._owned_project(project.id):
            tx,args=build_reviewed_chapter_transaction(trusted_context=c['trusted'],chapter_preparation=p,
                request=request,run_id='policy-formal-synthetic')
            tx.prepare(**args);result=tx.commit()
            assert result['committed']
        history=load_verified_reviewed_identity(c['root'])['history']
        assert history['value']['current_state']==p['fact_preparation']['state_after']
        assert history['value']['last_transition']['acceptance_kind']=='policy'
        # New process has no live service/author objects, and newer policy
        # selection must not reinterpret this already committed old history.
        script=r'''import json,socket,sys
from dataclasses import replace
from core import quality_decision
import core.quality.reviewed_disposition as disposition
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import digest
def deny(*args,**kwargs):raise AssertionError('cold history attempted network')
socket.socket.connect=deny
quality_decision.QUALITY_POLICIES['standard']=replace(quality_decision.QUALITY_POLICIES['standard'],version='future')
disposition._POLICY={'schema_version':'future'}
result=load_verified_reviewed_identity(sys.argv[1])['history']['value']
assert result['last_transition']['acceptance_kind']=='policy'
print(digest(result['current_state']))
'''
        cold=subprocess.run([sys.executable,'-B','-c',script,str(c['root'])],capture_output=True,text=True,timeout=35)
        assert cold.returncode==0,cold.stderr
        assert cold.stdout.strip()==digest(p['fact_preparation']['state_after'])
    finally:c['patch'].undo()
