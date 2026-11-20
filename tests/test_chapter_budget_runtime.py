"""Real durable stores and child-process crashes; no external Provider calls."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from api.contracts import ModelResponse, reported_model_usage
from api.retry import MODEL_READ_GENERATION, PartialResponseError, RetryOperationError, RetryPolicy
from core.chapter_budget_contract import freeze_chapter_budget
from core.chapter_budget_store import (ChapterBudgetStore, ChapterBudgetError,
    ChapterBudgetConflict, ChapterBudgetExceeded, ChapterBudgetUncertain)
from core.chapter_model_budget import ChapterModelCallBudget
from core.context_budget import ContextBudgetError
from core.engine.persistence import PersistenceLockError
from core.model_call_runtime import (ModelCallRuntimeContext, ProviderCallUncertainError,
    ProviderPartialResponseError, ProviderRejectedError, ProviderRequestNotSentError)
from core.model_calls import ModelCallStore, ModelCallConflictError, ModelCallIntegrityError, canonical_model_request_digest


FP='a'*64
REQUEST={'messages':[{'role':'user','content':'synthetic chapter'}]}


def setup(root,amount='0.01'):
    budget=freeze_chapter_budget(book_id='book-test',chapter=2,max_provider_calls=10,
        max_input_tokens=10000,max_output_tokens=10000,max_active_seconds='900',
        amount_limit=amount,currency='USD',pricing=[dict(provider='openai',model='fixture',
        endpoint_type='official',endpoint_fingerprint=FP,currency='USD',input_price_per_million='1',
        output_price_per_million='2',input_token_semantics='input_includes_cache')])
    s=ChapterBudgetStore(root/'b',book_id='book-test',chapter=2)
    s.initialize(budget)
    return s


def runtime(root,execution='execution_gen',**kwargs):
    s=ChapterBudgetStore(root/'b',book_id='book-test',chapter=2)
    calls=ModelCallStore(root/('c'+execution.removeprefix('execution_')))
    return ModelCallRuntimeContext(calls,chapter_budget=ChapterModelCallBudget(s,execution_id=execution),**kwargs)


def response(usage=None):
    return ModelResponse('synthetic result',actual_model='fixture',endpoint_type='official',
        usage=reported_model_usage(dict(input_tokens=10,output_tokens=20,cache_read_input_tokens=0,
            cache_creation_input_tokens=0) if usage is None else usage))


def args(**changes):
    values=dict(call_id='call',attempt_number=1,provider='openai',model='fixture',stage='generation',
        endpoint_type='official',endpoint_fingerprint=FP,request=REQUEST,max_output_tokens=200,
        input_tokens=100,input_bound=dict(max_billable_input_tokens=100,basis='verified_token_count',
            request_digest=canonical_model_request_digest(REQUEST),evidence_sha256='b'*64),operation=response)
    values.update(changes)
    return values


def forbidden():
    pytest.fail('Provider must not be invoked or replayed')


def crash_child(root,event):
    root=Path(root)
    def fault(name,*unused):
        if name==event:
            os._exit(42)
    def network():
        count=root/'calls.txt'
        count.write_text(str(int(count.read_text())+1) if count.exists() else '1')
        return response()
    runtime(root,fault_injector=fault).execute_attempt(**args(operation=network))
    raise AssertionError('crash point was not reached')


def crash(root,event):
    code="import sys; sys.path.insert(0,'tests'); from test_chapter_budget_runtime import crash_child; crash_child(sys.argv[1],sys.argv[2])"
    child=subprocess.run([sys.executable,'-B','-c',code,str(root),event],capture_output=True,text=True,timeout=25)
    assert child.returncode==42,child.stderr


def test_budget_and_dispatch_are_durable_before_provider_then_replay_once(tmp_path):
    s=setup(tmp_path)
    r=runtime(tmp_path)
    def network():
        row=s.execution_attempts('execution_gen')[0]
        assert row['dispatch_claimed'] and row['receipt'] is None
        assert r.store.load_intent('call-a1')==row['plan']['intent']
        assert s.report()['charged_amount']=='0.0005'
        return response()
    assert r.execute_attempt(**args(operation=network))=='synthetic result'
    before=(s.root/'ledger.json').read_bytes()
    assert runtime(tmp_path).execute_attempt(**args(operation=forbidden))=='synthetic result'
    assert (s.root/'ledger.json').read_bytes()==before
    assert s.report()['charged_amount']=='0.00005' and s.report()['charged']['calls']==1


@pytest.mark.parametrize('event',[
    'after_chapter_reservation_before_intent','after_intent_before_dispatch',
    'after_dispatch_claim_before_provider','after_provider_response_before_artifact',
    'after_response_artifact_before_receipt','after_receipt_before_chapter_settlement'])
def test_process_death_at_each_durable_boundary(tmp_path,event):
    s=setup(tmp_path)
    crash(tmp_path,event)
    original=s.execution_attempts('execution_gen')[0]['plan']['intent']
    assert s.report()['charged_amount']=='0.0005'
    r=runtime(tmp_path)
    if event in {'after_chapter_reservation_before_intent','after_intent_before_dispatch'}:
        with pytest.raises(ProviderRequestNotSentError):r.execute_attempt(**args(operation=forbidden))
        assert s.report()['charged_amount']=='0' and s.report()['charged']['calls']==0
        assert r.store.load_receipt('call-a1')['usage']=={}
        assert not (tmp_path/'calls.txt').exists()
    elif event=='after_receipt_before_chapter_settlement':
        assert r.execute_attempt(**args(operation=forbidden))=='synthetic result'
        assert s.report()['charged_amount']=='0.00005'
        assert (tmp_path/'calls.txt').read_text()=='1'
    else:
        with pytest.raises(ProviderCallUncertainError):r.execute_attempt(**args(operation=forbidden))
        with pytest.raises(ChapterBudgetUncertain):runtime(tmp_path,'execution_fact').execute_attempt(**args(operation=forbidden))
        assert s.report()['charged_amount']=='0.0005'
        assert not r.store.receipt_path('call-a1').exists()
        assert (tmp_path/'calls.txt').exists()==(event!='after_dispatch_claim_before_provider')
    assert r.store.load_intent('call-a1')==original


def test_retry_uses_new_physical_attempt_only_after_proven_not_sent(tmp_path):
    s=setup(tmp_path);crash(tmp_path,'after_chapter_reservation_before_intent')
    r=runtime(tmp_path);calls=[];attempts=[]
    def invoke():
        attempts.append(1)
        return r.execute_attempt(**args(attempt_number=len(attempts),operation=lambda:(calls.append(1),response())[1]))
    result=RetryPolicy(MODEL_READ_GENERATION,max_attempts=3,base_delay_seconds=0,max_delay_seconds=0).execute(invoke)
    assert result.value=='synthetic result' and len(calls)==1 and len(attempts)==2
    assert r.store.load_receipt('call-a1')['status']=='request_not_sent'
    assert r.store.load_receipt('call-a2')['status']=='succeeded'
    assert s.report()['charged']['calls']==1


def test_generation_and_fact_extraction_share_limit_across_execution_restart(tmp_path):
    s=setup(tmp_path,'0.00054')
    runtime(tmp_path).execute_attempt(**args())
    r=runtime(tmp_path,'execution_fact')
    with pytest.raises(ChapterBudgetExceeded):r.execute_attempt(**args(stage='chapter_fact_extraction',operation=forbidden))
    assert r.store.load_receipt('call-a1')['status']=='budget_rejected'
    assert s.report()['charged_amount']=='0.00005'
    with pytest.raises(ChapterBudgetExceeded):runtime(tmp_path,'execution_new').execute_attempt(**args(operation=forbidden))
    with pytest.raises(ChapterBudgetExceeded):runtime(tmp_path,'execution_fact').execute_attempt(**args(stage='chapter_fact_extraction',operation=forbidden))
    assert s.report()['charged']['calls']==1


def test_crash_during_local_budget_rejection_remains_terminal_not_sent(tmp_path):
    s=setup(tmp_path,'0')
    crash(tmp_path,'after_chapter_reservation_before_intent')
    r=runtime(tmp_path)
    with pytest.raises(ChapterBudgetExceeded):r.execute_attempt(**args(operation=forbidden))
    assert s.report()['charged']['calls']==0
    assert r.store.load_receipt('call-a1')['status']=='budget_rejected'


def test_local_tracker_rejection_releases_chapter_reservation(tmp_path):
    class Tracker:
        def reserve_model_call(self,**kwargs):raise ContextBudgetError('fixture_limit','fixture limit')
    s=setup(tmp_path);r=runtime(tmp_path,tracker=Tracker())
    with pytest.raises(ContextBudgetError):r.execute_attempt(**args(operation=forbidden))
    assert s.report()['charged_amount']=='0' and not s.report()['unresolved_attempts']
    with pytest.raises(ChapterBudgetExceeded):runtime(tmp_path).execute_attempt(**args(operation=forbidden))


@pytest.mark.parametrize('outcome',['connection','rejected','partial','unknown','missing_usage'])
def test_every_provider_terminal_path_settles_or_holds_without_refund(tmp_path,outcome):
    s=setup(tmp_path);r=runtime(tmp_path)
    def operation():
        if outcome=='connection':
            import httpx
            raise httpx.ConnectError('synthetic connection setup failure')
        if outcome=='rejected':
            error=RuntimeError('synthetic rejection');error.status_code=400;raise error
        if outcome=='partial':
            raise PartialResponseError(TimeoutError(),partial_content_received=True,partial_response=response())
        if outcome=='unknown':raise TimeoutError('synthetic ambiguous timeout')
        return response({})
    expected={'connection':ProviderRequestNotSentError,'rejected':ProviderRejectedError,
        'partial':ProviderPartialResponseError,'unknown':ProviderCallUncertainError}
    if outcome in expected:
        with pytest.raises(expected[outcome]):r.execute_attempt(**args(operation=operation))
        with pytest.raises(expected[outcome]):runtime(tmp_path).execute_attempt(**args(operation=forbidden))
    else:
        assert r.execute_attempt(**args(operation=operation))=='synthetic result'
    report=s.report()
    if outcome=='connection':
        assert report['charged_amount']=='0' and not report['unresolved_attempts']
    else:
        assert report['charged_amount']=='0.0005' and report['unresolved_attempts']
        with pytest.raises(ChapterBudgetUncertain):runtime(tmp_path,'execution_fact').execute_attempt(**args(operation=forbidden))


def test_budget_failures_are_non_retryable_even_if_error_cause_mentions_network(tmp_path):
    setup(tmp_path,'0');r=runtime(tmp_path);attempts=[]
    def invoke():
        attempts.append(1)
        return r.execute_attempt(**args(attempt_number=len(attempts),operation=forbidden))
    with pytest.raises(RetryOperationError) as caught:
        RetryPolicy(MODEL_READ_GENERATION,max_attempts=3).execute(invoke)
    assert caught.value.failure_category=='local_budget'
    assert caught.value.report['stop_reason']=='non_retryable' and len(attempts)==1


@pytest.mark.parametrize('changes',[
    {'endpoint_fingerprint':'c'*64},{'endpoint_type':'openai_compatible'},
    {'model':'another'},{'request':{'messages':['different']}},{'max_output_tokens':201},
    {'input_tokens':101}])
def test_restart_cannot_change_frozen_request_or_endpoint(tmp_path,changes):
    s=setup(tmp_path);runtime(tmp_path).execute_attempt(**args())
    before=(s.root/'ledger.json').read_bytes()
    with pytest.raises((ChapterBudgetConflict,ModelCallConflictError)):
        runtime(tmp_path).execute_attempt(**args(operation=forbidden,**changes))
    assert (s.root/'ledger.json').read_bytes()==before


def test_model_call_store_cannot_opt_out_of_existing_binding(tmp_path):
    setup(tmp_path);r=runtime(tmp_path);r.execute_attempt(**args())
    with pytest.raises(ChapterBudgetError):
        ModelCallRuntimeContext(r.store).execute_attempt(**args(call_id='another',operation=forbidden))


@pytest.mark.parametrize('legacy_complete',[False,True])
def test_old_unbudgeted_execution_cannot_receive_empty_budget(tmp_path,legacy_complete):
    s=setup(tmp_path);r=runtime(tmp_path);old=ModelCallRuntimeContext(r.store)
    if legacy_complete:
        old.execute_attempt(**args())
    else:
        with pytest.raises(ProviderCallUncertainError):
            old.execute_attempt(**args(operation=lambda:(_ for _ in ()).throw(TimeoutError())))
    with pytest.raises(ChapterBudgetError):r.execute_attempt(**args(operation=forbidden))
    assert s.report()['charged']['calls']==0 and not (r.store.root/'b.json').exists()


@pytest.mark.parametrize('removed',['b.json','intents/call-a1.json','receipts/call-a1.json','responses/call-a1.txt'])
def test_lost_physical_evidence_cannot_be_rebuilt_from_settled_ledger(tmp_path,removed):
    s=setup(tmp_path);r=runtime(tmp_path);r.execute_attempt(**args())
    (r.store.root/removed).unlink()
    with pytest.raises((ChapterBudgetError,OSError,ValueError)):
        runtime(tmp_path).execute_attempt(**args(operation=forbidden))
    assert not (r.store.root/removed).exists()
    assert s.report()['charged']['calls']==1


def test_unsettled_receipt_with_damaged_artifact_never_refunds(tmp_path):
    s=setup(tmp_path);crash(tmp_path,'after_receipt_before_chapter_settlement')
    r=runtime(tmp_path);(r.store.root/'responses/call-a1.txt').write_text('damaged')
    with pytest.raises(ModelCallIntegrityError):r.execute_attempt(**args(operation=forbidden))
    assert s.report()['charged_amount']=='0.0005'


def test_receipt_gap_is_reconciled_before_different_call_in_same_execution(tmp_path):
    s=setup(tmp_path);crash(tmp_path,'after_receipt_before_chapter_settlement')
    runtime(tmp_path).execute_attempt(**args(call_id='second'))
    assert s.report()['charged_amount']=='0.0001' and s.report()['charged']['calls']==2


def test_missing_endpoint_or_billable_bound_blocks_before_reservation(tmp_path):
    s=setup(tmp_path);r=runtime(tmp_path)
    with pytest.raises(ChapterBudgetError):r.execute_attempt(**args(endpoint_fingerprint=None,operation=forbidden))
    with pytest.raises(ChapterBudgetError):r.execute_attempt(**args(input_bound=None,operation=forbidden))
    assert s.report()['charged']['calls']==0 and not r.store.intents_dir.exists()


def holding_child(root,before_claim):
    def hold():
        print('held',flush=True)
        sys.stdin.read()
    def fault(event,*unused):
        if before_claim and event=='after_intent_before_dispatch':hold()
    def operation():
        if not before_claim:hold()
        return response()
    runtime(Path(root),fault_injector=fault).execute_attempt(**args(operation=operation))


@pytest.mark.parametrize('before_claim',[False,True])
def test_live_execution_cannot_be_recovered_or_dispatched_concurrently(tmp_path,before_claim):
    s=setup(tmp_path)
    code="import sys; sys.path.insert(0,'tests'); from test_chapter_budget_runtime import holding_child; holding_child(sys.argv[1],sys.argv[2]=='True')"
    child=subprocess.Popen([sys.executable,'-B','-c',code,str(tmp_path),str(before_claim)],stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='held'
        with pytest.raises(PersistenceLockError):runtime(tmp_path).execute_attempt(**args(operation=forbidden))
        assert s.report()['charged_amount']=='0.0005'
        child.kill();child.wait(timeout=10)
        expected=ProviderRequestNotSentError if before_claim else ProviderCallUncertainError
        with pytest.raises(expected):runtime(tmp_path).execute_attempt(**args(operation=forbidden))
        assert s.report()['charged_amount']==('0' if before_claim else '0.0005')
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=10)
        child.stdin.close();child.stdout.close();child.stderr.close()


@pytest.mark.parametrize('change',['execution','budget'])
def test_original_execution_binding_cannot_be_reassigned(tmp_path,change):
    s=setup(tmp_path);r=runtime(tmp_path);r.execute_attempt(**args())
    if change=='budget':
        other=tmp_path/'other';other.mkdir();s=setup(other)
        # Same configuration hash alone cannot grant a fresh empty ledger: the
        # original physical evidence still has no reservation in this store.
        execution='execution_gen'
    else:execution='execution_other'
    changed=ModelCallRuntimeContext(r.store,chapter_budget=ChapterModelCallBudget(s,execution_id=execution))
    with pytest.raises(ChapterBudgetError):changed.execute_attempt(**args(operation=forbidden))
