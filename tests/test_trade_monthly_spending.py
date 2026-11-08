from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps import crystal_spending as spending
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
import aigis_ps.trade_service as module
from test_quest_service import bundle, request
from test_trade_catalog import native_catalog
from test_trade_service import case, message, mutate, nodes


@pytest.fixture(autouse=True)
def baseline(case):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    state.pop("crystal_spending", None)
    state["tables"]["game_param2"][0]["StampCardGrade"] = 0
    case.store.update_state(spending.initialize_spending(state, case.clock.now, origin="new_local"), profile.revision)


def test_crystal_trade_counts_paid_and_free_and_uses_column_response(case):
    before = case.store.profile()
    response = nodes(case.service.purchase(message(case, 100007), case.store))
    after = case.store.profile()
    assert spending.read_spending(after.state)["current_spent"] == 10
    assert wallet_totals(before.state)["total"] - wallet_totals(after.state)["total"] == 10
    assert wallet_totals(after.state)["free"] == 0
    assert wallet_totals(after.state)["paid"] == 95
    assert response["PGP2"] == Container("PGP2", (Column("C1", "I", (10,)),))
    full = loads(case.service.bootstrap.game_param2(request(case.store, 35, ()), case.store))
    assert next(column for column in full.children if column.name == "C1").values == (10,)
    updates = nodes(case.service.bootstrap.game_param2_fields(request(case.store, 33, ()), case.store))["PGP2"]
    keys, values = (column.values for column in updates.children)
    assert dict(zip(keys, values))["C1"] == "10"


@pytest.mark.parametrize("shop", [100001, 100002, 100003, 100004, 100005, 100006, 100008])
def test_other_currencies_do_not_settle_or_increment_spending(case, shop):
    before = case.store.profile()
    ledger = deepcopy(before.state["crystal_spending"])
    case.clock.now += timedelta(days=32)
    response = nodes(case.service.purchase(message(case, shop), case.store))
    assert case.store.profile().state["crystal_spending"] == ledger
    assert "PGP2" not in response


def test_jst_four_am_rollover_keeps_previous_spend_and_adds_this_purchase(case):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    state.pop("crystal_spending")
    state["tables"]["game_param2"][0]["StampCardGrade"] = 0
    state = spending.spend_crystals(state, 3, datetime(2026, 11, 1, 3, 59, 59, tzinfo=JST))
    case.store.update_state(state, profile.revision)
    case.clock.now = datetime(2026, 11, 1, 4, tzinfo=JST)
    response = nodes(case.service.purchase(message(case, 100007), case.store))
    ledger = spending.read_spending(case.store.profile().state)
    assert (ledger["period"], ledger["previous_spent"], ledger["current_spent"]) == ("2026-11", 3, 10)
    assert response["PGP2"] == Container("PGP2", (Column("C1", "I", ((3 << 16) | 10,)),))


def test_repeated_new_purchases_accumulate_once_each(case):
    case.service.purchase(message(case, 100007, tick=1000), case.store)
    case.service.purchase(message(case, 100007, tick=1001), case.store)
    assert spending.read_spending(case.store.profile().state)["current_spent"] == 20


@pytest.mark.parametrize("failure", ["grant", "35", "33", "response"])
def test_failures_do_not_commit_payment_counter_or_goods(case, monkeypatch, failure):
    def reject(*args, **kwargs):
        raise ProtocolError("injected_failure", "Injected transaction failure", 503)
    if failure == "grant":
        monkeypatch.setattr(case.service, "_grant", reject)
    elif failure == "response":
        monkeypatch.setattr(case.service, "_response", reject)
    else:
        original = module.encode_response
        def encode(profile, *args, **kwargs):
            if profile.registration_keys == (int(failure),):
                reject()
            return original(profile, *args, **kwargs)
        monkeypatch.setattr(module, "encode_response", encode)
    before = case.store.profile()
    with pytest.raises(ProtocolError, match="Injected"):
        case.service.purchase(message(case, 100007), case.store)
    assert case.store.profile() == before


def test_exact_retry_does_not_read_clock_or_roll_into_next_month(case, monkeypatch):
    envelope = message(case, 100007)
    response = case.service.purchase(envelope, case.store)
    before = case.store.profile()
    monkeypatch.setattr(case.service, "clock", lambda: pytest.fail("Cached trade read the clock"))
    assert case.service.purchase(envelope, case.store) == response
    assert case.store.profile() == before


@pytest.mark.parametrize("group", ["PGP2", "PPC", "PFC", "key_values"])
def test_cached_spending_and_wallet_values_must_match_current_state(case, group):
    envelope = message(case, 100007)
    case.service.purchase(envelope, case.store)
    profile = case.store.profile()
    def corrupt(state):
        entry = state["trade_receipts"]["receipts"][-1]
        document = loads(entry["response"].encode())
        children = []
        for child in document.children:
            if group == "key_values" and child.name == "PGP2":
                child = Container("PGP2", (Column("A1", "S", ("C1",)), Column("A2", "S", ("10",))))
            elif child.name == group:
                columns = list(child.children)
                column = columns[-1]
                columns[-1] = replace(column, values=tuple(value + 1 for value in column.values))
                child = replace(child, children=tuple(columns))
            children.append(child)
        entry["response"] = dumps(Container(children=tuple(children))).decode()
        entry["revision"] = profile.revision + 1
    mutate(case, corrupt)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.purchase(envelope, case.store)
    assert caught.value.code == "trade_receipts_invalid"
    assert case.store.profile() == before


def test_spending_policy_changes_only_crystal_content_and_stales_its_receipt(case, monkeypatch):
    envelope = message(case, 100007)
    case.service.purchase(envelope, case.store)
    real = case.bundle.trades
    ordinary = {operation: real._content_version(operation) for operation in (None, 104, 105, 106, 107, 109, 110, 111, 113)}
    growth = real._content_version(113, grow=True)
    crystal = real._content_version(112)
    monkeypatch.setattr(spending, "POLICY_VERSION", spending.POLICY_VERSION + 1)
    assert real._content_version(112) != crystal
    assert {operation: real._content_version(operation) for operation in ordinary} == ordinary
    assert real._content_version(113, grow=True) == growth
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.purchase(envelope, case.store)
    assert caught.value.code == "trade_retry_stale"
    assert case.store.profile() == before


def test_noncrystal_receipt_survives_spending_policy_change(case, monkeypatch):
    envelope = message(case, 100003)
    response = case.service.purchase(envelope, case.store)
    before = case.store.profile()
    monkeypatch.setattr(spending, "POLICY_VERSION", spending.POLICY_VERSION + 1)
    monkeypatch.setattr(case.service, "clock", lambda: pytest.fail("Cached trade read the clock"))
    assert case.service.purchase(envelope, case.store) == response
    assert case.store.profile() == before


def test_cas_retry_reuses_one_instant_and_commits_one_charge(case, monkeypatch):
    original = case.store.update_state
    calls, clocks = [], []
    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            raise ValueError("Concurrent update")
        return original(state, revision)
    def now():
        clocks.append(case.clock.now)
        return case.clock.now
    monkeypatch.setattr(case.store, "update_state", update)
    monkeypatch.setattr(case.service, "clock", now)
    case.service.purchase(message(case, 100007), case.store)
    assert len(calls) == 2 and len(clocks) == 1
    assert spending.read_spending(case.store.profile().state)["current_spent"] == 10


def test_nonzero_legacy_counter_without_baseline_is_not_overwritten(case):
    def legacy(state):
        state.pop("crystal_spending")
        state["tables"]["game_param2"][0]["StampCardGrade"] = 12
    mutate(case, legacy)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.purchase(message(case, 100007), case.store)
    assert caught.value.code == "crystal_spending_baseline_required"
    assert case.store.profile() == before


def test_unconverted_event_flags_follow_the_shared_33_projection(case):
    mutate(case, lambda state: state["tables"]["game_param2"][0].update(_EvCmb=0))
    envelope = message(case, 100007)
    response = case.service.purchase(envelope, case.store)
    before = case.store.profile()
    assert before.state["tables"]["game_param2"][0]["_EvCmb"] == 0
    assert spending.read_spending(before.state)["current_spent"] == 10
    assert case.service.purchase(envelope, case.store) == response
    assert case.store.profile() == before
    projection = nodes(case.service.bootstrap.game_param2_fields_response(before))["PGP2"]
    keys, values = (column.values for column in projection.children)
    assert dict(zip(keys, values))["C1"] == "10"
