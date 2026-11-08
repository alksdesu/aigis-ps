from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.cli import service_readiness
from test_blessing_service import message
from test_evolution_integration import post
from test_present_items_integration import claim
from test_quest_service import bundle, request


ITEM = "battle_god_bless_001001"
GIFT = "gift_0002_ruby"


def initial(bundle):
    state = bundle.initialize(bundle.bootstrap.initial_state())
    return bundle.possessions.credit(state, {ITEM: 1, GIFT: 3})


def application(bundle, path, state):
    assert bundle.blessings is not None, bundle.blessing_error
    return create_app(path, initial_state=state, state_initializer=bundle.initialize,
                      handlers=bundle.bootstrap.handlers() | bundle.blessings.handlers() | bundle.presents.handlers())


def inventory(node):
    fields = {column.name: column.values for column in node.children}
    return {name: {key: values[index] for key, values in fields.items() if key != "ItemID"}
            for index, name in enumerate(fields.get("ItemID", ()))}


def test_native_http_enable_filter_readback_restart_and_disable(bundle, tmp_path):
    state = initial(bundle)
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        store = app.state.store
        before = store.profile()
        envelope = message(SimpleNamespace(store=store), {ITEM: 1}, 2)
        wire, result = post(client, envelope)
        saved = store.profile()
        assert set(result) == {"SYSTEM", "PGC", "POSSESSION"}
        assert result["PGC"].children == (Column("A1", "S", ("FE",)), Column("A2", "S", ("2",)))
        assert inventory(result["POSSESSION"]) == {ITEM: {"Count": 1, "Enabled": 1}}
        assert saved.revision == before.revision + 1
        for number in (129, 137):
            _, loaded = post(client, request(store, number, ()))
            assert inventory(loaded["POSSESSION"]) == {
                ITEM: {"Count": 1, "Enabled": 1}, GIFT: {"Count": 3, "Enabled": 0}}
        _, config = post(client, request(store, 36, ()))
        assert config["FE"].values == (2,)
        assert store.profile() == saved
    reopened = application(bundle, tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        store = reopened.state.store
        assert post(client, envelope)[0] == wire and store.profile() == saved
        _, result = post(client, message(SimpleNamespace(store=store), {ITEM: 0}, tick=1001))
        assert inventory(result["POSSESSION"]) == {ITEM: {"Count": 1, "Enabled": 0}}
        assert store.profile().state["tables"]["config"][0]["_BattleGodBlessFilter"] == 2


def test_mail_after_enable_keeps_setting_and_lists_enabled_on_the_item_delta(bundle, tmp_path):
    state = initial(bundle)
    state, mail = bundle.presents.enqueue(state, [
        {"kind": "item", "item_id": GIFT, "amount": 2, "description": "報酬"}])
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        store = app.state.store
        post(client, message(SimpleNamespace(store=store), {ITEM: 1}))
        _, result = post(client, claim(store, 1, [mail[0]["PresentID"]], tick=1001))
        assert inventory(result["POSSESSION"]) == {GIFT: {"Count": 5, "Enabled": 0}}
        _, loaded = post(client, request(store, 137, ()))
        assert inventory(loaded["POSSESSION"])[ITEM] == {"Count": 1, "Enabled": 1}
        assert not store.profile().state["tables"]["presents"]


@pytest.mark.parametrize("value", [0, 1, 2, 3])
def test_filter_only_http_does_not_create_or_change_inventory(bundle, tmp_path, value):
    state = bundle.initialize(bundle.bootstrap.initial_state())
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        store = app.state.store
        _, result = post(client, message(SimpleNamespace(store=store), selected_filter=value))
        assert not result["POSSESSION"].children
        assert not store.profile().state["tables"]["possessions"]
        assert store.profile().state["tables"]["config"][0]["_BattleGodBlessFilter"] == value


@pytest.mark.parametrize("value", [0, 1])
def test_unowned_blessing_cannot_be_enabled_or_disabled_over_http(bundle, tmp_path, value):
    state = bundle.initialize(bundle.bootstrap.initial_state())
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        store = app.state.store
        before = store.profile()
        response = post(client, message(SimpleNamespace(store=store), {ITEM: value}), 409)
        assert response.json()["error"]["code"] == "blessing_not_owned"
        assert store.profile() == before


def test_blessing_input_does_not_accept_response_field_or_bool_type(bundle, tmp_path):
    state = initial(bundle)
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        store = app.state.store
        before = store.profile()
        for field, kind, value in (("Enabled", "I", 1), ("Enable", "B", True)):
            envelope = request(store, 148, (Container("BGB", (
                Column("ItemID", "S", (ITEM,)), Column(field, kind, (value,)))),))
            response = post(client, envelope, 400)
            assert response.json()["error"]["code"] == "invalid_request_fields"
            assert store.profile() == before


def test_cli_reports_the_verified_blessing_catalog_and_handler(bundle):
    status = service_readiness(bundle)
    assert status["blessings"]["ready"] is True
    assert status["blessings"]["catalog"]["item_count"] == 242
    assert status["blessings"]["catalog"]["filters"] == [0, 1, 2, 3]
    assert set(bundle.blessings.handlers()) == {148}
