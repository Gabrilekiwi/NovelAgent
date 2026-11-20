"""Actual one-repair model cycle, complete re-extraction and final publication."""
import json,subprocess,sys
from pathlib import Path

import pytest

from core.chapter_execution_budget import ChapterExecutionBudget
from core.engine.chapter_candidate import ChapterCandidateStore
from core.engine.chapter_candidate import ChapterCandidateExecutionError
from core.engine.chapter_fact_contract import fact_contract_example
from core.engine.chapter_fact_extraction import ChapterFactExtractionStore
from core.engine.reviewed_batch_control import BatchControlError
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_batch_controls import started
from tests.test_reviewed_quality_disposition import PARAGRAPH
from tests.test_reviewed_policy_generation import factory as generation_factory
from tests.test_reviewed_policy_extractions import factory as extraction_factory
from workbench.reviewed_batch_controls import ReviewedBatchControlService
from workbench.reviewed_batch_driver import ReviewedBatchDriver
from workbench.reviewed_policy_repairs import repair_step
from workbench.reviewed_policy_publication import ReviewedPolicyPublicationService


def good(root,chapter):
    trusted=ReviewedChapterCandidateContextBuilder(root).build(chapter);blueprint=trusted.story_project_context['chapter_blueprint']
    prose=Path('tests/fixtures/chapter_quality/good_chapter.md').read_text(encoding='utf-8').replace('林雪','林澈').replace('陈岚','周禾')
    return (f'第{chapter}章，警戒行动进入第{chapter-1}轮。林澈左臂的旧伤再次裂开，闸门后的声音准确叫出了他的名字。周岚扶住他，继续检查封闭闸门旁的电路。\n\n'+prose
        +'\n\n'+fact_contract_example()['chapter_text']+'\n\n'+'。'.join(b['text'] for b in blueprint['required_beats'])+'。\n\n'+blueprint['ending_pressure']+'。')


def build(c,controls,*,repair_fails=False,fault=None):
    gc=c['root'].parent/'repair-generation.txt';ec=c['root'].parent/'repair-extraction.txt'
    def generate(root):
        view=controls.get(project_id=c['project'].id,batch_id=next(iter(controls.handles)))
        chapter=view['progress']['chapter'];stage=view['progress']['stage'];text=good(root,chapter)
        if stage=='generation' and chapter==2 or stage=='repair_generation' and repair_fails:
            text+='\n\n'+PARAGRAPH+'\n\n'+PARAGRAPH
        if stage=='repair_generation':
            claim=ChapterExecutionBudget(root,book_id=c['project'].book_id,chapter=chapter).store.repair_cycle_claim()
            assert claim is not None and claim['repair_cycles_consumed']==1
        return generation_factory(gc,prose=text)(root)
    return ReviewedBatchDriver(controls,generation_executor_factory=generate,extraction_executor_factory=extraction_factory(ec),
        fault_injector=fault),gc,ec


def saved(c,batch,stage):
    path=c['root']/'.novelagent/runtime/reviewed/batch_results'/(repair_step(batch,2,stage)+'.json')
    return path,json.loads(path.read_text(encoding='utf-8'))


def test_actual_single_repair_reextracts_every_fact_and_continues_after_formal_readback(batch_case,monkeypatch):
    c=batch_case;controls,batch,view,handle=started(c);initial={};stages=[]
    def deny(*args,**kwargs):pytest.fail('policy repair impersonated author confirmation')
    monkeypatch.setattr(c['publication'].preparations,'prepare',deny)
    monkeypatch.setattr('workbench.chapter_facts.ChapterFactReviewService.create_from_extraction',deny)
    def observe(event,step):
        stages.append((step['chapter'],step['stage']));print('repair flow',step['chapter'],step['stage'],flush=True)
        if step['chapter']==2 and step['stage'] in {'generation','extraction','quality'}:
            path,value=saved(c,batch,step['stage']);initial[step['stage']]=(path,path.read_bytes(),value)
        for path,original,_ in initial.values():assert path.read_bytes()==original
        if step['chapter']==2:
            assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0
    driver,gc,ec=build(c,controls,fault=observe)
    result=driver.run(handle,expected_control_sha256=view['control']['record_sha256'])
    assert result['control']['progress']['status']=='completed'
    assert stages==[(2,s) for s in ('generation','extraction','quality','repair_generation','repair_extraction','repair_quality','publication','readback')]+[(3,s) for s in ('generation','extraction','quality','publication','readback')]
    assert gc.read_text()==ec.read_text()=='3'
    budget=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2)
    claim=budget.store.repair_cycle_claim();assert claim['repair_cycles_consumed']==1 and budget.store.report()['charged']['calls']==4
    assert sum(c['action']=='begin_repair' for c in result['control']['control']['commands'])==1
    original=initial['generation'][2];_,repaired=saved(c,batch,'repair_generation')
    assert original['candidate_id']!=repaired['candidate_id'] and original['draft_id']!=repaired['draft_id']
    assert c['publication'].preparations.drafts.get(original['draft_id']).sha256==original['draft_sha256']
    intent=ChapterCandidateStore(c['root']).load_intent(repaired['candidate_id'])
    assert intent['policy_authorization']['repair_claim_sha256']==claim['claim_sha256'] and 'author_confirmation' not in intent
    assert 'original_prose' in intent['chapter_intent']['notes'][1] and PARAGRAPH in intent['chapter_intent']['notes'][1]
    _,extraction=saved(c,batch,'repair_extraction');assert extraction['extraction_id']!=initial['extraction'][2]['extraction_id']
    assert extraction['generation_result_sha256']==repaired['result_sha256']
    full=ChapterFactExtractionStore(c['root']).load(extraction['extraction_id'])
    assert len(full['proposal']['facts'])==2 and full['intent']['draft']['sha256']==repaired['draft_sha256']
    _,quality=saved(c,batch,'repair_quality');assert quality['schema_version']=='reviewed_policy_quality_result_v2'
    assert quality['review']['quality']['quality_coverage_complete'] and quality['review']['quality_disposition']['automatic_action']=='continue'
    state=load_verified_reviewed_identity(c['root'])['history']['value']
    assert state['committed_through']==3 and state['current_state']['numeric_counters']['alert_level']['current_value']==4
    with c['db'].connection() as db:assert db.execute('SELECT COUNT(*) FROM chapter_fact_reviews').fetchone()[0]==0
    # Historical claim/receipt reading must use the original frozen policy,
    # even when current policy selection is unavailable to this old contract.
    monkeypatch.setattr('core.quality.reviewed_disposition._POLICY',{'future_current_policy_unavailable':True})
    historical=ReviewedPolicyPublicationService(ReviewedBatchControlService(c['publication'])).resolve(
        project_id=c['project'].id,batch_id=batch,chapter=2)
    assert historical['state']=='published' and historical['publication']['chapter']==2
    assert budget.store.repair_cycle_claim()==claim and gc.read_text()==ec.read_text()=='3'


def test_failed_repair_pauses_and_restart_never_grants_second_cycle_or_next_chapter_call(batch_case):
    c=batch_case;controls,batch,view,handle=started(c);driver,gc,ec=build(c,controls,repair_fails=True)
    result=driver.run(handle,expected_control_sha256=view['control']['record_sha256'])
    assert result['control']['progress']['status']=='paused' and result['control']['progress']['stage']=='repair_quality'
    assert gc.read_text()==ec.read_text()=='2' and not controls.handles
    budget=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2)
    claim=budget.store.repair_cycle_claim();assert claim['repair_cycles_consumed']==1
    assert load_verified_reviewed_identity(c['root'])['history']['value']['committed_through']==1
    cold=ReviewedBatchControlService(c['publication']);continued,gc,ec=build(c,cold,repair_fails=True)
    observed=cold.get(project_id=c['project'].id,batch_id=batch);assert observed['explicit_resume_required'] and not cold.handles
    resumed,new_handle=cold.command(project_id=c['project'].id,batch_id=batch,action='resume',idempotency_key='explicit-failed-repair-resume',
        expected_control_sha256=observed['control']['record_sha256'],author_confirmed=True)
    again=continued.run(new_handle,expected_control_sha256=resumed['control']['record_sha256'])
    assert again['control']['progress']['status']=='paused' and gc.read_text()==ec.read_text()=='2'
    assert budget.store.repair_cycle_claim()==claim
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0


def test_author_edit_after_initial_failed_quality_is_preserved_and_prevents_consumption(batch_case):
    c=batch_case;controls,batch,view,handle=started(c);driver,gc,ec=build(c,controls);edited=[]
    def edit(event):
        if event=='after_quality_result_before_finish':
            _,generation=saved(c,batch,'generation');drafts=c['publication'].preparations.drafts;draft=drafts.get(generation['draft_id'])
            edited.append(drafts.update(draft.id,text=draft.text+'\n作者保留的判断。',expected_revision=draft.revision))
    driver.quality.fault_injector=edit
    with pytest.raises(BatchControlError):driver.run(handle,expected_control_sha256=view['control']['record_sha256'])
    assert len(edited)==1 and c['publication'].preparations.drafts.get(edited[0].id)==edited[0]
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2).store.repair_cycle_claim() is None
    assert gc.read_text()==ec.read_text()=='1' and not controls.handles


def test_post_claim_author_edit_preserves_consumed_cycle_but_blocks_repair_dispatch(batch_case):
    c=batch_case;controls,batch,view,handle=started(c);edited=[]
    def edit(event,step):
        if step['stage']=='quality':
            _,generation=saved(c,batch,'generation');drafts=c['publication'].preparations.drafts;draft=drafts.get(generation['draft_id'])
            edited.append(drafts.update(draft.id,text=draft.text+'\n作者接管后保留的判断。',expected_revision=draft.revision))
    driver,gc,ec=build(c,controls,fault=edit)
    with pytest.raises(BatchControlError):driver.run(handle,expected_control_sha256=view['control']['record_sha256'])
    budget=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2)
    assert budget.store.repair_cycle_claim()['repair_cycles_consumed']==1
    assert len(edited)==1 and c['publication'].preparations.drafts.get(edited[0].id)==edited[0]
    assert gc.read_text()==ec.read_text()=='1' and not controls.handles
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0


@pytest.mark.parametrize('missing',['claim_ledger','failed_quality','initial_extraction'])
def test_missing_original_repair_evidence_blocks_dispatch_without_replacing_it(batch_case,missing):
    from core.chapter_budget_store import ChapterBudgetError
    c=batch_case;controls,batch,view,handle=started(c);removed=[]
    def remove(event,step):
        if step['stage']=='quality':
            if missing=='claim_ledger':
                path=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2).store.root/'ledger.json'
            else:path,_=saved(c,batch,'quality' if missing=='failed_quality' else 'extraction')
            original=path.read_bytes();path.unlink();removed.append((path,original))
    driver,gc,ec=build(c,controls,fault=remove)
    try:
        with pytest.raises((ChapterBudgetError,BatchControlError)):driver.run(handle,expected_control_sha256=view['control']['record_sha256'])
        assert len(removed)==1 and not removed[0][0].exists()
        assert gc.read_text()==ec.read_text()=='1' and not controls.handles
        assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0
    finally:
        for path,original in removed:
            assert not path.exists();path.write_bytes(original)
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2).store.repair_cycle_claim()['repair_cycles_consumed']==1


def test_repair_stops_at_original_call_budget_before_dispatch_and_next_chapter(batch_case):
    from core.chapter_budget_store import ChapterBudgetError
    c=batch_case;c['route']['configuration']['chapter_limits']['max_provider_calls']=2
    controls,batch,view,handle=started(c);driver,gc,ec=build(c,controls)
    with pytest.raises((ChapterBudgetError,ChapterCandidateExecutionError)):driver.run(handle,expected_control_sha256=view['control']['record_sha256'])
    assert gc.read_text()==ec.read_text()=='1' and not controls.handles
    budget=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2)
    assert budget.store.report()['charged']['calls']==2
    assert controls.get(project_id=c['project'].id,batch_id=batch)['progress']['status']=='paused'
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0


def test_unknown_repair_physical_call_keeps_consumed_cycle_and_never_resends(batch_case):
    from core.engine.reviewed_candidate import ReviewedChapterCandidateExecutor
    from core.engine.reviewed_batch_control import current_policy_step
    from core.model_call_runtime import current_model_call_runtime
    from tests.test_chapter_budget_runtime import args
    from tests.test_workbench_chapter_candidates import _fixed_provenance
    from workbench.chapter_commits import ChapterCommitConflict
    c=batch_case;controls,batch,view,handle=started(c);driver,gc,ec=build(c,controls)
    def create(root):
        def generate(input_pack):
            assert current_policy_step().binding['stage']=='repair_generation'
            def operation():
                gc.write_text(str(int(gc.read_text())+1),encoding='utf-8')
                raise TimeoutError('synthetic repair dispatched outcome unknown')
            return current_model_call_runtime().execute_attempt(**args(call_id='repair-unknown',stage='chapter_candidate_generation',
                endpoint_fingerprint=current_policy_step().model['endpoint_fingerprint'],operation=operation))
        return ReviewedChapterCandidateExecutor(story_project_root=root,generator=generate,execution_kind='model',
            provenance_factory=_fixed_provenance,execution_budget_factory=ChapterExecutionBudget)
    driver.repair_generation.executor_factory=create
    with pytest.raises(ChapterCandidateExecutionError) as error:driver.run(handle,expected_control_sha256=view['control']['record_sha256'])
    assert error.value.status=='provider_uncertain' and gc.read_text()=='2' and ec.read_text()=='1'
    budget=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2);claim=budget.store.repair_cycle_claim()
    assert claim['repair_cycles_consumed']==1 and not controls.handles
    cold=ReviewedBatchControlService(c['publication']);observed=cold.get(project_id=c['project'].id,batch_id=batch)
    with pytest.raises(ChapterCommitConflict):cold.command(project_id=c['project'].id,batch_id=batch,action='resume',
        idempotency_key='cannot-resend-unknown-repair',expected_control_sha256=observed['control']['record_sha256'],author_confirmed=True)
    assert not cold.handles and gc.read_text()=='2' and ec.read_text()=='1' and budget.store.repair_cycle_claim()==claim
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0


def test_real_repair_process_exit_reuses_original_result_without_second_cycle_or_generation(batch_case):
    c=batch_case;controls,batch,view,handle=started(c)
    grant=controls.batches.get(project_id=c['project'].id,batch_id=batch)['grant'];selection=grant['manifest']['model_selection']
    spec={'root':str(c['root']),'database':str(c['db'].path),'machine':str(c['machine']),
        'book_id':c['project'].book_id,'project_id':c['project'].id,'batch_id':batch,'expected':view['control']['record_sha256'],
        'config':{k:v for k,v in selection.items() if k.startswith(('openai_','provider_'))}}
    spec['config']['openai_base_url']=c['config'].openai_base_url
    fixture=c['root'].parent/'repair.fixture.json';fixture.write_text(json.dumps(spec),encoding='utf-8')
    child=subprocess.run([sys.executable,'-B','-c',CHILD,str(fixture)],capture_output=True,text=True,timeout=360)
    assert child.returncode==93,child.stderr
    cold=ReviewedBatchControlService(c['publication']);driver,gc,ec=build(c,cold)
    observed=cold.get(project_id=c['project'].id,batch_id=batch)
    assert not cold.handles and observed['explicit_resume_required'] and observed['progress']['stage']=='repair_generation'
    assert gc.read_text()=='2' and ec.read_text()=='1'
    budget=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2);claim=budget.store.repair_cycle_claim()
    assert claim['repair_cycles_consumed']==1
    result_path,original=saved(c,batch,'repair_generation');original_bytes=result_path.read_bytes()
    resumed,new_handle=cold.command(project_id=c['project'].id,batch_id=batch,action='resume',idempotency_key='explicit-after-repair-exit',
        expected_control_sha256=observed['control']['record_sha256'],author_confirmed=True)
    def pause(event,step):
        if step['stage']=='readback':
            current=cold.get(project_id=c['project'].id,batch_id=batch)
            cold.command(project_id=c['project'].id,batch_id=batch,action='pause',idempotency_key='pause-after-repair-readback',
                expected_control_sha256=current['control']['record_sha256'],author_confirmed=True)
    driver.fault_injector=pause
    result=driver.run(new_handle,expected_control_sha256=resumed['control']['record_sha256'])
    assert result['control']['progress']['status']=='paused' and result['control']['progress']['chapter']==3
    assert result_path.read_bytes()==original_bytes and budget.store.repair_cycle_claim()==claim
    assert gc.read_text()==ec.read_text()=='2'
    assert load_verified_reviewed_identity(c['root'])['history']['value']['committed_through']==2
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0


CHILD=r'''import json,os,sys
from pathlib import Path
from types import SimpleNamespace
from tests.test_reviewed_batches import cold_service
from tests.test_reviewed_policy_repair_flow import build
from workbench.reviewed_batch_controls import ReviewedBatchControlService
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
controls=ReviewedBatchControlService(cold_service(spec).publication)
view,handle=controls.command(project_id=spec['project_id'],batch_id=spec['batch_id'],action='resume',idempotency_key='explicit-child-repair',
    expected_control_sha256=spec['expected'],author_confirmed=True)
case={'root':Path(spec['root']),'project':SimpleNamespace(id=spec['project_id'],book_id=spec['book_id'])}
driver,_,_=build(case,controls)
def stop(event):
    if event=='after_generation_result_before_finish':os._exit(93)
driver.repair_generation.fault_injector=stop
driver.run(handle,expected_control_sha256=view['control']['record_sha256'])
raise AssertionError('repair process exit point not reached')
'''
