"""Fingerprint the effective SDK endpoint without retaining its URL or secrets."""
from urllib.parse import urlsplit

from core.chapter_budget_store import ChapterBudgetError
from core.memory_v2.canonical import canonical_json_hash


def endpoint_fingerprint(provider,base_url):
    if provider not in {'openai','anthropic'}:
        raise ChapterBudgetError('unsupported Provider endpoint identity')
    try:
        parsed=urlsplit(str(base_url))
        if (parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.fragment
                or parsed.username is not None or parsed.password is not None):
            raise ValueError()
        host=parsed.hostname.lower().encode('idna').decode('ascii')
        port=parsed.port or (443 if parsed.scheme=='https' else 80)
        if any(ord(char)<32 for char in str(base_url)):raise ValueError()
    except (ValueError,UnicodeError) as exc:
        raise ChapterBudgetError('effective Provider URL cannot be bound to a chapter budget') from exc
    return canonical_json_hash(dict(provider=provider,scheme=parsed.scheme,host=host,port=port,
        path=parsed.path.rstrip('/'),query=parsed.query),exclude_environment_fields=False)
