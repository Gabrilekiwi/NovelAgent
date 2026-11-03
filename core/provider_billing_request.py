"""Source-bound billing request plans, not dispatch or spend permission.

The first reviewed capability is an optional exact OpenAI snapshot. It does
not replace any configured model. The adapter also requires a frozen tariff,
durable reservations and actual response billing observations.
"""
import copy
from math import isfinite
import re

from core.memory_v2.canonical import canonical_json_hash
from core.model_calls import canonical_model_request_digest


class BillingRequestError(ValueError):
    code='provider_billing_request_invalid'
    failure_category='local_budget'
    retryable=False


# Reviewed against the fetched official model/API/context-window documents.
# Prices are intentionally absent: the author must freeze an applicable tariff.
_MODEL='gpt-5.4-mini-2026-03-17'
_CAPABILITY={
    'schema_version':'official_text_billing_capability_v2','provider':'openai','model':_MODEL,
    'context_window':400000,'max_output_tokens':128000,'reviewed_on':'2026-11-03',
    'cache_policy':'legacy_automatic_no_write_surcharge',
    'sources':[
        'https://developers.openai.com/api/docs/models/gpt-5.4-mini',
        'https://developers.openai.com/api/docs/guides/conversation-state',
        'https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create',
        'https://developers.openai.com/api/docs/guides/prompt-caching'],
}
_REQUIRED={'model','messages','max_completion_tokens','service_tier','prompt_cache_retention','stream','n'}
_OPTIONAL={'temperature','stream_options','response_format'}


def _hash(value):return canonical_json_hash(value,exclude_environment_fields=False)


def openai_text_billing_policy():
    """Exact tariff policy for this reviewed capability; contains no rates."""
    return {'schema_version':'openai_text_tariff_policy_v1',
        'capability_sha256':_hash(_CAPABILITY),'service_tier':'default',
        'cache_policy':_CAPABILITY['cache_policy']}


def _stored_plan(request_digest, outputs, fingerprint):
    result={'schema_version':'provider_billing_request_plan_v2','provider':'openai','model':_MODEL,
        'endpoint_fingerprint':fingerprint,'request_digest':request_digest,
        'capability':copy.deepcopy(_CAPABILITY),'capability_sha256':_hash(_CAPABILITY),
        'max_billable_input_tokens':_CAPABILITY['context_window'],
        'input_bound_basis':'documented_model_context_window','max_billable_output_tokens':outputs,
        'service_tier':'default','cache_policy':_CAPABILITY['cache_policy'],
        'required_price_dimensions':['uncached_input','cached_input','output'],
        'response_billing_verification_required':True,'dispatch_authorized':False}
    return {**result,'plan_sha256':_hash(result)}


def validate_stored_openai_billing_plan(plan):
    """Verify a persisted capability snapshot without storing private prompts.

    This validates original evidence, not a fresh request. Dispatch must also
    rederive the plan from the final request with validate_openai_billing_plan.
    The incorrect, never-dispatched v1 plan is deliberately unsupported.
    """
    from api.budget_endpoint import endpoint_fingerprint
    if (not isinstance(plan,dict) or not isinstance(plan.get('request_digest'),str)
            or not re.fullmatch('[0-9a-f]{64}',plan['request_digest'])
            or type(plan.get('max_billable_output_tokens')) is not int
            or not 1<=plan['max_billable_output_tokens']<=_CAPABILITY['max_output_tokens']):
        raise BillingRequestError('持久金额请求计划不完整。')
    expected=_stored_plan(plan['request_digest'],plan['max_billable_output_tokens'],
        endpoint_fingerprint('openai','https://api.openai.com/v1'))
    if plan!=expected:raise BillingRequestError('持久金额请求计划与已核对能力不同。')
    return expected


def plan_openai_text_billing(request,*,base_url):
    """Bind the whole final logical request, explicit tier/cache and capability.

    The full documented context window is a conservative capability bound,
    never a prompt token estimate. This plan alone does not establish a price,
    settle a receipt, authorize dispatch or prove account/model availability.
    """
    from api.budget_endpoint import endpoint_fingerprint
    from core.chapter_budget_store import ChapterBudgetError
    official=endpoint_fingerprint('openai','https://api.openai.com/v1')
    try:actual=endpoint_fingerprint('openai',base_url)
    except ChapterBudgetError as exc:raise BillingRequestError('实际端点无法用于已核对的金额契约。') from exc
    if actual!=official:raise BillingRequestError('金额请求尚未支持此实际端点，不能套用官方契约。')
    if (not isinstance(request,dict) or not _REQUIRED<=request.keys()
            or request.keys()-_REQUIRED-_OPTIONAL or request['model']!=_MODEL):
        raise BillingRequestError('金额请求必须使用已核对的精确模型快照及完整请求字段。')
    if (type(request['max_completion_tokens']) is not int
            or not 1<=request['max_completion_tokens']<=_CAPABILITY['max_output_tokens']
            or type(request['n']) is not int or request['n']!=1 or type(request['stream']) is not bool
            or request['service_tier']!='default'
            or request['prompt_cache_retention']!='in_memory'):
        raise BillingRequestError('输出计费上限、单份输出、服务档位或缓存规则不符合已核对契约。')
    messages=request['messages']
    if (not isinstance(messages,list) or not 1<=len(messages)<=1000 or any(
            not isinstance(message,dict) or set(message)!={'role','content'}
            or not isinstance(message['role'],str) or message['role'] not in {'system','developer','user','assistant'}
            or not isinstance(message['content'],str) for message in messages)):
        raise BillingRequestError('金额请求仅支持明确的纯文本消息，不接受额外多模态或工具费用。')
    if 'temperature' in request and (type(request['temperature']) not in (int,float)
            or not 0<=request['temperature']<=2 or not isfinite(request['temperature'])):
        raise BillingRequestError('temperature不是有效的有限数值。')
    if 'stream_options' in request and (not isinstance(request['stream_options'],dict)
            or set(request['stream_options'])!={'include_usage'} or request['stream_options']['include_usage'] is not True):
        raise BillingRequestError('流式金额请求必须保留实际用量。')
    if request['stream'] and request.get('stream_options')!={'include_usage':True}:
        raise BillingRequestError('流式金额请求缺少实际用量选项。')
    if 'response_format' in request and request['response_format'] not in ({'type':'text'},{'type':'json_object'}):
        raise BillingRequestError('尚未核对该输出结构的金额契约。')
    return _stored_plan(canonical_model_request_digest(request),request['max_completion_tokens'],actual)


def validate_openai_billing_plan(plan,request,*,base_url):
    """Re-derive, so a rehashed caller plan cannot adopt a changed request."""
    expected=plan_openai_text_billing(request,base_url=base_url)
    if plan!=expected:raise BillingRequestError('金额请求计划与实际请求或受信契约不同。')
    return expected
