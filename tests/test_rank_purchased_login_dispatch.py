from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from threading import Barrier, Lock

import pytest

from aigis_ps.alxml import Column, Container
from aigis_ps.errors import ProtocolError
from aigis_ps.event_login_catalog import EventLoginCatalog
from aigis_ps.package_text_overlay import IMPLANT_RESOURCE, SYSTEM_TEXT_RESOURCE
from aigis_ps.purchased_login_catalog import PurchasedLoginCatalog
from aigis_ps.purchased_login_rules import PurchasedLoginRules
from aigis_ps.purchased_login_service import PurchasedLoginService
from aigis_ps.purchased_login_state import read_purchased_logins
import aigis_ps.rank_rewards as module
from aigis_ps.rank_rewards import RankRewardService
from aigis_ps.purchased_login_texts import build_purchased_login_texts
from aigis_ps.storage import Store
from test_rank_comeback_dispatch import (
    baseline, bundle, comeback_dispatch, dispatch, event_dispatch, factory, fail, nodes, overwrite, saved_marker,
)
from test_rank_rewards import message


@pytest.fixture(scope="module")
def purchased_catalog(bundle):
    def reader(name):
        return bundle.native_cdn.assets.read(name)[0]
    source = EventLoginCatalog(reader)
    rules = PurchasedLoginRules(enabled=True, campaign_ids=(22,))
    texts = build_purchased_login_texts(reader(IMPLANT_RESOURCE), reader(SYSTEM_TEXT_RESOURCE), source, rules)
    def effective(name):
        return texts.replacements[name] if name in texts.replacements else reader(name)
    return PurchasedLoginCatalog(source, effective, rules)


@pytest.fixture
def purchased_dispatch(comeback_dispatch, purchased_catalog):
    def make(*, passes=True, rank=True, events=True, comeback=True, purchased=True, acquire=True, purchase_today=False):
        case = comeback_dispatch(passes=passes, rank=rank, events=events, comeback=comeback, seed=comeback)
        case.purchased = PurchasedLoginService(purchased_catalog, case.presents)
        case.offer = purchased_catalog.get("local_login_22")
        profile = case.store.profile()
        state = deepcopy(profile.state)
        state.pop("purchased_login", None)
        if acquire:
            bought_at = case.clock.now if purchase_today else case.clock.now - timedelta(days=1)
            state, case.purchase = case.purchased.acquire(state, case.offer, "a" * 64, bought_at)
        case.store.update_state(state, profile.revision)
        case.rank = RankRewardService(case.bootstrap, case.bundle.player_state, case.presents, case.rank_catalog,
            event_passes=case.service if passes else None, event_logins=case.events if events else None,
            comeback_logins=case.comeback if comeback else None, purchased_logins=case.purchased if purchased else None)
        return case
    return make


def test_five_sources_commit_once_and_merge_three_login_notices(purchased_dispatch):
    case = purchased_dispatch()
    before = case.store.profile()
    result = nodes(case.rank.receive(message(case.store), case.store))
    saved, marker = case.store.profile(), saved_marker(case)
    assert saved.revision == before.revision + 1 and marker["schema_version"] == 5
    assert marker["rank_mail_ids"] == [1] and marker["event_pass_mail_ids"] == {case.entry.item_id: [2, 3]}
    assert marker["event_login_grants"][0]["mail_ids"] == [4]
    assert marker["comeback_login_grants"][0]["mail_ids"] == [5]
    grant, = marker["purchased_login_grants"]
    assert (grant["campaign_id"], grant["purchase_id"], grant["login_count"], grant["mail_ids"]) == (22, 1, 1, [6])
    assert marker["mail_ids"] == [1, 2, 3, 4, 5, 6]
    assert result["EBNS"] == Container("EBNS", (Column("LoginBonusID", "I", (6, 22, 135)),
        Column("LoginCount", "I", (1, 1, 1)), Column("PurchaseTime", "S", ("", "2026/10/31 12:00:00", ""))))
    assert "PGP2" in result and case.clock.calls == 1
    assert saved.state["tables"]["possessions"] == before.state["tables"]["possessions"]
    assert saved.state["tables"]["crystals"] == before.state["tables"]["crystals"]
    assert saved.state["player_state"] == before.state["player_state"]


def test_purchased_only_dispatch_has_no_unrelated_projection(purchased_dispatch):
    case = purchased_dispatch(passes=False, rank=False, events=False, comeback=False)
    result = nodes(case.rank.receive(message(case.store), case.store))
    assert set(result) == {"SYSTEM", "PPT", "EBNS"}
    assert result["EBNS"].children[0].values == (22,)
    assert saved_marker(case)["mail_ids"] == [1]
    assert saved_marker(case)["event_login_grants"] == saved_marker(case)["comeback_login_grants"] == []


def test_purchase_day_waits_for_next_logical_day_then_keeps_purchase_time(purchased_dispatch):
    case = purchased_dispatch(passes=False, rank=False, events=False, comeback=False, purchase_today=True)
    result = nodes(case.rank.receive(message(case.store), case.store))
    assert set(result) == {"SYSTEM"} and saved_marker(case)["mail_ids"] == []
    case.clock.now += timedelta(days=1)
    first = nodes(case.rank.receive(message(case.store, tick=1001), case.store))
    assert first["EBNS"].children[1].values == (1,)
    assert first["EBNS"].children[2].values == ("2026/11/01 12:00:00",)
    second = nodes(case.rank.receive(message(case.store, tick=1002), case.store))
    assert set(second) == {"SYSTEM"} and saved_marker(case)["mail_ids"] == []
    case.clock.now += timedelta(days=1)
    third = nodes(case.rank.receive(message(case.store, tick=1003), case.store))
    assert third["EBNS"].children[1].values == (2,)
    assert third["EBNS"].children[2].values == ("2026/11/01 12:00:00",)


@pytest.mark.parametrize("schema", [1, 2, 3, 4])
def test_previous_receipts_replay_without_new_purchase_helper(purchased_dispatch, monkeypatch, schema):
    case = purchased_dispatch(passes=schema >= 2, events=schema >= 3, comeback=schema >= 4,
                              purchased=False, acquire=False)
    envelope = message(case.store)
    payload = case.rank.receive(envelope, case.store)
    before = case.store.profile()
    assert saved_marker(case)["schema_version"] == schema
    case.rank = RankRewardService(case.bootstrap, case.bundle.player_state, case.presents, case.rank_catalog,
        event_passes=case.service if schema >= 2 else None, event_logins=case.events if schema >= 3 else None,
        comeback_logins=case.comeback if schema >= 4 else None, purchased_logins=case.purchased)
    monkeypatch.setattr(case.purchased, "_ready", lambda: pytest.fail("old receipt touched purchased dependency"))
    monkeypatch.setattr(case.purchased, "settle", lambda *_a, **_k: pytest.fail("old receipt issued purchase rewards"))
    case.bootstrap.clock = lambda: pytest.fail("old receipt read clock")
    assert case.rank.receive(envelope, Store(case.store.path)) == payload
    assert case.store.profile() == before


def test_new_receipt_replays_after_expiration_without_clock_or_settlement(purchased_dispatch, monkeypatch):
    case = purchased_dispatch()
    envelope = message(case.store)
    payload = case.rank.receive(envelope, case.store)
    before = case.store.profile()
    case.clock.now += timedelta(days=100)
    case.bootstrap.clock = lambda: pytest.fail("cached request read clock")
    monkeypatch.setattr(case.purchased, "settle", lambda *_a, **_k: pytest.fail("cached request settled again"))
    assert case.rank.receive(envelope, Store(case.store.path)) == payload
    assert case.store.profile() == before


def test_new_tick_after_expiration_does_not_create_missed_day_rewards(purchased_dispatch):
    case = purchased_dispatch(passes=False, rank=False, events=False, comeback=False)
    case.rank.receive(message(case.store), case.store)
    case.clock.now += timedelta(days=100)
    result = nodes(case.rank.receive(message(case.store, tick=1001), case.store))
    assert set(result) == {"SYSTEM"}
    assert len(read_purchased_logins(case.store.profile().state)["purchases"]["1"]["deliveries"]) == 1


def test_cas_retry_freezes_time_and_does_not_repeat_any_source(purchased_dispatch, monkeypatch):
    case = purchased_dispatch()
    original, calls = case.store.update_state, []
    def conflict(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            profile = case.store.profile()
            other = deepcopy(profile.state)
            other["sentinel"]["external"] = 1
            original(other, profile.revision)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    case.rank.receive(message(case.store), case.store)
    assert len(calls) == 2 and case.clock.calls == 1
    assert saved_marker(case)["mail_ids"] == [1, 2, 3, 4, 5, 6]
    assert case.store.profile().state["sentinel"]["external"] == 1


def test_concurrent_equal_requests_commit_one_purchased_reward(purchased_dispatch, monkeypatch):
    case = purchased_dispatch()
    original, barrier, lock, count = case.store.update_state, Barrier(2), Lock(), 0
    def update(state, revision):
        nonlocal count
        with lock:
            count += 1
            wait = count <= 2
        if wait:
            barrier.wait(timeout=20)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    envelope = message(case.store)
    with ThreadPoolExecutor(max_workers=2) as executor:
        payloads = list(executor.map(lambda _: case.rank.receive(envelope, case.store), range(2)))
    assert payloads[0] == payloads[1]
    assert saved_marker(case)["mail_ids"] == [1, 2, 3, 4, 5, 6]


def test_fatal_last_source_failure_rolls_back_all_earlier_deliveries(purchased_dispatch, monkeypatch):
    case = purchased_dispatch()
    def broken(*_a, **_k):
        raise ProtocolError("purchased_dependency_broken", "Injected last-source failure", 503)
    monkeypatch.setattr(case.purchased, "settle", broken)
    fail(case, message(case.store), "purchased_dependency_broken")


def test_final_encoding_failure_commits_no_partial_progress(purchased_dispatch):
    case = purchased_dispatch()
    case.rank._profiles[116] = replace(case.rank._profiles[116], response_limit=1)
    fail(case, message(case.store), "response_too_large")


def test_per_purchase_capacity_failure_retains_pending_for_same_day_retry(purchased_dispatch, monkeypatch):
    case = purchased_dispatch()
    original = case.rank.response
    def constrained(*args, **kwargs):
        if kwargs.get("purchased_login_grants"):
            raise ProtocolError("response_too_large", "Injected purchased notice capacity", 503)
        return original(*args, **kwargs)
    monkeypatch.setattr(case.rank, "response", constrained)
    first = nodes(case.rank.receive(message(case.store), case.store))
    assert first["EBNS"].children[0].values == (6, 135)
    assert saved_marker(case)["mail_ids"] == [1, 2, 3, 4, 5]
    assert read_purchased_logins(case.store.profile().state)["purchases"]["1"]["pending"]["ordinal"] == 1
    monkeypatch.setattr(case.rank, "response", original)
    second = nodes(case.rank.receive(message(case.store, tick=1001), case.store))
    assert second["EBNS"].children[0].values == (22,)
    assert saved_marker(case)["mail_ids"] == [6]


@pytest.mark.parametrize("tamper", ["overlap", "purchase", "time", "response"])
def test_new_marker_rejects_forged_purchase_sources_with_updated_state_digest(purchased_dispatch, tamper):
    case = purchased_dispatch()
    envelope = message(case.store)
    case.rank.receive(envelope, case.store)
    state = deepcopy(case.store.profile().state)
    marker = state["rank_reward_delivery"]
    grant = marker["purchased_login_grants"][0]
    receipt = state["rank_reward_history"]["receipts"][-1]
    if tamper == "overlap":
        grant["mail_ids"] = marker["comeback_login_grants"][0]["mail_ids"]
    elif tamper == "purchase":
        grant["purchase_id"] = 2
    elif tamper == "time":
        grant["purchase_time"] = "2026/10/30 12:00:00"
    else:
        receipt["response"] = receipt["response"].replace("2026/10/31 12:00:00", "2026/10/30 12:00:00")
    receipt["state"] = module._state_digest(state)
    overwrite(case, state)
    assert fail(case, envelope).status == 503


def test_missing_purchased_helper_cannot_initialize_existing_qualification(purchased_dispatch):
    case = purchased_dispatch()
    case.rank.receive(message(case.store), case.store)
    other = RankRewardService(case.bootstrap, case.bundle.player_state, case.presents, case.rank_catalog,
        event_passes=case.service, event_logins=case.events, comeback_logins=case.comeback)
    with pytest.raises(ProtocolError) as error:
        other.initialize(case.store.profile().state)
    assert error.value.code == "rank_purchased_login_unavailable"


def test_initialization_retains_history_after_purchased_mail_is_claimed(purchased_dispatch):
    case = purchased_dispatch()
    case.rank.receive(message(case.store), case.store)
    state = deepcopy(case.store.profile().state)
    state["tables"]["presents"] = []
    assert case.rank.initialize(state) == state


def test_purchased_dispatch_rejects_a_different_present_service(purchased_dispatch):
    case = purchased_dispatch()
    case.purchased.presents = case.bundle.presents
    fail(case, message(case.store), "rank_purchased_login_changed")
