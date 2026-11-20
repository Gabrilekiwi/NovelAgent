"""Actual command logs, revocable model scope and owned original batch grants."""
import copy,json,subprocess,sys,uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
from pathlib import Path

import pytest

from core.engine.reviewed_batch_control import (BatchControlStore,BatchControlError,BatchControlPaused,
    initial_control,validate_control,current_policy_step)
from core.story_project.reviewed_state_document import digest
from tests.test_reviewed_batches import batch_case,approved
from tests.test_reviewed_upgrade import case
from tests.test_chapter_budget_runtime import args,response
from core.chapter_budget_store import ChapterBudgetStore
from core.chapter_model_budget import ChapterModelCallBudget
from core.model_calls import ModelCallStore
from core.model_call_runtime import ModelCallRuntimeContext
from workbench.reviewed_batch_controls import ReviewedBatchControlService,control_paths


def fixture_store(root):
    grant={'grant_sha256':'a'*64,'manifest':{'batch_id':'batch_'+'1'*32,'book_id':'fixture-book',
        'project_id':'fixture-project','ownership_sha256':'b'*64,'start_chapter':2,'end_chapter':3,
        'budget':{'batch_budget_sha256':'c'*64}}}
    store=BatchControlStore(root/'machine/controls.json',root/'book/controls.json',grant)
    store.initialize(before_write=lambda:None)
    return store


def apply(store,action,payload=None,*,key=None):
    record,_=store.load()
    return store.apply(action=action,payload=payload or {},key=key or uuid.uuid4().hex,
        expected_sha256=record['record_sha256'],before_write=lambda:None)


def test_core_control_chain_preserves_pending_step_after_resume_and_stop_is_terminal(tmp_path):
    store=fixture_store(tmp_path)
    record,state=apply(store,'start',{'run_epoch':'1'*32})
    step='step_'+digest({'batch_id':state.get('batch_id',store.grant['manifest']['batch_id']),'chapter':2,'stage':'generation'})[:32]
    record,state=apply(store,'claim_step',{'run_epoch':'1'*32,'step_id':step,'execution_id':'execution_original','input_sha256':'d'*64})
    pending=copy.deepcopy(state['pending_step'])
    apply(store,'pause');_,state=apply(store,'resume',{'run_epoch':'2'*32})
    assert state['pending_step']==pending
    _,state=apply(store,'finish_step',{'run_epoch':'2'*32,'step_id':step,'result_sha256':'e'*64})
    assert state['stage']=='extraction' and state['chapter']==2
    apply(store,'stop')
    before=store.machine_path.read_bytes()
    with pytest.raises(BatchControlError):apply(store,'resume',{'run_epoch':'3'*32})
    assert store.machine_path.read_bytes()==before


def test_partial_command_publication_requires_exact_original_key_and_cas(tmp_path):
    store=fixture_store(tmp_path);original,_=store.load()
    class Stop(BaseException):pass
    def crash(event):
        if event=='after_control_machine':raise Stop()
    with pytest.raises(Stop):store.apply(action='start',payload={'run_epoch':'1'*32},key='original',
        expected_sha256=original['record_sha256'],before_write=lambda:None,fault_injector=crash)
    with pytest.raises(BatchControlError):store.load()
    with pytest.raises(BatchControlError):store.apply(action='start',payload={'run_epoch':'2'*32},key='new',
        expected_sha256=original['record_sha256'],before_write=lambda:None)
    machine=store.machine_path.read_bytes()
    result,state=store.apply(action='start',payload={'run_epoch':'1'*32},key='original',
        expected_sha256=original['record_sha256'],before_write=lambda:None)
    assert state['status']=='running' and store.machine_path.read_bytes()==machine
    assert store.book_path.read_bytes()==machine
    assert store.apply(action='start',payload={'run_epoch':'1'*32},key='original',expected_sha256=original['record_sha256'],
        before_write=lambda:pytest.fail('completed duplicate wrote again'))[0]==result


@pytest.mark.parametrize('kind',['boolean_sequence','invalid_epoch','unknown_action','missing_copy','null_commands'])
def test_damaged_control_never_becomes_empty_or_a_new_allowance(tmp_path,kind):
    store=fixture_store(tmp_path);apply(store,'start',{'run_epoch':'1'*32})
    if kind=='missing_copy':store.book_path.unlink()
    else:
        value=json.loads(store.machine_path.read_text(encoding='utf-8'))
        if kind=='null_commands':value['commands']=None
        else:
            command=value['commands'][0]
            if kind=='boolean_sequence':command['sequence']=True
            elif kind=='invalid_epoch':command['payload']['run_epoch']=False
            else:command['action']='silently_skip_chapter'
            command['command_sha256']=digest({k:v for k,v in command.items() if k!='command_sha256'})
        value['record_sha256']=digest({k:v for k,v in value.items() if k!='record_sha256'})
        store.machine_path.write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(BatchControlError):store.load()
    with pytest.raises(BatchControlError):store.initialize(before_write=lambda:pytest.fail('must not reset'))


CHILD=r'''
import json,os,sys
from pathlib import Path
from core.engine.reviewed_batch_control import BatchControlStore
root=Path(sys.argv[1]);grant=json.loads((root/'grant.fixture.json').read_text(encoding='utf-8'))
store=BatchControlStore(root/'machine/controls.json',root/'book/controls.json',grant)
record,_=store.load()
def crash(event):
    if event==sys.argv[2]:os._exit(79)
store.apply(action='start',payload={'run_epoch':'1'*32},key='original',expected_sha256=record['record_sha256'],
    before_write=lambda:None,fault_injector=crash)
'''


@pytest.mark.parametrize('phase',['after_control_machine','after_control_book'])
def test_real_process_death_replays_only_original_control_command(tmp_path,phase):
    store=fixture_store(tmp_path);before,_=store.load()
    (tmp_path/'grant.fixture.json').write_text(json.dumps(store.grant),encoding='utf-8')
    child=subprocess.run([sys.executable,'-B','-c',CHILD,str(tmp_path),phase],capture_output=True,text=True,timeout=25)
    assert child.returncode==79,child.stderr
    record,state=store.apply(action='start',payload={'run_epoch':'1'*32},key='original',
        expected_sha256=before['record_sha256'],before_write=lambda:None)
    assert state['status']=='running' and len(record['commands'])==1


def started(c):
    grant=approved(c);batch_id=grant['grant']['manifest']['batch_id']
    controls=ReviewedBatchControlService(c['publication'])
    initial=controls.initialize(project_id=c['project'].id,batch_id=batch_id,author_confirmed=True)
    view,handle=controls.command(project_id=c['project'].id,batch_id=batch_id,action='start',idempotency_key='start',
        expected_control_sha256=initial['control']['record_sha256'],author_confirmed=True)
    return controls,batch_id,view,handle


def runtime(c,execution_id,permit):
    root=c['root'];chapter=permit.binding['chapter'];book=c['project'].book_id
    store=ChapterBudgetStore(root/f'.novelagent/runtime/budgets/c{chapter}',book_id=book,chapter=chapter)
    calls=ModelCallStore(root/'.novelagent/runtime/runs/executions'/execution_id/'model_calls')
    return ModelCallRuntimeContext(calls,chapter_budget=ChapterModelCallBudget(store,execution_id=execution_id),active_interval=permit.active_interval)


def test_owned_step_executes_only_its_original_call_then_cold_service_requires_explicit_resume(batch_case):
    c=batch_case;controls,batch_id,view,handle=started(c);calls=[]
    with controls.step(handle,stage='generation',execution_id='execution_control_gen',input_sha256='d'*64,
            idempotency_key='generation',expected_control_sha256=view['control']['record_sha256'],verify_inputs=lambda:'d'*64) as permit:
        execute=runtime(c,'execution_control_gen',permit)
        request=args(endpoint_fingerprint=permit.model['endpoint_fingerprint'],operation=lambda:calls.append(1) or response())
        assert execute.execute_attempt(**request)=='synthetic result'
        assert execute.execute_attempt(**{**request,'operation':lambda:pytest.fail('replay called Provider')})=='synthetic result'
    assert permit.closed and current_policy_step() is None and calls==[1]
    with pytest.raises(BatchControlPaused):permit.require(execute.chapter_budget,provider='openai',model='fixture',
        endpoint_fingerprint=permit.model['endpoint_fingerprint'],stage='generation')
    cold=ReviewedBatchControlService(c['publication'])
    observed=cold.get(project_id=c['project'].id,batch_id=batch_id)
    assert observed['explicit_resume_required'] and observed['progress']['pending_step']['execution_id']=='execution_control_gen'
    with pytest.raises(BatchControlPaused):
        with cold.step(handle,stage='generation',execution_id='execution_control_gen',input_sha256='d'*64,
            idempotency_key='new',expected_control_sha256=observed['control']['record_sha256'],verify_inputs=lambda:'d'*64):pass
    resumed,new_handle=cold.command(project_id=c['project'].id,batch_id=batch_id,action='resume',idempotency_key='explicit-resume',
        expected_control_sha256=observed['control']['record_sha256'],author_confirmed=True)
    assert resumed['progress']['pending_step']==observed['progress']['pending_step']
    result=cold.finish_step(new_handle,expected_control_sha256=resumed['control']['record_sha256'],idempotency_key='finish-generation',
        result_sha256='e'*64,verify_result=lambda:'e'*64)
    assert result['progress']['stage']=='extraction' and calls==[1]


def test_stop_inside_physical_boundary_blocks_dispatch_and_original_reservation_is_released(batch_case):
    c=batch_case;controls,batch_id,view,handle=started(c)
    with controls.step(handle,stage='generation',execution_id='execution_control_stop',input_sha256='d'*64,
        idempotency_key='generation',expected_control_sha256=view['control']['record_sha256'],verify_inputs=lambda:'d'*64) as permit:
        execute=runtime(c,'execution_control_stop',permit)
        def stop():
            live=controls.get(project_id=c['project'].id,batch_id=batch_id)
            controls.command(project_id=c['project'].id,batch_id=batch_id,action='stop',idempotency_key='stop',
                expected_control_sha256=live['control']['record_sha256'],author_confirmed=True)
        # At this point Intent/reservation exist but physical dispatch has not
        # been claimed. Native runtime must recheck the just-revoked permit.
        with pytest.raises(BatchControlPaused):execute.execute_attempt(**args(endpoint_fingerprint=permit.model['endpoint_fingerprint'],
            before_dispatch=stop,operation=lambda:pytest.fail('stopped batch called Provider')))
        row=execute.chapter_budget.attempts()[0]
        assert row['receipt']['status']=='budget_rejected' and row['dispatch_claimed'] is False
        assert execute.chapter_budget.store.report()['charged']['calls']==0
    assert controls.get(project_id=c['project'].id,batch_id=batch_id)['progress']['status']=='stopped'


def test_scope_rejects_other_model_execution_stage_thread_and_changed_input_before_new_attempt(batch_case,monkeypatch):
    c=batch_case;controls,batch_id,view,handle=started(c);actual=['d'*64]
    with controls.step(handle,stage='generation',execution_id='execution_control_scope',input_sha256='d'*64,
        idempotency_key='generation',expected_control_sha256=view['control']['record_sha256'],verify_inputs=lambda:actual[0]) as permit:
        execute=runtime(c,'execution_control_scope',permit)
        options=args(endpoint_fingerprint=permit.model['endpoint_fingerprint'],operation=lambda:pytest.fail('wrong scope dispatched'))
        for changes in ({'model':'other'},{'provider':'anthropic'},{'stage':'fact_extraction'},{'endpoint_fingerprint':'f'*64}):
            with pytest.raises(BatchControlError):execute.execute_attempt(**{**options,**changes})
        other=runtime(c,'execution_other_scope',permit)
        with pytest.raises(BatchControlError):other.execute_attempt(**options)
        copied=copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            task=pool.submit(copied.run,lambda:permit.require(execute.chapter_budget,provider='openai',model='fixture',
                endpoint_fingerprint=permit.model['endpoint_fingerprint'],stage='generation'))
            with pytest.raises(BatchControlPaused):task.result(timeout=10)
        actual[0]='e'*64
        with pytest.raises(BatchControlPaused):execute.execute_attempt(**options)
        actual[0]='d'*64
        monkeypatch.setattr('workbench.reviewed_batches.get_config',lambda:replace(c['config'],openai_model='drifted'))
        with pytest.raises(BatchControlPaused):execute.execute_attempt(**options)
        assert execute.chapter_budget.attempts()==[] and other.chapter_budget.attempts()==[]


def test_pause_from_another_thread_during_call_keeps_actual_charge_and_prevents_next_call(batch_case):
    c=batch_case;controls,batch_id,view,handle=started(c);signals=ReviewedBatchControlService(c['publication'])
    with controls.step(handle,stage='generation',execution_id='execution_live_scope',input_sha256='d'*64,
        idempotency_key='generation',expected_control_sha256=view['control']['record_sha256'],verify_inputs=lambda:'d'*64) as permit:
        execute=runtime(c,'execution_live_scope',permit);effects=[]
        def operation():
            effects.append(1)
            def pause():
                observed=signals.get(project_id=c['project'].id,batch_id=batch_id)
                return signals.command(project_id=c['project'].id,batch_id=batch_id,action='pause',idempotency_key='pause-live',
                    expected_control_sha256=observed['control']['record_sha256'],author_confirmed=True)
            with ThreadPoolExecutor(max_workers=1) as pool:
                paused=pool.submit(pause).result(timeout=15)
                assert paused['progress']['status']=='paused'
            return response()
        assert execute.execute_attempt(**args(endpoint_fingerprint=permit.model['endpoint_fingerprint'],operation=operation))=='synthetic result'
        assert execute.chapter_budget.store.report()['charged']['calls']==1 and effects==[1]
        with pytest.raises(BatchControlPaused):execute.execute_attempt(**args(call_id='must-not-start-next',
            endpoint_fingerprint=permit.model['endpoint_fingerprint'],operation=lambda:pytest.fail('paused retry dispatched')))
        assert len(execute.chapter_budget.attempts())==1
    assert signals.get(project_id=c['project'].id,batch_id=batch_id)['explicit_resume_required']


def test_actual_cold_control_process_reads_running_progress_without_creating_live_permission(batch_case):
    c=batch_case;controls,batch_id,view,handle=started(c)
    spec={'root':str(c['root']),'database':str(c['db'].path),'machine':str(c['machine']),'config':{},
        'project_id':c['project'].id,'batch_id':batch_id}
    path=c['root'].parent/'cold-controls.fixture.json';path.write_text(json.dumps(spec),encoding='utf-8')
    script=r'''import json,sys
from pathlib import Path
from tests.test_reviewed_batches import cold_service
from workbench.reviewed_batch_controls import ReviewedBatchControlService
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
service=ReviewedBatchControlService(cold_service(spec).publication)
state=service.get(project_id=spec['project_id'],batch_id=spec['batch_id'])
assert state['progress']['status']=='running' and state['explicit_resume_required']
assert not service.handles and state['execution_permit'] is False
print(state['control']['record_sha256'])
'''
    child=subprocess.run([sys.executable,'-B','-c',script,str(path)],capture_output=True,text=True,timeout=35)
    assert child.returncode==0,child.stderr
    assert child.stdout.strip()==view['control']['record_sha256']
    assert not (c['root']/'.novelagent/runtime/runs/executions').exists()


@pytest.mark.parametrize('which',['anchor','machine','book'])
def test_ready_control_cannot_be_reinitialized_after_loss(batch_case,which):
    c=batch_case;controls,batch_id,view,handle=started(c)
    owner=c['publication']._owner(c['project'].book_id)
    paths={'anchor':owner.directory/('bc0-'+batch_id+'.json'),'machine':owner.directory/('bc-'+batch_id+'.json'),
        'book':c['root']/'.novelagent/runtime/reviewed/batch_controls'/(batch_id+'.json')}
    paths[which].unlink()
    with pytest.raises(BatchControlError):controls.get(project_id=c['project'].id,batch_id=batch_id)
    with pytest.raises(BatchControlError):controls.initialize(project_id=c['project'].id,batch_id=batch_id,author_confirmed=True)
    assert not paths[which].exists()


@pytest.mark.parametrize('phase',['after_control_machine','after_control_book'])
def test_actual_cold_service_finishes_original_start_without_issuing_new_live_permission(batch_case,phase):
    c=batch_case;grant=approved(c)['grant'];batch_id=grant['manifest']['batch_id']
    controls=ReviewedBatchControlService(c['publication'])
    initial=controls.initialize(project_id=c['project'].id,batch_id=batch_id,author_confirmed=True)
    selection=grant['manifest']['model_selection']
    spec={'root':str(c['root']),'database':str(c['db'].path),'machine':str(c['machine']),
        'project_id':c['project'].id,'batch_id':batch_id,'expected':initial['control']['record_sha256'],'phase':phase,
        'config':{k:v for k,v in selection.items() if k.startswith(('openai_','provider_'))}}
    spec['config']['openai_base_url']=c['config'].openai_base_url
    path=c['root'].parent/'crash-start.fixture.json';path.write_text(json.dumps(spec),encoding='utf-8')
    script=r'''import json,os,sys
from pathlib import Path
from tests.test_reviewed_batches import cold_service
from workbench.reviewed_batch_controls import ReviewedBatchControlService
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
def crash(event):
    if event==spec['phase']:os._exit(80)
service=ReviewedBatchControlService(cold_service(spec).publication,fault_injector=crash)
service.command(project_id=spec['project_id'],batch_id=spec['batch_id'],action='start',idempotency_key='original-start',
    expected_control_sha256=spec['expected'],author_confirmed=True)
'''
    child=subprocess.run([sys.executable,'-B','-c',script,str(path)],capture_output=True,text=True,timeout=70)
    assert child.returncode==80,child.stderr
    owner=c['publication']._owner(c['project'].book_id)
    machine=owner.directory/('bc-'+batch_id+'.json');original=machine.read_bytes()
    cold=ReviewedBatchControlService(c['publication'])
    observed,handle=cold.command(project_id=c['project'].id,batch_id=batch_id,action='start',idempotency_key='original-start',
        expected_control_sha256=initial['control']['record_sha256'],author_confirmed=True)
    assert handle is None and observed['explicit_resume_required'] and not cold.handles
    assert machine.read_bytes()==original and len(observed['control']['commands'])==1
    resumed,handle=cold.command(project_id=c['project'].id,batch_id=batch_id,action='resume',idempotency_key='fresh-explicit-resume',
        expected_control_sha256=observed['control']['record_sha256'],author_confirmed=True)
    assert handle is not None and not resumed['explicit_resume_required'] and len(resumed['control']['commands'])==2
    assert resumed['control']['commands'][0]==observed['control']['commands'][0]
    assert not (c['root']/'.novelagent/runtime/runs/executions').exists()
