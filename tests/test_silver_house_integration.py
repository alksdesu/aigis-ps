from collections import Counter

from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.app import create_app
from aigis_ps.protocol import decode_wire, encode_wire
from test_quest_service import bundle, request
from test_training_service import columns


def post(client, envelope):
    result = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
    assert result.status_code == 200, result.text
    return result.content, {child.name: child for child in loads(decode_wire(result.content, envelope.profile.response_key)).children}


def deposit_request(store, units, tick=1000):
    return request(store, 127, (Container("LST", (
        Column("A7", "I", tuple(unit["GetIndex"] for unit in units)),
        Column("AB", "I", tuple(unit["UniqueID"] for unit in units)))),), tick=tick)


def counts(table):
    return dict(zip(table.children[0].values, table.children[1].values))


def test_native_silver_deposit_compensation_withdrawal_and_restart(bundle, tmp_path):
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    units = []
    for card_id, level in ((8, 29), (8, 30), (7, 19), (7, 20)):
        initial, added = bundle.player_state.credit_units(initial, [{"card_id": card_id, "level": level, "count": 1}])
        units.extend(added)
    initial["player_state"]["units"][2].update(Love=100, WatchLoveEv=2, SkillLevel=3, CostModMixValue=-1)
    def application():
        return create_app(tmp_path, initial_state=initial, handlers=(bundle.bootstrap.handlers()
            | bundle.player_state.handlers() | bundle.silver_house.handlers()))
    app = application()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        store = app.state.store
        _, response = post(client, request(store, 126, ()))
        assert counts(response["PSH"]) == {}
        start = store.profile()
        _, response = post(client, deposit_request(store, units))
        assert counts(response["PSH"]) == {7: 2, 8: 2}
        assert response["RML"].children[0].values == tuple(unit["GetIndex"] for unit in units)
        assert Counter(response["PPU"].children[0].values) == Counter((460, 660))
        deposited = store.profile()
        assert deposited.state["tables"]["game_param"] == start.state["tables"]["game_param"]
        assert deposited.state["gift_state"]["claims"]["8"] == [1, 100]
        assert Counter(unit["CardID"] for unit in deposited.state["player_state"]["units"]) == Counter((1, 460, 660))
        withdrawal = request(store, 128, (Container("Draw", (Column("CardID", "I", (8,)),)),), tick=1001)
        wire, response = post(client, withdrawal)
        assert counts(response["PSH"]) == {7: 2, 8: 1}
        returned = store.profile().state["player_state"]["units"][-1]
        assert returned["CardID"] == 8 and returned["Exp"] == returned["Love"] == returned["CostModMixValue"] == 0
        assert returned["SkillLevel"] == 1
        assert returned["UniqueID"] > deposited.state["player_state"]["last_unique_id"]
        saved = store.profile()
        assert post(client, withdrawal)[0] == wire and store.profile() == saved
    reopened = application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, withdrawal)[0] == wire
        assert reopened.state.store.profile() == saved
        _, response = post(client, request(reopened.state.store, 126, ()))
        assert counts(response["PSH"]) == {7: 2, 8: 1}


def test_silver_capacity_failure_does_not_delete_or_compensate(bundle, tmp_path):
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    initial, units = bundle.player_state.credit_units(initial, [{"card_id": 8, "level": 30, "count": 1}])
    initial["silver_house_schema"] = 1
    initial["tables"]["silver_house"] = [{"CardID": 8, "Count": 200}]
    app = create_app(tmp_path, initial_state=initial, handlers=bundle.silver_house.handlers())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = deposit_request(app.state.store, units)
        before = app.state.store.profile()
        result = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert result.status_code == 409, result.text
        assert app.state.store.profile() == before


def training_request(store, target, materials, tick=1000):
    rows = [(kind, 0, 0, 0, card_id, 0, 0) for kind, card_id in materials]
    references = tuple(Column(name, "I", tuple(row[index] for row in rows)) for index, name in enumerate(
        ("STORAGETYPE", "STORAGEINDEX", "INVITEID", "UNIQUEID", "CARDID", "A7", "AB")))
    return request(store, 5, (Container("BASE", columns((target,))), Container("APPEND", references),
        Container("CMB", (Column("A1", "I", (0,)), Column("A7", "I", (0,)), Column("B0", "I", (0,))))), tick=tick)


def test_type1_synthesis_consumes_counts_and_reload_returns_authoritative_snapshot(bundle, tmp_path):
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    initial, units = bundle.player_state.credit_units(initial, [{"card_id": 3, "level": 1, "count": 1}])
    initial["silver_house_schema"] = 1
    initial["tables"]["silver_house"] = [{"CardID": 3, "Count": 2}]
    app = create_app(tmp_path, initial_state=initial, handlers=bundle.training.handlers() | bundle.silver_house.handlers())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = training_request(app.state.store, units[0], ((1, 3), (1, 3)))
        wire, response = post(client, envelope)
        assert set(response) == {"SYSTEM", "PGP", "PPU"}
        assert response["PPU"].children[1].values == (200,)
        state = app.state.store.profile()
        assert len(state.state["player_state"]["units"]) == 2
        assert state.state["player_state"]["units"][1]["SkillExperience"] == 2
        assert state.state["player_state"]["units"][1]["CostModMixValue"] == -1
        _, refreshed = post(client, request(app.state.store, 126, ()))
        assert counts(refreshed["PSH"]) == {3: 0}
        assert post(client, envelope)[0] == wire
        assert app.state.store.profile() == state


def test_mixed_silver_and_fairy_stock_failure_rolls_back_both_inventories(bundle, tmp_path):
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    initial, units = bundle.player_state.credit_units(initial, [{"card_id": 3, "level": 1, "count": 1}])
    initial["silver_house_schema"] = initial["fairy_farm_schema"] = 1
    initial["tables"]["silver_house"] = [{"CardID": 3, "Count": 1}]
    initial["tables"]["fairy_farm"] = [{"CardID": 55, "Count": 0}]
    app = create_app(tmp_path, initial_state=initial, handlers=bundle.training.handlers())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        envelope = training_request(app.state.store, units[0], ((1, 3), (2, 55)))
        response = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert response.status_code == 409, response.text
        assert app.state.store.profile() == before
