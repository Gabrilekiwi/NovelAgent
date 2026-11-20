"""Actual full policy generation/extraction evidence and true process-death recovery."""
import copy,json,os,socket,subprocess,sys
from pathlib import Path

import pytest

from api.contracts import ModelResponse
from core.chapter_execution_budget import ChapterExecutionBudget,current_chapter_operation
from core.engine.chapter_candidate import ChapterCandidateStore
from core.engine.chapter_fact_contract import fact_contract_example
from core.engine.chapter_fact_extraction import (ChapterFactExtractionExecutor,ChapterFactExtractionStore,
    ChapterFactExtractionIntegrityError,_validate_extraction_intent,_record_hash)
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.engine.reviewed_batch_control import BatchControlError,BatchControlPaused,current_policy_step
from core.engine.reviewed_policy_extraction import policy_extraction_authorization
from core.model_call_runtime import current_model_call_runtime
from core.story_project.reviewed_state_document import digest
from tests.test_chapter_budget_runtime import args,response
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_batch_controls import started
from tests.test_reviewed_policy_generation import factory as generation_factory
from tests.test_workbench_chapter_candidates import _fixed_provenance
from workbench.reviewed_batch_controls import ReviewedBatchControlService
from workbench.reviewed_policy_generation import ReviewedPolicyGenerationService
from workbench.reviewed_policy_extractions import ReviewedPolicyExtractionService
from workbench.reviewed_policy_reviews import ReviewedPolicyReviewService


def proposal():
    p=copy.deepcopy(fact_contract_example()['proposal'])
    p['facts']=[f for f in p['facts'] if f['kind'] in {'event','counter'}]
    return p


def factory(counter,*,crash=None):
    def create(root):
        def provider(messages,call_id,unused):
            raw=json.dumps(proposal(),ensure_ascii=False);runtime=current_model_call_runtime();permit=current_policy_step()
            def operation():
                n=int(counter.read_text()) if counter.exists() else 0;counter.write_text(str(n+1),encoding='utf-8')
                if crash=='inside_dispatch':os._exit(84)
                base=response()
                return ModelResponse(raw,actual_model=base.actual_model,endpoint_type=base.endpoint_type,usage=base.usage)
            return runtime.execute_attempt(**args(call_id=call_id,stage='chapter_fact_extraction',
                endpoint_fingerprint=permit.model['endpoint_fingerprint'],operation=operation))
        def fault(event,unused):
            if crash=='after_provider_before_parse' and event=='after_provider_before_parse':os._exit(85)
        return ChapterFactExtractionExecutor(story_project_root=root,provider=provider,execution_kind='model',
            provenance_factory=_fixed_provenance,execution_budget_factory=ChapterExecutionBudget,fault_injector=fault)
    return create


def generated(c,*,text_transform=None):
    controls,batch_id,view,handle=started(c);root=c['root']
    trusted=ReviewedChapterCandidateContextBuilder(root).build(2);blueprint=trusted.story_project_context['chapter_blueprint']
    good=Path('tests/fixtures/chapter_quality/good_chapter.md').read_text(encoding='utf-8').replace('林雪','林澈').replace('陈岚','周禾')
    text=('林澈左臂的旧伤再次裂开，闸门后的声音准确叫出了他的名字。周岚扶住他，继续检查封闭闸门旁的电路。\n\n'+good
        +'\n\n'+fact_contract_example()['chapter_text']+'\n\n'+'。'.join(b['text'] for b in blueprint['required_beats'])+'。\n\n'+blueprint['ending_pressure']+'。')
    if text_transform is not None:text=text_transform(text)
    gc=root.parent/'generation-count.txt'
    result=ReviewedPolicyGenerationService(controls,executor_factory=generation_factory(gc,prose=text)).generate(handle,
        expected_control_sha256=view['control']['record_sha256'])
    return dict(controls=controls,batch_id=batch_id,view=result['control'],handle=handle,draft=result['draft'],generation=result['generation'],gc=gc,
        ec=root.parent/'extraction-count.txt')


def test_actual_full_policy_extraction_never_imports_author_review_and_reaches_quality(batch_case,monkeypatch):
    c=batch_case;g=generated(c);service=ReviewedPolicyExtractionService(g['controls'],executor_factory=factory(g['ec']))
    def deny(*args,**kwargs):pytest.fail('policy extraction called author-only review/start')
    monkeypatch.setattr('workbench.chapter_fact_extractions.ChapterFactExtractionService.start',deny)
    monkeypatch.setattr('workbench.chapter_facts.ChapterFactReviewService.create_from_extraction',deny)
    plan=service._plan(g['handle']);step='step_'+digest({'batch_id':g['batch_id'],'chapter':2,'stage':'extraction'})[:32]
    auth=policy_extraction_authorization(grant=plan['grant'],prepared=plan['prepared'],generation_result_sha256=g['generation']['result_sha256'],
        binding={'step_id':step,'execution_id':plan['execution_id']})
    with pytest.raises(BatchControlPaused):plan['executor'].execute_policy(policy_authorization=auth,**plan['request'])
    assert not plan['executor'].store.has_intent(plan['extraction_id']) and not g['ec'].exists()
    result=service.extract(g['handle'],expected_control_sha256=g['view']['control']['record_sha256'])
    r=result['extraction'];loaded=ChapterFactExtractionStore(c['root']).load(r['extraction_id'])
    intent,record=loaded['intent'],loaded['record']
    assert intent['schema_version']==record['schema_version']=='1.1' and 'author_confirmation' not in intent
    assert intent['policy_authorization']==auth and result['author_confirmed'] is False and result['fact_review'] is None
    assert record['evidence']['receipt_statuses']==['succeeded'] and len(record['evidence']['intent_hashes'])==1
    assert loaded['proposal']['facts'] and len(loaded['proposal']['facts'])==len(proposal()['facts'])
    assert result['control']['progress']['stage']=='quality' and g['ec'].read_text()==g['gc'].read_text()=='1'
    assert c['publication'].preparations.drafts.get(g['draft']['id']).to_dict()==g['draft']
    with c['db'].connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM chapter_fact_reviews').fetchone()[0]==0
    for field,value in [('author_confirmed',True),('input_sha256','f'*64),('chapter',True)]:
        bad=copy.deepcopy(intent);bad['policy_authorization'][field]=value
        bad['intent_hash']=_record_hash(bad,'intent_hash')
        with pytest.raises(ChapterFactExtractionIntegrityError):_validate_extraction_intent(bad)
    from workbench.chapter_fact_extractions import ChapterFactExtractionService,ChapterFactExtractionWorkbenchIntegrity
    public=ChapterFactExtractionService(projects=c['publication'].projects,candidate_drafts=c['publication'].preparations.drafts,
        chapter_facts=c['publication'].preparations.fact_reviews,context_builder_factory=ReviewedChapterCandidateContextBuilder)
    observed=public.get(g['draft']['id'],r['extraction_id'])
    assert observed['status']=='completed' and observed['proposal']['fact_count']==len(proposal()['facts']) and observed['fact_review'] is None
    # Full source/parse/normalization feeds the real final policy reviewer.
    reviewed=ReviewedPolicyReviewService(c['publication']).prepare(project_id=c['project'].id,batch_id=g['batch_id'],
        draft_id=g['draft']['id'],extraction_id=r['extraction_id'],expected_draft_revision=g['draft']['revision'],
        expected_draft_sha256=g['draft']['sha256'],expected_extraction_record_hash=r['extraction_record_hash'])
    assert reviewed['quality']['quality_coverage_complete'] and reviewed['quality_disposition']['automatic_action']=='continue'
    assert reviewed['policy_review']['review_source']=='policy' and reviewed['policy_review']['author_confirmed'] is False
    assert g['ec'].read_text()==g['gc'].read_text()=='1'
    # Recompute the actual recursive Intent hash, so this reaches source
    # validation instead of merely failing at the outer checksum.
    path=plan['executor'].store.intent_path(r['extraction_id']);original=path.read_bytes()
    bad=copy.deepcopy(intent);bad['policy_authorization']['author_confirmed']=True;bad['intent_hash']=_record_hash(bad,'intent_hash')
    path.write_text(json.dumps(bad,ensure_ascii=False),encoding='utf-8')
    try:
        with pytest.raises(ChapterFactExtractionWorkbenchIntegrity):public.get(g['draft']['id'],r['extraction_id'])
    finally:path.write_bytes(original)
    assert g['ec'].read_text()==g['gc'].read_text()=='1'


def test_author_edit_after_generation_blocks_policy_extraction_before_first_call(batch_case):
    c=batch_case;g=generated(c);drafts=c['publication'].preparations.drafts
    edited=drafts.update(g['draft']['id'],text=g['draft']['text']+'\n作者新的判断。',expected_revision=1)
    with pytest.raises(BatchControlError):ReviewedPolicyExtractionService(g['controls'],executor_factory=factory(g['ec'])).extract(
        g['handle'],expected_control_sha256=g['view']['control']['record_sha256'])
    assert not g['ec'].exists() and drafts.get(edited.id)==edited
    assert g['controls'].get(project_id=c['project'].id,batch_id=g['batch_id'])['control']==g['view']['control']


def test_author_edit_after_extraction_result_blocks_finish_without_overwriting_or_second_call(batch_case):
    c=batch_case;g=generated(c);drafts=c['publication'].preparations.drafts;edited=[]
    def fault(event):
        if event=='after_extraction_result_before_finish':
            edited.append(drafts.update(g['draft']['id'],text=g['draft']['text']+'\n作者决定保留的新句。',expected_revision=1))
    with pytest.raises(BatchControlError):ReviewedPolicyExtractionService(g['controls'],executor_factory=factory(g['ec']),fault_injector=fault).extract(
        g['handle'],expected_control_sha256=g['view']['control']['record_sha256'])
    assert drafts.get(edited[0].id)==edited[0] and g['ec'].read_text()=='1'
    state=g['controls'].get(project_id=c['project'].id,batch_id=g['batch_id'])
    assert state['progress']['stage']=='extraction' and state['progress']['pending_step'] is not None


@pytest.mark.parametrize('phase',['after_result','after_provider_before_parse','inside_dispatch'])
def test_actual_process_death_uses_original_extraction_or_rejects_unknown_without_new_call(batch_case,phase):
    c=batch_case;g=generated(c);grant=g['controls'].batches.get(project_id=c['project'].id,batch_id=g['batch_id'])['grant']
    selection=grant['manifest']['model_selection']
    spec={'root':str(c['root']),'database':str(c['db'].path),'machine':str(c['machine']),
        'project_id':c['project'].id,'batch_id':g['batch_id'],'expected':g['view']['control']['record_sha256'],
        'counter':str(g['ec']),'phase':phase,'config':{k:v for k,v in selection.items() if k.startswith(('openai_','provider_'))}}
    spec['config']['openai_base_url']=c['config'].openai_base_url
    path=c['root'].parent/'policy-extraction.fixture.json';path.write_text(json.dumps(spec),encoding='utf-8')
    child=subprocess.run([sys.executable,'-B','-c',CHILD,str(path)],capture_output=True,text=True,timeout=150)
    assert child.returncode=={'after_result':83,'inside_dispatch':84,'after_provider_before_parse':85}[phase],child.stderr
    assert g['ec'].read_text()==g['gc'].read_text()=='1'
    cold=ReviewedBatchControlService(c['publication']);current=cold.get(project_id=c['project'].id,batch_id=g['batch_id'])
    assert current['explicit_resume_required'] and not cold.handles and current['progress']['stage']=='extraction'
    service=ReviewedPolicyExtractionService(cold,executor_factory=factory(g['ec']))
    with pytest.raises(BatchControlPaused):service.extract(g['handle'],expected_control_sha256=current['control']['record_sha256'])
    original=current['progress']['pending_step']
    budget=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2)
    time_bytes=budget.time_clock.path.read_bytes()
    if phase=='inside_dispatch':
        from workbench.chapter_commits import ChapterCommitConflict
        with pytest.raises(ChapterCommitConflict):cold.command(project_id=c['project'].id,batch_id=g['batch_id'],action='resume',
            idempotency_key='cold-explicit-resume',expected_control_sha256=current['control']['record_sha256'],author_confirmed=True)
        assert cold.get(project_id=c['project'].id,batch_id=g['batch_id'])['control']==current['control']
    else:
        resumed,handle=cold.command(project_id=c['project'].id,batch_id=g['batch_id'],action='resume',idempotency_key='cold-explicit-resume',
            expected_control_sha256=current['control']['record_sha256'],author_confirmed=True)
        assert resumed['progress']['pending_step']==original
        result=service.extract(handle,expected_control_sha256=resumed['control']['record_sha256'])
        assert result['control']['progress']['stage']=='quality' and result['extraction']['execution_id']==original['execution_id']
        assert result['extraction']['input_sha256']==original['input_sha256']
        if phase=='after_provider_before_parse':
            assert budget.time_clock.report()['active_status']=='unknown'
            assert budget.time_clock.path.read_bytes()==time_bytes
            from core.chapter_execution_time import ChapterTimeUnknown
            with pytest.raises(ChapterTimeUnknown):
                with budget.operation('execution_no_new_work_after_unknown',allow_provider_dispatch=False):
                    pytest.fail('local recovery reopened unknown original time')
    assert g['ec'].read_text()==g['gc'].read_text()=='1'


@pytest.mark.parametrize('value',[None,[],False])
def test_invalid_extraction_input_is_a_typed_integrity_error(value):
    with pytest.raises(ChapterFactExtractionIntegrityError):_validate_extraction_intent(value)


CHILD=r'''import json,os,socket,sys
from pathlib import Path
from tests.test_reviewed_batches import cold_service
from tests.test_reviewed_policy_extractions import factory
from workbench.reviewed_batch_controls import ReviewedBatchControlService
from workbench.reviewed_policy_extractions import ReviewedPolicyExtractionService
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
def deny(*args,**kwargs):raise AssertionError('actual network forbidden')
socket.socket.connect=deny
controls=ReviewedBatchControlService(cold_service(spec).publication)
view,handle=controls.command(project_id=spec['project_id'],batch_id=spec['batch_id'],action='resume',idempotency_key='child-explicit-resume',
    expected_control_sha256=spec['expected'],author_confirmed=True)
def fault(event):
    if event=='after_extraction_result_before_finish' and spec['phase']=='after_result':os._exit(83)
service=ReviewedPolicyExtractionService(controls,executor_factory=factory(Path(spec['counter']),crash=spec['phase']),fault_injector=fault)
service.extract(handle,expected_control_sha256=view['control']['record_sha256'])
raise AssertionError('process death point not reached')
'''
