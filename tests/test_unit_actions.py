from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.app import create_app
from aigis_ps.bootstrap import BootstrapService
from aigis_ps.config_state import configuration_delta, configuration_row
from aigis_ps.errors import ProtocolError
from aigis_ps.protocol import ProtocolRegistry, decode_wire, encode_wire, prepare_request
from aigis_ps.storage import Store
from aigis_ps.unit_actions import UnitActionService
from test_bootstrap import asset_root
from test_player_state import assets, master, service


@pytest.fixture
def actions(asset_root, service):
    bootstrap = BootstrapService(asset_root, clock=lambda: datetime(2026, 11, 1, 12), asset_version="a" * 40)
    return UnitActionService(bootstrap, service)


@pytest.fixture
def store(actions, tmp_path):
    state = actions.players.initialize(actions.bootstrap.initial_state())
    unit = deepcopy(state["player_state"]["units"][0])
    unit.update(CardID=2, ClassID=20, SettingSkillID=3, GetIndex=1, UniqueID=2, GeneralPurposeFlags=0x101)
    state["player_state"]["units"].append(unit)
    state["player_state"]["battle"]["UX"] = 1
    return Store(tmp_path / "actions.sqlite3", state)


def request(store, number, fields):
    registry = ProtocolRegistry.load()
    wire = registry.by_registration[number]
    player = store.profile()
    common = tuple(Column(name, kind, (value,)) for name, kind, value in (
        ("UI", "S", player.player_id), ("UK", "S", player.user_key), ("MK", "S", player.method_key),
        ("CT", "I", 1), ("PFN", "S", "dmm-r18"), ("PlatformCode", "I", 1),
        ("GPVI", "S", "unkown"), ("GPOT", "S", "unkown")))
    xml = prepare_request(dumps(Container(children=common + tuple(fields))), wire.st)
    return registry.decode(wire.route, encode_wire(xml, wire.request_key))


def sort_request(store, values=("010203", "0001", "1010"), number=25):
    return request(store, number, [Column(name, "S", (value,)) for name, value in zip(("F1", "F4", "F5"), values)])


def lock_request(store, rows=((1, 0x103),)):
    return request(store, 119, (Container("PU", (
        Column("A7", "I", tuple(index for index, _ in rows)),
        Column("AE", "I", tuple(flags for _, flags in rows)),
    )),))


def children(xml):
    return {child.name: child for child in loads(xml).children}


def assert_unchanged_error(store, callback, code=None):
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        callback()
    if code:
        assert error.value.code == code
    assert store.profile() == before


def test_sort_save_only_updates_three_strings_and_preserves_tutorial(actions, store):
    before = store.profile()
    envelope = sort_request(store)
    response = actions.save_sort(envelope, store)
    after = store.profile()
    expected = deepcopy(before.state)
    expected["tables"]["config"][0].update(_FlagSort="010203", _FlagMissionFilter="0001", TypeOfSortOrder="1010")
    assert after.state == expected
    delta = children(response)["PGC"]
    assert delta.children[0].values == ("F1", "F4")
    assert delta.children[1].values == ("010203", "0001")
    assert actions.save_sort(envelope, store) == response
    assert store.profile() == after
    assert_unchanged_error(store, lambda: configuration_delta(configuration_row(after.state), ("TypeOfSortOrder",)))


@pytest.mark.parametrize("values", [("", "", ""), ("1" * 127, "0" * 31, "2" * 127)])
def test_sort_accepts_empty_and_maximum_native_strings(actions, store, values):
    actions.save_sort(sort_request(store, values), store)
    row = store.profile().state["tables"]["config"][0]
    assert (row["_FlagSort"], row["_FlagMissionFilter"], row["TypeOfSortOrder"]) == values


@pytest.mark.parametrize("values", [("1" * 128, "", ""), ("", "1" * 32, ""), ("", "", "1" * 128),
                                    ("猫" * 43, "", ""), ("", "猫" * 11, "")])
def test_sort_uses_byte_limits_and_rejects_overflow_atomically(actions, store, values):
    assert_unchanged_error(store, lambda: actions.save_sort(sort_request(store, values), store), "invalid_request_fields")


@pytest.mark.parametrize("mutation", ["unknown", "duplicate", "missing", "wrong_type", "multiple_values", "nested",
                                     "root_attribute", "mixed_text", "missing_header", "bad_integer", "bad_metadata"])
def test_sort_rejects_malformed_requests_before_mutation(actions, store, mutation):
    envelope = sort_request(store)
    root = envelope.document
    if mutation == "unknown":
        ET.SubElement(root, "F2", T="S")
    elif mutation == "duplicate":
        root.append(deepcopy(root.find("F1")))
    elif mutation == "missing":
        root.remove(root.find("F4"))
    elif mutation == "wrong_type":
        root.find("F5").set("T", "I")
    elif mutation == "multiple_values":
        ET.SubElement(root.find("F1"), "V").text = "1"
    elif mutation == "nested":
        ET.SubElement(root.find("F1/V"), "V").text = "1"
    elif mutation == "root_attribute":
        root.set("T", "S")
    elif mutation == "mixed_text":
        root.find("F1").tail = "unexpected"
    elif mutation == "missing_header":
        root.remove(root.find("CT"))
    elif mutation == "bad_integer":
        root.find("CT/V").text = "2147483648"
    elif mutation == "bad_metadata":
        root.find("VN").set("T", "S")
    assert_unchanged_error(store, lambda: actions.save_sort(envelope, store), "invalid_request_fields")


def test_new_filter_registration_is_not_treated_as_sort(actions, store):
    assert_unchanged_error(store, lambda: actions.save_sort(sort_request(store, number=26), store), "invalid_request_fields")


@pytest.mark.parametrize("identity", ["UI", "UK", "MK"])
def test_sort_authenticates_account_and_method_key(actions, store, identity):
    envelope = sort_request(store)
    envelope.document.find(identity + "/V").text = "another-account"
    assert_unchanged_error(store, lambda: actions.save_sort(envelope, store))


def test_sort_preflights_response_before_commit(actions, store):
    envelope = sort_request(store)
    envelope = replace(envelope, profile=replace(envelope.profile, response_limit=1))
    assert_unchanged_error(store, lambda: actions.save_sort(envelope, store), "response_too_large")


def test_sort_native_http_and_config_reload_after_reopening(actions, store, tmp_path):
    def application():
        return create_app(tmp_path / "http", initial_state=store.profile().state,
                          handlers=actions.bootstrap.handlers() | actions.handlers())
    app = application()
    envelope = sort_request(app.state.store)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert response.status_code == 200
        assert children(decode_wire(response.content, envelope.profile.response_key))["PGC"].children[0].values == ("F1", "F4")
    saved = app.state.store.profile()
    app = application()
    envelope = request(app.state.store, 36, ())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert response.status_code == 200
        config = children(decode_wire(response.content, envelope.profile.response_key))
        assert tuple(config[key].values[0] for key in ("F1", "F4", "F5")) == ("010203", "0001", "1010")
    assert app.state.store.profile() == saved


def test_lock_updates_existing_unit_with_complete_attributes_and_is_idempotent(actions, store):
    before = store.profile()
    envelope = lock_request(store)
    response = actions.save_locks(envelope, store)
    after = store.profile()
    expected = deepcopy(before.state)
    expected["player_state"]["units"][1]["GeneralPurposeFlags"] = 0x103
    assert after.state == expected
    unit_response = children(response)["PPU"]
    assert len(unit_response.children) == 21
    columns = {column.name: column.values for column in unit_response.children}
    assert columns["A7"] == (1,) and columns["AE"] == (0x103,)
    assert columns["A2"] == (20,) and columns["AD"] == (3,) and columns["AB"] == (2,)
    assert columns["B4"] == columns["B5"] == (False,)
    assert actions.save_locks(envelope, store) == response
    assert store.profile() == after
    actions.save_locks(lock_request(store, ((1, 0x101),)), store)
    assert store.profile().state == before.state


def test_lock_omits_extra_barracks_rows_the_update_receiver_cannot_find(actions, store):
    current = store.profile()
    current.state["player_state"]["units"][1]["GeneralPurposeFlags"] = 0x111
    store.update_state(current.state, current.revision)
    response = actions.save_locks(lock_request(store, ((0, 2), (1, 0x113))), store)
    assert [unit["GeneralPurposeFlags"] for unit in store.profile().state["player_state"]["units"]] == [2, 0x113]
    columns = {column.name: column.values for column in children(response)["PPU"].children}
    assert columns["A7"] == (0,) and columns["AE"] == (2,)
    response = actions.save_locks(lock_request(store, ((1, 0x111),)), store)
    assert all(not column.values for column in children(response)["PPU"].children)
    assert store.profile().state["player_state"]["units"][1]["GeneralPurposeFlags"] == 0x111


def test_lock_ignores_stale_nonlock_flags_and_preserves_signed_bits(actions, store):
    current = store.profile()
    current.state["player_state"]["units"][1]["GeneralPurposeFlags"] = -0x80000000 + 0x101
    store.update_state(current.state, current.revision)
    actions.save_locks(lock_request(store, ((1, 2),)), store)
    unit = store.profile().state["player_state"]["units"][1]
    assert unit["GeneralPurposeFlags"] == -0x80000000 + 0x103


def test_lock_batch_preserves_player_counts_and_identity_highwater(actions, store):
    before = store.profile()
    actions.save_locks(lock_request(store, ((0, 2), (1, 0x103))), store)
    after = store.profile()
    assert len(after.state["player_state"]["units"]) == 2
    assert after.state["player_state"]["battle"] == before.state["player_state"]["battle"]
    assert after.state["tables"] == before.state["tables"]
    assert [unit["GeneralPurposeFlags"] for unit in after.state["player_state"]["units"]] == [2, 0x103]


@pytest.mark.parametrize("columns", [(), (Column("A7", "I", ()), Column("AE", "I", ()))])
def test_empty_lock_map_is_noop(actions, store, columns):
    before = store.profile()
    response = actions.save_locks(request(store, 119, (Container("PU", columns),)), store)
    assert store.profile() == before
    assert all(not column.values for column in children(response)["PPU"].children)


@pytest.mark.parametrize("rows,code", [(((1, 2), (99, 2)), "unit_not_owned"),
    (((1, 2), (1, 0)), "invalid_request_fields"), (((-1, 2),), "invalid_request_fields")])
def test_invalid_lock_batch_does_not_partially_apply(actions, store, rows, code):
    assert_unchanged_error(store, lambda: actions.save_locks(lock_request(store, rows), store), code)


@pytest.mark.parametrize("mutation", ["missing_flags", "mismatched_rows", "extra_field", "wrong_type", "missing_map", "duplicate_map"])
def test_lock_rejects_partial_or_extra_fields(actions, store, mutation):
    envelope = lock_request(store)
    root, table = envelope.document, envelope.document.find("PU")
    if mutation == "missing_flags":
        table.remove(table.find("AE"))
    elif mutation == "mismatched_rows":
        ET.SubElement(table.find("AE"), "V").text = "2"
    elif mutation == "extra_field":
        ET.SubElement(table, "A4", T="I")
    elif mutation == "wrong_type":
        table.find("AE").set("T", "S")
    elif mutation == "missing_map":
        root.remove(table)
    elif mutation == "duplicate_map":
        root.append(deepcopy(table))
    assert_unchanged_error(store, lambda: actions.save_locks(envelope, store), "invalid_request_fields")


def test_lock_response_capacity_is_checked_before_commit(actions, store):
    envelope = lock_request(store)
    envelope = replace(envelope, profile=replace(envelope.profile, response_limit=1))
    assert_unchanged_error(store, lambda: actions.save_locks(envelope, store), "response_too_large")


def test_lock_retry_preserves_concurrent_unit_changes(actions, store, monkeypatch):
    original_update = store.update_state
    calls = []
    def update(state, revision):
        if not calls:
            calls.append(revision)
            concurrent = store.profile()
            concurrent.state["player_state"]["units"][1].update(Exp=17, Love=12, GeneralPurposeFlags=0x501)
            original_update(concurrent.state, concurrent.revision)
        return original_update(state, revision)
    monkeypatch.setattr(store, "update_state", update)
    response = actions.save_locks(lock_request(store), store)
    unit = store.profile().state["player_state"]["units"][1]
    assert (unit["Exp"], unit["Love"], unit["GeneralPurposeFlags"]) == (17, 12, 0x503)
    columns = {column.name: column.values for column in children(response)["PPU"].children}
    assert (columns["A4"], columns["A5"], columns["AE"]) == ((17,), (12,), (0x503,))


def test_lock_native_http_and_roster_reload_do_not_duplicate_units(actions, store, tmp_path):
    def application():
        return create_app(tmp_path / "lock-http", initial_state=store.profile().state,
                          handlers=actions.players.handlers() | actions.handlers())
    app = application()
    envelope = lock_request(app.state.store)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for _ in range(2):
            response = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
            assert response.status_code == 200
            assert children(decode_wire(response.content, envelope.profile.response_key))["PPU"].children[6].values == (1,)
    saved = app.state.store.profile()
    app = application()
    envelope = request(app.state.store, 41, ())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert response.status_code == 200
        columns = children(decode_wire(response.content, envelope.profile.response_key))
        assert columns["A7"].values == (0, 1) and columns["AE"].values == (0, 0x103)
    assert app.state.store.profile() == saved
