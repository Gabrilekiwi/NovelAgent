import json
from dataclasses import replace

from tests.test_chapter_publication_api import api_case, service_case, ready, case
from workbench import generation_price as prices


def test_missing_or_out_of_scope_tariff_never_returns_money(tmp_path, monkeypatch):
    cfg = replace(prices.get_config(), openai_model='local-test', openai_api_key='test-value',
                  openai_base_url='https://fixture.invalid/v1', openai_max_output_tokens=16000)
    monkeypatch.setattr(prices, 'get_config', lambda: cfg)
    file = tmp_path / 'rates.json'
    monkeypatch.setattr(prices, 'PROFILE', file)
    assert prices.generation_quote(1000)['price'] is None
    profile = dict(version=1, binding=prices._binding(cfg), as_of='2026-01-01', input_tier_limit=1000,
                   input_usd_per_million='2.50', output_usd_per_million='8.25')
    file.write_text(json.dumps(profile), encoding='utf8')
    price = prices.generation_quote(1000)['price']
    assert price['example_usd'] == '0.035500' and price['at_output_limit_usd'] == '0.134500'
    assert prices.generation_quote(1001)['price'] is None
    for change in (dict(openai_model='other'), dict(openai_api_key='other'), dict(openai_base_url='https://other.invalid/v1')):
        monkeypatch.setattr(prices, 'get_config', lambda: replace(cfg, **change))
        assert prices.generation_quote(1000)['price'] is None
    monkeypatch.setattr(prices, 'get_config', lambda: cfg)
    for value in ('NaN', '1e99', '-1', None):
        file.write_text(json.dumps(dict(profile, input_usd_per_million=value)), encoding='utf8')
        assert prices.generation_quote(1000)['price'] is None


def test_confirmed_estimate_does_not_open_candidate_or_reserve_budget(api_case, monkeypatch):
    c = api_case
    from modules.chapter_generator import generator
    monkeypatch.setattr(generator, 'chat_completion', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('provider invoked')))
    client, path, confirmation = c['client'], c['path'], c['confirmation']
    def evidence():
        return (client.get(path+'/budget').json(), client.get(path+'/candidates').json())
    before = evidence()
    result = client.post(path+'/generation-estimate', json={'confirmation':confirmation})
    assert result.status_code == 200, result.text
    quote = result.json()['estimate']
    assert quote['input_tokens_estimate'] > 0 and quote['output_token_limit'] > 0
    assert client.post(path+'/generation-estimate', json={'confirmation':dict(confirmation, chapter_index=999)}).status_code in (400,409,422)
    assert evidence() == before
