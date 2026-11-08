from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from aigis_ps import cli
from aigis_ps.app import create_app
from aigis_ps.business_calendar import civil_day
from aigis_ps.crystal_wallet import credit_free_crystals, credit_paid_crystals, wallet_totals
from aigis_ps.quest_service import QuestService
from aigis_ps.recovery_service import RecoveryService
from test_evolution_integration import post
from test_quest_service import bundle, request, start_request, exit_request
from test_recovery_service import anchor_charisma


NOW = datetime(2026, 11, 1, 12)


def application(bundle, path, state):
    recovery = RecoveryService(bundle.bootstrap, recovery_seconds=bundle.quests.rules.charisma_recovery_seconds, clock=lambda: NOW)
    quests = QuestService(bundle.quests.catalog, bundle.bootstrap, bundle.player_state, bundle.possessions,
        rules=bundle.quests.rules, clock=lambda: NOW)
    return create_app(path, initial_state=bundle.initialize(state), state_initializer=bundle.initialize,
        handlers=bundle.bootstrap.handlers() | recovery.handlers() | quests.handlers())


def test_native_recovery_then_a_retreated_quest_and_reopen_use_one_energy_balance(bundle, tmp_path, monkeypatch):
    monkeypatch.setattr(bundle.bootstrap, "clock", lambda: NOW)
    state = bundle.bootstrap.initial_state()
    anchor = NOW - timedelta(minutes=6)
    anchor_charisma(state, 5, anchor)
    state = credit_free_crystals(state, 1, civil_day(NOW))
    state = credit_paid_crystals(state, 1, civil_day(NOW))
    amount = bundle.bootstrap.rank_rows[0]["CharismaMax"]
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = request(app.state.store, 85, ())
        wire, response = post(client, envelope)
        saved = app.state.store.profile()
        assert set(response) == {"SYSTEM", "PPC", "PFC", "PGP", "PGP2"}
        assert saved.state["tables"]["game_param"][0]["NowCharisma"] == 7 + amount
        assert wallet_totals(saved.state) == {"free": 0, "paid": 1, "total": 1}
        assert response["PFC"].children[2].values == (0,)
        assert response["PPC"].children[2].values == (1,)
        assert post(client, envelope)[0] == wire and app.state.store.profile() == saved
        # Only a successful result charges a battle, so the start and the retreat leave the recovered balance.
        post(client, start_request(bundle.quests, app.state.store, tick=1001))
        assert app.state.store.profile().state["tables"]["game_param"][0]["NowCharisma"] == 7 + amount
        post(client, exit_request(app.state.store))
        ended = app.state.store.profile()
        post(client, envelope, 409)
        assert app.state.store.profile() == ended
    reopened = application(bundle, tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        _, loaded = post(client, request(reopened.state.store, 34, ()))
        assert loaded["A4"].values == (7 + amount,)
        _, paid = post(client, request(reopened.state.store, 43, ()))
        _, free = post(client, request(reopened.state.store, 44, ()))
        assert paid["A2"].values == (1,) and free["A2"].values == (0,)
        assert reopened.state.store.profile() == ended


def test_recovery_exact_retry_survives_rebuilt_service(bundle, tmp_path, monkeypatch):
    monkeypatch.setattr(bundle.bootstrap, "clock", lambda: NOW)
    state = bundle.bootstrap.initial_state()
    anchor_charisma(state, 0, NOW)
    state = credit_free_crystals(state, 1, civil_day(NOW))
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = request(app.state.store, 85, ())
        wire, _ = post(client, envelope)
        saved = app.state.store.profile()
    rebuilt = application(bundle, tmp_path, state)
    with TestClient(rebuilt, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert rebuilt.state.store.profile() == saved


def test_cli_registers_recovery_without_listener(bundle, tmp_path, monkeypatch):
    assert bundle.recovery is not None, bundle.recovery_error
    assert bundle.recovery.recovery_seconds == bundle.quests.rules.charisma_recovery_seconds
    captured = {}
    monkeypatch.setattr(cli, "services", lambda _: bundle)
    monkeypatch.setattr(cli, "client_fingerprint", lambda _: bundle.masters.expected_hash)
    monkeypatch.setattr("aigis_ps.app.create_app", lambda *args, **kwargs: captured.update(kwargs))
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    assert cli.main(["serve", "--data-dir", str(tmp_path / "unused")]) == 0
    assert captured["handlers"][85] == bundle.recovery.recover
    assert captured["status_provider"]()["recovery"]["ready"] is True
    assert not (tmp_path / "unused").exists()
