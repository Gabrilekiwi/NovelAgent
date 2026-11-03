import copy

import pytest

from core.memory_v2.canonical import canonical_json_hash
from core.model_calls import canonical_model_request_digest
from core.provider_billing_request import BillingRequestError,plan_openai_text_billing,validate_openai_billing_plan


URL='https://api.openai.com/v1'


def request():return {'model':'gpt-5.4-mini-2026-03-17','messages':[{'role':'user','content':'中文正文'}],
    'max_completion_tokens':1024,'service_tier':'default','prompt_cache_retention':'in_memory',
    'stream':False,'n':1}


def test_final_request_binding_is_not_a_prompt_estimate_or_spending_authority():
    body=request();plan=plan_openai_text_billing(body,base_url=URL)
    assert plan['request_digest']==canonical_model_request_digest(body)
    assert plan['max_billable_input_tokens']==400000 and plan['max_billable_output_tokens']==1024
    assert not plan['dispatch_authorized'] and plan['response_billing_verification_required']
    assert plan['cache_policy']=='legacy_automatic_no_write_surcharge'
    assert plan['required_price_dimensions']==['uncached_input','cached_input','output']
    assert validate_openai_billing_plan(plan,body,base_url=URL)==plan
    body['messages'][0]['content']='different draft'
    with pytest.raises(BillingRequestError):validate_openai_billing_plan(plan,body,base_url=URL)
    forged=copy.deepcopy(plan);forged['max_billable_input_tokens']=1
    forged['plan_sha256']=canonical_json_hash({k:v for k,v in forged.items() if k!='plan_sha256'},exclude_environment_fields=False)
    with pytest.raises(BillingRequestError):validate_openai_billing_plan(forged,request(),base_url=URL)


@pytest.mark.parametrize('override',[{'model':'gpt-5.4-mini'},{'model':'gpt-4.1-mini'},{'max_tokens':1024},
    {'max_completion_tokens':True},{'max_completion_tokens':128001},{'n':2},{'n':True},
    {'service_tier':'auto'},{'service_tier':'priority'},{'tools':[]},{'modalities':['text','audio']},
    {'prompt_cache_options':{'mode':'implicit','ttl':'30m'}},
    {'prompt_cache_options':{'mode':'explicit','ttl':'30m'}},
    {'prompt_cache_retention':'24h'},{'temperature':float('nan')},
    {'messages':[{'role':'user','content':[{'type':'text','text':'draft'}]}]},
    {'messages':[{'role':'user','content':'draft','prompt_cache_breakpoint':{'mode':'explicit'}}]},
    {'stream':True},{'stream_options':{'include_usage':False}},{'stream_options':{'include_usage':1}},
    {'temperature':10**1000},{'messages':[{'role':[],'content':'draft'}]}])
def test_unknown_billable_features_cannot_use_a_known_tariff(override):
    with pytest.raises(BillingRequestError):plan_openai_text_billing({**request(),**override},base_url=URL)


@pytest.mark.parametrize('url',['https://compatible.example/v1','https://api.openai.com/v1?region=eu',
    'https://eu.api.openai.com/v1','http://api.openai.com/v1','https://api.openai.com/other',
    'https://user:secret@api.openai.com/v1'])
def test_other_endpoint_or_regional_uplift_cannot_inherit_official_contract(url):
    with pytest.raises(BillingRequestError):plan_openai_text_billing(request(),base_url=url)


def test_stream_usage_and_exact_request_options_remain_bound():
    body={**request(),'stream':True,'stream_options':{'include_usage':True},'response_format':{'type':'json_object'}}
    plan=plan_openai_text_billing(body,base_url=URL)
    body['response_format']={'type':'text'}
    with pytest.raises(BillingRequestError):validate_openai_billing_plan(plan,body,base_url=URL)
