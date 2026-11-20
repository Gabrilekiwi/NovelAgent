"""Actual finite stage services, original receipts, revocation and cold reads."""
import json,subprocess,sys
from pathlib import Path

import pytest

from core.chapter_execution_budget import ChapterExecutionBudget
from core.engine.chapter_fact_contract import fact_contract_example
from core.engine.chapter_candidate import ChapterCandidateExecutionError
from core.engine.reviewed_batch_control import BatchControlError,BatchControlPaused
from core.engine.reviewed_candidate import ReviewedChapterCandidateExecutor
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import digest
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_batch_controls import started
from tests.test_reviewed_policy_generation import factory as generation_factory
from tests.test_reviewed_policy_extractions import factory as extraction_factory
from tests.test_workbench_chapter_candidates import _fixed_provenance
from workbench.reviewed_batch_controls import ReviewedBatchControlService
from workbench.reviewed_batch_driver import ReviewedBatchDriver
from workbench.reviewed_policy_readback import ReviewedPolicyReadbackService


def driver(c,controls,*,fault=None,contexts=None):
    gc=c['root'].parent/'driver-generation.txt';ec=c['root'].parent/'driver-extraction.txt'
    good=Path('tests/fixtures/chapter_quality/good_chapter.md').read_text(encoding='utf-8').replace('林雪','林澈').replace('陈岚','周禾')
    def create(root):
        view=controls.get(project_id=c['project'].id,batch_id=next(iter(controls.handles)))
        chapter=view['progress']['chapter'];trusted=ReviewedChapterCandidateContextBuilder(root).build(chapter)
        blueprint=trusted.story_project_context['chapter_blueprint'];state=trusted.story_project_context['reviewed_state']
        assert state['committed_through']==chapter-1
        if contexts is not None:contexts[chapter]=trusted.context_sha256
        if chapter>2:
            previous=ReviewedPolicyReadbackService(controls).get(project_id=c['project'].id,
                batch_id=next(iter(controls.handles)),chapter=chapter-1)['result']
            if previous['next_context']['context_sha256']!=trusted.context_sha256:
                # A moved restored book must use the actual qualified bridge,
                # rather than treating an old physical root as current.
                from workbench.restored_batch_continuation import require_restored_parent_bridge
                from workbench.reviewed_policy_readback import _next
                context=controls._control_context(c['project'].id)
                grant=controls.batches._stored(context,view['control']['binding']['batch_id'])['grant']
                require_restored_parent_bridge(context,grant,previous,_next(root,chapter))
            assert previous['state_sha256']==digest(state['authoritative_state'])
        text=(f'第{chapter}章，警戒行动进入第{chapter-1}轮。林澈左臂的旧伤再次裂开，闸门后的声音准确叫出了他的名字。周岚扶住他，继续检查封闭闸门旁的电路。\n\n'+good
            +'\n\n'+fact_contract_example()['chapter_text']+'\n\n'+'。'.join(b['text'] for b in blueprint['required_beats'])+'。\n\n'+blueprint['ending_pressure']+'。')
        return generation_factory(gc,prose=text)(root)
    return ReviewedBatchDriver(controls,generation_executor_factory=create,extraction_executor_factory=extraction_factory(ec),
        fault_injector=fault),gc,ec


def calls(c,chapter):
    return ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=chapter).store.report()['charged']['calls']


def test_real_driver_finishes_three_chapters_from_one_start_with_actual_parent_context(batch_case,monkeypatch):
    c=batch_case;outline=c['root']/'大纲/细纲_第003章.md'
    (c['root']/'大纲/细纲_第004章.md').write_text(outline.read_text(encoding='utf-8').replace('第3章','第4章').replace('第003章','第004章'),encoding='utf-8')
    c['route']['end_chapter']=4
    controls,batch,view,handle=started(c);contexts={};seen=[]
    def deny(*args,**kwargs):pytest.fail('driver impersonated author review')
    monkeypatch.setattr(c['publication'].preparations,'prepare',deny)
    monkeypatch.setattr('workbench.chapter_facts.ChapterFactReviewService.create_from_extraction',deny)
    def observed(event,step):
        assert event=='after_stage';seen.append((step['chapter'],step['stage']))
        chapter=step['chapter']
        print('driver stage',chapter,step['stage'],flush=True)
        if chapter<4:assert calls(c,chapter+1)==0
        if step['stage']=='readback':
            actual=load_verified_reviewed_identity(c['root'])['history']['value']
            assert actual['committed_through']==chapter
            assert actual['current_state']['numeric_counters']['alert_level']['current_value']==2*(chapter-1)
    service,gc,ec=driver(c,controls,fault=observed,contexts=contexts)
    before=view['control'];assert service.get(project_id=c['project'].id,batch_id=batch)['control']==before
    assert not gc.exists() and not ec.exists()
    result=service.run(handle,expected_control_sha256=before['record_sha256'])
    assert result['control']['progress']['status']=='completed' and not result['explicit_resume_required']
    assert result['execution_permit'] is result['publication_authorized'] is False
    assert seen==[(n,s) for n in (2,3,4) for s in ('generation','extraction','quality','publication','readback')]
    assert len(result['steps'])==15 and set(contexts)=={2,3,4} and gc.read_text()==ec.read_text()=='3'
    commands=result['control']['control']['commands']
    assert sum(c['action']=='start' for c in commands)==1 and not any(c['action']=='resume' for c in commands)
    assert [calls(c,n) for n in (2,3,4)]==[2,2,2]
    with c['db'].connection() as db:
        assert db.execute('SELECT COUNT(*) FROM chapter_fact_reviews').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM chapter_publication_index').fetchone()[0]==3


def test_pause_duplicate_driver_and_cold_reads_require_explicit_resume_without_generation_replay(batch_case):
    c=batch_case;controls,batch,view,handle=started(c);signals=ReviewedBatchControlService(c['publication']);duplicates=[]
    def pause(event,step):
        current=controls.get(project_id=c['project'].id,batch_id=batch)
        with pytest.raises(BatchControlPaused):ReviewedBatchDriver(controls).run(handle,expected_control_sha256=current['control']['record_sha256'])
        duplicates.append(step)
        signals.command(project_id=c['project'].id,batch_id=batch,action='pause',idempotency_key='pause-driver-after-stage',
            expected_control_sha256=current['control']['record_sha256'],author_confirmed=True)
    service,gc,ec=driver(c,controls,fault=pause)
    with pytest.raises(BatchControlError):service.run(handle,expected_control_sha256='f'*64)
    assert controls.handles[batch] is handle and not gc.exists()
    result=service.run(handle,expected_control_sha256=view['control']['record_sha256'])
    assert result['control']['progress']['status']=='paused' and result['control']['progress']['stage']=='extraction'
    assert len(duplicates)==1 and gc.read_text()=='1' and not ec.exists() and calls(c,3)==0
    cold=ReviewedBatchControlService(c['publication']);read,gc,ec=driver(c,cold)
    observed=read.get(project_id=c['project'].id,batch_id=batch)
    assert observed['explicit_resume_required'] and not cold.handles and gc.read_text()=='1' and not ec.exists()
    with pytest.raises(BatchControlPaused):read.run(handle,expected_control_sha256=observed['control']['record_sha256'])
    resumed,new_handle=cold.command(project_id=c['project'].id,batch_id=batch,action='resume',idempotency_key='explicit-driver-resume',
        expected_control_sha256=observed['control']['record_sha256'],author_confirmed=True)
    def pause_extraction(event,step):
        assert step['stage']=='extraction'
        current=cold.get(project_id=c['project'].id,batch_id=batch)
        signals.command(project_id=c['project'].id,batch_id=batch,action='pause',idempotency_key='pause-after-extraction',
            expected_control_sha256=current['control']['record_sha256'],author_confirmed=True)
    continued,_,_=driver(c,cold,fault=pause_extraction)
    final=continued.run(new_handle,expected_control_sha256=resumed['control']['record_sha256'])
    assert final['control']['progress']['stage']=='quality' and final['control']['progress']['status']=='paused'
    assert gc.read_text()==ec.read_text()=='1' and calls(c,3)==0


def test_physical_unknown_call_stops_driver_and_cannot_be_replayed_by_cold_resume(batch_case):
    from core.model_call_runtime import current_model_call_runtime
    from core.engine.reviewed_batch_control import current_policy_step
    from tests.test_chapter_budget_runtime import args
    c=batch_case;controls,batch,view,handle=started(c);counter=c['root'].parent/'unknown-driver-count.txt'
    def factory(root):
        def generate(input_pack):
            def operation():
                counter.write_text(str((int(counter.read_text()) if counter.exists() else 0)+1),encoding='utf-8')
                raise TimeoutError('synthetic dispatched call outcome unknown')
            return current_model_call_runtime().execute_attempt(**args(call_id='driver-unknown',stage='chapter_candidate_generation',
                endpoint_fingerprint=current_policy_step().model['endpoint_fingerprint'],operation=operation))
        return ReviewedChapterCandidateExecutor(story_project_root=root,generator=generate,execution_kind='model',
            provenance_factory=_fixed_provenance,execution_budget_factory=ChapterExecutionBudget)
    service=ReviewedBatchDriver(controls,generation_executor_factory=factory)
    with pytest.raises(ChapterCandidateExecutionError) as uncertain:service.run(handle,expected_control_sha256=view['control']['record_sha256'])
    assert uncertain.value.status=='provider_uncertain'
    assert counter.read_text()=='1' and not controls.handles and calls(c,2)==1 and calls(c,3)==0
    cold=ReviewedBatchControlService(c['publication']);observed=cold.get(project_id=c['project'].id,batch_id=batch)
    assert observed['progress']['status']=='paused' and observed['explicit_resume_required']
    from workbench.chapter_commits import ChapterCommitConflict
    with pytest.raises(ChapterCommitConflict):cold.command(project_id=c['project'].id,batch_id=batch,action='resume',
        idempotency_key='unknown-must-not-resend',expected_control_sha256=observed['control']['record_sha256'],author_confirmed=True)
    assert not cold.handles and counter.read_text()=='1' and calls(c,2)==1 and calls(c,3)==0


def test_changed_frozen_source_at_driver_entrance_pauses_without_any_model_call(batch_case):
    c=batch_case;controls,batch,view,handle=started(c);service,gc,ec=driver(c,controls)
    path=c['root']/'大纲/细纲_第003章.md';original=path.read_bytes();path.write_bytes(original+'\n新增作者节拍。'.encode('utf-8'))
    with pytest.raises(ValueError):service.run(handle,expected_control_sha256=view['control']['record_sha256'])
    state=service.get(project_id=c['project'].id,batch_id=batch)
    assert state['progress']['status']=='paused' and state['explicit_resume_required'] and not controls.handles
    assert state['progress']['pending_step'] is None and not gc.exists() and not ec.exists() and calls(c,2)==calls(c,3)==0
    assert path.read_bytes()!=original


@pytest.mark.parametrize('old_error',[False,True])
def test_new_resume_in_same_service_is_never_continued_or_paused_by_old_driver(batch_case,old_error):
    c=batch_case;controls,batch,view,handle=started(c);newer=[]
    def resume(event,step):
        assert step['stage']=='generation' and not newer
        current=controls.get(project_id=c['project'].id,batch_id=batch)
        resumed,new_handle=controls.command(project_id=c['project'].id,batch_id=batch,action='resume',
            idempotency_key='new-author-resume',expected_control_sha256=current['control']['record_sha256'],author_confirmed=True)
        # Same-service epoch reuse must not make the old command current.
        assert new_handle.epoch==handle.epoch
        newer.append(resumed)
        if old_error:raise RuntimeError('synthetic old driver error after newer resume')
    service,gc,ec=driver(c,controls,fault=resume)
    with pytest.raises(RuntimeError if old_error else BatchControlPaused):service.run(handle,
        expected_control_sha256=view['control']['record_sha256'])
    current=controls.get(project_id=c['project'].id,batch_id=batch)
    assert current==newer[0] and current['progress']['status']=='running' and not current['explicit_resume_required']
    assert controls.handles[batch] is handle and current['progress']['stage']=='extraction'
    assert gc.read_text()=='1' and not ec.exists() and calls(c,2)==1 and calls(c,3)==0


def test_real_driver_process_exit_after_generation_requires_explicit_resume_and_never_regenerates(batch_case):
    c=batch_case;controls,batch,view,handle=started(c)
    grant=controls.batches.get(project_id=c['project'].id,batch_id=batch)['grant'];selection=grant['manifest']['model_selection']
    spec={'root':str(c['root']),'database':str(c['db'].path),'machine':str(c['machine']),
        'book_id':c['project'].book_id,'project_id':c['project'].id,'batch_id':batch,'expected':view['control']['record_sha256'],
        'config':{k:v for k,v in selection.items() if k.startswith(('openai_','provider_'))}}
    spec['config']['openai_base_url']=c['config'].openai_base_url
    fixture=c['root'].parent/'driver.fixture.json';fixture.write_text(json.dumps(spec),encoding='utf-8')
    child=subprocess.run([sys.executable,'-B','-c',CHILD,str(fixture)],capture_output=True,text=True,timeout=300)
    assert child.returncode==91,child.stderr
    cold=ReviewedBatchControlService(c['publication']);service,gc,ec=driver(c,cold)
    observed=service.get(project_id=c['project'].id,batch_id=batch)
    assert not cold.handles and observed['explicit_resume_required'] and observed['progress']['stage']=='extraction'
    assert gc.read_text()=='1' and not ec.exists() and calls(c,2)==1 and calls(c,3)==0
    with pytest.raises(BatchControlPaused):service.run(handle,expected_control_sha256=observed['control']['record_sha256'])
    resumed,new_handle=cold.command(project_id=c['project'].id,batch_id=batch,action='resume',idempotency_key='explicit-after-driver-exit',
        expected_control_sha256=observed['control']['record_sha256'],author_confirmed=True)
    def stop_after_readback(event,step):
        if step['stage']=='readback':
            current=cold.get(project_id=c['project'].id,batch_id=batch)
            cold.command(project_id=c['project'].id,batch_id=batch,action='pause',idempotency_key='pause-after-recovered-chapter',
                expected_control_sha256=current['control']['record_sha256'],author_confirmed=True)
    continuation,gc,ec=driver(c,cold,fault=stop_after_readback)
    result=continuation.run(new_handle,expected_control_sha256=resumed['control']['record_sha256'])
    assert [s['stage'] for s in result['steps']]==['extraction','quality','publication','readback']
    assert result['control']['progress']['chapter']==3 and result['control']['progress']['status']=='paused'
    assert gc.read_text()==ec.read_text()=='1' and calls(c,2)==2 and calls(c,3)==0
    assert load_verified_reviewed_identity(c['root'])['history']['value']['committed_through']==2


CHILD=r'''import json,os,sys
from pathlib import Path
from types import SimpleNamespace
from tests.test_reviewed_batches import cold_service
from tests.test_reviewed_batch_driver import driver
from workbench.reviewed_batch_controls import ReviewedBatchControlService
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
controls=ReviewedBatchControlService(cold_service(spec).publication)
view,handle=controls.command(project_id=spec['project_id'],batch_id=spec['batch_id'],action='resume',idempotency_key='explicit-child-driver',
    expected_control_sha256=spec['expected'],author_confirmed=True)
def stop(event,step):
    if step['stage']=='generation':os._exit(91)
case={'root':Path(spec['root']),'project':SimpleNamespace(id=spec['project_id'],book_id=spec['book_id'])}
service,_,_=driver(case,controls,fault=stop)
service.run(handle,expected_control_sha256=view['control']['record_sha256'])
raise AssertionError('driver death point not reached')
'''
