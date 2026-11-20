"""Autonomy public stores and append-only artifacts obey book snapshot locks."""
from contextlib import closing
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

from core.engine.persistence import PersistenceLockError
from tests.test_backup_external_writers import child
from tests.test_workbench_jobs import _job_store
from workbench.backup_files import inventory_tree
from workbench.backup_source import locked_backup_source
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for
from workbench.projects import ProjectRegistry


@pytest.fixture
def autonomy_book():
    # Bound the complete path including immutable evidence filenames on Windows.
    base=Path.cwd()/'.tmp'/('t71a-'+uuid.uuid4().hex[:12]);base.mkdir()
    _,jobs,project_id=_job_store(base)
    projects=ProjectRegistry(jobs.database,allowed_workspaces=[base/'books'])
    project=projects.get(project_id)
    return dict(root=project.root,project=project,projects=projects,runtime=base/'jobs',base=base)


COMMAND='''import sys
from pathlib import Path
from core.autonomy.common import atomic_append_json,atomic_replace_json,AutonomyContractError
from core.autonomy.session import AutonomySessionStore
from core.autonomy.lease import BookLeaseStore
from core.autonomy.arc import ArcPlanStore,build_run_arc_plan
from tests.test_autonomy_plans import trusted_profiles,instruction_plan,NOW
root=Path(sys.argv[1]);command=sys.argv[2]
try:
 if command=='append':atomic_append_json(root/'nested/evidence.json',{'source':'synthetic'})
 elif command=='replace':atomic_replace_json(root/'nested/head.json',{'revision':2})
 elif command in ('preview','custom'):
  store=AutonomySessionStore(root,trusted_profiles=trusted_profiles(),reconcile_on_open=False)
  store.save_preview(instruction_plan())
 elif command=='arc':ArcPlanStore(root).create(build_run_arc_plan(instruction_plan(),session_id='session-arc',created_at=NOW))
 elif command=='lease':BookLeaseStore(root).acquire(book_id='book-autonomy',session_id='session-one',plan_id='plan-one',at=NOW)
 else:AutonomySessionStore(root)
except AutonomyContractError as exc:
 if exc.code!='autonomy_state_locked':raise
 print('BACKUP_LOCK_BLOCKED',flush=True);sys.exit(17)
'''


@pytest.mark.parametrize('command',['append','replace','preview','arc','lease','open','custom'])
def test_autonomy_public_writes_are_blocked_before_creating_or_overwriting_artifacts(autonomy_book,command):
    c=autonomy_book;root=c['root'];base=c['base']
    runtime=root/('custom-autonomy' if command=='custom' else '.novelagent/runtime/autonomy')
    if command=='replace':
        (runtime/'nested').mkdir(parents=True)
        (runtime/'nested/head.json').write_text('{"revision":1}')
    with closing(InstanceLease(base).acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id']
        with locked_backup_source(c['projects'],c['project'].id,maintenance=gate,operation_id=op,
                task_runtime_root=c['runtime'],machine_store=base/'machine') as (_,scope,readers,_):
            before=scope['inventories']['story_project']
            result=child(COMMAND,runtime,command)
            assert result.returncode==17 and b'BACKUP_LOCK_BLOCKED' in result.stdout,result.stderr
            assert inventory_tree(root,locked_readers=readers)==before
        gate.finish(op)
        result=child(COMMAND,runtime,command)
        assert result.returncode==0,result.stderr.decode('utf-8',errors='replace')
    if command!='open':assert list(runtime.rglob('*.json'))


def test_backup_cannot_split_arc_revision_publication_from_mutable_head(autonomy_book):
    c=autonomy_book;root=c['root'];base=c['base']
    runtime=root/'.novelagent/runtime/autonomy';ready=base/'arc-ready';release=base/'arc-release'
    code='''import sys,time
from pathlib import Path
import core.autonomy.arc as arc
from tests.test_autonomy_plans import instruction_plan,NOW
root,ready,release=map(Path,sys.argv[1:]);original=arc.atomic_append_json
def append(path,payload):
 original(path,payload)
 ready.write_text(str(path))
 deadline=time.monotonic()+20
 while not release.exists():
  if time.monotonic()>deadline:raise RuntimeError('test release timeout')
  time.sleep(.02)
arc.atomic_append_json=append
arc.ArcPlanStore(root).create(arc.build_run_arc_plan(instruction_plan(),session_id='session-arc',created_at=NOW))
'''
    process=subprocess.Popen([sys.executable,'-B','-c',code,*map(str,(runtime,ready,release))],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
    try:
        deadline=time.monotonic()+10
        while not ready.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.02)
        assert ready.exists(),'arc did not reach its checkpoint'
        paths={str(p.relative_to(root)) for p in root.rglob('*')}
        revision=Path(ready.read_text());before=revision.read_bytes()
        assert not list(runtime.rglob('head.json'))
        with closing(InstanceLease(base).acquire()) as lease:
            gate=maintenance_for(lease);op=gate.begin()['operation_id']
            with pytest.raises(PersistenceLockError):
                with locked_backup_source(c['projects'],c['project'].id,maintenance=gate,operation_id=op,
                        task_runtime_root=c['runtime'],machine_store=base/'machine'):
                    pytest.fail('backup split one arc mutation')
            assert gate.status()['state']=='quiescent'
            gate.finish(op)
        assert revision.read_bytes()==before
        assert {str(p.relative_to(root)) for p in root.rglob('*')}==paths
        assert not (root/'.novelagent/runtime/runs').exists()
    finally:
        release.write_text('continue')
        try:stdout,stderr=process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill();process.communicate(timeout=5);raise
    assert process.returncode==0,stderr.decode('utf-8',errors='replace')
    assert len(list(runtime.rglob('head.json')))==1
