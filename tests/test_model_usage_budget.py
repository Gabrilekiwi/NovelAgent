"""Real local budget/runtime evidence; synthetic tariffs and no network calls."""
import json

import pytest

from api.contracts import ModelResponse, reported_model_usage
from core.chapter_budget_contract import freeze_chapter_budget
from core.chapter_budget_freeze import ChapterBudgetFreezeRegistry
from core.chapter_budget_store import ChapterBudgetStore
from core.chapter_model_budget import ChapterModelCallBudget
from core.memory_v2.canonical import canonical_json_hash
from core.model_call_runtime import ModelCallRuntimeContext
from core.model_calls import ModelCallStore, ModelCallIntegrityError
from core.model_call_usage import COST_EVIDENCE_KEY
from tests.test_chapter_budget_runtime import args
from tests.test_model_call_usage import cost
from workbench.model_usage import read_project_model_usage


class SyntheticStop(BaseException):
    """Exit the runtime without entering its ordinary request-failure cleanup."""


def seed(root, *, book_id='book-test', chapter=2, currency='USD', execution='execution_usage', fault=None,
         usage=None, bill=None, actual_model='fixture', amount='1', semantics='input_includes_cache'):
    budget=freeze_chapter_budget(book_id=book_id,chapter=chapter,max_provider_calls=10,
        max_input_tokens=10000,max_output_tokens=10000,max_active_seconds='900',
        amount_limit=amount,currency=currency,pricing=[dict(provider='openai',model='fixture',
            endpoint_type='official',endpoint_fingerprint='a'*64,currency=currency,input_price_per_million='1',
            output_price_per_million='2',cache_read_price_per_million='0.1',
            cache_write_5m_price_per_million='1.25',cache_write_1h_price_per_million='2',
            input_token_semantics=semantics)] if amount is not None else [])
    ChapterBudgetFreezeRegistry(root,book_id=book_id).freeze(budget,before_write=lambda:None)
    ledger=ChapterBudgetStore(root/f'.novelagent/runtime/budgets/c{chapter}',book_id=book_id,chapter=chapter)
    calls=ModelCallStore(root/'.novelagent/runtime/runs/executions'/execution/'model_calls')
    def interrupt(event,*unused):
        if event==fault:raise SyntheticStop('synthetic crash window')
    runtime=ModelCallRuntimeContext(calls,chapter_budget=ChapterModelCallBudget(ledger,execution_id=execution),
        fault_injector=interrupt)
    quantities=dict(input_tokens=100,output_tokens=20,cache_read_input_tokens=40,
        cache_creation_input_tokens=30,cache_creation={'ephemeral_5m_input_tokens':20,'ephemeral_1h_input_tokens':10})
    if usage is not None:quantities=usage
    normalized=reported_model_usage(quantities)
    # A trusted billing adapter's evidence is distinct from raw Provider usage.
    if bill is not None:normalized[COST_EVIDENCE_KEY]=bill
    response=ModelResponse('synthetic result',actual_model=actual_model,endpoint_type='official',usage=normalized)
    if fault:
        with pytest.raises(SyntheticStop,match='synthetic crash window'):
            runtime.execute_attempt(**args(operation=lambda:response))
    else:runtime.execute_attempt(**args(operation=lambda:response))
    return ledger,calls,runtime


def report(root,book_id='book-test'):
    return read_project_model_usage(root,book_id=book_id)


def files(root):
    return {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('semantics,expected',[
    ('input_includes_cache','0.000119'),('input_excludes_cache','0.000189')])
def test_frozen_cache_cost_is_verified_read_only_and_replay_counted_once(tmp_path,semantics,expected):
    ledger,calls,runtime=seed(tmp_path,semantics=semantics)
    before=files(tmp_path)
    first=report(tmp_path)
    item=first['items'][0]
    assert item['cost']['classification']=='estimated' and item['cost']['source']=='frozen_chapter_pricing'
    assert item['cost']['amount']==expected and item['cost']['source_id']==ledger.frozen_budget()['budget_sha256']
    assert len(item['cost']['calculation']['terms'])==5
    assert item['cost']['calculation']['input_token_semantics']==semantics
    assert first['summary']['costs_by_currency']['USD']['estimated']==expected
    def forbidden():pytest.fail('replay must not invoke Provider')
    runtime.execute_attempt(**args(operation=forbidden))
    second=report(tmp_path)
    assert first['items']==second['items'] and first['summary']==second['summary']
    assert first['summary']['attempt_count']==1 and files(tmp_path)==before


@pytest.mark.parametrize('fault',[
    'after_chapter_reservation_before_intent','after_intent_before_dispatch',
    'after_dispatch_claim_before_provider','after_receipt_before_chapter_settlement'])
def test_pending_reservation_never_becomes_a_fee_or_get_settles_it(tmp_path,fault):
    ledger,_,_=seed(tmp_path,fault=fault)
    before=files(tmp_path)
    result=report(tmp_path)
    assert result['summary']['costs_by_currency']=={}
    budget=result['chapter_budgets'][0]
    assert budget['charged_amount']=='0.0006' and budget['items'][0]['accounting_state']=='reserved'
    assert result['summary']['attempt_count']==(0 if fault=='after_chapter_reservation_before_intent' else 1)
    assert files(tmp_path)==before and ledger.execution_attempts('execution_usage')[0]['receipt'] is None


def test_unknown_usage_holds_occupancy_and_real_bill_has_priority(tmp_path):
    seed(tmp_path,usage={'input_tokens':100,'output_tokens':20},bill=cost(amount='0.003'))
    result=report(tmp_path)
    item=result['items'][0]
    assert item['budget_accounting']['accounting_state']=='held_unknown'
    assert item['cost']['classification']=='actual' and item['cost']['amount']=='0.003'
    assert result['summary']['costs_by_currency']['USD']['estimated_count']==0


def test_actual_bill_is_not_overwritten_by_a_settled_estimate(tmp_path):
    seed(tmp_path,usage={'input_tokens':100,'output_tokens':20,'cache_read_input_tokens':0,
        'cache_creation_input_tokens':0},bill=cost(amount='0.003'))
    item=report(tmp_path)['items'][0]
    assert item['budget_accounting']['cost_classification']=='estimated'
    assert item['cost']['classification']=='actual' and item['cost']['amount']=='0.003'


@pytest.mark.parametrize('kind',['unknown_cache','wrong_model','unpriced'])
def test_unsupported_pricing_is_not_projected_as_a_zero_or_estimate(tmp_path,kind):
    changes={'unknown_cache':{'usage':{'input_tokens':10,'output_tokens':20}},
        'wrong_model':{'actual_model':'other-model'},'unpriced':{'amount':None}}[kind]
    seed(tmp_path,**changes)
    result=report(tmp_path)
    assert result['summary']['unknown_cost_count']==1 and result['summary']['costs_by_currency']=={}


def test_different_chapter_currencies_keep_separate_subtotals(tmp_path):
    seed(tmp_path)
    seed(tmp_path,chapter=3,currency='CNY',execution='execution_other')
    result=report(tmp_path)
    assert set(result['summary']['costs_by_currency'])=={'CNY','USD'}
    assert all(group['estimated']=='0.000119' for group in result['summary']['costs_by_currency'].values())


@pytest.mark.parametrize('damage',['binding_missing','binding_other','ledger_missing','ledger_hash',
    'receipt_missing','response_missing','response_hash','ready_missing','book_identity','intent_missing'])
def test_damaged_frozen_evidence_never_returns_partial_costs_or_repairs(tmp_path,damage):
    ledger,calls,_=seed(tmp_path)
    if damage=='binding_missing':(calls.root/'b.json').unlink()
    elif damage=='binding_other':
        value=json.loads((calls.root/'b.json').read_text())
        value['book_id']='book-other'
        value['binding_sha256']=canonical_json_hash(value,exclude_fields=('binding_sha256',),exclude_environment_fields=False)
        (calls.root/'b.json').write_text(json.dumps(value),encoding='utf-8')
    elif damage=='ledger_missing':(ledger.root/'ledger.json').unlink()
    elif damage=='ledger_hash':(ledger.root/'ledger.json').write_text('{}',encoding='utf-8')
    elif damage=='receipt_missing':calls.receipt_path('call-a1').unlink()
    elif damage.startswith('response_'):
        path=calls.root/calls.load_receipt('call-a1')['response_artifact_ref']
        if damage=='response_missing':path.unlink()
        else:path.write_text('changed',encoding='utf-8')
    elif damage=='ready_missing':(ledger.root.parent/'r2.json').unlink()
    elif damage=='intent_missing':calls.intent_path('call-a1').unlink()
    before=files(tmp_path)
    with pytest.raises((OSError,ValueError,RuntimeError)):
        report(tmp_path,book_id='book-other' if damage=='book_identity' else 'book-test')
    assert files(tmp_path)==before


def test_bound_history_requires_trusted_book_identity(tmp_path):
    seed(tmp_path)
    with pytest.raises(ModelCallIntegrityError,match='registered book identity'):
        read_project_model_usage(tmp_path)
