from copy import copy, deepcopy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, loads
from aigis_ps.app import create_app
import aigis_ps.bootstrap as bootstrap_module
from aigis_ps.comeback_login_service import ComebackLoginService
from aigis_ps.comeback_login_rules import ComebackLoginRules
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.event_login_rules import EventLoginRules
from aigis_ps.event_login_service import EventLoginService
from aigis_ps.login_reward_service import LoginRewardService
import aigis_ps.login_reward_service as module
from aigis_ps.rank_rewards import RankRewardService
from test_evolution_integration import post
from test_quest_service import bundle, request


RETURN = datetime(2026, 11, 1, 12, tzinfo=JST)


@pytest.fixture
def comeback_factory(bundle, tmp_path):
    serial = 0

    def build(gap=30, *, public=True, old_projection=False):
        nonlocal serial
        serial += 1
        assert bundle.comeback_logins is not None, bundle.comeback_login_error
        bootstrap, presents = copy(bundle.bootstrap), copy(bundle.presents)
        clock = SimpleNamespace(now=RETURN - timedelta(days=gap))
        bootstrap.clock = lambda: clock.now
        presents.bootstrap = bootstrap
        comeback = ComebackLoginService(bundle.comeback_logins.catalog, presents, ComebackLoginRules())
        events = EventLoginService(bundle.event_logins.catalog, presents,
                                   EventLoginRules(campaign_ids=(135,) if public else ()))
        login = LoginRewardService(bootstrap, bundle.player_state, presents, bundle.login_rewards.catalog,
                                   event_logins=events, comeback_logins=comeback)
        rank = RankRewardService(bootstrap, bundle.player_state, presents, bundle.rank_rewards.catalog,
                                 event_logins=events, comeback_logins=comeback)
        bootstrap.comeback_logins = comeback
        bootstrap.present_provider = presents.response
        bootstrap.login_reward_seed_provider = login.seed_new
        state = bundle.player_state.initialize(bootstrap.initial_state())
        if old_projection:
            state["tables"]["game_param2"][0].update(LatestComebackLoginBonusDateTime="2025/01/01 12:00:00",
                                                     LatestComebackLoginBonusDays=90)
        state = rank.initialize(login.initialize(state))
        directory = tmp_path / str(serial)

        def application():
            return create_app(directory, initial_state=state,
                state_initializer=lambda value: rank.initialize(login.initialize(value)),
                handlers=bootstrap.handlers() | presents.handlers() | login.handlers() | rank.handlers())

        app = application()
        return SimpleNamespace(bundle=bundle, bootstrap=bootstrap, presents=presents, clock=clock,
            comeback=comeback, events=events, login=login, rank=rank, app=app, store=app.state.store,
            application=application)

    return build


def message(case, operation=115, tick=1000):
    columns = () if operation == 114 else (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,)))
    if operation == 116:
        columns += (Column("DmmPremiumReceiverId", "I", (0,)),)
    return request(case.store, operation, columns, tick=tick)


def return_login(case):
    case.bootstrap.login(message(case, 114), case.store)
    case.clock.now = RETURN
    return case.bootstrap.login(message(case, 114, 1001), case.store)


@pytest.mark.parametrize("first,second", [(115, 116), (116, 115)])
def test_login_observation_and_two_claim_paths_share_return_round(comeback_factory, first, second):
    case = comeback_factory()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, message(case, 114))
        assert case.presents.rows(case.store.profile().state) == []
        case.clock.now = RETURN
        _, login = post(client, message(case, 114, 1001))
        assert set(login) == {"SYSTEM", "LGI"}
        _, fields = post(client, request(case.store, 35, ()))
        assert fields["D0"].values == ("2026/11/01 12:00:00",)
        assert fields["D1"].values == (30,)
        _, result = post(client, message(case, first, 1002))
        assert result["EBNS"].children[0].values == (6, 135)
        pairs = dict(zip(result["PGP2"].children[0].values, result["PGP2"].children[1].values))
        assert pairs["LatestComebackLoginBonusDateTime"] == "2026/11/01 12:00:00"
        assert pairs["LatestComebackLoginBonusDays"] == "30"
        assert "D0" not in pairs and "D1" not in pairs
        _, later = post(client, message(case, second, 1003))
        assert "EBNS" not in later
    rows = case.presents.rows(case.store.profile().state)
    assert sum(row["Description"] == "カムバックログインボーナス" for row in rows) == 1


def test_existing_native_return_fields_are_preserved_without_backfill(comeback_factory):
    case = comeback_factory(old_projection=True)
    before = deepcopy(case.store.profile().state["tables"]["game_param2"])
    case.bootstrap.login(message(case, 114), case.store)
    assert case.store.profile().state["tables"]["game_param2"] == before
    assert case.presents.rows(case.store.profile().state) == []
    case.login.receive(message(case, tick=1001), case.store)
    assert not any(row["Description"] == "カムバックログインボーナス"
                   for row in case.presents.rows(case.store.profile().state))


def test_reopened_return_receipt_never_observes_a_new_login(comeback_factory, monkeypatch):
    case = comeback_factory()
    return_login(case)
    envelope = message(case, tick=1002)
    result = case.login.receive(envelope, case.store)
    saved = case.store.profile()
    reopened = case.application()
    monkeypatch.setattr(case.comeback, "observe", lambda *_: pytest.fail("Cached reply observed activity"))
    monkeypatch.setattr(case.login, "clock", lambda: pytest.fail("Cached reply read time"))
    assert case.login.receive(envelope, reopened.state.store) == result
    assert reopened.state.store.profile() == saved


def test_old_public_receipt_is_not_upgraded_into_return_reward(comeback_factory, monkeypatch):
    case = comeback_factory()
    legacy = LoginRewardService(case.bootstrap, case.bundle.player_state, case.presents, case.login.catalog,
                                event_logins=case.events)
    envelope = message(case)
    result = legacy.receive(envelope, case.store)
    saved = case.store.profile()
    monkeypatch.setattr(case.comeback, "observe", lambda *_: pytest.fail("Old reply observed activity"))
    assert case.login.receive(envelope, case.store) == result
    assert case.store.profile() == saved


@pytest.mark.parametrize("field", ["round_id", "notify", "base_mail_ids", "response"])
def test_return_source_cannot_be_forged_in_cached_login(comeback_factory, field):
    case = comeback_factory()
    return_login(case)
    envelope = message(case, tick=1002)
    case.login.receive(envelope, case.store)
    profile = case.store.profile()
    state = deepcopy(profile.state)
    receipt = state["login_reward_history"]["receipts"][-1]
    marker = state["login_reward_delivery"]
    if field in ("round_id", "notify"):
        value = 2 if field == "round_id" else False
        receipt["sources"]["comeback_login_grants"][0][field] = value
        marker["comeback_login_grants"][0][field] = value
    elif field == "base_mail_ids":
        marker["base_mail_ids"] = []
    else:
        receipt["response"] = receipt["response"].replace("2026/11/01", "2026/10/01")
    receipt["state"] = module._state_digest(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    before = case.store.profile()
    with pytest.raises(ProtocolError):
        case.login.receive(envelope, case.store)
    assert case.store.profile() == before


def test_failed_return_settlement_does_not_commit_login_or_ordinary_rewards(comeback_factory, monkeypatch):
    case = comeback_factory()
    return_login(case)
    before = case.store.profile()
    monkeypatch.setattr(case.comeback, "settle", lambda *args, **kwargs: (_ for _ in ()).throw(
        ProtocolError("comeback_login_state_invalid", "Invalid return journal", 503)))
    with pytest.raises(ProtocolError):
        case.login.receive(message(case, tick=1002), case.store)
    assert case.store.profile() == before


def test_fifteen_day_return_band_completes_without_duplicate_rewards(comeback_factory):
    case = comeback_factory(120, public=False)
    return_login(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        for ordinal in range(1, 16):
            _, result = post(client, message(case, 115 if ordinal % 2 else 116, 1001 + ordinal))
            assert result["EBNS"].children[0].values == (6,)
            assert result["EBNS"].children[1].values == (ordinal,)
            case.clock.now += timedelta(days=1)
        _, result = post(client, message(case, 115, 1100))
        assert "EBNS" not in result
    rows = [row for row in case.presents.rows(case.store.profile().state)
            if row["Description"] == "カムバックログインボーナス"]
    assert len(rows) == 15
    assert rows[-1]["ItemID"] == "item_smn_ticket_19114"
    saved = case.store.profile()
    assert case.application().state.store.profile() == saved


def test_inconsistent_native_return_projection_prevents_commit(comeback_factory, monkeypatch):
    case = comeback_factory()
    return_login(case)
    before = case.store.profile()
    monkeypatch.setattr(case.comeback, "pgp2_pairs", lambda _: (("D0", "wrong"),))
    with pytest.raises(ProtocolError) as failure:
        case.login.receive(message(case, tick=1002), case.store)
    assert failure.value.code == "login_comeback_projection_invalid"
    assert case.store.profile() == before


@pytest.mark.parametrize("registration", [33, 35])
def test_login_observation_is_not_saved_if_native_date_readback_cannot_encode(comeback_factory, monkeypatch, caplog, registration):
    case = comeback_factory()
    case.bootstrap.login(message(case, 114), case.store)
    case.clock.now = RETURN
    before = case.store.profile()
    original = bootstrap_module.encode_response

    def encode(profile, payload):
        if registration in profile.registration_keys:
            raise ProtocolError("fixture_date_capacity", "Date readback failed", 503)
        return original(profile, payload)

    monkeypatch.setattr(bootstrap_module, "encode_response", encode)
    login = loads(case.bootstrap.login(message(case, 114, 1001), case.store))
    assert [group.name for group in login.children] == ["SYSTEM", "LGI"]
    assert case.store.profile() == before
    assert any("fixture_date_capacity" in record.getMessage() for record in caplog.records)

def test_public_reward_capacity_includes_return_fields_before_commit(comeback_factory, monkeypatch):
    case = comeback_factory()
    return_login(case)
    original = case.login._response

    def capacity(*args, **kwargs):
        payload = original(*args, **kwargs)
        groups = {node.name: node for node in loads(payload).children}
        if ("EBNS" in groups and 135 in groups["EBNS"].children[0].values
                and "LatestComebackLoginBonusDateTime" in groups["PGP2"].children[0].values):
            raise ProtocolError("login_reward_response_capacity", "Combined result exceeds capacity", 503)
        return payload

    monkeypatch.setattr(case.login, "_response", capacity)
    case.login.receive(message(case, tick=1002), case.store)
    state = case.store.profile().state
    assert state["login_rewards"]["stamp_count"] == 1
    assert state["event_login"]["campaigns"]["135"]["pending"]["ordinal"] == 1
    assert any(row["Description"] == "カムバックログインボーナス" for row in case.presents.rows(state))


def test_return_only_service_replays_without_requiring_public_activity(comeback_factory):
    case = comeback_factory()
    return_login(case)
    service = LoginRewardService(case.bootstrap, case.bundle.player_state, case.presents,
        case.login.catalog, comeback_logins=case.comeback)
    envelope = message(case, tick=1002)
    response = service.receive(envelope, case.store)
    saved = case.store.profile()
    assert saved.state["login_reward_delivery"]["event_login_grants"] == []
    assert service.receive(envelope, case.store) == response
    assert case.store.profile() == saved
