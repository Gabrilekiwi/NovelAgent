"""Pure frozen chapter-budget and pricing contracts; no spending authority.

The caller must derive the book/chapter and endpoint identity on the server.
Cost reservation is conservative only when the caller supplies a valid upper
bound on *all* billable input tokens and the actual tariff is represented here.
Persistence, locks, elapsed-time accounting and Provider dispatch live elsewhere.
"""
from __future__ import annotations

import copy
import re
import unicodedata
from decimal import Decimal, localcontext
from typing import Any, Mapping

from core.memory_v2.canonical import canonical_json_hash


CHAPTER_BUDGET_SCHEMA_VERSION = "chapter_budget_v1"
MAX_COUNT = 10**15
MAX_DECIMAL_VALUE = Decimal("1000000000000000")
_DECIMAL = re.compile(r"[0-9]{1,16}(?:\.[0-9]{1,18})?\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}\Z", re.ASCII)
_IDENTITY_FIELDS = ("provider", "model", "endpoint_type", "endpoint_fingerprint")
_CACHE_RATES = {
    "cache_read_tokens": "cache_read_price_per_million",
    "cache_write_5m_tokens": "cache_write_5m_price_per_million",
    "cache_write_1h_tokens": "cache_write_1h_price_per_million",
}
_PRICE_FIELDS = {
    *_IDENTITY_FIELDS, "currency", "input_price_per_million",
    "output_price_per_million", "input_token_semantics",
}
_BUDGET_FIELDS = {
    "schema_version", "book_id", "chapter", "currency", "amount_limit",
    "max_provider_calls", "max_input_tokens", "max_output_tokens",
    "max_active_seconds", "pricing", "budget_sha256",
}


class ChapterBudgetContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _fail(message: str, code: str = "chapter_budget_invalid") -> None:
    raise ChapterBudgetContractError(code, message)


def _text(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 200
            or value != value.strip() or any(unicodedata.category(c).startswith("C") for c in value)):
        _fail(f"{label} must be bounded nonempty text without surrounding whitespace or controls")
    return value


def _fingerprint(value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail("endpoint_fingerprint must be a lowercase SHA-256 digest")
    return value


def _currency(value: Any) -> str:
    if not isinstance(value, str) or _CURRENCY.fullmatch(value) is None:
        _fail("currency must be a three-letter uppercase ASCII currency code")
    return value


def _count(value: Any, label: str, *, positive: bool = False) -> int:
    if type(value) is not int or not (1 if positive else 0) <= value <= MAX_COUNT:
        _fail(f"{label} must be {'positive' if positive else 'nonnegative'} integer <= {MAX_COUNT}")
    return value


def _amount(value: Decimal) -> str:
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def normalize_budget_decimal(value: Any, *, label: str = "amount", positive: bool = False) -> str:
    """Normalize a bounded plain decimal string without binary float coercion.

    Inputs have at most 16 integral/18 fractional digits and value <= 1e15.
    Exponents, signs, booleans, numeric Python objects and nonfinite values are
    rejected. Calculated cost outputs may exceed these configuration limits.
    """
    if not isinstance(value, str) or len(value) > 35 or _DECIMAL.fullmatch(value) is None:
        _fail(f"{label} must be a bounded unsigned plain decimal string")
    number = Decimal(value)
    if number > MAX_DECIMAL_VALUE or positive and number <= 0:
        _fail(f"{label} is outside the supported decimal bounds")
    return _amount(number)


def normalize_pricing_row(value: Any) -> dict[str, Any]:
    """Canonicalize one explicit tariff; absent cache rates mean unsupported."""
    if (not isinstance(value, Mapping) or not _PRICE_FIELDS.issubset(value)
            or set(value) - _PRICE_FIELDS - set(_CACHE_RATES.values()) - {'billing_policy'}):
        _fail("pricing row fields differ from the supported tariff contract")
    result = {name: _text(value[name], name) for name in _IDENTITY_FIELDS[:-1]}
    result["endpoint_fingerprint"] = _fingerprint(value["endpoint_fingerprint"])
    result["currency"] = _currency(value["currency"])
    semantics = value["input_token_semantics"]
    if semantics not in ("input_includes_cache", "input_excludes_cache"):
        _fail("input_token_semantics must explicitly include or exclude cache quantities")
    result["input_token_semantics"] = semantics
    for name in ("input_price_per_million", "output_price_per_million", *_CACHE_RATES.values()):
        if name in value:
            result[name] = normalize_budget_decimal(value[name], label=name)
    if 'billing_policy' in value:
        from api.budget_endpoint import endpoint_fingerprint
        from core.provider_billing_request import openai_text_billing_policy
        policy = openai_text_billing_policy()
        if (value['billing_policy'] != policy or result['provider'] != 'openai'
                or result['model'] != 'gpt-5.4-mini-2026-03-17'
                or result['endpoint_type'] != 'official'
                or result['endpoint_fingerprint'] != endpoint_fingerprint('openai', 'https://api.openai.com/v1')
                or semantics != 'input_includes_cache'
                or 'cache_read_price_per_million' not in result
                or any(name in result for name in ('cache_write_5m_price_per_million', 'cache_write_1h_price_per_million'))):
            _fail('reviewed billing policy requires its exact model, endpoint, tier and cache semantics')
        result['billing_policy'] = copy.deepcopy(policy)
    return result


def freeze_chapter_budget(
    *, book_id: str, chapter: int, max_provider_calls: int, max_input_tokens: int,
    max_output_tokens: int, max_active_seconds: str, amount_limit: str | None = None,
    currency: str | None = None, pricing: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Freeze server-selected chapter identity, limits and tariffs canonically."""
    rows = [] if pricing is None else pricing
    if not isinstance(rows, list) or len(rows) > 100:
        _fail("pricing must be a list of at most 100 tariffs")
    normalized = [normalize_pricing_row(row) for row in rows]
    identities = [tuple(row[name] for name in _IDENTITY_FIELDS) for row in normalized]
    if len(set(identities)) != len(identities):
        _fail("pricing contains duplicate provider/model/endpoint identities")
    selected_currency = _currency(currency) if currency is not None else None
    if any(row["currency"] != selected_currency for row in normalized):
        _fail("all pricing rows must use the budget's single explicit currency")
    if amount_limit is not None and (selected_currency is None or not normalized):
        _fail("amount_limit requires an explicit currency and at least one tariff", "chapter_budget_price_required")
    result = {
        "schema_version": CHAPTER_BUDGET_SCHEMA_VERSION,
        "book_id": _text(book_id, "book_id"),
        "chapter": _count(chapter, "chapter", positive=True),
        "currency": selected_currency,
        "amount_limit": (normalize_budget_decimal(amount_limit, label="amount_limit")
                         if amount_limit is not None else None),
        "max_provider_calls": _count(max_provider_calls, "max_provider_calls", positive=True),
        "max_input_tokens": _count(max_input_tokens, "max_input_tokens", positive=True),
        "max_output_tokens": _count(max_output_tokens, "max_output_tokens", positive=True),
        "max_active_seconds": normalize_budget_decimal(max_active_seconds, label="max_active_seconds", positive=True),
        "pricing": sorted(normalized, key=lambda row: tuple(row[name] for name in _IDENTITY_FIELDS)),
    }
    result["budget_sha256"] = canonical_json_hash(result, exclude_environment_fields=False)
    return result


def validate_chapter_budget(
    value: Any, *, expected_book_id: str | None = None, expected_chapter: int | None = None,
) -> dict[str, Any]:
    """Verify exact shape, canonical values and hash; optionally check ownership."""
    if (not isinstance(value, Mapping) or set(value) != _BUDGET_FIELDS
            or value["schema_version"] != CHAPTER_BUDGET_SCHEMA_VERSION):
        _fail("unsupported frozen chapter budget fields or schema")
    expected = freeze_chapter_budget(**{k: value[k] for k in _BUDGET_FIELDS - {"schema_version", "budget_sha256"}})
    if expected != value:
        _fail("frozen chapter budget is noncanonical or its hash differs", "chapter_budget_integrity_error")
    if ((expected_book_id is not None and expected["book_id"] != _text(expected_book_id, "expected_book_id"))
            or (expected_chapter is not None and expected["chapter"] != _count(expected_chapter, "expected_chapter", positive=True))):
        _fail("budget belongs to another book or chapter", "chapter_budget_binding_mismatch")
    return copy.deepcopy(expected)


def select_pricing(
    budget: Mapping[str, Any], *, provider: str, model: str,
    endpoint_type: str, endpoint_fingerprint: str,
) -> dict[str, Any]:
    """Require an exact frozen identity; never fall back to another endpoint."""
    checked = validate_chapter_budget(budget)
    identity = (_text(provider, "provider"), _text(model, "model"),
                _text(endpoint_type, "endpoint_type"), _fingerprint(endpoint_fingerprint))
    for row in checked["pricing"]:
        if tuple(row[name] for name in _IDENTITY_FIELDS) == identity:
            return copy.deepcopy(row)
    _fail("no frozen tariff matches this exact Provider identity", "chapter_budget_price_missing")


def _term(field: str, quantity: int, rate: str) -> dict[str, Any]:
    # Precision covers the bounded count/rate products and addition exactly,
    # independently of a caller's ambient Decimal context.
    with localcontext() as context:
        context.prec = 80
        amount = Decimal(quantity) * Decimal(rate) / Decimal(1_000_000)
    return {"usage_field": field, "quantity": quantity,
            "unit_price_per_million": rate, "amount": _amount(amount)}


def _calculation(price: dict[str, Any], terms: list[dict[str, Any]], *, billable_input_tokens: int) -> dict[str, Any]:
    with localcontext() as context:
        context.prec = 80
        total = sum((Decimal(term["amount"]) for term in terms), Decimal(0))
    return {"currency": price["currency"], "amount": _amount(total),
            "billable_input_tokens": billable_input_tokens, "terms": terms}


def reserve_attempt_cost(
    price: Mapping[str, Any], *, billable_input_upper_bound: int, max_output_tokens: int,
) -> dict[str, Any]:
    """Cost an externally justified all-input bound at the dearest input rate.

    This calculation neither proves that the input bound is valid nor grants
    permission to dispatch. Unknown cache tariffs must be rejected upstream;
    settlement below also rejects nonzero quantities without explicit prices.
    """
    checked = normalize_pricing_row(price)
    inputs = _count(billable_input_upper_bound, "billable_input_upper_bound")
    outputs = _count(max_output_tokens, "max_output_tokens")
    rate = max((checked[name] for name in ("input_price_per_million", *_CACHE_RATES.values()) if name in checked), key=Decimal)
    return _calculation(checked, [_term("billable_input_upper_bound", inputs, rate),
                                 _term("max_output_tokens", outputs, checked["output_price_per_million"])],
                        billable_input_tokens=inputs)


def estimate_attempt_cost(
    price: Mapping[str, Any], *, input_tokens: int, output_tokens: int,
    cache_read_tokens: int, cache_write_5m_tokens: int, cache_write_1h_tokens: int,
) -> dict[str, Any]:
    """Calculate disjoint known billing quantities; never infer absent cache data.

    All five quantities are mandatory, including explicit zeroes. A provider's
    aggregate cache-write count must be resolved to the supported TTL buckets
    before this call; it cannot be supplied as an extra/unknown usage field.
    For includes-cache input, read/write buckets are mutually exclusive subsets.
    For excludes-cache input, they are additional billable input quantities.
    """
    checked = normalize_pricing_row(price)
    inputs = _count(input_tokens, "input_tokens")
    outputs = _count(output_tokens, "output_tokens")
    caches = {"cache_read_tokens": _count(cache_read_tokens, "cache_read_tokens"),
              "cache_write_5m_tokens": _count(cache_write_5m_tokens, "cache_write_5m_tokens"),
              "cache_write_1h_tokens": _count(cache_write_1h_tokens, "cache_write_1h_tokens")}
    cached = sum(caches.values())
    if checked["input_token_semantics"] == "input_includes_cache":
        if cached > inputs:
            _fail("cache subsets exceed the inclusive input token count", "chapter_budget_usage_invalid")
        uncached, billable = inputs - cached, inputs
    else:
        uncached, billable = inputs, inputs + cached
    _count(billable, "total billable input tokens")
    terms = [_term("uncached_input_tokens", uncached, checked["input_price_per_million"]),
             _term("output_tokens", outputs, checked["output_price_per_million"])]
    for field, quantity in caches.items():
        rate_name = _CACHE_RATES[field]
        if quantity and rate_name not in checked:
            _fail(f"nonzero {field} has no explicit frozen tariff", "chapter_budget_cache_price_missing")
        if rate_name in checked:
            terms.append(_term(field, quantity, checked[rate_name]))
    return _calculation(checked, terms, billable_input_tokens=billable)


__all__ = [
    "CHAPTER_BUDGET_SCHEMA_VERSION", "ChapterBudgetContractError", "freeze_chapter_budget",
    "validate_chapter_budget", "normalize_budget_decimal", "normalize_pricing_row",
    "select_pricing", "reserve_attempt_cost", "estimate_attempt_cost",
]
