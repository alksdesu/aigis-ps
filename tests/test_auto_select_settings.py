from copy import deepcopy
from dataclasses import replace
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column
from aigis_ps.app import create_app
from aigis_ps.protocol import decode_wire, encode_wire
from test_unit_actions import actions, assert_unchanged_error, children, request, store
from test_bootstrap import asset_root
from test_player_state import assets, master, service


def auto_select_request(store, values=(513, -2147483648)):
    return request(store, 27, tuple(Column(name, "I", (value,)) for name, value in zip(("F6", "FB"), values)))


@pytest.mark.parametrize("values", [(0, 0), (-2147483648, 2147483647), (2147483647, -1), (513, 1027)])
def test_auto_select_replaces_both_signed_values_and_preserves_other_state(actions, store, values):
    current = store.profile()
    current.state["tables"]["config"][0].update(AutoSelectConfig=-1, IntegrationAutoSelectConfig=31)
    store.update_state(current.state, current.revision)
    before = store.profile()
    envelope = auto_select_request(store, values)
    response = actions.save_auto_select(envelope, store)
    after = store.profile()
    expected = deepcopy(before.state)
    expected["tables"]["config"][0].update(AutoSelectConfig=values[0], IntegrationAutoSelectConfig=values[1])
    assert after.state == expected
    delta = children(response)["PGC"]
    assert delta.children[0].values == ("F6", "FB")
    assert delta.children[1].values == tuple(map(str, values))
    assert actions.save_auto_select(envelope, store) == response
    assert store.profile() == after


@pytest.mark.parametrize("mutation", [
    "missing_f6", "missing_fb", "unknown", "duplicate", "boolean", "string", "multiple",
    "empty", "nested", "overflow", "underflow", "root_attribute", "missing_header", "mixed_text",
])
def test_auto_select_rejects_invalid_request_without_mutation(actions, store, mutation):
    envelope = auto_select_request(store)
    root, f6 = envelope.document, envelope.document.find("F6")
    if mutation.startswith("missing_f"):
        root.remove(root.find(mutation.removeprefix("missing_").upper()))
    elif mutation == "unknown":
        ET.SubElement(root, "F5", T="I")
    elif mutation == "duplicate":
        root.append(deepcopy(f6))
    elif mutation == "boolean":
        f6.set("T", "B")
        f6.find("V").text = "1"
    elif mutation == "string":
        f6.set("T", "S")
    elif mutation == "multiple":
        ET.SubElement(f6, "V").text = "0"
    elif mutation == "empty":
        f6.remove(f6.find("V"))
    elif mutation == "nested":
        ET.SubElement(f6.find("V"), "V").text = "0"
    elif mutation in {"overflow", "underflow"}:
        f6.find("V").text = "2147483648" if mutation == "overflow" else "-2147483649"
    elif mutation == "root_attribute":
        root.set("T", "S")
    elif mutation == "missing_header":
        root.remove(root.find("CT"))
    elif mutation == "mixed_text":
        f6.tail = "unexpected"
    assert_unchanged_error(store, lambda: actions.save_auto_select(envelope, store), "invalid_request_fields")


@pytest.mark.parametrize("identity", ["UI", "UK", "MK"])
def test_auto_select_requires_matching_identity(actions, store, identity):
    envelope = auto_select_request(store)
    envelope.document.find(identity + "/V").text = "another-account"
    assert_unchanged_error(store, lambda: actions.save_auto_select(envelope, store))


def test_auto_select_rejects_other_operation(actions, store):
    envelope = request(store, 25, (Column("F6", "I", (0,)), Column("FB", "I", (0,))))
    assert_unchanged_error(store, lambda: actions.save_auto_select(envelope, store), "invalid_request_fields")


@pytest.mark.parametrize("operation", [27, 36])
def test_auto_select_checks_both_wire_responses_before_commit(actions, store, operation):
    envelope = auto_select_request(store)
    if operation == 27:
        envelope = replace(envelope, profile=replace(envelope.profile, response_limit=1))
    else:
        actions._configuration_profile = replace(actions._configuration_profile, response_limit=1)
    assert_unchanged_error(store, lambda: actions.save_auto_select(envelope, store), "response_too_large")


@pytest.mark.parametrize("field", ["AutoSelectConfig", "IntegrationAutoSelectConfig"])
@pytest.mark.parametrize("value", [True, "1", 2147483648, -2147483649])
def test_auto_select_does_not_overwrite_corrupt_existing_preferences(actions, store, field, value):
    current = store.profile()
    current.state["tables"]["config"][0][field] = value
    store.update_state(current.state, current.revision)
    assert_unchanged_error(store, lambda: actions.save_auto_select(auto_select_request(store), store))


def test_auto_select_retry_preserves_concurrent_config_and_unit_changes(actions, store, monkeypatch):
    update = store.update_state
    calls = []

    def interleaved(state, revision):
        if not calls:
            calls.append(revision)
            current = store.profile()
            current.state["tables"]["config"][0].update(MenuTutorialFlags=71, _FlagSort="0312")
            current.state["player_state"]["units"][1]["Exp"] = 90
            update(current.state, current.revision)
        return update(state, revision)

    monkeypatch.setattr(store, "update_state", interleaved)
    actions.save_auto_select(auto_select_request(store), store)
    state = store.profile().state
    row = state["tables"]["config"][0]
    assert (row["AutoSelectConfig"], row["IntegrationAutoSelectConfig"]) == (513, -2147483648)
    assert (row["MenuTutorialFlags"], row["_FlagSort"]) == (71, "0312")
    assert state["player_state"]["units"][1]["Exp"] == 90


def test_auto_select_conflict_exhaustion_does_not_commit(actions, store, monkeypatch):
    calls = []

    def conflict(state, revision):
        calls.append(revision)
        raise ValueError("Conflict")

    monkeypatch.setattr(store, "update_state", conflict)
    assert_unchanged_error(store, lambda: actions.save_auto_select(auto_select_request(store), store), "profile_changed")
    assert len(calls) == 3


def test_auto_select_native_http_and_full_config_after_reopening(actions, store, tmp_path):
    def application():
        return create_app(tmp_path / "auto-select-http", initial_state=store.profile().state,
                          handlers=actions.bootstrap.handlers() | actions.handlers())

    app = application()
    envelope = auto_select_request(app.state.store)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert response.status_code == 200
        assert children(decode_wire(response.content, envelope.profile.response_key))["PGC"].children[1].values == ("513", "-2147483648")
        saved = app.state.store.profile()
        response = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert response.status_code == 200
        assert app.state.store.profile() == saved
    app = application()
    envelope = request(app.state.store, 36, ())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert response.status_code == 200
        config = children(decode_wire(response.content, envelope.profile.response_key))
        assert config["F6"].values == (513,)
        assert config["FB"].values == (-2147483648,)
    assert app.state.store.profile() == saved
