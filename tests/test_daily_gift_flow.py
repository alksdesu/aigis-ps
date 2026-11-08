from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps import cli
from aigis_ps.alxml import Column
from aigis_ps.app import create_app
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.daily_gift_state import read_daily_gifts
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.login_reward_service import LoginRewardService
import aigis_ps.login_reward_service as login_module
from aigis_ps.rank_rewards import RankRewardService
import aigis_ps.rank_rewards as rank_module
from test_evolution_integration import post
from test_present_recovery_integration import claim
from test_quest_service import request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
PROJECT = Path(__file__).resolve().parents[1]
GIFT_IDS = (8, 17, 114, 319)


@pytest.fixture(scope="module")
def configured_bundle(tmp_path_factory):
    path = tmp_path_factory.mktemp("daily-gift-flow") / "rules.json"
    path.write_text(json.dumps({"gift_ids": GIFT_IDS, "revivals": {
        str(key): {"start_date": "2026-11-01", "end_date": "2026-11-03"} for key in GIFT_IDS}}), encoding="utf8")
    bundle = cli.services(Namespace(asset_dir=cli.DEFAULT_ASSETS, data_dir=cli.DEFAULT_DATA,
        resource_dir=cli.DEFAULT_RESOURCES, rules=None, daily_gift_rules=path))
    assert bundle.daily_gifts is not None, bundle.daily_gift_error
    assert bundle.login_rewards is not None, bundle.login_reward_error
    assert bundle.rank_rewards is not None, bundle.rank_reward_error
    return bundle


@pytest.fixture
def case(configured_bundle, tmp_path, monkeypatch):
    bundle = configured_bundle
    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr(bundle.bootstrap, "clock", lambda: clock.now)
    monkeypatch.setattr(bundle.login_rewards, "clock", lambda: clock.now)
    initial = bundle.initialize(bundle.bootstrap.initial_state())
    handlers = (bundle.bootstrap.handlers() | bundle.presents.handlers()
                | bundle.login_rewards.handlers() | bundle.rank_rewards.handlers())

    def application():
        return create_app(tmp_path / "save", initial_state=initial, state_initializer=bundle.initialize,
                          handlers=handlers)

    app = application()
    return SimpleNamespace(bundle=bundle, clock=clock, app=app, store=app.state.store, application=application)


def login(case, operation=115, tick=1000):
    columns = (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,)))
    if operation == 116:
        columns += (Column("DmmPremiumReceiverId", "I", (0,)),)
    return request(case.store, operation, columns, tick=tick)


def service(case, operation):
    return case.bundle.login_rewards if operation == 115 else case.bundle.rank_rewards


@pytest.mark.parametrize("operation", (115, 116))
def test_multiple_gifts_reach_mail_without_asset_credit_and_replay_after_restart(case, operation):
    before = case.store.profile()
    envelope = login(case, operation)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        wire, reply = post(client, envelope)
        saved = case.store.profile()
        ledger = read_daily_gifts(saved.state)["gifts"]
        assert set(ledger) == {str(key) for key in GIFT_IDS}
        assert ledger["114"]["2026-11-01"]["status"] == "pending"
        for key in (8, 17, 319):
            assert ledger[str(key)]["2026-11-01"]["status"] == "delivered"
        mails = {row["PresentID"]: row for row in case.bundle.presents.rows(saved.state)}
        daily = [mails[ledger[str(key)]["2026-11-01"]["mail_ids"][0]] for key in (8, 17, 319)]
        assert [(row["Type"], row["Content"]) for row in daily] == [(0, 2), (1, 6), (22, 1)]
        assert daily[2]["ItemID"] == "item_cook_150010"
        assert saved.state["player_state"] == before.state["player_state"]
        assert saved.state["tables"]["crystals"] == before.state["tables"]["crystals"]
        assert "EBNS" not in reply or not set(GIFT_IDS).intersection(reply["EBNS"].children[0].values)
        assert post(client, envelope)[0] == wire and case.store.profile() == saved
    reopened = case.application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
    assert reopened.state.store.profile() == saved


def test_four_am_dates_include_last_day_without_absence_backfill(case):
    case.clock.now = NOW.replace(hour=3, minute=59)
    initial = case.bundle.initialize(case.bundle.bootstrap.initial_state())
    case.store.update_state(initial, case.store.profile().revision)
    service(case, 115).receive(login(case), case.store)
    assert "daily_gift" not in case.store.profile().state
    case.clock.now = NOW.replace(hour=4)
    service(case, 116).receive(login(case, 116, 1001), case.store)
    service(case, 115).receive(login(case, 115, 1002), case.store)
    case.clock.now = NOW + timedelta(days=2)
    service(case, 115).receive(login(case, 115, 1003), case.store)
    case.clock.now += timedelta(days=1)
    service(case, 116).receive(login(case, 116, 1004), case.store)
    for records in read_daily_gifts(case.store.profile().state)["gifts"].values():
        assert set(records) == {"2026-11-01", "2026-11-03"}


def test_daily_gift_claims_credit_crystals_units_and_cooking_once(case):
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, login(case))
        saved = case.store.profile()
        ledger = read_daily_gifts(saved.state)["gifts"]
        ids = {key: ledger[str(key)]["2026-11-01"]["mail_ids"][0] for key in (8, 17, 319)}
        crystals = wallet_totals(saved.state)
        post(client, claim(case.store, 1, [ids[8]], tick=1001))
        assert wallet_totals(case.store.profile().state)["free"] == crystals["free"] + 2
        _, units = post(client, claim(case.store, 1, [ids[17]], tick=1002))
        assert units["PPU"].children[0].values
        before = case.store.profile().state["tables"]["game_param"][0]["NowCharisma"]
        envelope = claim(case.store, 1, [ids[319]], tick=1003)
        wire, cooking = post(client, envelope)
        assert "PGP" in cooking
        after = case.store.profile()
        assert after.state["tables"]["game_param"][0]["NowCharisma"] > before
        assert post(client, envelope)[0] == wire and case.store.profile() == after
        post(client, login(case, 116, 1004))
        assert case.store.profile().state["tables"]["game_param"][0]["NowCharisma"] == after.state["tables"]["game_param"][0]["NowCharisma"]
        assert set(read_daily_gifts(case.store.profile().state)["gifts"]["319"]) == {"2026-11-01"}


@pytest.mark.parametrize("operation,level", [(115, n) for n in range(1, 5)] + [(116, n) for n in range(1, 6)])
def test_every_prior_receipt_schema_survives_adding_daily_gifts(case, operation, level, monkeypatch):
    bundle = case.bundle
    options = {}
    if operation == 115:
        constructor = LoginRewardService
        if level >= 2:
            options["event_logins"] = bundle.event_logins
        if level >= 3:
            options["comeback_logins"] = bundle.comeback_logins
        if level >= 4:
            options["purchased_logins"] = bundle.purchased_logins
    else:
        constructor = RankRewardService
        for threshold, name in ((2, "event_passes"), (3, "event_logins"), (4, "comeback_logins"), (5, "purchased_logins")):
            if level >= threshold:
                options[name] = getattr(bundle, name)
    args = (bundle.bootstrap, bundle.player_state, bundle.presents, service(case, operation).catalog)
    old = constructor(*args, **options)
    envelope = login(case, operation)
    wire = old.receive(envelope, case.store)
    assert "daily_gift" not in case.store.profile().state
    current = constructor(*args, **options, daily_gifts=bundle.daily_gifts)
    snapshot = case.store.profile()
    def unexpected(*args, **kwargs):
        raise AssertionError("Exact replay must not settle a new day")
    monkeypatch.setattr(bundle.daily_gifts, "settle", unexpected)
    monkeypatch.setattr(bundle.bootstrap, "clock", unexpected)
    if operation == 115:
        monkeypatch.setattr(current, "clock", unexpected)
    assert current.receive(envelope, case.store) == wire
    assert case.store.profile() == snapshot


@pytest.mark.parametrize("operation", (115, 116))
def test_daily_only_receipt_preserves_empty_optional_sources(case, operation):
    bundle = case.bundle
    constructor = LoginRewardService if operation == 115 else RankRewardService
    current = constructor(bundle.bootstrap, bundle.player_state, bundle.presents,
                          service(case, operation).catalog, daily_gifts=bundle.daily_gifts)
    envelope = login(case, operation)
    result = current.receive(envelope, case.store)
    saved = case.store.profile()
    assert current.receive(envelope, case.store) == result and case.store.profile() == saved
    assert b"EBNS" not in result


@pytest.mark.parametrize("operation", (115, 116))
def test_late_daily_failure_rolls_back_every_reward_source(case, operation, monkeypatch):
    before = case.store.profile()
    def broken(*args, **kwargs):
        raise ProtocolError("daily_gift_state_invalid", "Injected invalid state", 503)
    monkeypatch.setattr(case.bundle.daily_gifts, "settle", broken)
    with pytest.raises(ProtocolError):
        service(case, operation).receive(login(case, operation), case.store)
    assert case.store.profile() == before


@pytest.mark.parametrize("operation", (115, 116))
def test_rehashed_receipt_cannot_reassign_daily_reward_source(case, operation):
    current = service(case, operation)
    envelope = login(case, operation)
    current.receive(envelope, case.store)
    saved = case.store.profile()
    state = deepcopy(saved.state)
    prefix, module = ("login_reward", login_module) if operation == 115 else ("rank_reward", rank_module)
    state[prefix + "_delivery"]["daily_gift_grants"][0]["gift_id"] = 9
    receipt = state[prefix + "_history"]["receipts"][-1]
    if operation == 115:
        receipt["sources"]["daily_gift_grants"][0]["gift_id"] = 9
    receipt["state"] = module._state_digest(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    before = case.store.profile()
    with pytest.raises(ProtocolError):
        current.receive(envelope, case.store)
    assert case.store.profile() == before


def test_cli_wires_shared_daily_service_and_rejects_missing_state_owner(case):
    bundle = case.bundle
    assert bundle.login_rewards.daily_gifts is bundle.daily_gifts
    assert bundle.rank_rewards.daily_gifts is bundle.daily_gifts
    assert bundle.daily_gifts.rules.gift_ids == GIFT_IDS
    assert cli.service_readiness(bundle)["daily_gifts"]["ready"] is True
    state = deepcopy(case.store.profile().state)
    state["daily_gift"] = None
    broken = replace(bundle, daily_gifts=None)
    with pytest.raises(ProtocolError, match="Existing daily gift"):
        broken.initialize(state)
    assert cli.service_readiness(broken)["startup_ready"] is False


def test_doctor_transports_daily_configuration_without_starting_service(case, tmp_path, monkeypatch, capsys):
    observed = []
    monkeypatch.setattr(cli, "services", lambda args: observed.append(args) or case.bundle)
    path = PROJECT / "config/daily-gift-rules.example.json"
    assert cli.main(["doctor", "--daily-gift-rules", str(path), "--data-dir", str(tmp_path)]) == 0
    assert observed[0].daily_gift_rules == path
    assert '"daily_gifts"' in capsys.readouterr().out


def test_concurrent_login_paths_commit_daily_rewards_only_once(case, monkeypatch):
    original = case.store.update_state
    barrier, lock = Barrier(2), Lock()
    count = 0

    def raced(state, revision):
        nonlocal count
        with lock:
            count += 1
            wait = count <= 2
        if wait:
            barrier.wait(timeout=20)
        return original(state, revision)

    monkeypatch.setattr(case.store, "update_state", raced)
    requests = {number: login(case, number) for number in (115, 116)}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service(case, number).receive, requests[number], case.store) for number in (115, 116)]
        assert all(future.result(timeout=30) for future in futures)
    ledger = read_daily_gifts(case.store.profile().state)["gifts"]
    assert all(set(records) == {"2026-11-01"} for records in ledger.values())
    ids = [identifier for records in ledger.values() for record in records.values() for identifier in record["mail_ids"]]
    assert len(ids) == len(set(ids)) == 3


@pytest.mark.parametrize("operation", (115, 116))
def test_capacity_defers_one_daily_reward_without_losing_other_sources(case, operation, monkeypatch):
    current = service(case, operation)
    name = "_preflight" if operation == 115 else "_preflight_dispatch"
    original = getattr(current, name)

    def limited(profile, state, *args, **kwargs):
        record = read_daily_gifts(state)["gifts"].get("8", {}).get("2026-11-01", {})
        if record.get("status") == "delivered":
            raise ProtocolError("login_reward_response_capacity", "Injected response capacity", 503)
        return original(profile, state, *args, **kwargs)

    monkeypatch.setattr(current, name, limited)
    current.receive(login(case, operation), case.store)
    first = read_daily_gifts(case.store.profile().state)["gifts"]
    assert first["8"]["2026-11-01"]["status"] == "pending"
    assert first["17"]["2026-11-01"]["status"] == "delivered"
    assert first["319"]["2026-11-01"]["status"] == "delivered"
    monkeypatch.setattr(current, name, original)
    current.receive(login(case, operation, 1001), case.store)
    second = read_daily_gifts(case.store.profile().state)["gifts"]
    assert second["8"]["2026-11-01"]["status"] == "delivered"
    assert second["17"] == first["17"] and second["319"] == first["319"]
