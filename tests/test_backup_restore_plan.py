"""Plan from real archive proofs; inspect new parents without creating a restore."""
from pathlib import Path
import uuid
import os

import pytest

from tests.platform_requirements import require_windows_recovery
from tests.test_backup_archive import capture, software_tree
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive_executions import execution_archive
from tests.test_backup_archive_history import absent_source
from tests.test_backup_archive_sources import case, mixed, reviewed_archive
from workbench.backup_archive import BackupArchiveError
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_files import inventory_tree
from workbench.backup_restore_plan import plan_archived_restore, _target_layout


def plan(c, package, destination, scratch):
    require_windows_recovery()
    return plan_archived_restore(package, project_id=c['project'].id, book_id=c['project'].book_id,
        destination=destination, temporary_root=scratch)


def test_mixed_reviewed_plan_is_stable_and_preserves_historical_bindings(
        reviewed_archive, private_temporary_directory):
    c = reviewed_archive
    package = Path(capture(c)['path'])
    before = inventory_tree(package)
    source_before = inventory_tree(c['root'])
    destination = Path('.tmp').resolve() / ('rp-' + uuid.uuid4().hex[:6])
    with absent_source(c['root']):
        first = plan(c, package, destination, private_temporary_directory)
        second = plan(c, package, destination, private_temporary_directory)
    assert first == second
    assert first['plan_ready'], first['blockers']
    assert first['historical_registries']
    assert all(r['registry_id'] and r['registry_digest'] and r['sha256'] for r in first['historical_registries'])
    for registry in first['historical_registries']:
        for root in registry['roots']:
            assert root['root_uuid'] == root['historical_binding']['root_uuid']
            assert Path(root['destination']).is_relative_to(destination / 'book')
    assert first['archive_binding']['archive']['sha256']
    assert not first['recoverable'] and not first['write_activation_allowed'] and not first['restored']
    assert not first['other_project_isolation_enforced']
    assert not destination.exists()
    assert inventory_tree(package) == before and inventory_tree(c['root']) == source_before


def test_parent_identity_and_destination_changes_invalidate_layout(execution_archive):
    require_windows_recovery()
    c = execution_archive
    package = Path(capture(c)['path'])
    parent = Path('.tmp').resolve() / ('p-' + uuid.uuid4().hex[:6])
    parent.mkdir()
    destination = parent / '恢复 一'
    with verified_archive_view(package) as view:
        first = _target_layout(view, destination, package, registered_roots=[str(c['root'])])
        moved = parent.with_name(parent.name + '-old')
        workspace = Path('.tmp').resolve()
        assert parent.resolve().is_relative_to(workspace) and moved.resolve().is_relative_to(workspace)
        parent.rename(moved)
        parent.mkdir()
        second = _target_layout(view, destination, package, registered_roots=[str(c['root'])])
        third = _target_layout(view, parent / '恢复 二', package, registered_roots=[str(c['root'])])
    assert first['parent_binding'] != second['parent_binding']
    assert second['container'] != third['container']
    assert not destination.exists() and not (parent / '恢复 二').exists()


@pytest.mark.parametrize('bad', ['relative', 'traversal', 'source', 'source_parent', 'archive',
                                 'existing', 'unbacked_project', 'long', 'reserved'])
def test_unsafe_restore_destinations_are_rejected_before_creation(execution_archive, bad):
    c = execution_archive
    package = Path(capture(c)['path'])
    scratch = Path('.tmp').resolve()
    other = scratch / ('other-' + uuid.uuid4().hex[:6])
    destinations = {
        'relative':Path('restore'), 'traversal':scratch / '..' / 'restore',
        'source':c['root'] / 'new', 'source_parent':c['root'].parent,
        'archive':package / 'new', 'existing':scratch,
        'unbacked_project':other / 'restore', 'long':scratch / ('长' * 200),
        'reserved':scratch / 'CON',
    }
    with verified_archive_view(package) as view, pytest.raises((ValueError, RuntimeError, OSError)):
        _target_layout(view, destinations[bad], package, registered_roots=[str(c['root']), str(other)])
    assert not other.exists()


def test_configured_in_book_job_runtime_keeps_its_relative_binding(execution_archive):
    require_windows_recovery()
    c = execution_archive
    runtime = c['root'] / '.novelagent/runtime/jobs'
    c['service'].task_runtime_root = runtime
    package = Path(capture(c)['path'])
    destination = Path('.tmp').resolve() / ('ri-' + uuid.uuid4().hex[:6])
    with verified_archive_view(package) as view:
        layout = _target_layout(view, destination, package, registered_roots=[str(c['root'])])
    assert layout['task_runtime_root'] == str(destination / 'book/.novelagent/runtime/jobs')
    assert not destination.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX host refusal")
def test_posix_restore_destination_is_refused_without_creating_files(tmp_path):
    destination = tmp_path / "new-book"
    with pytest.raises(BackupArchiveError, match="本机盘符"):
        _target_layout(None, destination, tmp_path, registered_roots=[])
    assert not destination.exists()
