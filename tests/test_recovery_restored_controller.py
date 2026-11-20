"""Second-generation recovery through the original restored HTTP controller."""
from contextlib import contextmanager
import json
from pathlib import Path
import shutil

from fastapi.testclient import TestClient
import pytest

from core.autonomy.common import atomic_append_json
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_restore_database import logical
from tests.test_recovery_sessions import controller, start, advance, step
from tests.test_restore_budget_preparation import make_financial_source
from workbench.app import create_app
from workbench.backup_files import inventory_tree
from workbench.database import WorkbenchDatabase
from workbench.instance import InstanceLease
from workbench.restore_handoff import RestoreHandoff, RestoreHandoffError, _signed
from workbench.restore_scope import load_restore_scope, RestoreScopeError


@pytest.fixture(scope='module')
def original():
    return make_financial_source(include_uncertain=False)


@contextmanager
def restored_client(c):
    def forbidden(*args,**kwargs):
        pytest.fail('restored controller invoked an executor')
    app=create_app(database_path=c['source']/'workbench.sqlite',start_worker=True,auto_import_active=True,
        chapter_publication_machine_store=c['machine'],chapter_candidate_executor_factory=forbidden,
        chapter_fact_extraction_executor_factory=forbidden,reviewed_chapter_candidate_executor_factory=forbidden,
        reviewed_chapter_fact_extraction_executor_factory=forbidden)
    with TestClient(app) as client:
        assert client.get('/').status_code==200
        assert app.state.supervisor._thread is None
        yield app,client


@pytest.fixture
def case(original,tmp_path,monkeypatch):
    machine=tmp_path/'machine'
    shutil.copytree(original['machine'],machine)
    monkeypatch.setattr('workbench.book_ownership.default_machine_store',lambda:machine)
    c={**original,'machine':machine,'source':tmp_path/'原恢复 副本','target':tmp_path/'第二代 恢复副本'}
    from workbench.jobs import JobSupervisor
    def forbidden(*args,**kwargs):pytest.fail('automatic worker/import/SDK is forbidden')
    monkeypatch.setattr(JobSupervisor,'_start_worker',forbidden)
    monkeypatch.setattr('workbench.app._import_active_project',forbidden)
    monkeypatch.setattr('openai.OpenAI',forbidden)
    monkeypatch.setattr('anthropic.Anthropic',forbidden)
    app=controller(c,tmp_path/'first-controller')
    with TestClient(app) as client:
        first=advance(client,start(client,c,c['source']),'active')
        assert client.post('/api/maintenance/finish',json={'operation_id':first['operation_id']}).status_code==200
    with restored_client(c) as (app,client):
        gate=client.post('/api/maintenance/begin',json={'purpose':'backup'})
        assert gate.status_code==200,gate.text
        operation=gate.json()['operation_id']
        preview=client.post('/api/projects/'+c['project'].id+'/backups/preview',json={'operation_id':operation})
        assert preview.status_code==200,preview.text
        captured=client.post('/api/projects/'+c['project'].id+'/backups',json={'operation_id':operation,
            'expected_preview_sha256':preview.json()['preview_sha256'],'archive_purpose':'diagnostic'})
        assert captured.status_code==200,captured.text
        assert client.post('/api/maintenance/finish',json={'operation_id':operation}).status_code==200
        c['rebackup']=Path(captured.json()['path'])
    c['source_book']=inventory_tree(c['source']/'book')
    c['source_sql']=logical(c['source']/'workbench.sqlite')
    return c


def begin_second(c,client):
    return start(client,{**c,'package':c['rebackup']},c['target'])


def denied_source(c,client):
    for path in ('/api/projects','/api/projects/'+c['project'].id+'/chapters/2',
            '/api/drafts/'+c['draft'].id,'/api/backups'):
        assert client.get(path).status_code==409,path
    assert client.post('/api/maintenance/begin',json={'purpose':'restore'}).status_code==409
    path=c['source']/'book/.novelagent/runtime/controller-must-not-write.json'
    with pytest.raises((RuntimeError,ValueError,OSError)):
        atomic_append_json(path,{'forbidden':True})
    assert not path.exists()
    with pytest.raises(RestoreScopeError):
        WorkbenchDatabase(c['source']/'workbench.sqlite',machine_store=c['machine'])


def unchanged(c):
    assert inventory_tree(c['source']/'book')==c['source_book']
    assert logical(c['source']/'workbench.sqlite')==c['source_sql']


def test_actual_restored_source_restarts_resumes_and_activates_second_copy(case):
    c=case
    with restored_client(c) as (app,client):
        session=advance(client,begin_second(c,client),'source_revoked')
        denied_source(c,client)
        assert client.get('/api/recovery-sessions').json()['sessions']==[session]
        view=client.get('/api/bootstrap').json()['restore_scope']
        assert view['state']=='source_controller' and view['read_only']
        assert view['controller']['session_id']==session['session_id']
        read=client.post('/api/recovery-sessions/'+session['session_id']+'/readback',
            json={'operation_id':session['operation_id'],'chapter':2,'draft_id':c['draft'].id})
        assert read.status_code==200,read.text
        assert read.json()['author_draft']['text']==c['draft'].text
        assert [item['chapter'] for item in read.json()['chapters']]==[1,2]
        target_book=inventory_tree(c['target']/'book')
    with restored_client(c) as (app,client):
        assert app.state.restore_scope.controller['session_id']==session['session_id']
        assert client.get('/api/maintenance').json()['state']=='interrupted'
        rejected=step(client,session)
        assert rejected.status_code==409,rejected.text
        assert rejected.json()['error']['code']=='workbench_maintenance_invalid'
        assert client.post('/api/maintenance/resume',json={'operation_id':session['operation_id']}).status_code==200
        denied_source(c,client)
        session=advance(client,session,'active')
        assert client.post('/api/maintenance/finish',json={'operation_id':session['operation_id']}).status_code==200
        assert client.get('/api/maintenance').json()['state']=='idle'
        assert client.get('/api/recovery-sessions/'+session['session_id']).json()==session
        denied_source(c,client)
        scope=load_restore_scope(c['target']/'workbench.sqlite',machine_store=c['machine'])
        assert scope.active and scope.document['scope_generation']==2
        assert inventory_tree(c['target']/'book')==target_book
    unchanged(c)


def test_live_controller_explicit_rollback_reuses_its_own_real_instance_lease(case):
    c=case
    with restored_client(c) as (app,client):
        session=advance(client,begin_second(c,client),'source_revoked')
        response=step(client,session,action='rollback')
        assert response.status_code==200,response.text
        session=response.json()
        assert session['phase']=='rolled_back'
        assert client.get('/api/restore-scope').json()['state']=='active'
        assert client.post('/api/maintenance/finish',json={'operation_id':session['operation_id']}).status_code==200
        assert client.get('/api/projects/'+c['project'].id+'/chapters/2').status_code==200
        assert load_restore_scope(c['target']/'workbench.sqlite',machine_store=c['machine']).read_only
    unchanged(c)


def test_lost_handoff_response_and_restarted_rollback_never_auto_resume(case):
    c=case
    with restored_client(c) as (app,client):
        session=advance(client,begin_second(c,client),'handoff_ready')
        def fault(phase,state):
            if phase=='after_session_operation' and state['pending_action']=='handoff':
                raise RuntimeError('lost handoff result')
        app.state.recovery_sessions.fault_injector=fault
        assert step(client,session).status_code==409
        session=client.get('/api/recovery-sessions/'+session['session_id']).json()
        assert session['pending_action']=='handoff'
    with restored_client(c) as (app,client):
        assert client.get('/api/recovery-sessions/'+session['session_id']).json()==session
        denied_source(c,client)
        assert client.post('/api/maintenance/resume',json={'operation_id':session['operation_id']}).status_code==200
        response=step(client,session,action='handoff',resume=True)
        assert response.status_code==200,response.text
        session=response.json()
        response=step(client,session,action='rollback')
        assert response.status_code==200,response.text
        session=response.json()
        assert client.get('/api/bootstrap').json()['restore_scope']['restart_required'] is True
        assert client.post('/api/maintenance/finish',json={'operation_id':session['operation_id']}).status_code==200
        assert client.get('/api/projects').status_code==409
    with restored_client(c) as (app,client):
        assert app.state.restore_scope.active
        assert client.get('/api/projects/'+c['project'].id+'/chapters/2').status_code==200
    unchanged(c)


def test_controller_only_denies_other_sessions_operations_and_new_targets(case):
    c=case
    with restored_client(c) as (app,client):
        session=advance(client,begin_second(c,client),'source_revoked')
        other='restore_'+'0'*32
        for suffix in ('','/readback','/steps/capture_usage','/open'):
            path='/api/recovery-sessions/'+other+suffix
            response=client.post(path,json={'operation_id':session['operation_id']}) if suffix else client.get(path)
            assert response.status_code==409
        assert client.post('/api/maintenance/resume',json={'operation_id':'0'*32}).status_code==409
        assert client.post('/api/recovery-sessions/'+session['session_id']+'/steps/capture_usage',
            json={'operation_id':'0'*32,'expected_state_sha256':session['state_sha256'],'confirmed':False,'resume':False}).status_code==409
        target=c['target'].parent/'arbitrary'
        assert client.post('/api/recovery-sessions/preview',json={'operation_id':session['operation_id'],
            'archive_id':c['rebackup'].name,'destination':str(target)}).status_code==409
        assert not target.exists()
        assert client.get('/api/recovery-sessions/'+session['session_id']).json()==session
        assert step(client,session,action='rollback').status_code==200
    unchanged(c)


def test_controller_proof_loss_damage_and_newer_machine_record_refuse_startup(case):
    c=case
    with restored_client(c) as (app,client):
        session=advance(client,begin_second(c,client),'source_revoked')
        directory=app.state.recovery_sessions._directory(session['session_id'])
        intent,state=app.state.recovery_sessions._load(session['session_id'])
        preview=state['receipts']['preview_handoff']
        handoff=RestoreHandoff(c['project'].book_id,machine_store=c['machine'])
        paths=[c['source']/'write-qualification.json',directory/'intent.json',directory/'state.json',
            handoff._path(preview['handoff_id']),handoff._path(intent['plan']['source_restore_provenance']['qualification']['handoff_id'],'activation-done')]
        for path in paths:
            raw=path.read_bytes()
            try:
                path.write_text('{}',encoding='utf-8')
                assert client.get('/api/bootstrap').status_code==409,path
                with pytest.raises((RuntimeError,ValueError,OSError)):
                    load_restore_scope(c['source']/'workbench.sqlite',machine_store=c['machine'],allow_recovery_controller=True)
                assert path.read_text(encoding='utf-8')=='{}'
            finally:path.write_bytes(raw)
            assert client.get('/api/bootstrap').status_code==200,path
        raw=handoff.owner.record_path.read_bytes()
        try:
            newer=_signed({**preview['restoring_record'],'generation':preview['restoring_record']['generation']+20})
            handoff.owner.record_path.write_text(json.dumps(newer),encoding='utf-8')
            assert client.get('/api/bootstrap').status_code==409
            assert json.loads(handoff.owner.record_path.read_text(encoding='utf-8'))==newer
        finally:handoff.owner.record_path.write_bytes(raw)
        assert step(client,session,action='rollback').status_code==200
    unchanged(c)


def test_active_restored_controller_rejects_original_generation_archive_and_wrong_lease(case):
    c=case
    with restored_client(c) as (app,client):
        gate=client.post('/api/maintenance/begin',json={'purpose':'restore'}).json()
        app.state.recovery_sessions.archive_root=c['package'].parent
        response=client.post('/api/recovery-sessions/preview',json={'operation_id':gate['operation_id'],
            'archive_id':c['package'].name,'destination':str(c['target'])})
        assert response.status_code==409,response.text
        assert not c['target'].exists()
        assert client.post('/api/maintenance/finish',json={'operation_id':gate['operation_id']}).status_code==200
        app.state.recovery_sessions.archive_root=c['rebackup'].parent
        session=advance(client,begin_second(c,client),'source_revoked')
        _,state=app.state.recovery_sessions._load(session['session_id'])
        handoff=RestoreHandoff(c['project'].book_id,machine_store=c['machine'])
        lease=InstanceLease(c['target'].parent/'wrong-controller').acquire()
        try:
            with pytest.raises(RestoreHandoffError,match='不属于原数据库'):
                handoff.rollback(handoff_id=state['receipts']['preview_handoff']['handoff_id'],source_lease=lease)
        finally:lease.close()
        assert step(client,session,action='rollback').status_code==200
    unchanged(c)
