from copy import deepcopy
from dataclasses import astuple
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.app import create_app
from aigis_ps.errors import ProtocolError
from aigis_ps.protocol import decode_wire, encode_wire
from aigis_ps.request_ledger import SCHEMA_VERSION
from aigis_ps.storage import Store
from aigis_ps.training_catalog import UNHELD_GET_INDEX, OwnershipBonus, TrainingBoost
from test_quest_service import bundle, request
from test_training_service import columns


RESENDS = "the native client shows a communication error and resends the same request until the server accepts it"
SUPPORT_CARD, SUPPORT_BONUS = 416, 10
UNHELD_SUPPORT = TrainingBoost(SUPPORT_CARD, UNHELD_GET_INDEX, SUPPORT_BONUS)


def training_request(store, target, cards, *, storage_type=2, tick=1001, bonus=(0, 0, 0)):
    references = tuple(Column(name, "I", tuple(value(card) for card in cards)) for name, value in (
        ("STORAGETYPE", lambda _: storage_type), ("STORAGEINDEX", lambda _: 0), ("INVITEID", lambda _: 0),
        ("UNIQUEID", lambda _: 0), ("CARDID", lambda card: card), ("A7", lambda _: 0), ("AB", lambda _: 0)))
    return request(store, 5, (Container("BASE", columns((target,))), Container("APPEND", references),
        Container("CMB", tuple(Column(name, "I", (value,)) for name, value in zip(("A1", "A7", "B0"), bonus)))), tick=tick)


@pytest.fixture
def saved(bundle, tmp_path):
    assert bundle.fairy_farm is not None and bundle.training is not None
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state, units = bundle.player_state.credit_units(state, [
        {"card_id": 8, "level": 1, "count": 1}, {"card_id": 56, "level": 1, "count": 3}])
    state["tables"]["game_param"][0]["Money"] = 1000000
    return SimpleNamespace(bundle=bundle, store=Store(tmp_path / "initial.sqlite3", state), units=units)


def deposit(saved, *, tick=1000):
    return request(saved.store, 123, (Container("LST", (
        Column("A7", "I", tuple(unit["GetIndex"] for unit in saved.units[1:])),
        Column("AB", "I", tuple(unit["UniqueID"] for unit in saved.units[1:])))),), tick=tick)


def test_native_deposit_synthesis_withdrawal_and_restart_form_one_flow(saved, tmp_path):
    bundle = saved.bundle
    initial = saved.store.profile().state
    def application():
        return create_app(tmp_path / "http", initial_state=initial, handlers=(bundle.bootstrap.handlers()
            | bundle.player_state.handlers() | bundle.training.handlers() | bundle.fairy_farm.handlers()))
    def post(client, envelope):
        response = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert response.status_code == 200, response.text
        return response.content, {child.name: child for child in loads(decode_wire(response.content, envelope.profile.response_key)).children}
    def counts(node):
        return dict(zip(node.children[0].values, node.children[1].values))
    app = application()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        saved.store = app.state.store
        _, result = post(client, deposit(saved))
        assert len(counts(result["PFF"])) == 54 and counts(result["PFF"])[56] == 3
        before = saved.store.profile()
        synthesize = training_request(saved.store, saved.units[0], (56, 56))
        wire, result = post(client, synthesize)
        assert counts(result["PFF"]) == {56: 1}
        assert result["PPU"].children[1].values == (2500,)
        after = saved.store.profile()
        assert after.state["tables"]["game_param"][0]["Money"] == before.state["tables"]["game_param"][0]["Money"] - 400
        assert [unit["CardID"] for unit in after.state["player_state"]["units"]] == [1, 8]
        assert post(client, synthesize)[0] == wire and saved.store.profile() == after
        withdraw = request(saved.store, 125, (Container("Draw", (
            Column("CardID", "I", (56,)), Column("Count", "I", (1,)))),), tick=1002)
        last_wire, result = post(client, withdraw)
        assert counts(result["PFF"])[56] == 0 and len(counts(result["PFF"])) == 54
        returned = saved.store.profile().state["player_state"]["units"][-1]
        assert returned["UniqueID"] > max(unit["UniqueID"] for unit in saved.units)
        _, result = post(client, request(saved.store, 122, ()))
        assert counts(result["PFF"])[56] == 0
        final = saved.store.profile()
    reopened = application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, withdraw)[0] == last_wire
        assert reopened.state.store.profile() == final


def test_aggregate_stock_failure_is_atomic_and_final_zero_is_explicit(saved, caplog):
    saved.bundle.fairy_farm.deposit(deposit(saved), saved.store)
    before = saved.store.profile()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="aigis_ps"), pytest.raises(ProtocolError) as caught:
        saved.bundle.training.integrate(training_request(saved.store, saved.units[0], (56,) * 4), saved.store)
    assert caught.value.code == "fairy_farm_quantity" and saved.store.profile() == before
    assert [record.getMessage() for record in caplog.records if record.name == "aigis_ps"] == [
        "Training 5 selected 4 of card 56 from storage type 2, which holds 3",
        f"Training 5 refused with fairy_farm_quantity; {RESENDS}"]
    response = saved.bundle.training.integrate(training_request(saved.store, saved.units[0], (56,) * 3), saved.store)
    pff = next(child for child in loads(response).children if child.name == "PFF")
    assert pff.children[0].values == (56,) and pff.children[1].values == (0,)


def test_silver_stock_shortage_names_the_silver_warehouse(saved, caplog):
    before = saved.store.profile()
    envelope = training_request(saved.store, saved.units[0], (3,), storage_type=1)
    with caplog.at_level(logging.WARNING, logger="aigis_ps"), pytest.raises(ProtocolError) as caught:
        saved.bundle.training.integrate(envelope, saved.store)
    assert caught.value.code == "silver_house_quantity" and saved.store.profile() == before
    assert [record.getMessage() for record in caplog.records if record.name == "aigis_ps"] == [
        "Training 5 selected 1 of card 3 from storage type 1, which holds 0",
        f"Training 5 refused with silver_house_quantity; {RESENDS}"]


def test_evicted_aggregate_receipt_cannot_consume_stock_again(saved, monkeypatch):
    import aigis_ps.request_ledger as module
    monkeypatch.setattr(module, "HISTORY_LIMIT", 1)
    saved.bundle.fairy_farm.deposit(deposit(saved), saved.store)
    first = training_request(saved.store, saved.units[0], (56,), tick=1001)
    saved.bundle.training.integrate(first, saved.store)
    second = training_request(saved.store, saved.units[0], (56,), tick=1002)
    saved.bundle.training.integrate(second, saved.store)
    before = saved.store.profile()
    with pytest.raises(ProtocolError) as caught:
        saved.bundle.training.integrate(first, saved.store)
    assert caught.value.code == "training_request_stale" and saved.store.profile() == before


def test_legacy_training_receipts_migrate_without_losing_prior_identity(saved):
    original = saved.store.profile()
    state = deepcopy(original.state)
    state["training_history"] = {"schema_version": 1, "receipts": []}
    saved.store.update_state(state, original.revision)
    saved.bundle.fairy_farm.deposit(deposit(saved), saved.store)
    saved.bundle.training.integrate(training_request(saved.store, saved.units[0], (56,)), saved.store)
    history = saved.store.profile().state["training_history"]
    assert history["schema_version"] == SCHEMA_VERSION and len(history["receipts"]) == 1
    assert history["processed_keys"] == [history["receipts"][0]["request"]]


def test_unsupported_bonus_card_cannot_be_smuggled_into_current_warehouse(saved):
    assert SUPPORT_CARD not in saved.bundle.fairy_farm.card_ids
    profile = saved.store.profile()
    profile.state["fairy_farm_schema"] = 1
    profile.state["tables"]["fairy_farm"] = [{"CardID": SUPPORT_CARD, "Count": 1}, {"CardID": 56, "Count": 1}]
    saved.store.update_state(profile.state, profile.revision)
    envelope = training_request(saved.store, saved.units[0], (56,), bonus=astuple(UNHELD_SUPPORT))
    before = saved.store.profile()
    with pytest.raises(ProtocolError) as caught:
        saved.bundle.training.integrate(envelope, saved.store)
    assert caught.value.code == "fairy_farm_state_invalid" and saved.store.profile() == before


def test_bonus_query_preserves_native_aggregate_identity_for_compatible_inventory(saved, monkeypatch):
    catalog = saved.bundle.training.catalog
    inventory = SimpleNamespace(read=lambda state: [{"CardID": SUPPORT_CARD, "Count": 1}])
    monkeypatch.setattr(catalog, "fairy_farm", inventory)
    state = saved.store.profile().state
    bonus = catalog.ownership_bonus(state["player_state"], state)
    assert bonus == OwnershipBonus(SUPPORT_BONUS, frozenset((UNHELD_SUPPORT,)))
