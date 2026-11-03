"""A legacy migration and a backup must not initialize each other's target."""
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from core.engine.persistence import PersistenceLockError
from tests.test_backup_external_writers import child
from tests.test_backup_scope import legacy
from workbench.backup_files import inventory_tree
from workbench.backup_source import locked_backup_source
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for


def migration_source(c):
    source = c['root'].parent.parent/'old-runtime'
    (source/'runs').mkdir(parents=True)
    (source/'runs/chapter_1_legacy.json').write_text(json.dumps({'run': {
        'id':'chapter_1_legacy', 'story_project': {
            'root':str(c['root']), 'book_id':c['project'].book_id}}}))
    (source/'snapshot.json').write_text('{"chapter_index":1}')
    return source


MIGRATE = '''import sys
from core.engine.persistence import PersistenceLockError
from core.story_project.migration import migrate_story_project_runtime
try:migrate_story_project_runtime(story_project_root=sys.argv[1],source_runtime=sys.argv[2])
except PersistenceLockError:
 print('BACKUP_LOCK_BLOCKED',flush=True);sys.exit(17)
'''


def test_migration_refuses_before_inspection_or_staging_while_backup_holds_book(legacy):
    c=legacy;root=c['root'];base=root.parent.parent;source=migration_source(c)
    source_before=inventory_tree(source)
    with closing(InstanceLease(base).acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id']
        with locked_backup_source(c['projects'],c['project'].id,maintenance=gate,operation_id=op,
                task_runtime_root=c['runtime'],machine_store=base/'machine') as (_,scope,readers,_):
            result=child(MIGRATE,root,source)
            assert result.returncode==17 and b'BACKUP_LOCK_BLOCKED' in result.stdout,result.stderr
            assert inventory_tree(root,locked_readers=readers)==scope['inventories']['story_project']
        gate.finish(op)
    assert inventory_tree(source)==source_before


@pytest.mark.parametrize('competitor',['backup','migration'])
def test_active_migration_keeps_runtime_absent_until_its_own_publication(legacy,competitor):
    c=legacy;root=c['root'];base=root.parent.parent;source=migration_source(c)
    ready=base/'migration-ready';release=base/'migration-release'
    runtime=root/'.novelagent/runtime'
    assert not runtime.exists()
    source_before=inventory_tree(source)
    code='''import sys,time,uuid
from pathlib import Path
from core.story_project.migration import migrate_story_project_runtime
root,source,ready,release=map(Path,sys.argv[1:])
def checkpoint():
 ready.write_text('identity-ready')
 deadline=time.monotonic()+20
 while not release.exists():
  if time.monotonic()>deadline:raise RuntimeError('test release timeout')
  time.sleep(.02)
 return uuid.uuid4()
migrate_story_project_runtime(story_project_root=root,source_runtime=source,migration_id_factory=checkpoint)
'''
    process=subprocess.Popen([sys.executable,'-B','-c',code,*map(str,(root,source,ready,release))],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
    try:
        deadline=time.monotonic()+10
        while not ready.exists() and process.poll() is None and time.monotonic()<deadline:
            time.sleep(.02)
        assert ready.exists(),'migration did not reach its checkpoint'
        before=inventory_tree(root)
        if competitor=='migration':
            result=child(MIGRATE,root,source)
            assert result.returncode==17,result.stderr
        else:
            with closing(InstanceLease(base).acquire()) as lease:
                gate=maintenance_for(lease);op=gate.begin()['operation_id']
                with pytest.raises(PersistenceLockError):
                    with locked_backup_source(c['projects'],c['project'].id,maintenance=gate,operation_id=op,
                            task_runtime_root=c['runtime'],machine_store=base/'machine'):
                        pytest.fail('backup entered an active migration')
                assert gate.status()['state']=='quiescent'
                gate.finish(op)
        assert inventory_tree(root)==before
        assert not runtime.exists()
    finally:
        release.write_text('continue')
        try:
            stdout,stderr=process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill();process.communicate(timeout=5)
            raise
    assert process.returncode==0,stderr.decode('utf-8',errors='replace')
    assert json.loads((runtime/'snapshot.json').read_bytes())['book_id']==c['project'].book_id
    assert inventory_tree(source)==source_before
