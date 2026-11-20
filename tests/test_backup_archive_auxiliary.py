"""Actual archived controls are checked with the live source absent and no recovery."""
from pathlib import Path
import uuid

import pytest

from core.delivery import DeliveryQueue
from tests.test_backup_archive import archive_case,legacy,software_tree,capture
from tests.test_backup_archive_history import absent_source
from tests.test_backup_auxiliary_settlement import queue_case,patch_json
from tests.test_backup_autonomy_settlement import seed,forbid_recovery
from workbench.backup_archive import BackupArchiveError
from workbench.backup_archive_auxiliary import inspect_archived_auxiliary
from workbench.backup_files import inventory_tree


@pytest.fixture
def tmp_path():
    # Full autonomy event paths must fit the supported Windows path budget.
    path=Path.cwd()/'.tmp'/('a71-'+uuid.uuid4().hex[:12]);path.mkdir()
    return path


def inspect(c,package):
    return inspect_archived_auxiliary(package,project_id=c['project'].id,book_id=c['project'].book_id)


def forbid_live_reads(monkeypatch):
    forbid_recovery(monkeypatch)
    def reject(*args,**kwargs):pytest.fail('archive inspection opened a live control store or source')
    monkeypatch.setattr(DeliveryQueue,'__init__',reject)
    monkeypatch.setattr('workbench.backup_auxiliary.read_project_bytes',reject)


@pytest.mark.parametrize('layout',['canonical','custom','pending_delivery'])
def test_archived_settled_controls_keep_pending_work_without_live_source(archive_case,monkeypatch,layout):
    c=archive_case;custom=layout=='custom'
    queue_case(c,custom=custom,delivered=layout!='pending_delivery')
    seed(c,custom=custom)
    package=Path(capture(c)['path']);before=inventory_tree(package);source_before=inventory_tree(c['root'])
    forbid_live_reads(monkeypatch)
    with absent_source(c['root']):first=inspect(c,package);second=inspect(c,package)
    assert first==second and first['auxiliary_settled'] and not first['blockers']
    assert {item['kind'] for item in first['evidence']['stores']}=={'delivery','autonomy'}
    assert not first['restore_plan_ready'] and not first['recoverable']
    assert not first['evidence']['complete_history_certified']
    assert inventory_tree(package)==before and inventory_tree(c['root'])==source_before


@pytest.mark.parametrize('damage',['delivery_active','missing_receipt','wrong_receipt_binding',
    'autonomy_active','missing_lease_head','missing_latest','missing_operation_result'])
def test_archived_unsettled_or_incomplete_controls_are_never_repaired(archive_case,monkeypatch,damage):
    c=archive_case
    if damage.startswith('delivery') or damage in {'missing_receipt','wrong_receipt_binding'}:
        queue=queue_case(c,delivered=damage!='delivery_active')
        if damage=='delivery_active':queue._claim('delivery-1','stopped-worker')
        elif damage=='missing_receipt':next(queue.attempts_dir.rglob('*.json')).unlink()
        else:patch_json(queue.jobs_dir/'delivery-1.json',lambda x:x['last_attempt_receipt'].update(attempt_receipt_hash='b'*64))
        expected='delivery_not_settled' if damage=='delivery_active' else (
            'delivery_attempt_evidence_incomplete' if damage=='missing_receipt' else 'delivery_control_evidence_unverified')
    else:
        store,_=seed(c,state='active' if damage=='autonomy_active' else 'abandoned')
        if damage=='missing_lease_head':next((store.root/'leases').rglob('current.json')).unlink()
        elif damage=='missing_latest':(store.root/'sessions/latest.json').unlink()
        elif damage=='missing_operation_result':next((store.root/'operations').rglob('result.json')).unlink()
        expected={'autonomy_active':'autonomy_active_lease','missing_lease_head':'autonomy_lease_head_requires_recovery',
            'missing_latest':'autonomy_latest_requires_recovery','missing_operation_result':'autonomy_operation_not_settled'}[damage]
    package=Path(capture(c)['path']);before=inventory_tree(package);source_before=inventory_tree(c['root'])
    forbid_live_reads(monkeypatch)
    with absent_source(c['root']):result=inspect(c,package)
    assert not result['auxiliary_settled']
    assert expected in {b['code'] for b in result['blockers']},result['blockers']
    assert inventory_tree(package)==before and inventory_tree(c['root'])==source_before


def test_archived_auxiliary_requires_explicit_target_binding(archive_case):
    c=archive_case;package=Path(capture(c)['path'])
    with pytest.raises(BackupArchiveError,match='目标与作品身份不同'):
        inspect_archived_auxiliary(package,project_id=c['project'].id,book_id='wrong-book')
