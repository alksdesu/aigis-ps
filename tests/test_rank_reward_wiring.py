from argparse import Namespace
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps import cli
from aigis_ps.bootstrap import BootstrapService
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.rank_reward_state import read_rank_rewards
from aigis_ps.rank_rewards import RankRewardService
from test_evolution_integration import post
from test_quest_service import bundle, finish_request, request, start_request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


@pytest.fixture
def local(bundle):
    assert bundle.rank_rewards is not None, bundle.rank_reward_error
    bootstrap, presents, quests = copy(bundle.bootstrap), copy(bundle.presents), copy(bundle.quests)
    bootstrap.clock = lambda: NOW
    presents.bootstrap = bootstrap
    # Rank-only: production dispatch sources stay bound to the shared mail service, not this local copy
    rewards = RankRewardService(bootstrap, bundle.player_state, presents, bundle.rank_rewards.catalog)
    quests.bootstrap, quests.rank_rewards, quests.clock = bootstrap, rewards, bootstrap.clock
    quests.randbelow = lambda _: 9999
    bootstrap.present_provider = presents.response
    bootstrap.rank_reward_seed_provider = rewards.seed_new
    result = replace(bundle, bootstrap=bootstrap, presents=presents, rank_rewards=rewards, quests=quests)
    return result


def scenario(local, directory):
    threshold = sum(row["NextExp"] for row in local.bootstrap.rank_rows[:9])
    local.bootstrap.rules = replace(local.bootstrap.rules, rank=9, experience=threshold - 1)
    local.bootstrap.rank_reward_seed_provider = None
    initial = local.initialize(local.bootstrap.initial_state())
    initial["sentinel"] = {"keep": [1]}
    app = create(local, directory, initial)
    return SimpleNamespace(app=app, store=app.state.store, initial=initial)


def create(local, directory, initial):
    from aigis_ps.app import create_app
    return create_app(directory, initial_state=initial, state_initializer=local.initialize,
        handlers=local.bootstrap.handlers() | local.player_state.handlers() | local.presents.handlers()
                 | local.quests.handlers() | local.rank_rewards.handlers())


def begin(local, case, client):
    post(client, start_request(local.quests, case.store))
    selected = case.store.profile().state["quest_state"]["active"]["selected_mask"]
    return finish_request(case.store, selected=selected, obtained=0)


def test_cli_binds_catalog_seed_quest_and_readiness(bundle):
    assert bundle.rank_rewards is not None, bundle.rank_reward_error
    assert len(bundle.rank_rewards.catalog.bonuses) == 118
    assert bundle.bootstrap.rank_reward_seed_provider.__self__ is bundle.rank_rewards
    assert bundle.quests.rank_rewards is bundle.rank_rewards
    assert set(bundle.rank_rewards.handlers()) == {116}
    assert cli.service_readiness(bundle)["rank_rewards"]["ready"] is True


def test_rank_construction_does_not_depend_on_login_reward_availability(monkeypatch):
    project = Path(__file__).resolve().parents[1]
    def unavailable(*args, **kwargs):
        raise ProtocolError("login_fixture_unavailable", "unavailable login fixture", 503)
    monkeypatch.setattr("aigis_ps.login_reward_service.LoginRewardService", unavailable)
    result = cli.services(Namespace(asset_dir=project.parent / "aigisr_exe",
        data_dir=project / "runtime/server", resource_dir=project / "runtime/resources", rules=None))
    assert result.login_rewards is None and result.login_reward_error == "login_fixture_unavailable"
    assert result.rank_rewards is not None and result.rank_reward_error is None
    assert result.rank_rewards.status()["ready"] is True
    assert result.bootstrap.rank_reward_seed_provider.__self__ is result.rank_rewards
    assert result.quests.rank_rewards is result.rank_rewards
    assert set(result.rank_rewards.handlers()) == {116}


def test_bootstrap_uses_one_instant_and_runs_rank_seed_after_login_seed(local):
    calls = []
    def clock():
        calls.append("clock")
        return NOW
    def login_seed(state, now):
        assert now is NOW and "energy_clock" in state
        calls.append("login")
        updated = deepcopy(state)
        updated["test_login_seed"] = True
        return updated
    def rank_seed(state, now):
        assert now is NOW and state["test_login_seed"]
        calls.append("rank")
        return local.rank_rewards.seed_new(state, now)
    local.bootstrap.clock = clock
    local.bootstrap.login_reward_seed_provider = login_seed
    local.bootstrap.rank_reward_seed_provider = rank_seed
    state = local.bootstrap.initial_state()
    assert calls == ["clock", "login", "rank"]
    assert read_rank_rewards(state)["baseline_rank"] == 0


def test_bootstrap_rejects_noncallable_rank_seed_before_reading_assets(tmp_path):
    with pytest.raises(ValueError, match="Rank reward seed provider"):
        BootstrapService(tmp_path, rank_reward_seed_provider=True)


def test_new_account_and_legacy_account_have_distinct_rank_baselines(local):
    current = local.initialize(local.bootstrap.initial_state())
    assert read_rank_rewards(current)["baseline_rank"] == 0
    local.bootstrap.rank_reward_seed_provider = None
    local.bootstrap.rules = replace(local.bootstrap.rules, rank=50,
        experience=sum(row["NextExp"] for row in local.bootstrap.rank_rows[:49]))
    old = local.initialize(local.bootstrap.initial_state())
    ledger = read_rank_rewards(old)
    assert ledger["baseline_rank"] == ledger["observed_rank"] == 50
    assert ledger["entries"] == {}
    assert old["tables"]["presents"] == []


@pytest.mark.parametrize("key", ["rank_rewards", "rank_reward_history", "rank_reward_delivery"])
def test_missing_rank_service_rejects_every_persisted_rank_domain(local, key):
    state = local.initialize(local.bootstrap.initial_state())
    state.pop("rank_rewards", None)
    state[key] = None
    broken = replace(local, rank_rewards=None, rank_reward_error="fixture")
    with pytest.raises(ProtocolError) as caught:
        broken.initialize(state)
    assert caught.value.code == "rank_reward_service_unavailable"


def test_missing_rank_service_blocks_startup_readiness(local):
    broken = replace(local, rank_rewards=None, rank_reward_error="fixture")
    status = cli.service_readiness(broken)
    assert status["rank_rewards"] == {"ready": False, "error": "fixture"}
    assert status["startup_ready"] is False


def test_serve_collects_116_without_creating_a_store_or_listener(bundle, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "services", lambda _: bundle)
    monkeypatch.setattr(cli, "client_fingerprint", lambda _: bundle.masters.expected_hash)
    def create_app(*args, **kwargs):
        captured.update(kwargs)
        return object()
    monkeypatch.setattr("aigis_ps.app.create_app", create_app)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    directory = tmp_path / "not-created"
    assert cli.main(["serve", "--data-dir", str(directory)]) == 0
    assert captured["handlers"][116].__self__ is bundle.rank_rewards
    assert not directory.exists()


def test_quest_rank_crossing_persists_reward_mail_in_its_single_cas(local, tmp_path, monkeypatch):
    case = scenario(local, tmp_path)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        envelope = begin(local, case, client)
        before = case.store.profile()
        observed = []
        apply = local.rank_rewards.apply
        def observe(old, candidate, profile=None):
            observed.append((old["tables"]["game_param"][0]["Rank"],
                             candidate["tables"]["game_param"][0]["Rank"], profile.revision))
            assert candidate["quest_state"]["active"] is None
            assert candidate["mission_state"] != old["mission_state"]
            return apply(old, candidate, profile)
        monkeypatch.setattr(local.rank_rewards, "apply", observe)
        wire, response = post(client, envelope)
        saved = case.store.profile()
        assert observed == [(9, 10, before.revision)]
        assert saved.revision == before.revision + 1
        assert {"SYSTEM", "PGP", "PBP", "PPU", "QR", "REWARD", "TREASURE", "TreasureInfo", "PPT"} <= response.keys()
        game_before = before.state["tables"]["game_param"][0]
        game_after = saved.state["tables"]["game_param"][0]
        pgp = dict(zip(response["PGP"].children[0].values, response["PGP"].children[1].values))
        assert (int(pgp["A1"]), int(pgp["A2"]), int(pgp["A3"])) == (
            game_after["Money"], game_after["Experience"], game_after["Rank"])
        amounts = {column.name: column.values for column in response["REWARD"].children}
        assert amounts == {"EXPERIENCE": (game_after["Experience"] - game_before["Experience"],),
                           "GOLD": (game_after["Money"] - game_before["Money"],)}
        assert len(response["QR"].children) > 0
        assert all(not column.values for column in response["PPU"].children)
        ledger = read_rank_rewards(saved.state)
        assert set(ledger["entries"]) == {"7", "78", "264"}
        assert all(entry["status"] == "delivered" for entry in ledger["entries"].values())
        expected_ids = {identifier for entry in ledger["entries"].values() for identifier in entry["mail_ids"]}
        assert {row["PresentID"] for row in saved.state["tables"]["presents"]} == expected_ids
        assert next(column.values for column in response["PPT"].children if column.name == "PresentID") == tuple(
            row["PresentID"] for row in saved.state["tables"]["presents"])
        assert saved.state["quest_state"]["active"] is None
        assert saved.state["quest_state"]["history"][-1]["response_revision"] == saved.revision
        assert saved.state["sentinel"] == before.state["sentinel"]
        monkeypatch.setattr(local.rank_rewards, "apply", lambda *args: pytest.fail("Quest replay reapplied rank rewards"))
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved


@pytest.mark.parametrize("failure", ["rank_apply", "whole_response"])
def test_rank_hook_or_final_response_failure_rolls_back_whole_quest(local, tmp_path, monkeypatch, failure):
    case = scenario(local, tmp_path)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        envelope = begin(local, case, client)
        before = case.store.profile()
        def fail(*args, **kwargs):
            raise ProtocolError("rank_wiring_failure", "fixture failure", 503)
        if failure == "rank_apply":
            monkeypatch.setattr(local.rank_rewards, "apply", fail)
        else:
            monkeypatch.setattr(local.quests, "_prepare", fail)
        result = post(client, envelope, 503)
        assert result.json()["error"]["code"] == "rank_wiring_failure"
        assert case.store.profile() == before


def test_rank_crossing_retries_cas_without_losing_concurrent_state_or_duplicating_mail(local, tmp_path, monkeypatch):
    case = scenario(local, tmp_path)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        envelope = begin(local, case, client)
        original, attempts = case.store.update_state, []
        def conflict(state, revision):
            attempts.append(revision)
            if len(attempts) == 1:
                profile = case.store.profile()
                candidate = deepcopy(profile.state)
                candidate["sentinel"]["concurrent"] = True
                original(candidate, profile.revision)
                raise ValueError("concurrent mutation")
            return original(state, revision)
        monkeypatch.setattr(case.store, "update_state", conflict)
        post(client, envelope)
        final = case.store.profile().state
        assert len(attempts) == 2
        assert len(final["tables"]["presents"]) == 3
        assert final["sentinel"]["concurrent"] is True
        assert final["quest_state"]["active"] is None
        assert set(read_rank_rewards(final)["entries"]) == {"7", "78", "264"}
