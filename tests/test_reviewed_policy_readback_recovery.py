"""Real process death, original command mirror recovery and final time fence."""
import json,subprocess,sys
from pathlib import Path

import pytest

from core.chapter_execution_budget import ChapterExecutionBudget
from core.chapter_execution_time import ChapterExecutionClock,ChapterTimeExceeded
from core.engine.reviewed_batch_control import BatchControlError,BatchControlPaused
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_policy_publication import ready
from tests.test_reviewed_policy_quality_stage import unchanged_calls
from workbench.reviewed_batch_controls import ReviewedBatchControlService,control_paths
from workbench.reviewed_policy_publication import ReviewedPolicyPublicationService
from workbench.reviewed_policy_readback import ReviewedPolicyReadbackService


def published(c):
    g=ready(c)
    r=ReviewedPolicyPublicationService(g['controls']).publish(g['handle'],
        expected_control_sha256=g['view']['control']['record_sha256'])
    return {**g,'view':r['control'],'publication':r['result']}


def test_saved_readback_process_death_and_later_chapter_torn_resume_preserve_original_evidence(batch_case):
    c=batch_case;g=published(c)
    grant=g['controls'].batches.get(project_id=c['project'].id,batch_id=g['batch_id'])['grant']
    selection=grant['manifest']['model_selection']
    spec={'root':str(c['root']),'database':str(c['db'].path),'machine':str(c['machine']),
        'project_id':c['project'].id,'batch_id':g['batch_id'],'expected':g['view']['control']['record_sha256'],
        'config':{k:v for k,v in selection.items() if k.startswith(('openai_','provider_'))}}
    spec['config']['openai_base_url']=c['config'].openai_base_url
    fixture=c['root'].parent/'readback.fixture.json';fixture.write_text(json.dumps(spec),encoding='utf-8')
    child=subprocess.run([sys.executable,'-B','-c',CHILD,str(fixture)],capture_output=True,text=True,timeout=180)
    assert child.returncode==89,child.stderr
    cold=ReviewedBatchControlService(c['publication'])
    view=cold.get(project_id=c['project'].id,batch_id=g['batch_id'])
    assert not cold.handles and view['explicit_resume_required'] and view['progress']['stage']=='readback'
    pending=view['progress']['pending_step']
    path=c['root']/'.novelagent/runtime/reviewed/batch_results'/(pending['step_id']+'.json')
    original=path.read_bytes()
    with pytest.raises(BatchControlPaused):ReviewedPolicyReadbackService(cold).readback(g['handle'],
        expected_control_sha256=view['control']['record_sha256'])
    resumed,handle=cold.command(project_id=c['project'].id,batch_id=g['batch_id'],action='resume',
        idempotency_key='explicit-readback-resume',expected_control_sha256=view['control']['record_sha256'],author_confirmed=True)
    r=ReviewedPolicyReadbackService(cold).readback(handle,expected_control_sha256=resumed['control']['record_sha256'])
    assert path.read_bytes()==original and r['control']['progress']['chapter']==3
    assert r['control']['progress']['stage']=='generation'
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0
    # Die after the next chapter's original resume reached only its machine copy.
    class Stop(BaseException):pass
    def crash(event):
        if event=='after_control_machine':raise Stop()
    torn=ReviewedBatchControlService(c['publication'],fault_injector=crash)
    expected=r['control']['control']['record_sha256']
    with pytest.raises(Stop):torn.command(project_id=c['project'].id,batch_id=g['batch_id'],action='resume',
        idempotency_key='later-original-resume',expected_control_sha256=expected,author_confirmed=True)
    context=torn._control_context(c['project'].id);_,machine,book=control_paths(context,g['batch_id'])
    machine_bytes=machine.read_bytes();assert machine_bytes!=book.read_bytes()
    restarted=ReviewedBatchControlService(c['publication'])
    fixed,none=restarted.command(project_id=c['project'].id,batch_id=g['batch_id'],action='resume',
        idempotency_key='later-original-resume',expected_control_sha256=expected,author_confirmed=True)
    assert none is None and not restarted.handles and fixed['explicit_resume_required']
    assert machine.read_bytes()==book.read_bytes()==machine_bytes and path.read_bytes()==original
    assert load_verified_reviewed_identity(c['root'])['history']['value']['committed_through']==2
    unchanged_calls(g)


def test_time_exhausted_between_readback_and_finish_blocks_next_chapter(batch_case,monkeypatch):
    c=batch_case;g=published(c);original=g['controls'].finish_step
    def exhaust(handle,**kwargs):
        budget=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2)
        ticks=iter([0,901_000_000_000])
        budget.time_clock=ChapterExecutionClock(budget.root,budget.store.frozen_budget(),monotonic_ns=lambda:next(ticks))
        with budget.operation('execution_readback_other_local_work',allow_provider_dispatch=False):pass
        return original(handle,**kwargs)
    monkeypatch.setattr(g['controls'],'finish_step',exhaust)
    with pytest.raises(ChapterTimeExceeded):ReviewedPolicyReadbackService(g['controls']).readback(g['handle'],
        expected_control_sha256=g['view']['control']['record_sha256'])
    view=g['controls'].get(project_id=c['project'].id,batch_id=g['batch_id'])
    assert view['progress']['chapter']==2 and view['progress']['stage']=='readback' and view['progress']['pending_step']
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0
    unchanged_calls(g)


@pytest.mark.parametrize('stage',['publication','readback'])
def test_finish_process_death_reconciles_only_original_command_without_new_permission(batch_case,stage):
    from core.story_project.reviewed_state_document import digest
    c=batch_case;g=ready(c) if stage=='publication' else published(c)
    grant=g['controls'].batches.get(project_id=c['project'].id,batch_id=g['batch_id'])['grant']
    selection=grant['manifest']['model_selection']
    spec={'root':str(c['root']),'database':str(c['db'].path),'machine':str(c['machine']),
        'project_id':c['project'].id,'batch_id':g['batch_id'],'expected':g['view']['control']['record_sha256'],
        'stage':stage,'config':{k:v for k,v in selection.items() if k.startswith(('openai_','provider_'))}}
    spec['config']['openai_base_url']=c['config'].openai_base_url
    fixture=c['root'].parent/'finish.fixture.json';fixture.write_text(json.dumps(spec),encoding='utf-8')
    child=subprocess.run([sys.executable,'-B','-c',FINISH_CHILD,str(fixture)],capture_output=True,text=True,timeout=240)
    assert child.returncode==90,child.stderr
    cold=ReviewedBatchControlService(c['publication'])
    service=ReviewedPolicyPublicationService(cold) if stage=='publication' else ReviewedPolicyReadbackService(cold)
    context=cold._control_context(c['project'].id);_,machine,book=control_paths(context,g['batch_id'])
    original_machine=machine.read_bytes();original_book=book.read_bytes();assert original_machine!=original_book
    saved=service.get(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)['result']
    path=c['root']/'.novelagent/runtime/reviewed/batch_results'/(saved['step_id']+'.json');original=path.read_bytes()
    assert not cold.handles
    # A complete result must never manufacture a finish missing from both logs.
    machine.write_bytes(original_book)
    try:
        with pytest.raises(BatchControlError):service.recover_finish(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)
        assert machine.read_bytes()==book.read_bytes()==original_book and path.read_bytes()==original
    finally:machine.write_bytes(original_machine)
    # Valid outer checksum cannot make a boolean SHA acceptable to the reader.
    bad=dict(saved,input_sha256=True);bad['result_sha256']=digest({k:v for k,v in bad.items() if k!='result_sha256'})
    path.write_text(json.dumps(bad,ensure_ascii=False),encoding='utf-8')
    try:
        with pytest.raises(BatchControlError):service.get(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)
    finally:path.write_bytes(original)
    result=service.recover_finish(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)
    assert result['result']==saved and not cold.handles and result['control']['explicit_resume_required']
    assert result['control']['progress']['stage']==('readback' if stage=='publication' else 'generation')
    assert result['control']['progress']['chapter']==(2 if stage=='publication' else 3)
    assert machine.read_bytes()==book.read_bytes()==original_machine and path.read_bytes()==original
    assert service.recover_finish(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)==result
    assert machine.read_bytes()==book.read_bytes()==original_machine
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0
    unchanged_calls(g)


@pytest.mark.parametrize('phase,committed',[('before_commit_marker',False),('after_commit_marker',True)])
def test_pause_at_original_commit_marker_preserves_original_transaction_without_advancing(batch_case,phase,committed):
    c=batch_case;g=ready(c);signals=ReviewedBatchControlService(c['publication']);seen=[]
    def pause(event,*args):
        if event==phase:
            current=signals.get(project_id=c['project'].id,batch_id=g['batch_id'])
            view=signals.command(project_id=c['project'].id,batch_id=g['batch_id'],action='pause',
                idempotency_key='pause-at-marker',expected_control_sha256=current['control']['record_sha256'],author_confirmed=True)
            seen.append(view)
    c['publication'].persistence_fault_injector=pause
    with pytest.raises(BatchControlError):ReviewedPolicyPublicationService(g['controls']).publish(g['handle'],
        expected_control_sha256=g['view']['control']['record_sha256'])
    assert len(seen)==1 and seen[0]['progress']['status']=='paused'
    result=ReviewedPolicyPublicationService(signals).resolve(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)
    assert result['state']==('published' if committed else 'original_unpublished')
    assert load_verified_reviewed_identity(c['root'])['history']['value']['committed_through']==(2 if committed else 1)
    assert signals.get(project_id=c['project'].id,batch_id=g['batch_id'])['progress']['status']=='paused'
    assert c['publication'].preparations.drafts.get(g['draft']['id']).to_dict()==g['draft']
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0
    unchanged_calls(g)


FINISH_CHILD=r'''import json,os,socket,sys
from pathlib import Path
from tests.test_reviewed_batches import cold_service
from workbench.reviewed_batch_controls import ReviewedBatchControlService,control_paths
from workbench.reviewed_policy_publication import ReviewedPolicyPublicationService
from workbench.reviewed_policy_readback import ReviewedPolicyReadbackService
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
def deny(*args,**kwargs):raise AssertionError('actual network forbidden')
socket.socket.connect=deny
controls=ReviewedBatchControlService(cold_service(spec).publication)
_,machine,_=control_paths(controls._control_context(spec['project_id']),spec['batch_id'])
def crash(event):
    if event=='after_control_machine' and json.loads(machine.read_text(encoding='utf-8'))['commands'][-1]['action']=='finish_step':os._exit(90)
view,handle=controls.command(project_id=spec['project_id'],batch_id=spec['batch_id'],action='resume',
    idempotency_key='child-explicit-finish',expected_control_sha256=spec['expected'],author_confirmed=True)
controls.fault_injector=crash
if spec['stage']=='publication':ReviewedPolicyPublicationService(controls).publish(handle,expected_control_sha256=view['control']['record_sha256'])
else:ReviewedPolicyReadbackService(controls).readback(handle,expected_control_sha256=view['control']['record_sha256'])
raise AssertionError('original finish death point not reached')
'''


CHILD=r'''import json,os,socket,sys
from pathlib import Path
from tests.test_reviewed_batches import cold_service
from workbench.reviewed_batch_controls import ReviewedBatchControlService
from workbench.reviewed_policy_readback import ReviewedPolicyReadbackService
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
def deny(*args,**kwargs):raise AssertionError('actual network forbidden')
socket.socket.connect=deny
controls=ReviewedBatchControlService(cold_service(spec).publication)
view,handle=controls.command(project_id=spec['project_id'],batch_id=spec['batch_id'],action='resume',
    idempotency_key='child-explicit-readback',expected_control_sha256=spec['expected'],author_confirmed=True)
def crash(event):
    if event=='after_readback_before_finish':os._exit(89)
ReviewedPolicyReadbackService(controls,fault_injector=crash).readback(handle,expected_control_sha256=view['control']['record_sha256'])
raise AssertionError('readback process death point not reached')
'''
