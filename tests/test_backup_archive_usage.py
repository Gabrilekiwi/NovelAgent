"""Real local call/budget records, inspected offline with no Provider requests."""
from pathlib import Path
import json

import pytest

from core.chapter_budget_freeze import ChapterBudgetFreezeRegistry
from core.chapter_budget_store import ChapterBudgetStore
from core.chapter_execution_time import ChapterExecutionClock
from core.model_calls import ModelCallStore
from core.memory_v2.canonical import canonical_json_hash
from tests.test_backup_archive import archive_case,legacy,software_tree,capture
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_history import absent_source
from tests.test_model_usage_budget import seed
from workbench.backup_archive_usage import inspect_archived_usage
from workbench.backup_files import inventory_tree
from workbench.model_usage import read_project_model_usage


def inspect(c,package):
    return inspect_archived_usage(package,project_id=c['project'].id,book_id=c['project'].book_id)


def forbid_runtime(monkeypatch):
    def reject(*args,**kwargs):pytest.fail('offline usage opened live source or a runtime store')
    for cls in (ChapterBudgetFreezeRegistry,ChapterBudgetStore,ChapterExecutionClock,ModelCallStore):
        monkeypatch.setattr(cls,'__init__',reject)
    monkeypatch.setattr('workbench.backup_auxiliary.read_project_bytes',reject)
    monkeypatch.setattr('core.chapter_budget_store.read_project_bytes',reject)
    monkeypatch.setattr('core.chapter_execution_time.read_project_bytes',reject)
    monkeypatch.setattr('workbench.model_usage_budget.read_project_bytes',reject)


@pytest.mark.parametrize('semantics',['input_includes_cache','input_excludes_cache'])
def test_archived_usage_matches_live_frozen_costs_without_source_or_store_initialization(archive_case,monkeypatch,semantics):
    c=archive_case;seed(c['root'],book_id=c['project'].book_id,semantics=semantics)
    expected=read_project_model_usage(c['root'],book_id=c['project'].book_id)
    package=Path(capture(c)['path']);before=inventory_tree(package);source_before=inventory_tree(c['root'])
    forbid_runtime(monkeypatch)
    with absent_source(c['root']):first=inspect(c,package);second=inspect(c,package)
    assert first==second and first['model_and_budgets_verified'],first['blockers']
    for key in ('items','summary','chapter_budgets'):assert first['usage'][key]==expected[key]
    assert first['usage']['chapter_times'][0]['active_time_complete']
    assert not first['restore_plan_ready'] and not first['recoverable']
    assert inventory_tree(package)==before and inventory_tree(c['root'])==source_before


@pytest.mark.parametrize('damage',['binding_missing','binding_other','ledger_missing','ledger_hash',
    'receipt_missing','response_missing','response_hash','ready_missing','intent_missing','time_missing'])
def test_archived_budget_and_response_damage_rejects_full_usage_proof(archive_case,monkeypatch,damage):
    c=archive_case;ledger,calls,_=seed(c['root'],book_id=c['project'].book_id)
    if damage=='binding_missing':(calls.root/'b.json').unlink()
    elif damage=='binding_other':
        path=calls.root/'b.json';value=json.loads(path.read_bytes());value['book_id']='other-book'
        value['binding_sha256']=canonical_json_hash(value,exclude_fields=('binding_sha256',),exclude_environment_fields=False)
        path.write_text(json.dumps(value))
    elif damage=='ledger_missing':(ledger.root/'ledger.json').unlink()
    elif damage=='ledger_hash':(ledger.root/'ledger.json').write_text('{}')
    elif damage=='receipt_missing':calls.receipt_path('call-a1').unlink()
    elif damage.startswith('response_'):
        path=calls.root/calls.load_receipt('call-a1')['response_artifact_ref']
        if damage=='response_missing':path.unlink()
        else:path.write_text('changed')
    elif damage=='ready_missing':(ledger.root.parent/'r2.json').unlink()
    elif damage=='intent_missing':calls.intent_path('call-a1').unlink()
    else:(ledger.root/'time.json').unlink()
    package=Path(capture(c)['path']);before=inventory_tree(package)
    forbid_runtime(monkeypatch)
    with absent_source(c['root']):result=inspect(c,package)
    assert not result['model_and_budgets_verified'] and result['usage'] is None
    assert 'model_or_budget_evidence_unverified' in {b['code'] for b in result['blockers']}
    assert inventory_tree(package)==before


@pytest.mark.parametrize('fault',['after_chapter_reservation_before_intent','after_intent_before_dispatch',
    'after_dispatch_claim_before_provider','after_receipt_before_chapter_settlement'])
def test_archived_crash_windows_keep_unsettled_occupancy_without_reconciliation(archive_case,monkeypatch,fault):
    c=archive_case;seed(c['root'],book_id=c['project'].book_id,fault=fault)
    package=Path(capture(c)['path']);before=inventory_tree(package)
    forbid_runtime(monkeypatch)
    with absent_source(c['root']):result=inspect(c,package)
    assert not result['model_and_budgets_verified'] and result['usage'] is not None
    assert 'chapter_budget_unsettled' in {b['code'] for b in result['blockers']}
    assert result['usage']['chapter_budgets'][0]['charged_amount']=='0.0006'
    assert result['usage']['summary']['costs_by_currency']=={}
    assert inventory_tree(package)==before


@pytest.mark.parametrize('time_state',['closed','open','local_recovery'])
def test_archived_active_intervals_keep_measured_time_and_unknown_original(archive_case,monkeypatch,time_state):
    c=archive_case;ledger,_,_=seed(c['root'],book_id=c['project'].book_id)
    clock=ChapterExecutionClock(ledger.root,ledger.frozen_budget())
    with clock.active('synthetic'):
        pass
    if time_state!='closed':
        path=ledger.root/'time.json';value=json.loads(path.read_bytes())
        value['intervals'][-1].update(ended_at=None,elapsed_ns=None,awaiting_author_after=False)
        value['record_sha256']=canonical_json_hash(value,exclude_fields=('record_sha256',),exclude_environment_fields=False)
        path.write_text(json.dumps(value))
    if time_state=='local_recovery':
        with clock.local_recovery('synthetic-local'):
            pass
    expected=clock.report();package=Path(capture(c)['path']);before=inventory_tree(package)
    forbid_runtime(monkeypatch)
    with absent_source(c['root']):result=inspect(c,package)
    assert result['model_and_budgets_verified']==(time_state=='closed')
    actual=result['usage']['chapter_times'][0]
    for key in actual:assert actual[key]==expected[key]
    if time_state!='closed':assert 'chapter_active_time_unsettled' in {b['code'] for b in result['blockers']}
    assert inventory_tree(package)==before
