"""Real restore handoff with spending that happened AFTER the saved archive."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from core.story_project.reviewed_state_document import digest
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive_history import absent_source
from tests.test_backup_archive_sources import case, mixed
from tests.test_backup_restore_database import make_source_workbench, bound_copy, logical
from tests.test_model_usage_budget import seed
from tests.test_restore_handoff import restored, preview, begin, deny, ledger
from workbench.backup_archive_usage import inspect_archived_usage
from workbench.backup_files import inventory_tree
from workbench.restore_handoff import RestoreHandoffError
from workbench.restore_usage import RestoreUsage


@pytest.fixture(scope='module')
def full_source():
    c = make_source_workbench(mixed.__wrapped__(case.__wrapped__()))
    archived = inspect_archived_usage(c['package'], project_id=c['project'].id, book_id=c['project'].book_id)
    assert archived['usage']['summary']['attempt_count'] == 0
    # This module owns its source. Other restore modules' immutable sources are
    # never modified; no real Provider or real book is involved.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr('workbench.book_ownership.default_machine_store', lambda:c['machine'])
        seed(c['root'], book_id=c['project'].book_id, chapter=3, execution='execution_after_archive')
        seed(c['root'], book_id=c['project'].book_id, chapter=4, execution='execution_unsettled',
            fault='after_dispatch_claim_before_provider')
    return c


@pytest.fixture
def pending(restored):
    c = restored; value = preview(c); begin(c, value)
    return {**c, 'value':value, 'id':value['handoff_id'],
        'usage_service':RestoreUsage(c['project'].book_id, machine_store=c['store'])}


def saved_files(c):
    return {p.name:p.read_bytes() for p in c['owner'].directory.iterdir()
        if p.is_file() and (p.name.startswith('usage-') or p.name.startswith('handoff-'))}


def test_post_archive_spending_and_unknown_reservation_survive_source_loss(pending):
    c = pending; service = c['usage_service']
    source = inventory_tree(c['root']); target = inventory_tree(c['target']/'book')
    sql = logical(c['target']/'workbench.sqlite'); original_ledger = ledger(c)
    result = service.capture(handoff_id=c['id'])
    assert result['post_archive_usage_verified'] and not result['settled']
    assert not result['write_activation_allowed']
    usage = result['usage']
    assert usage['summary']['attempt_count'] == 2
    assert usage['summary']['costs_by_currency']['USD']['estimated'] == '0.000119'
    budgets = {item['chapter']:item for item in usage['chapter_budgets']}
    assert budgets[3]['charged_amount'] == '0.000119'
    assert budgets[4]['charged_amount'] == '0.0006'
    assert budgets[4]['unresolved_attempts'] == [['execution_unsettled', 'call-a1']]
    assert not result['budget_reconciliation_completed']
    assert 'chapter_budget_unsettled' in {item['code'] for item in result['blockers']}
    saved = saved_files(c)
    with absent_source(c['root']):
        assert service.read_usage(handoff_id=c['id']) == result
        assert service.capture(handoff_id=c['id']) == result
        assert service.resume_usage(handoff_id=c['id']) == result
        assert not c['root'].exists()
    assert saved_files(c) == saved
    assert inventory_tree(c['root']) == source and inventory_tree(c['target']/'book') == target
    assert logical(c['target']/'workbench.sqlite') == sql and ledger(c) == original_ledger
    deny(c['root']); deny(c['target']/'book')


def test_unavailable_source_is_durably_unknown_then_explicit_capture_can_preserve(pending):
    c = pending; service = c['usage_service']
    with absent_source(c['root']):
        unknown = service.capture(handoff_id=c['id'])
        assert unknown['usage_state'] == 'unknown' and unknown['usage'] is None
        assert not unknown['post_archive_usage_verified'] and not unknown['write_activation_allowed']
        assert service.capture(handoff_id=c['id']) == unknown
        assert service.read_usage(handoff_id=c['id']) == unknown
        assert not c['root'].exists()
        assert not service._path(c['id'], 'usage').exists()
    marker = service._path(c['id'], 'usage-unknown').read_bytes()
    result = service.capture(handoff_id=c['id'])
    assert result['usage']['summary']['attempt_count'] == 2
    assert service._path(c['id'], 'usage-unknown').read_bytes() == marker


@pytest.mark.parametrize('damage', ['missing_blob', 'changed_blob', 'changed_summary', 'missing_intent'])
def test_saved_summary_never_replaces_missing_or_changed_bytes(pending, damage):
    c = pending; service = c['usage_service']; service.capture(handoff_id=c['id'])
    intent = json.loads(service._path(c['id'], 'usage').read_text(encoding='utf-8'))
    record = next(record for name, record in intent['tree']['files'].items() if name.endswith('/ledger.json'))
    blob = c['owner'].directory/('usage-'+record['sha256']+'.bin')
    if damage == 'missing_blob': blob.unlink()
    elif damage == 'changed_blob': blob.write_bytes(b'changed')
    elif damage == 'missing_intent': service._path(c['id'], 'usage').unlink()
    else:
        path = service._path(c['id'], 'usage-done')
        value = json.loads(path.read_text(encoding='utf-8'))
        value['usage']['summary']['attempt_count'] = 0
        value['receipt_sha256'] = digest({k:v for k,v in value.items() if k != 'receipt_sha256'})
        path.write_text(json.dumps(value), encoding='utf-8')
    before = saved_files(c)
    with absent_source(c['root']), pytest.raises((RestoreHandoffError, FileNotFoundError)):
        service.capture(handoff_id=c['id'])
    assert saved_files(c) == before


@pytest.mark.parametrize('phase', ['after_usage_intent', 'after_usage_blob', 'before_usage_completion', 'after_usage_completion'])
def test_real_process_crash_requires_same_intent_and_preserves_consumption(pending, phase):
    c = pending
    code = '''import os,sys
from workbench.restore_usage import RestoreUsage
def fault(phase,value):
    if phase == sys.argv[4]: os._exit(85)
RestoreUsage(sys.argv[1],machine_store=sys.argv[2],fault_injector=fault).capture(handoff_id=sys.argv[3])
'''
    child = subprocess.run([sys.executable, '-c', code, c['project'].book_id, str(c['store']), c['id'], phase],
        cwd=Path.cwd(), capture_output=True, text=True, encoding='utf-8', timeout=90)
    assert child.returncode == 85, child.stdout+child.stderr
    intent = c['usage_service']._path(c['id'], 'usage').read_bytes()
    if phase != 'after_usage_completion':
        with pytest.raises(RestoreHandoffError, match='显式续接'):
            c['usage_service'].capture(handoff_id=c['id'])
    result = c['usage_service'].resume_usage(handoff_id=c['id'])
    assert result['usage']['summary']['attempt_count'] == 2
    assert c['usage_service']._path(c['id'], 'usage').read_bytes() == intent
    assert ledger(c) == c['value']['restoring_record'] and not result['write_activation_allowed']
    with absent_source(c['root']): assert c['usage_service'].read_usage(handoff_id=c['id']) == result


def test_rollback_invalidates_completed_usage_for_write_preparation(pending):
    c = pending; c['usage_service'].capture(handoff_id=c['id'])
    c['handoff'].rollback(handoff_id=c['id'])
    before = saved_files(c)
    with pytest.raises(RestoreHandoffError): c['usage_service'].read_usage(handoff_id=c['id'])
    assert saved_files(c) == before


def test_source_provider_lock_loss_never_recreates_a_lock_or_source_directory(pending):
    c = pending; service = c['usage_service']; before = saved_files(c)
    lock_root = c['root']/'.novelagent/runtime/runs/.provider-boundary-lock'
    with absent_source(lock_root, temporary_name='provider-offline'):
        with pytest.raises((OSError, RuntimeError, ValueError)):
            service.capture(handoff_id=c['id'])
        assert not lock_root.exists()
    assert saved_files(c) == before


@pytest.mark.parametrize('change', ['source', 'machine'])
def test_changed_source_or_machine_before_completion_never_receives_success(pending, change):
    from core.engine.persistence import atomic_write_json
    from workbench.restore_handoff import _signed
    c = pending; note = c['root']/'usage-change-probe.txt'
    assert note.is_relative_to((Path.cwd()/'.tmp').resolve()) and not note.exists()
    def fault(phase, value):
        if phase == 'before_usage_completion':
            if change == 'source': note.write_text('changed while saving', encoding='utf-8')
            else: atomic_write_json(c['owner'].record_path,
                _signed({**value['restoring_record'], 'generation':value['restoring_record']['generation']+1}))
    service = RestoreUsage(c['project'].book_id, machine_store=c['store'], fault_injector=fault)
    try:
        with pytest.raises(RestoreHandoffError): service.capture(handoff_id=c['id'])
        assert not service._path(c['id'], 'usage-done').exists()
        before = saved_files(c)
        with pytest.raises(RestoreHandoffError): c['usage_service'].resume_usage(handoff_id=c['id'])
        assert saved_files(c) == before
    finally:
        if note.exists(): note.unlink()


@pytest.mark.parametrize('stage', ['source', 'saved'])
def test_file_growth_between_inventory_and_open_is_bounded(pending, monkeypatch, stage):
    from workbench import restore_usage as module
    c = pending; service = c['usage_service']; name = '.novelagent/project.json'
    if stage == 'saved':
        service.capture(handoff_id=c['id'])
        intent = json.loads(service._path(c['id'], 'usage').read_text(encoding='utf-8'))
        target = c['owner'].directory/('usage-'+intent['tree']['files'][name]['sha256']+'.bin')
    else: target = c['root']/name
    assert target.is_relative_to((Path.cwd()/'.tmp').resolve())
    original_bytes = target.read_bytes(); stream = module._stream_member; changed = False
    def grow(resolver, relative, **kwargs):
        nonlocal changed
        actual = resolver.resolve({'root_id':'external:backup_source','relative_path':relative}).path
        if actual == target and not changed:
            changed = True
            target.write_bytes(original_bytes+b' ')
        return stream(resolver, relative, **kwargs)
    monkeypatch.setattr(module, '_stream_member', grow)
    try:
        with pytest.raises(RestoreHandoffError, match='超过保存清单大小'):
            if stage == 'source': service.capture(handoff_id=c['id'])
            else: service.read_usage(handoff_id=c['id'])
        assert changed
        if stage == 'source': assert not service._path(c['id'], 'usage-done').exists()
    finally:
        if stage == 'source': target.write_bytes(original_bytes)
