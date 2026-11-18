"""Actual author review/commit after policy takeover and explicit continuation."""
import copy

import pytest

from core.chapter_execution_budget import ChapterExecutionBudget
from core.engine.chapter_fact_contract import fact_contract_example
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_policy_extractions import generated
from tests.test_reviewed_batch_driver import driver
from workbench.reviewed_batch_takeovers import ReviewedBatchTakeoverService
from workbench.reviewed_batch_controls import ReviewedBatchControlService
from core.engine.reviewed_batch_control import BatchControlError,BatchControlPaused
from core.story_project.paths import canonical_prose_path


def paused(c):
    g=generated(c)
    g['view']=g['controls'].command(project_id=c['project'].id,batch_id=g['batch_id'],action='pause',idempotency_key='pause-for-author',
        expected_control_sha256=g['view']['control']['record_sha256'],author_confirmed=True)
    return g


def author_commit(c,draft):
    reviews=c['publication'].preparations.fact_reviews
    review=reviews.create(draft.id,expected_draft_revision=draft.revision,expected_draft_sha256=draft.sha256,
        proposals=[f for f in fact_contract_example()['proposal']['facts'] if f['kind'] in {'event','counter'}],author_confirmed=True)
    facts=copy.deepcopy(review['facts'])
    for fact in facts:fact['review']['decision']='accepted'
    review=reviews.update(review['id'],facts=facts,expected_revision=review['revision'])
    review=reviews.confirm(review['id'],expected_revision=review['revision'],expected_facts_sha256=review['facts_sha256'],
        expected_draft_revision=draft.revision,expected_draft_sha256=draft.sha256,author_confirmed=True)
    route=dict(project_id=c['project'].id,chapter=2,review_id=review['id'],expected_draft_revision=draft.revision,
        expected_review_revision=review['revision'],expected_draft_sha256=draft.sha256,expected_facts_sha256=review['facts_sha256'],
        idempotency_key='real-author-takeover-commit')
    return route,c['publication'].commit(**route)


def test_author_edits_original_draft_commits_as_author_then_explicit_batch_resume_reads_new_state(batch_case):
    c=batch_case;g=paused(c);controls=g['controls'];service=ReviewedBatchTakeoverService(controls)
    take=service.takeover(project_id=c['project'].id,batch_id=g['batch_id'],draft_id=g['draft']['id'],
        expected_control_sha256=g['view']['control']['record_sha256'],author_confirmed=True)
    assert take['control']['progress']['stage']=='author_review' and take['control']['progress']['status']=='paused'
    assert not controls.handles and take['takeover']['draft_at_takeover']==g['draft']
    assert service.takeover(project_id=c['project'].id,batch_id=g['batch_id'],draft_id=g['draft']['id'],
        expected_control_sha256=g['view']['control']['record_sha256'],author_confirmed=True)==take
    assert service.get(project_id=c['project'].id,batch_id=g['batch_id'],chapter=2)['original_control_recorded']
    cold=ReviewedBatchControlService(c['publication']);view=cold.get(project_id=c['project'].id,batch_id=g['batch_id'])
    with pytest.raises(BatchControlError):cold.command(project_id=c['project'].id,batch_id=g['batch_id'],action='resume',
        idempotency_key='cannot-resume-uncommitted-author',expected_control_sha256=view['control']['record_sha256'],author_confirmed=True)
    drafts=c['publication'].preparations.drafts
    edited=drafts.update(g['draft']['id'],text=g['draft']['text']+'\n\n作者补充：林澈记住了门后低语的次序。',expected_revision=1)
    route,actual=author_commit(c,edited)
    assert actual['publication_verified'] and load_verified_reviewed_identity(c['root'])['history']['value']['last_transition']['acceptance_kind']=='author'
    assert g['gc'].read_text()=='1' and not g['ec'].exists()
    with pytest.raises(BatchControlError):cold.command(project_id=c['project'].id,batch_id=g['batch_id'],action='resume',
        idempotency_key='cannot-skip-author-readback',expected_control_sha256=view['control']['record_sha256'],author_confirmed=True)
    finish=ReviewedBatchTakeoverService(cold).complete(project_id=c['project'].id,batch_id=g['batch_id'],route=route,
        expected_control_sha256=view['control']['record_sha256'],author_confirmed=True)
    assert finish['control']['progress']['chapter']==3 and finish['control']['progress']['status']=='paused'
    assert finish['result']['schema_version']=='reviewed_author_readback_v1' and not cold.handles
    assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0
    resumed,handle=cold.command(project_id=c['project'].id,batch_id=g['batch_id'],action='resume',idempotency_key='explicit-after-author-chapter',
        expected_control_sha256=finish['control']['control']['record_sha256'],author_confirmed=True)
    duplicate=ReviewedBatchTakeoverService(cold)
    assert duplicate.takeover(project_id=c['project'].id,batch_id=g['batch_id'],draft_id=g['draft']['id'],
        expected_control_sha256=g['view']['control']['record_sha256'],author_confirmed=True)['control']==resumed
    assert duplicate.complete(project_id=c['project'].id,batch_id=g['batch_id'],route=route,
        expected_control_sha256=view['control']['record_sha256'],author_confirmed=True)['control']==resumed
    assert cold.handles[g['batch_id']] is handle and drafts.get(edited.id)==edited
    continuation,gc,ec=driver(c,cold)
    result=continuation.run(handle,expected_control_sha256=resumed['control']['record_sha256'])
    assert result['control']['progress']['status']=='completed' and gc.read_text()==ec.read_text()=='1'
    assert drafts.get(edited.id)==edited and g['gc'].read_text()=='1'
    history=load_verified_reviewed_identity(c['root'])['history']['value']
    assert history['committed_through']==3 and history['current_state']['numeric_counters']['alert_level']['current_value']==4
    assert history['last_transition']['acceptance_kind']=='policy'
    with c['db'].connection() as db:
        assert db.execute('SELECT COUNT(*) FROM chapter_fact_reviews').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM chapter_publication_index').fetchone()[0]==2


def test_takeover_requires_explicit_confirmation_pause_and_current_version_without_overwriting_draft(batch_case):
    c=batch_case;g=generated(c);service=ReviewedBatchTakeoverService(g['controls'])
    route=dict(project_id=c['project'].id,batch_id=g['batch_id'],draft_id=g['draft']['id'],expected_control_sha256=g['view']['control']['record_sha256'])
    with pytest.raises(BatchControlError):service.takeover(**route,author_confirmed=False)
    with pytest.raises(BatchControlPaused):service.takeover(**route,author_confirmed=True)
    paused_view=g['controls'].command(project_id=c['project'].id,batch_id=g['batch_id'],action='pause',idempotency_key='author-pause',
        expected_control_sha256=g['view']['control']['record_sha256'],author_confirmed=True)
    with pytest.raises(BatchControlPaused):service.takeover(**route,author_confirmed=True)
    assert c['publication'].preparations.drafts.get(g['draft']['id']).to_dict()==g['draft']
    assert g['gc'].read_text()=='1' and not g['ec'].exists()
    assert g['controls'].get(project_id=c['project'].id,batch_id=g['batch_id'])==paused_view


def test_author_takes_failed_repair_draft_and_continues_without_resetting_cycle_or_overwriting_initial_draft(batch_case):
    from tests.test_reviewed_batch_controls import started
    from tests.test_reviewed_policy_repair_flow import build,saved,good
    c=batch_case;controls,batch,view,handle=started(c);automatic,gc,ec=build(c,controls,repair_fails=True)
    failed=automatic.run(handle,expected_control_sha256=view['control']['record_sha256'])
    assert failed['control']['progress']['stage']=='repair_quality' and failed['control']['progress']['status']=='paused'
    budget=ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=2);claim=budget.store.repair_cycle_claim()
    _,initial=saved(c,batch,'generation');_,repair=saved(c,batch,'repair_generation');drafts=c['publication'].preparations.drafts
    initial_draft=drafts.get(initial['draft_id']);repair_draft=drafts.get(repair['draft_id'])
    service=ReviewedBatchTakeoverService(controls)
    take=service.takeover(project_id=c['project'].id,batch_id=batch,draft_id=repair_draft.id,
        expected_control_sha256=failed['control']['control']['record_sha256'],author_confirmed=True)
    edited=drafts.update(repair_draft.id,text=good(c['root'],2),expected_revision=repair_draft.revision)
    route,_=author_commit(c,edited)
    finish=service.complete(project_id=c['project'].id,batch_id=batch,route=route,
        expected_control_sha256=take['control']['control']['record_sha256'],author_confirmed=True)
    assert gc.read_text()==ec.read_text()=='2' and budget.store.repair_cycle_claim()==claim
    assert drafts.get(initial_draft.id)==initial_draft and drafts.get(edited.id)==edited
    assert load_verified_reviewed_identity(c['root'])['history']['value']['last_transition']['acceptance_kind']=='author'
    resumed,new_handle=controls.command(project_id=c['project'].id,batch_id=batch,action='resume',idempotency_key='explicit-after-failed-repair-author',
        expected_control_sha256=finish['control']['control']['record_sha256'],author_confirmed=True)
    continuation,_,_=driver(c,controls);result=continuation.run(new_handle,expected_control_sha256=resumed['control']['record_sha256'])
    assert result['control']['progress']['status']=='completed' and budget.store.repair_cycle_claim()==claim
    assert gc.read_text()==ec.read_text()=='2' and drafts.get(initial_draft.id)==initial_draft and drafts.get(edited.id)==edited


def test_author_completes_last_chapter_without_starting_another_or_reissuing_permit(batch_case):
    c=batch_case;c['route']['end_chapter']=2;g=paused(c);service=ReviewedBatchTakeoverService(g['controls'])
    take=service.takeover(project_id=c['project'].id,batch_id=g['batch_id'],draft_id=g['draft']['id'],
        expected_control_sha256=g['view']['control']['record_sha256'],author_confirmed=True)
    route,_=author_commit(c,c['publication'].preparations.drafts.get(g['draft']['id']))
    request=dict(project_id=c['project'].id,batch_id=g['batch_id'],route=route,
        expected_control_sha256=take['control']['control']['record_sha256'],author_confirmed=True)
    result=service.complete(**request)
    assert result['control']['progress']['status']=='completed' and result['result']['next_context'] is None
    assert not result['control']['explicit_resume_required'] and not g['controls'].handles
    assert service.complete(**request)==result and not g['controls'].handles
    with pytest.raises(BatchControlError):g['controls'].command(project_id=c['project'].id,batch_id=g['batch_id'],action='resume',
        idempotency_key='cannot-resume-finished-author-batch',expected_control_sha256=result['control']['control']['record_sha256'],author_confirmed=True)
    assert g['gc'].read_text()=='1' and not g['ec'].exists() and not (c['root']/'.novelagent/runtime/budgets/c3').exists()


@pytest.mark.parametrize('wrong',['request','missing_request_evidence','damaged_formal_prose','different_candidate'])
def test_author_continuation_requires_exact_selected_draft_and_actual_intact_publication(batch_case,wrong):
    c=batch_case;g=paused(c);service=ReviewedBatchTakeoverService(g['controls'])
    take=service.takeover(project_id=c['project'].id,batch_id=g['batch_id'],draft_id=g['draft']['id'],
        expected_control_sha256=g['view']['control']['record_sha256'],author_confirmed=True)
    drafts=c['publication'].preparations.drafts;selected=drafts.get(g['draft']['id'])
    if wrong=='different_candidate':
        from tests.test_reviewed_candidate_context import author_confirmation
        from core.engine.reviewed_candidate import ReviewedChapterCandidateExecutor
        from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
        from tests.test_workbench_chapter_candidates import _fixed_provenance
        executor=ReviewedChapterCandidateExecutor(story_project_root=c['root'],generator=lambda _:selected.text,
            execution_kind='deterministic',provenance_factory=_fixed_provenance)
        confirmation=author_confirmation(c['publication'].projects,c['project'])
        actual=executor.execute(chapter_index=2,confirmation=confirmation,idempotency_key='different-real-author-candidate')
        selected=drafts.create_from_candidate(project_id=c['project'].id,chapter=2,candidate_id=actual['candidate_id'],
            source_sha256=actual['prose']['sha256'],source_artifact_ref=actual['prose']['artifact_ref'],text=selected.text)
    route,actual=author_commit(c,selected);restore=[]
    assert actual['publication_verified']
    assert load_verified_reviewed_identity(c['root'])['history']['value']['last_transition']['acceptance_kind']=='author'
    if wrong=='different_candidate':assert selected.id!=g['draft']['id']
    if wrong=='request':route={**route,'idempotency_key':'different-original-author-request'}
    elif wrong=='missing_request_evidence':
        path=c['root']/'.novelagent/runtime/reviewed/requests'/(actual['run_id']+'.json');restore.append((path,path.read_bytes()));path.unlink()
    elif wrong=='damaged_formal_prose':
        path=canonical_prose_path(c['root'],2);restore.append((path,path.read_bytes()));path.write_bytes(restore[0][1]+b'\nsynthetic damage')
    try:
        with pytest.raises(BatchControlError):service.complete(project_id=c['project'].id,batch_id=g['batch_id'],route=route,
            expected_control_sha256=take['control']['control']['record_sha256'],author_confirmed=True)
        assert not g['controls'].handles
        assert g['controls'].get(project_id=c['project'].id,batch_id=g['batch_id'])['control']==take['control']['control']
        assert g['gc'].read_text()=='1' and not g['ec'].exists()
        assert ChapterExecutionBudget(c['root'],book_id=c['project'].book_id,chapter=3).store.report()['charged']['calls']==0
    finally:
        for path,original in restore:path.write_bytes(original)
