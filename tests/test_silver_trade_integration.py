from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.barracks_service import EXTRA_SHIFT, PURCHASE_FIELD
from aigis_ps.energy_clock import JST
from aigis_ps.player_state import BARRACKS_SHIFT, LOCK_FLAG
from aigis_ps.protocol import encode_response
from aigis_ps.trade_grants import TradeUnitGrants
from aigis_ps.trade_history import TradeRules
from aigis_ps.trade_service import TradeService
from test_evolution_integration import post
from test_quest_service import bundle, request
from test_trade_integration import funded


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
CURRENCY = "trade_item_001000"
PRODUCT_MATERIALS = (
    (29, 1122, 8), (30, 1093, 14), (31, 1094, 23), (32, 1062, 238),
    (33, 204, 139), (34, 205, 12), (35, 206, 19), (36, 215, 24),
    (37, 174, 42), (38, 192, 123), (330, 153, 14), (331, 10, 8),
    (332, 310, 126), (333, 173, 139), (334, 14, 123), (335, 126, 10),
    (336, 237, 204), (337, 96, 19), (338, 118, 21), (339, 12, 42),
    (340, 85, 215), (341, 295, 23), (342, 123, 14), (343, 238, 24),
)


def application(bundle, path, state, *, silver=True):
    bootstrap = copy(bundle.bootstrap)
    service = TradeService(bootstrap, bundle.player_state, bundle.possessions, bundle.trades.catalog,
        grants=TradeUnitGrants(bundle.player_state), rules=TradeRules("never", "never"), clock=lambda: NOW,
        **({"silver": bundle.trades.silver, "gifts": bundle.gifts,
            "evolution_rewards": bundle.evolution_rewards} if silver else {}))
    bootstrap.exchange_history_provider = service.history_response
    app = create_app(path, initial_state=service.initialize(state), state_initializer=service.initialize,
        handlers=bootstrap.handlers() | bundle.player_state.handlers() | service.handlers())
    return app, service


def seeded(bundle, card_id=8, *, count=1, currency=20):
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state, added = bundle.player_state.credit_units(state, [{"card_id": card_id, "level": 1, "count": count}])
    state["tables"]["game_param"][0]["Money"] = 1000
    if currency:
        state = bundle.possessions.credit(state, {CURRENCY: currency})
    return state, added


def exchange(store, material, *, shop=29, quantity=1, tick=1000):
    return request(store, 106, (Column("SHOP", "I", (shop,)), Column("NUM", "I", (quantity,)),
        Container("BASE", (Column("A7", "I", (material["GetIndex"],)),
                           Column("AB", "I", (material["UniqueID"],))))), tick=tick)


def fields(node):
    return {column.name: column.values for column in node.children}


def reject(client, envelope, store, *, status=409):
    before = store.profile()
    response = post(client, envelope, expected=status)
    assert response.json()["error"]["code"] != "internal_error"
    assert store.profile() == before
    return response


@pytest.mark.parametrize("shop,product_card,material_card", PRODUCT_MATERIALS)
def test_all_24_real_silver_products_consume_one_unit_and_twenty_crystals(bundle, tmp_path, shop, product_card, material_card):
    state, added = seeded(bundle, material_card)
    app, _ = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        _, response = post(client, exchange(app.state.store, added[0], shop=shop))
        saved = app.state.store.profile()
        units = saved.state["player_state"]["units"]
        assert saved.revision == before.revision + 1
        assert len(units) == len(before.state["player_state"]["units"])
        assert all(row["GetIndex"] != added[0]["GetIndex"] for row in units)
        product = units[-1]
        assert product["CardID"] == product_card
        assert product["GetIndex"] > before.state["player_state"]["battle"]["UX"]
        assert product["UniqueID"] > before.state["player_state"]["last_unique_id"]
        assert (product["Exp"], product["Love"], product["SkillLevel"], product["CostModMixValue"]) == (0, 0, 1, 0)
        assert set(response) == {"SYSTEM", "PBP", "PPU", "POSSESSION", "ExchangeHistory"}
        assert len(response["PPU"].children) == 21
        assert fields(response["PPU"])["A1"] == (product_card,)
        assert fields(response["PBP"]) == {"A1": ("UX",), "A2": (product["GetIndex"],)}
        assert fields(response["POSSESSION"]) == {"ItemID": (CURRENCY,), "Count": (0,), "Enabled": (0,)}
        history = fields(response["ExchangeHistory"])
        assert dict(zip(history["ShopItemID"], history["ExchangeCount"]))[shop] == 0
        assert saved.state["trade_history"]["products"][str(shop)]["lifetime_count"] == 1
        assert saved.state["tables"]["game_param"] == before.state["tables"]["game_param"]
        assert saved.state["tables"]["crystals"] == before.state["tables"]["crystals"]


def test_grown_same_family_material_preserves_history_and_other_instance_after_restart(bundle, tmp_path):
    state, added = seeded(bundle, count=2, currency=40)
    state, _ = bundle.player_state.credit_units(state, [{"card_id": 17, "level": 1, "count": 1}])
    evolved = state["player_state"]["units"][-1]
    evolved.update(ClassChangeCount=3, ClassID=bundle.evolution.catalog.route(bundle.gifts.catalog.card(17), 3)["ClassID"])
    material = state["player_state"]["units"][1]
    material.update(ClassID=110, ClassChangeCount=1, Exp=bundle.player_state.experience_for_level(8, 50),
                    SkillLevel=2, Love=100, WatchLoveEv=2, CostModMixValue=-2,
                    SkillExperience=3, CostExperience=1, GeneralPurposeFlags=0x101)
    untouched = deepcopy(state["player_state"]["units"][2])
    state["player_state"]["battle"]["UX"] = 400
    state["player_state"]["last_unique_id"] = 700
    state = bundle.card_status.update(state, [{"CardID": 8, "A1": 7, "A7": 0, "B0": 1234}])
    status = bundle.card_status.read(state)
    assert "gift_state" not in state and "evolution_rewards" not in state
    app, _ = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = exchange(app.state.store, material)
        wire, _ = post(client, envelope)
        saved = app.state.store.profile()
        units = saved.state["player_state"]["units"]
        assert untouched in units and all(row["GetIndex"] != material["GetIndex"] for row in units)
        assert (units[-1]["GetIndex"], units[-1]["UniqueID"]) == (401, 701)
        assert (units[-1]["Exp"], units[-1]["Love"], units[-1]["SkillLevel"], units[-1]["CostModMixValue"]) == (0, 0, 1, 0)
        assert saved.state["gift_state"]["claims"]["8"] == [1, 100]
        assert saved.state["evolution_rewards"]["claimed"] == [17]
        assert not saved.state["tables"]["presents"]
        assert bundle.card_status.read(saved.state) == status
        assert post(client, envelope)[0] == wire and app.state.store.profile() == saved
    reopened, _ = application(bundle, tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        _, listed = post(client, request(reopened.state.store, 41, ()))
        assert listed["A7"].values == tuple(row["GetIndex"] for row in saved.state["player_state"]["units"])
        _, configuration = post(client, request(reopened.state.store, 137, ()))
        assert fields(configuration["ExchangeHistory"])["ExchangeCount"] == (0,)
        assert reopened.state.store.profile() == saved
        reject(client, exchange(reopened.state.store, material, tick=1001), reopened.state.store)
        post(client, exchange(reopened.state.store, untouched, tick=1001))
        assert reopened.state.store.profile().state["trade_history"]["products"]["29"]["lifetime_count"] == 2
        reject(client, envelope, reopened.state.store)


@pytest.mark.parametrize("protection", ["locked", "noncurrent_squad", "wrong_current_family", "not_silver", "prince", "extra_barracks"])
def test_ineligible_materials_are_never_consumed(bundle, tmp_path, protection):
    state, added = seeded(bundle, 7 if protection == "not_silver" else 8)
    material = state["player_state"]["units"][1]
    if protection == "locked":
        material["GeneralPurposeFlags"] |= LOCK_FLAG
    elif protection == "noncurrent_squad":
        state["player_state"]["squads"].append({"Address": 0x03020001, "UnitIndex": material["GetIndex"], "Name": "reserve", "Flags": 0})
    elif protection == "wrong_current_family":
        material["ClassID"] = 600
    elif protection == "prince":
        material = state["player_state"]["units"][0]
    elif protection == "extra_barracks":
        state["tables"]["config"][0][PURCHASE_FIELD] |= 1 << EXTRA_SHIFT
        material["GeneralPurposeFlags"] |= 1 << BARRACKS_SHIFT
    app, _ = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(client, exchange(app.state.store, material), app.state.store,
               status=501 if protection == "extra_barracks" else 409)


def test_crossed_getindex_and_uniqueid_are_not_an_owned_material(bundle, tmp_path):
    state, added = seeded(bundle, count=2)
    crossed = {**added[0], "UniqueID": added[1]["UniqueID"]}
    app, _ = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(client, exchange(app.state.store, crossed), app.state.store)


@pytest.mark.parametrize("quantity", [0, 2])
def test_silver_exchange_does_not_allow_multi_buy_with_one_material(bundle, tmp_path, quantity):
    state, added = seeded(bundle, currency=100)
    app, _ = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(client, exchange(app.state.store, added[0], quantity=quantity), app.state.store, status=400)


def test_nineteen_magic_crystals_are_insufficient(bundle, tmp_path):
    state, added = seeded(bundle, currency=19)
    app, _ = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(client, exchange(app.state.store, added[0]), app.state.store)


def test_original_roster_requires_free_slot_before_material_removal(bundle, tmp_path, monkeypatch):
    state, added = seeded(bundle)
    app, _ = application(bundle, tmp_path, state)
    observed = []
    def no_space(candidate):
        observed.append(tuple(row["GetIndex"] for row in candidate["player_state"]["units"]))
        return 0
    monkeypatch.setattr(bundle.player_state, "capacity_provider", no_space)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(client, exchange(app.state.store, added[0]), app.state.store)
    assert observed and all(added[0]["GetIndex"] in indices for indices in observed)


def test_peak_wire_capacity_is_checked_before_the_client_deletes_material(bundle, tmp_path):
    state, added = seeded(bundle)
    peak, _ = bundle.player_state.credit_units(state, [{"card_id": 1122, "level": 1, "count": 1}])
    final_units = [row for row in peak["player_state"]["units"] if row["GetIndex"] != added[0]["GetIndex"]]
    app, service = application(bundle, tmp_path, state)
    profile = service._profiles[41]
    limit = len(encode_response(profile, bundle.player_state.unit_response(final_units))[0])
    assert len(encode_response(profile, bundle.player_state.unit_response(peak["player_state"]["units"]))[0]) > limit
    service._profiles[41] = replace(profile, response_limit=limit)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(client, exchange(app.state.store, added[0]), app.state.store, status=500)


def test_ordinary_receipt_remains_compatible_when_106_is_enabled(bundle, tmp_path):
    product = next(row for row in bundle.trades.catalog.products
                   if row.ordinary and row.currency_kind == "possession" and row.reset_type == 0
                   and not row.disclosure_quest_ids and not row.buy_condition_item_id
                   and row.start_at is None and row.end_at is None)
    state = funded(bundle, product)
    old_app, old_service = application(bundle, tmp_path, state, silver=False)
    assert 106 not in old_service.handlers()
    with TestClient(old_app, base_url="http://127.0.0.1") as client:
        envelope = request(old_app.state.store, product.registration,
            (Column("SHOP", "I", (product.shop_id,)), Column("NUM", "I", (1,))))
        wire, _ = post(client, envelope)
        saved = old_app.state.store.profile()
    reopened, service = application(bundle, tmp_path, state)
    assert 106 in service.handlers()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert reopened.state.store.profile() == saved
