"""Reuse an actual normally registered archive after explicit source rollback."""
import json
from pathlib import Path
import uuid

from fastapi.testclient import TestClient
import pytest

from tests.test_legacy_registration import (
    tmp_path, private_temporary_directory, legacy, registration_case,
    endpoint, preview, body, contents, capture, logical, prepare,
)
from workbench.app import create_app
from workbench.backup_files import inventory_tree
from workbench.backup_restore_database import prepare_restored_workbench
from workbench.backup_restore_materialization import materialize_archived_restore
from workbench.backup_restore_roots import bind_restored_roots
from workbench.book_ownership import BookOwnership
from workbench.restore_activation import RestoreActivation
from workbench.restore_scope import load_restore_scope


@pytest.fixture
def owned_archive(registration_case):
    c = registration_case
    value = preview(c)
    result = c['client'].post(endpoint(c), json=body(c, value))
    assert result.status_code == 200, result.text
    package = Path(capture({'service': c['app'].state.backups,
        'op': c['op'], 'project': c['project']})['path'])
    c['app'].state.maintenance.finish(c['op'])
    owner = BookOwnership(c['project'].book_id, machine_store=c['machine'])
    return {**c, 'owned_package': package, 'owner': owner,
        'service': RestoreActivation(c['project'].book_id, machine_store=c['machine'])}


def copied(c):
    target = Path(__file__).resolve().parents[1]/'.tmp'/('rba-'+uuid.uuid4().hex[:8])
    package = c['owned_package']
    plan = prepare({'project': c['project']}, package, target, c['scratch'])
    materialize_archived_restore(package, plan=plan, temporary_root=c['scratch'])
    bind_restored_roots(package, destination=target, temporary_root=c['scratch'])
    prepare_restored_workbench(package, destination=target, temporary_root=c['scratch'])
    return target


def start(c, target):
    service = c['service']; package = c['owned_package']
    value = service.preview(package, destination=target, temporary_root=c['scratch'])
    result = service.begin(package, destination=target,
        expected_preview_sha256=value['intent_sha256'], temporary_root=c['scratch'])
    assert result['source_revoked'] and not result['target_writable']
    return value


def rolled(c, target):
    value = start(c, target)
    result = c['service'].rollback(handoff_id=value['handoff_id'])
    assert result['rolled_back'] and not result['target_writable']
    return value, result


@pytest.mark.parametrize('registration_case', ['1.0', '2.0'], indirect=True)
def test_normal_archive_can_be_reused_after_actual_rollback_and_target_restart(owned_archive):
    c = owned_archive; service = c['service']; package = c['owned_package']
    source_before = inventory_tree(c['root'])
    database_before = logical(c['app'].state.database.path)
    package_before = contents(package)
    first_target = copied(c)
    first, rollback = rolled(c, first_target)
    assert rollback['source_record']['generation'] == 3
    second_target = copied(c)
    second = start(c, second_target)
    assert second['source_record'] == rollback['source_record']
    assert second['archived_source_record'] == first['source_record']
    assert len(second['source_rollback_ancestry']) == 1
    assert service.resume(package, handoff_id=second['handoff_id'],
        temporary_root=c['scratch'])['source_revoked']
    assert service.capture(handoff_id=second['handoff_id'])['settled']
    for method in (service.prepare_budget, service.prepare_jobs, service.prepare_executions):
        method(package, handoff_id=second['handoff_id'], temporary_root=c['scratch'])
    assert service.activate(package, handoff_id=second['handoff_id'],
        temporary_root=c['scratch'])['target_writable']
    assert load_restore_scope(second_target/'workbench.sqlite', machine_store=c['machine']).active
    for _ in range(2):
        app = create_app(database_path=second_target/'workbench.sqlite', start_worker=True,
            auto_import_active=True, chapter_publication_machine_store=c['machine'])
        with TestClient(app) as client:
            assert app.state.supervisor._thread is None
            response = client.get('/api/projects/'+c['project'].id+'/chapters/2')
            assert response.status_code == 200, response.text
            assert response.json()['retained_author_draft']['text'] == c['draft'].text
            assert not response.json()['has_prose']
    assert inventory_tree(c['root']) == source_before
    assert logical(c['app'].state.database.path) == database_before
    assert contents(package) == package_before
