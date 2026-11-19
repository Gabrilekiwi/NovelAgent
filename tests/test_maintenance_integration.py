"""Actual API/worker admission, disconnected threads and durable restarts."""
import asyncio
from pathlib import Path
import subprocess
import sys
import threading

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.test_workbench_app import _client,_unlock,_create_project
from tests.test_workbench_jobs import _job_store
from workbench.app import create_app
from workbench.instance import InstanceLease,InstanceAlreadyRunning
from workbench.jobs import JobSupervisor,ProcessMonitorError
from workbench.maintenance import maintenance_for,MaintenanceBlocked
from workbench.maintenance_api import MaintenanceRoute,MaintenanceControlRoute
from workbench.backup_api import BackupControlRoute
from workbench.recovery_api import RecoveryControlRoute


def test_every_product_write_route_uses_admission_and_reads_remain_available(tmp_path):
    client,workspace=_client(tmp_path)
    with client:
        _unlock(client)
        routes=[r for r in client.app.routes if isinstance(r,APIRoute) and
            r.methods & {'POST','PUT','PATCH','DELETE'}]
        controls=[r for r in routes if isinstance(r,MaintenanceControlRoute)]
        assert len(controls)==1 and controls[0].path=='/api/maintenance/{action}'
        archives=[r for r in routes if isinstance(r,BackupControlRoute)]
        assert {r.path for r in archives}=={
            '/api/backups/{archive_id}/verify',
            '/api/projects/{project_id}/backups',
            '/api/projects/{project_id}/backups/preview',
            '/api/projects/{project_id}/backups/{archive_id}/certification',
            '/api/projects/{project_id}/backups/{archive_id}/certification/preview',
            '/api/projects/{project_id}/backups/{archive_id}/certification/verify',
            '/api/projects/{project_id}/memory-revisions/preview',
            '/api/projects/{project_id}/memory-revisions/confirm',
            '/api/projects/{project_id}/onboarding/baselines',
            '/api/projects/{project_id}/onboarding/baselines/preview',
            '/api/projects/{project_id}/onboarding/baselines/proposal',
            '/api/projects/{project_id}/onboarding/baselines/source',
            '/api/projects/{project_id}/onboarding/baselines/{identifier}/confirm',
            '/api/projects/{project_id}/onboarding/baselines/{identifier}/edit',
            '/api/projects/{project_id}/onboarding/baselines/{identifier}/inspect',
            '/api/projects/{project_id}/onboarding/preview',
            '/api/projects/{project_id}/onboarding/registration',
            '/api/projects/{project_id}/onboarding/registration/preview',
            '/api/projects/{project_id}/onboarding/upgrade/commit',
            '/api/projects/{project_id}/onboarding/upgrade/continue',
            '/api/projects/{project_id}/onboarding/upgrade/preview',
        }
        recoveries=[r for r in routes if isinstance(r,RecoveryControlRoute)]
        assert {r.path for r in recoveries}=={
            '/api/recovery-sessions',
            '/api/recovery-sessions/preview',
            '/api/recovery-sessions/{session_id}/open',
            '/api/recovery-sessions/{session_id}/readback',
            '/api/recovery-sessions/{session_id}/steps/{action}',
        }
        assert len(routes)>35 and all(isinstance(r,(MaintenanceRoute,MaintenanceControlRoute,BackupControlRoute,RecoveryControlRoute)) for r in routes)
        gate=client.app.state.maintenance;op=gate.begin()['operation_id']
        for method,path in [('POST','/api/projects'),('POST','/api/projects/absent/chapters/2/budget'),
            ('PUT','/api/candidate-drafts/absent'),('POST','/api/jobs/absent/resume'),
            ('POST','/api/projects/absent/chapters/2/candidates'),('POST','/api/chapters/2/amendments')]:
            result=client.request(method,path,json={})
            assert result.status_code==423,(path,result.text)
            assert result.json()['error']['code']=='workbench_maintenance_active'
        assert client.get('/api/projects').status_code==200
        assert client.get('/api/maintenance').json()['state']=='quiescent'
        assert list(workspace.iterdir())==[]
        gate.finish(op)
        assert _create_project(client,workspace)['name']=='测试长篇'
        assert gate.status()['active_writes']==0


def test_cancelled_http_request_keeps_actual_to_thread_write_admitted(tmp_path):
    client,_=_client(tmp_path);app=client.app;gate=app.state.maintenance
    started=threading.Event();finish=threading.Event();saved=tmp_path/'saved.txt'
    def write():
        started.set()
        if not finish.wait(5):raise AssertionError('completion signal missing')
        saved.write_text('finished',encoding='utf-8')
    @app.post('/api/test-slow-write')
    async def slow():
        await asyncio.to_thread(write)
        return {'saved':True}
    async def exercise():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver',
                headers={'x-novelagent-token':app.state.session_token}) as browser:
                request=asyncio.create_task(browser.post('/api/test-slow-write'))
                assert await asyncio.to_thread(started.wait,3)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):await request
                op=gate.begin()['operation_id']
                assert gate.status()['active_writes']==1 and not saved.exists()
                with pytest.raises(MaintenanceBlocked):gate.require_quiescent(op)
                with pytest.raises(InstanceAlreadyRunning):InstanceLease(app.state.instance_lease.directory).acquire()
                finish.set()
                await asyncio.wait_for(gate.drain_async(),3)
                assert saved.read_text()=='finished' and gate.require_quiescent(op)['state']=='quiescent'
                gate.finish(op)
        finally:
            finish.set()
            await gate.drain_async()
    try:asyncio.run(exercise())
    finally:app.state.instance_lease.close()


def test_interrupted_maintenance_defers_app_recovery_until_explicit_finish(tmp_path,monkeypatch):
    client,workspace=_client(tmp_path)
    with client:
        op=client.app.state.maintenance.begin()['operation_id']
    called=[]
    monkeypatch.setattr('workbench.planning.OutlinePlanner.recover_after_restart',lambda self:called.append('planning') or 0)
    monkeypatch.setattr('workbench.amendments.AmendmentService.recover_after_restart',lambda self:called.append('amendment') or 0)
    app=create_app(database_path=tmp_path/'workbench.db',runtime_root=tmp_path/'job-runtime',
        allowed_workspaces=[workspace],start_worker=False,auto_import_active=False)
    with TestClient(app) as restarted:
        _unlock(restarted)
        assert called==[] and restarted.get('/api/maintenance').json()['state']=='interrupted'
        assert restarted.post('/api/projects',json={}).status_code==423 and called==[]
        app.state.maintenance.resume(op)
        assert restarted.post('/api/projects',json={}).status_code==423 and called==[]
        app.state.maintenance.finish(op)
        _create_project(restarted,workspace)
        assert called==['planning','amendment']
        restarted.post('/api/projects',json={})
        assert called==['planning','amendment']


def supervisor_case(tmp_path):
    _,store,project_id=_job_store(tmp_path)
    lease=InstanceLease(tmp_path).acquire()
    supervisor=JobSupervisor(store=store,project_lookup=lambda key:None,
        repo_root=Path(__file__).resolve().parents[1],runtime_root=tmp_path/'jobs',instance_lease=lease)
    return store,project_id,lease,supervisor


def test_worker_startup_and_claim_wait_for_maintenance_to_finish(tmp_path,monkeypatch):
    store,project,lease,supervisor=supervisor_case(tmp_path)
    gate=maintenance_for(lease);op=gate.begin()['operation_id'];ran=threading.Event();blocked=threading.Event()
    job=store.create_job(project_id=project,kind='generation_session',start_chapter=1,end_chapter=1)
    observed=[];original_admit=gate.admit;original_recover=store.recover_after_restart
    def admit(kind):
        try:return original_admit(kind)
        except MaintenanceBlocked:blocked.set();raise
    def recover():observed.append('recovery');return original_recover()
    def execute(value):
        observed.append(('executed',value.id));ran.set();supervisor._stop.set()
    monkeypatch.setattr(gate,'admit',admit)
    monkeypatch.setattr(store,'recover_after_restart',recover)
    monkeypatch.setattr(supervisor,'_run_generation_session',execute)
    try:
        supervisor.start()
        assert blocked.wait(3) and supervisor._thread.is_alive()
        assert store.get(job.id).state=='queued' and observed==[]
        gate.finish(op);supervisor.notify()
        assert ran.wait(3)
        supervisor.shutdown()
        assert observed==['recovery',('executed',job.id)] and gate.status()['active_writes']==0
    finally:supervisor.shutdown();lease.close()


def test_existing_worker_drains_before_maintenance_becomes_quiescent(tmp_path,monkeypatch):
    store,project,lease,supervisor=supervisor_case(tmp_path)
    gate=maintenance_for(lease);started=threading.Event();finish=threading.Event()
    store.create_job(project_id=project,kind='generation_session',start_chapter=1,end_chapter=1)
    def execute(value):
        started.set()
        if not finish.wait(5):raise AssertionError('missing worker completion')
        supervisor._stop.set()
    monkeypatch.setattr(supervisor,'_run_generation_session',execute)
    try:
        supervisor.start();assert started.wait(3)
        op=gate.begin()['operation_id']
        assert gate.status()['state']=='draining'
        with pytest.raises(MaintenanceBlocked):gate.require_quiescent(op)
        finish.set();supervisor.shutdown()
        assert gate.require_quiescent(op)['active_writes']==0
    finally:finish.set();supervisor.shutdown();lease.close()


def test_monitor_failure_retains_admission_until_real_child_exit(tmp_path,monkeypatch):
    store,project,lease,supervisor=supervisor_case(tmp_path)
    gate=maintenance_for(lease)
    store.create_job(project_id=project,kind='generation_session',start_chapter=1,end_chapter=1)
    child=subprocess.Popen([sys.executable,'-B','-c','import sys; sys.stdin.read()'],stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,**({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
    observed=threading.Event()
    def execute(value):
        with supervisor._process_lock:supervisor._active_process=child
        observed.set()
        raise ProcessMonitorError('synthetic monitor failure with live local child')
    monkeypatch.setattr(supervisor,'_run_generation_session',execute)
    try:
        supervisor.start();assert observed.wait(3)
        supervisor._thread.join(3);assert not supervisor._thread.is_alive()
        op=gate.begin()['operation_id']
        assert child.poll() is None and gate.status()['active_writes']==1
        with pytest.raises(MaintenanceBlocked):gate.require_quiescent(op)
        with pytest.raises(RuntimeError):supervisor.shutdown()
        child.communicate(input=b'finish',timeout=3)
        supervisor.shutdown()
        assert gate.require_quiescent(op)['active_writes']==0
    finally:
        if child.poll() is None:child.communicate(input=b'finish',timeout=3)
        supervisor.shutdown();lease.close()
