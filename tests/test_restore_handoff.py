"""Actual final-format restore copies, ledger CAS, explicit rollback and crashes."""
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys

import pytest

from core.autonomy.common import atomic_append_json
from core.engine.persistence import atomic_write_json
from core.story_project.reviewed_state_document import digest
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_restore_database import full_source, bound_copy, run, logical
from workbench.backup_files import inventory_tree
from workbench.book_ownership import BookOwnership, BookOwnershipError
from workbench.instance import InstanceLease, InstanceAlreadyRunning
from workbench.restore_handoff import RestoreHandoff, RestoreHandoffError


@pytest.fixture
def restored(bound_copy, monkeypatch):
    c = bound_copy; run(c)
    # full_source shares one immutable archive; each test gets a fresh ledger
    # with the exact archived binding. No production enrollment is simulated.
    from workbench.backup_archive import verify_archive
    from workbench import book_ownership
    store = c['target'].parent / (c['target'].name+'-machine')
    archived = verify_archive(c['package'])['manifest']['preview']['scope']['machine_ownership']['record']
    owner = BookOwnership(c['project'].book_id, machine_store=store)
    with owner.locked(): atomic_write_json(owner.record_path, archived)
    monkeypatch.setattr(book_ownership, 'default_machine_store', lambda:store)
    return {**c,'store':store,'owner':owner,'handoff':RestoreHandoff(c['project'].book_id,machine_store=store),
        'archived':archived}


def preview(c):
    return c['handoff'].preview(c['package'], destination=c['target'], temporary_root=c['scratch'])


def begin(c, value=None, service=None):
    value = value or preview(c)
    return (service or c['handoff']).begin(c['package'], destination=c['target'],
        expected_preview_sha256=value['intent_sha256'], temporary_root=c['scratch'])


def ledger(c): return json.loads(c['owner'].record_path.read_text(encoding='utf-8'))


def deny(root):
    target = root/'handoff-denied/new.json'
    with pytest.raises(BookOwnershipError): atomic_append_json(target, {'forbidden':True})
    assert not target.parent.exists()


def test_actual_restored_copy_revokes_source_and_resume_is_byte_idempotent(restored):
    c = restored; before = inventory_tree(c['target']/'book'); db_before = logical(c['target']/'workbench.sqlite')
    value = preview(c)
    assert ledger(c) == c['archived'] and not c['handoff']._path(value['handoff_id']).exists()
    result = begin(c, value)
    assert result['source_revoked'] and not result['target_writable'] and not result['handoff_completed']
    assert ledger(c) == value['restoring_record'] and ledger(c)['generation'] == c['archived']['generation']+1
    deny(c['root']); deny(c['target']/'book')
    control = {p.name:p.read_bytes() for p in c['owner'].directory.glob('handoff-*.json')}
    for _ in range(2):
        assert c['handoff'].resume(c['package'],handoff_id=value['handoff_id'],temporary_root=c['scratch']) == result
    assert {p.name:p.read_bytes() for p in c['owner'].directory.glob('handoff-*.json')} == control
    assert inventory_tree(c['target']/'book') == before and logical(c['target']/'workbench.sqlite') == db_before


def test_explicit_rollback_only_reactivates_original_and_invalidates_old_handoff(restored):
    c = restored; value = preview(c); begin(c,value)
    original_before = inventory_tree(c['root']); copy_before = inventory_tree(c['target']/'book')
    result = c['handoff'].rollback(handoff_id=value['handoff_id'])
    assert result['rolled_back'] and result['source_record']['generation'] == c['archived']['generation']+2
    assert c['handoff'].rollback(handoff_id=value['handoff_id']) == result
    with c['owner'].locked(), c['db'].transaction() as connection:
        assert c['owner'].require(root=c['root'], database=c['db'], project_id=c['project'].id, connection=connection) == result['source_record']
    probe = c['root']/'.novelagent/runtime/rollback-probe.json'
    assert not probe.exists()
    try:
        atomic_append_json(probe, {'original_writable':True})
        assert json.loads(probe.read_text(encoding='utf-8')) == {'original_writable':True}
    finally:
        if probe.exists(): probe.unlink()
    deny(c['target']/'book')
    with pytest.raises(RestoreHandoffError):
        c['handoff'].resume(c['package'],handoff_id=value['handoff_id'],temporary_root=c['scratch'])
    with pytest.raises(RestoreHandoffError): begin(c,value)
    assert inventory_tree(c['root']) == original_before and inventory_tree(c['target']/'book') == copy_before


@pytest.mark.parametrize('change', ['generation','missing_ledger','target_file','preview_hash'])
def test_stale_preview_and_changed_evidence_never_revoke_current_owner(restored, change):
    c = restored; value = preview(c)
    if change == 'generation':
        record = ledger(c); record['generation'] += 1
        record['record_sha256'] = digest({k:v for k,v in record.items() if k != 'record_sha256'})
        atomic_write_json(c['owner'].record_path, record)
    elif change == 'missing_ledger': c['owner'].record_path.unlink()
    elif change == 'target_file': (c['target']/'book'/'changed.txt').write_text('changed')
    else: value['intent_sha256'] = '0'*64
    before = c['owner'].record_path.read_bytes() if c['owner'].record_path.exists() else None
    with pytest.raises((ValueError, RuntimeError)): begin(c,value)
    assert (c['owner'].record_path.read_bytes() if c['owner'].record_path.exists() else None) == before
    assert not list(c['owner'].directory.glob('handoff-*.json'))


def test_open_restored_instance_blocks_transfer_before_intent(restored):
    c = restored
    with closing(InstanceLease(c['target']).acquire()):
        with pytest.raises(InstanceAlreadyRunning): preview(c)
    assert ledger(c) == c['archived'] and not list(c['owner'].directory.glob('handoff-*.json'))


@pytest.mark.parametrize('phase', ['after_handoff_intent','after_source_revoked','after_revocation_receipt'])
def test_real_process_crash_resumes_original_revocation(restored, phase):
    c = restored; value = preview(c)
    code = '''import os,sys
from workbench.restore_handoff import RestoreHandoff
def fault(phase,value):
 if phase==sys.argv[6]:os._exit(83)
s=RestoreHandoff(sys.argv[1],machine_store=sys.argv[2],fault_injector=fault)
s.begin(sys.argv[3],destination=sys.argv[4],expected_preview_sha256=sys.argv[5],temporary_root=sys.argv[7])
'''
    result = subprocess.run([sys.executable,'-B','-c',code,c['project'].book_id,str(c['store']),str(c['package']),
        str(c['target']),value['intent_sha256'],phase,str(c['scratch'])],capture_output=True,timeout=120,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
    assert result.returncode == 83, result.stderr
    expected = value['source_record'] if phase == 'after_handoff_intent' else value['restoring_record']
    assert ledger(c) == expected
    resumed = RestoreHandoff(c['project'].book_id,machine_store=c['store']).resume(c['package'],
        handoff_id=value['handoff_id'],temporary_root=c['scratch'])
    assert resumed['source_revoked'] and ledger(c) == value['restoring_record']
    deny(c['root']); deny(c['target']/'book')
