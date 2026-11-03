"""Apply overlapping, nonzero archive/source budgets to a real restore copy."""
import json
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from core.chapter_budget_store import ChapterBudgetStore
from core.chapter_execution_time import ChapterExecutionClock
from core.model_call_runtime import ModelCallRuntimeContext
from core.story_project.reviewed_state_document import digest
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive_history import absent_source
from tests.test_backup_archive_sources import case, mixed
from tests.test_backup_restore_database import make_source_workbench, bound_copy, logical, run
from tests.test_chapter_budget_runtime import args
from tests.test_model_usage_budget import seed, SyntheticStop
from tests.test_restore_handoff import restored, preview, begin, deny, ledger
from tests.test_restore_usage import pending
from workbench.app import create_app
from workbench.backup_archive_usage import inspect_archived_usage
from workbench.backup_files import inventory_tree
from workbench.restore_budget_preparation import RestoreBudgetPreparation
from workbench.restore_handoff import RestoreHandoffError
from workbench.model_usage import read_project_model_usage


def make_financial_source(*, before_archive=None, include_uncertain=True, job_runtime_relative=None):
    retained = {}
    def before(c):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr('workbench.book_ownership.default_machine_store', lambda:c['machine'])
            store, calls, runtime = seed(c['root'], book_id=c['project'].book_id,
                chapter=3, execution='execution_shared', amount='0.0009')
            clock = ChapterExecutionClock(store.root, store.frozen_budget())
            with clock.active('before-archive'): pass
            retained.update(store=store, calls=calls, runtime=runtime, clock=clock)
        if before_archive is not None: before_archive(c)
    c = make_source_workbench(mixed.__wrapped__(case.__wrapped__()), before_maintenance=before,
        job_runtime_relative=job_runtime_relative)
    old = inspect_archived_usage(c['package'], project_id=c['project'].id, book_id=c['project'].book_id)
    assert old['usage']['summary']['attempt_count'] == 1 and old['model_and_budgets_verified']
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr('workbench.book_ownership.default_machine_store', lambda:c['machine'])
        retained['runtime'].execute_attempt(**args(call_id='after'))
        with retained['clock'].active('after-archive'): pass
        def fault(phase, *unused):
            if phase == 'after_dispatch_claim_before_provider': raise SyntheticStop()
        uncertain = ModelCallRuntimeContext(retained['calls'],
            chapter_budget=retained['runtime'].chapter_budget, fault_injector=fault)
        if include_uncertain:
            with pytest.raises(SyntheticStop): uncertain.execute_attempt(**args(call_id='uncertain'))
    outside = c['root']/'.novelagent/runtime/runs/executions/execution_shared/post-archive-plan.json'
    outside.write_text('{"must_not_import_as_executable_history":true}', encoding='utf-8')
    return {**c, 'before_usage':old['usage'], 'source_usage':read_project_model_usage(c['root'],book_id=c['project'].book_id),
        'outside_finance':outside.relative_to(c['root']).as_posix()}


@pytest.fixture(scope='module')
def full_source():
    return make_financial_source()


@pytest.fixture
def financial(pending):
    c = pending; usage = c['usage_service'].capture(handoff_id=c['id'])
    service = RestoreBudgetPreparation(c['project'].book_id, machine_store=c['store'])
    return {**c, 'budget_service':service, 'preserved_usage':usage}


def prepare(c, *, resume=False):
    method = c['budget_service'].resume_budget if resume else c['budget_service'].prepare_budget
    return method(c['package'], handoff_id=c['id'], temporary_root=c['scratch'])


def test_actual_target_budget_contains_overlap_once_and_keeps_unknown_occupancy(financial):
    c = financial; source = inventory_tree(c['root']); target = inventory_tree(c['target']/'book')
    sql = logical(c['target']/'workbench.sqlite'); original_machine = ledger(c)
    result = prepare(c)
    assert result['target_budget_prepared'] and result['budget_evidence_reconciled']
    assert not result['settled'] and not result['write_activation_allowed'] and not result['handoff_completed']
    actual = read_project_model_usage(c['target']/'book', book_id=c['project'].book_id)
    assert actual['summary']['attempt_count'] == 3  # 1 archived + 2 later, never 1 + 3.
    for field in ('items','summary','chapter_budgets'):
        assert actual[field] == c['source_usage'][field]
    budget = ChapterBudgetStore(c['target']/'book/.novelagent/runtime/budgets/c3',
        book_id=c['project'].book_id, chapter=3).usage_snapshot()
    assert budget['report']['charged_amount'] == '0.000769'
    assert len(budget['attempts']) == 3 and budget['report']['unresolved_attempts']
    assert not (c['target']/'book'/c['outside_finance']).exists()
    changed = inventory_tree(c['target']/'book')
    allowed = json.loads(c['budget_service']._path(c['id'], 'budget').read_text(encoding='utf-8'))['reconciliation']['files']
    for name, record in target['files'].items():
        if name not in allowed: assert changed['files'][name] == record
    assert logical(c['target']/'workbench.sqlite') == sql and ledger(c) == original_machine
    assert inventory_tree(c['root']) == source
    after = inventory_tree(c['target']/'book')
    with absent_source(c['root']):
        assert prepare(c, resume=True) == result
        assert prepare(c) == result
        app = create_app(database_path=c['target']/'workbench.sqlite', start_worker=True, auto_import_active=True,
            chapter_publication_machine_store=c['store'])
        with TestClient(app) as client:
            assert client.get('/api/restore-scope').json()['read_only']
            assert app.state.jobs.claim_next() is None
    assert inventory_tree(c['target']/'book') == after
    deny(c['root']); deny(c['target']/'book')


def test_ordinary_database_repair_cannot_accept_an_unqualified_budget_overlay(financial):
    c = financial; prepare(c); before = inventory_tree(c['target']/'book')
    with pytest.raises((OSError, RuntimeError, ValueError)): run(c)
    assert inventory_tree(c['target']/'book') == before


@pytest.mark.parametrize('phase', ['after_budget_intent','after_budget_temp_fsync','after_budget_member',
    'before_budget_completion','after_budget_completion'])
def test_real_process_crash_resumes_budget_preparation_without_original_source(financial, phase):
    c = financial
    code = '''import os,sys
from workbench.restore_budget_preparation import RestoreBudgetPreparation
def fault(phase,value):
    if phase == sys.argv[6]: os._exit(86)
RestoreBudgetPreparation(sys.argv[1],machine_store=sys.argv[2],fault_injector=fault).prepare_budget(
    sys.argv[3],handoff_id=sys.argv[4],temporary_root=sys.argv[5])
'''
    child = subprocess.run([sys.executable,'-c',code,c['project'].book_id,str(c['store']),str(c['package']),
        c['id'],str(c['scratch']),phase], cwd=Path.cwd(), capture_output=True, text=True, encoding='utf-8', timeout=120)
    assert child.returncode == 86, child.stdout+child.stderr
    intent = c['budget_service']._path(c['id'], 'budget').read_bytes()
    if phase != 'after_budget_completion':
        with pytest.raises(RestoreHandoffError, match='显式续接'): prepare(c)
    with absent_source(c['root']): result = prepare(c, resume=True)
    assert result['usage']['summary']['attempt_count'] == 3 and not result['write_activation_allowed']
    assert c['budget_service']._path(c['id'], 'budget').read_bytes() == intent
    assert ledger(c) == c['value']['restoring_record']
    assert not list((c['target']/'book').rglob('.restore-budget-*.tmp'))


def test_completed_preparation_rejects_corruption_and_never_rewrites_other_history(financial):
    c = financial; prepare(c)
    budget = c['target']/'book/.novelagent/runtime/budgets/c3/ledger.json'
    original = budget.read_bytes(); sql = logical(c['target']/'workbench.sqlite')
    budget.write_bytes(b'{}')
    before = inventory_tree(c['target']/'book')
    with pytest.raises((OSError, RuntimeError, ValueError)): prepare(c, resume=True)
    assert inventory_tree(c['target']/'book') == before and logical(c['target']/'workbench.sqlite') == sql
    budget.write_bytes(original)
    extra = c['target']/'book/unapproved.txt'; extra.write_text('unapproved', encoding='utf-8')
    with pytest.raises((OSError, RuntimeError, ValueError)): prepare(c, resume=True)
    assert extra.read_text(encoding='utf-8') == 'unapproved'


def test_rollback_invalidates_prepared_budget_qualification(financial):
    c = financial; prepare(c)
    c['handoff'].rollback(handoff_id=c['id'])
    before = inventory_tree(c['target']/'book')
    with pytest.raises(RestoreHandoffError): prepare(c, resume=True)
    assert inventory_tree(c['target']/'book') == before
    deny(c['target']/'book')


def test_lost_budget_intent_before_file_publication_keeps_previous_ledger(financial):
    c = financial
    path = c['target']/'book/.novelagent/runtime/budgets/c3/ledger.json'
    before = path.read_bytes()
    def fault(phase, value):
        if phase == 'after_budget_temp_fsync':
            service._path(c['id'], 'budget').write_text('{}', encoding='utf-8')
    service = RestoreBudgetPreparation(c['project'].book_id, machine_store=c['store'], fault_injector=fault)
    with pytest.raises(RestoreHandoffError):
        service.prepare_budget(c['package'], handoff_id=c['id'], temporary_root=c['scratch'])
    assert path.read_bytes() == before
    assert not service._path(c['id'], 'budget-done').exists()
    assert ledger(c) == c['value']['restoring_record']


def test_target_budget_growth_during_projection_is_bounded_and_not_completed(financial, monkeypatch):
    from workbench import restore_budget_preparation as module
    c = financial
    target = c['target']/'book/.novelagent/runtime/budgets/c3/budget.json'
    stream = module._stream_member; changed = False
    def grow(resolver, relative, **kwargs):
        nonlocal changed
        path = resolver.resolve({'root_id':'external:backup_source','relative_path':relative}).path
        if path == target and not changed:
            changed = True
            path.write_bytes(path.read_bytes()+b' ')
        return stream(resolver, relative, **kwargs)
    monkeypatch.setattr(module, '_stream_member', grow)
    with pytest.raises(RestoreHandoffError, match='超过保存清单大小'):
        prepare(c)
    assert changed and not c['budget_service']._path(c['id'], 'budget-done').exists()
    assert ledger(c) == c['value']['restoring_record']
