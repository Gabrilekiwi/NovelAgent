"""Real process exits at consumed claim and paired begin-command publication."""
import json,subprocess,sys

import pytest

from core.chapter_execution_budget import ChapterExecutionBudget
from core.chapter_execution_time import ChapterTimeUnknown
from core.engine.reviewed_batch_control import BatchControlError
from core.story_project.reviewed_state_document import RELATIVE_PATH
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_batch_controls import started
from tests.test_reviewed_policy_repair_flow import build
from workbench.reviewed_batch_controls import ReviewedBatchControlService,control_paths
from workbench.reviewed_policy_repairs import ReviewedPolicyRepairService


@pytest.mark.parametrize('phase',['after_claim_before_repair_control','after_control_machine','after_control_book'])
def test_consumed_claim_and_begin_process_death_never_clear_time_or_issue_new_cycle(batch_case,phase):
    c=batch_case;controls,batch,view,handle=started(c)
    grant=controls.batches.get(project_id=c['project'].id,batch_id=batch)['grant'];selection=grant['manifest']['model_selection']
    spec={'root':str(c['root']),'database':str(c['db'].path),'machine':str(c['machine']),
        'book_id':c['project'].book_id,'project_id':c['project'].id,'batch_id':batch,'expected':view['control']['record_sha256'],
        'phase':phase,'config':{k:v for k,v in selection.items() if k.startswith(('openai_','provider_'))}}
    spec['config']['openai_base_url']=c['config'].openai_base_url
    fixture=c['root'].parent/'begin.fixture.json';fixture.write_text(json.dumps(spec),encoding='utf-8')
    child=subprocess.run([sys.executable,'-B','-c',CHILD,str(fixture)],capture_output=True,text=True,timeout=300)
    assert child.returncode==94,child.stderr
    cold=ReviewedBatchControlService(c['publication']);service=ReviewedPolicyRepairService(cold)
    context=cold._control_context(c['project'].id);_,machine,book=control_paths(context,batch)
    machine_original=machine.read_bytes();book_original=book.read_bytes();formal=(c['root']/RELATIVE_PATH).read_bytes()
    budget=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2)
    claim=budget.store.repair_cycle_claim();time_bytes=budget.time_clock.path.read_bytes()
    assert claim['repair_cycles_consumed']==1 and budget.time_clock.report()['active_status']=='unknown' and not cold.handles
    if phase=='after_claim_before_repair_control':
        assert machine_original==book_original
        with pytest.raises(BatchControlError):service.recover_begin(project_id=c['project'].id,batch_id=batch,chapter=2)
        assert machine.read_bytes()==machine_original and book.read_bytes()==book_original
    else:
        assert (machine_original!=book_original)==(phase=='after_control_machine')
        fixed=service.recover_begin(project_id=c['project'].id,batch_id=batch,chapter=2)
        assert fixed['control']['progress']['stage']=='repair_generation' and fixed['control']['explicit_resume_required']
        assert fixed['execution_permit'] is fixed['repair_execution_authorized'] is False and not cold.handles
        assert machine.read_bytes()==book.read_bytes()==machine_original
        assert service.recover_begin(project_id=c['project'].id,batch_id=batch,chapter=2)==fixed
    assert budget.time_clock.path.read_bytes()==time_bytes and budget.store.repair_cycle_claim()==claim
    assert (c['root']/RELATIVE_PATH).read_bytes()==formal
    observed=cold.get(project_id=c['project'].id,batch_id=batch)
    resumed,new_handle=cold.command(project_id=c['project'].id,batch_id=batch,action='resume',idempotency_key='explicit-unknown-time-repair',
        expected_control_sha256=observed['control']['record_sha256'],author_confirmed=True)
    driver,gc,ec=build(c,cold)
    if phase=='after_claim_before_repair_control':
        result=driver.run(new_handle,expected_control_sha256=resumed['control']['record_sha256'])
        assert result['control']['progress']['status']=='paused' and result['control']['progress']['stage']=='quality'
    else:
        with pytest.raises(ChapterTimeUnknown):driver.run(new_handle,expected_control_sha256=resumed['control']['record_sha256'])
    assert gc.read_text()==ec.read_text()=='1' and not cold.handles
    assert budget.time_clock.path.read_bytes()==time_bytes and budget.store.repair_cycle_claim()==claim
    assert budget.time_clock.report()['active_status']=='unknown'
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0


CHILD=r'''import json,os,sys
from pathlib import Path
from types import SimpleNamespace
from tests.test_reviewed_batches import cold_service
from tests.test_reviewed_policy_repair_flow import build
from workbench.reviewed_batch_controls import ReviewedBatchControlService
import workbench.reviewed_policy_repairs as repairs
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
controls=ReviewedBatchControlService(cold_service(spec).publication)
view,handle=controls.command(project_id=spec['project_id'],batch_id=spec['batch_id'],action='resume',idempotency_key='explicit-child-begin',
    expected_control_sha256=spec['expected'],author_confirmed=True)
original=repairs.ReviewedPolicyRepairService
def crash(event):
    if event==spec['phase']:os._exit(94)
repairs.ReviewedPolicyRepairService=lambda controls:original(controls,fault_injector=crash)
case={'root':Path(spec['root']),'project':SimpleNamespace(id=spec['project_id'],book_id=spec['book_id'])}
driver,_,_=build(case,controls)
driver.run(handle,expected_control_sha256=view['control']['record_sha256'])
raise AssertionError('begin repair exit point not reached')
'''
