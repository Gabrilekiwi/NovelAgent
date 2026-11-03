import copy
from decimal import Decimal

import pytest

from api.contracts import ModelResponse, reported_model_usage
from core.model_calls import ModelCallEvidenceError, ModelCallIntegrityError, build_model_call_intent, build_model_call_receipt
from core.model_call_usage import project_model_attempt, summarize_model_attempts, USAGE_SOURCE_KEY, COST_EVIDENCE_KEY


def evidence(usage=None, status='succeeded'):
    intent=build_model_call_intent(call_id='same-call',attempt_id='same-call-a1',provider='openai',model='requested',
        stage='chapter_fact_extraction',request={'messages':[]},created_at='2026-09-10T00:00:00+00:00',
        budget_reservation={'reserved_input_tokens':100,'reserved_output_tokens':500,'estimated_cost':0})
    receipt=build_model_call_receipt(intent,response=ModelResponse('sample',usage=usage or {},actual_model='returned',endpoint_type='openai_compatible'),
        status=status,received_at='2026-09-10T00:00:01+00:00')
    return intent,receipt


def cost(kind='actual',amount='0.03',currency='USD',**changes):
    return {'schema_version':'model_cost_evidence_v1','classification':kind,'amount':amount,'currency':currency,
            'source':'provider_billing' if kind=='actual' else 'pricing_snapshot','source_id':'fixture-source-v1',
            **({'calculation':{'model':'returned','endpoint_type':'openai_compatible','terms':[
                {'usage_field':'input_tokens','quantity':10,'unit_price_per_million':str(Decimal(amount)*100000)},
                {'usage_field':'output_tokens','quantity':1,'unit_price_per_million':'0'}]}} if kind=='estimated' else {}),**changes}


def test_missing_usage_reservations_and_historical_zero_cost_are_not_actual_usage():
    intent,receipt=evidence({'input_tokens':0,'output_tokens':0,'cost':0,'estimated_cost':0})
    original=copy.deepcopy((intent,receipt))
    result=project_model_attempt('execution_a',intent,receipt)
    assert result['reservation']['reserved_output_tokens']==500
    assert result['usage']['output_tokens']['value'] is None
    assert result['cost']['classification']=='unknown' and result['cost']['amount'] is None
    assert (intent,receipt)==original


def test_provider_zero_is_explicit_and_cache_reasoning_are_not_added_twice():
    intent,receipt=evidence({USAGE_SOURCE_KEY:'provider_reported','prompt_tokens':10,'completion_tokens':0,'total_tokens':10,
        'prompt_tokens_details':{'cached_tokens':8},'completion_tokens_details':{'reasoning_tokens':0}})
    result=project_model_attempt('execution_a',intent,receipt)
    assert result['usage']['output_tokens']['value']==0
    assert result['usage']['cache_read_tokens']['value']==8
    assert result['usage']['total_tokens']['value']==10
    assert result['actual_model']=='returned' and result['requested_model']=='requested'
    assert result['endpoint_type']=='openai_compatible'


@pytest.mark.parametrize('value',[True,-1,1.5,'7',float('inf')])
def test_invalid_token_values_are_not_coerced(value):
    if value==float('inf'):
        # Infinite values cannot enter a canonical immutable receipt at all.
        with pytest.raises(ModelCallEvidenceError):evidence({'input_tokens':value})
        return
    intent,receipt=evidence({'input_tokens':value})
    assert project_model_attempt('execution_a',intent,receipt)['usage']['input_tokens']['value'] is None


def test_conflicting_aliases_do_not_choose_a_more_convenient_count():
    intent,receipt=evidence({'input_tokens':10,'prompt_tokens':12})
    assert project_model_attempt('execution_a',intent,receipt)['usage']['input_tokens']['reason']=='conflicting_usage_fields'


def test_program_estimates_keep_their_source_separate_from_provider_counts():
    intent,receipt=evidence({USAGE_SOURCE_KEY:'program_estimate','input_tokens':12,'output_tokens':0})
    row=project_model_attempt('execution_a',intent,receipt)
    assert row['usage']['output_tokens']['value']==0
    assert row['usage']['input_tokens']['source']=='program_estimate'
    assert summarize_model_attempts([row])['tokens']['input_tokens']['source_subtotals']=={
        'program_estimate':12,'provider_reported':0,'legacy_receipt':0}


@pytest.mark.parametrize('status',['budget_rejected','request_not_sent','provider_rejected'])
def test_local_failure_zeroes_do_not_become_provider_usage(status):
    intent,receipt=evidence({USAGE_SOURCE_KEY:'provider_reported','input_tokens':0,'output_tokens':0},status=status)
    row=project_model_attempt('execution_a',intent,receipt)
    assert row['usage']['input_tokens']['value'] is None
    assert row['cost']['classification']=='unknown'
    assert row['status']==status


def test_partial_failure_and_uncertain_attempts_are_not_lost():
    intent,receipt=evidence({'input_tokens':10,'output_tokens':4},status='partial_response_failed')
    failed=project_model_attempt('execution_a',intent,receipt)
    uncertain=project_model_attempt('execution_b',intent)
    active=project_model_attempt('execution_c',intent,active=True)
    assert failed['usage']['output_tokens']['value']==4
    assert uncertain['status']=='provider_uncertain' and uncertain['cost']['reason']=='no_receipt'
    assert active['status']=='running'
    assert summarize_model_attempts([failed,uncertain,active])['attempt_count']==3


def test_cost_evidence_and_currencies_stay_separate_and_replay_is_not_counted_twice():
    rows=[]
    for execution, value in [('a',cost()),('b',cost('estimated','0.1')),('c',cost(currency='CNY')),('d',None)]:
        intent,receipt=evidence({COST_EVIDENCE_KEY:value,'input_tokens':10,'output_tokens':1} if value else {})
        rows.append(project_model_attempt('execution_'+execution,intent,receipt))
    summary=summarize_model_attempts(rows+[rows[0]])
    assert summary['attempt_count']==4 and summary['unknown_cost_count']==1
    assert summary['costs_by_currency']['USD']=={'actual':'0.03','estimated':'0.1','actual_count':1,'estimated_count':1}
    assert summary['costs_by_currency']['CNY']['actual']=='0.03'


def test_raw_compatible_endpoint_cannot_assign_our_billing_attribution():
    result=reported_model_usage({'input_tokens':0,COST_EVIDENCE_KEY:cost(),USAGE_SOURCE_KEY:'program_estimate'})
    assert COST_EVIDENCE_KEY not in result
    assert result[USAGE_SOURCE_KEY]=='provider_reported'


def test_estimate_requires_exact_price_calculation_and_usage_binding():
    value=cost('estimated','0.1')
    intent,receipt=evidence({COST_EVIDENCE_KEY:value,'input_tokens':9,'output_tokens':1})
    assert project_model_attempt('execution_a',intent,receipt)['cost']['classification']=='unknown'


@pytest.mark.parametrize('changes',[{'amount':-1},{'amount':True},{'currency':''},{'currency':'usd'},{'source_id':''},
    {'source':'unknown'},{'classification':'actual','source':'pricing_snapshot'},{'schema_version':'old'}])
def test_unattributed_or_invalid_amounts_are_unknown(changes):
    intent,receipt=evidence({COST_EVIDENCE_KEY:cost(**changes)})
    assert project_model_attempt('execution_a',intent,receipt)['cost']['classification']=='unknown'


def test_receipt_tampering_and_projection_conflicts_fail_closed():
    intent,receipt=evidence({'input_tokens':10})
    altered=copy.deepcopy(receipt);altered['usage']['input_tokens']=1
    with pytest.raises(ModelCallIntegrityError):project_model_attempt('execution_a',intent,altered)
    row=project_model_attempt('execution_a',intent,receipt)
    changed=copy.deepcopy(row);changed['status']='failed'
    with pytest.raises(ModelCallIntegrityError):summarize_model_attempts([row,changed])
