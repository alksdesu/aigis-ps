import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.barracks_service import EXTRA_SHIFT, PURCHASE_FIELD
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import BARRACKS_BITS, BARRACKS_SHIFT
from test_evolution_integration import post
from test_evolution_service import columns
from test_quest_service import bundle, request
from test_training_service import case, mutate, fails


def configure(case, indices=(1, 2)):
    case.service.barracks = case.bundle.unit_capacity
    def update(state):
        state["tables"]["config"][0][PURCHASE_FIELD] = 2 << EXTRA_SHIFT
        for saved, index in zip(state["player_state"]["units"][2:], indices):
            saved["GeneralPurposeFlags"] = (saved["GeneralPurposeFlags"] & ~BARRACKS_BITS) | index << BARRACKS_SHIFT
    mutate(case, update)
    return [{**unit, "StorageIndex": index} for unit, index in zip(case.units[1:], indices)]


def message(store, target, materials, tick=1000):
    return request(store, 5, (Container("BASE", columns((target,))), Container("APPEND", columns(materials)),
        Container("CMB", (Column("A1", "I", (0,)), Column("A7", "I", (0,)), Column("B0", "I", (0,))))), tick=tick)


@pytest.mark.parametrize("indices", [(0, 1), (1, 2), (2, 2)])
def test_training_consumes_materials_from_verified_extra_barracks(case, indices):
    selected = configure(case, indices)
    before = case.store.profile()
    envelope = message(case.store, case.units[0], selected)
    result = case.service.integrate(envelope, case.store)
    saved = case.store.profile()
    assert len(saved.state["player_state"]["units"]) == 2
    assert saved.state["player_state"]["units"][1]["Exp"] == 1000
    assert saved.state["tables"]["game_param"][0]["Money"] == 600
    assert saved.state["tables"]["config"] == before.state["tables"]["config"]
    assert saved.state["player_state"]["battle"] == before.state["player_state"]["battle"]
    assert case.service.integrate(envelope, case.store) == result
    assert case.store.profile() == saved


@pytest.mark.parametrize("requested", [0, 2, 3, 15])
def test_training_rejects_stale_material_locations(case, requested):
    selected = configure(case, (1, 1))
    selected[0]["StorageIndex"] = requested
    fails(case, message(case.store, case.units[0], selected), "training_storage_mismatch")


def test_training_does_not_consume_unpurchased_barracks(case):
    selected = configure(case)
    mutate(case, lambda state: state["tables"]["config"][0].update({PURCHASE_FIELD: 0}))
    fails(case, message(case.store, case.units[0], selected), "unit_capacity_barracks_unsupported")


@pytest.mark.parametrize("protection", ["locked", "organized"])
def test_training_preserves_extra_material_protection(case, protection):
    selected = configure(case)
    def update(state):
        if protection == "locked":
            state["player_state"]["units"][2]["GeneralPurposeFlags"] |= 2
        else:
            state["player_state"]["squads"].append(
                {"Address": 0, "UnitIndex": selected[0]["GetIndex"], "Name": "old", "Flags": 0})
    mutate(case, update)
    fails(case, message(case.store, case.units[0], selected), "training_material_protected")


def test_training_target_remains_primary_and_capacity_changes_invalidate_replay(case, monkeypatch):
    selected = configure(case)
    fails(case, message(case.store, {**case.units[0], "StorageIndex": 1}, selected), "training_storage_unsupported")
    envelope = message(case.store, case.units[0], selected)
    case.service.integrate(envelope, case.store)
    monkeypatch.setattr(case.service.barracks, "content_version", "b" * 64)
    fails(case, envelope, "training_retry_stale")


def test_training_cas_retry_rechecks_material_location(case, monkeypatch):
    selected = configure(case, (1, 1))
    original = case.store.update_state
    first = True
    def save(state, revision):
        nonlocal first
        if first:
            first = False
            current = case.store.profile()
            current.state["player_state"]["units"][2]["GeneralPurposeFlags"] = 0x21
            original(current.state, current.revision)
            raise ValueError("Concurrent transfer")
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", save)
    with pytest.raises(ProtocolError) as caught:
        case.service.integrate(message(case.store, case.units[0], selected), case.store)
    assert caught.value.code == "training_storage_mismatch"
    saved = case.store.profile().state
    assert len(saved["player_state"]["units"]) == 4
    assert saved["tables"]["game_param"][0]["Money"] == 1000
    assert "training_history" not in saved


def test_native_transfer_training_and_roster_reload_use_the_same_instances(bundle, tmp_path):
    state = bundle.initialize(bundle.bootstrap.initial_state())
    state, units = bundle.player_state.credit_units(state, [
        {"card_id": 7, "level": 1, "count": 1}, {"card_id": 2, "level": 1, "count": 2}])
    state["tables"]["config"][0][PURCHASE_FIELD] = 2 << EXTRA_SHIFT
    state["tables"]["game_param"][0]["Money"] = 1000000
    app = create_app(tmp_path, initial_state=bundle.initialize(state), state_initializer=bundle.initialize,
        handlers=bundle.training.handlers() | bundle.barracks.handlers() | bundle.player_state.handlers())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, request(app.state.store, 118, (Column("DEST", "I", (2,)), Container("BASE", (
            Column("A7", "I", tuple(unit["GetIndex"] for unit in units[1:])),
            Column("AB", "I", tuple(unit["UniqueID"] for unit in units[1:])))))))
        selected = [{**unit, "StorageIndex": 2} for unit in units[1:]]
        envelope = message(app.state.store, units[0], selected, tick=1001)
        wire, response = post(client, envelope)
        saved = app.state.store.profile()
        assert len(saved.state["player_state"]["units"]) == 2
        assert saved.state["player_state"]["units"][1]["Exp"] > 0
        assert bundle.unit_capacity.usage(saved.state).counts == (2, 0, 0)
        assert post(client, envelope)[0] == wire and app.state.store.profile() == saved
        _, listing = post(client, request(app.state.store, 41, ()))
        assert listing["A7"].values == (0, units[0]["GetIndex"])
