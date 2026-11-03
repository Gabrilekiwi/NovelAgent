"""A Windows lock remains held while its exact bytes enter the snapshot."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import copy_context
import os
import subprocess
import sys

import pytest

from core.engine.persistence import persistence_run_lock,held_persistence_lock_readers,PersistenceLockError
from core.engine.persistence import persistence_state_lock,_borrow_held_run_locks
from workbench.backup_files import inventory_tree,copy_inventory,BackupFilesError
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for


def test_locked_file_copied_through_owner_handle_without_unlocking_for_external_process(tmp_path):
    source=tmp_path/'作品';source.mkdir();run=source/'runtime';destination=tmp_path/'副本'
    with closing(InstanceLease(tmp_path/'data').acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id']
        with gate.capture(op) as permit,persistence_run_lock(run):
            readers=held_persistence_lock_readers()
            if os.name=='nt':
                with pytest.raises(PermissionError):(run/'.persistence.lock').read_bytes()
            before=inventory_tree(source,locked_readers=readers)
            code="from core.engine.persistence import persistence_run_lock,PersistenceLockError;import sys\ntry:\n with persistence_run_lock(sys.argv[1]):sys.exit(99)\nexcept PersistenceLockError:sys.exit(17)"
            observed=[]
            def probe(relative):
                child=subprocess.run([sys.executable,'-B','-c',code,str(run)],timeout=5,capture_output=True,
                    **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
                assert child.returncode==17,child.stderr
                observed.append(relative)
            report=copy_inventory(source,destination,before,snapshot_permit=permit,locked_readers=readers,progress=probe)
            assert observed==['runtime/.persistence.lock']
            assert (destination/'runtime/.persistence.lock').read_bytes()==b'\0'
            assert report['total_bytes']==1
            assert inventory_tree(source,locked_readers=readers)==before
        assert held_persistence_lock_readers()=={}


def test_ended_or_foreign_reader_cannot_supply_bytes_for_inventory(tmp_path):
    root=tmp_path/'root';root.mkdir()
    with persistence_run_lock(root):readers=held_persistence_lock_readers()
    with pytest.raises(PersistenceLockError,match='no longer active'):
        inventory_tree(root,locked_readers=readers)
    with pytest.raises(BackupFilesError,match='读取器无效'):
        inventory_tree(root,locked_readers={root/'.persistence.lock':lambda:b'forged'})


def test_reader_cannot_be_borrowed_from_another_execution_thread(tmp_path):
    with persistence_run_lock(tmp_path):
        reader=held_persistence_lock_readers()[tmp_path/'.persistence.lock']
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(held_persistence_lock_readers).result()=={}
            with pytest.raises(PersistenceLockError,match='no longer active'):
                pool.submit(reader.read,reader.path).result()


def test_nested_locks_and_exception_restore_outer_reader_context(tmp_path):
    with persistence_run_lock(tmp_path/'outer'):
        outer=held_persistence_lock_readers()
        with pytest.raises(RuntimeError):
            with persistence_run_lock(tmp_path/'inner'):
                assert len(held_persistence_lock_readers())==2
                raise RuntimeError('synthetic')
        assert held_persistence_lock_readers()==outer
        assert next(iter(outer.values())).read(next(iter(outer)))==b'\0'
    assert held_persistence_lock_readers()=={}


def test_lock_reader_rejects_hardlinked_source(tmp_path):
    with persistence_run_lock(tmp_path):
        reader=held_persistence_lock_readers()[tmp_path/'.persistence.lock']
        os.link(reader.path,tmp_path/'alias')
        with pytest.raises(PersistenceLockError,match='binding changed'):reader.read(reader.path)


def test_explicit_verified_source_borrow_keeps_original_run_and_state_os_locks(tmp_path):
    root=tmp_path/'run';target=tmp_path/'state'
    with persistence_run_lock(root,state_paths=[target]):
        original=held_persistence_lock_readers()
        with pytest.raises(PersistenceLockError):
            with persistence_run_lock(root):pytest.fail('ordinary nested run lock entered')
        with _borrow_held_run_locks(),persistence_run_lock(root,state_paths=[target]):
            assert held_persistence_lock_readers()==original
            code="from core.engine.persistence import persistence_run_lock,PersistenceLockError;import sys\ntry:\n with persistence_run_lock(sys.argv[1],state_paths=[sys.argv[2]]):sys.exit(99)\nexcept PersistenceLockError:sys.exit(17)"
            result=subprocess.run([sys.executable,'-B','-c',code,str(root),str(target)],capture_output=True,timeout=10,
                **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
            assert result.returncode==17,result.stderr
        with pytest.raises(PersistenceLockError):
            with persistence_run_lock(root):pytest.fail('borrow permission survived its scope')


def test_copied_verified_source_permission_cannot_borrow_run_lock_from_another_thread(tmp_path):
    def borrow():
        with persistence_run_lock(tmp_path):pytest.fail('other thread borrowed source lock')
    with persistence_run_lock(tmp_path),_borrow_held_run_locks():
        copied=copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(PersistenceLockError,match='thread/context'):
                pool.submit(copied.run,borrow).result(timeout=10)


def test_ended_verified_source_permission_cannot_reacquire_a_released_run_lock(tmp_path):
    with persistence_run_lock(tmp_path),_borrow_held_run_locks():copied=copy_context()
    def borrow():
        with persistence_run_lock(tmp_path):pytest.fail('stale permission reacquired source lock')
    with pytest.raises(PersistenceLockError,match='thread/context'):copied.run(borrow)


def test_source_borrow_does_not_cover_run_roots_acquired_after_permission_started(tmp_path):
    with persistence_run_lock(tmp_path/'original'),_borrow_held_run_locks():
        with persistence_run_lock(tmp_path/'later'):
            with pytest.raises(PersistenceLockError):
                with persistence_run_lock(tmp_path/'later'):pytest.fail('later root became reentrant')


def test_run_writer_can_borrow_its_own_auxiliary_state_fence_without_borrowing_a_run(tmp_path):
    target=tmp_path/'state'
    with persistence_state_lock([target]):
        original=held_persistence_lock_readers()
        with persistence_run_lock(tmp_path/'run',state_paths=[target]):
            assert all(held_persistence_lock_readers()[path] is reader for path,reader in original.items())
        assert held_persistence_lock_readers()==original
