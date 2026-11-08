from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from datetime import datetime
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.card_status import CARD_STATUS_FIELDS
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.shop_growth import PAYMENT_ITEM
from aigis_ps.storage import Store
from aigis_ps.trade_grants import TradeUnitGrants
from aigis_ps.trade_service import TradeService
import aigis_ps.trade_service as trade_module
from test_quest_service import bundle, request


@pytest.fixture
def case(bundle, tmp_path):
    bootstrap, players, gifts = copy(bundle.bootstrap), copy(bundle.player_state), copy(bundle.gifts)
    gifts.players = players
    rewards = copy(bundle.evolution_rewards)
    rewards.players = players
    clock = SimpleNamespace(now=datetime(2026, 11, 1, 12, tzinfo=JST))
    grants = TradeUnitGrants(players)
    service = TradeService(bootstrap, players, bundle.possessions, bundle.trades.catalog, grants=grants,
        clock=lambda: clock.now, gifts=gifts, evolution_rewards=rewards,
        silver=bundle.trades.silver, growth_shop=bundle.trades.growth_shop)
    bootstrap.exchange_history_provider = service.history_response
    saved = players.initialize(bootstrap.initial_state())
    saved, _ = players.credit_units(saved, [{"card_id": 7, "level": 1, "count": 1},
                                          {"card_id": 8, "level": 1, "count": 1}])
    saved = bundle.possessions.credit(saved, {PAYMENT_ITEM: 100})
    saved["unrelated_progress"] = {"keep": [1, 2]}
    return SimpleNamespace(service=service, players=players, gifts=gifts, rewards=rewards, grants=grants,
        growth=service.growth_shop, clock=clock, store=Store(tmp_path / "shop-growth.sqlite3", service.initialize(saved)))


def message(case, *, columns=None, operation=113, tick=1000):
    if columns is None:
        columns = (Column("SHOP", "I", (2004,)), Column("NUM", "I", (1,)), Column("UnitID", "I", (1,)))
    return request(case.store, operation, columns, tick=tick)


def change(case, action):
    profile = case.store.profile()
    updated = deepcopy(profile.state)
    action(updated)
    case.store.update_state(updated, profile.revision)


def balance(state):
    return next(row["Count"] for row in state["tables"]["possessions"] if row["ItemID"] == PAYMENT_ITEM)


def failure(case, envelope, code):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as error:
        case.service.purchase(envelope, case.store)
    assert error.value.code == code
    assert case.store.profile() == before


@pytest.mark.parametrize("node,quantity,extra,operation,code", [
    (None, 1, (), 113, "invalid_request_fields"),
    (Column("UnitID", "S", ("1",)), 1, (), 113, "invalid_request_fields"),
    (Column("UnitID", "I", ()), 1, (), 113, "invalid_request_fields"),
    (Column("UnitID", "I", (1, 2)), 1, (), 113, "invalid_request_fields"),
    (Column("UnitID", "I", (-1,)), 1, (), 113, "invalid_request_fields"),
    (Column("UnitID", "I", (1,)), 0, (), 113, "invalid_request_fields"),
    (Column("UnitID", "I", (1,)), 2, (), 113, "shop_growth_quantity_invalid"),
    (Column("UnitID", "I", (1,)), 1, (Column("UnitID", "I", (1,)),), 113, "invalid_request_fields"),
    (Column("UnitID", "I", (1,)), 1, (Container("BASE"),), 113, "invalid_request_fields"),
    (Column("UnitID", "I", (1,)), 1, (Column("PRICE", "I", (0,)),), 113, "invalid_request_fields"),
    (Column("UnitID", "I", (1,)), 1, (), 104, "invalid_request_fields"),
])
def test_growth_request_cannot_fall_through_to_another_purchase(case, node, quantity, extra, operation, code):
    columns = (Column("SHOP", "I", (2004,)), Column("NUM", "I", (quantity,)))
    columns += (() if node is None else (node,)) + extra
    failure(case, message(case, columns=columns, operation=operation), code)


def test_ordinary_product_with_unit_id_is_not_a_growth_target(case):
    product = next(row for row in case.service.catalog.products if row.ordinary and row.registration == 113)
    columns = (Column("SHOP", "I", (product.shop_id,)), Column("NUM", "I", (1,)), Column("UnitID", "I", (1,)))
    failure(case, message(case, columns=columns), "shop_growth_product_unsupported")


@pytest.mark.parametrize("operation", [41, 72, 137, 113])
def test_growth_response_preflight_failure_rolls_back_payment_target_and_ledgers(case, monkeypatch, operation):
    encode = trade_module.encode_response
    reached = []
    def reject(profile, payload):
        if profile.registration_keys == (operation,):
            reached.append(operation)
            raise ProtocolError("fixture_preflight", "Response cannot fit", 503)
        return encode(profile, payload)
    monkeypatch.setattr(trade_module, "encode_response", reject)
    failure(case, message(case), "fixture_preflight")
    assert reached == [operation]


@pytest.mark.parametrize("helper", ["preserve_history", "record_current_thresholds", "evolution"])
def test_reward_history_failure_does_not_spend_or_awaken(case, monkeypatch, helper):
    def reject(*_):
        raise ProtocolError("fixture_history", "Reward history is unavailable", 503)
    if helper == "evolution":
        monkeypatch.setattr(case.rewards, "initialize", reject)
    else:
        monkeypatch.setattr(case.gifts, helper, reject)
    failure(case, message(case), "fixture_history")


@pytest.mark.parametrize("patch", [{"UniqueID": 999}, {"ClassChangeCount": 1}, {"CardID": 8}])
def test_invalid_growth_patch_cannot_reassign_or_overwrite_the_target(case, monkeypatch, patch):
    original = case.growth.values
    def wrong(*args, **kwargs):
        return {**original(*args, **kwargs), **patch}
    monkeypatch.setattr(case.growth, "values", wrong)
    failure(case, message(case), "shop_growth_result_invalid")


def test_concurrent_same_growth_request_charges_and_updates_one_time(case, monkeypatch):
    update, gate, lock = case.store.update_state, Barrier(2), Lock()
    calls = []
    def concurrent(candidate, revision):
        with lock:
            calls.append(revision)
            wait = len(calls) <= 2
        if wait:
            gate.wait(timeout=10)
        return update(candidate, revision)
    monkeypatch.setattr(case.store, "update_state", concurrent)
    before = case.store.profile()
    envelope = message(case)
    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(lambda _: case.service.purchase(envelope, case.store), range(2)))
    after = case.store.profile()
    assert responses[0] == responses[1] and after.revision == before.revision + 1
    assert balance(after.state) == 98
    assert after.state["player_state"]["units"][1]["ClassChangeCount"] == 2
    assert after.state["player_state"]["units"][2] == before.state["player_state"]["units"][2]
    assert after.state["trade_history"]["products"]["2004"]["lifetime_count"] == 1
    assert len(after.state["trade_receipts"]["receipts"]) == 1
    nodes = {node.name: node for node in loads(responses[0]).children}
    assert "PBP" not in nodes and nodes["TrustReward"].children[0].values == ()
    failure(case, message(case, tick=1001), "shop_growth_target_ineligible")


@pytest.mark.parametrize("reserve_fails", [False, True])
def test_reservation_trial_is_isolated_and_never_uses_the_grant_path(case, monkeypatch, reserve_fails):
    reservation = [{"card_id": 3, "level": 1, "count": 1}]
    change(case, lambda state: state.update(quest_state={"active": {"unit_reservations": reservation,
                                                                  "unchanged_marker": "reserved"}}))
    credit, seen = case.players.credit_units, []
    def trial(state, grants):
        assert state["quest_state"]["active"] is None
        assert state["player_state"]["units"][1]["ClassChangeCount"] == 2
        assert grants == reservation
        seen.append(True)
        if reserve_fails:
            raise ProtocolError("fixture_reservation", "Reserved result does not fit", 409)
        return credit(state, grants)
    monkeypatch.setattr(case.players, "credit_units", trial)
    monkeypatch.setattr(case.grants, "grant", lambda *_: pytest.fail("Growth must not allocate through the grant adapter"))
    before = case.store.profile()
    if reserve_fails:
        failure(case, message(case), "fixture_reservation")
    else:
        case.service.purchase(message(case), case.store)
        after = case.store.profile().state
        assert after["quest_state"] == before.state["quest_state"]
        assert len(after["player_state"]["units"]) == len(before.state["player_state"]["units"])
        assert after["player_state"]["battle"] == before.state["player_state"]["battle"]
        assert after["player_state"].get("last_unique_id") == before.state["player_state"].get("last_unique_id")
    assert seen == [True]


def test_card80_keeps_old_claims_and_identity_without_issuing_rewards(case, monkeypatch):
    def legacy(state):
        target = state["player_state"]["units"][1]
        target.update(CardID=80, ClassID=700, ClassChangeCount=0, Exp=0, Love=0, SettingSkillID=47)
        status = {field: 0 for field in CARD_STATUS_FIELDS}
        status.update(CardID=80, A7=3, B3=100)
        state["tables"]["card_status"] = [status]
        state["card_status_schema"] = 1
    change(case, legacy)
    before = case.store.profile()
    monkeypatch.setattr(case.grants, "grant", lambda *_: pytest.fail("Growth must not grant another unit"))
    monkeypatch.setattr(case.gifts.presents, "enqueue", lambda *_: pytest.fail("Growth must not send trust rewards"))
    columns = (Column("SHOP", "I", (2000,)), Column("NUM", "I", (1,)), Column("UnitID", "I", (1,)))
    response = {node.name: node for node in loads(case.service.purchase(message(case, columns=columns), case.store)).children}
    after = case.store.profile().state
    target = after["player_state"]["units"][1]
    assert (target["CardID"], target["ClassID"], target["SettingSkillID"]) == (99, 730, 72)
    assert (target["GetIndex"], target["UniqueID"]) == (1, 2)
    assert target["WatchLoveEv"] == before.state["player_state"]["units"][1]["WatchLoveEv"]
    assert set(after["gift_state"]["claims"]["80"]) >= {1, 100}
    assert set(after["gift_state"]["claims"]["99"]) >= {1, 100}
    assert 80 in after["evolution_rewards"]["claimed"]
    assert after["tables"]["card_status"] == before.state["tables"]["card_status"]
    assert after["tables"]["presents"] == before.state["tables"]["presents"]
    assert after["tables"]["crystals"] == before.state["tables"]["crystals"]
    assert "PBP" not in response and "PPT" not in response and "CardStatus" not in response


def test_ordinary_and_silver_content_identity_do_not_depend_on_growth_shop(case):
    plain = TradeService(case.service.bootstrap, case.players, case.service.possessions, case.service.catalog,
        grants=case.grants, rules=case.service.rules, silver=case.service.silver, gifts=case.gifts,
        evolution_rewards=case.rewards, clock=lambda: case.clock.now)
    assert plain._content_version() == case.service._content_version()
    assert plain._content_version(113) == case.service._content_version(113)
    assert plain._content_version(106) == case.service._content_version(106)
    assert case.service._content_version(113, grow=True) != case.service._content_version(113)
