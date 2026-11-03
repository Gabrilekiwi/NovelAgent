"""Real CLI writers and state/identity locks competing with a held snapshot."""
from contextlib import closing
import json
import subprocess
import sys

import pytest

from tests.test_backup_scope import legacy
from tests.test_story_state_activation import StoryStateActivationTest
from workbench.backup_files import inventory_tree
from workbench.backup_source import locked_backup_source
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for


def child(code,*args):
    return subprocess.run([sys.executable,'-B','-c',code,*map(str,args)],capture_output=True,timeout=15,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))


@pytest.mark.parametrize('command',['init','activate'])
def test_cli_force_initialization_and_identity_activation_cannot_write_during_backup(legacy,command):
    c=legacy;base=c['root'].parent.parent;runtime=c['root']/'.novelagent/runtime'
    runtime.mkdir(parents=True,exist_ok=True)
    snapshot=runtime/'snapshot.json';snapshot.write_text('{"chapter_index":9}')
    report=base/'calibration.json';report.write_text(json.dumps(StoryStateActivationTest()._report(c['project'].book_id)))
    code='''import socket,sys
from unittest.mock import patch
import main
from core.engine.persistence import PersistenceLockError
root,report,command=sys.argv[1:]
sys.argv=['main.py','--story-project',root]
sys.argv+=['--init-runtime','--force-init-runtime'] if command=='init' else ['--activate-story-state','--story-state-calibration-report',report]
try:
 with patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')):main.main()
except PersistenceLockError:
 print('BACKUP_LOCK_BLOCKED',flush=True);sys.exit(17)
'''
    with closing(InstanceLease(base).acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id']
        with locked_backup_source(c['projects'],c['project'].id,maintenance=gate,operation_id=op,
                task_runtime_root=c['runtime'],machine_store=base/'machine') as (_,scope,readers,_):
            before=scope['inventories']['story_project']
            result=child(code,c['root'],report,command)
            assert result.returncode==17 and b'BACKUP_LOCK_BLOCKED' in result.stdout,result.stderr
            assert inventory_tree(c['root'],locked_readers=readers)==before
        gate.finish(op)
        result=child(code,c['root'],report,command)
        assert result.returncode==0,result.stderr
    if command=='init':assert json.loads(snapshot.read_bytes())['chapter_index']!=9
    else:assert json.loads((c['root']/'.novelagent/project.json').read_bytes())['story_state_mode']=='strict'


@pytest.mark.parametrize('target',['identity','book','snapshot','memory'])
def test_shared_state_lock_blocks_an_independent_run_directory(legacy,target):
    c=legacy;base=c['root'].parent.parent;runtime=c['root']/'.novelagent/runtime'
    state={'identity':c['root']/'.novelagent/project.json','book':c['root'],
           'snapshot':runtime/'snapshot.json','memory':runtime/'memory/notion_memory.json'}[target]
    code='''import sys
from core.engine.persistence import persistence_run_lock,PersistenceLockError
try:
 with persistence_run_lock(sys.argv[1],state_paths=[sys.argv[2]]):sys.exit(99)
except PersistenceLockError:sys.exit(17)
'''
    with closing(InstanceLease(base).acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id']
        with locked_backup_source(c['projects'],c['project'].id,maintenance=gate,operation_id=op,
                task_runtime_root=c['runtime'],machine_store=base/'machine'):
            result=child(code,base/'alternate-writer',state)
            assert result.returncode==17,result.stderr


def test_initialization_holds_both_state_locks_through_actual_file_copy(tmp_path,monkeypatch):
    from core import runtime_paths
    snapshot=tmp_path/'runtime/snapshot.json';memory=tmp_path/'different-memory.json'
    source=tmp_path/'example.json';source.write_text('{}')
    original=runtime_paths.shutil.copyfile;seen=[]
    code='''import sys
from core.engine.persistence import persistence_run_lock,PersistenceLockError
try:
 with persistence_run_lock(sys.argv[1],state_paths=[sys.argv[2]]):sys.exit(99)
except PersistenceLockError:sys.exit(17)
'''
    def copy(*args,**kwargs):
        for target in (snapshot,memory):
            result=child(code,tmp_path/'other-writer',target)
            assert result.returncode==17,result.stderr
        seen.append(str(args[1]));return original(*args,**kwargs)
    monkeypatch.setattr(runtime_paths.shutil,'copyfile',copy)
    result=runtime_paths.init_runtime_state(snapshot_source=source,memory_source=source,
        snapshot_target=snapshot,memory_target=memory,overwrite=True)
    assert len(seen)==2 and len(result['copied'])==2


@pytest.mark.parametrize('command',['recover_latest','authority','authority_reconcile'])
def test_auxiliary_recovery_and_authority_bootstrap_are_blocked_before_creating_output(legacy,command):
    c=legacy;base=c['root'].parent.parent
    code='''import sys
from pathlib import Path
from core.engine.persistence import PersistenceLockError
from core.engine.recovery import recover_latest_chapter_draft
from core.story_project.authority_persistence import event_authority_write_operation,reconcile_event_authority_persistence,EventAuthorityPersistenceBarrierError
root=Path(sys.argv[1]);command=sys.argv[2]
try:
 if command=='recover_latest':
  recover_latest_chapter_draft(run_dir=root/'.novelagent/runtime/runs',chapter_dir=root/'.novelagent/runtime/chapters')
 elif command=='authority_reconcile':reconcile_event_authority_persistence(root)
 else:
  with event_authority_write_operation(root,expected_book_id=None,writer_kind='chapter'):pass
except (PersistenceLockError,EventAuthorityPersistenceBarrierError) as exc:
 print(type(exc).__name__,str(exc),flush=True);sys.exit(17)
sys.exit(99)
'''
    with closing(InstanceLease(base).acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id']
        with locked_backup_source(c['projects'],c['project'].id,maintenance=gate,operation_id=op,
                task_runtime_root=c['runtime'],machine_store=base/'machine') as (_,scope,readers,_):
            result=child(code,c['root'],command)
            assert result.returncode==17,result.stderr
            if command.startswith('authority'):assert b'event_authority_writer_busy' in result.stdout
            assert inventory_tree(c['root'],locked_readers=readers)==scope['inventories']['story_project']
