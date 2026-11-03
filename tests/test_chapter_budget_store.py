import copy
from decimal import localcontext
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from api.contracts import ModelResponse, reported_model_usage
from core.chapter_budget_contract import ChapterBudgetContractError, freeze_chapter_budget
from core.chapter_budget_store import (ChapterBudgetStore, ChapterBudgetError,
    ChapterBudgetConflict, ChapterBudgetExceeded, ChapterBudgetUncertain)
from core.model_calls import ModelCallStore, build_model_call_intent, build_model_call_receipt
from core.engine.persistence import PersistenceLockError


FP='a'*64


def budget(amount='0.01',**changes):
    args=dict(book_id='book-test',chapter=2,max_provider_calls=10,max_input_tokens=10000,
        max_output_tokens=10000,max_active_seconds='900',amount_limit=amount,
        currency='USD' if amount is not None else None,pricing=[] if amount is None else [dict(
        provider='openai',model='fixture',endpoint_type='official',endpoint_fingerprint=FP,currency='USD',
        input_price_per_million='1',output_price_per_million='2',input_token_semantics='input_includes_cache',
        cache_read_price_per_million='0.1',cache_write_5m_price_per_million='1.25',cache_write_1h_price_per_million='2')])
    args.update(changes)
    return freeze_chapter_budget(**args)


def intent(stage='generation',request=None):
    return build_model_call_intent(call_id='fact',attempt_id='fact-a1',provider='openai',model='fixture',
        stage=stage,request={'messages':['test']} if request is None else request,
        created_at='2026-09-10T00:00:00+00:00',budget_reservation={
        'reserved_input_tokens':100,'reserved_output_tokens':200,'reserved_total_tokens':300})


def receipt(source,*,status='succeeded',inputs=10,outputs=20,usage=None,model='fixture',endpoint='official'):
    raw=dict(input_tokens=inputs,output_tokens=outputs,cache_read_input_tokens=0,cache_creation_input_tokens=0)
    if usage is not None:raw=usage
    return build_model_call_receipt(source,status=status,received_at='2026-09-10T00:00:01+00:00',
        response=ModelResponse('fixture response',usage=reported_model_usage(raw),actual_model=model,endpoint_type=endpoint))


def bounds(source):
    return dict(max_billable_input_tokens=100,basis='verified_token_count',request_digest=source['request_digest'],evidence_sha256='b'*64)


def store(root,config=None):
    s=ChapterBudgetStore(root,book_id='book-test',chapter=2)
    s.initialize(config or budget())
    return s


def reserve(s,execution='execution_gen',source=None,**changes):
    source=source or intent()
    args=dict(endpoint_type='official',endpoint_fingerprint=FP,input_bound=bounds(source))
    args.update(changes)
    return s.reserve(execution,source,**args)


def settle(s,execution,result):
    if result['status'] not in {'budget_rejected','request_not_sent'}:
        row=next(x for x in s.execution_attempts(execution) if x['plan']['intent']['attempt_id']==result['attempt_id'])
        if not row['dispatch_claimed']:
            s.mark_dispatched(execution,row['plan']['intent'])
    return s.settle(execution,result)


def test_same_chapter_generating_then_extracting_shares_money_across_restart(tmp_path):
    # Maximum input cache tariff=2: reservation .0006, settled cost .00005.
    root=tmp_path/'budget';s=store(root,budget('0.00064'));first=intent()
    reserve(s,source=first)
    settle(s,'execution_gen',receipt(first))
    restarted=ChapterBudgetStore(root,book_id='book-test',chapter=2)
    with pytest.raises(ChapterBudgetExceeded,match='amount_limit'):
        reserve(restarted,'execution_fact',intent('chapter_fact_extraction'))
    report=restarted.report()
    assert report['charged_amount']=='0.00005'
    assert report['charged']['calls']==1
    assert report['items'][1]['accounting_state']=='released'
    with pytest.raises(ChapterBudgetExceeded):reserve(restarted,'execution_new_key',intent('generation'))
    assert restarted.report()['charged_amount']=='0.00005'


def test_same_attempt_id_in_different_executions_counts_twice_and_replay_once(tmp_path):
    s=store(tmp_path);source=intent();result=receipt(source)
    reserve(s,'execution_a',source);settle(s,'execution_a',result)
    reserve(s,'execution_b',source);settle(s,'execution_b',result)
    before=(tmp_path/'ledger.json').read_bytes()
    settle(s,'execution_a',result);settle(s,'execution_b',result)
    reserve(s,'execution_a',source)
    assert (tmp_path/'ledger.json').read_bytes()==before
    report=s.report()
    assert report['charged_amount']=='0.0001' and report['charged']['calls']==2


def test_store_uses_real_durable_intent_and_receipt_after_process_reopen(tmp_path):
    s=store(tmp_path/'b');calls=ModelCallStore(tmp_path/'calls')
    source=calls.record_intent(intent());reserve(s,source=source)
    result=calls.record_receipt(receipt(source))
    # Crash window: immutable Receipt exists, ledger still only reserves.
    assert s.report()['charged_amount']=='0.0006'
    reopened=ChapterBudgetStore(tmp_path/'b',book_id='book-test',chapter=2)
    settle(reopened,'execution_gen',calls.load_receipt(result['attempt_id']))
    code='''import json,sys
from core.chapter_budget_store import ChapterBudgetStore
print(json.dumps(ChapterBudgetStore(sys.argv[1],book_id='book-test',chapter=2).report()))
'''
    child=subprocess.run([sys.executable,'-B','-c',code,str(tmp_path/'b')],capture_output=True,text=True,timeout=20,check=True)
    actual=json.loads(child.stdout)
    assert actual==reopened.report() and actual['charged_amount']=='0.00005'


def test_intent_without_receipt_keeps_reserve_and_blocks_new_attempt(tmp_path):
    s=store(tmp_path);source=intent();reserve(s,source=source)
    before=(tmp_path/'ledger.json').read_bytes()
    other=ChapterBudgetStore(tmp_path,book_id='book-test',chapter=2)
    with pytest.raises(ChapterBudgetUncertain):reserve(other,'execution_other')
    assert (tmp_path/'ledger.json').read_bytes()==before
    assert other.report()['charged_amount']=='0.0006'


@pytest.mark.parametrize('status',['budget_rejected','request_not_sent'])
def test_provably_not_sent_releases_reserved_amount_and_capacity(tmp_path,status):
    s=store(tmp_path);source=intent();reserve(s,source=source)
    got=settle(s,'execution_gen',receipt(source,status=status,inputs=0,outputs=0))
    assert got['charged_amount']=='0' and got['charged_calls']==0
    reserve(s,'execution_after_release')
    assert s.report()['charged']['calls']==1


@pytest.mark.parametrize('status',['provider_rejected','partial_response_failed'])
def test_terminal_provider_failure_never_refunds_reservation(tmp_path,status):
    s=store(tmp_path);source=intent();reserve(s,source=source)
    got=settle(s,'execution_gen',receipt(source,status=status,inputs=0,outputs=0))
    assert got['accounting_state']=='held_unknown' and got['charged_amount']=='0.0006'
    with pytest.raises(ChapterBudgetUncertain):reserve(s,'execution_retry')


@pytest.mark.parametrize('usage',[
    {},{'input_tokens':10,'output_tokens':20},
    {'input_tokens':10,'output_tokens':20,'cache_read_input_tokens':0,'cache_creation_input_tokens':2},
    {'input_tokens':10,'output_tokens':20,'cache_read_input_tokens':0,'cache_creation_input_tokens':0,
     'cache_creation':{'ephemeral_5m_input_tokens':1,'ephemeral_1h_input_tokens':0}},
    {'input_tokens':True,'output_tokens':0,'cache_read_input_tokens':0,'cache_creation_input_tokens':0},
])
def test_missing_or_conflicting_billing_usage_is_not_zero_or_refundable(tmp_path,usage):
    s=store(tmp_path);source=intent();reserve(s,source=source)
    got=settle(s,'execution_gen',receipt(source,usage=usage))
    assert got['accounting_state']=='held_unknown' and got['charged_amount']=='0.0006'


@pytest.mark.parametrize('changes',[{'model':'different'},{'endpoint':'openai_compatible'}])
def test_returned_provider_identity_drift_records_receipt_and_blocks(tmp_path,changes):
    s=store(tmp_path);source=intent();reserve(s,source=source)
    got=settle(s,'execution_gen',receipt(source,**changes))
    assert got['reason']=='returned_model_or_endpoint_differs' and got['receipt_hash']
    with pytest.raises(ChapterBudgetUncertain):reserve(s,'execution_retry')


def test_actual_usage_above_reservation_is_recorded_then_blocks_following_call(tmp_path):
    s=store(tmp_path,budget('0.0007'));source=intent();reserve(s,source=source)
    result=settle(s,'execution_gen',receipt(source,inputs=1000,outputs=1000))
    assert result['charged_amount']=='0.003'
    assert s.report()['exceeded']==['amount']
    with pytest.raises(ChapterBudgetExceeded):reserve(s,'execution_next')
    assert s.report()['charged_amount']=='0.003'


@pytest.mark.parametrize('limit',['max_provider_calls','max_input_tokens','max_output_tokens'])
def test_no_price_mode_still_shares_token_and_call_limits(tmp_path,limit):
    config=budget(None,**{limit:{'max_provider_calls':1,'max_input_tokens':150,'max_output_tokens':210}[limit]})
    s=store(tmp_path,config);source=intent();reserve(s,source=source,input_bound=None)
    settle(s,'execution_gen',receipt(source))
    with pytest.raises(ChapterBudgetExceeded,match=limit):reserve(s,'execution_fact',input_bound=None)
    report=s.report()
    assert report['charged_amount'] is None and not report['amount_controlled']


def test_foreign_request_and_modified_endpoint_cannot_reuse_attempt(tmp_path):
    s=store(tmp_path);source=intent();reserve(s,source=source)
    with pytest.raises(ChapterBudgetConflict):reserve(s,source=intent(request={'messages':['changed']}))
    with pytest.raises(ChapterBudgetContractError):reserve(s,endpoint_fingerprint='c'*64)
    with pytest.raises(ChapterBudgetConflict):settle(s,'execution_gen',receipt(intent(request={'messages':['changed']})))


def test_freeze_cannot_reset_or_reprice_used_chapter(tmp_path):
    s=store(tmp_path);reserve(s)
    before=(tmp_path/'ledger.json').read_bytes()
    with pytest.raises(ChapterBudgetConflict):s.initialize(budget('1'))
    assert (tmp_path/'ledger.json').read_bytes()==before
    s.initialize(budget())
    assert s.report()['charged_amount']=='0.0006'


def test_missing_ledger_after_freeze_is_not_silently_recreated(tmp_path):
    s=store(tmp_path);(tmp_path/'ledger.json').unlink()
    with pytest.raises(ChapterBudgetError):s.initialize(budget())
    assert not (tmp_path/'ledger.json').exists()


@pytest.mark.parametrize('file',['budget.json','ledger.json'])
def test_damaged_evidence_blocks_reads_and_reservations(tmp_path,file):
    s=store(tmp_path);reserve(s)
    (tmp_path/file).write_text('{}',encoding='utf-8')
    with pytest.raises((ChapterBudgetError,ChapterBudgetContractError)):s.report()
    with pytest.raises((ChapterBudgetError,ChapterBudgetContractError)):reserve(s,'execution_new')
    assert (tmp_path/file).read_text()=='{}'


def test_budget_read_before_initialization_is_read_only(tmp_path):
    root=tmp_path/'b';s=ChapterBudgetStore(root,book_id='book-test',chapter=2)
    with pytest.raises(ChapterBudgetError):s.report()
    assert not root.exists()


def test_money_mode_refuses_approximate_or_unbound_input(tmp_path):
    s=store(tmp_path);source=intent()
    with pytest.raises(ChapterBudgetError):reserve(s,source=source,input_bound=None)
    bad=bounds(source);bad['request_digest']='f'*64
    with pytest.raises(ChapterBudgetError):reserve(s,source=source,input_bound=bad)
    bad=bounds(source);bad['basis']='calibrated_estimate'
    with pytest.raises(ChapterBudgetError):reserve(s,source=source,input_bound=bad)
    assert s.report()['charged']['calls']==0


def test_amount_sum_ignores_ambient_decimal_precision(tmp_path):
    s=store(tmp_path);source=intent();reserve(s,source=source)
    settle(s,'execution_gen',receipt(source,inputs=11,outputs=27))
    reserve(s,'execution_fact',source);settle(s,'execution_fact',receipt(source,inputs=12,outputs=29))
    with localcontext() as context:
        context.prec=2
        assert s.report()['charged_amount']=='0.000135'


def test_hardlinked_lock_is_rejected_without_touching_other_file(tmp_path):
    root=tmp_path/'b';lock=root/'budget-lock';lock.mkdir(parents=True)
    target=tmp_path/'other.txt';target.write_bytes(b'unchanged')
    os.link(target,lock/'.persistence.lock')
    with pytest.raises(PersistenceLockError):store(root)
    assert target.read_bytes()==b'unchanged'
    assert not (root/'budget.json').exists()


def test_process_lock_blocks_second_writer_without_new_reservation(tmp_path):
    s=store(tmp_path)
    code='''import sys
from core.chapter_budget_lock import chapter_budget_lock
from pathlib import Path
with chapter_budget_lock(Path(sys.argv[1])/'budget-lock'):
    print('held',flush=True)
    sys.stdin.read()
'''
    child=subprocess.Popen([sys.executable,'-B','-c',code,str(tmp_path)],stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='held'
        before=(tmp_path/'ledger.json').read_bytes()
        with pytest.raises(PersistenceLockError):reserve(s)
        assert (tmp_path/'ledger.json').read_bytes()==before
        child.kill();child.wait(timeout=10)
        reserve(s)
        assert s.report()['charged']['calls']==1
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=10)
        child.stdin.close();child.stdout.close();child.stderr.close()


def test_dispatch_claim_is_one_way_and_required_for_provider_receipt(tmp_path):
    s=store(tmp_path);source=intent();reserve(s,source=source)
    with pytest.raises(ChapterBudgetConflict):s.settle('execution_gen',receipt(source))
    s.mark_dispatched('execution_gen',source)
    before=(tmp_path/'ledger.json').read_bytes()
    with pytest.raises(ChapterBudgetConflict):s.mark_dispatched('execution_gen',source)
    with pytest.raises(ChapterBudgetConflict):s.settle('execution_gen',receipt(source,status='budget_rejected'))
    assert (tmp_path/'ledger.json').read_bytes()==before
    s.settle('execution_gen',receipt(source))
    with pytest.raises(ChapterBudgetConflict):s.mark_dispatched('execution_gen',source)


def test_dispatch_cannot_claim_foreign_unreserved_or_rejected_attempt(tmp_path):
    s=store(tmp_path,budget('0'));source=intent()
    with pytest.raises(ChapterBudgetConflict):s.mark_dispatched('execution_gen',source)
    with pytest.raises(ChapterBudgetExceeded):reserve(s,source=source)
    with pytest.raises(ChapterBudgetConflict):s.mark_dispatched('execution_gen',source)
    assert s.report()['charged']['calls']==0


@pytest.mark.parametrize('raw',[
    {'prompt_tokens':100,'completion_tokens':20,'prompt_tokens_details':{'cached_tokens':0,'cache_write_tokens':30}},
    {'input_tokens':100,'output_tokens':20,'cache_creation_input_tokens':0,
     'prompt_tokens_details':{'cached_tokens':0,'cache_write_tokens':30}},
])
def test_openai_cache_write_quantity_is_not_ignored_or_assumed_free(tmp_path,raw):
    s=store(tmp_path);source=intent();reserve(s,source=source)
    got=settle(s,'execution_gen',receipt(source,usage=raw))
    assert got['accounting_state']=='held_unknown'
    assert got['charged_amount']=='0.0006'
    with pytest.raises(ChapterBudgetUncertain):reserve(s,'execution_next')
