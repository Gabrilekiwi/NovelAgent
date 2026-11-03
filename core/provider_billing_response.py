"""Versioned adapter observations inside an immutable physical Receipt.

Raw usage remains unchanged. These observations record actual SDK metadata;
they never fill in a missing model/tier from request settings or prices.
"""
import copy
import unicodedata


BILLING_RESPONSE_KEY = 'novelagent_billing_response'


class OpenAIBillingObservations:
    def __init__(self):
        self.models = []
        self.tiers = []
        self.invalid = False

    def observe(self, *, model, service_tier):
        for value, target in ((model, self.models), (service_tier, self.tiers)):
            if value is None:
                continue
            if (not isinstance(value, str) or not value or len(value) > 200
                    or value != value.strip()
                    or any(unicodedata.category(c).startswith('C') for c in value)):
                self.invalid = True
            elif value not in target:
                if len(target) >= 8:
                    self.invalid = True
                else:
                    target.append(value)

    def attach(self, usage):
        return {**copy.deepcopy(dict(usage)), BILLING_RESPONSE_KEY: {
            'schema_version': 'openai_billing_observations_v1',
            'source': 'sdk_response_metadata', 'models': list(self.models),
            'service_tiers': list(self.tiers), 'invalid_observation': self.invalid}}


def reviewed_openai_billing_quantities(plan, receipt, projection):
    """Return known legacy-model quantities or a reason to retain occupancy."""
    expected = {'schema_version': 'openai_billing_observations_v1',
        'source': 'sdk_response_metadata', 'models': [plan['model']],
        'service_tiers': [plan['service_tier']], 'invalid_observation': False}
    observed = receipt['usage'].get(BILLING_RESPONSE_KEY)
    if (not isinstance(observed, dict) or observed != expected
            or type(observed.get('invalid_observation')) is not bool):
        return None, 'response_billing_identity_unknown_or_changed'
    tokens = projection['usage']
    if any(tokens[k]['source'] != 'provider_reported' or tokens[k]['value'] is None
            for k in ('input_tokens', 'output_tokens', 'cache_read_tokens')):
        return None, 'billing_quantities_unknown'
    # Before GPT-5.6 there is no separate cache-write surcharge. Absence of
    # that optional counter is supported by the reviewed model policy, not
    # fabricated Provider-reported zero usage. A nonzero counter contradicts
    # this capability and cannot be settled under its tariff.
    raw = receipt['usage']
    for container, key in ((raw, 'cache_creation_input_tokens'),
            (raw.get('prompt_tokens_details', {}), 'cache_write_tokens'),
            (raw.get('input_tokens_details', {}), 'cache_write_tokens')):
        if not isinstance(container, dict):
            return None, 'billing_quantities_unknown'
        if key in container and (type(container[key]) is not int or container[key] != 0):
            return None, 'billing_cache_policy_differs'
    for detail in ('prompt_tokens_details', 'completion_tokens_details', 'input_tokens_details', 'output_tokens_details'):
        container = raw.get(detail, {})
        if not isinstance(container, dict):
            return None, 'billing_quantities_unknown'
        if 'audio_tokens' in container and (type(container['audio_tokens']) is not int or container['audio_tokens'] != 0):
            return None, 'billing_feature_differs'
    quantities = dict(input_tokens=tokens['input_tokens']['value'],
        output_tokens=tokens['output_tokens']['value'], cache_read_tokens=tokens['cache_read_tokens']['value'],
        cache_write_5m_tokens=0, cache_write_1h_tokens=0)
    if quantities['cache_read_tokens'] > quantities['input_tokens']:
        return None, 'billing_quantities_unknown'
    return quantities, None
