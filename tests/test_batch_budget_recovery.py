"""Actual process deaths, cross-chapter exclusion and a completed ZIP archive."""
import json,subprocess,sys,time
from pathlib import Path

import pytest

from tests.test_batch_budget import tmp_path,setup,contract,runtime,report,forbidden
from tests.test_chapter_budget_store import budget
from tests.test_chapter_budget_runtime import args
from tests.test_backup_archive import archive_case,legacy,software_tree,capture
from tests.test_backup_archive_history import absent_source
from tests.test_backup_archive_usage import forbid_runtime
from core.batch_budget_store import BatchBudgetRegistry
from core.chapter_budget_store import ChapterBudgetUncertain
from workbench.backup_archive_usage import inspect_archived_usage
from workbench.backup_files import inventory_tree


CHILD=r'''
import os,sys
from pathlib import Path
from tests.test_batch_budget import runtime
from tests.test_chapter_budget_runtime import args,response
root=Path(sys.argv[1])
def fault(event,*unused):
    if event==sys.argv[2]:os._exit(76)
def operation():
    (root/'physical-effects.fixture.txt').write_text('1',encoding='utf-8')
    return response()
runtime(root,2,'execution_original',fault_injector=fault).execute_attempt(**args(operation=operation))
'''


@pytest.mark.parametrize('phase',['after_chapter_reservation_before_intent','after_intent_before_dispatch',
    'after_dispatch_claim_before_provider','after_provider_response_before_artifact','after_receipt_before_chapter_settlement'])
def test_real_attempt_process_death_preserves_whole_batch_occupancy_and_requires_local_resolution(tmp_path,phase):
    registry=setup(tmp_path)
    child=subprocess.run([sys.executable,'-B','-c',CHILD,str(tmp_path),phase],capture_output=True,text=True,timeout=30)
    assert child.returncode==76,child.stderr
    assert report(registry)['charged_amount']=='0.0006'
    with pytest.raises(ChapterBudgetUncertain):runtime(tmp_path,3,'execution_next').execute_attempt(**args(operation=forbidden))
    # Construction/read never reconcile or invoke the original operation.
    original=runtime(tmp_path,2,'execution_original');before=report(registry)
    assert report(registry)==before
    original.reconcile_chapter_evidence()
    if phase in ('after_chapter_reservation_before_intent','after_intent_before_dispatch'):
        assert report(registry)['charged_amount']=='0' and not (tmp_path/'physical-effects.fixture.txt').exists()
        runtime(tmp_path,3,'execution_next').execute_attempt(**args())
        assert report(registry)['charged']['calls']==1
    elif phase=='after_receipt_before_chapter_settlement':
        assert report(registry)['charged_amount']=='0.00005'
        assert original.execute_attempt(**args(operation=forbidden))=='synthetic result'
        assert (tmp_path/'physical-effects.fixture.txt').read_text(encoding='utf-8')=='1'
    else:
        assert report(registry)['charged_amount']=='0.0006' and report(registry)['unresolved_attempts']
        with pytest.raises(ChapterBudgetUncertain):runtime(tmp_path,3,'execution_next').execute_attempt(**args(operation=forbidden))


LIVE=r'''
import sys
from pathlib import Path
from tests.test_batch_budget import runtime
from tests.test_chapter_budget_runtime import args,response
root=Path(sys.argv[1])
def operation():
    (root/'live.fixture.txt').write_text('entered',encoding='utf-8')
    sys.stdin.read()
    return response()
runtime(root,2,'execution_live').execute_attempt(**args(operation=operation))
'''
OTHER=r'''
import sys
from pathlib import Path
from tests.test_batch_budget import runtime
from tests.test_chapter_budget_runtime import args
from core.chapter_budget_store import ChapterBudgetUncertain
def forbidden():raise SystemExit(99)
try:runtime(Path(sys.argv[1]),3,'execution_other').execute_attempt(**args(operation=forbidden))
except ChapterBudgetUncertain:sys.exit(2)
'''


def test_other_real_process_cannot_start_a_different_chapter_while_batch_call_is_live(tmp_path):
    registry=setup(tmp_path)
    child=subprocess.Popen([sys.executable,'-B','-c',LIVE,str(tmp_path)],stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        deadline=time.monotonic()+15
        while not (tmp_path/'live.fixture.txt').exists() and child.poll() is None and time.monotonic()<deadline:time.sleep(0.02)
        assert (tmp_path/'live.fixture.txt').exists()
        other=subprocess.run([sys.executable,'-B','-c',OTHER,str(tmp_path)],capture_output=True,text=True,timeout=25)
        assert other.returncode==2,other.stderr
        output=child.communicate(input='finish',timeout=20)
        assert child.returncode==0,output
        assert report(registry)['charged']['calls']==1 and report(registry)['charged_amount']=='0.00005'
    finally:
        if child.poll() is None:child.kill();child.communicate(timeout=10)


def test_completed_actual_zip_verifies_batch_prices_and_bindings_without_live_book_or_store_initialization(archive_case,monkeypatch):
    c=archive_case;root=c['root'];book=c['project'].book_id
    batch=contract(chapter_budgets=[budget(book_id=book,chapter=chapter) for chapter in (3,4,5)])
    registry=BatchBudgetRegistry(root/'.novelagent/runtime/budgets',book_id=book)
    registry.freeze(batch,before_write=lambda:None)
    # Same real runtime and original chapter ledger; only the operation is a fixture.
    from core.chapter_budget_store import ChapterBudgetStore
    from core.chapter_model_budget import ChapterModelCallBudget
    from core.model_call_runtime import ModelCallRuntimeContext
    from core.model_calls import ModelCallStore
    store=ChapterBudgetStore(registry.root/'c3',book_id=book,chapter=3)
    calls=ModelCallStore(root/'.novelagent/runtime/runs/executions/execution_archive/model_calls')
    execute=ModelCallRuntimeContext(calls,chapter_budget=ChapterModelCallBudget(store,execution_id='execution_archive'))
    execute.execute_attempt(**args())
    expected=report(registry);package=Path(capture(c)['path'])
    before=inventory_tree(package);source_before=inventory_tree(root)
    forbid_runtime(monkeypatch)
    with absent_source(root):
        first=inspect_archived_usage(package,project_id=c['project'].id,book_id=book)
        second=inspect_archived_usage(package,project_id=c['project'].id,book_id=book)
    assert first==second and first['model_and_budgets_verified'],first['blockers']
    assert first['usage']['batch_budgets']==[expected] and expected['charged_amount']=='0.00005'
    assert inventory_tree(package)==before and inventory_tree(root)==source_before
