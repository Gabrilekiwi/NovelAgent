"""Read-only usage and cost projection over immutable physical-attempt evidence.

This module never adjusts a budget, writes a receipt, or starts a model call.
Unattributed historical zeroes and missing fields stay explicitly unknown.
"""
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext
from typing import Mapping

from api.contracts import MODEL_USAGE_SOURCE_KEY
from core.model_calls import ModelCallIntegrityError, validate_model_call_intent, validate_model_call_receipt


USAGE_SOURCE_KEY = MODEL_USAGE_SOURCE_KEY
COST_EVIDENCE_KEY = 'novelagent_cost_evidence'
TOKEN_FIELDS = {
    'input_tokens': ('input_tokens', 'prompt_tokens'),
    'output_tokens': ('output_tokens', 'completion_tokens'),
    'total_tokens': ('total_tokens',),
    'cache_read_tokens': ('cache_read_input_tokens', 'prompt_tokens_details.cached_tokens', 'input_tokens_details.cached_tokens'),
    'cache_creation_tokens': ('cache_creation_input_tokens', 'prompt_tokens_details.cache_write_tokens'),
    'reasoning_tokens': ('output_tokens_details.reasoning_tokens', 'completion_tokens_details.reasoning_tokens'),
}


def _field(value, path):
    for name in path.split('.'):
        if not isinstance(value, Mapping) or name not in value:
            return None
        value = value[name]
    return value


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**15 else None


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite() or result < 0 or result > Decimal('1e15') or result.as_tuple().exponent < -18:
        return None
    return result


def _amount(value):
    raw = format(value, 'f')
    return raw.rstrip('0').rstrip('.') if '.' in raw else raw


def _token_usage(receipt):
    usage = receipt['usage'] if receipt else {}
    # These failures contain local synthetic usage, not a Provider usage report.
    synthetic = receipt and receipt['status'] in {'budget_rejected', 'request_not_sent', 'provider_rejected'}
    declared = usage.get(USAGE_SOURCE_KEY)
    if declared not in {'provider_reported','program_estimate'}:
        declared = None
    result = {}
    for name, aliases in TOKEN_FIELDS.items():
        present = [(key, _field(usage, key)) for key in aliases if _field(usage, key) is not None]
        valid = [(key, _count(value)) for key, value in present]
        reason = None
        if synthetic:
            reason = 'synthetic_failure_usage'
        elif not valid:
            reason = 'missing_usage'
        elif any(value is None for _, value in valid):
            reason = 'invalid_usage'
        elif len({value for _, value in valid}) != 1:
            reason = 'conflicting_usage_fields'
        elif valid[0][1] == 0 and not declared:
            reason = 'unattributed_zero'
        result[name] = {'value': None if reason else valid[0][1],
            'source': 'unknown' if reason else declared or 'legacy_receipt',
            'fields': [key for key, _ in present], 'reason': reason}
    # Cache/reasoning fields are detail counters; never sum them into totals.
    return result


def _cost(receipt):
    unknown = {'classification':'unknown','amount':None,'currency':None,'source':None,'source_id':None,
               'reason':'no_attributed_cost'}
    if receipt is None:
        return {**unknown,'reason':'no_receipt'}
    evidence = receipt['usage'].get(COST_EVIDENCE_KEY)
    if evidence is None:
        return unknown
    if not isinstance(evidence, Mapping):
        return {**unknown,'reason':'invalid_cost_evidence'}
    classification = evidence.get('classification')
    source = evidence.get('source')
    source_id = evidence.get('source_id')
    amount = _decimal(evidence.get('amount'))
    currency = evidence.get('currency')
    allowed_source = {'actual':'provider_billing','estimated':'pricing_snapshot'}
    if (evidence.get('schema_version') != 'model_cost_evidence_v1'
            or classification not in allowed_source or source != allowed_source[classification]
            or not isinstance(source_id,str) or not source_id.strip() or len(source_id)>200
            or amount is None or not isinstance(currency,str) or len(currency)!=3
            or not currency.isascii() or not currency.isalpha() or not currency.isupper()):
        return {**unknown,'reason':'invalid_cost_evidence'}
    if classification == 'estimated':
        calculation = evidence.get('calculation')
        if (not isinstance(calculation, Mapping) or not receipt['actual_model']
                or calculation.get('model') != receipt['actual_model']
                or calculation.get('endpoint_type') != receipt['endpoint_type']
                or not isinstance(calculation.get('terms'), list) or not calculation['terms']):
            return {**unknown,'reason':'invalid_cost_evidence'}
        total, seen = Decimal(0), set()
        tokens = _token_usage(receipt)
        with localcontext() as context:
            context.prec = 64
            for term in calculation['terms']:
                if not isinstance(term, Mapping):
                    return {**unknown,'reason':'invalid_cost_evidence'}
                field = term.get('usage_field')
                rate = _decimal(term.get('unit_price_per_million'))
                quantity = _count(term.get('quantity'))
                if (field not in tokens or field in seen or rate is None or quantity is None
                        or tokens[field]['value'] != quantity):
                    return {**unknown,'reason':'invalid_cost_evidence'}
                seen.add(field)
                total += Decimal(quantity)*rate/Decimal(1000000)
        # A partial input-only estimate may not masquerade as the attempt cost.
        # Cache rates are intentionally deferred until their distinct billing
        # semantics are represented; raw detail counts overlap input totals.
        if seen != {'input_tokens','output_tokens'} or total != amount:
            return {**unknown,'reason':'invalid_cost_evidence'}
        if any((tokens[name]['value'] or 0)>0 for name in ('cache_read_tokens','cache_creation_tokens')):
            return {**unknown,'reason':'invalid_cost_evidence'}
    return {'classification':classification,'amount':_amount(amount),'currency':currency,
            'source':source,'source_id':source_id,'reason':None,
            **({'calculation':calculation} if classification=='estimated' else {})}


def project_model_attempt(execution_id, intent, receipt=None, *, active=False):
    """Project one verified attempt; cost metadata must name its attribution.

    Cost evidence, when supplied by a trusted adapter, lives within the existing
    extensible usage object and therefore within the immutable receipt hash.
    A bare cost/estimated_cost field is intentionally insufficient.
    """
    intent = validate_model_call_intent(intent)
    if receipt is not None:
        receipt = validate_model_call_receipt(receipt)
        if any(receipt[k] != intent[k] for k in ('call_id','attempt_id','intent_hash')):
            raise ModelCallIntegrityError('usage receipt does not belong to this attempt')
    status = receipt['status'] if receipt else 'running' if active is True else 'provider_uncertain' if active is False else 'awaiting_receipt'
    reserved = intent['budget_reservation']
    return {
        'execution_id':execution_id,'call_id':intent['call_id'],'attempt_id':intent['attempt_id'],
        'intent_hash':intent['intent_hash'],'receipt_hash':receipt['receipt_hash'] if receipt else None,
        'stage':intent['stage'],'provider':intent['provider'],'requested_model':intent['model'],
        'actual_model':receipt['actual_model'] if receipt else None,
        'endpoint_type':receipt['endpoint_type'] if receipt else 'unknown','status':status,
        'created_at':intent['created_at'],'received_at':receipt['received_at'] if receipt else None,
        'usage':_token_usage(receipt),
        'reservation':{name:_count(reserved.get(name)) for name in ('reserved_input_tokens','reserved_output_tokens','reserved_total_tokens')},
        'cost':_cost(receipt),
        'provider_attempted':False if status in {'budget_rejected','request_not_sent'} else None if not receipt else True,
    }


def summarize_model_attempts(items):
    """De-duplicate exact evidence, keeping currencies and unknowns separate."""
    unique = {}
    for item in items:
        key = item['execution_id'], item['attempt_id']
        if key in unique and unique[key] != item:
            raise ModelCallIntegrityError('conflicting projections for one physical attempt')
        unique[key] = item
    rows = list(unique.values())
    costs = {}
    for item in rows:
        cost = item['cost']
        if cost['classification'] == 'unknown':
            continue
        group = costs.setdefault(cost['currency'], {'actual':Decimal(0),'estimated':Decimal(0),'actual_count':0,'estimated_count':0})
        with localcontext() as context:
            context.prec = 64
            group[cost['classification']] += Decimal(cost['amount'])
        group[cost['classification'] + '_count'] += 1
    return {'attempt_count':len(rows),'status_counts':dict(Counter(item['status'] for item in rows)),
        'unknown_cost_count':sum(item['cost']['classification']=='unknown' for item in rows),
        'costs_by_currency':{currency:{key:_amount(value) if isinstance(value,Decimal) else value for key,value in group.items()}
                             for currency,group in sorted(costs.items())},
        'tokens':{name:{'known_subtotal':sum(item['usage'][name]['value'] or 0 for item in rows),
                        'source_subtotals':{source:sum(item['usage'][name]['value'] or 0 for item in rows if item['usage'][name]['source']==source)
                            for source in {'provider_reported','program_estimate','legacy_receipt'}},
                        'unknown_count':sum(item['usage'][name]['value'] is None for item in rows)} for name in TOKEN_FIELDS}}
