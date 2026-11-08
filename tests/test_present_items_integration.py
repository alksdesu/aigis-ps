from copy import deepcopy
from datetime import datetime
import json

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.presents import PresentService
from test_evolution_integration import post
from test_quest_service import bundle, request


BOUQUET = "gift_0000_bouquet"
NOW = datetime(2026, 11, 1, 12)


def item(amount):
    return {"kind": "item", "item_id": BOUQUET, "amount": amount, "description": "花束の報酬"}


def legacy(amount):
    return {"kind": "legacy_item", "present_type": 3, "amount": amount, "description": "贈り物の報酬"}


def gold(amount):
    return {"kind": "gold", "amount": amount, "description": "ゴールドの報酬"}


def crystals(amount):
    return {"kind": "free_crystal", "amount": amount, "description": "結晶の報酬"}


def unit():
    return {"kind": "unit", "template_id": 2, "count": 1, "description": "聖霊の報酬"}


def state_for(bundle, monkeypatch, count=0):
    monkeypatch.setattr(bundle.bootstrap, "clock", lambda: NOW)
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state["tables"]["game_param"][0]["Money"] = 1000
    if count:
        state = bundle.possessions.credit(state, {BOUQUET: count})
    return state


def application(bundle, tmp_path, state, monkeypatch, *, reader=None, items=True):
    reader = reader or (lambda name: bundle.native_cdn.assets.read(name)[0])
    presents = PresentService(bundle.bootstrap, bundle.player_state, asset_reader=reader,
                              possessions=bundle.possessions if items else None)
    monkeypatch.setattr(bundle.bootstrap, "present_provider", presents.response)
    app = create_app(tmp_path, initial_state=state,
                     handlers=bundle.bootstrap.handlers() | bundle.player_state.handlers() | presents.handlers())
    return app, presents


def claim(store, operation, ids, tick=1000):
    return request(store, operation, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", tuple(ids)),)),)),), tick=tick)


def fields(node):
    return {column.name: column.values for column in node.children}


def counts(node):
    columns = fields(node)
    return dict(zip(columns.get("ItemID", ()), columns.get("Count", ())))


def stored_counts(state):
    return {row["ItemID"]: row["Count"] for row in state["tables"]["possessions"]}


@pytest.mark.parametrize("operation", [1, 2, 117])
def test_mixed_item_rewards_read_back_and_replay_after_reopening(bundle, tmp_path, monkeypatch, operation):
    state = state_for(bundle, monkeypatch, 7)
    assert bundle.presents.item_catalog.definition(3).item_id == BOUQUET
    state, mail = bundle.presents.enqueue(state, [unit(), gold(125), legacy(2), crystals(3), item(2)])
    assert [row["Type"] for row in mail] == [1, 2, 3, 0, 22]
    assert all("ItemID" not in row for row in mail[:-1]) and mail[-1]["ItemID"] == BOUQUET
    app, presents = application(bundle, tmp_path, state, monkeypatch)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, listing = post(client, request(app.state.store, 42, ()))
        values = fields(listing["PPT"])
        assert values["A1"] == (1, 2, 3, 0, 22)
        assert values["A6"] == ("", "", "", "", BOUQUET)
        envelope = claim(app.state.store, operation, [row["PresentID"] for row in mail])
        wire, response = post(client, envelope)
        assert set(response) == {"SYSTEM", "PGP", "PFC", "PBP", "PPU", "POSSESSION"}
        assert fields(response["PGP"]) == {"A1": ("A1",), "A2": ("1125",)}
        assert counts(response["POSSESSION"]) == {BOUQUET: 11}
        assert fields(response["PPU"])["A1"] == (56,)
        assert fields(response["PBP"])["A2"] == (1,)
        pfc = fields(response["PFC"])
        assert pfc["Num"] == (3,) if operation == 117 else pfc["value"][0] == 3
        saved = app.state.store.profile()
        assert saved.state["tables"]["game_param"][0]["Money"] == 1125
        assert wallet_totals(saved.state)["free"] == 3
        assert stored_counts(saved.state) == {BOUQUET: 11}
        assert not presents.rows(saved.state)
        assert [row["CardID"] for row in saved.state["player_state"]["units"]] == [1, 56]
        assert post(client, envelope)[0] == wire and app.state.store.profile() == saved

    reopened, _ = application(bundle, tmp_path, state, monkeypatch)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        for operation in (129, 137):
            _, result = post(client, request(reopened.state.store, operation, ()))
            assert counts(result["POSSESSION"]) == {BOUQUET: 11}
        _, listing = post(client, request(reopened.state.store, 42, ()))
        assert all(not values for values in fields(listing["PPT"]).values())
        assert reopened.state.store.profile() == saved


@pytest.mark.parametrize("space,accepted_index", [(3, 1), (2, 0), (1, 2)])
def test_partial_claim_shares_item_capacity_and_uses_requested_order(bundle, tmp_path, monkeypatch, space, accepted_index):
    limit = bundle.presents.item_catalog.definition(3).stack_limit
    state = state_for(bundle, monkeypatch, limit - space)
    state, mail = bundle.presents.enqueue(state, [legacy(2), item(3), item(1), gold(5), crystals(2)])
    app, presents = application(bundle, tmp_path, state, monkeypatch)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = claim(app.state.store, 117, [mail[index]["PresentID"] for index in (1, 0, 2, 3, 4)])
        wire, response = post(client, envelope)
        saved = app.state.store.profile()
        assert stored_counts(saved.state)[BOUQUET] == limit
        assert counts(response["POSSESSION"]) == {BOUQUET: limit}
        assert [row["PresentID"] for row in presents.rows(saved.state)] == [
            row["PresentID"] for index, row in enumerate(mail[:3]) if index != accepted_index]
        assert saved.state["tables"]["game_param"][0]["Money"] == 1005
        assert wallet_totals(saved.state)["free"] == 2
        assert all(not values for values in fields(response["PPU"]).values())
        assert saved.state["player_state"] == state["player_state"]
    reopened, _ = application(bundle, tmp_path, state, monkeypatch)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire and reopened.state.store.profile() == saved


@pytest.mark.parametrize("operation", [1, 2])
def test_item_overflow_keeps_every_selected_reward_and_all_balances(bundle, tmp_path, monkeypatch, operation):
    limit = bundle.presents.item_catalog.definition(3).stack_limit
    state = state_for(bundle, monkeypatch, limit - 1)
    state, mail = bundle.presents.enqueue(state, [unit(), gold(5), crystals(2), legacy(2)])
    app, _ = application(bundle, tmp_path, state, monkeypatch)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        response = post(client, claim(app.state.store, operation, [row["PresentID"] for row in mail]), expected=409)
        assert response.json()["error"]["code"] == "present_item_capacity"
        assert app.state.store.profile() == before


@pytest.mark.parametrize("failure", ["resource_unavailable", "unknown_item"])
def test_missing_item_definitions_never_delete_pending_mail(bundle, tmp_path, monkeypatch, failure):
    state = state_for(bundle, monkeypatch)
    state, mail = bundle.presents.enqueue(state, [gold(5), item(2)])
    if failure == "unknown_item":
        state["tables"]["presents"][-1]["ItemID"] = "unverified_missing_item"
    def reader(name):
        if failure == "resource_unavailable" and name == "ItemConfigTable.atb":
            raise OSError("Item resource unavailable")
        return bundle.native_cdn.assets.read(name)[0]
    app, _ = application(bundle, tmp_path, state, monkeypatch, reader=reader)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        response = post(client, claim(app.state.store, 117, [row["PresentID"] for row in mail]), expected=503)
        assert response.json()["error"]["code"] == "present_state_invalid"
        assert app.state.store.profile() == before


def test_receipt_from_unit_only_service_replays_unchanged_after_item_support(bundle, tmp_path, monkeypatch):
    state = state_for(bundle, monkeypatch)
    state, mail = bundle.presents.enqueue(state, [unit()])
    old_app, old_service = application(bundle, tmp_path, state, monkeypatch, items=False)
    assert old_service.item_catalog is None
    with TestClient(old_app, base_url="http://127.0.0.1") as client:
        envelope = claim(old_app.state.store, 1, [mail[0]["PresentID"]])
        wire, response = post(client, envelope)
        assert set(response) == {"SYSTEM", "PBP", "PPU"}
        saved = old_app.state.store.profile()
        receipt = deepcopy(saved.state["present_claim_history"])
    reopened, current_service = application(bundle, tmp_path, state, monkeypatch)
    assert current_service.item_catalog is not None
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert reopened.state.store.profile() == saved
        assert reopened.state.store.profile().state["present_claim_history"] == receipt


def completed_claim(bundle, tmp_path, monkeypatch, reward, operation=117):
    state = state_for(bundle, monkeypatch)
    state, mail = bundle.presents.enqueue(state, [reward])
    app, _ = application(bundle, tmp_path, state, monkeypatch)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = claim(app.state.store, operation, [mail[0]["PresentID"]])
        wire, _ = post(client, envelope)
        saved = app.state.store.profile()
    return state, envelope, wire, saved


@pytest.mark.parametrize("reward_kind", ["item", "unit"])
def test_cached_claim_without_item_dependencies_checks_only_item_receipts(bundle, tmp_path, monkeypatch, reward_kind):
    reward = item(2) if reward_kind == "item" else unit()
    state, envelope, wire, saved = completed_claim(bundle, tmp_path, monkeypatch, reward)
    reopened, service = application(bundle, tmp_path, state, monkeypatch, items=False)
    assert service.possessions is None and service.item_catalog is None
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        if reward_kind == "item":
            response = post(client, envelope, expected=503)
            assert response.json()["error"]["code"] != "internal_error"
        else:
            assert post(client, envelope)[0] == wire
        assert reopened.state.store.profile() == saved


def test_cached_claim_rejects_changed_catalog_dependency_fingerprint(bundle, tmp_path, monkeypatch):
    state, envelope, _, saved = completed_claim(bundle, tmp_path, monkeypatch, item(2))
    reopened, service = application(bundle, tmp_path, state, monkeypatch)
    assert service.item_catalog is not None
    monkeypatch.setattr(bundle.possessions, "asset_sha256", "f" * 64)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        response = post(client, envelope, expected=503)
        assert response.json()["error"]["code"] != "internal_error"
        assert reopened.state.store.profile() == saved


@pytest.mark.parametrize("count", ["two", -1, 0x80000000, 1], ids=["wrong_type", "negative", "overflow", "absolute_mismatch"])
def test_cached_claim_rejects_invalid_or_different_absolute_inventory(bundle, tmp_path, monkeypatch, count):
    state, envelope, _, saved = completed_claim(bundle, tmp_path, monkeypatch, item(2))
    reopened, _ = application(bundle, tmp_path, state, monkeypatch)
    corrupted = deepcopy(saved.state)
    corrupted["tables"]["possessions"][0]["Count"] = count
    with reopened.state.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(corrupted),))
    before = reopened.state.store.profile()
    assert before.revision == saved.revision
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        response = post(client, envelope, expected=503)
        assert response.json()["error"]["code"] != "internal_error"
        assert reopened.state.store.profile() == before


@pytest.mark.parametrize("operation", [1, 2, 117])
def test_cached_claim_with_valid_dependencies_preserves_wire_and_history(bundle, tmp_path, monkeypatch, operation):
    state, envelope, wire, saved = completed_claim(bundle, tmp_path, monkeypatch, item(2), operation)
    reopened, _ = application(bundle, tmp_path, state, monkeypatch)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        replayed, response = post(client, envelope)
        assert replayed == wire and counts(response["POSSESSION"]) == {BOUQUET: 2}
        assert reopened.state.store.profile() == saved


@pytest.mark.parametrize("money", [-1, True, 0x80000000], ids=["negative", "wrong_type", "overflow"])
def test_cached_gold_claim_rejects_corrupt_balance(bundle, tmp_path, monkeypatch, money):
    state, envelope, _, saved = completed_claim(bundle, tmp_path, monkeypatch, gold(10))
    reopened, _ = application(bundle, tmp_path, state, monkeypatch)
    corrupted = deepcopy(saved.state)
    corrupted["tables"]["game_param"][0]["Money"] = money
    with reopened.state.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(corrupted),))
    before = reopened.state.store.profile()
    assert before.revision == saved.revision
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        response = post(client, envelope, expected=503)
        assert response.json()["error"]["code"] != "internal_error"
        assert reopened.state.store.profile() == before


@pytest.mark.parametrize("field,value", [
    ("Num", -1), ("Num", 0x80000000), ("FreeFlag", 1), ("Date", "today"), ("Date", -1),
], ids=["negative_amount", "amount_overflow", "flag_type", "date_type", "negative_date"])
def test_cached_crystal_claim_rejects_corrupt_bucket(bundle, tmp_path, monkeypatch, field, value):
    state, envelope, _, saved = completed_claim(bundle, tmp_path, monkeypatch, crystals(2))
    reopened, _ = application(bundle, tmp_path, state, monkeypatch)
    corrupted = deepcopy(saved.state)
    corrupted["tables"]["crystals"][0][field] = value
    with reopened.state.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(corrupted),))
    before = reopened.state.store.profile()
    assert before.revision == saved.revision
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        response = post(client, envelope, expected=503)
        assert response.json()["error"]["code"] == "invalid_crystal_state"
        assert reopened.state.store.profile() == before
