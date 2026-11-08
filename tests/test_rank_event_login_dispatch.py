from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
import json
from threading import Barrier, Lock

import pytest

from aigis_ps.alxml import Column, Container
from aigis_ps.errors import ProtocolError
from aigis_ps.event_login_catalog import EventLoginCatalog
from aigis_ps.event_login_rules import EventLoginRules
from aigis_ps.event_login_service import EventLoginService
import aigis_ps.rank_rewards as module
from aigis_ps.rank_rewards import RankRewardService
from aigis_ps.storage import Store
from test_event_pass_service import baseline, bundle, factory, nodes
from test_rank_event_pass_dispatch import dispatch
from test_rank_rewards import message


@pytest.fixture
def event_dispatch(dispatch):
    def make(*, passes=True, rank=True, events=True):
        case = dispatch(enabled=passes, rank_reward=rank)
        catalog = EventLoginCatalog(lambda name: case.bundle.native_cdn.assets.read(name)[0])
        case.events = EventLoginService(catalog, case.presents, EventLoginRules(campaign_ids=(135,)))
        case.rank = RankRewardService(case.bootstrap, case.bundle.player_state, case.presents, case.rank_catalog,
            event_passes=case.service if passes else None, event_logins=case.events if events else None)
        return case
    return make


def fail(case, envelope, code=None):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as error:
        case.rank.receive(envelope, case.store)
    assert case.store.profile() == before
    if code is not None:
        assert error.value.code == code
    return error.value


def saved_marker(case):
    return case.store.profile().state["rank_reward_delivery"]


def overwrite(case, state):
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def test_rank_pass_and_real_event_login_share_one_commit_and_distinct_mail_sources(event_dispatch):
    case = event_dispatch()
    before = case.store.profile()
    response = nodes(case.rank.receive(message(case.store), case.store))
    saved = case.store.profile()
    marker = saved_marker(case)
    assert marker["schema_version"] == 3 and saved.revision == before.revision + 1
    assert marker["rank_mail_ids"] == [1]
    assert marker["event_pass_mail_ids"] == {case.entry.item_id: [2, 3]}
    assert marker["mail_ids"] == [1, 2, 3, 4]
    grant, = marker["event_login_grants"]
    assert (grant["campaign_id"], grant["login_count"], grant["mail_ids"]) == (135, 1, [4])
    assert grant["purchase_time"] == "" and grant["earned_day"] == grant["delivery_day"] == "2026-11-01"
    assert response["EBNS"] == Container("EBNS", (Column("LoginBonusID", "I", (135,)),
        Column("LoginCount", "I", (1,)), Column("PurchaseTime", "S", ("",))))
    assert set(response) == {"SYSTEM", "PPT", "EventPassStatus", "EBNS"}
    assert case.clock.calls == 1
    assert saved.state["player_state"] == before.state["player_state"]
    assert saved.state["tables"]["possessions"] == before.state["tables"]["possessions"]
    assert saved.state["tables"]["crystals"] == before.state["tables"]["crystals"]


def test_real_event_only_dispatch_does_not_require_event_pass_dependency(event_dispatch):
    case = event_dispatch(passes=False, rank=False)
    result = nodes(case.rank.receive(message(case.store), case.store))
    assert set(result) == {"SYSTEM", "PPT", "EBNS"}
    marker = saved_marker(case)
    assert marker["rank_mail_ids"] == [] and marker["event_pass_mail_ids"] == {}
    assert marker["mail_ids"] == [1]


@pytest.mark.parametrize("passes", [False, True])
def test_legacy_receipt_replays_after_enabling_events_without_calling_new_helper(event_dispatch, monkeypatch, passes):
    case = event_dispatch(passes=passes, events=False)
    envelope = message(case.store)
    payload = case.rank.receive(envelope, case.store)
    before = case.store.profile()
    base, previous = case.rank.content_version, case.rank.receive_content_version
    case.rank = RankRewardService(case.bootstrap, case.bundle.player_state, case.presents, case.rank_catalog,
        event_passes=case.service if passes else None, event_logins=case.events)
    assert case.rank.content_version == base and case.rank.receive_content_version != previous
    monkeypatch.setattr(case.events, "_ready", lambda: pytest.fail("legacy receipt touched event dependency"))
    monkeypatch.setattr(case.events, "settle", lambda *_a, **_k: pytest.fail("legacy receipt issued new events"))
    case.bootstrap.clock = lambda: pytest.fail("legacy receipt read clock")
    assert case.rank.receive(envelope, Store(case.store.path)) == payload
    assert case.store.profile() == before


def test_event_receipt_reopens_without_clock_or_redelivery(event_dispatch, monkeypatch):
    case = event_dispatch()
    envelope = message(case.store)
    payload = case.rank.receive(envelope, case.store)
    saved = case.store.profile()
    monkeypatch.setattr(case.events, "settle", lambda *_a, **_k: pytest.fail("event receipt issued again"))
    case.bootstrap.clock = lambda: pytest.fail("event receipt read clock")
    assert case.rank.receive(envelope, Store(case.store.path)) == payload
    assert case.store.profile() == saved


def test_new_same_day_request_has_no_duplicate_event_notice_or_mail(event_dispatch):
    case = event_dispatch()
    case.rank.receive(message(case.store), case.store)
    before = case.store.profile()
    response = nodes(case.rank.receive(message(case.store, tick=1001), case.store))
    assert set(response) == {"SYSTEM"}
    assert saved_marker(case)["mail_ids"] == [] and saved_marker(case)["event_login_grants"] == []
    assert case.store.profile().state["tables"]["presents"] == before.state["tables"]["presents"]


def test_new_request_after_legacy_receipt_only_issues_the_unobserved_event(event_dispatch):
    case = event_dispatch(events=False)
    case.rank.receive(message(case.store), case.store)
    case.rank = RankRewardService(case.bootstrap, case.bundle.player_state, case.presents, case.rank_catalog,
                                  event_passes=case.service, event_logins=case.events)
    result = nodes(case.rank.receive(message(case.store, tick=1001), case.store))
    assert set(result) == {"SYSTEM", "PPT", "EBNS"}
    marker = saved_marker(case)
    assert marker["rank_mail_ids"] == [] and marker["event_pass_mail_ids"] == {}
    assert marker["mail_ids"] == [4]


@pytest.mark.parametrize("number", [42, 116, 137])
def test_final_response_capacity_failure_commits_no_reward_domain(event_dispatch, number):
    case = event_dispatch()
    case.rank._profiles[number] = replace(case.rank._profiles[number], response_limit=1)
    fail(case, message(case.store), "response_too_large")


def test_fatal_event_failure_rolls_back_rank_and_pass_candidates(event_dispatch, monkeypatch):
    case = event_dispatch()
    def broken(*_args, **_kwargs):
        raise ProtocolError("event_login_broken", "Injected event dependency failure", 503)
    monkeypatch.setattr(case.events, "settle", broken)
    fail(case, message(case.store), "event_login_broken")


def test_per_campaign_response_capacity_retains_event_pending_after_rank_and_pass(event_dispatch, monkeypatch):
    case = event_dispatch()
    original = case.rank.response
    def constrained(*args, **kwargs):
        if kwargs.get("event_login_grants"):
            raise ProtocolError("response_too_large", "Injected event notice capacity", 503)
        return original(*args, **kwargs)
    monkeypatch.setattr(case.rank, "response", constrained)
    result = nodes(case.rank.receive(message(case.store), case.store))
    assert set(result) == {"SYSTEM", "PPT", "EventPassStatus"}
    assert saved_marker(case)["mail_ids"] == [1, 2, 3]
    assert saved_marker(case)["event_login_grants"] == []
    monkeypatch.setattr(case.rank, "response", original)
    case.rank.receive(message(case.store, tick=1001), case.store)
    assert saved_marker(case)["mail_ids"] == [4]
    assert saved_marker(case)["event_login_grants"][0]["login_count"] == 1


def test_cas_retry_uses_one_clock_and_does_not_duplicate_any_source(event_dispatch, monkeypatch):
    case = event_dispatch()
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
    assert saved_marker(case)["mail_ids"] == [1, 2, 3, 4]
    assert case.store.profile().state["sentinel"]["external"] == 1


def test_concurrent_identical_requests_commit_one_event(event_dispatch, monkeypatch):
    case = event_dispatch()
    barrier, lock, count = Barrier(2), Lock(), 0
    original = case.store.update_state
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
        responses = list(executor.map(lambda _: case.rank.receive(envelope, case.store), range(2)))
    assert responses[0] == responses[1] and saved_marker(case)["mail_ids"] == [1, 2, 3, 4]


@pytest.mark.parametrize("tamper", ["overlap", "ordinal", "purchase_time", "response"])
def test_mixed_delivery_cannot_forge_event_source_or_notice(event_dispatch, tamper):
    case = event_dispatch()
    envelope = message(case.store)
    case.rank.receive(envelope, case.store)
    state = deepcopy(case.store.profile().state)
    marker = state["rank_reward_delivery"]
    receipt = state["rank_reward_history"]["receipts"][-1]
    if tamper == "overlap":
        marker["event_login_grants"][0]["mail_ids"] = marker["rank_mail_ids"]
    elif tamper == "ordinal":
        marker["event_login_grants"][0]["login_count"] = 2
    elif tamper == "purchase_time":
        marker["event_login_grants"][0]["purchase_time"] = "2026/11/01 12:00:00"
    else:
        receipt["response"] = receipt["response"].replace("LoginCount", "FalseCount")
    receipt["state"] = module._state_digest(state)
    overwrite(case, state)
    assert fail(case, envelope).status == 503


def test_initialization_preserves_event_history_after_its_mail_was_claimed(event_dispatch):
    case = event_dispatch()
    case.rank.receive(message(case.store), case.store)
    state = deepcopy(case.store.profile().state)
    state["tables"]["presents"] = []
    assert case.rank.initialize(state) == state


def test_missing_event_helper_cannot_initialize_schema3_history(event_dispatch):
    case = event_dispatch()
    case.rank.receive(message(case.store), case.store)
    case.rank = RankRewardService(case.bootstrap, case.bundle.player_state, case.presents, case.rank_catalog,
                                  event_passes=case.service)
    with pytest.raises(ProtocolError) as error:
        case.rank.initialize(case.store.profile().state)
    assert error.value.code == "rank_event_login_unavailable"


def test_rank_apply_never_runs_event_login_settlement(event_dispatch, monkeypatch):
    case = event_dispatch()
    monkeypatch.setattr(case.events, "settle", lambda *_a, **_k: pytest.fail("rank apply dispatched login rewards"))
    before = case.store.profile().state
    updated, mail_ids = case.rank.apply(before, before)
    assert mail_ids == (1,) and updated.get("event_login") == before.get("event_login")


def test_event_mail_dependency_must_match_rank_mail_service(event_dispatch):
    case = event_dispatch()
    case.events.presents = copy(case.presents)
    fail(case, message(case.store), "rank_event_login_changed")
