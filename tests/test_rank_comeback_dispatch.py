from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from threading import Barrier, Lock

import pytest

from aigis_ps.alxml import Column, Container
from aigis_ps.comeback_login_catalog import ComebackLoginCatalog
from aigis_ps.comeback_login_rules import ComebackLoginRules
from aigis_ps.comeback_login_service import ComebackLoginService
from aigis_ps.comeback_login_state import DATE_FIELD, DAYS_FIELD, read_comeback_logins
from aigis_ps.errors import ProtocolError
import aigis_ps.rank_rewards as module
from aigis_ps.rank_rewards import RankRewardService
from aigis_ps.storage import Store
from test_rank_event_login_dispatch import (
    baseline, bundle, dispatch, event_dispatch, factory, fail, nodes, overwrite, saved_marker,
)
from test_rank_rewards import message


@pytest.fixture
def comeback_dispatch(event_dispatch):
    def make(*, passes=True, rank=True, events=True, comeback=True, seed=True, enabled=True):
        case = event_dispatch(passes=passes, rank=rank, events=events)
        case.comeback = ComebackLoginService(ComebackLoginCatalog(case.events.catalog), case.presents,
                                            ComebackLoginRules(enabled=enabled))
        profile = case.store.profile()
        state = deepcopy(profile.state)
        state.pop("comeback_login", None)
        state["tables"]["game_param2"][0].update({DATE_FIELD: "", DAYS_FIELD: 0})
        if seed:
            state = case.comeback.observe(state, case.clock.now - timedelta(days=30))
        case.store.update_state(state, profile.revision)
        case.rank = RankRewardService(case.bootstrap, case.bundle.player_state, case.presents, case.rank_catalog,
            event_passes=case.service if passes else None, event_logins=case.events if events else None,
            comeback_logins=case.comeback if comeback else None)
        return case
    return make


def pgp2(case):
    row = case.store.profile().state["tables"]["game_param2"][0]
    return Container("PGP2", (Column("A1", "S", (DATE_FIELD, DAYS_FIELD)),
        Column("A2", "S", (row[DATE_FIELD], str(row[DAYS_FIELD])))))


def test_four_sources_share_one_commit_with_one_ebns_and_canonical_projection(comeback_dispatch):
    case = comeback_dispatch()
    before = case.store.profile()
    result = nodes(case.rank.receive(message(case.store), case.store))
    saved, marker = case.store.profile(), saved_marker(case)
    assert saved.revision == before.revision + 1 and marker["schema_version"] == 4
    assert marker["rank_mail_ids"] == [1]
    assert marker["event_pass_mail_ids"] == {case.entry.item_id: [2, 3]}
    assert marker["event_login_grants"][0]["mail_ids"] == [4]
    grant, = marker["comeback_login_grants"]
    assert (grant["campaign_id"], grant["round_id"], grant["login_count"], grant["notify"], grant["mail_ids"]) == (6, 1, 1, True, [5])
    assert marker["mail_ids"] == [1, 2, 3, 4, 5]
    assert result["EBNS"] == Container("EBNS", (Column("LoginBonusID", "I", (6, 135)),
        Column("LoginCount", "I", (1, 1)), Column("PurchaseTime", "S", ("", ""))))
    assert result["PGP2"] == pgp2(case)
    assert case.clock.calls == 1 and saved.state["tables"]["game_param2"][0][DAYS_FIELD] == 30
    assert saved.state["player_state"] == before.state["player_state"]
    assert saved.state["tables"]["crystals"] == before.state["tables"]["crystals"]
    assert saved.state["tables"]["possessions"] == before.state["tables"]["possessions"]


def test_comeback_only_does_not_require_other_dispatch_services(comeback_dispatch):
    case = comeback_dispatch(passes=False, rank=False, events=False)
    result = nodes(case.rank.receive(message(case.store), case.store))
    assert set(result) == {"SYSTEM", "PPT", "PGP2", "EBNS"}
    assert saved_marker(case)["event_login_grants"] == []
    assert saved_marker(case)["mail_ids"] == [1]


def test_first_observation_only_sets_baseline_and_preserves_legacy_projection(comeback_dispatch):
    case = comeback_dispatch(passes=False, rank=False, events=False, seed=False)
    profile = case.store.profile()
    state = deepcopy(profile.state)
    state["tables"]["game_param2"][0].update({DATE_FIELD: "2024/01/02 12:00:00", DAYS_FIELD: 365})
    case.store.update_state(state, profile.revision)
    result = nodes(case.rank.receive(message(case.store), case.store))
    ledger = read_comeback_logins(case.store.profile().state)
    assert ledger["rounds"] == {} and ledger["current_round_id"] is None
    assert set(result) == {"SYSTEM", "PGP2"}
    assert result["PGP2"] == pgp2(case)
    assert result["PGP2"].children[1].values == ("2024/01/02 12:00:00", "365")


def test_same_day_new_request_observes_but_does_not_repeat_rewards(comeback_dispatch):
    case = comeback_dispatch()
    case.rank.receive(message(case.store), case.store)
    before = case.store.profile()
    result = nodes(case.rank.receive(message(case.store, tick=1001), case.store))
    assert set(result) == {"SYSTEM", "PGP2"} and saved_marker(case)["mail_ids"] == []
    assert case.store.profile().state["tables"]["presents"] == before.state["tables"]["presents"]
    assert read_comeback_logins(case.store.profile().state)["current_round_id"] == 1


def test_next_day_uses_ebns_ordinal_without_incrementing_absence_days(comeback_dispatch):
    case = comeback_dispatch(passes=False, rank=False, events=False)
    case.rank.receive(message(case.store), case.store)
    case.clock.now += timedelta(days=1)
    result = nodes(case.rank.receive(message(case.store, tick=1001), case.store))
    assert result["EBNS"].children[1].values == (2,)
    assert result["PGP2"].children[1].values == ("2026/11/01 12:00:00", "30")


@pytest.mark.parametrize("schema", [1, 2, 3])
def test_old_schema_replays_after_enabling_comeback_without_observing(comeback_dispatch, monkeypatch, schema):
    case = comeback_dispatch(passes=schema >= 2, events=schema >= 3, comeback=False, seed=False)
    envelope = message(case.store)
    payload = case.rank.receive(envelope, case.store)
    before = case.store.profile()
    assert saved_marker(case)["schema_version"] == schema
    old_content = case.rank.content_version
    case.rank = RankRewardService(case.bootstrap, case.bundle.player_state, case.presents, case.rank_catalog,
        event_passes=case.service if schema >= 2 else None,
        event_logins=case.events if schema >= 3 else None, comeback_logins=case.comeback)
    assert case.rank.content_version == old_content
    monkeypatch.setattr(case.comeback, "_ready", lambda: pytest.fail("old receipt touched comeback dependency"))
    monkeypatch.setattr(case.comeback, "observe", lambda *_a: pytest.fail("old receipt observed login"))
    case.bootstrap.clock = lambda: pytest.fail("old receipt read clock")
    assert case.rank.receive(envelope, Store(case.store.path)) == payload
    assert case.store.profile() == before


def test_exact_new_receipt_replays_without_clock_observation_or_settlement(comeback_dispatch, monkeypatch):
    case = comeback_dispatch()
    envelope = message(case.store)
    payload = case.rank.receive(envelope, case.store)
    before = case.store.profile()
    for name in ("observe", "settle"):
        monkeypatch.setattr(case.comeback, name, lambda *_a, **_k: pytest.fail("cached receipt changed comeback state"))
    case.bootstrap.clock = lambda: pytest.fail("cached receipt read clock")
    assert case.rank.receive(envelope, Store(case.store.path)) == payload
    assert case.store.profile() == before


def test_cas_retry_preserves_external_change_and_one_captured_time(comeback_dispatch, monkeypatch):
    case = comeback_dispatch()
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
    assert saved_marker(case)["mail_ids"] == [1, 2, 3, 4, 5]
    assert case.store.profile().state["sentinel"]["external"] == 1


def test_concurrent_identical_requests_commit_one_round_and_delivery(comeback_dispatch, monkeypatch):
    case = comeback_dispatch()
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
        responses = list(executor.map(lambda _: case.rank.receive(envelope, case.store), range(2)))
    assert responses[0] == responses[1]
    assert saved_marker(case)["mail_ids"] == [1, 2, 3, 4, 5]
    assert read_comeback_logins(case.store.profile().state)["next_round_id"] == 2


@pytest.mark.parametrize("operation", [33, 35, 116])
def test_projection_or_response_capacity_failure_rolls_back_observation_and_all_rewards(comeback_dispatch, operation):
    case = comeback_dispatch()
    case.rank._profiles[operation] = replace(case.rank._profiles[operation], response_limit=1)
    fail(case, message(case.store), "response_too_large")
    assert read_comeback_logins(case.store.profile().state)["current_round_id"] is None


def test_fatal_comeback_failure_rolls_back_earlier_sources_and_observation(comeback_dispatch, monkeypatch):
    case = comeback_dispatch()
    def broken(*_a, **_k):
        raise ProtocolError("comeback_dependency_broken", "Injected comeback failure", 503)
    monkeypatch.setattr(case.comeback, "settle", broken)
    fail(case, message(case.store), "comeback_dependency_broken")


def test_failed_notice_capacity_retains_pending_then_same_day_retries(comeback_dispatch, monkeypatch):
    case = comeback_dispatch()
    original = case.rank.response
    def constrained(*args, **kwargs):
        if kwargs.get("comeback_login_grants"):
            raise ProtocolError("response_too_large", "Injected final comeback notice capacity", 503)
        return original(*args, **kwargs)
    monkeypatch.setattr(case.rank, "response", constrained)
    result = nodes(case.rank.receive(message(case.store), case.store))
    assert result["EBNS"].children[0].values == (135,)
    assert saved_marker(case)["mail_ids"] == [1, 2, 3, 4]
    assert read_comeback_logins(case.store.profile().state)["rounds"]["1"]["pending"]["ordinal"] == 1
    monkeypatch.setattr(case.rank, "response", original)
    result = nodes(case.rank.receive(message(case.store, tick=1001), case.store))
    assert result["EBNS"].children[0].values == (6,)
    assert saved_marker(case)["mail_ids"] == [5]


@pytest.mark.parametrize("tamper", ["overlap", "round", "notify", "pgp2"])
def test_delivery_and_projection_cannot_be_forged_with_matching_state_digest(comeback_dispatch, tamper):
    case = comeback_dispatch()
    envelope = message(case.store)
    case.rank.receive(envelope, case.store)
    state = deepcopy(case.store.profile().state)
    grant = state["rank_reward_delivery"]["comeback_login_grants"][0]
    receipt = state["rank_reward_history"]["receipts"][-1]
    if tamper == "overlap":
        grant["mail_ids"] = state["rank_reward_delivery"]["rank_mail_ids"]
    elif tamper == "round":
        grant["round_id"] = 2
    elif tamper == "notify":
        grant["notify"] = False
    else:
        receipt["response"] = receipt["response"].replace(DAYS_FIELD, "D1")
    receipt["state"] = module._state_digest(state)
    overwrite(case, state)
    assert fail(case, envelope).status == 503


def test_old_round_pending_after_new_qualification_has_no_obsolete_ebns(comeback_dispatch, monkeypatch):
    case = comeback_dispatch(passes=False, rank=False, events=False)
    original = case.rank.response
    def constrained(*args, **kwargs):
        if kwargs.get("comeback_login_grants"):
            raise ProtocolError("response_too_large", "Injected pending reward", 503)
        return original(*args, **kwargs)
    monkeypatch.setattr(case.rank, "response", constrained)
    case.rank.receive(message(case.store), case.store)
    monkeypatch.setattr(case.rank, "response", original)
    case.clock.now += timedelta(days=30)
    result = nodes(case.rank.receive(message(case.store, tick=1001), case.store))
    grant, = saved_marker(case)["comeback_login_grants"]
    assert grant["round_id"] == 1 and grant["notify"] is False
    assert "EBNS" not in result and "PPT" in result
    assert read_comeback_logins(case.store.profile().state)["current_round_id"] == 2
    assert result["PGP2"].children[1].values == ("2026/12/01 12:00:00", "30")


def test_missing_helper_cannot_initialize_saved_comeback_domain(comeback_dispatch):
    case = comeback_dispatch()
    case.rank.receive(message(case.store), case.store)
    service = RankRewardService(case.bootstrap, case.bundle.player_state, case.presents, case.rank_catalog,
        event_passes=case.service, event_logins=case.events)
    with pytest.raises(ProtocolError, match="Comeback history"):
        service.initialize(case.store.profile().state)


def test_history_initialization_preserves_claimed_mail_without_redelivery(comeback_dispatch):
    case = comeback_dispatch()
    case.rank.receive(message(case.store), case.store)
    state = deepcopy(case.store.profile().state)
    state["tables"]["presents"] = []
    assert case.rank.initialize(state) == state


def test_comeback_must_share_the_same_present_service(comeback_dispatch):
    case = comeback_dispatch()
    case.comeback.presents = case.bundle.presents
    fail(case, message(case.store), "rank_comeback_login_changed")


def test_apply_never_observes_or_settles_comeback(comeback_dispatch, monkeypatch):
    case = comeback_dispatch()
    monkeypatch.setattr(case.comeback, "observe", lambda *_a: pytest.fail("rank crossing observed login"))
    monkeypatch.setattr(case.comeback, "settle", lambda *_a: pytest.fail("rank crossing settled comeback"))
    before = case.store.profile().state
    updated, ids = case.rank.apply(before, before)
    assert ids == (1,) and updated["comeback_login"] == before["comeback_login"]
