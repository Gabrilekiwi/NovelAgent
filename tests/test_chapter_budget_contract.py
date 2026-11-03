"""Synthetic tariffs only: no Provider calls, real prices or persistence."""
import copy
from decimal import Decimal, localcontext

import pytest

from core.chapter_budget_contract import (
    ChapterBudgetContractError, estimate_attempt_cost, freeze_chapter_budget,
    normalize_budget_decimal, reserve_attempt_cost, select_pricing, validate_chapter_budget,
)
from core.memory_v2.canonical import canonical_json_hash


def price(**changes):
    return {"provider": "synthetic", "model": "fixture-model", "endpoint_type": "fixture_compatible",
            "endpoint_fingerprint": "a" * 64, "currency": "USD",
            "input_price_per_million": "1.25", "output_price_per_million": "3.5",
            "input_token_semantics": "input_includes_cache", **changes}


def budget(**changes):
    return freeze_chapter_budget(book_id="fixture-book", chapter=2, **{
        "max_provider_calls": 10, "max_input_tokens": 10000, "max_output_tokens": 3000,
        "max_active_seconds": "900", "amount_limit": "0.1", "currency": "USD",
        "pricing": [price()], **changes})


def usage(**changes):
    return {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 0,
            "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0, **changes}


def test_freeze_is_canonical_immutable_by_copy_and_bound_to_server_chapter():
    rows = [price(model="z"), price(model="a", input_price_per_million="01.2500")]
    frozen = budget(pricing=rows, amount_limit="00.1000", max_active_seconds="0900.00")
    equivalent = budget(pricing=list(reversed([price(model="z"), price(model="a")])))
    assert frozen == equivalent
    assert frozen["amount_limit"] == "0.1" and frozen["max_active_seconds"] == "900"
    assert frozen["schema_version"] == "chapter_budget_v1"
    assert frozen["budget_sha256"] == canonical_json_hash(
        {k: v for k, v in frozen.items() if k != "budget_sha256"}, exclude_environment_fields=False)
    checked = validate_chapter_budget(frozen, expected_book_id="fixture-book", expected_chapter=2)
    rows[0]["model"] = "changed"
    checked["pricing"][0]["model"] = "changed"
    assert [row["model"] for row in frozen["pricing"]] == ["a", "z"]
    for binding in ({"expected_book_id": "other-book"}, {"expected_chapter": 3}):
        with pytest.raises(ChapterBudgetContractError, match="another book or chapter"):
            validate_chapter_budget(frozen, **binding)


@pytest.mark.parametrize("value", [True, False, 1, 0.1, Decimal("0.1"), None, "NaN", "Infinity", "-1", "-0",
                                    "+1", "1e-3", " 1", "1 ", "", "1.", ".1", "١", "9" * 1000,
                                    "0." + "0" * 18 + "1", "1000000000000001"])
def test_decimal_configuration_rejects_coercion_nonfinite_negative_or_unbounded_values(value):
    with pytest.raises(ChapterBudgetContractError):
        normalize_budget_decimal(value)


def test_decimal_configuration_preserves_smallest_supported_fraction_exactly():
    assert normalize_budget_decimal("0.000000000000000001") == "0.000000000000000001"
    assert normalize_budget_decimal("0.000000000000000000") == "0"
    assert normalize_budget_decimal("1000000000000000") == "1000000000000000"
    with pytest.raises(ChapterBudgetContractError):
        budget(max_active_seconds="0")
    assert budget(amount_limit="0")["amount_limit"] == "0"


@pytest.mark.parametrize("field", ["max_provider_calls", "max_input_tokens", "max_output_tokens"])
@pytest.mark.parametrize("value", [0, -1, True, 1.0, "1", 10**15 + 1])
def test_limits_require_positive_bounded_integers(field, value):
    with pytest.raises(ChapterBudgetContractError):
        budget(**{field: value})


def test_missing_prices_allow_token_only_mode_but_never_money_control():
    token_only = budget(pricing=[], currency=None, amount_limit=None)
    assert validate_chapter_budget(token_only) == token_only
    assert token_only["currency"] is token_only["amount_limit"] is None
    with pytest.raises(ChapterBudgetContractError, match="price_required"):
        budget(pricing=[])
    with pytest.raises(ChapterBudgetContractError):
        budget(currency=None)
    with pytest.raises(ChapterBudgetContractError, match="price_missing"):
        select_pricing(token_only, **{name: price()[name] for name in ("provider", "model", "endpoint_type", "endpoint_fingerprint")})


@pytest.mark.parametrize("rows", [[price(), price()], [price(), price(model="other", currency="CNY")],
                                    [price(currency="usd")], [price(endpoint_fingerprint="A" * 64)],
                                    [price(endpoint_fingerprint="https://secret.invalid")],
                                    [price(provider=" synthetic")], [price(model="hidden\nmodel")],
                                    [price(input_token_semantics="guess")], [price(cache_read_price_per_million=None)],
                                    [price(cache_read_price_per_million=-1)], [price(extra_price="1")]])
def test_price_identity_currency_shape_and_cache_rates_are_explicit(rows):
    with pytest.raises(ChapterBudgetContractError):
        budget(pricing=rows)


@pytest.mark.parametrize("changes", [{"provider": "other"}, {"model": "fixture-model-alias"},
                                        {"endpoint_type": "official"}, {"endpoint_fingerprint": "b" * 64}])
def test_price_selection_never_falls_back_across_identity(changes):
    identity = {name: price()[name] for name in ("provider", "model", "endpoint_type", "endpoint_fingerprint")}
    assert select_pricing(budget(), **identity) == price()
    with pytest.raises(ChapterBudgetContractError, match="price_missing"):
        select_pricing(budget(), **{**identity, **changes})


@pytest.mark.parametrize("change", ["hash", "price", "book", "chapter", "extra", "noncanonical_decimal", "order"])
def test_frozen_budget_tampering_or_noncanonical_serialization_is_rejected(change):
    frozen = budget(pricing=[price(model="a"), price(model="z")])
    if change == "hash":
        frozen["budget_sha256"] = "0" * 64
    elif change == "price":
        frozen["pricing"][0]["input_price_per_million"] = "0"
    elif change == "book":
        frozen["book_id"] = "other"
    elif change == "chapter":
        frozen["chapter"] = 3
    elif change == "extra":
        frozen["client_authorized"] = True
    elif change == "noncanonical_decimal":
        frozen["amount_limit"] = "0.1000"
    else:
        frozen["pricing"].reverse()
    with pytest.raises(ChapterBudgetContractError):
        validate_chapter_budget(frozen)


def test_estimate_is_exact_under_small_ambient_decimal_context():
    tariff = price(input_price_per_million="0.000000000000000001", output_price_per_million="0.2")
    with localcontext() as context:
        context.prec = 3
        actual = estimate_attempt_cost(tariff, **usage(input_tokens=1, output_tokens=1))
        reservation = reserve_attempt_cost(tariff, billable_input_upper_bound=1, max_output_tokens=1)
    assert actual["amount"] == "0.000000200000000000000001"
    assert actual["amount"] == reservation["amount"]
    assert actual["terms"][0]["amount"] == "0.000000000000000000000001"


@pytest.mark.parametrize("semantics,inputs", [("input_includes_cache", 100), ("input_excludes_cache", 20)])
def test_cache_inclusive_and_exclusive_counts_produce_identical_disjoint_bill(semantics, inputs):
    tariff = price(input_token_semantics=semantics, cache_read_price_per_million="0.1",
                   cache_write_5m_price_per_million="2", cache_write_1h_price_per_million="4")
    original = copy.deepcopy(tariff)
    calculated = estimate_attempt_cost(tariff, **usage(input_tokens=inputs, cache_read_tokens=30,
        cache_write_5m_tokens=40, cache_write_1h_tokens=10))
    assert calculated["billable_input_tokens"] == 100
    assert calculated["amount"] == "0.000218"  # (20*1.25 + 20*3.5 + 30*.1 + 40*2 + 10*4)/1e6
    assert sum(term["quantity"] for term in calculated["terms"] if term["usage_field"] != "output_tokens") == 100
    assert tariff == original
    reserved = reserve_attempt_cost(tariff, billable_input_upper_bound=100, max_output_tokens=20)
    assert reserved["amount"] == "0.00047"
    assert reserved["terms"][0]["unit_price_per_million"] == "4"
    assert Decimal(reserved["amount"]) >= Decimal(calculated["amount"])


@pytest.mark.parametrize("field", ["cache_read_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens"])
def test_nonzero_cache_requires_that_exact_rate(field):
    with pytest.raises(ChapterBudgetContractError, match="cache_price_missing"):
        estimate_attempt_cost(price(), **usage(**{field: 1}))
    assert estimate_attempt_cost(price(), **usage())["amount"] == "0.000195"


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens"])
@pytest.mark.parametrize("value", [None, True, "0", 0.0, -1])
def test_unknown_or_invalid_billing_quantity_cannot_silently_become_zero(field, value):
    with pytest.raises(ChapterBudgetContractError):
        estimate_attempt_cost(price(), **usage(**{field: value}))


def test_missing_or_aggregate_cache_write_breakdown_is_not_guessed():
    omitted = usage()
    del omitted["cache_write_5m_tokens"]
    with pytest.raises(TypeError):
        estimate_attempt_cost(price(), **omitted)
    with pytest.raises(TypeError):
        estimate_attempt_cost(price(), **usage(), cache_write_tokens=5)
    with pytest.raises(ChapterBudgetContractError, match="cache subsets exceed"):
        estimate_attempt_cost(price(cache_read_price_per_million="0.1"), **usage(input_tokens=1, cache_read_tokens=2))


@pytest.mark.parametrize("semantics", ["input_includes_cache", "input_excludes_cache"])
def test_reservation_covers_known_settlement_for_every_supported_input_bucket(semantics):
    tariff = price(input_token_semantics=semantics, input_price_per_million="0.001",
                   output_price_per_million="0.0001", cache_read_price_per_million="0.009",
                   cache_write_5m_price_per_million="0", cache_write_1h_price_per_million="0.007")
    for plain, read, write5, write1 in [(0, 0, 0, 0), (0, 10, 0, 0), (10, 0, 0, 0),
                                      (0, 0, 10, 0), (0, 0, 0, 10), (11, 3, 5, 7)]:
        total = plain + read + write5 + write1
        charged = estimate_attempt_cost(tariff, **usage(input_tokens=total if semantics == "input_includes_cache" else plain,
            output_tokens=11, cache_read_tokens=read, cache_write_5m_tokens=write5, cache_write_1h_tokens=write1))
        reserved = reserve_attempt_cost(tariff, billable_input_upper_bound=total + 5, max_output_tokens=15)
        assert Decimal(charged["amount"]) <= Decimal(reserved["amount"])


@pytest.mark.parametrize("field,value", [("billable_input_upper_bound", None), ("billable_input_upper_bound", -1),
                                           ("billable_input_upper_bound", True), ("max_output_tokens", 1.5)])
def test_reservation_requires_explicit_bounded_token_upper_limits(field, value):
    kwargs = {"billable_input_upper_bound": 10, "max_output_tokens": 20, field: value}
    with pytest.raises(ChapterBudgetContractError):
        reserve_attempt_cost(price(), **kwargs)

