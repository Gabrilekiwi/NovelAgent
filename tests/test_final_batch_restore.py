"""Final automatic flow must survive a real copy, activation and explicit continuation."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from tests.test_reviewed_upgrade import case
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_batch_api import web,grant,command
from tests.test_reviewed_batch_driver import driver,calls
from tests.test_reviewed_batch_tasks import joined
from tests.test_registration_upgrade_archive import copied,activate
from tests.test_backup_restore_database import logical
from workbench.app import create_app
from workbench.backup_files import inventory_tree
from workbench.restore_activation import RestoreActivation


def author_extraction_factory(counter):
    """An explicit author extraction uses the original frozen chapter budget."""
    from api.contracts import ModelResponse
    from api.budget_endpoint import endpoint_fingerprint
    from core.chapter_execution_budget import ChapterExecutionBudget
    from core.engine.chapter_fact_extraction import ChapterFactExtractionExecutor
    from core.model_call_runtime import current_model_call_runtime
    from tests.test_chapter_budget_runtime import args, response
    from tests.test_reviewed_policy_extractions import proposal
    from tests.test_workbench_chapter_candidates import _fixed_provenance
    def create(root):
        def provider(messages, call_id, unused):
            def operation():
                counter.write_text(str(int(counter.read_text())+1 if counter.exists() else 1),encoding='utf-8')
                base=response()
                return ModelResponse(json.dumps(proposal(),ensure_ascii=False),actual_model=base.actual_model,
                    endpoint_type=base.endpoint_type,usage=base.usage)
            return current_model_call_runtime().execute_attempt(**args(call_id=call_id,stage='chapter_fact_extraction',
                endpoint_fingerprint=endpoint_fingerprint('openai','https://api.openai.com/v1'),operation=operation)).text
        return ChapterFactExtractionExecutor(story_project_root=root,provider=provider,execution_kind='model',
            provenance_factory=_fixed_provenance,execution_budget_factory=ChapterExecutionBudget)
    return create


def post(client,path,body):
    response=client.post(path,json=body)
    assert response.status_code==200,response.text
    return response.json()


def exercise_final_restore(web,*,preparation_only=False):
    c,app,client,batches=web
    outline=c['root']/'大纲/细纲_第003章.md'
    (c['root']/'大纲/细纲_第004章.md').write_text(outline.read_text(encoding='utf-8').replace('第3章','第4章').replace('第003章','第004章'),encoding='utf-8')
    c['route']['end_chapter']=4
    path,view=grant(c,client);batch=path.rsplit('/',1)[1]
    def pause(event,step):
        if step['chapter']==3 and step['stage']=='generation':
            current=batches.controls.get(project_id=c['project'].id,batch_id=batch)
            batches.controls.command(project_id=c['project'].id,batch_id=batch,action='pause',
                idempotency_key='restore-source-pause',expected_control_sha256=current['control']['record_sha256'],author_confirmed=True)
    batches.tasks.driver_factory=lambda controls:driver(c,controls,fault=pause)[0]
    command(client,path,view,'start','restore-source-start');joined(batches.tasks)
    response=client.get(path);assert response.status_code==200,response.text
    source=response.json();assert source['control']['progress']['chapter']==3 and source['control']['progress']['status']=='paused'
    assert calls(c,2)==2 and calls(c,3)==1 and calls(c,4)==0
    draft_id=source['drafts'][0]['draft_id'];draft=app.state.chapter_publication.preparations.drafts.get(draft_id)
    edited=app.state.chapter_publication.preparations.drafts.update(draft.id,text=draft.text+'\n作者尚未审核的保留修改。',expected_revision=draft.revision)
    base='/api/projects/'+c['project'].id
    op=post(client,'/api/maintenance/begin',{})['operation_id']
    try:
        preview=post(client,base+'/backups/preview',{'operation_id':op})
        assert preview['scope_checks_passed'],preview['blockers']
        saved=post(client,base+'/backups',{'operation_id':op,'expected_preview_sha256':preview['preview_sha256'],'archive_purpose':'diagnostic'})
        package=Path(saved['path'])
    finally:post(client,'/api/maintenance/finish',{'operation_id':op})
    print('final policy source archived',str(package),flush=True)
    source_tree=inventory_tree(c['root']);source_sql=logical(c['db'].path);archive_tree=inventory_tree(package)
    scratch=c['root'].parent/'restore-scratch';scratch.mkdir();target=c['root'].parent/'restored'
    restore_case={**c,'scratch':scratch}
    copied(restore_case,package,target)
    print('final policy copy materialized and database bound',flush=True)
    activation=RestoreActivation(c['project'].book_id,machine_store=c['machine'])
    intent=activation.preview(package,destination=target,temporary_root=scratch)
    activate(restore_case,activation,package,intent)
    print('final policy copy explicitly activated',flush=True)
    assert inventory_tree(c['root'])==source_tree and logical(c['db'].path)==source_sql
    assert inventory_tree(package)==archive_tree and not list(scratch.iterdir())
    restored=create_app(database_path=target/'workbench.sqlite',start_worker=True,auto_import_active=True,
        chapter_publication_machine_store=c['machine'])
    with TestClient(restored) as restored_client:
        restored_client.get('/reviewed-batches')
        assert restored.state.supervisor._thread is None
        retained=restored.state.chapter_publication.preparations.drafts.get(edited.id)
        assert retained==edited
        read=restored_client.get(path);assert read.status_code==200,read.text
        current=read.json();assert not current['execution_live'] and current['explicit_resume_required']
        assert current['control']['progress']['chapter']==3
        assert current['history_only'] and current['continuation_required']
        assert restored_client.get(base+'/reviewed-batches').status_code==200
        original_grants={p.name:p.read_bytes() for p in (target/'book/.novelagent/runtime/reviewed/batches').glob('*.json')}
        original_controls={p.name:p.read_bytes() for p in (target/'book/.novelagent/runtime/reviewed/batch_controls').glob('*.json')}
        preview=post(restored_client,path+'/restored-continuation/preview',{})
        assert preview['can_prepare'],preview['blockers']
        assert not preview['execution_permit'] and preview['original_progress']['chapter']==3
        assert restored_client.post(path+'/restored-continuation',json={'expected_preview_sha256':preview['record_sha256'],'author_confirmed':False}).status_code==409
        assert restored_client.post(path+'/restored-continuation',json={'expected_preview_sha256':'f'*64,'author_confirmed':True}).status_code==409
        prepared=post(restored_client,path+'/restored-continuation',{'expected_preview_sha256':preview['record_sha256'],'author_confirmed':True})
        assert not prepared['execution_permit'] and prepared['explicit_resume_required']
        assert post(restored_client,path+'/restored-continuation',{'expected_preview_sha256':preview['record_sha256'],'author_confirmed':True})==prepared
        assert restored.state.reviewed_batches.tasks._active is None and not restored.state.reviewed_batches.controls.handles
        assert {p.name:p.read_bytes() for p in (target/'book/.novelagent/runtime/reviewed/batches').glob('*.json')}==original_grants
        assert {p.name:p.read_bytes() for p in (target/'book/.novelagent/runtime/reviewed/batch_controls').glob('*.json')}==original_controls
        for chapter,expected_calls in [(2,2),(3,1),(4,0)]:
            context={'root':target/'book','project':c['project']}
            assert calls(context,chapter)==expected_calls
        assert restored.state.chapter_publication.preparations.drafts.get(edited.id)==edited
        print('restored batch historical view and explicit preparation verified without dispatch',flush=True)
        if preparation_only:return
        read=restored_client.get(path);assert read.status_code==200,read.text
        current=read.json();assert not current['history_only'] and not current['execution_live']
        # The restored grant may never silently validate an author's changed
        # prose. It must preserve the draft and provide an explicit takeover.
        takeover=post(restored_client,path+'/takeover',{'draft_id':draft_id,
            'expected_control_sha256':current['control']['control']['record_sha256'],'author_confirmed':True})
        assert takeover['control']['progress']['stage']=='author_review'
        from tests.test_reviewed_policy_repair_flow import good
        publication=restored.state.chapter_publication
        fixed=publication.preparations.drafts.update(edited.id,text=good(target/'book',3),expected_revision=edited.revision)
        # Continue using the real author's existing fact-review/publication
        # services; chapter helpers are adapted below after actual restoration.
        context={**c,'root':target/'book','publication':publication,'project':publication.projects.get(c['project'].id)}
        review_service=publication.preparations.fact_reviews
        # Use the same extraction endpoint as the author page. Directly creating
        # review rows misses restored-source checks at this real boundary.
        counter=target/'author-extraction-calls.txt'
        restored.state.chapter_fact_extractions.reviewed.executor_factory=author_extraction_factory(counter)
        extraction_path='/api/candidate-drafts/'+fixed.id+'/fact-extractions'
        extraction_body={'expected_draft_revision':fixed.revision,'expected_draft_sha256':fixed.sha256,
            'author_confirmed':True}
        def extract():
            return restored_client.post(extraction_path,json=extraction_body,headers={'Idempotency-Key':'restored-author-facts-3'})
        # A confirmed physical-root bridge must not admit changed content.
        outline=target/'book/大纲/细纲_第003章.md';original_outline=outline.read_bytes()
        outline.write_bytes(original_outline+'\n不属于原批次的章纲变化。'.encode('utf-8'))
        try:
            denied=extract();assert denied.status_code==409,denied.text
            assert not counter.exists() and calls(context,3)==1
        finally:outline.write_bytes(original_outline)
        extracted=extract();assert extracted.status_code==200,extracted.text
        extracted=extracted.json()['fact_extraction'];assert extracted['source_state']=='current' and extracted['status']=='completed'
        review=extracted['fact_review'];assert review['source']['extraction_id']==extracted['extraction_id']
        replay=extract();assert replay.status_code==200,replay.text
        assert replay.json()['fact_extraction']['extraction_id']==extracted['extraction_id'] and counter.read_text()=='1'
        assert calls(context,3)==2
        for fact in review['facts']:fact['review']['decision']='accepted'
        review=review_service.update(review['id'],facts=review['facts'],expected_revision=review['revision'])
        review=review_service.confirm(review['id'],expected_revision=review['revision'],expected_facts_sha256=review['facts_sha256'],
            expected_draft_revision=fixed.revision,expected_draft_sha256=fixed.sha256,author_confirmed=True)
        route=dict(project_id=c['project'].id,chapter=3,review_id=review['id'],expected_draft_revision=fixed.revision,
            expected_review_revision=review['revision'],expected_draft_sha256=fixed.sha256,expected_facts_sha256=review['facts_sha256'],idempotency_key='restored-author-3')
        result=publication.commit(**route);assert result['publication_verified']
        # The real browser returns to GET the batch before it can send the
        # completion command. The submitted draft's old context is now stale,
        # but that must not make historical reviews or batch status unreadable.
        after_commit=restored_client.get(path)
        assert after_commit.status_code==200,after_commit.text
        assert after_commit.json()['control']['progress']['stage']=='author_review'
        assert after_commit.json()['takeover']['takeover']['draft_at_takeover']['id']==fixed.id
        assert any(r['id']==review['id'] for r in after_commit.json()['author_reviews'])
        old_reviews=restored_client.get('/api/candidate-drafts/'+fixed.id+'/fact-reviews')
        assert old_reviews.status_code==200,old_reviews.text
        stale=review_service.get(review['id'])
        assert stale['state']=='stale' and stale['stored_state']=='confirmed'
        assert stale['stale_reason']=='authority_or_source_changed'
        assert extract().status_code==409
        assert counter.read_text(encoding='utf-8')=='1' and calls(context,3)==2
        body={'key':route['idempotency_key'],'body':{k:v for k,v in route.items() if k not in {'project_id','chapter','idempotency_key'}}}
        completed=post(restored_client,path+'/complete-author',{'chapter':3,'request':body,
            'expected_control_sha256':takeover['control']['control']['record_sha256'],'author_confirmed':True})
        assert completed['control']['progress']['chapter']==4 and completed['control']['progress']['status']=='paused'
        restored.state.reviewed_batches.tasks.driver_factory=lambda controls:driver(context,controls)[0]
        command(restored_client,path,completed,'resume','restored-explicit-next-chapter');joined(restored.state.reviewed_batches.tasks)
        final=restored_client.get(path);assert final.status_code==200,final.text
        assert final.json()['control']['progress']['status']=='completed'
        from core.story_project.reviewed_identity import load_verified_reviewed_identity
        assert load_verified_reviewed_identity(target/'book')['history']['value']['committed_through']==4


def test_restored_batch_history_and_explicit_preparation_preserve_original_evidence_without_dispatch(web):
    exercise_final_restore(web,preparation_only=True)


def test_final_policy_book_paused_author_draft_and_unused_frozen_budget_restore_then_continue(web):
    exercise_final_restore(web)
