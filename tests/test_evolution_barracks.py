import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.barracks_service import EXTRA_SHIFT, PURCHASE_FIELD
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import BARRACKS_BITS, BARRACKS_SHIFT
from test_evolution_catalog import materials, unit
from test_evolution_integration import message as evolve, post, seed
from test_evolution_service import case, message, mutate, fails
from test_quest_service import bundle, request


def configure(case, indices=(0, 1, 2, 1)):
    case.service.barracks = case.bundle.unit_capacity
    def update(state):
        state["tables"]["config"][0][PURCHASE_FIELD] = 2 << EXTRA_SHIFT
        for saved, index in zip(state["player_state"]["units"][2:], indices):
            saved["GeneralPurposeFlags"] = (saved["GeneralPurposeFlags"] & ~BARRACKS_BITS) | index << BARRACKS_SHIFT
    mutate(case, update)
    return [{**row, "StorageIndex": index} for row, index in zip(case.units[1:], indices)]


@pytest.mark.parametrize("indices", [(0, 1, 2, 1), (1, 1, 1, 1), (2, 2, 2, 2)])
def test_materials_are_consumed_from_their_actual_unlocked_barracks(case, indices):
    selected = configure(case, indices)
    before = case.store.profile()
    envelope = message(case, materials=selected)
    result = case.service.evolve(envelope, case.store)
    after = case.store.profile()
    assert len(after.state["player_state"]["units"]) == 2
    assert after.state["player_state"]["units"][1]["ClassChangeCount"] == 2
    assert after.state["tables"]["game_param"][0]["Money"] == 600
    assert after.state["tables"]["config"] == before.state["tables"]["config"]
    assert after.state["player_state"]["battle"] == before.state["player_state"]["battle"]
    assert case.service.evolve(envelope, case.store) == result
    assert case.store.profile() == after


@pytest.mark.parametrize("requested", [0, 2, 3, 15])
def test_stale_or_forged_material_barracks_are_rejected_without_spending(case, requested):
    selected = configure(case, (1, 1, 1, 1))
    selected[0]["StorageIndex"] = requested
    fails(case, message(case, materials=selected), "evolution_storage_mismatch")


def test_materials_in_unpurchased_barracks_are_rejected(case):
    selected = configure(case)
    mutate(case, lambda state: state["tables"]["config"][0].update({PURCHASE_FIELD: 0}))
    fails(case, message(case, materials=selected), "unit_capacity_barracks_unsupported")


@pytest.mark.parametrize("protection", ["locked", "organized"])
def test_extra_barracks_do_not_remove_material_protection(case, protection):
    selected = configure(case, (1, 1, 1, 1))
    def update(state):
        if protection == "locked":
            state["player_state"]["units"][2]["GeneralPurposeFlags"] |= 2
        else:
            state["player_state"]["squads"].append(
                {"Address": 0, "UnitIndex": selected[0]["GetIndex"], "Name": "old", "Flags": 0})
    mutate(case, update)
    fails(case, message(case, materials=selected), "evolution_material_protected")


def test_extra_barracks_target_remains_restricted(case):
    selected = configure(case, (1, 1, 1, 1))
    fails(case, message(case, base={**case.units[0], "StorageIndex": 1}, materials=selected), "evolution_storage_unsupported")


def test_skill_awakening_consumes_an_extra_barracks_material(case):
    selected = configure(case, (1, 1, 1, 1))
    case.service.evolve(message(case, operation=8, materials=selected[:1]), case.store)
    remaining = case.store.profile().state["player_state"]["units"]
    assert len(remaining) == 5
    assert remaining[1]["SkillEvolved"] == 1
    assert all(unit["GetIndex"] != selected[0]["GetIndex"] for unit in remaining)


def test_concurrent_material_transfer_is_rechecked_before_commit(case, monkeypatch):
    selected = configure(case, (1, 1, 1, 1))
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
        case.service.evolve(message(case, materials=selected), case.store)
    assert caught.value.code == "evolution_storage_mismatch"
    saved = case.store.profile().state
    assert len(saved["player_state"]["units"]) == 6
    assert saved["tables"]["game_param"][0]["Money"] == 1000
    assert "evolution_history" not in saved


@pytest.mark.parametrize("operation", [7, 8, 9])
def test_native_transfer_then_actual_awakening_consumes_extra_materials(bundle, tmp_path, operation):
    catalog = bundle.evolution.catalog
    target = unit(catalog, 17 if operation == 9 else 7, level=999, stage=2 if operation != 7 else 0)
    if operation == 8:
        target["SkillLevel"] = catalog.growth.describe(target).skill_max
    selected = [unit(catalog, 234)] if operation == 8 else materials(catalog, target, operation)
    state, rows = seed(bundle, target, selected)
    quote = catalog.quote(rows[0], rows[1:], operation, 1 if operation == 9 else 0)
    state = bundle.possessions.credit(state, dict(quote.items))
    state["tables"]["config"][0][PURCHASE_FIELD] = 2 << EXTRA_SHIFT
    app = create_app(tmp_path, initial_state=bundle.initialize(state), state_initializer=bundle.initialize,
        handlers=bundle.evolution.handlers() | bundle.barracks.handlers() | bundle.player_state.handlers())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        selected = rows[1:]
        moving = request(app.state.store, 118, (Column("DEST", "I", (2,)), Container("BASE", (
            Column("A7", "I", tuple(row["GetIndex"] for row in selected)),
            Column("AB", "I", tuple(row["UniqueID"] for row in selected))))))
        post(client, moving)
        selected = [{**row, "StorageIndex": 2} for row in selected]
        envelope = evolve(app.state.store, rows[0], selected, operation, branch=1 if operation == 9 else 0, tick=1001)
        wire, response = post(client, envelope)
        saved = app.state.store.profile()
        assert saved.state["player_state"]["units"][1]["ClassChangeCount"] == (3 if operation == 9 else 2)
        if operation == 8:
            assert saved.state["player_state"]["units"][1]["SkillEvolved"] == 1
        assert bundle.unit_capacity.usage(saved.state).counts == (2, 0, 0)
        assert response["PPU"].children[0].values == (rows[0]["GetIndex"],)
        assert post(client, envelope)[0] == wire and app.state.store.profile() == saved
        _, listing = post(client, request(app.state.store, 41, ()))
        assert listing["A7"].values == (0, rows[0]["GetIndex"])
