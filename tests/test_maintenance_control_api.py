"""Real control routes retain local security and cannot release active writes."""
import asyncio
import threading

import httpx

from tests.test_workbench_app import _client,_unlock,_create_project
from workbench.maintenance import DeferredStartup


def test_control_routes_keep_local_token_origin_and_operation_binding(tmp_path):
    client,_=_client(tmp_path)
    with client:
        assert client.post('/api/maintenance/begin',json={}).status_code==403
        _unlock(client)
        assert client.post('/api/maintenance/begin',json={},headers={'origin':'https://external.invalid'}).status_code==403
        assert client.post('/api/maintenance/begin',json={'purpose':'unsupported'}).status_code==409
        assert client.get('/api/maintenance').json()['state']=='idle'
        result=client.post('/api/maintenance/begin',json={})
        assert result.status_code==200 and result.json()['state']=='quiescent'
        op=result.json()['operation_id']
        assert client.post('/api/maintenance/begin',json={}).status_code==423
        assert client.post('/api/maintenance/finish',json={'operation_id':'wrong'}).status_code==409
        assert client.post('/api/maintenance/unknown',json={'operation_id':op}).status_code==409
        assert client.post('/api/maintenance/finish',json={'operation_id':op}).json()['state']=='idle'


def test_control_cannot_finish_while_actual_write_or_capture_is_in_progress(tmp_path):
    client,_=_client(tmp_path)
    with client:
        _unlock(client);gate=client.app.state.maintenance
        with gate.admit('local_writer'):
            result=client.post('/api/maintenance/begin',json={}).json()
            assert result['state']=='draining'
            body={'operation_id':result['operation_id']}
            assert client.post('/api/maintenance/finish',json=body).status_code==423
        with gate.capture(body['operation_id']):
            assert client.post('/api/maintenance/resume',json=body).status_code==423
            assert client.post('/api/maintenance/finish',json=body).status_code==423
        assert client.post('/api/maintenance/finish',json=body).json()['state']=='idle'


def test_explicit_control_resumes_interrupted_maintenance_and_initializes_database(tmp_path):
    from fastapi.testclient import TestClient
    from workbench.app import create_app
    client,workspace=_client(tmp_path)
    with client:
        _unlock(client);op=client.post('/api/maintenance/begin',json={}).json()['operation_id']
    app=create_app(database_path=tmp_path/'workbench.db',runtime_root=tmp_path/'jobs',
        allowed_workspaces=[workspace],start_worker=False,auto_import_active=False)
    with TestClient(app) as restarted:
        _unlock(restarted);body={'operation_id':op}
        assert app.state.database.initialization_pending
        assert restarted.post('/api/maintenance/finish',json=body).status_code==409
        resumed=restarted.post('/api/maintenance/resume',json=body).json()
        assert resumed['state']=='quiescent' and resumed['generation']==2
        assert app.state.database.initialization_pending
        result=restarted.post('/api/maintenance/finish',json=body)
        assert result.status_code==200 and result.json()['state']=='idle'
        assert not app.state.database.initialization_pending
        _create_project(restarted,workspace)


def test_cancelled_finish_response_keeps_startup_work_admitted(tmp_path):
    client,_=_client(tmp_path);app=client.app;gate=app.state.maintenance
    op=gate.begin()['operation_id'];started=threading.Event();complete=threading.Event()
    saved=tmp_path/'startup-finished.txt'
    def startup():
        started.set()
        if not complete.wait(5):raise AssertionError('missing completion signal')
        saved.write_text('finished')
    app.state.startup_recovery=DeferredStartup(startup)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as request:
            pending=asyncio.create_task(request.post('/api/maintenance/finish',json={'operation_id':op},
                headers={'x-novelagent-token':app.state.session_token}))
            try:
                assert await asyncio.to_thread(started.wait,3)
                pending.cancel()
                try:await pending
                except asyncio.CancelledError:pass
                assert gate.status()['active_writes']==1 and not saved.exists()
                assert gate.begin()['state']=='draining'
            finally:
                complete.set();await gate.drain_async()
    try:
        asyncio.run(exercise())
        assert saved.exists() and gate.status()['active_writes']==0
    finally:complete.set();app.state.instance_lease.close()
