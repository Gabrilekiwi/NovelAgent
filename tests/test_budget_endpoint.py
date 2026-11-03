import pytest

from api.budget_endpoint import endpoint_fingerprint
from core.chapter_budget_store import ChapterBudgetError


def test_equivalent_endpoint_identity_and_effective_route_changes():
    official=endpoint_fingerprint('openai','https://api.openai.com/v1')
    assert official==endpoint_fingerprint('openai','https://API.OPENAI.COM:443/v1/')
    for provider,url in [('anthropic','https://api.openai.com/v1'),('openai','http://api.openai.com/v1'),
        ('openai','https://api.openai.com/v2'),('openai','https://proxy.example/v1'),
        ('openai','https://api.openai.com/v1?route=different')]:
        assert official!=endpoint_fingerprint(provider,url)


@pytest.mark.parametrize('value',['https://user:secret@example.com/v1','https://example.com/#x',
    'file:///tmp/endpoint','https://example.com:bad/v1','https://example.com/\npath'])
def test_invalid_endpoint_identity_never_echoes_credentials(value):
    with pytest.raises(ChapterBudgetError) as caught:endpoint_fingerprint('openai',value)
    assert value not in str(caught.value) and 'secret' not in str(caught.value)
