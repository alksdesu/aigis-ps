from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.energy_clock import JST
from aigis_ps.evolution_catalog import EvolutionRules
from aigis_ps.player_state import UNIT_FIELDS
from aigis_ps.shop_growth import ShopGrowthCatalog
from aigis_ps.trade_grants import TradeUnitGrants
from aigis_ps.trade_history import TradeRules
from aigis_ps.trade_service import TradeService
from aigis_ps.training_math import experience_threshold
from test_evolution_integration import post
from test_quest_service import bundle, request
from test_trade_integration import funded


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
CURRENCY = "trade_item_005000"
TARGETS = ((7, 3, 2004, 2), (18, 4, 2001, 4), (17, 5, 2000, 6),
           (168, 7, 2003, 4), (904, 10, 2001, 4), (903, 11, 2000, 6))


def application(bundle, path, state, *, growth=True, policy=None):
    bootstrap = copy(bundle.bootstrap)
    catalog = bundle.trades.growth_shop
    assert catalog is not None, bundle.trades.growth_shop_error
    if policy is not None:
        catalog = ShopGrowthCatalog(bundle.trades.catalog, bundle.training.catalog,
            lambda name: bundle.native_cdn.assets.read(name)[0], rules=EvolutionRules(first_awake_experience=policy))
    service = TradeService(bootstrap, bundle.player_state, bundle.possessions, bundle.trades.catalog,
        grants=TradeUnitGrants(bundle.player_state), rules=TradeRules("never", "never"), clock=lambda: NOW,
        **({"growth_shop": catalog, "gifts": bundle.gifts,
            "evolution_rewards": bundle.evolution_rewards} if growth else {}))
    bootstrap.exchange_history_provider = service.history_response
    app = create_app(path, initial_state=service.initialize(state), state_initializer=service.initialize,
        handlers=bootstrap.handlers() | bundle.player_state.handlers() | service.handlers())
    return app, service


def seeded(bundle, card_id=7, *, amount=20, stage=0):
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state, _ = bundle.player_state.credit_units(state, [{"card_id": card_id, "level": 1, "count": 1},
                                                      {"card_id": 8, "level": 1, "count": 1}])
    target = state["player_state"]["units"][1]
    card = bundle.gifts.catalog.card(card_id)
    if card_id in (903, 904):
        assert target["ClassChangeCount"] == 2
    target.update(ClassID=710 if card_id == 80 and stage == 1 else card["InitClassID"],
                  ClassChangeCount=stage, Exp=0, Love=0, SettingSkillID=card["ClassLV0SkillID"])
    state["tables"]["game_param"][0]["Money"] = 1000
    if amount:
        state = bundle.possessions.credit(state, {CURRENCY: amount})
    return state, deepcopy(target)


def buy(store, target, *, shop=2004, quantity=1, tick=1000):
    return request(store, 113, (Column("SHOP", "I", (shop,)), Column("NUM", "I", (quantity,)),
                                Column("UnitID", "I", (target["GetIndex"],))), tick=tick)


def fields(node):
    return {column.name: column.values for column in node.children}


def assert_rollback(client, envelope, store, status=409):
    before = store.profile()
    response = post(client, envelope, status)
    assert response.json()["error"]["code"] != "internal_error"
    assert store.profile() == before
    return response


@pytest.mark.parametrize("card_id,rarity,shop,price", TARGETS)
def test_all_four_products_and_six_rarities_update_only_the_low_level_target(bundle, tmp_path, card_id, rarity, shop, price):
    state, target = seeded(bundle, card_id, amount=price)
    assert bundle.gifts.catalog.card(card_id)["Rare"] == rarity
    assert target["Exp"] == target["Love"] == target["ClassChangeCount"] == 0
    app, _ = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        _, response = post(client, buy(app.state.store, target, shop=shop))
        saved = app.state.store.profile()
        player = saved.state["player_state"]
        after = player["units"][1]
        card = bundle.gifts.catalog.card(card_id)
        expected_class = bundle.training.catalog._classes[card["InitClassID"]]["JobChange"]
        assert (after["CardID"], after["ClassID"], after["ClassChangeCount"], after["SettingSkillID"]) == (
            card_id, expected_class, 2, card["ClassLV1SkillID"])
        assert after["Love"] == 100 and after["Exp"] == 0
        assert saved.revision == before.revision + 1
        assert player["units"][0] == before.state["player_state"]["units"][0]
        assert player["units"][2:] == before.state["player_state"]["units"][2:]
        assert {key: value for key, value in player.items() if key != "units"} == {
            key: value for key, value in before.state["player_state"].items() if key != "units"}
        assert set(response) == {"SYSTEM", "ExchangeHistory", "POSSESSION", "PPU", "TrustReward"}
        assert fields(response["PPU"]) == {alias: (after[name],) for name, alias in UNIT_FIELDS}
        assert fields(response["POSSESSION"]) == {"ItemID": (CURRENCY,), "Count": (0,), "Enabled": (0,)}
        assert fields(response["TrustReward"]) == {"ID": ()}
        assert saved.state["tables"]["game_param"] == before.state["tables"]["game_param"]
        assert saved.state["tables"]["crystals"] == before.state["tables"]["crystals"]
        assert saved.state["trade_history"]["products"][str(shop)]["lifetime_count"] == 1
        assert not saved.state["tables"]["presents"]
        assert saved.state["gift_state"]["claims"][str(card_id)] == [1, 100]


def test_lock_squads_history_and_highwater_survive_restart_and_exact_retry(bundle, tmp_path):
    state, _ = seeded(bundle, 7, amount=4)
    target = state["player_state"]["units"][1]
    target.update(GeneralPurposeFlags=0x103, WatchLoveEv=2, ClassChangeStatusMod=7,
                  CostModMixValue=-1, SkillLevel=2, SkillExperience=3, CostExperience=1,
                  SkillEvolved=1, _DisplayType=1)
    state["player_state"]["battle"]["UX"] = 400
    state["player_state"]["last_unique_id"] = 700
    state["player_state"]["squads"].append({"Address": 0x03020001, "UnitIndex": target["GetIndex"],
                                           "Name": "reserve", "Flags": 0})
    state = bundle.card_status.update(state, [{"CardID": 7, "A1": 7, "A7": 0, "B0": 1234}])
    original_status = bundle.card_status.read(state)
    app, _ = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = buy(app.state.store, target)
        wire, _ = post(client, envelope)
        saved = app.state.store.profile()
        after = saved.state["player_state"]["units"][1]
        for name in ("GeneralPurposeFlags", "WatchLoveEv", "ClassChangeStatusMod", "UniqueID", "GetIndex",
                     "SkillLevel", "SkillExperience", "CostExperience", "SkillEvolved", "OrganizationIndex2"):
            assert after[name] == target[name]
        assert saved.state["player_state"]["squads"] == state["player_state"]["squads"]
        assert saved.state["player_state"]["battle"]["UX"] == 400
        assert saved.state["player_state"]["last_unique_id"] == 700
        assert bundle.card_status.read(saved.state) == original_status
        assert post(client, envelope)[0] == wire and app.state.store.profile() == saved
        _, squads = post(client, request(app.state.store, 72, ()))
        assert set(squads) == {"SYSTEM", "PBP", "TGI"}
        assert target["GetIndex"] in fields(squads["TGI"])["A1"]
    reopened, _ = application(bundle, tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert reopened.state.store.profile() == saved
        _, roster = post(client, request(reopened.state.store, 41, ()))
        assert roster["AD"].values[1] == after["SettingSkillID"]
        assert_rollback(client, buy(reopened.state.store, target, tick=1001), reopened.state.store)


@pytest.mark.parametrize("stage,old_class,old_cap", [(0, 700, 30), (1, 710, 80)])
@pytest.mark.parametrize("policy", ["overflow", "total", "reset"])
def test_card80_uses_new_card_skill_and_old_class_experience_cap(bundle, tmp_path, stage, old_class, old_cap, policy):
    state, _ = seeded(bundle, 80, stage=stage, amount=6)
    target = state["player_state"]["units"][1]
    assert target["ClassID"] == old_class and target["SettingSkillID"] == 47
    growth = bundle.training.catalog
    assert min(growth._classes[old_class]["MaxLevel"], growth._rarities[6][f"LevelCap{stage:02}"]) == old_cap
    cap_exp = experience_threshold(growth._experience[old_cap - 1], growth._rarities[6]["RequireExpRatio"])
    target.update(Exp=cap_exp + 123, CostModMixValue=-3, Love=120, SkillLevel=4, WatchLoveEv=2)
    app, _ = application(bundle, tmp_path, state, policy=policy)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, response = post(client, buy(app.state.store, target, shop=2000))
        saved = app.state.store.profile().state
        after = saved["player_state"]["units"][1]
        assert (after["CardID"], after["ClassID"], after["ClassChangeCount"], after["SettingSkillID"],
                after["CostModMixValue"]) == (99, 730, 2, 72, 0)
        assert after["Exp"] == {"overflow": 123, "total": cap_exp + 123, "reset": 0}[policy]
        assert (after["Love"], after["SkillLevel"], after["WatchLoveEv"]) == (120, 4, 2)
        assert after["GetIndex"] == target["GetIndex"] and after["UniqueID"] == target["UniqueID"]
        assert fields(response["PPU"])["AD"] == (72,)
        assert saved["gift_state"]["claims"]["99"] == [1, 100]
        assert not saved["tables"]["presents"]


@pytest.mark.parametrize("kind", ["missing_target", "wrong_rarity", "prince", "already_awakened", "insufficient"])
def test_failed_growth_does_not_charge_or_change_any_state(bundle, tmp_path, kind):
    state, target = seeded(bundle, amount=1 if kind == "insufficient" else 20)
    if kind == "already_awakened":
        row = state["player_state"]["units"][1]
        row.update(ClassChangeCount=2, ClassID=bundle.training.catalog._classes[row["ClassID"]]["JobChange"])
    elif kind == "missing_target":
        target["GetIndex"] = 100
    elif kind == "prince":
        target = state["player_state"]["units"][0]
    app, _ = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert_rollback(client, buy(app.state.store, target, shop=2000 if kind == "wrong_rarity" else 2004), app.state.store)


@pytest.mark.parametrize("extra", ["missing_unit", "duplicate_unit", "base", "client_money", "count_two"])
def test_growth_request_has_only_shop_num_and_unitid(bundle, tmp_path, extra):
    state, target = seeded(bundle)
    columns = [Column("SHOP", "I", (2004,)), Column("NUM", "I", (2 if extra == "count_two" else 1,))]
    if extra != "missing_unit":
        columns.append(Column("UnitID", "I", (target["GetIndex"],)))
    if extra == "duplicate_unit":
        columns.append(Column("UnitID", "I", (target["GetIndex"],)))
    elif extra == "base":
        columns.append(Container("BASE", (Column("A7", "I", (target["GetIndex"],)),)))
    elif extra == "client_money":
        columns.append(Column("Money", "I", (0,)))
    app, _ = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert_rollback(client, request(app.state.store, 113, columns), app.state.store, 400)


def test_ordinary_113_receipt_and_content_do_not_change_when_growth_is_enabled(bundle, tmp_path):
    product = next(row for row in bundle.trades.catalog.products if row.ordinary and row.registration == 113
        and row.product_type == 3 and row.reset_type == 0 and not row.disclosure_quest_ids
        and not row.buy_condition_item_id and row.start_at is None and row.end_at is None)
    state = funded(bundle, product)
    old_app, old_service = application(bundle, tmp_path, state, growth=False)
    old_content = old_service._content_version()
    with TestClient(old_app, base_url="http://127.0.0.1") as client:
        envelope = request(old_app.state.store, 113, (Column("SHOP", "I", (product.shop_id,)), Column("NUM", "I", (1,))))
        wire, response = post(client, envelope)
        saved = old_app.state.store.profile()
        assert "TrustReward" not in response
    reopened, enabled = application(bundle, tmp_path, state)
    assert enabled._content_version() == old_content
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert reopened.state.store.profile() == saved


def test_current_response_capacity_failure_rolls_back_growth(bundle, tmp_path, monkeypatch):
    import aigis_ps.trade_service as module

    state, target = seeded(bundle)
    app, _ = application(bundle, tmp_path, state)
    original = module.encode_response

    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = buy(app.state.store, target)

        def limited(profile, xml):
            return original(replace(profile, response_limit=1), xml) if profile.route == envelope.profile.route else original(profile, xml)

        monkeypatch.setattr(module, "encode_response", limited)
        assert_rollback(client, envelope, app.state.store, 500)


@pytest.mark.parametrize("registration", [41, 72, 137])
def test_required_readback_capacity_is_checked_before_growth_commits(bundle, tmp_path, registration):
    state, target = seeded(bundle)
    app, service = application(bundle, tmp_path, state)
    service._profiles[registration] = replace(service._profiles[registration], response_limit=1)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert_rollback(client, buy(app.state.store, target), app.state.store, 500)


def test_cas_retry_keeps_concurrent_unrelated_state_without_double_charge(bundle, tmp_path, monkeypatch):
    state, target = seeded(bundle, amount=4)
    app, _ = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        store = app.state.store
        original = store.update_state
        attempts = []

        def collide(candidate, revision):
            attempts.append(revision)
            if len(attempts) == 1:
                other = deepcopy(store.profile().state)
                other["concurrent_note"] = "preserved"
                original(other, revision)
                raise ValueError("Concurrent update")
            return original(candidate, revision)

        monkeypatch.setattr(store, "update_state", collide)
        post(client, buy(store, target))
        saved = store.profile().state
        assert len(attempts) == 2
        assert saved["concurrent_note"] == "preserved"
        assert next(row["Count"] for row in saved["tables"]["possessions"] if row["ItemID"] == CURRENCY) == 2
        assert saved["trade_history"]["products"]["2004"]["lifetime_count"] == 1
