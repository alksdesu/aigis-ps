from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column
from aigis_ps.app import create_app
from aigis_ps.cli import service_readiness
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from test_evolution_integration import post
from test_quest_service import bundle, request


MONTH_END = datetime(2026, 12, 1, 3, 59, tzinfo=JST)
MONTH_START = datetime(2026, 12, 1, 4, tzinfo=JST)
LIMITED = 10001


@pytest.fixture(autouse=True)
def clock(bundle, monkeypatch):
    moment = SimpleNamespace(now=MONTH_END)
    if bundle.tower_trades is not None:
        monkeypatch.setattr(bundle.tower_trades, "clock", lambda: moment.now)
    return moment


def application(bundle, tmp_path, *, initial=None):
    service = bundle.tower_trades
    assert service is not None, bundle.tower_trade_error
    if initial is None:
        initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
        initial = bundle.possessions.credit(initial, {currency: 100000 for currency in service.catalog.currencies})
    return create_app(tmp_path, initial_state=initial, state_initializer=service.initialize,
        handlers=service.handlers() | bundle.player_state.handlers() | bundle.bootstrap.handlers())


def buying(store, shop=12038, quantity=1, tick=1000):
    return request(store, 69, (Column("SHOP", "I", (shop,)), Column("NUM", "I", (quantity,))), tick=tick)


def querying(store, currency="tower_crystal_110001"):
    return request(store, 65, (Column("ITEM", "S", (currency,)),))


def test_cli_wires_both_tower_operations_and_keeps_initial_save_empty(bundle):
    service = bundle.tower_trades
    assert service is not None, bundle.tower_trade_error
    status = service_readiness(bundle)["tower_trades"]
    assert status["ready"] and status["implemented_operations"] == [65, 69]
    assert status["grants"]["supported_products"] == 164
    saved = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    original = deepcopy(saved)
    assert service.initialize(saved) == saved == original
    assert "tower_trade_history" not in saved and "tower_trade_receipts" not in saved


@pytest.mark.parametrize("shop,quantity", ((10000, 1), (12038, 3)))
def test_native_wire_purchase_restart_replay_and_all_currency_views(bundle, tmp_path, shop, quantity):
    app = application(bundle, tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        _, empty = post(client, querying(app.state.store))
        assert empty["TowerTradeHistory"].children == (Column("ID", "I", ()), Column("TradeCount", "I", ()))
        assert app.state.store.profile() == before
        envelope = buying(app.state.store, shop, quantity)
        wire, response = post(client, envelope)
        after = app.state.store.profile()
        assert after.revision == before.revision + 1
        for currency in bundle.tower_trades.catalog.currencies:
            _, listing = post(client, querying(app.state.store, currency))
            assert listing["TowerTradeHistory"] == response["TowerTradeHistory"]
        assert app.state.store.profile() == after
        assert post(client, envelope)[0] == wire
    reopened = application(bundle, tmp_path)
    assert reopened.state.store.profile() == after
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert reopened.state.store.profile() == after
        _, stock = post(client, request(reopened.state.store, 129, ()))
        paid = {row[0]: row[1] for row in zip(*(column.values for column in response["POSSESSION"].children[:2]))}
        current = {row[0]: row[1] for row in zip(*(column.values for column in stock["POSSESSION"].children[:2]))}
        assert all(current[key] == value for key, value in paid.items())


def test_native_wire_unsupported_summon_and_bad_identity_never_commit(bundle, tmp_path):
    app = application(bundle, tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        rejected = post(client, buying(app.state.store, 10009), expected=501)
        assert rejected.json()["error"]["code"] == "tower_trade_grant_unsupported"
        assert app.state.store.profile() == before
        unknown = post(client, querying(app.state.store, "consume_ticket"), expected=400)
        assert unknown.json()["error"]["code"] == "invalid_request_fields"
        assert app.state.store.profile() == before


def test_existing_tower_save_requires_verified_service_at_startup(bundle, tmp_path):
    app = application(bundle, tmp_path)
    bundle.tower_trades.purchase(buying(app.state.store), app.state.store)
    saved = app.state.store.profile().state
    original = deepcopy(saved)
    with pytest.raises(ProtocolError) as error:
        replace(bundle, tower_trades=None).initialize(saved)
    assert error.value.code == "tower_trade_unavailable"
    assert saved == original


@pytest.mark.parametrize("domain", ("tower_trade_history", "tower_trade_receipts"))
def test_partial_save_deletion_is_rejected_on_reopen(bundle, tmp_path, domain):
    app = application(bundle, tmp_path)
    bundle.tower_trades.purchase(buying(app.state.store), app.state.store)
    before = app.state.store.profile()
    broken = deepcopy(before.state)
    broken.pop(domain)
    app.state.store.update_state(broken, before.revision)
    with pytest.raises(ProtocolError) as error:
        application(bundle, tmp_path)
    assert error.value.code == "tower_trade_receipts_invalid"


def test_native_wire_counts_restart_when_the_business_month_turns(bundle, tmp_path, clock):
    app = application(bundle, tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, first = post(client, buying(app.state.store, LIMITED))
        assert first["TowerTradeHistory"].children == (Column("ID", "I", (LIMITED,)), Column("TradeCount", "I", (1,)))
        refused = post(client, buying(app.state.store, LIMITED, tick=1001), expected=409)
        assert refused.json()["error"]["code"] == "tower_trade_limit_exceeded"
        clock.now = MONTH_START
        before = app.state.store.profile()
        _, listing = post(client, querying(app.state.store))
        assert listing["TowerTradeHistory"].children == (Column("ID", "I", (LIMITED,)), Column("TradeCount", "I", (0,)))
        assert app.state.store.profile() == before
        envelope = buying(app.state.store, LIMITED, tick=1002)
        wire, second = post(client, envelope)
        assert second["TowerTradeHistory"] == first["TowerTradeHistory"]
        after = app.state.store.profile()
    clock.now = MONTH_START + timedelta(days=40)
    reopened = application(bundle, tmp_path)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert reopened.state.store.profile() == after
        _, later = post(client, querying(reopened.state.store))
        assert later["TowerTradeHistory"].children[1] == Column("TradeCount", "I", (0,))
