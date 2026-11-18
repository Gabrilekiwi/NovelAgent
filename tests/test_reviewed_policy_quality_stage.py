"""Actual batch generation/extraction/quality; local-only scope and cold results."""
import copy,json,os,subprocess,sys
from pathlib import Path

import pytest

from core.chapter_execution_budget import ChapterExecutionBudget,current_chapter_operation
from core.engine.reviewed_batch_control import BatchControlError,BatchControlPaused,current_policy_step
from core.story_project.reviewed_state_document import digest
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_policy_extractions import generated,factory
from workbench.reviewed_policy_extractions import ReviewedPolicyExtractionService
from workbench.reviewed_policy_quality import ReviewedPolicyQualityService,validate_policy_quality_result
from workbench.reviewed_batch_controls import ReviewedBatchControlService


def extracted(c,*,text_transform=None):
    g=generated(c,text_transform=text_transform)
    r=ReviewedPolicyExtractionService(g['controls'],executor_factory=factory(g['ec'])).extract(g['handle'],
        expected_control_sha256=g['view']['control']['record_sha256'])
    return {**g,'view':r['control'],'extraction':r['extraction']}


def unchanged_calls(g):assert g['gc'].read_text()==g['ec'].read_text()=='1'


def test_actual_quality_stage_uses_original_local_scope_and_only_verified_continue_advances(batch_case,monkeypatch):
    c=batch_case;g=extracted(c);checks=[]
    from workbench import reviewed_policy_reviews as module
    original=module.prepare_candidate_chapter_quality
    def checked(**kwargs):
        permit=current_policy_step();action=current_chapter_operation()
        assert permit.binding['stage']=='quality' and action.provider_dispatch_allowed is False
        assert action.execution_id==permit.binding['execution_id']
        checks.append(permit)
        from tests.test_reviewed_batch_controls import runtime
        from tests.test_chapter_budget_runtime import args
        bound=runtime(c,permit.binding['execution_id'],permit)
        with pytest.raises(BatchControlError):bound.execute_attempt(**args(endpoint_fingerprint=permit.model['endpoint_fingerprint'],
            operation=lambda:pytest.fail('quality called Provider')))
        assert bound.chapter_budget.attempts()==[]
        return original(**kwargs)
    monkeypatch.setattr(module,'prepare_candidate_chapter_quality',checked)
    result=ReviewedPolicyQualityService(g['controls']).check(g['handle'],expected_control_sha256=g['view']['control']['record_sha256'])
    r=result['result'];assert validate_policy_quality_result(r)==r
    assert result['automatic_action']=='continue' and result['control']['progress']['stage']=='publication'
    assert result['control']['progress']['pending_step'] is None and not result['publication_authorized']
    assert len(checks)==1 and r['review']['policy_review']['author_confirmed'] is False
    with pytest.raises(BatchControlPaused):
        with checks[0]._owned_project(c['publication'],c['project'].id):pytest.fail('closed scope borrowed original ownership')
    assert c['publication'].preparations.drafts.get(g['draft']['id']).to_dict()==g['draft']
    cold=ReviewedBatchControlService(c['publication']);read=ReviewedPolicyQualityService(cold)
    assert read.get(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)['result']==r
    assert cold.get(project_id=c['project'].id,batch_id=g['batch_id'])['explicit_resume_required'] and not cold.handles
    with pytest.raises(BatchControlPaused):read.check(g['handle'],expected_control_sha256=result['control']['control']['record_sha256'])
    for kind in ('disposition','grant','preparation','type','input_sha256','extraction_result_sha256'):
        bad=copy.deepcopy(r)
        if kind=='disposition':bad['review']['quality_disposition']['automatic_action']='pause'
        elif kind=='grant':bad['grant_sha256']='f'*64
        elif kind=='preparation':bad['review']['preparation']['binding']['draft_revision']=99
        elif kind=='type':bad['review']['publication_authorized']=0
        else:bad[kind]=True
        bad['result_sha256']=digest({k:v for k,v in bad.items() if k!='result_sha256'})
        with pytest.raises(BatchControlError):validate_policy_quality_result(bad)
    unchanged_calls(g)


@pytest.mark.parametrize('mode',['semantic_pause','repair_once'])
def test_actual_failed_quality_pauses_and_does_not_create_repair_or_next_chapter_call(batch_case,mode):
    def transform(text):
        if mode=='semantic_pause':return text.split('\n\n',1)[1]
        from tests.test_reviewed_quality_disposition import PARAGRAPH
        return text+'\n\n'+PARAGRAPH+'\n\n'+PARAGRAPH
    c=batch_case;g=extracted(c,text_transform=transform)
    r=ReviewedPolicyQualityService(g['controls']).check(g['handle'],expected_control_sha256=g['view']['control']['record_sha256'])
    assert r['automatic_action']==('pause' if mode=='semantic_pause' else 'repair_once'),r['result']['review']['quality_disposition']
    assert r['control']['progress']['status']=='paused' and r['control']['progress']['stage']=='quality'
    assert r['control']['progress']['pending_step'] is not None and not g['controls'].handles
    assert not r['repair_execution_authorized'] and not r['publication_authorized']
    if mode=='semantic_pause':assert r['result']['review']['quality']['local_quality_passed']
    with pytest.raises(BatchControlPaused):ReviewedPolicyQualityService(g['controls']).check(g['handle'],
        expected_control_sha256=r['control']['control']['record_sha256'])
    with c['db'].connection() as db:assert db.execute('SELECT COUNT(*) FROM chapter_fact_reviews').fetchone()[0]==0
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0
    assert c['publication'].preparations.drafts.get(g['draft']['id']).to_dict()==g['draft']
    unchanged_calls(g)


def test_author_edit_after_quality_result_preserved_and_blocks_publication_stage(batch_case):
    c=batch_case;g=extracted(c);edited=[];drafts=c['publication'].preparations.drafts
    def fault(event):
        if event=='after_quality_result_before_finish':
            edited.append(drafts.update(g['draft']['id'],text=g['draft']['text']+'\n作者的新判断。',expected_revision=1))
    with pytest.raises(BatchControlError):ReviewedPolicyQualityService(g['controls'],fault_injector=fault).check(g['handle'],
        expected_control_sha256=g['view']['control']['record_sha256'])
    state=g['controls'].get(project_id=c['project'].id,batch_id=g['batch_id'])
    assert state['progress']['stage']=='quality' and state['progress']['pending_step'] is not None
    assert drafts.get(edited[0].id)==edited[0];unchanged_calls(g)


def test_pause_during_actual_quality_prevents_result_publication_and_stage_advance(batch_case,monkeypatch):
    c=batch_case;g=extracted(c)
    from workbench import reviewed_policy_reviews as module
    original=module.prepare_candidate_chapter_quality
    def pause(**kwargs):
        controls=g['controls'];state=controls.get(project_id=c['project'].id,batch_id=g['batch_id'])
        controls.command(project_id=c['project'].id,batch_id=g['batch_id'],action='pause',idempotency_key='author-pause-during-quality',
            expected_control_sha256=state['control']['record_sha256'],author_confirmed=True)
        return original(**kwargs)
    monkeypatch.setattr(module,'prepare_candidate_chapter_quality',pause)
    with pytest.raises(BatchControlPaused):ReviewedPolicyQualityService(g['controls']).check(g['handle'],
        expected_control_sha256=g['view']['control']['record_sha256'])
    state=g['controls'].get(project_id=c['project'].id,batch_id=g['batch_id'])
    step=state['progress']['pending_step']['step_id']
    assert state['progress']['status']=='paused' and state['progress']['stage']=='quality'
    assert not (c['root']/'.novelagent/runtime/reviewed/batch_results'/(step+'.json')).exists()
    unchanged_calls(g)


@pytest.mark.parametrize('phase',['after_quality_result_before_finish','after_quality_result_inside_scope','during_finish_control'])
def test_actual_quality_process_death_rechecks_original_result_and_preserves_unknown_time(batch_case,phase):
    c=batch_case;g=extracted(c);grant=g['controls'].batches.get(project_id=c['project'].id,batch_id=g['batch_id'])['grant']
    selection=grant['manifest']['model_selection']
    spec={'root':str(c['root']),'database':str(c['db'].path),'machine':str(c['machine']),
        'project_id':c['project'].id,'book_id':c['project'].book_id,'batch_id':g['batch_id'],'expected':g['view']['control']['record_sha256'],
        'phase':phase,'config':{k:v for k,v in selection.items() if k.startswith(('openai_','provider_'))}}
    spec['config']['openai_base_url']=c['config'].openai_base_url
    path=c['root'].parent/'policy-quality.fixture.json';path.write_text(json.dumps(spec),encoding='utf-8')
    child=subprocess.run([sys.executable,'-B','-c',CHILD,str(path)],capture_output=True,text=True,timeout=120)
    assert child.returncode==(87 if phase=='during_finish_control' else 86),child.stderr
    cold=ReviewedBatchControlService(c['publication']);service=ReviewedPolicyQualityService(cold)
    if phase=='during_finish_control':
        with pytest.raises(BatchControlError):cold.get(project_id=c['project'].id,batch_id=g['batch_id'])
        owner=c['publication']._owner(c['project'].book_id);machine=owner.directory/('bc-'+g['batch_id']+'.json')
        original=machine.read_bytes()
        result=service.recover_finish(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)
        assert result['control']['progress']['stage']=='publication' and result['control']['explicit_resume_required']
        assert not result['execution_permit'] and not cold.handles and machine.read_bytes()==original
        assert (c['root']/'.novelagent/runtime/reviewed/batch_controls'/(g['batch_id']+'.json')).read_bytes()==original
        assert service.recover_finish(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)==result
        assert machine.read_bytes()==original
        unchanged_calls(g)
        return
    current=cold.get(project_id=c['project'].id,batch_id=g['batch_id'])
    assert current['explicit_resume_required'] and not cold.handles
    saved=service.get(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)['result']
    result_path=c['root']/'.novelagent/runtime/reviewed/batch_results'/(saved['step_id']+'.json');original_bytes=result_path.read_bytes()
    budget=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2);time_bytes=budget.time_clock.path.read_bytes()
    with pytest.raises(BatchControlPaused):service.check(g['handle'],expected_control_sha256=current['control']['record_sha256'])
    resumed,handle=cold.command(project_id=c['project'].id,batch_id=g['batch_id'],action='resume',idempotency_key='cold-explicit-resume',
        expected_control_sha256=current['control']['record_sha256'],author_confirmed=True)
    assert resumed['progress']['pending_step']==current['progress']['pending_step']
    result=service.check(handle,expected_control_sha256=resumed['control']['record_sha256'])
    assert result['result']==saved and result_path.read_bytes()==original_bytes and budget.time_clock.path.read_bytes()==time_bytes
    if phase=='after_quality_result_before_finish':
        assert result['control']['progress']['stage']=='publication' and not result['time_blocked']
    else:
        assert result['time_blocked'] and result['control']['progress']['status']=='paused'
        assert result['control']['progress']['stage']=='quality' and budget.time_clock.report()['active_status']=='unknown'
    unchanged_calls(g)


def test_time_exhausted_between_quality_and_finish_blocks_advancement(batch_case,monkeypatch):
    from core.chapter_execution_time import ChapterExecutionClock,ChapterTimeExceeded
    c=batch_case;g=extracted(c);original=g['controls'].finish_step
    def exhaust(handle,**kwargs):
        budget=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2)
        ticks=iter([0,901_000_000_000])
        budget.time_clock=ChapterExecutionClock(budget.root,budget.store.frozen_budget(),monotonic_ns=lambda:next(ticks))
        with budget.operation('execution_synthetic_other_local_work',allow_provider_dispatch=False):pass
        assert budget.time_clock.report()['active_limit_reached']
        return original(handle,**kwargs)
    monkeypatch.setattr(g['controls'],'finish_step',exhaust)
    with pytest.raises(ChapterTimeExceeded):ReviewedPolicyQualityService(g['controls']).check(g['handle'],
        expected_control_sha256=g['view']['control']['record_sha256'])
    state=g['controls'].get(project_id=c['project'].id,batch_id=g['batch_id'])
    assert state['progress']['stage']=='quality' and state['progress']['pending_step'] is not None
    unchanged_calls(g)


CHILD=r'''import json,os,socket,sys
from pathlib import Path
from tests.test_reviewed_batches import cold_service
from workbench.reviewed_batch_controls import ReviewedBatchControlService
from workbench.reviewed_policy_quality import ReviewedPolicyQualityService
from core.story_project.reviewed_state_document import digest
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
def deny(*args,**kwargs):raise AssertionError('actual network forbidden')
socket.socket.connect=deny
controls=ReviewedBatchControlService(cold_service(spec).publication)
view,handle=controls.command(project_id=spec['project_id'],batch_id=spec['batch_id'],action='resume',idempotency_key='child-explicit-resume',
    expected_control_sha256=spec['expected'],author_confirmed=True)
def crash(event):
    if event==spec['phase']:os._exit(86)
def control_fault(event):
    if event=='after_control_machine' and spec['phase']=='during_finish_control':
        path=controls.publication._owner(spec['book_id']).directory/('bc-'+spec['batch_id']+'.json')
        command=json.loads(path.read_text(encoding='utf-8'))['commands'][-1]
        step='step_'+digest({'batch_id':spec['batch_id'],'chapter':2,'stage':'quality'})[:32]
        if command['action']=='finish_step' and command['payload']['step_id']==step:os._exit(87)
controls.fault_injector=control_fault
ReviewedPolicyQualityService(controls,fault_injector=crash).check(handle,expected_control_sha256=view['control']['record_sha256'])
raise AssertionError('quality death point not reached')
'''
