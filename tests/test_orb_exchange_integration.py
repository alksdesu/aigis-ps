from argparse import Namespace
import pytest
from fastapi.testclient import TestClient

from aigis_ps import cli
from aigis_ps.alxml import Column
from aigis_ps.app import create_app
from aigis_ps.errors import ProtocolError
from test_evolution_catalog import materials, unit
from test_evolution_integration import message as evolve, post, seed
from test_quest_service import bundle, request


SOURCE = "evolve_orb_100001"
TARGET = "evolve_orb_100002"


def exchange(store, target=TARGET, source=SOURCE, tick=1000):
    return request(store, 139, (Column("ITEM", "S", (target,)), Column("BASE", "S", (source,))), tick=tick)


def application(bundle, path, state):
    return create_app(path, initial_state=bundle.initialize(state), state_initializer=bundle.initialize,
        handlers=bundle.orb_exchange.handlers() | bundle.evolution.handlers() | bundle.bootstrap.handlers())


def stock(state):
    return {row["ItemID"]: row["Count"] for row in state["tables"]["possessions"]}


def test_native_exchange_reloads_zero_source_and_replays_after_restart(bundle, tmp_path):
    assert bundle.orb_exchange is not None, bundle.orb_exchange_error
    state = bundle.bootstrap.initial_state()
    state["tables"]["game_param"][0]["Money"] = 100000
    state = bundle.possessions.credit(state, {SOURCE: 5, "gift_0002_ruby": 7})
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        envelope = exchange(app.state.store)
        wire, response = post(client, envelope)
        saved = app.state.store.profile()
        assert set(response) == {"SYSTEM", "PGP", "POSSESSION"}
        assert response["PGP"].children == (Column("A1", "I", (85000,)),)
        assert response["POSSESSION"].children[0].values == (SOURCE, TARGET)
        assert response["POSSESSION"].children[1].values == (0, 1)
        assert saved.state["player_state"] == before.state["player_state"]
        assert stock(saved.state) == {SOURCE: 0, TARGET: 1, "gift_0002_ruby": 7}
        assert post(client, envelope)[0] == wire and app.state.store.profile() == saved
    reopened = application(bundle, tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert reopened.state.store.profile() == saved
        _, configuration = post(client, request(reopened.state.store, 137, ()))
        possession = configuration["POSSESSION"]
        assert dict(zip(possession.children[0].values, possession.children[1].values)) == stock(saved.state)


def test_exchange_cannot_spend_again_after_another_operation_changes_the_save(bundle, tmp_path):
    state = bundle.bootstrap.initial_state()
    state["tables"]["game_param"][0]["Money"] = 100000
    state = bundle.possessions.credit(state, {SOURCE: 10})
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = exchange(app.state.store)
        post(client, envelope)
        current = app.state.store.profile()
        updated = bundle.possessions.credit(current.state, {"gift_0002_ruby": 1})
        app.state.store.update_state(updated, current.revision)
        before = app.state.store.profile()
        post(client, envelope, 409)
        assert app.state.store.profile() == before


def test_exchanged_orb_is_consumed_by_actual_first_awakening(bundle, tmp_path):
    catalog = bundle.evolution.catalog
    target = unit(catalog, level=999)
    state, rows = seed(bundle, target, materials(catalog, target, 7))
    awakening = catalog.quote(rows[0], rows[1:], 7)
    required = dict(awakening.items)
    orb = next(name for name in required if name.startswith("evolve_orb_100"))
    count = required.pop(orb)
    source = next(row.item_id for row in bundle.orb_exchange.definitions if row.item_id != orb)
    state = bundle.possessions.credit(state, {**required, source: count * 5})
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for index in range(count):
            post(client, exchange(app.state.store, orb, source, tick=1000 + index))
        assert stock(app.state.store.profile().state)[orb] == count
        _, response = post(client, evolve(app.state.store, rows[0], rows[1:], tick=2000))
        saved = app.state.store.profile().state
        assert saved["player_state"]["units"][1]["ClassChangeCount"] == 2
        assert stock(saved)[orb] == 0
        assert saved["tables"]["game_param"][0]["Money"] == 1000000 - count * 15000 - awakening.price
        possession = response["POSSESSION"]
        assert dict(zip(possession.children[0].values, possession.children[1].values))[orb] == 0


def test_cli_registers_exchange_without_starting_a_listener(bundle, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "services", lambda _: bundle)
    monkeypatch.setattr(cli, "client_fingerprint", lambda _: bundle.masters.expected_hash)
    monkeypatch.setattr("aigis_ps.app.create_app", lambda *args, **kwargs: captured.update(kwargs))
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    assert cli.main(["serve", "--data-dir", str(tmp_path / "unused")]) == 0
    assert captured["handlers"][139] == bundle.orb_exchange.exchange
    assert captured["status_provider"]()["orb_exchange"]["implemented_operations"] == [139]
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("unavailable", ["training", "orb_exchange"])
def test_exchange_dependency_and_failure_reporting_are_independent(bundle, monkeypatch, unavailable):
    def fail(*args, **kwargs):
        raise ProtocolError("fixture_resource_invalid", "Invalid fixture resource", 503)
    name = "aigis_ps.training_catalog.TrainingCatalog" if unavailable == "training" else "aigis_ps.orb_exchange.OrbExchangeService"
    monkeypatch.setattr(name, fail)
    args = Namespace(asset_dir=cli.DEFAULT_ASSETS, data_dir=cli.DEFAULT_DATA, resource_dir=cli.DEFAULT_RESOURCES, rules=None)
    updated = cli.services(args)
    if unavailable == "training":
        assert updated.training is None
        assert updated.orb_exchange.status()["ready"] is True
    else:
        assert updated.training is not None
        assert updated.orb_exchange is None
        assert cli.service_readiness(updated)["orb_exchange"] == {"available": False, "error": "fixture_resource_invalid"}
