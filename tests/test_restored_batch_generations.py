"""An advanced restored batch survives a second actual archive/activation."""
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_batch_api import web,command
from tests.test_reviewed_batch_tasks import joined
from tests.test_reviewed_batch_driver import driver,calls
from tests.test_registration_upgrade_archive import copied,activate
import tests.test_final_batch_restore as original
from workbench.app import create_app
from workbench.restore_activation import RestoreActivation
from workbench.book_ownership import BookOwnership
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.chapter_execution_budget import ChapterExecutionBudget


def test_advanced_restored_batch_second_archive_preserves_actual_author_parent_and_continues_next(web,monkeypatch):
    c,_,_,_=web;saved=[]
    class PauseBeforeFour(Exception):pass
    def defer(client,path,view,action,key):
        if key=='restored-explicit-next-chapter':
            saved.append((path,view));raise PauseBeforeFour()
        return command(client,path,view,action,key)
    with monkeypatch.context() as patch:
        patch.setattr(original,'command',defer)
        with pytest.raises(PauseBeforeFour):original.exercise_final_restore(web)
    assert len(saved)==1
    path,first_view=saved[0];batch=path.rsplit('/',1)[1]
    first=c['root'].parent/'restored';first_root=first/'book'
    assert first_view['control']['progress']['chapter']==4 and first_view['control']['progress']['status']=='paused'
    assert load_verified_reviewed_identity(first_root)['history']['value']['committed_through']==3
    # exercise_final_restore now performs one explicitly authorized author fact
    # extraction after takeover, in addition to chapter 3's original generation.
    assert [calls({'root':first_root,'project':c['project']},n) for n in (2,3,4)]==[2,2,0]
    effective=first_view['control']['control']
    app=create_app(database_path=first/'workbench.sqlite',start_worker=True,auto_import_active=True,
        chapter_publication_machine_store=c['machine'])
    with TestClient(app) as client:
        assert client.get('/reviewed-batches').status_code==200
        before=client.get(path);assert before.status_code==200,before.text
        assert before.json()['control']['control']==effective
        operation=original.post(client,'/api/maintenance/begin',{})['operation_id']
        base='/api/projects/'+c['project'].id
        try:
            preview=original.post(client,base+'/backups/preview',{'operation_id':operation})
            assert preview['scope_checks_passed'],preview['blockers']
            package=Path(original.post(client,base+'/backups',{'operation_id':operation,
                'expected_preview_sha256':preview['preview_sha256'],'archive_purpose':'diagnostic'})['path'])
        finally:original.post(client,'/api/maintenance/finish',{'operation_id':operation})
    print('advanced restored author3 archived again',str(package),flush=True)
    scratch=c['root'].parent/'second-restore-scratch';scratch.mkdir()
    target=c['root'].parent/'restored-again';context={**c,'scratch':scratch}
    copied(context,package,target)
    activation=RestoreActivation(c['project'].book_id,machine_store=c['machine'])
    intent=activation.preview(package,destination=target,temporary_root=scratch)
    activate(context,activation,package,intent)
    root=target/'book'
    app=create_app(database_path=target/'workbench.sqlite',start_worker=True,auto_import_active=True,
        chapter_publication_machine_store=c['machine'])
    with TestClient(app) as client:
        assert client.get('/reviewed-batches').status_code==200
        assert app.state.supervisor._thread is None
        response=client.get(path)
        print('second generation historical batch response',response.status_code,response.text[:500],flush=True)
        assert response.status_code==200,response.text
        view=response.json();assert view['history_only'] and not view['execution_live']
        assert view['control']['control']==effective
        assert view['control']['progress']['chapter']==4
        # The new qualified copy must retain the predecessor's independent
        # consent and completed control, including its author chapter finish.
        previous=list((root/'.novelagent/runtime/reviewed/continuations').iterdir())
        assert len(previous)==1
        previous=previous[0]
        owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
        for proof in (owner.directory/('brc-'+previous.name+'.json'),owner.directory/('brcc-'+previous.name+'.json')):
            raw=proof.read_bytes()
            try:
                proof.unlink()
                denied=client.get(path);assert denied.status_code==409,denied.text
                denied=client.post(path+'/restored-continuation/preview',json={});assert denied.status_code==409,denied.text
                assert app.state.reviewed_batches.tasks._active is None and not app.state.reviewed_batches.controls.handles
            finally:proof.write_bytes(raw)
        paths=(previous/'control.json',owner.directory/('brcc-'+previous.name+'.json'))
        retained={p:p.read_bytes() for p in paths}
        try:
            for p in paths:p.write_bytes((previous/'anchor.json').read_bytes())
            denied=client.get(path);assert denied.status_code==409,denied.text
            assert app.state.reviewed_batches.tasks._active is None and not app.state.reviewed_batches.controls.handles
        finally:
            for p,raw in retained.items():p.write_bytes(raw)
        assert client.get(path).status_code==200
        preview=original.post(client,path+'/restored-continuation/preview',{})
        assert preview['can_prepare'],preview['blockers']
        assert preview['original_progress']['chapter']==4
        original.post(client,path+'/restored-continuation',{'author_confirmed':True,'expected_preview_sha256':preview['record_sha256']})
        current=client.get(path);assert current.status_code==200,current.text
        assert not current.json()['execution_live']
        for n,expected in [(2,2),(3,2),(4,0)]:
            old=ChapterExecutionBudget(first_root,book_id=c['project'].book_id,chapter=n)
            new=ChapterExecutionBudget(root,book_id=c['project'].book_id,chapter=n)
            assert old.store.frozen_budget()==new.store.frozen_budget()
            assert old.store.repair_cycle_claim()==new.store.repair_cycle_claim()
            assert calls({'root':root,'project':c['project']},n)==expected
        publication=app.state.chapter_publication
        run_context={**c,'root':root,'publication':publication,'project':publication.projects.get(c['project'].id)}
        app.state.reviewed_batches.tasks.driver_factory=lambda controls:driver(run_context,controls)[0]
        command(client,path,current.json(),'resume','explicit-second-generation-next4')
        joined(app.state.reviewed_batches.tasks)
        final=client.get(path);assert final.status_code==200,final.text
        assert final.json()['control']['progress']['status']=='completed'
        assert load_verified_reviewed_identity(root)['history']['value']['committed_through']==4
        assert [calls({'root':root,'project':c['project']},n) for n in (2,3,4)]==[2,2,2]
    assert load_verified_reviewed_identity(first_root)['history']['value']['committed_through']==3
