"""Completed archives, unavailable sources and damaged retained transactions."""
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from core.engine.persistence import LocalPersistenceTransaction, PersistenceTarget
from tests.test_backup_archive import archive_case, legacy, software_tree, capture
from tests.test_backup_mixed_history import mixed, case, transaction
from workbench.backup_archive import BackupArchiveService, BackupArchiveError, verify_archive
from workbench.backup_archive_history import verify_archived_persistence_history
from workbench.backup_files import inventory_tree
from workbench.backup_history import verify_persistence_history
from workbench.book_ownership import BookOwnership
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for


def check(c, package):
    return verify_archived_persistence_history(package, project_id=c['project'].id, book_id=c['project'].book_id)


def legacy_transaction(c):
    for run, text in [('first', 'before'), ('second', 'after')]:
        tx = LocalPersistenceTransaction(run_dir=c['root']/'.novelagent/runtime/runs', run_id=run,
                                        allowed_roots=[c['root']], book_id=c['project'].book_id)
        tx.prepare([PersistenceTarget(kind='synthetic', path=c['root']/'note.txt', content=text)],
                   candidate_result={'run': {'id': run}})
        tx.commit(); tx.complete_publication()
    return tx


@contextmanager
def absent_source(root, *, temporary_name='source-offline'):
    # Move only this independently generated fixture within its checked parent.
    root = root.resolve()
    temporary = root.parent/temporary_name
    assert root.is_relative_to((Path.cwd()/'.tmp').resolve())
    assert temporary.parent == root.parent and not temporary.exists()
    root.rename(temporary)
    try:
        assert not root.exists()
        yield
    finally:
        assert not root.exists() and temporary.parent == root.parent
        temporary.rename(root)


def test_archive_rechecks_overlapping_legacy_history_with_source_absent_and_no_live_readers(archive_case, monkeypatch):
    c = archive_case
    legacy_transaction(c)
    expected = verify_persistence_history(c['root'], inventory_tree(c['root']), book_id=c['project'].book_id)
    package = Path(capture(c)['path'])
    before = inventory_tree(package); source_before = inventory_tree(c['root'])
    def forbidden(*args, **kwargs):
        raise AssertionError('Live history reader must never run offline')
    for module in ('backup_history', 'backup_v2_history', 'backup_history_discovery'):
        monkeypatch.setattr('workbench.'+module+'.read_project_bytes', forbidden)
    with absent_source(c['root']):
        first = check(c, package); second = check(c, package)
    assert first == second and first['history'] == expected
    assert first['history_verified'] and not first['recoverable']
    assert not first['restore_plan_ready'] and not first['write_activation_allowed']
    assert inventory_tree(package) == before
    assert inventory_tree(c['root']) == source_before


@pytest.mark.parametrize('damage', ['marker', 'staged', 'manifest', 'orphan_registry'])
def test_valid_diagnostic_package_does_not_hide_invalid_history(archive_case, damage):
    c = archive_case; tx = legacy_transaction(c)
    if damage == 'orphan_registry':
        path = c['root']/'.novelagent/runtime/persistence/registry/completed/lost.json'
        path.parent.mkdir(parents=True)
        path.write_text('{}')
    elif damage == 'manifest':
        (tx.journal_dir/'manifest.json').unlink()
    else:
        (tx.journal_dir/('commit.marker' if damage == 'marker' else 'staged/000.bin')).write_bytes(b'{}')
    package = Path(capture(c)['path'])
    assert verify_archive(package)['archive_verified']
    before = inventory_tree(package)
    with absent_source(c['root']), pytest.raises((ValueError, RuntimeError)):
        check(c, package)
    assert inventory_tree(package) == before


@pytest.mark.parametrize('field', ['book_id', 'project_id'])
def test_offline_history_requires_explicit_matching_target(archive_case, field):
    c = archive_case; package = Path(capture(c)['path'])
    values = {'book_id': c['project'].book_id, 'project_id': c['project'].id}
    values[field] = 'wrong'
    with pytest.raises(BackupArchiveError, match='目标'):
        verify_archived_persistence_history(package, **values)


@pytest.mark.parametrize('damage', [None, 'old_marker', 'new_receipt'])
def test_actual_upgrade_and_reviewed_chapter_are_checked_offline_without_rewriting_history(mixed, software_tree, damage):
    c = mixed; assert transaction(c).commit()['committed']
    base = c['root'].parent; machine = base/'m'
    with BookOwnership(c['project'].book_id, machine_store=machine).locked() as owner:
        with c['db'].transaction() as connection:
            owner.enroll(root=c['root'], database=c['db'], project_id=c['project'].id, connection=connection)
    if damage == 'old_marker':
        (c['old_journal']/'commit.marker').write_text('{}')
    elif damage == 'new_receipt':
        (c['root']/'.novelagent/runtime/reviewed/receipts/upgrade-1.json').write_text('{}')
    with closing(InstanceLease(base).acquire()) as lease:
        gate = maintenance_for(lease); op = gate.begin()['operation_id']
        service = BackupArchiveService(projects=c['projects'], maintenance=gate, task_runtime_root=c['runtime'],
            archive_root=base/'archives', machine_store=machine, runtime_config={'workbench_version':'0.1.0-beta',
            'job_execution_contract':'workbench_execution_v1', 'worker_enabled':False, 'auto_import_active':False})
        package = Path(capture({**c, 'service':service, 'op':op})['path'])
        gate.finish(op)
    assert verify_archive(package)['archive_verified']
    before = inventory_tree(c['root']); archived_before = inventory_tree(package)
    with absent_source(c['root']):
        if damage:
            with pytest.raises((ValueError, RuntimeError)):
                check(c, package)
        else:
            value = check(c, package)
            assert len(value['history']['transactions']) == 3
            assert value['history']['consistent_reverse_order'][-1].endswith('/pre-upgrade')
    assert inventory_tree(c['root']) == before
    assert inventory_tree(package) == archived_before
