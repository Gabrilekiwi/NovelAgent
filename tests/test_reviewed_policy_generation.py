"""Actual original-grant policy Candidate, durable calls and author preservation."""
import copy,json,os,socket,subprocess,sys
from pathlib import Path

import pytest

from api.contracts import ModelResponse
from core.chapter_execution_budget import ChapterExecutionBudget
from core.engine.chapter_candidate import ChapterCandidateStore,ChapterCandidateIntegrityError,_validate_candidate_intent
from core.engine.reviewed_batch_control import BatchControlError,BatchControlPaused
from core.engine.reviewed_candidate import ReviewedChapterCandidateExecutor
from core.engine.reviewed_policy_generation import policy_generation_authorization
from core.model_call_runtime import current_model_call_runtime
from core.story_project.reviewed_state_document import digest
from tests.test_chapter_budget_runtime import args,response
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_batch_controls import started
from tests.test_workbench_chapter_candidates import _fixed_provenance,PROSE
from workbench.reviewed_batch_controls import ReviewedBatchControlService
from workbench.reviewed_policy_generation import ReviewedPolicyGenerationService


def factory(count_path,*,crash_inside=False,prose=PROSE):
    def create(root):
        def generator(input_pack):
            runtime=current_model_call_runtime()
            def operation():
                n=int(count_path.read_text()) if count_path.exists() else 0
                count_path.write_text(str(n+1),encoding='utf-8')
                if crash_inside:os._exit(82)
                base=response()
                return ModelResponse(prose,actual_model=base.actual_model,endpoint_type=base.endpoint_type,usage=base.usage)
            from core.engine.reviewed_batch_control import current_policy_step
            permit=current_policy_step()
            return runtime.execute_attempt(**args(call_id='policy-generation',stage='chapter_candidate_generation',
                endpoint_fingerprint=permit.model['endpoint_fingerprint'],operation=operation))
        return ReviewedChapterCandidateExecutor(story_project_root=root,generator=generator,
            execution_kind='model',provenance_factory=_fixed_provenance,execution_budget_factory=ChapterExecutionBudget)
    return create


def test_actual_policy_generation_binds_call_and_original_draft_without_author_card(batch_case,monkeypatch):
    c=batch_case;controls,batch_id,view,handle=started(c);counter=c['root'].parent/'generation-count.txt'
    def deny(*args,**kwargs):pytest.fail('automatic generation impersonated author confirmation')
    monkeypatch.setattr('workbench.chapter_cards.ChapterCardService.confirm',deny)
    service=ReviewedPolicyGenerationService(controls,executor_factory=factory(counter))
    plan=service._plan(handle);step_id='step_'+digest({'batch_id':batch_id,'chapter':2,'stage':'generation'})[:32]
    authorization=policy_generation_authorization(grant=plan['grant'],prepared=plan['prepared'],
        binding={'step_id':step_id,'execution_id':plan['execution_id']})
    with pytest.raises(BatchControlPaused):plan['executor'].execute_policy(chapter_index=2,chapter_intent=plan['chapter_intent'],
        policy_authorization=authorization,idempotency_key=plan['key'])
    assert not plan['executor'].store.has_intent(plan['candidate_id']) and not counter.exists()
    result=service.generate(handle,expected_control_sha256=view['control']['record_sha256'])
    loaded=ChapterCandidateStore(c['root']).load(result['generation']['candidate_id'])
    assert loaded['intent']['schema_version']=='1.2' and loaded['record']['schema_version']=='1.2'
    assert loaded['intent']['policy_authorization']==authorization and 'author_confirmation' not in loaded['intent']
    assert result['request_source']=='policy' and result['author_confirmed'] is False
    assert result['draft']['revision']==1 and result['draft']['text']==PROSE
    assert result['control']['progress']['stage']=='extraction' and result['control']['progress']['pending_step'] is None
    assert counter.read_text()=='1'
    assert loaded['record']['evidence']['receipt_statuses']==['succeeded']
    assert len(loaded['record']['evidence']['intent_hashes'])==len(loaded['record']['evidence']['receipt_hashes'])==1
    assert not (c['root']/'.novelagent/runtime/reviewed/policy_reviews').exists()
    # Public reads used for author takeover must not invent an author card.
    from workbench.chapter_candidates import ChapterCandidateService
    from workbench.chapter_cards import ChapterCardService
    public=ChapterCandidateService(projects=c['publication'].projects,chapter_cards=ChapterCardService(c['publication'].projects),
        database=c['db'])
    observed=public.get(c['project'].id,2,plan['candidate_id'])
    assert observed['confirmation'] is None and observed['generation_source']=='policy'
    assert counter.read_text()=='1'
    for kind in ('fake_author','wrong_execution','boolean_chapter','wrong_input'):
        forged=copy.deepcopy(loaded['intent'])
        if kind=='fake_author':forged['author_confirmation']={'confirmed':True}
        elif kind=='wrong_execution':forged['policy_authorization']['execution_id']='execution_candidate_'+'f'*32
        elif kind=='boolean_chapter':forged['policy_authorization']['chapter']=True
        else:forged['policy_authorization']['input_sha256']='f'*64
        forged['intent_hash']=digest({k:v for k,v in forged.items() if k!='intent_hash'})
        with pytest.raises((BatchControlError,ChapterCandidateIntegrityError)):_validate_candidate_intent(forged)


def test_author_edit_between_generation_and_finish_is_preserved_and_blocks_advance(batch_case):
    c=batch_case;controls,batch_id,view,handle=started(c);counter=c['root'].parent/'generation-count.txt';edited=[]
    drafts=c['publication'].preparations.drafts
    def edit(event):
        if event=='after_generation_result_before_finish':
            plan=service._plan(handle);draft=drafts.get_for_candidate(c['project'].id,plan['candidate_id'])
            edited.append(drafts.update(draft.id,text=draft.text+'\n作者保留的修改。',expected_revision=draft.revision))
    service=ReviewedPolicyGenerationService(controls,executor_factory=factory(counter),fault_injector=edit)
    with pytest.raises(BatchControlError):service.generate(handle,expected_control_sha256=view['control']['record_sha256'])
    current=controls.get(project_id=c['project'].id,batch_id=batch_id)
    assert current['progress']['stage']=='generation' and current['progress']['pending_step'] is not None
    cold=ReviewedBatchControlService(c['publication'])
    resumed,new_handle=cold.command(project_id=c['project'].id,batch_id=batch_id,action='resume',idempotency_key='author-explicit-resume',
        expected_control_sha256=current['control']['record_sha256'],author_confirmed=True)
    with pytest.raises(BatchControlError):ReviewedPolicyGenerationService(cold,executor_factory=factory(counter)).generate(new_handle,
        expected_control_sha256=resumed['control']['record_sha256'])
    assert drafts.get(edited[0].id)==edited[0] and counter.read_text()=='1'
    assert cold.get(project_id=c['project'].id,batch_id=batch_id)['progress']['stage']=='generation'


@pytest.mark.parametrize('phase',['after_result','inside_dispatch'])
def test_real_process_death_never_creates_automatic_second_generation(batch_case,phase):
    c=batch_case;controls,batch_id,view,handle=started(c);counter=c['root'].parent/'generation-count.txt'
    grant=controls.batches.get(project_id=c['project'].id,batch_id=batch_id)['grant']
    selection=grant['manifest']['model_selection']
    spec={'root':str(c['root']),'database':str(c['db'].path),'machine':str(c['machine']),
        'project_id':c['project'].id,'batch_id':batch_id,'expected':view['control']['record_sha256'],
        'counter':str(counter),'phase':phase,'config':{k:v for k,v in selection.items() if k.startswith(('openai_','provider_'))}}
    spec['config']['openai_base_url']=c['config'].openai_base_url
    path=c['root'].parent/'policy-generation.fixture.json';path.write_text(json.dumps(spec),encoding='utf-8')
    child=subprocess.run([sys.executable,'-B','-c',CHILD,str(path)],capture_output=True,text=True,timeout=120)
    assert child.returncode==(81 if phase=='after_result' else 82),child.stderr
    assert counter.read_text()=='1'
    cold=ReviewedBatchControlService(c['publication'])
    observed=cold.get(project_id=c['project'].id,batch_id=batch_id)
    assert observed['explicit_resume_required'] and not cold.handles and observed['progress']['stage']=='generation'
    service=ReviewedPolicyGenerationService(cold,executor_factory=factory(counter))
    with pytest.raises(BatchControlPaused):service.generate(handle,expected_control_sha256=observed['control']['record_sha256'])
    assert counter.read_text()=='1'
    original=observed['progress']['pending_step']
    if phase=='inside_dispatch':
        # The existing owned boundary rejects unresolved calls even before a
        # new resume command can be recorded. Do not weaken it to reach a
        # later clock check, and never turn a crash into a second request.
        from workbench.chapter_commits import ChapterCommitConflict
        with pytest.raises(ChapterCommitConflict):cold.command(project_id=c['project'].id,batch_id=batch_id,
            action='resume',idempotency_key='cold-explicit-resume',expected_control_sha256=observed['control']['record_sha256'],author_confirmed=True)
        assert cold.get(project_id=c['project'].id,batch_id=batch_id)['control']==observed['control']
        assert counter.read_text()=='1' and original is not None
        return
    resumed,new_handle=cold.command(project_id=c['project'].id,batch_id=batch_id,action='resume',idempotency_key='cold-explicit-resume',
        expected_control_sha256=observed['control']['record_sha256'],author_confirmed=True)
    assert resumed['progress']['pending_step']==original
    if phase=='after_result':
        result=service.generate(new_handle,expected_control_sha256=resumed['control']['record_sha256'])
        assert result['control']['progress']['stage']=='extraction'
        assert result['generation']['execution_id']==original['execution_id']
    else:
        from core.chapter_budget_store import ChapterBudgetError
        with pytest.raises(ChapterBudgetError):service.generate(new_handle,expected_control_sha256=resumed['control']['record_sha256'])
        assert cold.get(project_id=c['project'].id,batch_id=batch_id)['progress']['stage']=='generation'
    assert counter.read_text()=='1'


def test_actual_policy_candidate_reaches_full_final_quality_and_same_author_domain(batch_case):
    from core.engine.chapter_fact_contract import fact_contract_example
    from core.engine.chapter_fact_context import build_chapter_fact_context
    from core.engine.chapter_fact_extraction import ChapterFactExtractionExecutor
    from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
    from core.engine.reviewed_chapter_quality import prepare_candidate_chapter_quality
    from core.engine.chapter_commit_prepare import prepare_reviewed_chapter
    from workbench.reviewed_policy_reviews import ReviewedPolicyReviewService
    c=batch_case;controls,batch_id,view,handle=started(c);counter=c['root'].parent/'generation-count.txt'
    trusted=ReviewedChapterCandidateContextBuilder(c['root']).build(2)
    blueprint=trusted.story_project_context['chapter_blueprint'];example=fact_contract_example()
    good=Path('tests/fixtures/chapter_quality/good_chapter.md').read_text(encoding='utf-8').replace('林雪','林澈').replace('陈岚','周禾')
    text=('林澈左臂的旧伤再次裂开，闸门后的声音准确叫出了他的名字。周岚扶住他，继续检查封闭闸门旁的电路。\n\n'
        +good+'\n\n'+example['chapter_text']+'\n\n'+'。'.join(b['text'] for b in blueprint['required_beats'])+'。\n\n'+blueprint['ending_pressure']+'。')
    generated=ReviewedPolicyGenerationService(controls,executor_factory=factory(counter,prose=text)).generate(handle,
        expected_control_sha256=view['control']['record_sha256'])
    draft=generated['draft'];intent=ChapterCandidateStore(c['root']).load_intent(generated['generation']['candidate_id'])
    proposal=copy.deepcopy(example['proposal']);proposal['facts']=[f for f in proposal['facts'] if f['kind'] in {'event','counter'}]
    # The policy extraction entrypoint is still separate work. This explicitly
    # synthetic author extraction builds actual complete evidence for testing
    # the new policy generation -> shared final quality boundary.
    extracted=ChapterFactExtractionExecutor(story_project_root=c['root'],provider=lambda *args:json.dumps(proposal,ensure_ascii=False),
        execution_kind='deterministic',provenance_factory=_fixed_provenance).execute(book_id=c['project'].book_id,chapter_index=2,
            candidate_binding={'candidate_id':draft['candidate_id'],'intent_hash':intent['intent_hash']},
            draft_binding={'draft_id':draft['id'],'revision':draft['revision'],'sha256':draft['sha256']},
            context_sha256=trusted.context_sha256,authority_binding=intent['authority'],chapter_text=text,
            authority_view=build_chapter_fact_context(trusted,text).to_dict(),idempotency_key='explicit-fixture-extraction',author_confirmed=True)
    result=ReviewedPolicyReviewService(c['publication']).prepare(project_id=c['project'].id,batch_id=batch_id,draft_id=draft['id'],
        extraction_id=extracted['extraction_id'],expected_draft_revision=draft['revision'],expected_draft_sha256=draft['sha256'],
        expected_extraction_record_hash=extracted['record_hash'])
    quality=result['quality'];evidence=quality['candidate_evidence']
    assert quality['quality_coverage_complete'] and quality['local_quality_passed']
    assert result['quality_disposition']['automatic_action']=='continue'
    assert evidence['candidate_schema_version']=='1.2' and 'candidate_author_confirmation_sha256' not in evidence
    assert evidence['candidate_policy_authorization_sha256']==digest(intent['policy_authorization'])
    review=copy.deepcopy(result['policy_review'])
    review.update(record_type='chapter_fact_review',schema_version='1.0',state='confirmed',review_source='author',confirmation_sha256='a'*64)
    manual=prepare_reviewed_chapter(trusted_context=trusted,project_id=c['project'].id,draft=draft,review=review,idempotency_key='manual-domain-comparison')
    assert manual['fact_preparation']==result['preparation']['fact_preparation'] and manual['final_prose']==text.strip()+'\n'
    assert prepare_candidate_chapter_quality(trusted_context=trusted,chapter_preparation=manual)['candidate_evidence']==evidence
    assert counter.read_text()=='1'


CHILD=r'''import json,os,socket,sys
from pathlib import Path
from tests.test_reviewed_batches import cold_service
from tests.test_reviewed_policy_generation import factory
from workbench.reviewed_batch_controls import ReviewedBatchControlService
from workbench.reviewed_policy_generation import ReviewedPolicyGenerationService
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
def deny(*args,**kwargs):raise AssertionError('actual network forbidden')
socket.socket.connect=deny
controls=ReviewedBatchControlService(cold_service(spec).publication)
view,handle=controls.command(project_id=spec['project_id'],batch_id=spec['batch_id'],action='resume',idempotency_key='child-explicit-resume',
    expected_control_sha256=spec['expected'],author_confirmed=True)
def fault(event):
    if event=='after_generation_result_before_finish' and spec['phase']=='after_result':os._exit(81)
service=ReviewedPolicyGenerationService(controls,executor_factory=factory(Path(spec['counter']),crash_inside=spec['phase']=='inside_dispatch'),fault_injector=fault)
service.generate(handle,expected_control_sha256=view['control']['record_sha256'])
raise AssertionError('process death point not reached')
'''
