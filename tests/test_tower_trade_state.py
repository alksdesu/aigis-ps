from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import ValueType
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.tower_trade_state import (INT32_MAX, REBIND_UNNEEDED, TowerTradeHistory, TowerTradeRecord, history_digest,
                                        rebind)
from test_tower_trade_catalog import build, native_catalog, product, sources
from test_trade_catalog import native_date


NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
PERIOD, NEXT_PERIOD = "2026-11", "2026-12"
LATER_THIS_MONTH, NEXT_MONTH = NOW + timedelta(days=20), NOW + timedelta(days=30)
MONTH_END = datetime(2026, 12, 1, 3, 59, 59, 999999, tzinfo=JST)
MONTH_START = datetime(2026, 12, 1, 4, tzinfo=JST)
BEFORE_CALENDAR = datetime(1960, 1, 1, tzinfo=timezone.utc)
CURRENCY = "tower_crystal_110001"
SOURCE = "a" * 64
RECORD = {"source": SOURCE, "period": PERIOD, "count": 1, "lifetime": 1}
# The history version that receipts committed while purchase counts never reset still carry.
NEVER_RESET_TERMS = {"schema": 1, "reset_policy": "never", "source_identity": "card_item_currency_effect",
                     "price": "current_purchase_ordinal_tier", "batch_crossing_price_tiers": "unavailable",
                     "date": "both_boundaries_required", "disclosure": "all_titles_and_owned_item",
                     "response": "all_currencies_and_retained_records"}


def state():
    return {"bootstrap_schema": 1, "tables": {"possessions": [{"ItemID": CURRENCY, "Count": 50000}],
            "game_param": [{"Rank": 1}], "quest_records": []}, "unrelated": {"keep": [1, 2, 3]}}


def subject(rows=None, **kwargs):
    return TowerTradeHistory(build(sources(rows), **kwargs))


def fail(code, function):
    with pytest.raises(ProtocolError) as caught:
        function()
    assert caught.value.code == code


def counts(response):
    return response.children[1].values


def test_missing_history_initialization_is_pure_and_empty_response_is_typed():
    history, saved = subject(), state()
    original = deepcopy(saved)
    initialized = history.initialize(saved)
    assert initialized == saved == original and initialized is not saved
    assert "tower_trade_history" not in initialized
    selected = history.catalog.product(1)
    assert history.count(saved, selected, NOW) == history.lifetime(saved, selected) == 0
    response = history.response(saved, CURRENCY, PERIOD)
    assert response.name == "TowerTradeHistory"
    assert [(column.name, column.type, column.values) for column in response.children] == [
        ("ID", ValueType.INT, ()), ("TradeCount", ValueType.INT, ())]


def test_recording_saves_the_month_count_beside_the_lifetime_and_quote_records_are_frozen():
    history, saved = subject(), state()
    original = deepcopy(saved)
    selected = history.catalog.product(1)
    quote = history.quote(saved, selected, 3, NOW)
    assert (quote.total_price, quote.quantity, quote.currency_item_id, quote.unit_price, quote.previous_count,
            quote.period) == (300, 3, CURRENCY, 100, 0, PERIOD)
    with pytest.raises(FrozenInstanceError):
        quote.total_price = 0
    updated = history.record_purchase(saved, selected, 3, NOW)
    assert saved == original and updated["tables"] == saved["tables"]
    assert updated["unrelated"] == saved["unrelated"]
    assert (history.count(updated, selected, NOW), history.lifetime(updated, selected)) == (3, 3)
    assert updated["tower_trade_history"] == {"schema_version": 2, "records": {
        "1": {"source": history.source(selected), "period": PERIOD, "count": 3, "lifetime": 3}}}
    with pytest.raises(TypeError):
        history.records(updated)[1] = None
    with pytest.raises(FrozenInstanceError):
        history.records(updated)[1].count = 0


def test_price_follows_this_months_purchase_ordinal_and_restarts_each_month():
    rows = [product(RequiredValue=200, SingleBuyOnly=1),
            product(0, CardID=0, RequiredGeneralItemID="", TradeCount=2, RequiredValue=150)]
    history, saved = subject(rows), state()
    selected = history.catalog.product(1)
    assert history.quote(saved, selected, 1, NOW).total_price == 200
    saved = history.record_purchase(saved, selected, 1, NOW)
    assert history.quote(saved, selected, 1, NOW).total_price == 150
    saved = history.record_purchase(saved, selected, 1, NOW)
    assert history.quote(saved, selected, 1, LATER_THIS_MONTH).total_price == 150
    assert history.quote(saved, selected, 1, NEXT_MONTH).total_price == 200
    assert (history.count(saved, selected, NOW), history.count(saved, selected, NEXT_MONTH)) == (2, 0)
    saved = history.record_purchase(saved, selected, 1, NEXT_MONTH)
    assert history.quote(saved, selected, 1, NEXT_MONTH).total_price == 150
    assert history.lifetime(saved, selected) == 3


def test_unverified_batch_across_different_tiers_rejects_without_writes():
    rows = [product(RequiredValue=200), product(0, CardID=0, RequiredGeneralItemID="", TradeCount=3, RequiredValue=150)]
    history, saved = subject(rows), state()
    before = deepcopy(saved)
    assert history.quote(saved, history.catalog.product(1), 2, NOW).total_price == 400
    fail("tower_trade_tier_batch_unsupported", lambda: history.record_purchase(saved, history.catalog.product(1), 3, NOW))
    assert saved == before
    rows[1]["RequiredValue"] = 200
    same_price = subject(rows)
    assert same_price.quote(saved, same_price.catalog.product(1), 4, NOW).total_price == 800


def test_limits_count_this_month_and_single_buy_is_separate_from_the_limit():
    history, saved = subject([product(Limitation=2, SingleBuyOnly=1), product(2)]), state()
    selected = history.catalog.product(1)
    fail("tower_trade_single_only", lambda: history.quote(saved, selected, 2, NOW))
    saved = history.record_purchase(saved, selected, 1, NOW)
    saved = history.record_purchase(saved, selected, 1, NOW + timedelta(days=1))
    fail("tower_trade_limit_exceeded", lambda: history.quote(saved, selected, 1, LATER_THIS_MONTH))
    assert history.quote(saved, selected, 1, NEXT_MONTH).previous_count == 0
    assert (history.count(saved, selected, NOW), history.lifetime(saved, selected)) == (2, 2)
    assert history.quote(saved, history.catalog.product(2), 100, NOW).total_price == 10000


def test_month_turns_on_the_first_at_the_business_day_start():
    history, saved = subject([product(Limitation=1)]), state()
    selected = history.catalog.product(1)
    assert (history.period(MONTH_END), history.period(MONTH_START)) == (PERIOD, NEXT_PERIOD)
    saved = history.record_purchase(saved, selected, 1, MONTH_END)
    fail("tower_trade_limit_exceeded", lambda: history.quote(saved, selected, 1, MONTH_END))
    assert (history.count(saved, selected, MONTH_END), history.count(saved, selected, MONTH_START)) == (1, 0)
    assert counts(history.response(saved, CURRENCY, history.period(MONTH_END))) == (1,)
    assert counts(history.response(saved, CURRENCY, history.period(MONTH_START))) == (0,)
    saved = history.record_purchase(saved, selected, 1, MONTH_START)
    assert saved["tower_trade_history"]["records"]["1"] == {
        "source": history.source(selected), "period": NEXT_PERIOD, "count": 1, "lifetime": 2}
    fail("tower_trade_limit_exceeded", lambda: history.quote(saved, selected, 1, MONTH_START))


def test_a_clock_set_back_before_the_saved_month_keeps_its_count_and_refuses_that_product():
    history, saved = subject([product(Limitation=3), product(2)]), state()
    selected = history.catalog.product(1)
    saved = history.record_purchase(saved, selected, 1, MONTH_START)
    before = deepcopy(saved)
    assert history.count(saved, selected, MONTH_END) == 1
    assert counts(history.response(saved, CURRENCY, history.period(MONTH_END))) == (1,)
    fail("tower_trade_clock_backwards", lambda: history.record_purchase(saved, selected, 1, MONTH_END))
    assert saved == before
    other = history.record_purchase(saved, history.catalog.product(2), 1, MONTH_END)
    assert other["tower_trade_history"]["records"] == {**before["tower_trade_history"]["records"], "2": {
        "source": history.source(history.catalog.product(2)), "period": PERIOD, "count": 1, "lifetime": 1}}
    assert counts(history.response(other, CURRENCY, PERIOD)) == (1, 1)
    assert history.quote(other, selected, 1, MONTH_START).previous_count == 1


def test_date_requires_both_boundaries_and_end_is_exclusive():
    begin, end = NOW, NOW + timedelta(days=1)
    rows = [product(StartDateTime=native_date(begin), EndDateTime=native_date(end)),
            product(2, StartDateTime=native_date(end)), product(3, EndDateTime=native_date(begin))]
    history, saved = subject(rows), state()
    selected = history.catalog.product(1)
    actual_begin, actual_end = selected.start_at, selected.end_at
    assert history.quote(saved, selected, 1, actual_begin).total_price == 100
    fail("tower_trade_not_available", lambda: history.quote(saved, selected, 1, actual_begin - timedelta(microseconds=1)))
    fail("tower_trade_not_available", lambda: history.quote(saved, selected, 1, actual_end))
    assert history.quote(saved, history.catalog.product(2), 1, NOW - timedelta(days=100)).total_price == 100
    assert history.quote(saved, history.catalog.product(3), 1, NOW + timedelta(days=100)).total_price == 100


def test_all_emblem_predicates_and_positive_item_ownership_are_required():
    history = subject([product(DiscloseEmblemIDs="18,19,", DiscloseGeneralItemID="gift")], emblems=True)
    calls, eligible = [], {18: True, 19: False}
    def check(index, saved):
        calls.append(index)
        return eligible[index]
    history.catalog.emblems.eligible = check
    saved, selected = state(), history.catalog.product(1)
    fail("tower_trade_locked", lambda: history.quote(saved, selected, 1, NOW))
    assert calls == [18, 19]
    eligible[19] = True
    fail("tower_trade_locked", lambda: history.quote(saved, selected, 1, NOW))
    saved["tables"]["possessions"].append({"ItemID": "gift", "Count": 0})
    fail("tower_trade_locked", lambda: history.quote(saved, selected, 1, NOW))
    saved["tables"]["possessions"][-1]["Count"] = 1
    assert history.quote(saved, selected, 1, NOW).total_price == 100
    eligible[18] = 1
    fail("tower_trade_disclosure_unavailable", lambda: history.quote(saved, selected, 1, NOW))


def test_missing_emblem_provider_and_unsupported_reward_do_not_touch_state():
    history = subject([product(DiscloseEmblemIDs="18,"), product(2, CardID=0, GeneralItemID="ticket")])
    saved, before = state(), state()
    fail("tower_trade_disclosure_unavailable", lambda: history.record_purchase(saved, history.catalog.product(1), 1, NOW))
    fail("tower_trade_unsupported", lambda: history.record_purchase(saved, history.catalog.product(2), 1, NOW))
    assert saved == before


@pytest.mark.parametrize("quantity", [0, -1, True, False, "1", 1.0, None, INT32_MAX + 1])
def test_invalid_quantities_never_write(quantity):
    history, saved = subject(), state()
    before = deepcopy(saved)
    fail("tower_trade_quantity_invalid", lambda: history.record_purchase(saved, history.catalog.product(1), quantity, NOW))
    assert saved == before


@pytest.mark.parametrize("clock", [None, "2026-11-01", datetime(2026, 11, 1), BEFORE_CALENDAR])
def test_quotes_and_months_require_an_aware_clock_inside_the_client_calendar(clock):
    history = subject()
    fail("tower_trade_clock_invalid", lambda: history.quote(state(), history.catalog.product(1), 1, clock))
    fail("tower_trade_clock_invalid", lambda: history.count(state(), history.catalog.product(1), clock))
    fail("tower_trade_clock_invalid", lambda: history.period(clock))


def test_count_and_total_overflow_reject_atomically():
    history, saved = subject(), state()
    selected = history.catalog.product(1)
    saved = history.record_purchase(saved, selected, 1, NOW)
    saved["tower_trade_history"]["records"]["1"]["lifetime"] = INT32_MAX
    fail("tower_trade_history_capacity", lambda: history.record_purchase(saved, selected, 1, NOW))
    fail("tower_trade_history_capacity", lambda: history.record_purchase(saved, selected, 1, NEXT_MONTH))
    assert (history.count(saved, selected, NOW), history.lifetime(saved, selected)) == (1, INT32_MAX)
    fail("tower_trade_price_overflow", lambda: history.quote(state(), selected, INT32_MAX, NOW))


@pytest.mark.parametrize("domain", [
    None, [], {}, {"schema_version": True, "records": {}}, {"schema_version": 3, "records": {}},
    {"schema_version": 1, "records": [], "extra": 1},
    {"schema_version": 1, "records": {"01": {"count": 1, "source": SOURCE}}},
    {"schema_version": 1, "records": {"2147483648": {"count": 1, "source": SOURCE}}},
    {"schema_version": 1, "records": {"1": {"count": 0, "source": SOURCE}}},
    {"schema_version": 1, "records": {"1": {"count": True, "source": SOURCE}}},
    {"schema_version": 1, "records": {"1": {"count": 1, "source": "bad"}}},
    {"schema_version": 1, "records": {"1": {"count": 1, "source": SOURCE, "reset": "month"}}},
    {"schema_version": 1, "records": {"1": RECORD}},
    {"schema_version": 2, "records": {"1": {"count": 1, "source": SOURCE}}},
    {"schema_version": 2, "records": {"01": RECORD}},
    {"schema_version": 2, "records": {"1": {**RECORD, "reset": "month"}}},
    {"schema_version": 2, "records": {"1": {name: value for name, value in RECORD.items() if name != "lifetime"}}},
    {"schema_version": 2, "records": {"1": {**RECORD, "source": "A" * 64}}},
    {"schema_version": 2, "records": {"1": {**RECORD, "count": 2}}},
    {"schema_version": 2, "records": {"1": {**RECORD, "count": -1}}},
    {"schema_version": 2, "records": {"1": {**RECORD, "count": True}}},
    {"schema_version": 2, "records": {"1": {**RECORD, "lifetime": INT32_MAX + 1}}},
    {"schema_version": 2, "records": {"1": {**RECORD, "period": None, "count": 0, "lifetime": 0}}},
    {"schema_version": 2, "records": {"1": {**RECORD, "period": None}}},
    {"schema_version": 2, "records": {"1": {**RECORD, "count": 0}}},
    {"schema_version": 2, "records": {"1": {**RECORD, "period": "2026-13"}}},
    {"schema_version": 2, "records": {"1": {**RECORD, "period": "2026-1"}}},
    {"schema_version": 2, "records": {"1": {**RECORD, "period": 202611}}},
])
def test_malformed_history_is_rejected_instead_of_reinitialized(domain):
    history, saved = subject(), state()
    saved["tower_trade_history"] = domain
    before = deepcopy(saved)
    fail("tower_trade_history_invalid", lambda: history.initialize(saved))
    fail("tower_trade_history_invalid", lambda: history_digest(saved))
    assert saved == before


def test_schema_1_counts_read_as_lifetime_totals_with_an_empty_month_and_are_never_rewritten():
    history = subject([product(Limitation=2), product(2)])
    selected = history.catalog.product(1)
    saved = state()
    saved["tower_trade_history"] = {"schema_version": 1, "records": {
        "1": {"count": 2, "source": history.source(selected)}, "7": {"count": 4, "source": SOURCE}}}
    original = deepcopy(saved)
    assert history.initialize(saved) == saved == original
    assert dict(history.records(saved)) == {1: TowerTradeRecord(history.source(selected), None, 0, 2),
                                            7: TowerTradeRecord(SOURCE, None, 0, 4)}
    assert (history.count(saved, selected, NOW), history.lifetime(saved, selected)) == (0, 2)
    assert counts(history.response(saved, CURRENCY, PERIOD)) == (0, 0)
    assert counts(history.legacy_response(saved, CURRENCY)) == (2, 4)
    assert history.quote(saved, selected, 2, NOW).previous_count == 0
    updated = history.record_purchase(saved, selected, 1, NOW)
    assert saved == original
    assert updated["tower_trade_history"] == {"schema_version": 2, "records": {
        "1": {"source": history.source(selected), "period": PERIOD, "count": 1, "lifetime": 3},
        "7": {"source": SOURCE, "period": None, "count": 0, "lifetime": 4}}}


def test_saved_digest_and_never_reset_version_match_what_earlier_receipts_recorded():
    history, saved = subject(), state()
    assert history_digest(saved) == sha256(b"{}").hexdigest()
    legacy = {"1": {"count": 2, "source": SOURCE}}
    saved["tower_trade_history"] = {"schema_version": 1, "records": legacy}
    assert history_digest(saved) == sha256(json.dumps(legacy, sort_keys=True,
                                                      separators=(",", ":")).encode()).hexdigest()
    rebound = rebind(saved, history.catalog.product(1), True)
    assert history_digest(rebound) != history_digest(saved)
    expected = sha256(json.dumps({**NEVER_RESET_TERMS, "catalog": history.catalog.content_version}, sort_keys=True,
                                 separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert history.legacy_content_version == expected != history.content_version
    status = history.status()
    assert (status["ready"], status["reset_policy"], status["legacy_content_version"]) == (
        True, "business_month", expected)


def test_resource_prices_limits_and_removed_products_do_not_reset_old_counts():
    first, saved = subject([product(), product(2)]), state()
    saved = first.record_purchase(saved, first.catalog.product(1), 2, NOW)
    source = saved["tower_trade_history"]["records"]["1"]["source"]
    second = subject([product(RequiredValue=150, Limitation=1), product(2)])
    assert second.content_version != first.content_version
    assert second.count(saved, second.catalog.product(1), NOW) == 2
    assert second.records(saved)[1].source == source
    fail("tower_trade_limit_exceeded", lambda: second.quote(saved, second.catalog.product(1), 1, NOW))
    retired = subject([product(2)])
    assert retired.initialize(saved) == saved
    response = retired.response(saved, CURRENCY, PERIOD)
    assert response.children[0].values == (1,) and counts(response) == (2,)


def test_reused_product_loads_but_refuses_every_read_until_it_is_rebound():
    first, saved = subject(), state()
    selected = first.catalog.product(1)
    saved = first.record_purchase(saved, selected, 1, NOW)
    changed = subject([product(CardID=8)])
    assert changed.initialize(saved) == saved and changed.reused(saved) == (1,)
    assert changed.stored(saved)[1].source == first.source(selected)
    fail("tower_trade_product_reused", lambda: changed.records(saved))
    fail("tower_trade_product_reused", lambda: changed.quote(saved, changed.catalog.product(1), 1, NOW))
    fail("tower_trade_product_reused", lambda: changed.response(saved, CURRENCY, PERIOD))
    fail("tower_trade_product_invalid", lambda: first.quote(saved, replace(selected, limitation=0), 1, NOW))
    assert first.reused(saved) == ()


def test_reused_products_are_listed_in_id_order_whatever_the_saved_order():
    first, saved = subject([product(), product(2)]), state()
    for shop in (2, 1):
        saved = first.record_purchase(saved, first.catalog.product(shop), 1, NOW)
    assert list(saved["tower_trade_history"]["records"]) == ["2", "1"]
    assert subject([product(CardID=8), product(2, CardID=8)]).reused(saved) == (1, 2)


@pytest.mark.parametrize("carry", [True, False])
def test_rebinding_moves_the_record_to_the_current_identity(carry):
    first, saved = subject([product(Limitation=2), product(2)]), state()
    saved = first.record_purchase(saved, first.catalog.product(1), 2, NOW)
    saved = first.record_purchase(saved, first.catalog.product(2), 1, NOW)
    changed = subject([product(Limitation=2, CardID=8), product(2)])
    selected = changed.catalog.product(1)
    original = deepcopy(saved)
    updated = rebind(saved, selected, carry)
    assert saved == original and updated["unrelated"] == original["unrelated"]
    records = updated["tower_trade_history"]["records"]
    assert records["1"] == {"source": changed.source(selected), "period": PERIOD if carry else None,
                            "count": 2 if carry else 0, "lifetime": 2}
    assert records["2"] == original["tower_trade_history"]["records"]["2"]
    assert changed.reused(updated) == () and changed.lifetime(updated, selected) == 2
    if carry:
        fail("tower_trade_limit_exceeded", lambda: changed.quote(updated, selected, 1, NOW))
    else:
        assert changed.quote(updated, selected, 2, NOW).previous_count == 0


def test_rebinding_a_schema_1_history_writes_schema_2_and_keeps_its_lifetime_total():
    changed, saved = subject([product(CardID=8)]), state()
    saved["tower_trade_history"] = {"schema_version": 1, "records": {"1": {"count": 3, "source": SOURCE}}}
    updated = rebind(saved, changed.catalog.product(1), True)
    assert updated["tower_trade_history"] == {"schema_version": 2, "records": {
        "1": {"source": changed.source(changed.catalog.product(1)), "period": None, "count": 0, "lifetime": 3}}}


def test_rebinding_refuses_records_needing_no_change_and_forged_arguments():
    history, saved = subject(), state()
    selected = history.catalog.product(1)
    fail(REBIND_UNNEEDED, lambda: rebind(saved, selected, True))
    saved = history.record_purchase(saved, selected, 1, NOW)
    fail(REBIND_UNNEEDED, lambda: rebind(saved, selected, False))
    fail("tower_trade_product_invalid", lambda: rebind(saved, SimpleNamespace(shop_id=1), True))
    fail("tower_trade_rebind_invalid", lambda: rebind(saved, selected, 1))
    broken = {**saved, "tower_trade_history": {"schema_version": 2, "records": {"1": {**RECORD, "count": 5}}}}
    fail("tower_trade_history_invalid", lambda: rebind(broken, selected, True))


def test_response_includes_other_currencies_so_native_clear_does_not_erase_limits(native_catalog):
    history, saved = TowerTradeHistory(native_catalog), state()
    selected = []
    for currency in native_catalog.currencies:
        match = next(product for product in native_catalog.products if product.currency_item_id == currency
                     and product.card_id and not product.disclose_emblem_ids and not product.disclose_item_id)
        saved = history.record_purchase(saved, match, 1, NOW)
        selected.append(match.shop_id)
    for currency in native_catalog.currencies:
        response = history.response(saved, currency, PERIOD)
        assert response.children[0].values == tuple(sorted(selected))
        assert counts(response) == (1, 1, 1)
        assert counts(history.response(saved, currency, NEXT_PERIOD)) == (0, 0, 0)
    fail("tower_trade_currency_unknown", lambda: history.response(saved, "unrecognized_currency", PERIOD))


def test_history_detects_catalog_fingerprint_drift():
    history = subject()
    history.catalog.content_version = "a" * 64
    fail("tower_trade_history_changed", lambda: history.records(state()))
