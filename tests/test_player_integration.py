from argparse import Namespace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.app import create_app
from aigis_ps.cli import service_readiness, services
from aigis_ps.crystal_wallet import credit_free_crystals
from aigis_ps.filter_state import filter_response, name_hash
from aigis_ps.protocol import decode_wire, encode_wire, prepare_request


def test_native_squads_and_configuration_share_a_persistent_profile(tmp_path):
    project = Path(__file__).resolve().parents[1]
    args = Namespace(asset_dir=project.parent / "aigisr_exe", data_dir=project / "runtime/server",
                     resource_dir=project / "runtime/resources", rules=None)
    if not (args.asset_dir / "Aigis_r.exe").is_file() or not (args.data_dir / "masterdata/active.json").is_file():
        pytest.skip("Verified local client resources and public masters are required")
    bundle = services(args)
    assert service_readiness(bundle)["startup_ready"]
    assert bundle.possessions.resolve_hash(1832285076) == "gift_0002_ruby"
    assert bundle.possessions.resolve_hash(-1557561988) == "gift_1000_beer"

    def application():
        return create_app(tmp_path, handlers=bundle.bootstrap.handlers() | bundle.player_state.handlers() | bundle.unit_actions.handlers() | bundle.filters.handlers(),
                          initial_state=bundle.player_state.initialize(bundle.bootstrap.initial_state()),
                          state_initializer=bundle.player_state.initialize)

    app = application()
    original = app.state.store.profile()
    items = {"gift_0002_ruby": 2, "gift_1000_beer": 3}
    rewarded, new_units = bundle.player_state.credit_units(original.state, [{"card_id": 4, "level": 2, "count": 1}])
    rewarded = bundle.possessions.credit(rewarded, items)
    rewarded = credit_free_crystals(rewarded, 1, 739920)
    assert bundle.card_status is not None
    rewarded = bundle.card_status.update(rewarded, [{"CardID": 4, "A7": 1, "B0": 1793491200}])
    app.state.store.update_state(rewarded, original.revision)
    unit_index = new_units[0]["GetIndex"]
    assert new_units[0]["Exp"] == 32

    def post(client, number, columns=()):
        profile = app.state.registry.by_registration[number]
        identity = tuple(Column(name, "S", (value,)) for name, value in (
            ("UI", original.player_id), ("UK", original.user_key), ("MK", original.method_key),
            ("PFN", "dmm-r18"), ("GPVI", "1"), ("GPOT", "local-only"),
        ))
        xml = dumps(Container(children=identity + (Column("CT", "I", (0,)), Column("PlatformCode", "I", (1,))) + columns))
        response = client.post("/api/" + profile.route, content=encode_wire(prepare_request(xml, profile.st), profile.request_key))
        assert response.status_code == 200, (number, response.text)
        return loads(decode_wire(response.content, profile.response_key))

    initial_squads = (Column("A0", "I", (0x01000001, 0x02000001)), Column("A1", "I", (unit_index, -1)),
                      Column("A3", "S", ("One", "Two")), Column("A4", "I", (0, 0)))
    changed_squad = (Column("A0", "I", (0x01000001,)), Column("A1", "I", (unit_index,)),
                     Column("A3", "S", ("Changed",)), Column("A4", "I", (1,)), Column("TL", "I", (1,)))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for number in (129, 137):
            possession = next(node for node in post(client, number).children if node.name == "POSSESSION")
            assert dict(zip(possession.children[0].values, possession.children[1].values)) == items
        assert all(not column.values for column in post(client, 72).children[2].children)
        post(client, 74, initial_squads)
        post(client, 73, changed_squad)
        post(client, 22, (Column("A4", "I", (7,)),))
        post(client, 21, tuple(Column(name, "I", (value,)) for name, value in
                              (("A2", 0), ("A3", 1), ("C2", 80), ("C3", 70), ("F9", 1), ("FA", 0), ("FD", 1))))
        locked = post(client, 119, (Container("PU", (Column("A7", "I", (unit_index,)), Column("AE", "I", (3,)))),))
        assert locked.children[1].children[13].values == (3,)
        post(client, 25, tuple(Column(name, "S", (value,)) for name, value in (("F1", "0102"), ("F4", "01"), ("F5", "1001"))))
        post(client, 26, (Container("Filter", (Column("FilterNameHash", "I", (name_hash("TeamEdit"), name_hash("UnitCheck"))),
                                              Column("SaveValue", "S", ("1,2", "0")),
                                              Column("FilterName", "S", ("TeamEdit", "UnitCheck")))),))
        assert next(node for node in post(client, 137).children if node.name == "PlayerFilterParam") == filter_response(app.state.store.profile().state)
        config = {column.name: column.values[0] for column in post(client, 36).children}
        assert (config["A4"], config["A2"], config["C2"]) == (7, False, 80)
        assert (config["F1"], config["F4"], config["F5"]) == ("0102", "01", "1001")
        assert post(client, 41).children[0].values == (1, 4)
        assert post(client, 41).children[13].values == (0, 3)
        status = {column.name: column.values for column in post(client, 51).children}
        assert status["CardID"] == (4,) and status["A7"] == (1,) and status["B0"] == (1793491200,)
        assert post(client, 44).children[1].values == (1,)
    saved = app.state.store.profile()
    assert saved.state["tables"]["game_param"] == original.state["tables"]["game_param"]
    assert {row["Name"] for row in saved.state["player_state"]["squads"]} == {"Changed", "Two"}

    app = application()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        possession = next(node for node in post(client, 129).children if node.name == "POSSESSION")
        assert dict(zip(possession.children[0].values, possession.children[1].values)) == items
        assert next(node for node in post(client, 137).children if node.name == "PlayerFilterParam") == filter_response(saved.state)
        loaded = post(client, 72)
        assert set(loaded.children[2].children[2].values) == {"Changed", "Two"}
        post(client, 74, initial_squads)
        config = {column.name: column.values[0] for column in post(client, 36).children}
        assert (config["A4"], config["A2"], config["C2"]) == (7, False, 80)
        assert (config["F1"], config["F4"], config["F5"]) == ("0102", "01", "1001")
        assert post(client, 41).children[0].values == (1, 4)
        assert post(client, 41).children[13].values == (0, 3)
        assert {column.name: column.values for column in post(client, 51).children} == status
        assert post(client, 44).children[1].values == (1,)
    assert app.state.store.profile() == saved
