from copy import copy, deepcopy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.event_login_rules import EventLoginRules
from aigis_ps.event_login_service import EventLoginService
from aigis_ps.login_reward_service import LoginRewardService
import aigis_ps.login_reward_service as module
from aigis_ps.rank_rewards import RankRewardService
from test_evolution_integration import post
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


@pytest.fixture
def event_factory(bundle, tmp_path):
    serial = 0

    def build(*, enabled=True, legacy=False, now=NOW):
        nonlocal serial
        serial += 1
        assert bundle.event_logins is not None, bundle.event_login_error
        clock = SimpleNamespace(now=now)
        bootstrap, presents = copy(bundle.bootstrap), copy(bundle.presents)
        bootstrap.clock = lambda: clock.now
        presents.bootstrap = bootstrap
        events = EventLoginService(bundle.event_logins.catalog, presents,
                                   EventLoginRules(enabled=enabled, campaign_ids=(135,)))
        login = LoginRewardService(bootstrap, bundle.player_state, presents, bundle.login_rewards.catalog,
                                   event_logins=None if legacy else events)
        rank = RankRewardService(bootstrap, bundle.player_state, presents, bundle.rank_rewards.catalog,
                                 event_logins=events)
        bootstrap.login_reward_seed_provider = login.seed_new
        bootstrap.present_provider = presents.response
        state = bundle.player_state.initialize(bootstrap.initial_state())
        state = rank.initialize(login.initialize(state))
        directory = tmp_path / str(serial)

        def application(service=login):
            return create_app(directory, initial_state=state,
                state_initializer=lambda value: rank.initialize(service.initialize(value)),
                handlers=bootstrap.handlers() | presents.handlers() | service.handlers() | rank.handlers())

        app = application()
        return SimpleNamespace(bundle=bundle, clock=clock, bootstrap=bootstrap, presents=presents,
            events=events, login=login, rank=rank, app=app, store=app.state.store, application=application)

    return build


def message(case, operation=115, tick=1000):
    columns = (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,)))
    if operation == 116:
        columns += (Column("DmmPremiumReceiverId", "I", (0,)),)
    return request(case.store, operation, columns, tick=tick)


def grant_rows(case):
    return case.store.profile().state.get("event_login", {}).get("campaigns", {}).get("135", {}).get("deliveries", {})


def change(case, callback, *, revision=True):
    profile = case.store.profile()
    candidate = deepcopy(profile.state)
    callback(candidate)
    if revision:
        case.store.update_state(candidate, profile.revision)
    else:
        with case.store.connection() as connection:
            connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(candidate),))


@pytest.mark.parametrize("first,second", [(115, 116), (116, 115)])
def test_native_login_paths_share_one_daily_entitlement(event_factory, first, second):
    case = event_factory()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, response = post(client, message(case, first))
        assert response["EBNS"].children == (Column("LoginBonusID", "I", (135,)),
            Column("LoginCount", "I", (1,)), Column("PurchaseTime", "S", ("",)))
        original = deepcopy(grant_rows(case))
        _, response = post(client, message(case, second, 1001))
        assert "EBNS" not in response
        assert grant_rows(case) == original
    campaign_mails = [row for row in case.presents.rows(case.store.profile().state)
                      if row["Description"] == "イベントログインボーナス"]
    assert len(campaign_mails) == 1 and campaign_mails[0]["Content"] == 1016


def test_event_retry_survives_reopen_without_reading_clock(event_factory):
    case = event_factory()
    envelope = message(case)
    response = case.login.receive(envelope, case.store)
    before = case.store.profile()
    reopened = case.application()
    case.login.clock = lambda: pytest.fail("Committed receipt consulted current time")
    assert case.login.receive(envelope, reopened.state.store) == response
    assert reopened.state.store.profile() == before


def test_missed_days_advance_only_one_ordinal_and_expiry_stops_new_grants(event_factory):
    case = event_factory()
    case.login.receive(message(case), case.store)
    case.clock.now += timedelta(days=3)
    case.login.receive(message(case, tick=1001), case.store)
    assert set(grant_rows(case)) == {"1", "2"}
    assert grant_rows(case)["2"]["delivery_day"] == "2026-11-04"
    case.clock.now = datetime(2026, 11, 6, 4, tzinfo=JST)
    case.login.receive(message(case, tick=1002), case.store)
    assert set(grant_rows(case)) == {"1", "2"}


def test_jst_four_am_is_the_shared_daily_boundary(event_factory):
    case = event_factory()
    case.login.receive(message(case), case.store)
    case.clock.now = datetime(2026, 11, 2, 3, 59, 59, tzinfo=JST)
    case.login.receive(message(case, tick=1001), case.store)
    assert len(grant_rows(case)) == 1
    case.clock.now += timedelta(seconds=1)
    case.login.receive(message(case, tick=1002), case.store)
    assert len(grant_rows(case)) == 2


def test_pending_event_does_not_block_ordinary_login_and_retries_after_expiry(event_factory, monkeypatch):
    case = event_factory()
    original = case.presents.enqueue_groups

    def fail_event(state, groups):
        if any(reward.get("description") == "イベントログインボーナス" for group in groups for reward in group):
            raise ProtocolError("event_pool_unavailable", "Unavailable reward source", 501)
        return original(state, groups)

    monkeypatch.setattr(case.presents, "enqueue_groups", fail_event)
    case.login.receive(message(case), case.store)
    state = case.store.profile().state
    assert state["login_rewards"]["stamp_count"] == 1
    pending = state["event_login"]["campaigns"]["135"]["pending"]
    assert pending["ordinal"] == 1 and pending["earned_day"] == "2026-11-01"
    assert len(case.presents.rows(state)) == 2
    monkeypatch.setattr(case.presents, "enqueue_groups", original)
    case.clock.now = datetime(2026, 11, 7, 12, tzinfo=JST)
    case.login.receive(message(case, tick=1001), case.store)
    assert len(grant_rows(case)) == 1
    assert grant_rows(case)["1"]["earned_day"] == "2026-11-01"
    assert grant_rows(case)["1"]["delivery_day"] == "2026-11-07"


def test_same_day_pending_can_retry_without_another_ordinary_stamp(event_factory, monkeypatch):
    case = event_factory()
    original = case.login._response

    def capacity(profile, state, ids, beginner, event_login_grants=None, comeback_login_grants=None, **sources):
        if event_login_grants:
            raise ProtocolError("login_reward_response_capacity", "Event result will not fit", 503)
        return original(profile, state, ids, beginner, event_login_grants, comeback_login_grants, **sources)

    monkeypatch.setattr(case.login, "_response", capacity)
    case.login.receive(message(case), case.store)
    assert not grant_rows(case)
    monkeypatch.setattr(case.login, "_response", original)
    case.login.receive(message(case, tick=1001), case.store)
    assert len(grant_rows(case)) == 1
    assert case.store.profile().state["login_rewards"]["stamp_count"] == 1
    assert len(case.presents.rows(case.store.profile().state)) == 3


def test_nonrecoverable_event_failure_rolls_back_ordinary_rewards(event_factory, monkeypatch):
    case = event_factory()
    before = case.store.profile()
    monkeypatch.setattr(case.events, "settle", lambda *args, **kwargs: (_ for _ in ()).throw(
        ProtocolError("event_login_state_invalid", "Invalid state", 503)))
    with pytest.raises(ProtocolError, match="Invalid state"):
        case.login.receive(message(case), case.store)
    assert case.store.profile() == before


def test_legacy_committed_login_receipt_does_not_gain_new_event_rewards(event_factory):
    case = event_factory(legacy=True)
    envelope = message(case)
    response = case.login.receive(envelope, case.store)
    before = case.store.profile()
    newer = LoginRewardService(case.bootstrap, case.bundle.player_state, case.presents,
        case.login.catalog, event_logins=case.events)
    newer.clock = lambda: pytest.fail("Legacy receipt consulted current time")
    assert newer.receive(envelope, case.store) == response
    assert case.store.profile() == before
    assert "event_login" not in before.state


@pytest.mark.parametrize("field", ["definition", "campaign_id", "mail_ids", "base_mail_ids", "response"])
def test_forged_event_receipt_cannot_reassign_the_reward_source(event_factory, field):
    case = event_factory()
    envelope = message(case)
    case.login.receive(envelope, case.store)

    def forge(state):
        entry = state["login_reward_history"]["receipts"][-1]
        if field == "base_mail_ids":
            state["login_reward_delivery"]["base_mail_ids"] = []
        elif field == "response":
            entry["response"] = entry["response"].replace("135", "134")
        else:
            value = {"definition": "f" * 64, "campaign_id": 134, "mail_ids": [1]}[field]
            entry["sources"]["event_login_grants"][0][field] = value
            state["login_reward_delivery"]["event_login_grants"][0][field] = deepcopy(value)
        entry["state"] = module._state_digest(state)

    change(case, forge, revision=False)
    before = case.store.profile()
    with pytest.raises(ProtocolError):
        case.login.receive(envelope, case.store)
    assert case.store.profile() == before


def test_all_ten_campaign_days_and_actual_character_claim(event_factory):
    case = event_factory(now=datetime(2026, 10, 22, 12, tzinfo=JST))
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        for ordinal in range(1, 11):
            _, result = post(client, message(case, 115 if ordinal % 2 else 116, 1000 + ordinal))
            assert result["EBNS"].children[1].values == (ordinal,)
            case.clock.now += timedelta(days=1)
        ledger = grant_rows(case)
        assert set(ledger) == {str(ordinal) for ordinal in range(1, 11)}
        ids = [ledger[str(ordinal)]["mail_ids"][0] for ordinal in range(1, 11)]
        indexed = {row["PresentID"]: row for row in case.presents.rows(case.store.profile().state)}
        assert [indexed[identifier]["Content"] for identifier in ids] == [
            1016, 1080, 1129, 1192, 1284, 1417, 1523, 1616, 1714, 1828]
        before = len(case.store.profile().state["player_state"]["units"])
        envelope = request(case.store, 1, (Container("RT", (Container("PT2", (
            Column("PresentID", "I", (ids[0],)),)),)),), tick=1100)
        _, result = post(client, envelope)
        assert "PPU" in result
        units = case.store.profile().state["player_state"]["units"]
        assert len(units) == before + 1 and units[-1]["CardID"] == 1225
        _, result = post(client, message(case, 115, 1101))
        assert "EBNS" not in result and len(grant_rows(case)) == 10
    saved = case.store.profile()
    assert case.application().state.store.profile() == saved
