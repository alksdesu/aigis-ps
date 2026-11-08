from copy import deepcopy
from dataclasses import replace
import logging
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.app import create_app
from aigis_ps.bootstrap import BootstrapService
from aigis_ps.errors import ProtocolError
from aigis_ps.filter_state import FilterService, filter_response, merge_filters, name_hash, parse_filters, read_filters
from aigis_ps.protocol import NATIVE_CELL_BYTES, decode_wire, encode_wire
from test_unit_actions import actions, asset_root, assets, master, request, service, sort_request, store


@pytest.fixture
def filters(actions):
    actions.bootstrap.filter_provider = filter_response
    return FilterService(actions.bootstrap)


def filter_request(store, rows=(("TeamEdit", "1,2"), ("UnitCheck", "3")), *, names=True):
    columns = [Column("FilterNameHash", "I", tuple(name_hash(name) for name, _ in rows)),
               Column("SaveValue", "S", tuple(value for _, value in rows))]
    if names:
        columns.insert(0, Column("FilterName", "S", tuple(name for name, _ in rows)))
    return request(store, 26, (Container("Filter", tuple(columns)),))


def reject(filters, store, envelope, code=None):
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        filters.save(envelope, store)
    if code:
        assert error.value.code == code
    assert store.profile() == before


def test_filter_name_hash_matches_native_examples():
    assert name_hash("TeamEdit") == -813072340
    assert name_hash("UnitCheck") == -1369599755


def test_save_filters_creates_versioned_rows_and_preserves_other_state(filters, store):
    before = store.profile()
    envelope = filter_request(store)
    result = filters.save(envelope, store)
    after = store.profile()
    assert [child.name for child in loads(result).children] == ["SYSTEM", "PGC"]
    expected = deepcopy(before.state)
    expected["filter_settings_schema"] = 1
    expected["tables"]["filter_settings"] = [
        {"FilterName": name, "FilterNameHash": name_hash(name), "SaveValue": value}
        for name, value in (("TeamEdit", "1,2"), ("UnitCheck", "3"))]
    assert after.state == expected
    assert filters.save(envelope, store) == result and store.profile() == after


def test_partial_update_and_clear_preserve_unsubmitted_groups_and_names(filters, store):
    filters.save(filter_request(store), store)
    filters.save(filter_request(store, (("TeamEdit", ""),), names=False), store)
    rows = {row["FilterName"]: row["SaveValue"] for row in read_filters(store.profile().state)}
    assert rows == {"TeamEdit": "", "UnitCheck": "3"}
    response = filter_response(store.profile().state)
    assert response.children[0].values == ("TeamEdit", "UnitCheck")
    assert response.children[2].values == ("", "3")


def test_sparse_names_are_joined_by_hash_not_position(filters, store):
    envelope = filter_request(store)
    name_column = envelope.document.find("Filter/FilterName")
    name_column.remove(name_column[0])
    filters.save(envelope, store)
    rows = read_filters(store.profile().state)
    assert [row["FilterName"] for row in rows] == ["", "UnitCheck"]
    filters.save(filter_request(store, (("TeamEdit", "1,2"),)), store)
    assert [row["FilterName"] for row in read_filters(store.profile().state)] == ["TeamEdit", "UnitCheck"]


@pytest.mark.parametrize("columns", [(), (Column("FilterNameHash", "I", ()), Column("SaveValue", "S", ()))])
def test_empty_filter_upload_does_not_clear_existing_groups(filters, store, columns):
    filters.save(filter_request(store), store)
    before = store.profile()
    filters.save(request(store, 26, (Container("Filter", columns),)), store)
    assert store.profile() == before


def test_legacy_empty_filters_load_without_migration(filters, store):
    before = store.profile()
    assert "filter_settings_schema" not in before.state
    response = filter_response(before.state)
    assert [column.name for column in response.children] == ["FilterName", "FilterNameHash", "SaveValue"]
    assert all(not column.values for column in response.children)
    filters.save(request(store, 26, (Container("Filter"),)), store)
    assert store.profile() == before


@pytest.mark.parametrize("value", ["", "0", "-2147483648,0,2147483647", "31,2,5"])
def test_selected_ids_preserve_native_order_and_signed_values(filters, store, value):
    filters.save(filter_request(store, (("TeamEdit", value),)), store)
    assert read_filters(store.profile().state)[0]["SaveValue"] == value


WHOLE_CELL = ",".join(map(str, range(1000, 1050))) + ",99999"


@pytest.mark.parametrize("value,expected", [
    (WHOLE_CELL, WHOLE_CELL),
    (WHOLE_CELL + ",7", WHOLE_CELL),
    (WHOLE_CELL[:249] + ",123456", WHOLE_CELL[:249]),
    (",".join(map(str, range(1000, 1052))), ",".join(map(str, range(1000, 1051))))])
def test_startup_reload_keeps_whole_identifiers_within_the_native_cell(filters, store, caplog, value, expected):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    assert len(WHOLE_CELL) == NATIVE_CELL_BYTES
    filters.save(filter_request(store, (("TeamEdit", value),)), store)
    state = store.profile().state
    assert read_filters(state)[0]["SaveValue"] == value
    reloaded = filter_response(state).children[2].values
    assert reloaded == (expected,) and len(expected.encode()) <= NATIVE_CELL_BYTES
    assert any("native cell" in record.getMessage() for record in caplog.records) == (value != expected)


@pytest.mark.parametrize("name,reloaded", [("N" * NATIVE_CELL_BYTES, "N" * NATIVE_CELL_BYTES), ("あ" * 85, "あ" * 85),
                                           ("あ" * 86, ""), ("N" * (NATIVE_CELL_BYTES + 1), "")])
def test_names_beyond_the_native_cell_reload_empty_with_hash_and_selection(filters, store, name, reloaded):
    filters.save(filter_request(store, ((name, "1,2"), ("TeamEdit", "3"))), store)
    state = store.profile().state
    assert [row["FilterName"] for row in read_filters(state)] == [name, "TeamEdit"]
    response = filter_response(state)
    assert response.children[0].values == (reloaded, "TeamEdit")
    assert response.children[1].values == (name_hash(name), name_hash("TeamEdit"))
    assert response.children[2].values == ("1,2", "3")


def test_long_selection_reloads_whole_identifiers_through_native_http(filters, actions, store, tmp_path):
    value = ",".join(map(str, range(1000, 1052)))
    app = create_app(tmp_path / "filter-long", initial_state=store.profile().state,
                     handlers=actions.bootstrap.handlers() | actions.handlers() | filters.handlers())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for envelope in (filter_request(app.state.store, (("TeamEdit", value),)), request(app.state.store, 137, ())):
            reply = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
            assert reply.status_code == 200
    groups = {node.name: node for node in loads(decode_wire(reply.content, envelope.profile.response_key)).children}
    assert groups["PlayerFilterParam"].children[2].values == (",".join(map(str, range(1000, 1051))),)
    assert read_filters(app.state.store.profile().state)[0]["SaveValue"] == value


@pytest.mark.parametrize("value", ["1,", ",1", "1,,2", "1, 2", "+1", "2147483648", "-2147483649",
                                   "1,1", "1.0", "x", "1\n", ",".join(map(str, range(4097)))])
def test_malformed_selection_is_rejected_without_saving(filters, store, value):
    reject(filters, store, filter_request(store, (("TeamEdit", value),)), "invalid_request_fields")


@pytest.mark.parametrize("mutation", ["duplicate_hash", "unequal_rows", "missing_value", "wrong_hash_type", "foreign_name",
                                     "duplicate_name", "extra_column", "duplicate_container", "missing_container", "nested_value"])
def test_filter_structure_and_name_alignment_are_strict(filters, store, mutation):
    envelope = filter_request(store)
    root, table = envelope.document, envelope.document.find("Filter")
    if mutation == "duplicate_hash":
        table.find("FilterNameHash")[1].text = table.find("FilterNameHash")[0].text
    elif mutation == "unequal_rows":
        table.find("SaveValue").remove(table.find("SaveValue")[1])
    elif mutation == "missing_value":
        table.remove(table.find("SaveValue"))
    elif mutation == "wrong_hash_type":
        table.find("FilterNameHash").set("T", "S")
    elif mutation == "foreign_name":
        table.find("FilterName")[0].text = "DifferentName"
    elif mutation == "duplicate_name":
        table.find("FilterName")[1].text = "TeamEdit"
    elif mutation == "extra_column":
        ET.SubElement(table, "F2", T="S")
    elif mutation == "duplicate_container":
        root.append(deepcopy(table))
    elif mutation == "missing_container":
        root.remove(table)
    elif mutation == "nested_value":
        ET.SubElement(table.find("SaveValue")[0], "V").text = "1"
    reject(filters, store, envelope, "invalid_request_fields")


def test_sort_upload_is_not_accepted_as_filter(filters, store):
    reject(filters, store, sort_request(store), "invalid_request_fields")


@pytest.mark.parametrize("field", ["UI", "UK", "MK"])
def test_filter_save_checks_account_and_method_key(filters, store, field):
    envelope = filter_request(store)
    envelope.document.find(field + "/V").text = "other"
    reject(filters, store, envelope)


def test_save_checks_complete_startup_response_capacity(filters, store):
    filters._startup_profile = replace(filters._startup_profile, response_limit=1)
    reject(filters, store, filter_request(store), "response_too_large")


@pytest.mark.parametrize("version,rows", [(2, []), (True, []), (None, [{"FilterNameHash": 1, "SaveValue": ""}]),
    (1, [{"FilterName": "Wrong", "FilterNameHash": 1, "SaveValue": ""}]),
    (1, [{"FilterName": "", "FilterNameHash": 1, "SaveValue": "a"}])])
def test_invalid_stored_filters_are_not_silently_replaced(filters, store, version, rows):
    profile = store.profile()
    profile.state["tables"]["filter_settings"] = rows
    if version is not None:
        profile.state["filter_settings_schema"] = version
    store.update_state(profile.state, profile.revision)
    reject(filters, store, filter_request(store), "filter_state_invalid")


def test_filter_retry_preserves_concurrent_group(filters, store, monkeypatch):
    original_update = store.update_state
    calls = []
    def update(state, revision):
        if not calls:
            calls.append(revision)
            profile = store.profile()
            concurrent = merge_filters(profile.state, [{"FilterName": "Concurrent", "FilterNameHash": name_hash("Concurrent"), "SaveValue": "7"}])
            original_update(concurrent, profile.revision)
        return original_update(state, revision)
    monkeypatch.setattr(store, "update_state", update)
    filters.save(filter_request(store), store)
    assert {row["FilterName"] for row in read_filters(store.profile().state)} == {"Concurrent", "TeamEdit", "UnitCheck"}


def test_sort_then_filter_native_http_and_full_startup_reload(filters, actions, store, tmp_path):
    def application():
        return create_app(tmp_path / "filter-http", initial_state=store.profile().state,
                          handlers=actions.bootstrap.handlers() | actions.handlers() | filters.handlers())
    app = application()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for envelope in (sort_request(app.state.store), filter_request(app.state.store)):
            reply = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
            assert reply.status_code == 200
    saved = app.state.store.profile()
    app = application()
    envelope = request(app.state.store, 137, ())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reply = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert reply.status_code == 200
        groups = {node.name: node for node in loads(decode_wire(reply.content, envelope.profile.response_key)).children}
        assert groups["PlayerFilterParam"] == filter_response(saved.state)
    assert app.state.store.profile() == saved


def test_filter_provider_configuration_and_return_contract(asset_root, store):
    with pytest.raises(ValueError):
        BootstrapService(asset_root, filter_provider=3)
    bootstrap = BootstrapService(asset_root, filter_provider=lambda _: Container("Wrong"))
    with pytest.raises(ProtocolError, match="Filter provider"):
        bootstrap._configuration_response(store.profile())
