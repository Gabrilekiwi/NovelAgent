"""A real original V2 registration can restore its pre-upgrade archive."""
from pathlib import Path
import json
import subprocess
import sys
import uuid

from fastapi.testclient import TestClient
import pytest

from tests.test_legacy_restore_identity import legacy
from tests.test_legacy_registration import registration_case,contents
from tests.test_registered_reviewed_upgrade import register,preparation
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive import capture
from tests.test_backup_restore_materialization import prepare
from tests.test_backup_restore_database import logical
from core.engine.reviewed_upgrade import prepare_reviewed_upgrade_transaction
from workbench.backup_files import inventory_tree
from workbench.backup_restore_materialization import materialize_archived_restore
from workbench.backup_restore_roots import bind_restored_roots
from workbench.backup_restore_database import prepare_restored_workbench
from workbench.restore_activation import RestoreActivation
from workbench.book_ownership import BookOwnership,WITNESS_PATH,_record
from workbench.app import create_app


@pytest.fixture
def tmp_path():
    root=Path(__file__).resolve().parents[1]/'.tmp'/('原登记 升级归档-'+uuid.uuid4().hex[:8])
    root.mkdir()
    return root


def old_package(c):
    register(c)
    return Path(capture({'service':c['app'].state.backups,'op':c['op'],'project':c['project']})['path'])


def copied(c,package,target):
    plan=prepare({'project':c['project']},package,target,c['scratch'])
    materialize_archived_restore(package,plan=plan,temporary_root=c['scratch'])
    bind_restored_roots(package,destination=target,temporary_root=c['scratch'])
    prepare_restored_workbench(package,destination=target,temporary_root=c['scratch'])


def activate(c,service,package,intent):
    handoff=intent['handoff_id']
    service.begin(package,destination=Path(intent['target']),expected_preview_sha256=intent['intent_sha256'],temporary_root=c['scratch'])
    assert service.capture(handoff_id=handoff)['settled']
    for method in (service.prepare_budget,service.prepare_jobs,service.prepare_executions):
        method(package,handoff_id=handoff,temporary_root=c['scratch'])
    assert service.activate(package,handoff_id=handoff,temporary_root=c['scratch'])['target_writable']


def read_twice(c,target,version):
    assert json.loads((target/'book/.novelagent/project.json').read_bytes())['schema_version']==version
    for _ in range(2):
        app=create_app(database_path=target/'workbench.sqlite',start_worker=True,auto_import_active=True,
            chapter_publication_machine_store=c['machine'])
        with TestClient(app) as client:
            assert app.state.supervisor._thread is None
            response=client.get('/api/projects/'+c['project'].id+'/chapters/2')
            assert response.status_code==200,response.text
            assert response.json()['retained_author_draft']['text']==c['draft'].text


@pytest.mark.parametrize('registration_case',['1.0','2.0'],indirect=True)
def test_actual_original_registration_restores_its_pre_upgrade_archive(registration_case,tmp_path):
    c=registration_case;package=old_package(c);archive=contents(package)
    version=json.loads((c['root']/'.novelagent/project.json').read_bytes())['schema_version']
    tx=prepare_reviewed_upgrade_transaction(preparation=preparation(c));assert tx.commit()['committed']
    c['app'].state.maintenance.finish(c['op'])
    book=inventory_tree(c['root']);sql=logical(c['app'].state.database.path)
    witness=(c['root']/WITNESS_PATH).read_bytes();target=tmp_path/'恢复 升级前'
    copied(c,package,target)
    service=RestoreActivation(c['project'].book_id,machine_store=c['machine'])
    intent=service.preview(package,destination=target,temporary_root=c['scratch'])
    assert intent['schema_version']=='machine_restore_handoff_v5'
    assert intent['archived_source_record']['generation']==1 and intent['source_record']['generation']==2
    activate(c,service,package,intent)
    read_twice(c,target,version)
    assert inventory_tree(c['root'])==book and logical(c['app'].state.database.path)==sql
    assert (c['root']/WITNESS_PATH).read_bytes()==witness and contents(package)==archive


def controls(owner):
    return {p.name:p.read_bytes() for p in owner.directory.glob('*.json') if p.name!='workbench-instance.json'}


def test_pre_and_post_upgrade_actual_rollbacks_connect_same_original_archive(registration_case,tmp_path):
    c=registration_case;package=old_package(c);archive=contents(package)
    service=RestoreActivation(c['project'].book_id,machine_store=c['machine'])
    target=tmp_path/'升级前 回退';copied(c,package,target)
    first=service.preview(package,destination=target,temporary_root=c['scratch'])
    assert first['schema_version']=='machine_restore_handoff_v1'
    service.begin(package,destination=target,expected_preview_sha256=first['intent_sha256'],temporary_root=c['scratch'])
    assert service.rollback(handoff_id=first['handoff_id'])['rolled_back']
    tx=prepare_reviewed_upgrade_transaction(preparation=preparation(c));assert tx.commit()['committed']
    c['app'].state.maintenance.finish(c['op'])
    book=inventory_tree(c['root']);sql=logical(c['app'].state.database.path)
    for attempt in range(2):
        target=tmp_path/('升级后 恢复'+str(attempt));copied(c,package,target)
        intent=service.preview(package,destination=target,temporary_root=c['scratch'])
        proof=intent['source_identity_upgrade']
        assert len(proof['pre_upgrade_rollbacks'])==1
        assert len(proof['post_upgrade_rollbacks'])==attempt
        assert intent['source_record']['generation']==4+2*attempt
        if attempt==0:
            service.begin(package,destination=target,expected_preview_sha256=intent['intent_sha256'],temporary_root=c['scratch'])
            assert service.rollback(handoff_id=intent['handoff_id'])['rolled_back']
            owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
            with owner.locked(),c['app'].state.database.transaction() as connection:
                assert owner.require(root=c['root'],database=c['app'].state.database,
                    project_id=c['project'].id,connection=connection)['generation']==6
            continue
        activate(c,service,package,intent);read_twice(c,target,'1.0')
    assert inventory_tree(c['root'])==book and logical(c['app'].state.database.path)==sql
    assert contents(package)==archive


def child_exit(c,service,package,intent,event,action):
    code=r'''
import os,sys
from pathlib import Path
from workbench.restore_activation import RestoreActivation
import workbench.book_ownership as b
b.default_machine_store=lambda:Path(sys.argv[1])
def fault(name,value):
    if name==sys.argv[6]:os._exit(95)
s=RestoreActivation(sys.argv[2],machine_store=Path(sys.argv[1]),fault_injector=fault)
if sys.argv[7]=='begin':s.begin(Path(sys.argv[3]),destination=Path(sys.argv[4]),expected_preview_sha256=sys.argv[5],temporary_root=Path(sys.argv[9]))
else:s.rollback(handoff_id=sys.argv[8])
raise AssertionError('requested real exit not reached')
'''
    child=subprocess.run([sys.executable,'-X','utf8','-c',code,str(c['machine']),c['project'].book_id,
        str(package),intent['target'],intent['intent_sha256'],event,action,intent['handoff_id'],str(c['scratch'])],
        cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=120)
    assert child.returncode==95,child.stdout+child.stderr


def test_v5_original_files_are_required_before_and_after_four_real_exits(registration_case,tmp_path):
    from workbench.registered_identity_upgrade import _name
    from workbench.restore_rollback_ancestry import rollback_receipt_name
    from workbench.restore_handoff import RestoreHandoffError
    c=registration_case;package=old_package(c);archive=contents(package)
    p=preparation(c);tx=prepare_reviewed_upgrade_transaction(preparation=p);assert tx.commit()['committed']
    c['app'].state.maintenance.finish(c['op'])
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    service=RestoreActivation(c['project'].book_id,machine_store=c['machine'])
    target=tmp_path/'原交接 续接';copied(c,package,target)
    intent=service.preview(package,destination=target,temporary_root=c['scratch'])
    assert intent['schema_version']=='machine_restore_handoff_v5'
    paths=[owner.directory/_name(p['binding']['new_identity_sha256'],suffix) for suffix in ('intent','done')]
    paths += [c['root']/'.novelagent/runtime/reviewed/receipts/registered-upgrade-1.json']
    for path in paths:
        raw=path.read_bytes();path.unlink();machine=controls(owner);book=contents(c['root']);sql=logical(c['app'].state.database.path)
        try:
            with pytest.raises(RestoreHandoffError):
                service.preview(package,destination=target,temporary_root=c['scratch'])
            assert controls(owner)==machine and contents(c['root'])==book
            assert logical(c['app'].state.database.path)==sql and not path.exists()
        finally:path.write_bytes(raw)
    child_exit(c,service,package,intent,'after_handoff_intent','begin')
    handoff=intent['handoff_id']
    assert service.resume(package,handoff_id=handoff,temporary_root=c['scratch'])['source_revoked']
    path=paths[0];raw=path.read_bytes();path.unlink();machine=controls(owner);book=contents(c['root'])
    target_book=contents(target/'book');target_sql=logical(target/'workbench.sqlite')
    try:
        for operation in (lambda:service.resume(package,handoff_id=handoff,temporary_root=c['scratch']),
                lambda:service.capture(handoff_id=handoff),lambda:service.rollback(handoff_id=handoff)):
            with pytest.raises(RestoreHandoffError):operation()
        assert controls(owner)==machine and contents(c['root'])==book and contents(target/'book')==target_book
        assert logical(target/'workbench.sqlite')==target_sql and not path.exists()
    finally:path.write_bytes(raw)
    child_exit(c,service,package,intent,'after_registration_rollback_pending','rollback')
    assert _record(owner.directory,owner.book_id)['generation']==4
    assert _record(owner.directory,owner.book_id)['state']=='reactivating'
    from core.autonomy.common import atomic_append_json
    for event in ('after_registration_rollback_receipt','after_rollback_completion'):
        with pytest.raises(ValueError):
            atomic_append_json(c['root']/'.novelagent/runtime/forbidden-pending-rollback.json',{'forbidden':True})
        child_exit(c,service,package,intent,event,'rollback')
    assert service.rollback(handoff_id=handoff)['rolled_back']
    record=_record(owner.directory,owner.book_id)
    path=owner.directory/rollback_receipt_name(record);raw=path.read_bytes();path.unlink();machine=controls(owner)
    try:
        with pytest.raises(RestoreHandoffError):service.rollback(handoff_id=handoff)
        assert controls(owner)==machine and not path.exists()
    finally:path.write_bytes(raw)
    target=tmp_path/'再次 恢复';copied(c,package,target)
    next_intent=service.preview(package,destination=target,temporary_root=c['scratch'])
    assert len(next_intent['source_identity_upgrade']['post_upgrade_rollbacks'])==1
    activate(c,service,package,next_intent);read_twice(c,target,'1.0')
    assert contents(package)==archive


def test_http_original_session_resumes_v5_pending_rollback_after_real_app_restart(registration_case,tmp_path,monkeypatch):
    import threading
    from tests.test_recovery_sessions import start,advance,step
    from tests.test_workbench_app import _unlock
    from workbench.jobs import JobSupervisor
    from workbench.maintenance import MaintenanceBlocked
    c=registration_case;package=old_package(c)
    tx=prepare_reviewed_upgrade_transaction(preparation=preparation(c));assert tx.commit()['committed']
    c['app'].state.maintenance.finish(c['op'])
    book=contents(c['root']);sql=logical(c['app'].state.database.path);archive=contents(package)
    session=advance(c['client'],start(c['client'],{**c,'package':package},tmp_path/'网页 恢复'),'source_revoked')
    original=RestoreActivation._fault;enabled=[True]
    def interrupted(self,phase,value):
        if enabled[0] and phase=='after_registration_rollback_pending':
            raise RuntimeError('actual pending rollback response interrupted')
        return original(self,phase,value)
    monkeypatch.setattr(RestoreActivation,'_fault',interrupted)
    rejected=step(c['client'],session,action='rollback')
    assert rejected.status_code==409,rejected.text
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    assert _record(owner.directory,owner.book_id)['state']=='reactivating'
    view=c['client'].get('/api/recovery-sessions/'+session['session_id'])
    assert view.status_code==200,view.text
    assert view.json()['pending_action']=='rollback'
    enabled[0]=False
    c['client'].__exit__(None,None,None);c['client_closed']=True
    # Original controllers retain a listening worker during maintenance. Exercise
    # that real loop, while making any admission to recovery/execution fatal.
    unexpected=[]
    def forbidden_work(*args,**kwargs):
        unexpected.append('startup recovery or execution was admitted')
        return c['forbidden'](*args,**kwargs)
    monkeypatch.setattr(JobSupervisor,'_start_worker',c['actual_start_worker'])
    monkeypatch.setattr(JobSupervisor,'_recover_startup_once',forbidden_work)
    monkeypatch.setattr(JobSupervisor,'_run_one_job',forbidden_work)
    app=create_app(database_path=c['app'].state.database.path,start_worker=True,auto_import_active=True,
        runtime_root=c['app'].state.supervisor.runtime_root,
        allowed_workspaces=[c['root'].parent],chapter_publication_machine_store=c['machine'])
    blocked=threading.Event();admit=app.state.maintenance.admit
    def observe_admission(kind):
        try:return admit(kind)
        except MaintenanceBlocked:
            if kind=='worker' and threading.current_thread().name=='novelagent-workbench-worker':
                blocked.set()
            raise
    monkeypatch.setattr(app.state.maintenance,'admit',observe_admission)
    with TestClient(app) as client:
        _unlock(client)
        assert blocked.wait(3), 'real worker did not reach durable maintenance admission'
        assert app.state.supervisor._thread.is_alive()
        assert app.state.supervisor._startup_recovery_pending and not unexpected
        assert client.get('/api/maintenance').json()['state']=='interrupted'
        session=client.get('/api/recovery-sessions/'+session['session_id']).json()
        assert session['pending_action']=='rollback'
        assert client.post('/api/maintenance/resume',json={'operation_id':session['operation_id']}).status_code==200
        assert step(client,session,action='rollback',resume=False).status_code==409
        completed=step(client,session,action='rollback',resume=True)
        assert completed.status_code==200,completed.text
        assert completed.json()['phase']=='rolled_back' and completed.json()['pending_action'] is None
        assert _record(owner.directory,owner.book_id)['state']=='active'
        assert client.get('/api/projects/'+c['project'].id+'/chapters/2').json()['retained_author_draft']['text']==c['draft'].text
        assert app.state.supervisor._thread.is_alive()
        assert app.state.supervisor._startup_recovery_pending and not unexpected
    assert not app.state.supervisor._thread.is_alive() and not unexpected
    assert contents(c['root'])==book and logical(c['app'].state.database.path)==sql
    assert contents(package)==archive
