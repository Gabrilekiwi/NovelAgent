"""Actual session process death, financial ambiguity and HTTP cancellation."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys
import threading

from fastapi.testclient import TestClient
import httpx
import pytest

from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_restore_database import logical
from tests.test_recovery_sessions import controller, start, step, advance
from tests.test_restore_budget_preparation import make_financial_source
from workbench.backup_files import inventory_tree
from workbench.book_ownership import BookOwnership
from workbench.instance import InstanceLease, InstanceAlreadyRunning
from workbench.maintenance import maintenance_for
from workbench.restore_scope import load_restore_scope
from workbench.model_usage import read_project_model_usage


@pytest.mark.parametrize('action,event,phase', [
    ('materialize','after_session_pending','planned'),
    ('handoff','after_session_operation','handoff_ready'),
    ('activate','after_session_operation','execution_prepared'),
])
def test_actual_session_process_death_explicitly_resumes_original_intent(tmp_path,action,event,phase):
    c=make_financial_source(include_uncertain=False)
    directory=tmp_path/'原控制 台';target=tmp_path/'恢复 副本'
    original_book,original_sql=inventory_tree(c['root']),logical(c['db'].path)
    with TestClient(controller(c,directory)) as client:
        session=advance(client,start(client,c,target),phase)
    child_code='''import json,os,socket,sys
from pathlib import Path
from fastapi.testclient import TestClient
from tests.test_recovery_sessions import controller,step
original_connect=socket.socket.connect
def local_connect(self,address):
    if isinstance(address,tuple) and address[0] in {'127.0.0.1','::1','localhost'}:
        return original_connect(self,address)
    raise AssertionError('remote socket during recovery')
socket.socket.connect=local_connect
c={key:Path(value) for key,value in json.loads(sys.argv[1]).items()}
app=controller(c,Path(sys.argv[2]))
def fault(event,state):
    if event==sys.argv[5] and state.get('pending_action')==sys.argv[4]:os._exit(96)
app.state.recovery_sessions.fault_injector=fault
with TestClient(app) as client:
    client.get('/')
    session=client.get('/api/recovery-sessions/'+sys.argv[3]).json()
    resumed=client.post('/api/maintenance/resume',json={'operation_id':session['operation_id']})
    assert resumed.status_code==200,resumed.text
    response=step(client,session,action=sys.argv[4])
    raise AssertionError(response.text)
'''
    config=json.dumps({key:str(c[key]) for key in ('root','machine','package')})
    child=subprocess.run([sys.executable,'-B','-c',child_code,config,str(directory),
        session['session_id'],action,event],capture_output=True,text=True,encoding='utf-8',
        cwd=Path.cwd(),timeout=180)
    assert child.returncode==96,child.stdout+child.stderr
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    original_machine=owner.record_path.read_bytes()
    if action=='materialize':assert not target.exists()
    if action=='handoff':assert json.loads(original_machine)['state']=='restoring'
    if action=='activate':assert load_restore_scope(target/'workbench.sqlite',machine_store=c['machine']).active
    app=controller(c,directory)
    with TestClient(app) as client:
        client.get('/')
        assert client.get('/api/maintenance').json()['state']=='interrupted'
        pending=client.get('/api/recovery-sessions/'+session['session_id']).json()
        assert pending['phase']==phase and pending['pending_action']==action
        assert not pending['activated'] and not pending['automatic_provider_calls_allowed']
        assert client.get('/api/recovery-sessions/'+session['session_id']).json()==pending
        assert owner.record_path.read_bytes()==original_machine
        if action=='materialize':assert not target.exists()
        assert client.post('/api/maintenance/resume',json={'operation_id':pending['operation_id']}).status_code==200
        assert step(client,pending,action=action,resume=False).status_code==409
        if action=='activate':
            assert step(client,pending,action='rollback',resume=False).status_code==409
            assert owner.record_path.read_bytes()==original_machine
        response=step(client,pending,action=action,resume=True)
        assert response.status_code==200,response.text
        resumed=response.json()
        assert resumed['pending_action'] is None and resumed['phase']!=phase
        duplicate=step(client,pending,action=action,resume=True)
        assert duplicate.status_code==200 and duplicate.json()==resumed
        if action=='handoff':
            rollback=step(client,resumed,action='rollback')
            assert rollback.status_code==200,rollback.text
            assert rollback.json()['phase']=='rolled_back'
            assert json.loads(owner.record_path.read_text(encoding='utf-8'))['state']=='active'
            assert not load_restore_scope(target/'workbench.sqlite',machine_store=c['machine']).active
        if action=='activate':
            assert resumed['activated'] and owner.record_path.read_bytes()==original_machine
        if action=='materialize':
            cancelled=step(client,resumed,action='cancel')
            assert cancelled.status_code==200 and cancelled.json()['phase']=='cancelled'
            assert (target/'restore-materialization.json').exists()
        assert app.state.supervisor._thread is None
        assert client.post('/api/maintenance/finish',json={'operation_id':pending['operation_id']}).status_code==200
    assert inventory_tree(c['root'])==original_book and logical(c['db'].path)==original_sql


def test_uncertain_usage_never_becomes_zero_or_active_and_explicit_rollback_recovers_source(tmp_path):
    c=make_financial_source(include_uncertain=True)
    original_book,original_sql=inventory_tree(c['root']),logical(c['db'].path)
    target=tmp_path/'copy';app=controller(c,tmp_path/'controller')
    with TestClient(app) as client:
        session=advance(client,start(client,c,target),'execution_prepared')
        assert session['usage_settled'] is False and session['usage_blockers']
        usage=read_project_model_usage(target/'book',book_id=c['project'].book_id)
        assert usage['summary']['attempt_count']==3
        assert usage['chapter_budgets']==c['source_usage']['chapter_budgets']
        before=inventory_tree(target/'book')
        response=step(client,session,action='activate')
        assert response.status_code==409,response.text
        pending=app.state.recovery_sessions.get(session['session_id'])
        assert pending['pending_action'] is None and not pending['activated']
        assert not load_restore_scope(target/'workbench.sqlite',machine_store=c['machine']).active
        assert inventory_tree(target/'book')==before
        # Known unresolved usage must be rejected before starting activation,
        # so the source can still be explicitly recovered without inventing usage.
        response=step(client,pending,action='rollback')
        assert response.status_code==200,response.text
        assert response.json()['phase']=='rolled_back'
        owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
        assert json.loads(owner.record_path.read_text(encoding='utf-8'))['state']=='active'
        assert not load_restore_scope(target/'workbench.sqlite',machine_store=c['machine']).active
        assert app.state.supervisor._thread is None
    assert inventory_tree(c['root'])==original_book and logical(c['db'].path)==original_sql


def test_readback_rejects_other_book_draft_and_invalid_chapter_without_revealing_text(tmp_path):
    c=make_financial_source(include_uncertain=False)
    target=tmp_path/'copy';app=controller(c,tmp_path/'controller')
    with TestClient(app) as client:
        session=advance(client,start(client,c,target),'readonly_ready')
        url='/api/recovery-sessions/'+session['session_id']+'/readback'
        body={'operation_id':session['operation_id']}
        assert client.post(url,json=body).status_code==200
        before=inventory_tree(target/'book');sql=logical(target/'workbench.sqlite')
        for extra in ({'draft_id':c['other_draft'].id},{'chapter':999},{'chapter':True}):
            response=client.post(url,json={**body,**extra})
            assert response.status_code==409,response.text
            assert c['other_draft'].text not in response.text
            assert inventory_tree(target/'book')==before and logical(target/'workbench.sqlite')==sql
        assert app.state.recovery_sessions.get(session['session_id'])==session


def test_cancelled_session_response_and_shutdown_keep_capture_and_instance_until_real_copy_finishes(tmp_path):
    c=make_financial_source(include_uncertain=False)
    directory=tmp_path/'controller';target=tmp_path/'copy'
    with TestClient(controller(c,directory)) as client:session=start(client,c,target)
    app=controller(c,directory);gate=app.state.maintenance
    started,release=threading.Event(),threading.Event()
    def fault(event,state):
        if event=='after_session_operation' and state.get('pending_action')=='materialize':
            started.set()
            if not release.wait(30):raise AssertionError('missing worker release signal')
    app.state.recovery_sessions.fault_injector=fault
    async def exercise():
        lifetime=app.router.lifespan_context(app)
        await lifetime.__aenter__()
        pending=shutdown=None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver',
                headers={'x-novelagent-token':app.state.session_token}) as request:
            try:
                assert (await request.post('/api/maintenance/resume',json={'operation_id':session['operation_id']})).status_code==200
                body={'operation_id':session['operation_id'],'expected_state_sha256':session['state_sha256'],
                    'confirmed':False,'resume':False}
                pending=asyncio.create_task(request.post('/api/recovery-sessions/'+session['session_id']+'/steps/materialize',json=body))
                assert await asyncio.to_thread(started.wait,60)
                assert (target/'restore-materialization.json').exists()
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):await pending
                assert gate.status()['state']=='capturing'
                assert (await request.post('/api/maintenance/finish',json={'operation_id':session['operation_id']})).status_code==423
                assert (await request.post('/api/recovery-sessions/'+session['session_id']+'/steps/materialize',json=body)).status_code==423
                with pytest.raises(InstanceAlreadyRunning):InstanceLease(directory).acquire()
                shutdown=asyncio.create_task(lifetime.__aexit__(None,None,None))
                await asyncio.sleep(.05)
                assert not shutdown.done() and app.state.instance_lease.held
                release.set();await asyncio.wait_for(shutdown,30)
                assert not app.state.instance_lease.held and not gate.pending_tasks
            finally:
                release.set()
                if pending is not None and not pending.done():await pending
                if shutdown is None:await lifetime.__aexit__(None,None,None)
                elif not shutdown.done():await shutdown
    try:asyncio.run(exercise())
    finally:release.set();app.state.instance_lease.close()
    with closing(InstanceLease(directory).acquire()) as lease:
        assert maintenance_for(lease).status()['state']=='interrupted'
    assert app.state.recovery_sessions.get(session['session_id'])['phase']=='materialized'
    assert app.state.supervisor._thread is None
