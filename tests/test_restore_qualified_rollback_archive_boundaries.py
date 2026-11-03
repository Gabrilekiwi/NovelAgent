"""Original machine proof loss must not silently repair or revoke a source."""
import pytest

from tests.test_restore_qualified_rollback_archive import (
    tmp_path, original, case, restored_client, begin_second, unchanged,
)
from tests.test_recovery_sessions import start, advance, step
from tests.test_backup_restore_database import logical
from workbench.backup_files import inventory_tree
from workbench.book_ownership import BookOwnership
from workbench.restore_activation import RestoreActivation
from workbench.restore_handoff import RestoreHandoffError, rollback_decision
from workbench.restore_reinstatement import reinstatement_name


def controls(owner):
    return {path.name:path.read_bytes() for path in owner.directory.glob('*.json')
        if path.name != 'workbench-instance.json'}


def test_qualified_old_archive_requires_original_completed_machine_files_before_and_after_begin(case):
    c = case; owner = BookOwnership(c['project'].book_id, machine_store=c['machine'])
    package = inventory_tree(c['rebackup'])
    with restored_client(c) as (app, client):
        first = advance(client, begin_second(c, client), 'source_revoked')
        _, state = app.state.recovery_sessions._load(first['session_id'])
        first_handoff = state['receipts']['preview_handoff']
        response = step(client, first, action='rollback')
        assert response.status_code == 200, response.text
        assert client.post('/api/maintenance/finish', json={'operation_id':first['operation_id']}).status_code == 200
    current = rollback_decision(first_handoff)['record']
    service = RestoreActivation(c['project'].book_id, machine_store=c['machine'])
    target = c['target'].parent/'续接证明 检查副本'
    with restored_client(c) as (app, client):
        session = advance(client, start(client, {**c, 'package':c['rebackup']}, target), 'readonly_ready')
        machine_before = controls(owner)
        target_before = inventory_tree(target/'book'), logical(target/'workbench.sqlite')
        paths = [owner.directory/reinstatement_name(current),
            owner.directory/reinstatement_name(current, done=True),
            service._path(first_handoff['handoff_id']),
            service._path(first_handoff['handoff_id'], 'rollback')]
        for path in paths:
            raw = path.read_bytes()
            try:
                path.unlink()
                expected = controls(owner)
                with pytest.raises(RestoreHandoffError):
                    service.preview(c['rebackup'], destination=target)
                assert controls(owner) == expected and not path.exists()
                assert (inventory_tree(target/'book'), logical(target/'workbench.sqlite')) == target_before
            finally:
                path.write_bytes(raw)
        assert controls(owner) == machine_before
        session = advance(client, session, 'handoff_ready')
        _, state = app.state.recovery_sessions._load(session['session_id'])
        handoff = state['receipts']['preview_handoff']
        assert handoff['schema_version'] == 'machine_restore_handoff_v4'

        receipt = owner.directory/reinstatement_name(current, done=True)
        raw = receipt.read_bytes()
        try:
            receipt.unlink()
            expected = controls(owner)
            rejected = step(client, session)
            assert rejected.status_code == 409, rejected.text
            assert controls(owner) == expected and not receipt.exists()
            assert (inventory_tree(target/'book'), logical(target/'workbench.sqlite')) == target_before
        finally:
            receipt.write_bytes(raw)
        session = client.get('/api/recovery-sessions/'+session['session_id']).json()
        response = step(client, session, action='handoff', resume=session['pending_action'] == 'handoff')
        assert response.status_code == 200, response.text
        session = response.json()

        raw = receipt.read_bytes()
        try:
            receipt.unlink()
            expected = controls(owner)
            for operation in (
                lambda: service.resume(c['rebackup'], handoff_id=handoff['handoff_id']),
                lambda: service.capture(handoff_id=handoff['handoff_id']),
                lambda: service.rollback(handoff_id=handoff['handoff_id'], source_lease=app.state.maintenance.lease),
            ):
                with pytest.raises(RestoreHandoffError): operation()
                assert controls(owner) == expected and not receipt.exists()
            assert (inventory_tree(target/'book'), logical(target/'workbench.sqlite')) == target_before
        finally:
            receipt.write_bytes(raw)
        response = step(client, session, action='rollback')
        assert response.status_code == 200, response.text
        assert client.post('/api/maintenance/finish', json={'operation_id':session['operation_id']}).status_code == 200
    unchanged(c)
    assert inventory_tree(c['rebackup']) == package
