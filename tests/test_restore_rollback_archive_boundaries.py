"""Missing or mismatched ancestry never supplies an old archive's authority."""
import json

import pytest

from core.engine.persistence import atomic_write_json
from tests.test_restore_rollback_archive import (
    tmp_path, private_temporary_directory, legacy, registration_case, owned_archive,
    copied, start, rolled, inventory_tree, logical,
)
from workbench.restore_handoff import _signed
from workbench.restore_rollback_ancestry import rollback_receipt, rollback_receipt_name


def snapshot(c, target):
    return {'source': inventory_tree(c['root']), 'source_sql': logical(c['app'].state.database.path),
        'target': inventory_tree(target/'book'), 'target_sql': logical(target/'workbench.sqlite'),
        # InstanceLease publishes a fresh operational owner token on every
        # real locked read; it is not an ownership/ancestry control record.
        'machine': {p.name: p.read_bytes() for p in c['owner'].directory.glob('*.json')
            if p.name != 'workbench-instance.json'}}


@pytest.mark.parametrize('damage', [
    'missing_completion', 'missing_handoff', 'missing_decision', 'changed_completion',
    'changed_decision', 'numeric_generation', 'changed_registration', 'changed_identity',
])
def test_old_archive_rejects_lost_or_rehashed_foreign_proof_without_repair(owned_archive, damage):
    c = owned_archive; target = copied(c); value, result = rolled(c, target)
    service = c['service']; owner = c['owner']; record = result['source_record']
    receipt_path = owner.directory/rollback_receipt_name(record)
    if damage == 'missing_completion': receipt_path.unlink()
    elif damage == 'missing_handoff': service._path(value['handoff_id']).unlink()
    elif damage == 'missing_decision': service._path(value['handoff_id'], 'rollback').unlink()
    elif damage == 'changed_completion':
        receipt = rollback_receipt(value)
        atomic_write_json(receipt_path, _signed({**receipt, 'decision_sha256': '0'*64}, 'receipt_sha256'))
    elif damage == 'changed_decision':
        path = service._path(value['handoff_id'], 'rollback'); decision = json.loads(path.read_bytes())
        decision['record'] = _signed({**record, 'generation': record['generation']+2})
        atomic_write_json(path, _signed(decision, 'decision_sha256'))
    else:
        if damage == 'numeric_generation': record = _signed({**record, 'generation': record['generation']+2})
        elif damage == 'changed_registration': record = _signed({**record, 'registration_id': '0'*32})
        else: record = _signed({**record, 'binding': {**record['binding'], 'identity_sha256': '0'*64}})
        atomic_write_json(owner.record_path, record)
    before = snapshot(c, target)
    with pytest.raises((OSError, ValueError, RuntimeError)):
        service.preview(c['owned_package'], destination=target, temporary_root=c['scratch'])
    assert snapshot(c, target) == before


@pytest.mark.parametrize('phase', ['before_begin', 'after_begin'])
def test_ancestral_completion_loss_invalidates_preview_or_pending_actions(owned_archive, phase):
    c = owned_archive; target = copied(c); first, result = rolled(c, target)
    service = c['service']; package = c['owned_package']
    value = service.preview(package, destination=target, temporary_root=c['scratch'])
    if phase == 'after_begin':
        service.begin(package, destination=target, expected_preview_sha256=value['intent_sha256'],
            temporary_root=c['scratch'])
    receipt_path = c['owner'].directory/rollback_receipt_name(result['source_record'])
    receipt_path.unlink()
    before = snapshot(c, target)
    if phase == 'before_begin':
        with pytest.raises((OSError, ValueError, RuntimeError)):
            service.begin(package, destination=target, expected_preview_sha256=value['intent_sha256'],
                temporary_root=c['scratch'])
        assert not service._path(value['handoff_id']).exists()
    else:
        actions = [
            lambda: service.resume(package, handoff_id=value['handoff_id'], temporary_root=c['scratch']),
            lambda: service.capture(handoff_id=value['handoff_id']),
            lambda: service.rollback(handoff_id=value['handoff_id']),
        ]
        for action in actions:
            with pytest.raises((OSError, ValueError, RuntimeError)):
                action()
            assert snapshot(c, target) == before
    assert snapshot(c, target) == before and not receipt_path.exists()


def test_corrupt_rollback_completion_is_rejected_before_reactivation(owned_archive):
    c = owned_archive; target = copied(c); value = start(c, target)
    service = c['service']; receipt = rollback_receipt(value)
    from workbench.restore_handoff import rollback_decision
    path = c['owner'].directory/rollback_receipt_name(rollback_decision(value)['record'])
    atomic_write_json(path, _signed({**receipt, 'source_reactivated': False}, 'receipt_sha256'))
    before = snapshot(c, target)
    with pytest.raises((OSError, ValueError, RuntimeError)):
        service.rollback(handoff_id=value['handoff_id'])
    assert snapshot(c, target) == before
