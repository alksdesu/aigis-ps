from argparse import Namespace
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from aigis_ps import cli
from aigis_ps.alxml import Column, loads
from aigis_ps.config_state import (
    CONFIGURATION_ALIASES,
    CONFIGURATION_DELTA_FIELDS,
    CONFIGURATION_FIELDS,
    CONFIGURATION_REQUIRED_FIELDS,
    CONFIGURATION_STRING_SIZES,
    configuration_delta,
    configuration_row,
    patch_configuration,
)
from aigis_ps.errors import ProtocolError
from aigis_ps.storage import Store
from test_quest_service import bundle, request


def minimal_state(value=""):
    row = {name: False if CONFIGURATION_FIELDS[name][1] is bool else 0
           for name in CONFIGURATION_REQUIRED_FIELDS}
    row.update(EventViewRequest=value, future_field={"preserve": [1]})
    return {"bootstrap_schema": 1, "tables": {"config": [row]}, "other": [1]}


def test_configuration_exposes_f8_storage_without_unverified_delta():
    assert CONFIGURATION_FIELDS["EventViewRequest"] == ("F8", str)
    assert CONFIGURATION_ALIASES["F8"] == "EventViewRequest"
    assert CONFIGURATION_STRING_SIZES["EventViewRequest"] == 256
    assert "EventViewRequest" not in CONFIGURATION_REQUIRED_FIELDS
    assert "EventViewRequest" not in CONFIGURATION_DELTA_FIELDS
    with pytest.raises(ProtocolError) as caught:
        configuration_delta(configuration_row(minimal_state()), ("EventViewRequest",))
    assert caught.value.code == "invalid_configuration_state"


@pytest.mark.parametrize("value", ["", "x" * 255, "役" * 85, "old_format=keep,<unknown>&\"'"])
def test_configuration_preserves_bounded_historical_strings(value):
    original = minimal_state("previous")
    before = deepcopy(original)
    updated = patch_configuration(original, {"EventViewRequest": value})
    assert configuration_row(updated)["EventViewRequest"] == value
    assert original == before
    expected = deepcopy(before)
    expected["tables"]["config"][0]["EventViewRequest"] = value
    assert updated == expected
    updated["tables"]["config"][0]["future_field"]["preserve"].append(2)
    assert original == before


@pytest.mark.parametrize("value", [None, 0, False, [], "x" * 256, "役" * 86, "bad\x00value", "bad\ud800"])
def test_invalid_f8_is_rejected_by_storage_and_other_configuration_writes(value):
    original = minimal_state(value)
    before = deepcopy(original)
    for operation in (lambda: configuration_row(original),
                      lambda: patch_configuration(original, {"UseBgm": True}),
                      lambda: patch_configuration(minimal_state(), {"EventViewRequest": value})):
        with pytest.raises(ProtocolError) as caught:
            operation()
        assert caught.value.code == "invalid_configuration_state"
    assert original == before


def test_partial_helper_fixture_without_f8_is_not_implicitly_migrated():
    original = minimal_state()
    del original["tables"]["config"][0]["EventViewRequest"]
    updated = patch_configuration(original, {"UseBgm": True})
    assert "EventViewRequest" not in configuration_row(updated)
    assert "EventViewRequest" not in original["tables"]["config"][0]


def test_cli_constructs_bound_view_event_service_and_reports_status(bundle):
    assert bundle.view_event_error is None
    assert bundle.view_events is not None
    assert bundle.view_events.bootstrap is bundle.bootstrap
    assert bundle.view_events.players is bundle.player_state
    assert set(bundle.view_events.handlers()) == {23, 24}
    assert cli.service_readiness(bundle)["view_events"]["ready"] is True


def test_bundle_initializer_preserves_legacy_f8_and_calls_service(bundle, monkeypatch):
    original = bundle.initialize(bundle.bootstrap.initial_state())
    original["tables"]["config"][0]["EventViewRequest"] = "legacy_format=keep"
    before = deepcopy(original)
    initialize = bundle.view_events.initialize
    calls = []

    def observe(state):
        calls.append(deepcopy(state))
        return initialize(state)

    monkeypatch.setattr(bundle.view_events, "initialize", observe)
    updated = bundle.initialize(original)
    assert len(calls) == 1
    assert calls[0]["tables"]["config"][0]["EventViewRequest"] == "legacy_format=keep"
    assert updated == before
    assert original == before
    updated["tables"]["config"][0]["EventViewRequest"] = ""
    assert original == before


@pytest.mark.parametrize("history", [None, {}, []])
def test_missing_view_service_rejects_any_existing_history(bundle, history):
    original = bundle.initialize(bundle.bootstrap.initial_state())
    original["view_event_history"] = history
    before = deepcopy(original)
    broken = replace(bundle, view_events=None, view_event_error="fixture_unavailable")
    with pytest.raises(ProtocolError) as caught:
        broken.initialize(original)
    assert caught.value.code == "view_event_service_unavailable"
    assert original == before


def test_missing_view_service_does_not_erase_legacy_request_without_history(bundle):
    original = bundle.initialize(bundle.bootstrap.initial_state())
    original["tables"]["config"][0]["EventViewRequest"] = "legacy_format=keep"
    original.pop("view_event_history", None)
    broken = replace(bundle, view_events=None, view_event_error="fixture_unavailable")
    assert broken.initialize(original) == original
    assert cli.service_readiness(broken)["view_events"] == {
        "ready": False, "error": "fixture_unavailable"}


def test_cli_captures_view_service_construction_failure(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ProtocolError("view_fixture_unavailable", "Unavailable view fixture", 503)

    monkeypatch.setattr("aigis_ps.view_events.ViewEventService", unavailable)
    project = Path(__file__).resolve().parents[1]
    result = cli.services(Namespace(asset_dir=project.parent / "aigisr_exe",
        data_dir=project / "runtime/server", resource_dir=project / "runtime/resources", rules=None))
    assert result.view_events is None
    assert result.view_event_error == "view_fixture_unavailable"
    assert cli.service_readiness(result)["view_events"] == {
        "ready": False, "error": "view_fixture_unavailable"}


@pytest.mark.parametrize("value", ["", "旧形式=保存,<opaque>&\"'", "x" * 255])
def test_bootstrap_36_returns_exact_f8_and_does_not_mutate_state(bundle, tmp_path, value):
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state["tables"]["config"][0]["EventViewRequest"] = value
    store = Store(tmp_path / "view.sqlite3", state)
    before = store.profile()
    result = loads(bundle.bootstrap.config(request(store, 36, ()), store))
    column = next(child for child in result.children if child.name == "F8")
    assert column == Column("F8", "S", (value,))
    assert store.profile() == before


@pytest.mark.parametrize("number", [21, 22])
def test_old_configuration_actions_still_reject_f8(bundle, tmp_path, number):
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state["tables"]["config"][0]["EventViewRequest"] = "legacy_format=keep"
    store = Store(tmp_path / "view.sqlite3", state)
    before = store.profile()
    with pytest.raises(ProtocolError) as caught:
        bundle.bootstrap.save_configuration(request(store, number, (Column("F8", "S", ("",)),)), store)
    assert caught.value.code == "invalid_configuration_request"
    assert store.profile() == before


def test_serve_registers_both_operations_without_starting_server(bundle, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "services", lambda _: bundle)
    monkeypatch.setattr(cli, "client_fingerprint", lambda _: bundle.masters.expected_hash)

    def create_app(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("aigis_ps.app.create_app", create_app)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    directory = tmp_path / "not-created"
    assert cli.main(["serve", "--data-dir", str(directory)]) == 0
    for number in (23, 24):
        assert captured["handlers"][number].__self__ is bundle.view_events
    assert captured["state_initializer"].__self__ is bundle
    assert not directory.exists()
