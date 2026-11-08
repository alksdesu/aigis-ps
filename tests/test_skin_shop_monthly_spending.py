from copy import copy, deepcopy
from datetime import datetime

import pytest

from aigis_ps import crystal_spending as spending
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
import aigis_ps.skin_shop_service as module
from test_quest_service import bundle
from test_skin_shop_service import case, catalog, change, message, nodes


@pytest.fixture(autouse=True)
def baseline(case):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    state.pop("crystal_spending", None)
    state["tables"]["game_param2"][0]["StampCardGrade"] = 0
    state["tables"]["crystals"] = [{"FreeFlag": True, "Date": 10, "Num": 5},
                                   {"FreeFlag": False, "Date": 3, "Num": 100}]
    now = case.service.bootstrap.clock()
    case.store.update_state(spending.initialize_spending(state, now, origin="new_local"), profile.revision)


def test_purchase_counts_both_wallet_kinds_without_unregistered_pgp2(case):
    before = case.store.profile()
    result = nodes(case.service.purchase(message(case), case.store))
    after = case.store.profile()
    assert spending.read_spending(after.state)["current_spent"] == case.product.price == 15
    assert wallet_totals(after.state) == {"paid": 90, "free": 0, "total": 90}
    assert wallet_totals(before.state)["total"] - wallet_totals(after.state)["total"] == 15
    assert "PGP2" not in result
    assert after.state["tables"]["game_param2"][0]["StampCardGrade"] == 15


def test_distinct_skin_purchases_accumulate_once(case):
    case.service.purchase(message(case, case.products[0], tick=1000), case.store)
    case.service.purchase(message(case, case.products[1], tick=1001), case.store)
    assert spending.read_spending(case.store.profile().state)["current_spent"] == 30


def test_month_rollover_is_atomic_with_skin_and_mission_hook(case):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    state.pop("crystal_spending")
    state["tables"]["game_param2"][0]["StampCardGrade"] = 0
    state = spending.spend_crystals(state, 2, datetime(2026, 11, 1, 3, 59, 59, tzinfo=JST))
    case.store.update_state(state, profile.revision)
    case.service.bootstrap.clock = lambda: datetime(2026, 11, 1, 4, tzinfo=JST)
    case.service.purchase(message(case), case.store)
    ledger = spending.read_spending(case.store.profile().state)
    assert (ledger["period"], ledger["previous_spent"], ledger["current_spent"]) == ("2026-11", 2, 15)
    assert case.product.item_id in case.store.profile().state["skin_acquisition_history"]["seen"]


def test_retry_does_not_read_any_clock_or_recount(case, monkeypatch):
    envelope = message(case)
    result = case.service.purchase(envelope, case.store)
    before = case.store.profile()
    monkeypatch.setattr(case.service.bootstrap, "clock", lambda: pytest.fail("Retry read the purchase clock"))
    monkeypatch.setattr(case.bundle.trades, "clock", lambda: pytest.fail("Retry read the trade provider clock"))
    assert case.service.purchase(envelope, case.store) == result
    assert case.store.profile() == before


def test_new_purchase_reuses_captured_time_in_exchange_history_preflight(case, monkeypatch):
    calls = []
    when = case.service.bootstrap.clock()
    def clock():
        calls.append(when)
        return when
    monkeypatch.setattr(case.service.bootstrap, "clock", clock)
    case.service.bootstrap.exchange_history_provider = case.bundle.trades.history_response
    monkeypatch.setattr(case.bundle.trades, "clock", lambda: pytest.fail("Preflight read another clock"))
    case.service.purchase(message(case), case.store)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["credit", "35", "response", "hook"])
def test_failure_keeps_spending_wallet_skin_and_flags_unchanged(case, monkeypatch, failure):
    def reject(*args, **kwargs):
        raise ProtocolError("injected_failure", "Injected transaction failure", 503)
    if failure == "credit":
        possessions = copy(case.service.possessions)
        monkeypatch.setattr(possessions, "credit", reject)
        case.service.possessions = possessions
    elif failure == "response":
        monkeypatch.setattr(case.service, "_response", reject)
    elif failure == "hook":
        missions = copy(case.service.missions)
        def hook(before, updated, now):
            altered = deepcopy(updated)
            altered["crystal_spending"]["current_spent"] = 0
            return altered
        monkeypatch.setattr(missions, "record_acquisitions", hook)
        case.service.missions = missions
    else:
        original = module.encode_response
        def encode(profile, *args, **kwargs):
            if profile.registration_keys == (35,):
                reject()
            return original(profile, *args, **kwargs)
        monkeypatch.setattr(module, "encode_response", encode)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.purchase(message(case), case.store)
    assert caught.value.code == ("skin_shop_hook_invalid" if failure == "hook" else "injected_failure")
    assert case.store.profile() == before


def test_cas_retry_uses_one_time_and_one_charge(case, monkeypatch):
    original = case.store.update_state
    calls, clocks = [], []
    when = case.service.bootstrap.clock()
    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            raise ValueError("Concurrent update")
        return original(state, revision)
    def clock():
        clocks.append(when)
        return when
    monkeypatch.setattr(case.store, "update_state", update)
    monkeypatch.setattr(case.service.bootstrap, "clock", clock)
    case.service.purchase(message(case), case.store)
    assert len(calls) == 2 and len(clocks) == 1
    assert spending.read_spending(case.store.profile().state)["current_spent"] == 15


def test_reopened_new_policy_rejects_old_receipt_without_charging_again(case, monkeypatch):
    envelope = message(case)
    case.service.purchase(envelope, case.store)
    before = case.store.profile()
    monkeypatch.setattr(spending, "POLICY_VERSION", spending.POLICY_VERSION + 1)
    restarted = copy(case.service)
    restarted.content_version = restarted._content()
    monkeypatch.setattr(restarted.bootstrap, "clock", lambda: pytest.fail("Stale retry read the clock"))
    with pytest.raises(ProtocolError) as caught:
        restarted.purchase(envelope, case.store)
    assert caught.value.code == "skin_shop_retry_stale"
    assert case.store.profile() == before


def test_legacy_nonzero_projection_needs_explicit_baseline(case):
    def legacy(state):
        state.pop("crystal_spending")
        state["tables"]["game_param2"][0]["StampCardGrade"] = 50
    change(case, legacy)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.purchase(message(case), case.store)
    assert caught.value.code == "crystal_spending_baseline_required"
    assert case.store.profile() == before
