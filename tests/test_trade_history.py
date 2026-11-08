from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.native_clock import UTC_DAY_ONE
from aigis_ps.trade_catalog import PRODUCT_UNKNOWN
from aigis_ps.trade_history import TradeHistory, TradeRules, server_milliseconds


NOW = datetime(2026, 11, 1, 0, tzinfo=JST)
NEXT = datetime(2026, 12, 1, 0, tzinfo=JST)


def product(identifier=1, *, reset=0, limit=5, start=None, end=None):
    return SimpleNamespace(shop_id=identifier, reset_type=reset, data_restrict=limit, start_at=start, end_at=end)


class Catalog:
    def __init__(self, *products):
        self.products = {item.shop_id: item for item in products}

    def product(self, identifier):
        if identifier not in self.products:
            raise ProtocolError(PRODUCT_UNKNOWN, "Unknown test product")
        return self.products[identifier]


def state():
    return {"bootstrap_schema": 1, "tables": {"exchange_history": []}}


def count(history, saved, selected, now=NOW):
    return history.count(history.records(saved, now), selected, now)


def test_permanent_counts_limit_and_input_remain_unchanged():
    selected = product()
    history = TradeHistory(Catalog(selected))
    source = state()
    saved = history.record_purchase(source, selected, 3, NOW)
    assert source == state()
    assert count(history, saved, selected, NEXT) == 3
    assert saved["tables"]["exchange_history"] == [{"ShopItemID": 1, "ExchangeCount": 3}]
    with pytest.raises(ProtocolError, match="remaining purchase limit"):
        history.record_purchase(saved, selected, 3, NOW)
    assert history.record_purchase(saved, selected, 2, NOW)["trade_history"]["products"]["1"]["lifetime_count"] == 5


def test_unlimited_client_count_zero_keeps_real_server_audit():
    selected = product(limit=0)
    history = TradeHistory(Catalog(selected))
    saved = history.record_purchase(state(), selected, 8, NOW)
    saved = history.record_purchase(saved, selected, 9, NEXT)
    assert saved["trade_history"]["products"]["1"] == {"period": "never", "count": 17, "lifetime_count": 17}
    assert count(history, saved, selected) == 0
    assert saved["tables"]["exchange_history"] == [{"ShopItemID": 1, "ExchangeCount": 0}]


def test_month_boundary_uses_jst_and_explicitly_returns_zero_without_mutation():
    selected = product(reset=1)
    history = TradeHistory(Catalog(selected))
    saved = history.record_purchase(state(), selected, 5, NOW)
    before = deepcopy(saved)
    assert count(history, saved, selected, datetime(2026, 11, 30, 14, 59, 59, tzinfo=timezone.utc)) == 5
    assert count(history, saved, selected, datetime(2026, 11, 30, 15, tzinfo=timezone.utc)) == 0
    response = history.response(saved, NEXT)
    assert response.children[0].values == (1,) and response.children[1].values == (0,)
    assert saved == before
    saved = history.record_purchase(saved, selected, 1, NEXT)
    assert saved["trade_history"]["products"]["1"]["lifetime_count"] == 6


def test_window_resets_when_resource_window_changes_and_never_on_ordinary_reads():
    selected = product(reset=2, end=NEXT)
    catalog = Catalog(selected)
    history = TradeHistory(catalog)
    saved = history.record_purchase(state(), selected, 5, NOW)
    assert count(history, saved, selected, NEXT) == 5
    replacement = product(reset=2, start=NEXT)
    catalog.products[1] = replacement
    assert count(history, saved, replacement, NEXT) == 0
    assert history.record_purchase(saved, replacement, 2, NEXT)["trade_history"]["products"]["1"]["lifetime_count"] == 7


@pytest.mark.parametrize("reset", [1, 2])
def test_unconfigured_reset_cannot_accept_a_purchase(reset):
    selected = product(reset=reset)
    history = TradeHistory(Catalog(selected), TradeRules("unconfigured", "unconfigured"))
    with pytest.raises(ProtocolError) as caught:
        history.record_purchase(state(), selected, 1, NOW)
    assert caught.value.code == "trade_reset_unconfigured"


def test_legacy_nonempty_history_is_baselined_before_first_reset():
    selected = product(reset=1)
    history = TradeHistory(Catalog(selected), TradeRules(reset_type_1="month"))
    source = state()
    source["tables"]["exchange_history"] = [{"ShopItemID": 1, "ExchangeCount": 4}]
    saved = history.initialize(source, NOW)
    assert "trade_history" not in source
    assert count(history, saved, selected) == 4
    assert count(history, saved, selected, NEXT) == 0
    assert history.initialize(saved, NEXT) == saved


def test_changed_rule_carries_existing_count_and_removed_product_remains_visible():
    selected = product(reset=1)
    catalog = Catalog(selected)
    history = TradeHistory(catalog, TradeRules(reset_type_1="never"))
    saved = history.record_purchase(state(), selected, 4, NOW)
    changed = TradeHistory(catalog, TradeRules(reset_type_1="month"))
    assert count(changed, saved, selected, NEXT) == 4
    catalog.products.clear()
    response = changed.response(saved, NEXT)
    assert response.children[0].values == (1,) and response.children[1].values == (4,)


def test_catalog_failures_other_than_an_unknown_product_are_not_read_as_removal():
    selected = product()
    catalog = Catalog(selected)
    history = TradeHistory(catalog)
    saved = history.record_purchase(state(), selected, 1, NOW)

    def broken(identifier):
        raise ProtocolError("trade_catalog_invalid", "Broken test catalog", 503)

    catalog.product = broken
    with pytest.raises(ProtocolError) as caught:
        history.response(saved, NOW)
    assert caught.value.code == "trade_catalog_invalid"


@pytest.mark.parametrize("moment", [
    UTC_DAY_ONE, UTC_DAY_ONE + timedelta(microseconds=999), NOW, NOW + timedelta(microseconds=999999),
    datetime.max.replace(tzinfo=timezone.utc),
])
def test_server_milliseconds_count_whole_milliseconds_from_utc_day_one(moment):
    assert server_milliseconds(moment) == (moment - UTC_DAY_ONE) // timedelta(milliseconds=1)


@pytest.mark.parametrize("moment, message", [
    (datetime(2026, 11, 1), "aware server clock"), (None, "aware server clock"),
    (datetime.min.replace(tzinfo=JST), "cannot be represented in UTC"),
    (datetime.max.replace(tzinfo=timezone(timedelta(hours=-1))), "cannot be represented in UTC"),
])
def test_server_clocks_outside_utc_keep_the_trade_clock_code(moment, message):
    with pytest.raises(ProtocolError, match=message) as caught:
        server_milliseconds(moment)
    assert (caught.value.code, caught.value.status) == ("trade_clock_invalid", 503)


@pytest.mark.parametrize("reset", [1, 2])
def test_rule_activation_baselines_existing_limit_then_allows_next_period(reset):
    selected = product(reset=reset, end=NEXT)
    catalog = Catalog(selected)
    source = state()
    source["tables"]["exchange_history"] = [{"ShopItemID": 1, "ExchangeCount": 5}]
    pending = TradeHistory(catalog, TradeRules("unconfigured", "unconfigured")).initialize(source, NOW)
    history = TradeHistory(catalog)
    saved = history.initialize(pending, NOW)
    assert count(history, saved, selected, NOW) == 5
    with pytest.raises(ProtocolError) as caught:
        history.record_purchase(saved, selected, 1, NOW)
    assert caught.value.code == "trade_limit_exceeded"
    if reset == 2:
        selected = product(reset=reset, start=NEXT)
        catalog.products[1] = selected
    assert count(history, saved, selected, NEXT) == 0
    assert saved["trade_history"]["products"]["1"]["lifetime_count"] == 5
    assert pending["trade_history"]["products"]["1"]["period"] == "unconfigured"


@pytest.mark.parametrize("reset", [1, 2])
def test_explicitly_disabling_defaults_preserves_history_and_blocks_purchase(reset):
    selected = product(reset=reset, end=NEXT)
    catalog = Catalog(selected)
    saved = TradeHistory(catalog).record_purchase(state(), selected, 3, NOW)
    before = deepcopy(saved)
    disabled = TradeHistory(catalog, TradeRules("unconfigured", "unconfigured"))
    assert disabled.initialize(saved, NEXT) == saved
    assert count(disabled, saved, selected, NEXT) == 3
    assert disabled.response(saved, NEXT).children[1].values == (3,)
    with pytest.raises(ProtocolError) as caught:
        disabled.record_purchase(saved, selected, 1, NEXT)
    assert caught.value.code == "trade_reset_unconfigured"
    assert saved == before


def test_clock_reversal_never_resets_counts_or_accepts_purchase():
    selected = product(reset=1)
    history = TradeHistory(Catalog(selected), TradeRules(reset_type_1="month"))
    saved = history.record_purchase(state(), selected, 2, NEXT)
    assert count(history, saved, selected, NOW) == 2
    with pytest.raises(ProtocolError) as caught:
        history.record_purchase(saved, selected, 1, NOW)
    assert caught.value.code == "trade_clock_backwards"


@pytest.mark.parametrize("change", [
    lambda saved: saved.update(bootstrap_schema=True),
    lambda saved: saved.update(trade_history=None),
    lambda saved: saved["trade_history"].update(schema_version=True),
    lambda saved: saved["trade_history"]["products"]["1"].update(period="month:2026-99"),
    lambda saved: saved["trade_history"]["products"]["1"].update(count=True),
    lambda saved: saved["trade_history"]["products"]["1"].update(lifetime_count=0),
    lambda saved: saved["tables"].update(exchange_history=[{"ShopItemID": 1, "ExchangeCount": -1}]),
])
def test_invalid_history_is_rejected_without_replacement(change):
    selected = product()
    history = TradeHistory(Catalog(selected))
    saved = history.record_purchase(state(), selected, 1, NOW)
    change(saved)
    before = deepcopy(saved)
    with pytest.raises(ProtocolError) as caught:
        history.response(saved, NOW)
    assert caught.value.code == "trade_history_invalid"
    assert saved == before


@pytest.mark.parametrize("values", [{"reset_type_1": "day"}, {"reset_type_2": "month"}, {"reset_type_1": True}])
def test_invalid_rule_names_are_rejected(values):
    with pytest.raises(ValueError):
        TradeRules(**values)
