from copy import copy, deepcopy
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.energy_clock import JST
from aigis_ps.login_reward_service import LoginRewardService
from aigis_ps.login_stamp_pending import read_pending_stamps
from aigis_ps.recovery_service import RecoveryService
from test_evolution_integration import post
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


@pytest.fixture
def factory(bundle, tmp_path):
    def build(now=NOW, *, legacy=False, mutate=None):
        local = copy(bundle)
        clock = SimpleNamespace(now=now)
        bootstrap = copy(bundle.bootstrap)
        bootstrap.clock = lambda: clock.now
        presents = copy(bundle.presents)
        presents.bootstrap = bootstrap
        tickets = copy(bundle.summon_tickets)
        tickets.bootstrap, tickets.presents = bootstrap, presents
        tickets.randbelow = lambda _: 0
        presents.tickets = tickets
        # Login-only: production 115 also dispatches campaign, daily and vote rewards via the shared mail service
        service = LoginRewardService(bootstrap, bundle.player_state, presents, bundle.login_rewards.catalog,
                                     rules=bundle.login_rewards.rules)
        bootstrap.present_provider = presents.response
        bootstrap.login_reward_seed_provider = None if legacy else service.seed_new
        local.bootstrap, local.presents, local.summon_tickets, local.login_rewards = bootstrap, presents, tickets, service
        state = bootstrap.initial_state()
        if mutate is not None:
            mutate(state)
        state = local.initialize(state)
        recovery = RecoveryService(bootstrap)

        def application():
            return create_app(tmp_path, initial_state=state, state_initializer=local.initialize,
                              handlers=bootstrap.handlers() | service.handlers() | presents.handlers() | recovery.handlers())

        return SimpleNamespace(bundle=local, service=service, state=state, clock=clock, application=application)
    return build


def login(store, tick=1000):
    return request(store, 115, (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,))), tick=tick)


def claim(store, identifier, tick):
    return request(store, 1, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", (identifier,)),)),)),), tick=tick)


def pairs(node):
    return dict(zip(node.children[0].values, node.children[1].values))


def test_new_login_ticket_claim_beginner_reward_and_same_day_restart(factory):
    case = factory()
    app = case.application()
    before = app.state.store.profile()
    envelope = login(app.state.store)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, response = post(client, envelope)
        assert set(response) == {"SYSTEM", "PGP2", "PPT", "BBNS"}
        assert pairs(response["PGP2"]) == {"A5": str(NOW.date().toordinal() - 1), "A6": "1", "C1": "0", "CC": "0", "CD": "0"}
        assert response["BBNS"].children == (Column("GetCount", "I", (1,)), Column("GetRemain", "I", (6,)))
        saved = app.state.store.profile()
        mail = saved.state["tables"]["presents"]
        assert [row["Type"] for row in mail] == [16, 0]
        assert saved.state["player_state"] == before.state["player_state"]
        assert saved.state["tables"]["crystals"] == before.state["tables"]["crystals"]
        _, result = post(client, claim(app.state.store, mail[0]["PresentID"], 1001))
        assert {column.name: column.values for column in result["PPU"].children}["A1"] == (8,)
        post(client, claim(app.state.store, mail[1]["PresentID"], 1002))
        _, repeated = post(client, login(app.state.store, 1003))
        assert set(repeated) == {"SYSTEM", "PGP2"}
        assert not app.state.store.profile().state["tables"]["presents"]
        assert sum(row["Num"] for row in app.state.store.profile().state["tables"]["crystals"]) == 3
        _, game = post(client, request(app.state.store, 35, ()))
        assert game["A6"].values == (1,)
    reopened = case.application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        _, repeated = post(client, login(reopened.state.store, 1004))
        assert set(repeated) == {"SYSTEM", "PGP2"}
        assert reopened.state.store.profile().state["login_rewards"]["beginner_claimed"] == 1


def test_old_zero_account_does_not_receive_new_account_bonus(factory):
    case = factory(legacy=True)
    app = case.application()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, result = post(client, login(app.state.store))
        assert "BBNS" not in result
        state = app.state.store.profile().state
        assert state["login_rewards"]["baseline_source"] == "legacy"
        assert [row["Type"] for row in state["tables"]["presents"]] == [16]


def test_old_progress_survives_then_changes_month_at_four(factory):
    now = datetime(2026, 10, 31, 12, tzinfo=JST)
    def prior(state):
        state["tables"]["game_param2"][0].update(
            LastLoginDate=date(2026, 10, 30).toordinal() - 1, LogInStampCount=5)
    case = factory(now, legacy=True, mutate=prior)
    app = case.application()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, response = post(client, login(app.state.store))
        assert pairs(response["PGP2"])["A6"] == "6" and "BBNS" not in response
        count = len(app.state.store.profile().state["tables"]["presents"])
        case.clock.now = datetime(2026, 11, 1, 3, 59, 59, 999999, tzinfo=JST)
        _, response = post(client, login(app.state.store, 1001))
        assert "PPT" not in response and pairs(response["PGP2"])["A6"] == "6"
        case.clock.now = datetime(2026, 11, 1, 4, tzinfo=JST)
        _, response = post(client, login(app.state.store, 1002))
        assert pairs(response["PGP2"])["A6"] == "1"
        assert len(app.state.store.profile().state["tables"]["presents"]) == count + 1


def test_seven_beginner_days_stop_without_stopping_normal_stamps(factory):
    case = factory()
    app = case.application()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for index in range(8):
            case.clock.now = NOW + timedelta(days=index)
            _, result = post(client, login(app.state.store, 1000 + index))
            assert ("BBNS" in result) == (index < 7)
        state = app.state.store.profile().state
        assert state["login_rewards"]["beginner_claimed"] == 7
        assert state["tables"]["game_param2"][0]["LogInStampCount"] == 8
        assert sum(row["Description"] == "初心者ログインボーナス" for row in state["tables"]["presents"]) == 7


def test_skipped_days_advance_once(factory):
    case = factory()
    app = case.application()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, login(app.state.store))
        case.clock.now += timedelta(days=13)
        _, result = post(client, login(app.state.store, 1001))
        assert pairs(result["PGP2"])["A6"] == "2"
        assert app.state.store.profile().state["login_rewards"]["beginner_claimed"] == 2


def test_missing_month_holds_the_stamp_but_commits_the_login_day(factory):
    case = factory(datetime(2027, 1, 1, 12, tzinfo=JST))
    app = case.application()
    before = app.state.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, result = post(client, login(app.state.store))
        assert pairs(result["PGP2"])["A6"] == "1"
    after = app.state.store.profile()
    assert after.revision == before.revision + 1
    [held] = read_pending_stamps(after.state)
    assert (held["day"], held["ordinal"], held["reason"]) == ("2027-01-01", 1, "login_reward_month_unavailable")


def test_actual_crystal_recovery_is_carried_into_next_month_stamp_grade(factory):
    def wallet(state):
        state["tables"]["crystals"] = [{"FreeFlag": True, "Date": 1, "Num": 5}]
    case = factory(datetime(2026, 10, 31, 12, tzinfo=JST), mutate=wallet)
    app = case.application()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, login(app.state.store))
        _, recovered = post(client, request(app.state.store, 85, (), tick=1001))
        assert pairs(recovered["PGP2"])["C1"] == "1"
        assert app.state.store.profile().state["crystal_spending"]["current_spent"] == 1
        case.clock.now = datetime(2026, 11, 1, 4, tzinfo=JST)
        _, logged_in = post(client, login(app.state.store, 1002))
        assert pairs(logged_in["PGP2"])["C1"] == str(1 << 16)
        assert pairs(logged_in["PGP2"])["A6"] == "1"
        _, loaded = post(client, request(app.state.store, 35, ()))
        assert loaded["C1"].values == (1 << 16,)


def test_all_current_normal_stamp_months_accept_every_real_calendar_day(factory):
    start = datetime(2026, 10, 1, 12, tzinfo=JST)
    case = factory(start)
    app = case.application()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for offset in range(92):
            case.clock.now = start + timedelta(days=offset)
            _, result = post(client, login(app.state.store, 1000 + offset))
            assert pairs(result["PGP2"])["A6"] == str(case.clock.now.day)
            assert ("BBNS" in result) == (offset < 7)
        state = app.state.store.profile().state
        assert state["login_rewards"]["last_claim_day"] == "2026-12-31"
        assert state["login_rewards"]["stamp_count"] == 31
        assert len(state["tables"]["presents"]) == 99
        assert state["login_rewards"]["beginner_claimed"] == 7
