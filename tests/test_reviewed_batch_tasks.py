"""Real background stages, cold reads, duplicate commands and OS lease lifetime."""
import json,os,subprocess,sys,threading
from pathlib import Path

import pytest

from core.engine.reviewed_batch_control import BatchControlError
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import digest
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_batches import batch_case,approved
from tests.test_reviewed_batch_driver import driver,calls
from workbench.instance import InstanceLease,InstanceAlreadyRunning
from workbench.maintenance import maintenance_for,MaintenanceBlocked
from workbench.reviewed_batch_controls import ReviewedBatchControlService
from workbench.reviewed_batch_tasks import ReviewedBatchTaskService


def initialized(c):
    result=approved(c);batch=result['grant']['manifest']['batch_id']
    controls=ReviewedBatchControlService(c['publication'])
    view=controls.initialize(project_id=c['project'].id,batch_id=batch,author_confirmed=True)
    return controls,batch,view


def joined(service):
    with service._mutex:active=service._active
    if active is not None:
        active['thread'].join(timeout=900)
        assert not active['thread'].is_alive(),'original background task did not finish'


def test_real_background_pause_cold_read_explicit_resume_and_duplicate_requests_do_not_repeat_generation(batch_case):
    c=batch_case;c['route']['end_chapter']=2
    controls,batch,initial=initialized(c);entered=threading.Event();release=threading.Event()
    lease=InstanceLease(c['root'].parent/'task-instance').acquire();gate=maintenance_for(lease)
    def stop(event,step):
        assert step['stage']=='generation';entered.set();assert release.wait(300)
    tasks=ReviewedBatchTaskService(controls,instance_lease=lease,maintenance=gate,
        driver_factory=lambda controls:driver(c,controls,fault=stop)[0])
    original=dict(project_id=c['project'].id,batch_id=batch,action='start',idempotency_key='background-start',
        expected_control_sha256=initial['control']['record_sha256'],author_confirmed=True)
    try:
        tasks.submit(**original);assert entered.wait(300)
        live=tasks.get(project_id=c['project'].id,batch_id=batch)
        assert live['execution_live'] and not live['explicit_resume_required'] and gate.status()['active_writes']==1
        assert tasks.submit(**original)['execution_live']
        before=controls.get(project_id=c['project'].id,batch_id=batch)
        with pytest.raises(BatchControlError):tasks.submit(**{**original,'expected_control_sha256':'f'*64})
        with pytest.raises(BatchControlError):tasks.submit(**{**original,'action':'resume','idempotency_key':'second-run',
            'expected_control_sha256':before['control']['record_sha256']})
        assert controls.get(project_id=c['project'].id,batch_id=batch)==before
        controls.command(project_id=c['project'].id,batch_id=batch,action='pause',idempotency_key='background-pause',
            expected_control_sha256=before['control']['record_sha256'],author_confirmed=True)
        release.set();joined(tasks)
        paused=tasks.get(project_id=c['project'].id,batch_id=batch)
        assert paused['task_state']=='finished' and paused['task']['result']['outcome']=='returned'
        assert paused['control']['progress']['stage']=='extraction' and paused['explicit_resume_required']
        assert not controls.handles and gate.status()['active_writes']==0
        folder=c['root']/'.novelagent/runtime/reviewed/batch_tasks'
        saved={p.name:p.read_bytes() for p in folder.iterdir()}
        cold=ReviewedBatchControlService(c['publication'])
        cold_tasks=ReviewedBatchTaskService(cold,instance_lease=lease,maintenance=gate,
            driver_factory=lambda controls:driver(c,controls)[0])
        assert cold_tasks.get(project_id=c['project'].id,batch_id=batch)['explicit_resume_required']
        assert cold_tasks.submit(**original)['explicit_resume_required'] and not cold.handles
        assert {p.name:p.read_bytes() for p in folder.iterdir()}==saved
        gc=c['root'].parent/'driver-generation.txt';ec=c['root'].parent/'driver-extraction.txt'
        assert gc.read_text()=='1' and not ec.exists() and calls(c,2)==1
        cold_tasks.submit(project_id=c['project'].id,batch_id=batch,action='resume',idempotency_key='background-explicit-resume',
            expected_control_sha256=paused['control']['control']['record_sha256'],author_confirmed=True)
        joined(cold_tasks)
        final=cold_tasks.get(project_id=c['project'].id,batch_id=batch)
        assert final['control']['progress']['status']=='completed' and final['task_state']=='finished'
        assert final['task']['result']['outcome']=='returned' and not final['explicit_resume_required']
        assert gc.read_text()==ec.read_text()=='1' and calls(c,2)==2
        assert load_verified_reviewed_identity(c['root'])['history']['value']['committed_through']==2
        assert cold_tasks.submit(**original)==final and gate.status()['active_writes']==0
        result=next(p for p in folder.glob('*.result.json') if json.loads(p.read_bytes())['task_sha256']==final['task']['ticket']['record_sha256'])
        raw=result.read_bytes();bad=json.loads(raw);bad['control_sequence']=True
        bad['record_sha256']=digest({k:v for k,v in bad.items() if k!='record_sha256'});result.write_text(json.dumps(bad),encoding='utf-8')
        try:
            with pytest.raises(BatchControlError):cold_tasks.get(project_id=c['project'].id,batch_id=batch)
        finally:result.write_bytes(raw)
        cold_tasks.close()
    finally:
        release.set();tasks.close();gate.require_no_active();lease.close()


def test_real_background_shutdown_preserves_instance_and_maintenance_until_blocked_stage_stops(batch_case):
    c=batch_case;c['route']['end_chapter']=2
    controls,batch,initial=initialized(c);entered=threading.Event();release=threading.Event();closed=threading.Event()
    lease=InstanceLease(c['root'].parent/'task-instance').acquire();gate=maintenance_for(lease)
    def create(controls):
        service,_,_=driver(c,controls);original=service.generation.executor_factory
        def blocked(root):
            entered.set();assert release.wait(300)
            return original(root)
        service.generation.executor_factory=blocked
        return service
    tasks=ReviewedBatchTaskService(controls,instance_lease=lease,maintenance=gate,driver_factory=create)
    try:
        tasks.submit(project_id=c['project'].id,batch_id=batch,action='start',idempotency_key='shutdown-start',
            expected_control_sha256=initial['control']['record_sha256'],author_confirmed=True)
        assert entered.wait(300)
        assert tasks.get(project_id=c['project'].id,batch_id=batch)['execution_live']
        operation=gate.begin();assert gate.status()['state']=='draining'
        with pytest.raises(MaintenanceBlocked):gate.require_quiescent(operation['operation_id'])
        thread=threading.Thread(target=lambda:(tasks.close(),closed.set()));thread.start()
        assert not closed.wait(.1) and lease.held and gate.status()['active_writes']==1
        with pytest.raises(InstanceAlreadyRunning):InstanceLease(lease.directory).acquire()
        release.set();thread.join(timeout=300);assert closed.is_set()
        gate.require_quiescent(operation['operation_id'])
        result=tasks.get(project_id=c['project'].id,batch_id=batch)
        assert result['explicit_resume_required'] and result['control']['progress']['status']=='paused'
        assert not controls.handles and calls(c,2)==0
        assert not (c['root'].parent/'driver-generation.txt').exists()
        with pytest.raises(BatchControlError):tasks.submit(project_id=c['project'].id,batch_id=batch,action='resume',
            idempotency_key='closed-resume',expected_control_sha256=result['control']['control']['record_sha256'],author_confirmed=True)
    finally:
        release.set();tasks.close();gate.require_no_active();lease.close()


def test_real_process_exit_after_task_ticket_never_launches_on_restart_or_same_request(batch_case):
    c=batch_case;c['route']['end_chapter']=2
    controls,batch,view=initialized(c)
    spec={'root':str(c['root']),'database':str(c['db'].path),'machine':str(c['machine']),
        'book_id':c['project'].book_id,'project_id':c['project'].id,'batch_id':batch,'expected':view['control']['record_sha256'],
        'config':{k:v for k,v in controls.batches.get(project_id=c['project'].id,batch_id=batch)['grant']['manifest']['model_selection'].items()
            if k.startswith(('openai_','provider_'))}}
    spec['config']['openai_base_url']=c['config'].openai_base_url
    path=c['root'].parent/'task-exit.fixture.json';path.write_text(json.dumps(spec),encoding='utf-8')
    child=subprocess.run([sys.executable,'-B','-c',CHILD,str(path)],capture_output=True,text=True,timeout=300)
    assert child.returncode==98,child.stderr
    cold=ReviewedBatchControlService(c['publication'])
    lease=InstanceLease(c['root'].parent/'task-instance').acquire();gate=maintenance_for(lease)
    tasks=ReviewedBatchTaskService(cold,instance_lease=lease,maintenance=gate,
        driver_factory=lambda controls:pytest.fail('cold request dispatched driver'))
    try:
        current=tasks.get(project_id=c['project'].id,batch_id=batch)
        assert current['task_state']=='interrupted' and current['explicit_resume_required'] and not cold.handles
        assert current['task']['result'] is None and current['control']['progress']['status']=='running'
        assert tasks.submit(project_id=c['project'].id,batch_id=batch,action='start',idempotency_key='crash-start',
            expected_control_sha256=view['control']['record_sha256'],author_confirmed=True)==current
        assert calls(c,2)==0 and gate.status()['active_writes']==0 and not cold.handles
        folder=c['root']/'.novelagent/runtime/reviewed/batch_tasks';ticket=next(folder.glob('*.json'))
        raw=ticket.read_bytes();bad=json.loads(raw);bad['run_command']['sequence']=True
        ticket.write_text(json.dumps(bad),encoding='utf-8')
        try:
            with pytest.raises(BatchControlError):tasks.get(project_id=c['project'].id,batch_id=batch)
        finally:ticket.write_bytes(raw)
    finally:tasks.close();gate.require_no_active();lease.close()


CHILD=r'''import json,os,sys
from pathlib import Path
from tests.test_reviewed_batches import cold_service
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for
from workbench.reviewed_batch_controls import ReviewedBatchControlService
from workbench.reviewed_batch_tasks import ReviewedBatchTaskService
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
controls=ReviewedBatchControlService(cold_service(spec).publication)
lease=InstanceLease(Path(spec['root']).parent/'task-instance').acquire()
def stop(event):
    if event=='after_task_saved':os._exit(98)
tasks=ReviewedBatchTaskService(controls,instance_lease=lease,maintenance=maintenance_for(lease),fault_injector=stop)
tasks.submit(project_id=spec['project_id'],batch_id=spec['batch_id'],action='start',idempotency_key='crash-start',
    expected_control_sha256=spec['expected'],author_confirmed=True)
raise AssertionError('task exit point not reached')
'''
