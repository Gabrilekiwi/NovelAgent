"""Finish a real in-flight child and persist its DB boundary before yielding."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from tests.test_workbench_jobs import _job_store
from workbench.instance import InstanceLease
from workbench.jobs import JobError,JobSupervisor
from workbench.maintenance import maintenance_for,MaintenanceBlocked
from workbench.projects import ProjectRegistry


@pytest.fixture
def c(tmp_path):
    _,store,project_id=_job_store(tmp_path)
    projects=ProjectRegistry(store.database,allowed_workspaces=[tmp_path/'books'])
    with closing(InstanceLease(tmp_path).acquire()) as lease:
        gate=maintenance_for(lease)
        supervisor=JobSupervisor(store=store,project_lookup=projects.get,
            repo_root=Path(__file__).resolve().parents[1],runtime_root=tmp_path/'jobs',instance_lease=lease)
        queued=store.create_job(project_id=project_id,kind='generation_session',start_chapter=1,end_chapter=3,
            config={'project_state_head':'same-head'})
        job=store.claim_next()
        yield dict(store=store,job=job,gate=gate,supervisor=supervisor)


def test_already_requested_maintenance_pauses_before_first_process_without_changing_cursor(c):
    supervisor=c['supervisor'];store=c['store'];gate=c['gate'];job=c['job']
    def forbidden(*a,**kw):pytest.fail('maintenance must stop before generation preflight')
    supervisor.process_factory=forbidden
    with gate.admit('worker'):
        op=gate.begin()['operation_id']
        supervisor._run_generation_session(job)
        assert gate.status()['state']=='draining'
    paused=store.get(job.id)
    assert paused.state=='awaiting_input' and paused.next_chapter==1 and paused.result is None
    assert paused.error is None and paused.manual_confirmation_required
    event=store.events_after(job.id)[-1]
    assert event['event']=='job.paused' and event['payload']['operation_id']==op
    assert event['payload']['completed_chapters']==0 and not event['payload']['automatic_resume']
    gate.finish(op)
    assert store.claim_next() is None
    assert store.resume(job.id,confirmed=True).next_chapter==1


def test_maintenance_waits_for_real_child_and_keeps_committed_result_without_next_call(c,monkeypatch):
    store=c['store'];gate=c['gate'];supervisor=c['supervisor'];job=c['job']
    started=threading.Event();release=threading.Event();calls=[];children=[]
    monkeypatch.setattr('workbench.jobs.compute_project_state_head',lambda project:'same-head')
    monkeypatch.setattr('workbench.jobs.verify_approved_outline_binding',lambda *a:{'ok':True})
    monkeypatch.setattr('workbench.jobs._git_provenance_preflight',lambda *a:{'ok':True})
    # This is a worker-control test. A synthetic verified result stands in for
    # the separately tested publication verifier; no formal chapter is written.
    monkeypatch.setattr('workbench.jobs.verify_committed_chapter',lambda *a:{'ok':True,'receipt':True})
    def execute(job_id,chapter,command,paths):
        calls.append(chapter)
        process=subprocess.Popen([sys.executable,'-B','-c',"import sys; sys.stdin.readline(); print('finished')"],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
            **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
        children.append(process)
        with supervisor._process_lock:supervisor._active_process=process
        started.set()
        assert release.wait(5)
        out,err=process.communicate('finish\n',timeout=5)
        assert process.returncode==0 and 'finished' in out and not err
        return {'cancelled':False,'exit_code':0,'result':{'committed':True,
            'run':{'id':'synthetic-1','chapter_index':1},'review_pipeline':{'status':'pass'}}}
    monkeypatch.setattr(supervisor,'_execute_subprocess',execute)
    def worker():
        with gate.admit('worker'):supervisor._run_generation_session(job)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(worker)
        try:
            assert started.wait(4)
            op=gate.begin()['operation_id']
            assert gate.status()['state']=='draining' and children[0].poll() is None
            with pytest.raises(MaintenanceBlocked):gate.capture(op)
            release.set();pending.result(timeout=8)
        finally:
            release.set()
            for child in children:
                if child.poll() is None:child.communicate('finish\n',timeout=5)
    paused=store.get(job.id)
    assert calls==[1] and paused.state=='awaiting_input' and paused.next_chapter==2
    assert paused.result['run_id']=='synthetic-1' and paused.result['committed']
    assert paused.config['project_state_head']=='same-head' and paused.error is None
    events=store.events_after(job.id)
    assert events[-2]['event']=='chapter.committed' and events[-1]['event']=='job.paused'
    assert events[-1]['payload']['completed_chapters']==1
    with gate.capture(op):pass
    gate.finish(op)
    assert store.claim_next() is None
    resumed=store.resume(job.id,confirmed=True)
    assert resumed.state=='queued' and resumed.next_chapter==2


def test_running_or_uncertain_chapter_cannot_be_reclassified_as_safe_maintenance_pause(c):
    store=c['store'];job=c['job']
    store.mark_running(job.id,chapter=1,command=['python','main.py'])
    with pytest.raises(JobError,match='settled'):
        store.pause_for_maintenance(job.id,chapter=1,operation_id='a'*32)
    failed=store.chapter_failed(job.id,chapter=1,error={'code':'unknown'},uncertain=True)
    unchanged=store.pause_for_maintenance(job.id,chapter=1,operation_id='a'*32)
    assert unchanged==failed and unchanged.state=='needs_recovery'


def test_cancellation_before_next_chapter_is_honored_at_maintenance_boundary(c):
    store=c['store'];job=c['job'];gate=c['gate']
    store.request_cancel(job.id)
    with gate.admit('worker'):
        gate.begin();c['supervisor']._run_generation_session(job)
    cancelled=store.get(job.id)
    assert cancelled.state=='cancelled' and cancelled.finished_at and cancelled.next_chapter==1
    assert not cancelled.manual_confirmation_required


def test_live_child_cannot_release_the_maintenance_boundary(c):
    class LiveChild:
        def poll(self):return None
    supervisor=c['supervisor'];supervisor._active_process=LiveChild()
    with c['gate'].admit('worker'):
        c['gate'].begin()
        with pytest.raises(JobError,match='子进程'):
            supervisor._run_generation_session(c['job'])
        assert c['store'].get(c['job'].id).state=='starting'
    supervisor._active_process=None
