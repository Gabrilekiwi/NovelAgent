"""Real process exits in original author handoff and completion commands."""
import json,subprocess,sys

import pytest

from core.chapter_execution_budget import ChapterExecutionBudget
from core.chapter_execution_time import ChapterTimeUnknown
from core.engine.reviewed_batch_control import BatchControlError
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_batch_takeovers import paused,author_commit
from workbench.reviewed_batch_controls import ReviewedBatchControlService
from workbench.reviewed_batch_takeovers import ReviewedBatchTakeoverService


@pytest.mark.parametrize('operation,phase',[
    ('takeover','after_takeover_snapshot'),('takeover','after_control_machine'),('takeover','after_control_book'),
    ('complete','after_author_readback'),('complete','after_control_machine'),('complete','after_control_book')])
def test_original_author_process_exit_preserves_snapshot_receipt_time_and_never_grants_paid_resume(batch_case,operation,phase):
    c=batch_case;g=paused(c);controls=g['controls'];grant=controls.batches.get(project_id=c['project'].id,batch_id=g['batch_id'])['grant']
    selection=grant['manifest']['model_selection'];route=None;expected=g['view']['control']['record_sha256']
    if operation=='complete':
        take=ReviewedBatchTakeoverService(controls).takeover(project_id=c['project'].id,batch_id=g['batch_id'],draft_id=g['draft']['id'],
            expected_control_sha256=expected,author_confirmed=True)
        expected=take['control']['control']['record_sha256']
        route,_=author_commit(c,c['publication'].preparations.drafts.get(g['draft']['id']))
    spec={'root':str(c['root']),'database':str(c['db'].path),'machine':str(c['machine']),
        'project_id':c['project'].id,'batch_id':g['batch_id'],'draft_id':g['draft']['id'],'expected':expected,
        'operation':operation,'phase':phase,'route':route,
        'config':{k:v for k,v in selection.items() if k.startswith(('openai_','provider_'))}}
    spec['config']['openai_base_url']=c['config'].openai_base_url
    fixture=c['root'].parent/'takeover.fixture.json';fixture.write_text(json.dumps(spec),encoding='utf-8')
    child=subprocess.run([sys.executable,'-B','-c',CHILD,str(fixture)],capture_output=True,text=True,timeout=240)
    assert child.returncode==95,child.stderr
    cold=ReviewedBatchControlService(c['publication']);service=ReviewedBatchTakeoverService(cold)
    budget=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2)
    time_bytes=budget.time_clock.path.read_bytes();assert budget.time_clock.report()['active_status']=='unknown'
    observed=service.get(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)
    assert not cold.handles and observed['execution_permit'] is False
    kwargs=dict(project_id=c['project'].id,batch_id=g['batch_id'],expected_control_sha256=expected,author_confirmed=True)
    if operation=='takeover':kwargs['draft_id']=g['draft']['id']
    else:kwargs['route']=route
    call=service.takeover if operation=='takeover' else service.complete
    if phase in {'after_takeover_snapshot','after_author_readback'}:
        with pytest.raises(ChapterTimeUnknown):call(**kwargs)
    else:
        fixed=call(**kwargs)
        assert call(**kwargs)==fixed and not cold.handles
        assert not service.get(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)['control_reconciliation_required']
    assert budget.time_clock.path.read_bytes()==time_bytes and budget.time_clock.report()['active_status']=='unknown'
    current=cold.get(project_id=c['project'].id,batch_id=g['batch_id'])
    if phase=='after_takeover_snapshot':
        from tests.test_reviewed_batch_driver import driver
        resumed,handle=cold.command(project_id=c['project'].id,batch_id=g['batch_id'],action='resume',
            idempotency_key='must-not-pay-after-author-crash',expected_control_sha256=current['control']['record_sha256'],author_confirmed=True)
        continuation,gc,ec=driver(c,cold)
        with pytest.raises(ChapterTimeUnknown):continuation.run(handle,expected_control_sha256=resumed['control']['record_sha256'])
        assert not gc.exists() and not ec.exists()
    else:
        with pytest.raises((BatchControlError,ChapterTimeUnknown)):cold.command(project_id=c['project'].id,batch_id=g['batch_id'],action='resume',
            idempotency_key='must-not-pay-after-author-crash',expected_control_sha256=current['control']['record_sha256'],author_confirmed=True)
    assert not cold.handles and g['gc'].read_text()=='1' and not g['ec'].exists()
    assert c['publication'].preparations.drafts.get(g['draft']['id']).to_dict()==g['draft']
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0


CHILD=r'''import json,os,sys
from pathlib import Path
from tests.test_reviewed_batches import cold_service
from workbench.reviewed_batch_controls import ReviewedBatchControlService
from workbench.reviewed_batch_takeovers import ReviewedBatchTakeoverService
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
controls=ReviewedBatchControlService(cold_service(spec).publication)
def crash(event):
    if event==spec['phase']:os._exit(95)
service=ReviewedBatchTakeoverService(controls,fault_injector=crash)
kwargs=dict(project_id=spec['project_id'],batch_id=spec['batch_id'],expected_control_sha256=spec['expected'],author_confirmed=True)
if spec['operation']=='takeover':service.takeover(**kwargs,draft_id=spec['draft_id'])
else:service.complete(**kwargs,route=spec['route'])
raise AssertionError('author process exit point not reached')
'''
