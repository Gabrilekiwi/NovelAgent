"""Synthetic tariffs, isolated stores and fake SDK only; no paid requests."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from api.budget_endpoint import endpoint_fingerprint
from api.contracts import ModelCallError, ModelResponse, reported_model_usage
from api.openai_client import chat_completion
from api.retry import MODEL_READ_GENERATION, RetryPolicy
from core.chapter_budget_contract import ChapterBudgetContractError, freeze_chapter_budget, normalize_pricing_row
from core.chapter_budget_store import ChapterBudgetStore, ChapterBudgetError
from core.chapter_model_budget import ChapterModelCallBudget
from core.memory_v2.canonical import canonical_json_hash
from core.model_call_runtime import ModelCallRuntimeContext, ProviderCallUncertainError
from core.model_calls import ModelCallStore, canonical_model_request_digest
from core.provider_billing_request import (BillingRequestError, openai_text_billing_policy,
    plan_openai_text_billing, validate_stored_openai_billing_plan)
from core.provider_billing_response import BILLING_RESPONSE_KEY, OpenAIBillingObservations


URL='https://api.openai.com/v1'
MODEL='gpt-5.4-mini-2026-03-17'
FP=endpoint_fingerprint('openai',URL)


def body():
    return dict(model=MODEL,messages=[{'role':'user','content':'synthetic private draft'}],
        temperature=0.8,max_completion_tokens=200,service_tier='default',
        prompt_cache_retention='in_memory',n=1,stream=False)


def price():
    return dict(provider='openai',model=MODEL,endpoint_type='official',endpoint_fingerprint=FP,
        currency='USD',input_price_per_million='1',output_price_per_million='2',
        cache_read_price_per_million='0.1',input_token_semantics='input_includes_cache',
        billing_policy=openai_text_billing_policy())


def setup(root,*,amount='1'):
    store=ChapterBudgetStore(root/'b',book_id='fixture-book',chapter=2)
    store.initialize(freeze_chapter_budget(book_id='fixture-book',chapter=2,max_provider_calls=10,
        max_input_tokens=2000000,max_output_tokens=10000,max_active_seconds='900',
        amount_limit=amount,currency='USD',pricing=[price()]))
    return store


def runtime(root,*,execution='execution_gen',**kwargs):
    store=ChapterBudgetStore(root/'b',book_id='fixture-book',chapter=2)
    return ModelCallRuntimeContext(ModelCallStore(root/execution),
        chapter_budget=ChapterModelCallBudget(store,execution_id=execution),**kwargs)


def usage():
    return {'prompt_tokens':100,'completion_tokens':20,'total_tokens':120,
        'prompt_tokens_details':{'cached_tokens':40}}


def result():
    observed=OpenAIBillingObservations()
    observed.observe(model=MODEL,service_tier='default')
    return ModelResponse('draft',actual_model=MODEL,endpoint_type='official',
        usage=observed.attach(reported_model_usage(usage())))


def args(request=None,**changes):
    request=body() if request is None else request
    value=dict(call_id='call',attempt_number=1,provider='openai',model=MODEL,stage='generation',
        endpoint_type='official',endpoint_fingerprint=FP,request=request,max_output_tokens=200,
        input_tokens=50,billing_plan=plan_openai_text_billing(request,base_url=URL),
        billing_base_url=URL,operation=result)
    value.update(changes)
    return value


def forbidden():
    pytest.fail('SDK/Provider must not be invoked')


@pytest.mark.parametrize('change',[
    {'model':'gpt-5.6'}, {'endpoint_type':'openai_compatible'}, {'endpoint_fingerprint':'a'*64},
    {'input_token_semantics':'input_excludes_cache'}, {'cache_read_price_per_million':None},
    {'cache_write_5m_price_per_million':'1.25'}, {'cache_write_1h_price_per_million':'2'},
    {'billing_policy':{**openai_text_billing_policy(),'service_tier':'auto'}},
    {'billing_policy':{**openai_text_billing_policy(),'capability_sha256':'a'*64}},
])
def test_frozen_tariff_policy_cannot_be_reused_for_other_billing_semantics(change):
    with pytest.raises(ChapterBudgetContractError):normalize_pricing_row({**price(),**change})


def test_persisted_plan_rederives_capability_and_keeps_prompt_out_of_ledger(tmp_path):
    s=setup(tmp_path);r=runtime(tmp_path)
    assert r.execute_attempt(**args())=='draft'
    record=s.execution_attempts('execution_gen')[0]
    assert record['plan']['billing_request_plan']['capability']['cache_policy']=='legacy_automatic_no_write_surcharge'
    assert record['plan']['pricing']==price()
    raw=(s.root/'ledger.json').read_text(encoding='utf-8')
    assert 'synthetic private draft' not in raw
    before=(s.root/'ledger.json').read_bytes()
    assert runtime(tmp_path).execute_attempt(**args(operation=forbidden))=='draft'
    assert (s.root/'ledger.json').read_bytes()==before
    assert s.report()['charged_amount']=='0.000104'
    value=json.loads(raw)
    plan=value['attempts'][0]['plan']['billing_request_plan']
    plan['max_billable_input_tokens']=1
    plan['plan_sha256']=canonical_json_hash({k:v for k,v in plan.items() if k!='plan_sha256'},exclude_environment_fields=False)
    value['ledger_sha256']=canonical_json_hash({k:v for k,v in value.items() if k!='ledger_sha256'},exclude_environment_fields=False)
    (s.root/'ledger.json').write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(BillingRequestError):s.report()


def test_invalid_original_v1_request_plan_is_not_automatically_reinterpreted():
    plan=plan_openai_text_billing(body(),base_url=URL)
    plan['schema_version']='provider_billing_request_plan_v1'
    plan['plan_sha256']=canonical_json_hash({k:v for k,v in plan.items() if k!='plan_sha256'},exclude_environment_fields=False)
    with pytest.raises(BillingRequestError):validate_stored_openai_billing_plan(plan)


@pytest.mark.parametrize('model,url,available',[(MODEL,URL,True),('alias',URL,False),
    (MODEL,'https://user:synthetic-secret@api.openai.com/v1',False),(MODEL,'https://proxy.invalid/v1',False)])
def test_product_monetary_option_only_exposes_supported_identity_without_keys(monkeypatch,model,url,available):
    from workbench.chapter_budgets import ChapterBudgetService
    monkeypatch.setattr('workbench.chapter_budgets.get_config',lambda:SimpleNamespace(
        openai_model=model,openai_base_url=url,openai_api_key='synthetic-secret'))
    option=ChapterBudgetService.monetary_option()
    assert option['available'] is available
    assert 'synthetic-secret' not in json.dumps(option) and url not in json.dumps(option)
    if available:
        assert option['pricing_template']['billing_policy']==openai_text_billing_policy()
        assert option['input_reservation_per_call']==400000


@pytest.mark.parametrize('change',[
    {'request':None}, {'base_url':'https://proxy.invalid/v1'},
    {'request':{**body(),'service_tier':'priority'}},
])
def test_self_hashed_plan_without_matching_final_request_cannot_reserve(tmp_path,change):
    s=setup(tmp_path)
    source=runtime(tmp_path)
    from core.model_calls import build_model_call_intent
    intent=build_model_call_intent(call_id='call',attempt_id='call-a1',provider='openai',model=MODEL,
        stage='generation',request=body(),budget_reservation={
            'reserved_input_tokens':50,'reserved_output_tokens':200,'reserved_total_tokens':250})
    before=(s.root/'ledger.json').read_bytes()
    with pytest.raises(BillingRequestError):
        s.reserve('execution_gen',intent,endpoint_type='official',endpoint_fingerprint=FP,
            billing_plan=plan_openai_text_billing(body(),base_url=URL),**{'request':body(),'base_url':URL,**change})
    assert (s.root/'ledger.json').read_bytes()==before
    assert source.chapter_budget.store.report()['items']==[]


def test_mutating_request_after_reservation_is_provably_not_sent(tmp_path):
    s=setup(tmp_path);request=body()
    def mutate():request['messages'][0]['content']='changed after intent'
    with pytest.raises(BillingRequestError):
        runtime(tmp_path).execute_attempt(**args(request,before_dispatch=mutate,operation=forbidden))
    report=s.report()
    assert report['charged_amount']=='0' and report['items'][0]['accounting_state']=='released'
    assert not s.execution_attempts('execution_gen')[0]['dispatch_claimed']


def install_sdk(monkeypatch,root,*,response=None,stream=False,create=None,base_url=URL):
    calls=[];clients=[]
    response=response or SimpleNamespace(model=MODEL,service_tier='default',usage=usage(),
        choices=[SimpleNamespace(message=SimpleNamespace(content='draft'),finish_reason='stop')])
    def send(**payload):
        calls.append(copy.deepcopy(payload))
        row=ChapterBudgetStore(root/'b',book_id='fixture-book',chapter=2).execution_attempts('execution_gen')[0]
        assert row['dispatch_claimed'] and row['receipt'] is None
        assert row['plan']['intent']['request_digest']==canonical_model_request_digest(payload)
        assert row['plan']['reserved_cost']['amount']=='0.4004'
        return create(payload) if create is not None else response
    class FakeOpenAI:
        def __init__(self,**kwargs):
            self.base_url=base_url
            self.chat=SimpleNamespace(completions=SimpleNamespace(create=send))
            clients.append(self)
    monkeypatch.setitem(sys.modules,'openai',SimpleNamespace(OpenAI=FakeOpenAI))
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-unused-key')
    monkeypatch.setenv('OPENAI_MODEL',MODEL)
    monkeypatch.setenv('OPENAI_BASE_URL','')
    monkeypatch.setenv('OPENAI_STREAM','true' if stream else 'false')
    monkeypatch.setenv('OPENAI_MAX_OUTPUT_TOKENS','200')
    return calls,clients


def sdk_call(r,**changes):
    return chat_completion([{'role':'user','content':'synthetic private draft'}],
        model_call_runtime=r,call_id='call',input_tokens=50,
        retry_policy=RetryPolicy(profile=MODEL_READ_GENERATION,max_attempts=1,allow_retry=False),**changes)


def test_sdk_final_payload_price_reserve_receipt_and_restart_replay(monkeypatch,tmp_path):
    s=setup(tmp_path);calls,_=install_sdk(monkeypatch,tmp_path)
    assert sdk_call(runtime(tmp_path))=='draft'
    assert calls==[body()]
    receipt=runtime(tmp_path).store.load_receipt('call-a1')
    assert receipt['usage'][BILLING_RESPONSE_KEY]['service_tiers']==['default']
    assert receipt['usage']['prompt_tokens_details']=={'cached_tokens':40}
    assert s.report()['items'][0]['accounting_state']=='settled'
    assert sdk_call(runtime(tmp_path))=='draft' and len(calls)==1


def test_installed_sdk_typed_response_preserves_optional_nulls_and_settles(monkeypatch,tmp_path):
    from openai.types.chat import ChatCompletion
    s=setup(tmp_path)
    response=ChatCompletion.model_validate(dict(id='synthetic',created=0,object='chat.completion',
        model=MODEL,service_tier='default',usage=usage(),choices=[{
            'index':0,'message':{'role':'assistant','content':'draft'},'finish_reason':'stop'}]))
    calls,_=install_sdk(monkeypatch,tmp_path,response=response)
    assert sdk_call(runtime(tmp_path))=='draft'
    assert s.report()['charged_amount']=='0.000104' and len(calls)==1


@pytest.mark.parametrize('field,value',[('prompt_tokens',400001),('completion_tokens',201)])
def test_provider_usage_beyond_reviewed_bound_blocks_future_spending(tmp_path,field,value):
    s=setup(tmp_path);raw=usage();raw[field]=value
    observed=OpenAIBillingObservations();observed.observe(model=MODEL,service_tier='default')
    response=ModelResponse('draft',actual_model=MODEL,endpoint_type='official',
        usage=observed.attach(reported_model_usage(raw)))
    runtime(tmp_path).execute_attempt(**args(operation=lambda:response))
    account=s.report()['items'][0]
    assert account['accounting_state']=='held_unknown' and account['reason']=='billing_bound_exceeded'
    assert float(account['charged_amount'])>=0.4004


def test_generated_then_extracted_requests_cannot_reset_chapter_money(monkeypatch,tmp_path):
    s=setup(tmp_path,amount='0.40045');calls,_=install_sdk(monkeypatch,tmp_path)
    assert sdk_call(runtime(tmp_path))=='draft'
    with pytest.raises(ModelCallError) as failure:
        sdk_call(runtime(tmp_path,execution='execution_fact'),stage='chapter_fact_extraction')
    assert failure.value.failure_category=='local_budget'
    assert len(calls)==1 and s.report()['charged_amount']=='0.000104'
    assert s.report()['items'][1]['accounting_state']=='released'


@pytest.mark.parametrize('metadata,raw',[
    ({'service_tier':None},usage()), ({'service_tier':'priority'},usage()),
    ({'model':None},usage()), ({'model':'alias-other'},usage()),
    ({},{'prompt_tokens':100,'completion_tokens':20}),
    ({},{**usage(),'prompt_tokens_details':{'cached_tokens':40,'cache_write_tokens':1}}),
    ({},{**usage(),'prompt_tokens_details':{'cached_tokens':40,'audio_tokens':1}}),
    ({'service_tier':None},{**usage(),BILLING_RESPONSE_KEY:{'service_tiers':['default']}}),
])
def test_missing_changed_or_spoofed_actual_billing_evidence_retains_money(monkeypatch,tmp_path,metadata,raw):
    s=setup(tmp_path)
    response=SimpleNamespace(**{**dict(model=MODEL,service_tier='default',usage=raw,
        choices=[SimpleNamespace(message=SimpleNamespace(content='draft'),finish_reason='stop')]),**metadata})
    calls,_=install_sdk(monkeypatch,tmp_path,response=response)
    assert sdk_call(runtime(tmp_path))=='draft'
    report=s.report()
    assert report['items'][0]['accounting_state']=='held_unknown' and report['charged_amount']=='0.4004'
    with pytest.raises(ModelCallError):sdk_call(runtime(tmp_path,execution='execution_other'))
    assert len(calls)==1


def test_effective_sdk_endpoint_change_before_dispatch_releases_without_sending(monkeypatch,tmp_path):
    s=setup(tmp_path);calls,clients=install_sdk(monkeypatch,tmp_path)
    def fault(event,*unused):
        if event=='after_intent_before_dispatch':clients[0].base_url='https://proxy.invalid/v1'
    with pytest.raises(ModelCallError):sdk_call(runtime(tmp_path,fault_injector=fault))
    assert calls==[] and s.report()['charged_amount']=='0'
    assert s.report()['items'][0]['accounting_state']=='released'


@pytest.mark.parametrize('tier,model,expected',[
    ('default',MODEL,'settled'), ('priority',MODEL,'held_unknown'),
    ('default','other-model','held_unknown'), (None,MODEL,'settled')])
def test_streamed_billing_observations_do_not_hide_identity_changes(monkeypatch,tmp_path,tier,model,expected):
    s=setup(tmp_path)
    def chunks(payload):
        assert payload['stream_options']=={'include_usage':True}
        return iter([SimpleNamespace(model=MODEL,service_tier='default',usage=None,
            choices=[SimpleNamespace(delta=SimpleNamespace(content='draft'),finish_reason=None)]),
            SimpleNamespace(model=model,service_tier=tier,usage=usage(),choices=[])])
    calls,_=install_sdk(monkeypatch,tmp_path,stream=True,create=chunks)
    assert sdk_call(runtime(tmp_path))=='draft'
    assert s.report()['items'][0]['accounting_state']==expected and len(calls)==1


def crash_child(root,event):
    root=Path(root)
    def fault(name,*unused):
        if name==event:os._exit(42)
    def send():
        (root/'sent.txt').write_text('1',encoding='utf-8')
        return result()
    runtime(root,fault_injector=fault).execute_attempt(**args(operation=send))
    raise AssertionError('crash boundary was not reached')


@pytest.mark.parametrize('event',[
    'after_chapter_reservation_before_intent','after_intent_before_dispatch',
    'after_dispatch_claim_before_provider','after_provider_response_before_artifact',
    'after_response_artifact_before_receipt','after_receipt_before_chapter_settlement'])
def test_original_billing_plan_and_occupancy_survive_process_death(tmp_path,event):
    s=setup(tmp_path)
    code="import sys; from tests.test_reviewed_monetary_admission import crash_child; crash_child(sys.argv[1],sys.argv[2])"
    child=subprocess.run([sys.executable,'-B','-c',code,str(tmp_path),event],capture_output=True,text=True,timeout=25)
    assert child.returncode==42,child.stderr
    original=s.execution_attempts('execution_gen')[0]['plan']
    r=runtime(tmp_path)
    if event=='after_receipt_before_chapter_settlement':
        assert r.execute_attempt(**args(operation=forbidden))=='draft'
        assert s.report()['charged_amount']=='0.000104'
    else:
        r.reconcile_chapter_evidence()
        if event in {'after_chapter_reservation_before_intent','after_intent_before_dispatch'}:
            assert s.report()['charged_amount']=='0'
        else:
            assert s.report()['charged_amount']=='0.4004'
            with pytest.raises(ProviderCallUncertainError):r.execute_attempt(**args(operation=forbidden))
    assert s.execution_attempts('execution_gen')[0]['plan']==original
