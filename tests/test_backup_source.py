"""Actual combined source locks, SQLite backup and file capture on v3 books."""
from contextlib import closing
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_chapter_publication_service import case,ready,service_case
from workbench.backup_database import capture_database
from workbench.backup_files import copy_inventory,inventory_tree
from workbench.backup_source import locked_backup_source,BackupSourceError
from workbench.instance import InstanceLease
from workbench.jobs import JobStore
from workbench.maintenance import maintenance_for,MaintenanceBlocked


def test_book_files_and_database_copy_under_supported_external_writer_locks(service_case):
    c=service_case;JobStore(c['db']);base=c['root'].parent
    destination=base/'locked-source-copy';destination.mkdir()
    code='''import sys
from core.engine.persistence import persistence_run_lock,PersistenceLockError
try:
 with persistence_run_lock(sys.argv[1]):sys.exit(99)
except PersistenceLockError:sys.exit(17)
'''
    with closing(InstanceLease(base).acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id']
        with locked_backup_source(c['projects'],c['project'].id,maintenance=gate,operation_id=op,
                task_runtime_root=base/'jobs',machine_store=c['machine']) as (snapshot,scope,readers,permit):
            assert scope['scope_checks_passed'],scope['blockers']
            assert not scope['archive_certified'] and gate.status()['state']=='capturing'
            for relative in ['runs/.provider-boundary-lock','.root-remap-fence','runs','reviewed/p']:
                result=subprocess.run([sys.executable,'-B','-c',code,str(c['root']/'.novelagent/runtime'/relative)],
                    timeout=5,capture_output=True,**({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
                assert result.returncode==17,result.stderr
            with pytest.raises(MaintenanceBlocked):gate.finish(op)
            db=capture_database(c['db'],destination/'workbench.sqlite',snapshot=snapshot,snapshot_permit=permit)
            files=copy_inventory(c['root'],destination/'book',scope['inventories']['story_project'],
                snapshot_permit=permit,locked_readers=readers)
            assert db['scope']=='whole_database' and files['files']
        assert gate.status()['state']=='quiescent'
        assert inventory_tree(destination/'book')['total_bytes']==files['total_bytes']


@pytest.mark.parametrize('target',['book','job'])
def test_source_mutation_inside_capture_invalidates_context_without_finishing_maintenance(service_case,target):
    c=service_case;jobs=JobStore(c['db']);base=c['root'].parent
    job=jobs.create_job(project_id=c['project'].id,kind='generation_session',start_chapter=2,end_chapter=3)
    jobroot=base/'jobs'/job.id;jobroot.mkdir(parents=True)
    with closing(InstanceLease(base).acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id']
        with pytest.raises(BackupSourceError,match='来源发生变化'):
            with locked_backup_source(c['projects'],c['project'].id,maintenance=gate,operation_id=op,
                    task_runtime_root=base/'jobs',machine_store=c['machine']):
                ((c['root'] if target=='book' else jobroot)/'unexpected.txt').write_text('changed')
        assert gate.status()['state']=='quiescent'
        with pytest.raises(MaintenanceBlocked):gate.admit('http_write')
