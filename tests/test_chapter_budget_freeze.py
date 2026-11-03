import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from core.chapter_budget_contract import freeze_chapter_budget
from core.chapter_budget_freeze import ChapterBudgetFreezeRegistry
from core.chapter_budget_store import ChapterBudgetError, ChapterBudgetConflict
from core.chapter_execution_budget import ChapterExecutionBudget
from core.chapter_execution_time import ChapterTimeError
from tests.test_chapter_budget_runtime import args


def budget(**changes):
    values=dict(book_id='book-test',chapter=2,max_provider_calls=3,max_input_tokens=1000,
        max_output_tokens=1000,max_active_seconds='120')
    values.update(changes)
    return freeze_chapter_budget(**values)


def registry(root):return ChapterBudgetFreezeRegistry(root,book_id='book-test')


def test_first_freeze_publishes_budget_time_and_binding_together(tmp_path):
    r=registry(tmp_path);seen=[]
    assert r.status(2)=={'state':'not_configured','budget':None}
    assert not r.root.exists()
    frozen=r.freeze(budget(),before_write=lambda:seen.append('preflight'))
    assert len(seen)==5
    assert r.status(2)=={'state':'ready','budget':frozen}
    scope=ChapterExecutionBudget(tmp_path,book_id='book-test',chapter=2)
    assert scope.store.report()['charged']['calls']==0
    assert scope.time_clock.report()['interval_count']==0
    before={p.name:p.read_bytes() for p in (r.root/'c2').iterdir()}
    r.freeze(budget(),before_write=lambda:pytest.fail('ready idempotent freeze must not initialize again'))
    assert before=={p.name:p.read_bytes() for p in (r.root/'c2').iterdir()}


def child_crash(root,event):
    def fault(point):
        if event==point:os._exit(42)
    registry(root).freeze(budget(),before_write=lambda:None,fault_injector=fault)
    raise AssertionError('crash point not reached')


@pytest.mark.parametrize('event',['after_freeze_intent','after_budget','after_ledger','after_time','after_ready'])
def test_real_process_crash_during_first_freeze_requires_exact_explicit_retry(tmp_path,event):
    code="import sys;sys.path.insert(0,'tests');from test_chapter_budget_freeze import child_crash;child_crash(sys.argv[1],sys.argv[2])"
    child=subprocess.run([sys.executable,'-B','-c',code,str(tmp_path),event],capture_output=True,text=True,timeout=20)
    assert child.returncode==42,child.stderr
    r=registry(tmp_path)
    if event!='after_ready':
        assert r.status(2)['state']=='initializing'
        with pytest.raises(ChapterBudgetError):ChapterExecutionBudget(tmp_path,book_id='book-test',chapter=2)
    else:assert r.status(2)['state']=='ready'
    with pytest.raises(ChapterBudgetConflict):r.freeze(budget(max_provider_calls=5),before_write=lambda:None)
    assert r.freeze(budget(),before_write=lambda:None)==budget()
    assert ChapterExecutionBudget(tmp_path,book_id='book-test',chapter=2).store.report()['charged']['calls']==0


@pytest.mark.parametrize('file',['budget.json','ledger.json','time.json'])
def test_ready_budget_never_recreates_lost_component(tmp_path,file):
    r=registry(tmp_path);r.freeze(budget(),before_write=lambda:None)
    target=r.root/'c2'/file;target.unlink()
    with pytest.raises((ChapterBudgetError,ChapterTimeError)):
        r.freeze(budget(),before_write=lambda:pytest.fail('lost completed budget cannot initialize'))
    assert not target.exists()


@pytest.mark.parametrize('file',['i2.json','r2.json'])
def test_lost_initialization_binding_cannot_reset_used_time(tmp_path,file):
    r=registry(tmp_path);r.freeze(budget(),before_write=lambda:None)
    scope=ChapterExecutionBudget(tmp_path,book_id='book-test',chapter=2)
    with scope.time_clock.active('synthetic'):pass
    before=(r.root/'c2/time.json').read_bytes()
    (r.root/file).unlink()
    with pytest.raises(ChapterBudgetError):r.freeze(budget(),before_write=lambda:None)
    assert (r.root/'c2/time.json').read_bytes()==before
    assert not (r.root/file).exists()


def test_changed_source_preflight_leaves_freeze_unpublished_then_can_retry(tmp_path):
    r=registry(tmp_path);calls=[]
    def guard():
        calls.append(1)
        if len(calls)==3:raise ChapterBudgetConflict('source changed')
    with pytest.raises(ChapterBudgetConflict):r.freeze(budget(),before_write=guard)
    assert r.status(2)['state']=='initializing'
    with pytest.raises(ChapterBudgetError):ChapterExecutionBudget(tmp_path,book_id='book-test',chapter=2)
    r.freeze(budget(),before_write=lambda:None)
    assert r.status(2)['state']=='ready'


def test_foreign_chapter_and_damaged_intent_cannot_initialize(tmp_path):
    r=registry(tmp_path)
    with pytest.raises(ValueError):r.freeze(budget(book_id='another'),before_write=lambda:None)
    assert not r.root.exists()
    r.root.mkdir(parents=True);(r.root/'i2.json').write_text('{}')
    with pytest.raises(ChapterBudgetError):r.freeze(budget(),before_write=lambda:None)
    assert not (r.root/'c2').exists()


def test_nonempty_unfinished_ledger_cannot_be_reset(tmp_path):
    r=registry(tmp_path)
    class Crash(BaseException):pass
    def fault(point):
        if point=='after_time':raise Crash()
    with pytest.raises(Crash):r.freeze(budget(),before_write=lambda:None,fault_injector=fault)
    ledger=r.root/'c2/ledger.json';old=json.loads(ledger.read_text())
    old['attempts'].append({'unexplained':'evidence'})
    ledger.write_text(json.dumps(old))
    before=ledger.read_bytes()
    with pytest.raises(ChapterBudgetConflict):r.freeze(budget(),before_write=lambda:None)
    assert ledger.read_bytes()==before and not (r.root/'r2.json').exists()
