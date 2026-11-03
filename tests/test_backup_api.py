"""Actual archive routes and cancellation/shutdown ownership of their workers."""
import asyncio
from contextlib import closing
from pathlib import Path
import threading

import httpx
import pytest

from tests.test_workbench_app import _client,_unlock,_create_project
from tests.test_backup_archive import software_tree
from workbench.instance import InstanceLease,InstanceAlreadyRunning
from workbench.maintenance import maintenance_for,MaintenanceError,MaintenanceBlocked


def configured(tmp_path):
    client,workspace=_client(tmp_path)
    client.app.state.backups.machine_store=tmp_path/'machine'
    return client,workspace


def begin(client):return client.post('/api/maintenance/begin',json={}).json()['operation_id']


def preview(client,project,op):
    result=client.post(f"/api/projects/{project['id']}/backups/preview",json={'operation_id':op})
    assert result.status_code==200,result.text
    return result.json()


def capture_body(value):
    return {'operation_id':value['operation_id'],'expected_preview_sha256':value['preview_sha256'],
        'archive_purpose':'diagnostic'}


def test_real_routes_preview_capture_list_verify_and_reject_tampered_package(tmp_path,software_tree):
    client,workspace=configured(tmp_path)
    with client:
        _unlock(client);project=_create_project(client,workspace);op=begin(client)
        assert client.get('/api/backups').json()=={'archives':[]}
        assert not client.app.state.backups.archive_root.exists()
        value=preview(client,project,op)
        assert value['archive_purpose']=='diagnostic' and not value['recoverable']
        assert value['roots'][0]['files']>0 and value['total_source_bytes']>0
        assert value['runtime_config']['restore_start_worker'] is False
        assert value['loaded_modules_match_proven'] is False
        result=client.post(f"/api/projects/{project['id']}/backups",json=capture_body(value))
        assert result.status_code==200,result.text
        package=result.json();archive_id=package['archive_id']
        assert client.get('/api/maintenance').json()['state']=='quiescent'
        assert client.get('/api/backups').json()['archives']==[{'archive_id':archive_id,'verification':'required'}]
        verified=client.post(f'/api/backups/{archive_id}/verify',json={'operation_id':op})
        assert verified.status_code==200 and verified.json()['archive_verified']
        assert not verified.json()['recoverable'] and 'manifest' not in verified.json()
        Path(package['path'],'archive.zip').write_bytes(b'corrupt synthetic zip')
        assert client.post(f'/api/backups/{archive_id}/verify',json={'operation_id':op}).status_code==409
        assert client.post('/api/maintenance/finish',json={'operation_id':op}).status_code==200


@pytest.mark.parametrize('route',['/api/projects/absent/backups/preview','/api/projects/absent/backups',
    '/api/backups/backup_'+('a'*32)+'/verify'])
def test_archive_routes_retain_origin_and_local_session_checks(tmp_path,route):
    client,_=configured(tmp_path)
    with client:
        assert client.post(route,json={}).status_code==403
        _unlock(client)
        assert client.post(route,json={},headers={'origin':'https://external.invalid'}).status_code==403
        assert client.post(route,json={},headers={'host':'untrusted.invalid'}).status_code==400
        assert not client.app.state.backups.archive_root.exists()


def test_archive_requests_require_exact_intent_operation_and_quiescence(tmp_path,software_tree):
    client,workspace=configured(tmp_path)
    with client:
        _unlock(client);project=_create_project(client,workspace);gate=client.app.state.maintenance
        url=f"/api/projects/{project['id']}/backups/preview"
        assert client.post(url,json={'operation_id':'a'*32}).status_code==409
        with gate.admit('local_writer'):
            op=begin(client)
            assert client.post(url,json={'operation_id':op}).status_code==423
        assert client.post(url,json={'operation_id':'b'*32}).status_code==409
        assert client.post(url,json={'operation_id':op,'archive_root':str(tmp_path/'elsewhere')}).status_code==409
        assert client.post('/api/backups/invalid/verify',json={'operation_id':op}).status_code==409
        assert client.post(f"/api/projects/{project['id']}/backups",json={
            'operation_id':op,'expected_preview_sha256':'a'*64,'archive_purpose':'recoverable'}).status_code==409
        assert not (tmp_path/'elsewhere').exists() and not client.app.state.backups.archive_root.exists()


@pytest.mark.parametrize('change',['source','project','runtime_config'])
def test_api_refuses_changed_preview_without_creating_package(tmp_path,software_tree,change):
    client,workspace=configured(tmp_path)
    with client:
        _unlock(client);project=_create_project(client,workspace)
        other=_create_project(client,workspace,name='other-book') if change=='project' else None
        op=begin(client);value=preview(client,project,op)
        target=project['id']
        if change=='source':Path(project['root'],'new-note.txt').write_text('changed')
        elif change=='project':target=other['id']
        else:
            service=client.app.state.backups
            service.runtime_config={**service.runtime_config,'worker_enabled':True}
        response=client.post(f'/api/projects/{target}/backups',json=capture_body(value))
        assert response.status_code==409,response.text
        assert not client.app.state.backups.archive_root.exists()


def test_cancelled_capture_and_shutdown_wait_for_real_thread_after_source_release(tmp_path,software_tree,monkeypatch):
    client,workspace=configured(tmp_path);app=client.app;gate=app.state.maintenance
    _unlock(client);project=_create_project(client,workspace);op=begin(client)
    value=preview(client,project,op);service=app.state.backups;original=service.capture
    started=threading.Event();release=threading.Event();packages=[]
    def fault(event,path):
        if event=='after_zip':
            packages.append(path);started.set()
            if not release.wait(15):raise AssertionError('missing release signal')
    def capture(*args,**kwargs):return original(*args,**kwargs,fault=fault)
    monkeypatch.setattr(service,'capture',capture)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver',
                headers={'x-novelagent-token':app.state.session_token}) as request:
            lifetime=app.router.lifespan_context(app)
            await lifetime.__aenter__()
            pending=asyncio.create_task(request.post(f"/api/projects/{project['id']}/backups",json=capture_body(value)))
            shutdown=None
            try:
                assert await asyncio.to_thread(started.wait,10)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):await pending
                assert gate.status()['state']=='capturing' and not (packages[0]/'completion.json').exists()
                assert (await request.post('/api/maintenance/finish',json={'operation_id':op})).status_code==423
                assert (await request.post(f"/api/projects/{project['id']}/backups/preview",json={'operation_id':op})).status_code==423
                with pytest.raises(InstanceAlreadyRunning):InstanceLease(tmp_path).acquire()
                shutdown=asyncio.create_task(lifetime.__aexit__(None,None,None))
                await asyncio.sleep(.05)
                assert not shutdown.done() and app.state.instance_lease.held
                release.set();await asyncio.wait_for(shutdown,10)
                assert (packages[0]/'completion.json').exists()
                assert not app.state.instance_lease.held and not gate.pending_tasks
            finally:
                release.set()
                if not pending.done():await pending
                if shutdown is None:await lifetime.__aexit__(None,None,None)
                elif not shutdown.done():await shutdown
    try:asyncio.run(exercise())
    finally:release.set();app.state.instance_lease.close()
    with closing(InstanceLease(tmp_path).acquire()) as lease:
        assert maintenance_for(lease).status()['state']=='interrupted'


def test_capture_permit_cannot_be_borrowed_for_another_operation_or_after_release(tmp_path):
    with closing(InstanceLease(tmp_path).acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id'];permit=gate.capture(op)
        with pytest.raises(MaintenanceError):
            with gate.borrow_capture(permit,'wrong'):pass
        with pytest.raises(MaintenanceError):
            with gate.borrow_capture(object(),op):pass
        with gate.borrow_capture(permit,op):assert gate.status()['state']=='capturing'
        with pytest.raises(MaintenanceBlocked):gate.finish(op)
        permit.close()
        with pytest.raises(MaintenanceError):
            with gate.borrow_capture(permit,op):pass
