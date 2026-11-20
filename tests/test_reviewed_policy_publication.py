"""Original owned policy publication, real atomic state/prose and cold receipts."""
import json,subprocess,sys
from dataclasses import replace
from pathlib import Path

import pytest

from core.engine.reviewed_batch_control import BatchControlError,BatchControlPaused
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.paths import canonical_prose_path
from core.story_project.reviewed_state_document import RELATIVE_PATH
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_policy_quality_stage import extracted,unchanged_calls
from workbench.reviewed_policy_quality import ReviewedPolicyQualityService
from workbench.reviewed_policy_publication import ReviewedPolicyPublicationService
from workbench.reviewed_batch_controls import ReviewedBatchControlService
from workbench.reviewed_policy_readback import ReviewedPolicyReadbackService


def ready(c):
    g=extracted(c)
    r=ReviewedPolicyQualityService(g['controls']).check(g['handle'],expected_control_sha256=g['view']['control']['record_sha256'])
    assert r['automatic_action']=='continue'
    return {**g,'view':r['control'],'quality':r['result']}


def test_actual_policy_publication_commits_original_request_and_cold_index_without_author_impersonation(batch_case,monkeypatch):
    c=batch_case;g=ready(c);service=ReviewedPolicyPublicationService(g['controls']);p=c['publication']
    def deny(*args,**kwargs):pytest.fail('policy publication called author preparation')
    monkeypatch.setattr(p.preparations,'prepare',deny)
    callbacks=[]
    def original_fault(event,*args):
        assert p.persistence_fault_injector is original_fault
        callbacks.append(event)
    p.persistence_fault_injector=original_fault
    assert service.resolve(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)['state']=='not_started'
    r=service.publish(g['handle'],expected_control_sha256=g['view']['control']['record_sha256'])
    assert r['control']['progress']['stage']=='readback' and r['control']['progress']['pending_step'] is None
    actual=r['result']['publication'];assert actual['publication_verified'] and actual['chapter']==2
    assert 'before_commit_marker' in callbacks and p.persistence_fault_injector is original_fault
    assert canonical_prose_path(c['root'],2).read_bytes()==g['quality']['review']['preparation']['final_prose'].encode('utf-8')
    verified=load_verified_reviewed_identity(c['root'])
    assert verified['history']['value']['committed_through']==2
    assert verified['history']['value']['last_transition']['acceptance_kind']=='policy'
    assert p.preparations.drafts.get(g['draft']['id']).to_dict()==g['draft']
    with c['db'].connection() as db:
        assert db.execute('SELECT COUNT(*) FROM chapter_fact_reviews').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM chapter_publication_index').fetchone()[0]==1
    frozen=(c['root']/RELATIVE_PATH).read_bytes()
    cold=ReviewedBatchControlService(p);resolver=ReviewedPolicyPublicationService(cold)
    assert not cold.handles and cold.get(project_id=c['project'].id,batch_id=g['batch_id'])['explicit_resume_required']
    assert resolver.resolve(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)['publication']==actual
    assert resolver.resolve(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)['publication']==actual
    assert (c['root']/RELATIVE_PATH).read_bytes()==frozen
    with pytest.raises(BatchControlPaused):resolver.publish(g['handle'],expected_control_sha256=r['control']['control']['record_sha256'])
    monkeypatch.setattr('workbench.reviewed_batches.get_config',lambda:replace(c['config'],openai_model='changed-model'))
    assert resolver.resolve(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)['publication']==actual
    observed=cold.get(project_id=c['project'].id,batch_id=g['batch_id'])
    with pytest.raises(ValueError):cold.command(project_id=c['project'].id,batch_id=g['batch_id'],action='resume',
        idempotency_key='changed-model-denied',expected_control_sha256=observed['control']['record_sha256'],author_confirmed=True)
    assert not cold.handles
    unchanged_calls(g)


def test_author_edit_after_quality_blocks_original_publication_without_writing_intent(batch_case):
    c=batch_case;g=ready(c);drafts=c['publication'].preparations.drafts
    changed=drafts.update(g['draft']['id'],text=g['draft']['text']+'\n作者保留的新判断。',expected_revision=1)
    before=(c['root']/RELATIVE_PATH).read_bytes()
    with pytest.raises(BatchControlError):ReviewedPolicyPublicationService(g['controls']).publish(g['handle'],
        expected_control_sha256=g['view']['control']['record_sha256'])
    assert (c['root']/RELATIVE_PATH).read_bytes()==before and drafts.get(changed.id)==changed
    assert not (c['root']/'.novelagent/runtime/reviewed/intents').exists()
    unchanged_calls(g)


def test_readback_proves_formal_parent_before_next_generation_and_missing_evidence_blocks_it(batch_case):
    from core.chapter_execution_budget import ChapterExecutionBudget
    from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
    from core.engine.chapter_candidate import ChapterCandidateStore
    from tests.test_reviewed_policy_generation import factory
    from workbench.reviewed_policy_generation import ReviewedPolicyGenerationService
    from core.story_project.reviewed_state_document import digest
    c=batch_case;g=ready(c);published=ReviewedPolicyPublicationService(g['controls']).publish(g['handle'],
        expected_control_sha256=g['view']['control']['record_sha256'])
    # Before the explicit readback stage, chapter3 has zero physical calls.
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0
    read=ReviewedPolicyReadbackService(g['controls']).readback(g['handle'],
        expected_control_sha256=published['control']['control']['record_sha256'])
    saved=read['result'];next_=saved['next_context']
    trusted=ReviewedChapterCandidateContextBuilder(c['root']).build(3)
    assert next_['context_sha256']==trusted.context_sha256
    assert next_['committed_through']==2 and next_['authoritative_state_sha256']==digest(trusted.story_project_context['reviewed_state']['authoritative_state'])
    assert read['control']['progress']['stage']=='generation' and read['control']['progress']['chapter']==3
    path=c['root']/'.novelagent/runtime/reviewed/batch_results'/(saved['step_id']+'.json');original=path.read_bytes();path.unlink()
    try:
        with pytest.raises((BatchControlError,ValueError)):ReviewedPolicyGenerationService(g['controls'],executor_factory=factory(g['gc'])).generate(
            g['handle'],expected_control_sha256=read['control']['control']['record_sha256'])
        assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0
    finally:path.write_bytes(original)
    nextgen=ReviewedPolicyGenerationService(g['controls'],executor_factory=factory(g['gc'])).generate(g['handle'],
        expected_control_sha256=read['control']['control']['record_sha256'])
    intent=ChapterCandidateStore(c['root']).load_intent(nextgen['generation']['candidate_id'])
    assert intent['chapter_index']==3 and intent['source']['context_sha256']==trusted.context_sha256
    assert nextgen['control']['progress']['stage']=='extraction' and nextgen['control']['progress']['chapter']==3
    assert g['gc'].read_text()=='2' and g['ec'].read_text()=='1'


def test_readback_rejects_damaged_formal_prose_without_advancing_or_overwriting_draft(batch_case):
    c=batch_case;g=ready(c);published=ReviewedPolicyPublicationService(g['controls']).publish(g['handle'],
        expected_control_sha256=g['view']['control']['record_sha256'])
    path=canonical_prose_path(c['root'],2);original=path.read_bytes();path.write_bytes(original+'\n合成损坏。'.encode('utf-8'))
    try:
        with pytest.raises((BatchControlError,ValueError,RuntimeError)):ReviewedPolicyReadbackService(g['controls']).readback(g['handle'],
            expected_control_sha256=published['control']['control']['record_sha256'])
        assert g['controls'].get(project_id=c['project'].id,batch_id=g['batch_id'])['progress']['stage']=='readback'
        assert c['publication'].preparations.drafts.get(g['draft']['id']).to_dict()==g['draft']
    finally:path.write_bytes(original)
    unchanged_calls(g)


@pytest.mark.parametrize('phase,committed',[('after_apply_target',False),('after_commit_marker',True),('after_file_commit_before_index',True)])
def test_actual_policy_publication_process_death_resolves_original_transaction_without_new_calls(batch_case,phase,committed):
    c=batch_case;g=ready(c);grant=g['controls'].batches.get(project_id=c['project'].id,batch_id=g['batch_id'])['grant']
    selection=grant['manifest']['model_selection']
    spec={'root':str(c['root']),'database':str(c['db'].path),'machine':str(c['machine']),
        'project_id':c['project'].id,'batch_id':g['batch_id'],'expected':g['view']['control']['record_sha256'],
        'phase':phase,'config':{k:v for k,v in selection.items() if k.startswith(('openai_','provider_'))}}
    spec['config']['openai_base_url']=c['config'].openai_base_url
    path=c['root'].parent/'policy-publication.fixture.json';path.write_text(json.dumps(spec),encoding='utf-8')
    child=subprocess.run([sys.executable,'-B','-c',CHILD,str(path)],capture_output=True,text=True,timeout=180)
    assert child.returncode==88,child.stderr
    cold=ReviewedBatchControlService(c['publication']);service=ReviewedPolicyPublicationService(cold)
    assert not cold.handles
    with pytest.raises(BatchControlPaused):service.publish(g['handle'],expected_control_sha256=g['view']['control']['record_sha256'])
    first=service.resolve(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)
    assert service.resolve(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)==first
    verified=load_verified_reviewed_identity(c['root'])
    assert verified['history']['value']['committed_through']==(2 if committed else 1)
    assert first['state']==('published' if committed else 'original_unpublished')
    if committed:
        observed=cold.get(project_id=c['project'].id,batch_id=g['batch_id'])
        assert observed['explicit_resume_required']
        resumed,handle=cold.command(project_id=c['project'].id,batch_id=g['batch_id'],action='resume',idempotency_key='cold-explicit-resume',
            expected_control_sha256=observed['control']['record_sha256'],author_confirmed=True)
        result=service.publish(handle,expected_control_sha256=resumed['control']['record_sha256'])
        assert result['result']['publication']==first['publication'] and result['control']['progress']['stage']=='readback'
    else:
        assert first['publication']['state']=='rolled_back' and first['publication']['retry_requires_new_key']
        assert not canonical_prose_path(c['root'],2).exists()
    assert c['publication'].preparations.drafts.get(g['draft']['id']).to_dict()==g['draft']
    unchanged_calls(g)


def test_three_chapters_commit_in_order_with_actual_readback_state_and_one_start(batch_case,monkeypatch):
    from core.chapter_execution_budget import ChapterExecutionBudget
    from core.engine.chapter_candidate import ChapterCandidateStore
    from core.engine.chapter_fact_contract import fact_contract_example
    from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
    from core.story_project.reviewed_state_document import digest
    from tests.test_reviewed_batch_controls import started
    from tests.test_reviewed_policy_generation import factory as generation_factory
    from tests.test_reviewed_policy_extractions import factory as extraction_factory
    from workbench.reviewed_policy_generation import ReviewedPolicyGenerationService
    from workbench.reviewed_policy_extractions import ReviewedPolicyExtractionService
    c=batch_case;root=c['root']
    outline=root/'大纲/细纲_第003章.md'
    (root/'大纲/细纲_第004章.md').write_text(outline.read_text(encoding='utf-8').replace('第3章','第4章').replace('第003章','第004章'),encoding='utf-8')
    c['route']['end_chapter']=4
    controls,batch,view,handle=started(c)
    def deny(*args,**kwargs):pytest.fail('automatic batch invoked author review/confirmation')
    monkeypatch.setattr(c['publication'].preparations,'prepare',deny)
    monkeypatch.setattr('workbench.chapter_facts.ChapterFactReviewService.create_from_extraction',deny)
    gc=root.parent/'three-generation.txt';ec=root.parent/'three-extraction.txt'
    good=Path('tests/fixtures/chapter_quality/good_chapter.md').read_text(encoding='utf-8').replace('林雪','林澈').replace('陈岚','周禾')
    publications=[];previous=None
    baseline=load_verified_reviewed_identity(root)['history']['value']['current_state']['numeric_counters']['alert_level']['current_value']
    for chapter in (2,3,4):
        print('three-chapter stage generation',chapter,flush=True)
        trusted=ReviewedChapterCandidateContextBuilder(root).build(chapter)
        state=trusted.story_project_context['reviewed_state'];blueprint=trusted.story_project_context['chapter_blueprint']
        assert state['committed_through']==chapter-1
        if previous is not None:
            assert previous['next_context']['context_sha256']==trusted.context_sha256
            assert previous['state_sha256']==digest(state['authoritative_state'])
            assert previous['document_sha256']==state['source_sha256']
        assert ChapterExecutionBudget(root,book_id=c['project'].book_id,chapter=chapter).store.report()['charged']['calls']==0
        text=(f'第{chapter}章，警戒行动进入第{chapter-1}轮。林澈左臂的旧伤再次裂开，闸门后的声音准确叫出了他的名字。周岚扶住他，继续检查封闭闸门旁的电路。\n\n'+good
            +'\n\n'+fact_contract_example()['chapter_text']+'\n\n'+'。'.join(b['text'] for b in blueprint['required_beats'])+'。\n\n'+blueprint['ending_pressure']+'。')
        g=ReviewedPolicyGenerationService(controls,executor_factory=generation_factory(gc,prose=text)).generate(handle,
            expected_control_sha256=view['control']['record_sha256'])
        intent=ChapterCandidateStore(root).load_intent(g['generation']['candidate_id'])
        assert intent['source']['context_sha256']==trusted.context_sha256 and intent['chapter_index']==chapter
        e=ReviewedPolicyExtractionService(controls,executor_factory=extraction_factory(ec)).extract(handle,
            expected_control_sha256=g['control']['control']['record_sha256'])
        q=ReviewedPolicyQualityService(controls).check(handle,expected_control_sha256=e['control']['control']['record_sha256'])
        assert q['automatic_action']=='continue',q['result']['review']['quality_disposition']
        print('three-chapter stage publication',chapter,flush=True)
        p=ReviewedPolicyPublicationService(controls).publish(handle,expected_control_sha256=q['control']['control']['record_sha256'])
        publications.append(p['result']['publication'])
        assert canonical_prose_path(root,chapter).read_bytes()==q['result']['review']['preparation']['final_prose'].encode('utf-8')
        assert controls.publication.preparations.drafts.get(g['draft']['id']).to_dict()==g['draft']
        if chapter<4:
            assert ChapterExecutionBudget(root,book_id=c['project'].book_id,chapter=chapter+1).store.report()['charged']['calls']==0
        r=ReviewedPolicyReadbackService(controls).readback(handle,expected_control_sha256=p['control']['control']['record_sha256'])
        previous=r['result'];view=r['control']
        verified=load_verified_reviewed_identity(root)['history']['value']
        assert verified['committed_through']==chapter and verified['last_transition']['acceptance_kind']=='policy'
        assert verified['current_state']['numeric_counters']['alert_level']['current_value']==baseline+2*(chapter-1)
        assert int(gc.read_text())==int(ec.read_text())==chapter-1
    assert view['progress']['status']=='completed' and view['progress']['chapter']==4 and previous['next_context'] is None
    assert [c['action'] for c in view['control']['commands']].count('start')==1
    assert all(c['action']!='resume' for c in view['control']['commands'])
    resolver=ReviewedPolicyPublicationService(ReviewedBatchControlService(c['publication']))
    for chapter,actual in zip((2,3,4),publications):
        assert resolver.resolve(project_id=c['project'].id,batch_id=batch,chapter=chapter)['publication']==actual
    with c['db'].connection() as db:
        assert db.execute('SELECT COUNT(*) FROM chapter_fact_reviews').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM chapter_publication_index').fetchone()[0]==3


CHILD=r'''import json,os,socket,sys
from pathlib import Path
from tests.test_reviewed_batches import cold_service
from workbench.reviewed_batch_controls import ReviewedBatchControlService
from workbench.reviewed_policy_publication import ReviewedPolicyPublicationService
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
def deny(*args,**kwargs):raise AssertionError('actual network forbidden')
socket.socket.connect=deny
controls=ReviewedBatchControlService(cold_service(spec).publication)
view,handle=controls.command(project_id=spec['project_id'],batch_id=spec['batch_id'],action='resume',idempotency_key='child-explicit-resume',
    expected_control_sha256=spec['expected'],author_confirmed=True)
def persistence_fault(event,*args):
    if event==spec['phase']:os._exit(88)
def service_fault(event,*args):
    if event==spec['phase']:os._exit(88)
controls.publication.persistence_fault_injector=persistence_fault
controls.publication.fault_injector=service_fault
ReviewedPolicyPublicationService(controls).publish(handle,expected_control_sha256=view['control']['record_sha256'])
raise AssertionError('process death point not reached')
'''
