from copy import copy, deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import loads
from aigis_ps.crystal_spending import read_spending
from aigis_ps.energy_clock import JST
from aigis_ps.login_cooking_state import read_cooking
from aigis_ps.login_reward_service import LoginRewardRules, LoginRewardService
from aigis_ps.monthly_capacity import grant_next_capacity, read_capacity
from aigis_ps.startup_diagnostics import StartupDiagnostics
from aigis_ps.storage import Store
from test_quest_service import bundle, request


@pytest.fixture
def case(bundle, tmp_path):
    now = datetime(2026, 11, 30, 12, tzinfo=JST)
    bootstrap = copy(bundle.bootstrap)
    clock = SimpleNamespace(now=now)
    bootstrap.clock = lambda: clock.now
    bootstrap.login_reward_seed_provider = None
    state = bootstrap.initial_state()
    row = state["tables"]["game_param2"][0]
    day = now.date() - timedelta(days=1)
    row.update(StampCardGrade=(280 << 16) | 625, LastLoginDate=day.toordinal() - 1,
               LogInStampCount=29, HomeCookingReceivedAmount=1, HomeCookingReceivedDate=day.toordinal() - 1)
    importer = LoginRewardService(bootstrap, bundle.player_state, bundle.presents, bundle.login_rewards.catalog,
        rules=LoginRewardRules(legacy_spending_period="2026-11", legacy_cooking_period="2026-11"))
    state = bundle.initialize(importer.initialize(state, now))
    state = grant_next_capacity(state, 20, now)
    store = Store(tmp_path / "monthly.sqlite3", state)
    clock.now = datetime(2026, 12, 1, 4, tzinfo=JST)
    return SimpleNamespace(bootstrap=bootstrap, clock=clock, store=store, original=deepcopy(state))


def test_production_monthly_provider_settles_three_ledgers_once(case):
    original = case.store.profile()
    case.bootstrap.game_param2(request(case.store, 35, ()), case.store)
    saved = case.store.profile()
    assert saved.revision == original.revision + 1
    spend, cook, capacity = read_spending(saved.state), read_cooking(saved.state), read_capacity(saved.state)
    assert spend["period"] == cook["period"] == capacity["period"] == "2026-12"
    assert (spend["previous_spent"], spend["current_spent"]) == (625, 0)
    assert (cook["quota"], cook["issued"]) == (25, 0)
    assert (capacity["current"], capacity["next"]) == (20, 0)
    assert saved.state["tables"]["game_param2"][0]["HomeCookingReceivedDate"] == case.original["tables"]["game_param2"][0]["HomeCookingReceivedDate"]
    assert saved.state["tables"]["presents"] == original.state["tables"]["presents"]
    assert saved.state["login_rewards"] == original.state["login_rewards"]
    case.clock.now += timedelta(seconds=5)
    case.bootstrap.game_param2_fields(request(case.store, 33, ()), case.store)
    case.bootstrap.game_param(request(case.store, 34, ()), case.store)
    assert case.store.profile() == saved


def test_cooking_failure_serves_the_unsettled_month_and_rolls_back_every_ledger(case):
    current = case.store.profile()
    damaged = deepcopy(current.state)
    damaged["tables"]["game_param2"][0]["HomeCookingReceivedAmount"] += 1
    case.store.update_state(damaged, current.revision)
    before = case.store.profile()
    case.bootstrap.startup_diagnostics = StartupDiagnostics()
    loaded = loads(case.bootstrap.handlers()[35](request(case.store, 35, ()), case.store))
    stored = before.state["tables"]["game_param2"][0]
    assert {column.name: column.values for column in loaded.children}["CC"] == (stored["HomeCookingReceivedAmount"],)
    assert case.store.profile() == before
    assert read_spending(before.state)["period"] == read_capacity(before.state)["period"] == "2026-11"
    issues = case.bootstrap.status()["startup_diagnostics"]["loads"]["35"]["issues"]
    assert [issue["code"] for issue in issues] == ["login_cooking_invalid"]


def test_legacy_event_flags_settle_the_month_and_convert_on_33(case):
    current = case.store.profile()
    legacy = deepcopy(current.state)
    legacy["tables"]["game_param2"][0].update(_EvCmb=0, LookHarlemFlag00=1)
    case.store.update_state(legacy, current.revision)
    case.bootstrap.startup_diagnostics = StartupDiagnostics()
    handlers = case.bootstrap.handlers()
    handlers[34](request(case.store, 34, ()), case.store)
    settled = case.store.profile()
    assert read_spending(settled.state)["period"] == read_capacity(settled.state)["period"] == "2026-12"
    loaded = loads(handlers[35](request(case.store, 35, ()), case.store))
    assert {column.name: column.values for column in loaded.children}["C7"] == (0,)
    response = loads(handlers[33](request(case.store, 33, ()), case.store))
    group = next(child for child in response.children if child.name == "PGP2")
    values = dict(zip(group.children[0].values, group.children[1].values))
    assert values["C7"] == "1" and values["AD"][15] == "1" and values["AD"].count("1") == 1
    converted = case.store.profile()
    assert converted.revision == settled.revision + 1
    assert converted.state["tables"]["game_param2"][0]["_EvCmb"] == 1
    assert case.bootstrap.status()["startup_diagnostics"]["loads"] == {}
