from copy import copy
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column
from aigis_ps.app import create_app
from aigis_ps.crystal_wallet import credit_free_crystals, wallet_totals
from aigis_ps.energy_clock import JST
from aigis_ps.trade_catalog import TradeCatalog
from aigis_ps.trade_grants import TradeUnitGrants
from aigis_ps.trade_history import TradeRules
from aigis_ps.trade_service import TradeService
from test_evolution_integration import post
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


@pytest.fixture(scope="module")
def catalog(bundle):
    return TradeCatalog(bundle.gifts.catalog, bundle.possessions,
        lambda name: bundle.native_cdn.assets.read(name)[0], resource_version=bundle.native_cdn.version)


def application(bundle, catalog, path, state, *, rules=None, now=NOW):
    bootstrap = copy(bundle.bootstrap)
    service = TradeService(bootstrap, bundle.player_state, bundle.possessions, catalog,
        grants=TradeUnitGrants(bundle.player_state),
        rules=rules or TradeRules(reset_type_1="never", reset_type_2="never"), clock=lambda: now)
    bootstrap.exchange_history_provider = service.history_response
    def initialize(saved):
        saved = bundle.player_state.initialize(saved)
        saved = bundle.evolution_rewards.initialize(saved)
        return service.initialize(saved)
    app = create_app(path, initial_state=initialize(state), state_initializer=initialize,
        handlers=bootstrap.handlers() | bundle.player_state.handlers() | service.handlers())
    return app, service


def funded(bundle, product, quantity=1, *, now=None):
    bootstrap = copy(bundle.bootstrap)
    if now is not None:
        bootstrap.clock = lambda: now
    state = bundle.player_state.initialize(bootstrap.initial_state())
    price = product.tiers[0][1] * quantity
    state["tables"]["game_param"][0]["Money"] = max(1000000, price + 1)
    state["tables"]["quest_records"] = [{"QuestID": identifier, "MaxRank": 3, "AchieveFlag": 0x01000000,
        "RemainAssault": 0, "FirstDefeatDate": 0, "Shown": True} for identifier in product.disclosure_quest_ids]
    if product.currency_kind == "possession":
        state = bundle.possessions.credit(state, {product.currency_item_id: max(1, price)})
    elif product.currency_kind == "crystals":
        state = credit_free_crystals(state, max(1, price), 739555)
    return state


def buy(store, product, quantity=1, tick=1000):
    return request(store, product.registration,
        (Column("SHOP", "I", (product.shop_id,)), Column("NUM", "I", (quantity,))), tick=tick)


def available(product):
    return (product.ordinary and product.currency_kind is not None
        and (product.start_at is None or product.start_at <= NOW)
        and (product.end_at is None or NOW < product.end_at))


@pytest.mark.parametrize("registration", [104, 105, 107, 109, 110, 111, 112, 113])
def test_all_ordinary_trade_routes_charge_grant_and_replay_after_restart(bundle, catalog, tmp_path, registration):
    products = [product for product in catalog.products if product.ordinary and product.currency_kind is not None and product.registration == registration]
    active = [product for product in products if available(product)]
    product = min(active or products, key=lambda item: (item.product_type != 3, bool(item.disclosure_quest_ids), item.shop_id))
    now = NOW if active else product.start_at or product.end_at - timedelta(seconds=1)
    state = funded(bundle, product, now=now)
    app, service = application(bundle, catalog, tmp_path, state, now=now)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        quote = catalog.quote(before.state, product.shop_id, 1, 0, now, registration)
        envelope = buy(app.state.store, product)
        wire, response = post(client, envelope)
        saved = app.state.store.profile()
        assert saved.revision == before.revision + 1
        assert "ExchangeHistory" in response
        if quote.currency_kind == "money":
            assert saved.state["tables"]["game_param"][0]["Money"] == before.state["tables"]["game_param"][0]["Money"] - quote.total_price
        elif quote.currency_kind == "crystals":
            assert wallet_totals(saved.state)["total"] == wallet_totals(before.state)["total"] - quote.total_price
        else:
            holdings = {row["ItemID"]: row["Count"] for row in saved.state["tables"]["possessions"]}
            expected = quote.currency_balance - quote.total_price
            if product.product_type == 3 and product.item_id == quote.currency_item_id:
                expected += quote.amount
            assert holdings[quote.currency_item_id] == expected
        if product.product_type == 1:
            assert len(saved.state["player_state"]["units"]) == len(before.state["player_state"]["units"]) + quote.amount
            assert response["PPU"].children[0].values == (product.card_id,) * quote.amount
        else:
            holdings = {row["ItemID"]: row["Count"] for row in saved.state["tables"]["possessions"]}
            assert holdings[product.item_id] >= quote.amount
        assert post(client, envelope)[0] == wire and app.state.store.profile() == saved
    reopened, _ = application(bundle, catalog, tmp_path, state, now=now)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        _, configuration = post(client, request(reopened.state.store, 137, ()))
        rows = configuration["ExchangeHistory"]
        assert dict(zip(rows.children[0].values, rows.children[1].values))[product.shop_id] == (1 if product.data_restrict else 0)
        assert reopened.state.store.profile() == saved


@pytest.mark.parametrize("shop_id,class_id,skill_id", [(768, 141420, 2800), (769, 150920, 2703), (348, 150920, 2024)])
def test_explicit_and_native_forced_awakened_products_keep_their_initial_stage(bundle, catalog, tmp_path, shop_id, class_id, skill_id):
    product = catalog.product(shop_id)
    app, _ = application(bundle, catalog, tmp_path, funded(bundle, product), now=product.start_at or NOW)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, response = post(client, buy(app.state.store, product))
        unit = app.state.store.profile().state["player_state"]["units"][-1]
        assert unit["CardID"] == product.card_id
        assert (unit["ClassID"], unit["ClassChangeCount"], unit["Exp"], unit["SkillLevel"], unit["SettingSkillID"]) == (class_id, 2, 0, 1, skill_id)
        assert len(response["PPU"].children) == 21
        assert response["PBP"].children[0].values == ("UX",)
        assert response["PBP"].children[1].values == (unit["GetIndex"],)


def test_pending_reset_policy_rejects_trade_without_spending(bundle, catalog, tmp_path):
    product = next(product for product in catalog.products if available(product) and product.reset_type == 1)
    app, _ = application(bundle, catalog, tmp_path, funded(bundle, product),
        rules=TradeRules("unconfigured", "unconfigured"))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        post(client, buy(app.state.store, product), 501)
        assert app.state.store.profile() == before


@pytest.mark.parametrize("reset", [1, 2])
def test_default_reset_policy_preserves_legacy_usage_and_accepts_remaining_purchase(bundle, catalog, tmp_path, reset):
    product = next(product for product in catalog.products if available(product)
        and product.reset_type == reset and product.data_restrict > 0 and len(product.tiers) == 1)
    state = funded(bundle, product)
    state["tables"]["exchange_history"] = [{"ShopItemID": product.shop_id,
        "ExchangeCount": product.data_restrict - 1}]
    app, service = application(bundle, catalog, tmp_path, state, rules=TradeRules())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert service.status()["unconfigured_resets"] == []
        _, response = post(client, buy(app.state.store, product))
        saved = app.state.store.profile()
        record = saved.state["trade_history"]["products"][str(product.shop_id)]
        assert record["count"] == record["lifetime_count"] == product.data_restrict
        assert record["period"].startswith("month:" if reset == 1 else "window:")
        rows = response["ExchangeHistory"]
        assert dict(zip(rows.children[0].values, rows.children[1].values))[product.shop_id] == product.data_restrict
        rejected = post(client, buy(app.state.store, product, tick=1001), 409)
        assert rejected.json()["error"]["code"] == "trade_limit_reached"
        assert app.state.store.profile() == saved
