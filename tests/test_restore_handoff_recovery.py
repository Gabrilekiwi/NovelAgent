"""Rollback decisions and failed recovery must never overwrite newer ownership."""
from pathlib import Path
import subprocess
import sys

import pytest

from core.engine.persistence import atomic_write_json
from core.story_project.reviewed_state_document import digest
from tests.test_backup_archive_history import absent_source
from tests.test_restore_handoff import (tmp_path, private_temporary_directory, full_source, bound_copy,
    restored, preview, begin, ledger, deny)
from workbench.book_ownership import BookOwnershipError
from workbench.restore_handoff import RestoreHandoff, RestoreHandoffError


def test_missing_destination_is_not_created(restored):
    c = restored; target = c['target'].parent/(c['target'].name+'-absent')
    assert not target.exists()
    with pytest.raises((OSError, ValueError, RuntimeError)):
        c['handoff'].preview(c['package'],destination=target,temporary_root=c['scratch'])
    assert not target.exists() and ledger(c) == c['archived']


def test_missing_database_completion_is_not_repaired_by_handoff(restored):
    c = restored; done = c['target']/'database-binding-done.json'; done.unlink()
    with pytest.raises((OSError, ValueError, RuntimeError)): preview(c)
    assert not done.exists() and ledger(c) == c['archived']


def test_source_unavailable_still_allows_revocation_but_not_rollback(restored):
    c = restored; value = preview(c)
    with absent_source(c['root']):
        begin(c,value)
        assert ledger(c) == value['restoring_record']
        with pytest.raises((OSError, ValueError, RuntimeError)):
            c['handoff'].rollback(handoff_id=value['handoff_id'])
        assert not c['root'].exists() and ledger(c) == value['restoring_record']
        assert not c['handoff']._path(value['handoff_id'],'rollback').exists()
    assert c['handoff'].rollback(handoff_id=value['handoff_id'])['rolled_back']


def test_rollback_intent_cannot_overwrite_changed_machine_record(restored):
    c = restored; value = preview(c); begin(c,value)
    newer = {**value['restoring_record'],'generation':value['restoring_record']['generation']+5}
    newer['record_sha256'] = digest({k:v for k,v in newer.items() if k != 'record_sha256'})
    def change(phase, value):
        if phase == 'after_rollback_intent': atomic_write_json(c['owner'].record_path,newer)
    service = RestoreHandoff(c['project'].book_id,machine_store=c['store'],fault_injector=change)
    with pytest.raises(RestoreHandoffError): service.rollback(handoff_id=value['handoff_id'])
    assert ledger(c) == newer
    deny(c['root']); deny(c['target']/'book')


def test_rollback_never_recreates_missing_original_provider_lock(restored):
    c = restored; value = preview(c); begin(c,value)
    lock = c['root']/'.novelagent/runtime/runs/.provider-boundary-lock/.persistence.lock'
    saved = lock.with_name('.persistence.lock-held-for-test')
    assert lock.is_relative_to(Path.cwd()/'.tmp') and saved.is_relative_to(Path.cwd()/'.tmp')
    assert lock.is_file() and not saved.exists()
    lock.rename(saved)
    try:
        with pytest.raises((OSError, ValueError, RuntimeError)):
            c['handoff'].rollback(handoff_id=value['handoff_id'])
        assert not lock.exists() and ledger(c) == value['restoring_record']
        assert not c['handoff']._path(value['handoff_id'],'rollback').exists()
    finally:
        saved.rename(lock)


@pytest.mark.parametrize('phase', ['after_rollback_intent','after_source_reactivated'])
def test_real_process_crash_resumes_same_explicit_rollback(restored, phase):
    c = restored; value = preview(c); begin(c,value)
    code = '''import os,sys
from workbench.restore_handoff import RestoreHandoff
def fault(phase,value):
 if phase==sys.argv[4]:os._exit(84)
s=RestoreHandoff(sys.argv[1],machine_store=sys.argv[2],fault_injector=fault)
s.rollback(handoff_id=sys.argv[3])
'''
    result = subprocess.run([sys.executable,'-B','-c',code,c['project'].book_id,str(c['store']),
        value['handoff_id'],phase],capture_output=True,timeout=60,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
    assert result.returncode == 84, result.stderr
    before = ledger(c)
    with pytest.raises(RestoreHandoffError):
        c['handoff'].resume(c['package'],handoff_id=value['handoff_id'],temporary_root=c['scratch'])
    assert ledger(c) == before
    result = RestoreHandoff(c['project'].book_id,machine_store=c['store']).rollback(handoff_id=value['handoff_id'])
    assert result['rolled_back'] and result['source_record']['generation'] == c['archived']['generation']+2
    deny(c['target']/'book')
