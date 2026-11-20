"""A real process death must not prevent adopting an already paid-for result."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from core.chapter_budget_store import ChapterBudgetError
from core.chapter_execution_budget import ChapterExecutionBudget
from tests.test_chapter_budget_executors import (short_root,scope,inject,executor,fact_provider,
    model_request,response,Crash)
from tests.test_chapter_fact_extraction import _execute,BOOK_ID,_provenance
from tests import test_chapter_candidate as candidate_fixture
from core.model_call_runtime import current_model_call_runtime


def crash_fact(root,point):
    code=r'''
import os,sys
from pathlib import Path
from tests.test_chapter_budget_executors import scope,inject,executor,fact_provider
from tests.test_chapter_fact_extraction import _execute
root=Path(sys.argv[1]);point=sys.argv[2]
def fault(event,*unused):
    if event==point:os._exit(42)
class Calls:
    def append(self,value):
        path=root/'calls.txt';path.write_text(path.read_text()+'call\n' if path.exists() else 'call\n')
_execute(executor(root,inject(scope(root),fault),fact_provider(Calls())))
raise AssertionError('crash boundary was not reached')
'''
    result=subprocess.run([sys.executable,'-B','-c',code,str(root),point],
        capture_output=True,text=True,timeout=30)
    assert result.returncode==42,result.stderr or result.stdout
    return ChapterExecutionBudget(root,book_id=BOOK_ID,chapter=27)


def test_fact_real_process_death_after_successful_receipt_can_finish_locally(short_root):
    budget=crash_fact(short_root,'after_receipt_before_chapter_settlement')
    before=(budget.root/'time.json').read_bytes()
    assert budget.time_clock.report()['active_status']=='unknown'
    calls=(short_root/'calls.txt').read_text()
    first=executor(short_root,budget,lambda *args:pytest.fail('no Provider invocation in local recovery'))
    extraction=next(first.store.root.iterdir()).name
    record=first.resume(extraction)
    assert record['status']=='completed'
    assert (budget.root/'time.json').read_bytes()==before
    assert (budget.root/'local-recovery/time.json').is_file()
    assert budget.time_clock.report()['active_status']=='unknown'
    assert (short_root/'calls.txt').read_text()==calls
    assert budget.store.report()['charged']['calls']==1
    assert not budget.store.report()['unresolved_attempts']


@pytest.mark.parametrize('point',['after_chapter_reservation_before_intent','after_dispatch_claim_before_provider','after_response_artifact_before_receipt'])
def test_missing_successful_receipt_cannot_dispatch_during_unknown_time_recovery(short_root,point):
    budget=crash_fact(short_root,point)
    before=(budget.root/'time.json').read_bytes()
    first=executor(short_root,budget,lambda *args:pytest.fail('recovery must not send'))
    extraction=next(first.store.root.iterdir()).name
    with pytest.raises(ChapterBudgetError):first.resume(extraction)
    assert (budget.root/'time.json').read_bytes()==before
    assert not first.store.has_record(extraction)


def test_corrupted_successful_response_cannot_be_adopted(short_root):
    budget=crash_fact(short_root,'after_receipt_before_chapter_settlement')
    first=executor(short_root,budget,lambda *args:pytest.fail('no Provider'))
    extraction=next(first.store.root.iterdir()).name
    store=first.store.model_call_store(first.store.load_intent(extraction))
    receipt=store.load_receipt('fact-a1')
    before=(budget.root/'time.json').read_bytes()
    (store.root/receipt['response_artifact_ref']).write_text('corrupt',encoding='utf-8')
    with pytest.raises(Exception):first.resume(extraction)
    assert (budget.root/'time.json').read_bytes()==before
    assert not first.store.has_record(extraction)


def test_exhausted_time_can_recover_successful_fact_and_escaped_runtime_cannot_spend(short_root):
    budget=scope(short_root)
    def fault(event,*unused):
        if event=='after_receipt_before_chapter_settlement':raise Crash()
    first=executor(short_root,inject(budget,fault),fact_provider([]))
    with pytest.raises(Crash):_execute(first)
    extraction=next(first.store.root.iterdir()).name
    manual=budget.time_clock.monotonic_ns.__self__
    with budget.time_clock.active('consume-remaining'):manual.advance(900)
    before=(budget.root/'time.json').read_bytes()
    observed=inject(budget,None)
    record=executor(short_root,observed,lambda *args:pytest.fail('no Provider')).resume(extraction)
    assert record['status']=='completed'
    assert (budget.root/'time.json').read_bytes()==before
    assert observed.saved_runtime.dispatch_forbidden
    rows=budget.store.report()['items']
    with pytest.raises(ChapterBudgetError):
        model_request(observed.saved_runtime,{},'escaped','facts',lambda:pytest.fail('cannot spend'))
    assert budget.store.report()['items']==rows


def test_local_recovery_time_is_charged_without_relabeling_unknown_interval(short_root):
    budget=crash_fact(short_root,'after_receipt_before_chapter_settlement')
    from tests.test_chapter_execution_deadline import ManualClock
    from core.chapter_execution_time import ChapterExecutionClock,ChapterTimeUnknown
    manual=ManualClock()
    clock=ChapterExecutionClock(budget.root,budget.store.frozen_budget(),wall_clock=manual.wall,monotonic_ns=manual.monotonic_ns)
    before=(budget.root/'time.json').read_bytes()
    with clock.local_recovery('local-completion'):
        report=clock.report()
        assert report['active_status']=='unknown' and report['local_recovery_status']=='running'
        manual.advance(3)
    report=clock.report()
    assert report['active_seconds']=='3' and report['local_recovery_active_seconds']=='3'
    assert not report['active_time_complete'] and report['author_wait_seconds'] is None
    assert (budget.root/'time.json').read_bytes()==before
    with pytest.raises(ChapterTimeUnknown):
        with clock.active('cannot-reopen'):pytest.fail('unknown original interval remains blocked')


def test_local_recovery_of_exhausted_time_adds_measured_time_and_missing_ledger_is_not_reset(short_root):
    from core.chapter_execution_time import ChapterTimeError,ChapterTimeExceeded
    budget=scope(short_root);clock=budget.time_clock
    manual=clock.monotonic_ns.__self__
    with clock.active('original',awaiting_author_after=True):manual.advance(900)
    manual.advance(1000)
    with clock.local_recovery('finish-saved-result'):manual.advance(4)
    report=clock.report()
    assert report['active_seconds']=='904' and report['local_recovery_active_seconds']=='4'
    assert report['author_wait_seconds']=='1000'
    with pytest.raises(ChapterTimeExceeded):
        with clock.active('no-new-provider'):pytest.fail('must remain exhausted')
    (budget.root/'local-recovery/time.json').unlink()
    with pytest.raises(ChapterTimeError):
        with clock.local_recovery('missing-record'):pytest.fail('must not initialize a missing ledger')
    assert not (budget.root/'local-recovery/time.json').exists()


def test_candidate_real_process_death_uses_successful_prose_without_generator(short_root):
    code=r'''
import os,sys,json
from pathlib import Path
from tests import test_chapter_candidate as fixture_module
from tests.test_chapter_budget_executors import scope,inject,model_request,response
from core.model_call_runtime import current_model_call_runtime
fixture=fixture_module.ChapterCandidateTest();fixture.setUp()
Path(sys.argv[1]).write_text(json.dumps({'root':str(fixture.book),'book_id':fixture.identity.book_id}))
def fault(event,*unused):
    if event=='after_receipt_before_chapter_settlement':os._exit(42)
budget=inject(scope(fixture.book,book_id=fixture.identity.book_id,chapter=2),fault)
def generator(prompt):
    runtime=current_model_call_runtime()
    return model_request(runtime,{'prompt':prompt},runtime.new_call_id(provider='fixture',stage='generation'),
        'generation',lambda:response('林澈推开控制室的门，周岚守住走廊。旧终端亮起时，失踪信号终于显出来源。')).text
fixture._execute(fixture._executor(generator=generator,execution_budget=budget),key='death')
'''
    info=short_root/'candidate.json'
    result=subprocess.run([sys.executable,'-B','-c',code,str(info)],capture_output=True,text=True,timeout=45)
    assert result.returncode==42,result.stderr or result.stdout
    data=json.loads(info.read_text());root=Path(data['root'])
    budget=ChapterExecutionBudget(root,book_id=data['book_id'],chapter=2)
    before=(budget.root/'time.json').read_bytes()
    fixture=candidate_fixture.ChapterCandidateTest();fixture.book=root
    first=fixture._executor(generator=lambda *args:pytest.fail('generator must not be entered'),execution_budget=budget)
    candidate=next(first.store.root.iterdir()).name
    record=first.resume(candidate)
    assert record['status']=='completed'
    assert record['prose']['chars']>0
    assert (budget.root/'time.json').read_bytes()==before
    assert budget.store.report()['charged']['calls']==1
