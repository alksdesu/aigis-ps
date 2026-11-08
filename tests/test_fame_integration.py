from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from aigis_ps.app import create_app
from test_evolution_integration import post
from test_fame_service import make_case, catalog, message, NOW
from test_quest_service import bundle, request


def application(case, path, initial):
    app = create_app(path, initial_state=initial, state_initializer=case.bundle.initialize,
        handlers=case.bootstrap.handlers() | case.service.handlers() | case.bundle.player_state.handlers()
                 | case.missions.handlers())
    case.store = app.state.store
    return app


def columns(node):
    return {column.name: column.values for column in node.children}


def test_native_register_deposit_withdraw_reopen_is_one_complete_flow(make_case, tmp_path):
    case = make_case(count=2, card_id=17, stages=(3, 4))
    initial = case.store.profile().state
    original_ids = {unit["UniqueID"] for unit in case.selected}
    original_high = initial["player_state"]["battle"]["UX"]
    app = application(case, tmp_path / "flow", initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = case.store.profile()
        registration = message(case)
        wire, result = post(client, registration)
        assert set(result) == {"SYSTEM", "PPU", "CardStatus", "FameRegister"}
        ppu = columns(result["PPU"])
        assert ppu["B5"] == (1, 1)
        assert next(column.type for column in result["PPU"].children if column.name == "B5") == "I"
        assert len(ppu) == 21
        registered = case.store.profile()
        status = case.bundle.card_status.read(registered.state)[0]
        assert status["B0"] == status["B1"] == status["B2"] == 0
        assert status["B3"] == status["B4"] == int(NOW.timestamp())
        assert registered.state["tables"]["game_param"] == before.state["tables"]["game_param"]
        assert post(client, registration)[0] == wire and case.store.profile() == registered
        progress = deepcopy(registered.state["mission_state"])

        _, result = post(client, message(case, 150, tick=1001))
        assert set(result) == {"SYSTEM", "PPU", "FameDepository"}
        assert not result["PPU"].children
        stock = columns(result["FameDepository"])
        assert stock["Count3"] == stock["Count4"] == (1,)
        assert len(case.store.profile().state["player_state"]["units"]) == 1

        withdrawal = message(case, 151, stage=3, tick=1002)
        wire, result = post(client, withdrawal)
        assert set(result) == {"SYSTEM", "PPU", "PBP", "FameDepository"}
        saved = case.store.profile()
        returned = saved.state["player_state"]["units"][-1]
        assert returned["GetIndex"] > original_high and returned["UniqueID"] not in original_ids
        assert returned["ClassChangeCount"] == 3
        assert returned["Famed"] and case.catalog.eligible(returned)
        assert all(returned[key] == value for key, value in case.catalog.maximum_values(17, 3).items())
        assert saved.state["mission_state"] == progress
        assert saved.state["tables"]["game_param"] == before.state["tables"]["game_param"]
        _, loaded = post(client, request(case.store, 137, ()))
        counts = columns(loaded["FameDepository"])
        assert counts["CardId"] == (17,) and counts["Count3"] == (0,) and counts["Count4"] == (1,)
        _, units = post(client, request(case.store, 41, ()))
        assert units["A1"].values == (1, 17)
    reopened = application(case, tmp_path / "flow", initial)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, withdrawal)[0] == wire
        assert case.store.profile() == saved


def test_support_registration_updates_total_and_support_tasks_without_immediate_rewards(make_case, catalog, tmp_path):
    identifier = min(catalog.card_ids & catalog.support_card_ids)
    case = make_case(card_id=identifier)
    initial = case.store.profile().state
    app = application(case, tmp_path / "support", initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, message(case))
        after = case.store.profile().state
        progress = {row["ID"]: row for row in case.missions.progress.snapshot(after, NOW)}
        for definition in case.missions.catalog.active(NOW):
            if definition.condition_type in (10, 16):
                assert progress[definition.condition_id]["Progress"] == 1
        assert after["tables"]["presents"] == initial["tables"]["presents"]
        assert after["tables"]["crystals"] == initial["tables"]["crystals"]


def test_extra_barracks_registration_is_rejected_before_state_changes(make_case, tmp_path):
    case = make_case(source=1)
    initial = case.store.profile().state
    app = application(case, tmp_path / "extra", initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = case.store.profile()
        response = post(client, message(case), 409)
        assert response.json()["error"]["code"] == "fame_registration_barracks"
        assert case.store.profile() == before


def test_missing_repository_provider_cannot_commit_an_unreadable_deposit(make_case, tmp_path):
    case = make_case(registered=True)
    initial = case.store.profile().state
    case.bootstrap.fame_provider = None
    app = application(case, tmp_path / "provider", initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = case.store.profile()
        response = post(client, message(case, 150), 503)
        assert response.json()["error"]["code"] == "collection_rows_unavailable"
        assert case.store.profile() == before
