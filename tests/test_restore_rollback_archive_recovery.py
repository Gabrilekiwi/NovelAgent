"""Real exit receipts and multiple genuine source rollback generations."""
import json
import subprocess
import sys

import pytest

from tests.test_restore_rollback_archive import (
    tmp_path, private_temporary_directory, legacy, registration_case, owned_archive,
    copied, start, rolled, inventory_tree, logical, contents,
)
from workbench.restore_handoff import RestoreHandoff, rollback_decision
from workbench.restore_rollback_ancestry import rollback_receipt, rollback_receipt_name


@pytest.mark.parametrize('phase', ['after_rollback_intent', 'after_source_reactivated', 'after_rollback_completion'])
def test_actual_exit_resumes_original_rollback_then_accepts_original_archive(owned_archive, phase):
    c = owned_archive; target = copied(c); value = start(c, target)
    before_book = inventory_tree(c['root']); before_sql = logical(c['app'].state.database.path)
    before_target = inventory_tree(target/'book'); before_target_sql = logical(target/'workbench.sqlite')
    before_archive = contents(c['owned_package'])
    code = '''import os,sys
from workbench.restore_handoff import RestoreHandoff
def fault(event,value):
 if event==sys.argv[4]:os._exit(88)
RestoreHandoff(sys.argv[1],machine_store=sys.argv[2],fault_injector=fault).rollback(handoff_id=sys.argv[3])
'''
    result = subprocess.run([sys.executable, '-B', '-c', code, c['project'].book_id,
        str(c['machine']), value['handoff_id'], phase], capture_output=True, timeout=90,
        **({'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}))
    assert result.returncode == 88, result.stderr
    expected = rollback_decision(value)['record']
    path = c['owner'].directory/rollback_receipt_name(expected)
    if phase == 'after_source_reactivated':
        assert not path.exists()
        with pytest.raises((OSError, ValueError, RuntimeError)):
            c['service'].preview(c['owned_package'], destination=target, temporary_root=c['scratch'])
        assert not path.exists()
    service = RestoreHandoff(c['project'].book_id, machine_store=c['machine'])
    resumed = service.rollback(handoff_id=value['handoff_id'])
    assert resumed['source_record'] == expected and json.loads(path.read_bytes()) == rollback_receipt(value)
    assert service.rollback(handoff_id=value['handoff_id']) == resumed
    assert inventory_tree(c['root']) == before_book and logical(c['app'].state.database.path) == before_sql
    assert inventory_tree(target/'book') == before_target and logical(target/'workbench.sqlite') == before_target_sql
    assert contents(c['owned_package']) == before_archive
    next_value = c['service'].preview(c['owned_package'], destination=target, temporary_root=c['scratch'])
    assert next_value['source_record'] == expected and len(next_value['source_rollback_ancestry']) == 1
    c['service'].begin(c['owned_package'], destination=target,
        expected_preview_sha256=next_value['intent_sha256'], temporary_root=c['scratch'])
    assert c['service'].resume(c['owned_package'], handoff_id=next_value['handoff_id'],
        temporary_root=c['scratch'])['source_revoked']
    with pytest.raises((OSError, ValueError, RuntimeError)):
        service.rollback(handoff_id=value['handoff_id'])


def test_two_actual_rollbacks_keep_complete_ordered_ancestry_and_activate(owned_archive):
    c = owned_archive; target = copied(c); service = c['service']; package = c['owned_package']
    before_book = inventory_tree(c['root']); before_sql = logical(c['app'].state.database.path)
    before_archive = contents(package)
    first, _ = rolled(c, target)
    second, result = rolled(c, target)
    assert second['source_record']['generation'] == 3 and result['source_record']['generation'] == 5
    third = start(c, target)
    assert third['source_record'] == result['source_record']
    assert third['archived_source_record'] == first['source_record']
    chain = third['source_rollback_ancestry']
    assert [item['handoff_intent']['handoff_id'] for item in chain] == [first['handoff_id'], second['handoff_id']]
    assert chain[1]['handoff_intent']['source_rollback_ancestry'] == chain[:1]
    assert service.capture(handoff_id=third['handoff_id'])['settled']
    for method in (service.prepare_budget, service.prepare_jobs, service.prepare_executions):
        method(package, handoff_id=third['handoff_id'], temporary_root=c['scratch'])
    assert service.activate(package, handoff_id=third['handoff_id'], temporary_root=c['scratch'])['target_writable']
    assert inventory_tree(c['root']) == before_book and logical(c['app'].state.database.path) == before_sql
    assert contents(package) == before_archive
