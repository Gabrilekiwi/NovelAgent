"""Real stage-by-stage process death; typed inputs and model artifacts stay authoritative."""
import json,os,subprocess,sys

import pytest

from core.chapter_execution_budget import ChapterExecutionBudget
from core.engine.reviewed_batch_control import BatchControlError
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import digest
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_batch_controls import started
from tests.test_reviewed_policy_repair_flow import build
from workbench.reviewed_batch_controls import ReviewedBatchControlService,control_paths
from workbench.reviewed_stage_recovery import ReviewedBatchStageRecoveryService


ORDER=('generation','extraction','quality','repair_generation','repair_extraction','repair_quality','publication','readback')


def counters(c):
    return tuple((c['root'].parent/name).read_text() if (c['root'].parent/name).exists() else None
        for name in ('repair-generation.txt','repair-extraction.txt'))


def run_stage(c,controls,handle,view,stage):
    driver,_,_=build(c,controls)
    service=getattr(driver,stage)
    operation=service.generate if stage.endswith('generation') else service.extract if stage.endswith('extraction') else service.check if stage.endswith('quality') else service.publish if stage=='publication' else service.readback
    return operation(handle,expected_control_sha256=view['control']['record_sha256'])['control']


def crash(c,batch,view,stage,kind):
    controls=ReviewedBatchControlService(c['publication'])
    grant=controls.batches.get(project_id=c['project'].id,batch_id=batch)['grant'];selection=grant['manifest']['model_selection']
    spec={'root':str(c['root']),'database':str(c['db'].path),'machine':str(c['machine']),
        'book_id':c['project'].book_id,'project_id':c['project'].id,'batch_id':batch,'expected':view['control']['record_sha256'],
        'stage':stage,'kind':kind,'config':{k:v for k,v in selection.items() if k.startswith(('openai_','provider_'))}}
    spec['config']['openai_base_url']=c['config'].openai_base_url
    path=c['root'].parent/(kind+'-'+stage+'.fixture.json');path.write_text(json.dumps(spec),encoding='utf-8')
    child=subprocess.run([sys.executable,'-B','-c',CHILD,str(path)],capture_output=True,text=True,timeout=300)
    assert child.returncode==(96 if kind=='claim_step' else 97),child.stderr
    return ReviewedBatchControlService(c['publication'])


def record_observation(c,stage,kind):
    with (c['root'].parent/'stage-recovery-observations.jsonl').open('a',encoding='utf-8') as stream:
        stream.write(json.dumps({'stage':stage,'kind':kind,'counters':counters(c)},ensure_ascii=False)+'\n')
        stream.flush();os.fsync(stream.fileno())
    print('original stage recovered',kind,stage,flush=True)


def test_all_eight_actual_stage_claim_process_exits_require_typed_original_reconciliation_and_explicit_resume(batch_case):
    c=batch_case;c['route']['end_chapter']=2
    controls,batch,view,handle=started(c)
    recovery=ReviewedBatchStageRecoveryService(controls)
    before=view['control']
    with pytest.raises(BatchControlError):recovery.recover_claim(project_id=c['project'].id,batch_id=batch,chapter=2,stage='generation')
    assert controls.get(project_id=c['project'].id,batch_id=batch)['control']==before and counters(c)==(None,None)
    for stage in ORDER:
        assert view['progress']['stage']==stage
        previous=counters(c);controls=crash(c,batch,view,stage,'claim_step');recovery=ReviewedBatchStageRecoveryService(controls)
        observed=recovery.get(project_id=c['project'].id,batch_id=batch)
        assert observed['control_reconciliation_required'] and observed['original_recovery']['stage']==stage
        assert observed['original_recovery']['action']=='claim_step' and not controls.handles and counters(c)==previous
        context=controls._control_context(c['project'].id);_,machine,book=control_paths(context,batch)
        original=machine.read_bytes();assert original!=book.read_bytes()
        fixed=recovery.recover_claim(project_id=c['project'].id,batch_id=batch,chapter=2,stage=stage)
        assert not controls.handles and fixed['control']['explicit_resume_required']
        assert machine.read_bytes()==book.read_bytes()==original and counters(c)==previous
        assert recovery.recover_claim(project_id=c['project'].id,batch_id=batch,chapter=2,stage=stage)==fixed
        view,handle=controls.command(project_id=c['project'].id,batch_id=batch,action='resume',idempotency_key='explicit-claim-'+stage,
            expected_control_sha256=fixed['control']['control']['record_sha256'],author_confirmed=True)
        assert recovery.recover_claim(project_id=c['project'].id,batch_id=batch,chapter=2,stage=stage)['control']==view
        assert controls.handles[batch] is handle and counters(c)==previous
        view=run_stage(c,controls,handle,view,stage)
        record_observation(c,stage,'claim_step')
    assert view['progress']['status']=='completed' and counters(c)==('2','2')
    assert load_verified_reviewed_identity(c['root'])['history']['value']['committed_through']==2
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2).store.repair_cycle_claim()['repair_cycles_consumed']==1
    assert not (c['root']/'.novelagent/runtime/budgets/c3').exists()


def test_four_actual_model_stage_finish_exits_require_original_command_full_artifacts_and_preserve_new_run(batch_case):
    finish_pipeline(batch_case,{'generation','extraction','repair_generation','repair_extraction'})


def test_actual_repair_quality_finish_exit_reconciles_only_original_full_quality_and_preserves_new_run(batch_case):
    finish_pipeline(batch_case,{'repair_quality'})


@pytest.mark.parametrize('stage',['generation','extraction','quality','repair_generation','repair_extraction','repair_quality'])
def test_original_finish_recovers_after_author_edit_but_edited_draft_is_not_automatically_rechecked_or_dispatched(batch_case,stage):
    from tests.test_reviewed_policy_repair_flow import good
    from tests.test_reviewed_policy_generation import factory as generation_factory
    from workbench.reviewed_batch_driver import ReviewedBatchDriver
    c=batch_case;controls,batch,view,handle=started(c)
    for before in ORDER:
        if before==stage:break
        if before=='generation' and stage=='quality':
            driver,gc,_=build(c,controls);driver.generation.executor_factory=generation_factory(gc,prose=good(c['root'],2))
            view=driver.generation.generate(handle,expected_control_sha256=view['control']['record_sha256'])['control']
        else:view=run_stage(c,controls,handle,view,before)
    controls=crash(c,batch,view,stage,'finish_step');recovery=ReviewedBatchStageRecoveryService(controls)
    context=controls._control_context(c['project'].id);_,machine,book=control_paths(context,batch)
    original=machine.read_bytes();old_book=book.read_bytes();saved=recovery._service(stage).get(
        project_id=c['project'].id,batch_id=batch,chapter=2)['result']
    draft_id=saved['review']['preparation']['binding']['draft_id'] if stage.endswith('quality') else saved['draft_id']
    drafts=c['publication'].preparations.drafts;draft=drafts.get(draft_id)
    edited=drafts.update(draft.id,text=draft.text+'\n作者中断后编辑，尚未确认新事实。',expected_revision=draft.revision)
    before_calls=counters(c)
    fixed=recovery.recover_finish(project_id=c['project'].id,batch_id=batch,chapter=2,stage=stage)
    assert fixed['result']==saved and drafts.get(edited.id)==edited and not controls.handles
    assert machine.read_bytes()==book.read_bytes()==original and counters(c)==before_calls
    assert recovery.recover_finish(project_id=c['project'].id,batch_id=batch,chapter=2,stage=stage)==fixed
    view,handle=controls.command(project_id=c['project'].id,batch_id=batch,action='resume',idempotency_key='explicit-edited-'+stage,
        expected_control_sha256=fixed['control']['control']['record_sha256'],author_confirmed=True)
    with pytest.raises(BatchControlError):ReviewedBatchDriver(controls).run(handle,expected_control_sha256=view['control']['record_sha256'])
    assert drafts.get(edited.id)==edited and counters(c)==before_calls and not controls.handles


def finish_pipeline(c,finish_stages):
    from core.engine.chapter_candidate import ChapterCandidateStore
    from core.engine.chapter_fact_extraction import ChapterFactExtractionStore
    c['route']['end_chapter']=2
    controls,batch,view,handle=started(c)
    for stage in ORDER:
        assert view['progress']['stage']==stage
        if stage in finish_stages:
            controls=crash(c,batch,view,stage,'finish_step');recovery=ReviewedBatchStageRecoveryService(controls)
            context=controls._control_context(c['project'].id);_,machine,book=control_paths(context,batch)
            original=machine.read_bytes();prefix=book.read_bytes();previous=counters(c)
            assert original!=prefix
            metadata=recovery.get(project_id=c['project'].id,batch_id=batch)
            assert metadata['original_recovery']['action']=='finish_step' and metadata['original_recovery']['stage']==stage
            service=recovery._service(stage);saved=service.get(project_id=c['project'].id,batch_id=batch,chapter=2)['result']
            result_path=context[6]/'reviewed/batch_results'/(saved['step_id']+'.json');result_bytes=result_path.read_bytes()
            # A saved success alone must not append a finish missing from both logs.
            machine.write_bytes(prefix)
            try:
                with pytest.raises(BatchControlError):recovery.recover_finish(project_id=c['project'].id,batch_id=batch,chapter=2,stage=stage)
                assert machine.read_bytes()==book.read_bytes()==prefix and result_path.read_bytes()==result_bytes
            finally:machine.write_bytes(original)
            bad=dict(saved,input_sha256=True);bad['result_sha256']=digest({k:v for k,v in bad.items() if k!='result_sha256'})
            result_path.write_text(json.dumps(bad,ensure_ascii=False),encoding='utf-8')
            try:
                with pytest.raises(BatchControlError):service.get(project_id=c['project'].id,batch_id=batch,chapter=2)
            finally:result_path.write_bytes(result_bytes)
            store=ChapterCandidateStore(c['root']) if stage.endswith('generation') else ChapterFactExtractionStore(c['root'])
            artifact=saved['candidate_id'] if stage.endswith('generation') else (
                saved['review']['policy_review']['extraction']['extraction_id'] if stage.endswith('quality') else saved['extraction_id'])
            intent=store.load_intent(artifact)
            receipt=next(store.model_call_store(intent).receipts_dir.glob('*.json'));receipt_bytes=receipt.read_bytes();receipt.unlink()
            try:
                with pytest.raises(BatchControlError):recovery.recover_finish(project_id=c['project'].id,batch_id=batch,chapter=2,stage=stage)
                assert machine.read_bytes()==original and book.read_bytes()==prefix and not receipt.exists()
            finally:receipt.write_bytes(receipt_bytes)
            fixed=recovery.recover_finish(project_id=c['project'].id,batch_id=batch,chapter=2,stage=stage)
            assert not controls.handles and fixed['control']['explicit_resume_required'] and counters(c)==previous
            assert machine.read_bytes()==book.read_bytes()==original and result_path.read_bytes()==result_bytes
            assert recovery.recover_finish(project_id=c['project'].id,batch_id=batch,chapter=2,stage=stage)==fixed
            view,handle=controls.command(project_id=c['project'].id,batch_id=batch,action='resume',idempotency_key='explicit-finish-'+stage,
                expected_control_sha256=fixed['control']['control']['record_sha256'],author_confirmed=True)
            assert recovery.recover_finish(project_id=c['project'].id,batch_id=batch,chapter=2,stage=stage)['control']==view
            assert controls.handles[batch] is handle and counters(c)==previous
            record_observation(c,stage,'finish_step')
        else:view=run_stage(c,controls,handle,view,stage)
    assert view['progress']['status']=='completed' and counters(c)==('2','2')
    assert load_verified_reviewed_identity(c['root'])['history']['value']['committed_through']==2
    assert not (c['root']/'.novelagent/runtime/budgets/c3').exists()


CHILD=r'''import json,os,sys
from pathlib import Path
from types import SimpleNamespace
from tests.test_reviewed_batches import cold_service
from tests.test_reviewed_stage_recovery import run_stage
from workbench.reviewed_batch_controls import ReviewedBatchControlService,control_paths
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
controls=ReviewedBatchControlService(cold_service(spec).publication)
view,handle=controls.command(project_id=spec['project_id'],batch_id=spec['batch_id'],action='resume',
    idempotency_key='explicit-child-'+spec['kind']+'-'+spec['stage'],expected_control_sha256=spec['expected'],author_confirmed=True)
_,machine,_=control_paths(controls._control_context(spec['project_id']),spec['batch_id'])
def stop(event):
    if event=='after_control_machine':
        command=json.loads(machine.read_text(encoding='utf-8'))['commands'][-1]
        if command['action']==spec['kind']:os._exit(96 if spec['kind']=='claim_step' else 97)
controls.fault_injector=stop
case={'root':Path(spec['root']),'project':SimpleNamespace(id=spec['project_id'],book_id=spec['book_id'])}
run_stage(case,controls,handle,view,spec['stage'])
raise AssertionError('original stage process exit point not reached')
'''
