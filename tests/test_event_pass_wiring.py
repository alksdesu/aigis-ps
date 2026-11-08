from argparse import Namespace
from copy import copy, deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from aigis_ps import cli
from aigis_ps.alxml import Column, Container, loads
from aigis_ps.bootstrap import BootstrapService
from aigis_ps.errors import ProtocolError
from aigis_ps.storage import Store
from test_quest_service import bundle, request


def legacy_row():
    return {"ID": "old_pass_unknown_to_current_catalog", "ReceivedNormalPoint": 35,
            "ReceivedPremiumPoint": 20}


def test_bootstrap_rejects_noncallable_event_pass_provider(tmp_path):
    with pytest.raises(ValueError, match="Event-pass provider"):
        BootstrapService(tmp_path, event_pass_provider=True)


def test_cli_binds_shared_dependencies_provider_and_handlers(bundle):
    assert bundle.event_pass_error is None
    service = bundle.event_passes
    assert service is not None
    assert service.bootstrap is bundle.bootstrap
    assert service.players is bundle.player_state
    assert service.presents is bundle.presents
    assert service.possessions is bundle.possessions
    assert set(service.handlers()) == {158, 159}
    assert bundle.bootstrap.event_pass_provider.__self__ is service
    assert bundle.bootstrap.status()["event_pass_provider_configured"] is True
    status = cli.service_readiness(bundle)["event_passes"]
    assert status["ready"] is True
    assert status["catalog"]["pass_count"] == 6
    assert status["catalog"]["reward_count"] == 264
    assert status["catalog"]["status_counts"] == {2: 6}


def test_bundle_initializes_empty_pass_state_without_inventory_or_awards(bundle):
    original = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    before = deepcopy(original)
    updated = bundle.initialize(original)
    assert updated["event_pass_state"] == {"schema_version": 1, "passes": {}}
    assert updated["tables"]["event_pass_status"] == []
    assert updated["tables"]["possessions"] == before["tables"]["possessions"]
    assert updated["tables"]["presents"] == before["tables"]["presents"]
    assert original == before


def test_bundle_preserves_unknown_legacy_row_and_establishes_baseline_once(bundle):
    original = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    original["tables"]["event_pass_status"] = [legacy_row()]
    saved = deepcopy(original)
    initialized = bundle.initialize(original)
    assert initialized["tables"]["event_pass_status"] == [legacy_row()]
    baseline = initialized["event_pass_state"]["passes"][legacy_row()["ID"]]
    assert baseline["definition"] is None
    assert (baseline["baseline_normal"], baseline["baseline_premium"]) == (35, 20)
    assert baseline["deliveries"] == {"normal": {}, "premium": {}}
    assert bundle.initialize(initialized) == initialized
    assert original == saved


@pytest.mark.parametrize("key", ["event_pass_state", "event_pass_history", "event_pass_delivery"])
def test_missing_service_rejects_every_existing_event_pass_domain(bundle, key):
    original = bundle.initialize(bundle.bootstrap.initial_state())
    original.pop("event_pass_state", None)
    original[key] = None
    saved = deepcopy(original)
    broken = replace(bundle, event_passes=None, event_pass_error="fixture_unavailable")
    with pytest.raises(ProtocolError) as caught:
        broken.initialize(original)
    assert caught.value.code == "event_pass_service_unavailable"
    assert original == saved


def test_missing_service_rejects_nonempty_native_rows_without_journal(bundle):
    original = bundle.initialize(bundle.bootstrap.initial_state())
    original.pop("event_pass_state", None)
    original["tables"]["event_pass_status"] = [legacy_row()]
    broken = replace(bundle, event_passes=None, event_pass_error="fixture_unavailable")
    with pytest.raises(ProtocolError) as caught:
        broken.initialize(original)
    assert caught.value.code == "event_pass_service_unavailable"
    assert original["tables"]["event_pass_status"] == [legacy_row()]


def test_missing_service_allows_empty_legacy_state_and_reports_unavailable(bundle):
    original = bundle.initialize(bundle.bootstrap.initial_state())
    original.pop("event_pass_state", None)
    broken = replace(bundle, event_passes=None, event_pass_error="fixture_unavailable")
    assert broken.initialize(original) == original
    assert cli.service_readiness(broken)["event_passes"] == {
        "ready": False, "error": "fixture_unavailable"}


def test_137_and_158_return_same_complete_native_rows_without_mutation(bundle, tmp_path):
    original = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    original["tables"]["event_pass_status"] = [legacy_row(),
        {"ID": "event_pass_20261015", "ReceivedNormalPoint": 10, "ReceivedPremiumPoint": 0}]
    initialized = bundle.initialize(original)
    store = Store(tmp_path / "status.sqlite3", initialized)
    before = store.profile()
    startup = loads(bundle.bootstrap.configuration_database(request(store, 137, ()), store))
    explicit = loads(bundle.event_passes.read(request(store, 158, ()), store))
    full = next(group for group in startup.children if group.name == "EventPassStatus")
    selected = next(group for group in explicit.children if group.name == "EventPassStatus")
    assert full == selected == Container("EventPassStatus", (
        Column("ID", "S", (legacy_row()["ID"], "event_pass_20261015")),
        Column("ReceivedNormalPoint", "I", (35, 10)), Column("ReceivedPremiumPoint", "I", (20, 0))))
    assert store.profile() == before


@pytest.mark.parametrize("result", [None, "wrong", Column("EventPassStatus", "S", ()),
                                     Container("Other"), Container("EventPassStatus", index="1")])
def test_bootstrap_rejects_incorrect_event_pass_provider_shape(bundle, tmp_path, result):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.event_pass_provider = lambda _: result
    store = Store(tmp_path / "status.sqlite3", bundle.initialize(bundle.bootstrap.initial_state()))
    before = store.profile()
    with pytest.raises(ProtocolError) as caught:
        bootstrap.configuration_database(request(store, 137, ()), store)
    assert caught.value.code == "event_pass_provider_invalid"
    assert store.profile() == before


def test_bootstrap_without_provider_keeps_old_empty_only_contract(bundle, tmp_path):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.event_pass_provider = None
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    store = Store(tmp_path / "status.sqlite3", initial)
    response = loads(bootstrap.configuration_database(request(store, 137, ()), store))
    assert next(group for group in response.children if group.name == "EventPassStatus") == Container("EventPassStatus")
    profile = store.profile()
    updated = deepcopy(profile.state)
    updated["tables"]["event_pass_status"] = [legacy_row()]
    store.update_state(updated, profile.revision)
    before = store.profile()
    with pytest.raises(ProtocolError) as caught:
        bootstrap.configuration_database(request(store, 137, ()), store)
    assert caught.value.code == "collection_rows_unavailable"
    assert store.profile() == before


def test_event_pass_provider_does_not_disable_other_unimplemented_collection_checks(bundle, tmp_path):
    initial = bundle.initialize(bundle.bootstrap.initial_state())
    initial["tables"]["recommendations"] = [{"unknown": 1}]
    store = Store(tmp_path / "status.sqlite3", initial)
    before = store.profile()
    with pytest.raises(ProtocolError) as caught:
        bundle.bootstrap.configuration_database(request(store, 137, ()), store)
    assert caught.value.code == "collection_rows_unavailable"
    assert store.profile() == before


def test_cli_preserves_constructor_failure_as_diagnostic(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ProtocolError("event_pass_fixture_unavailable", "Unavailable event pass fixture", 503)

    monkeypatch.setattr("aigis_ps.event_pass_service.EventPassService", unavailable)
    project = Path(__file__).resolve().parents[1]
    result = cli.services(Namespace(asset_dir=project.parent / "aigisr_exe",
        data_dir=project / "runtime/server", resource_dir=project / "runtime/resources", rules=None))
    assert result.event_passes is None
    assert result.event_pass_error == "event_pass_fixture_unavailable"
    assert result.bootstrap.event_pass_provider is None
    assert cli.service_readiness(result)["event_passes"] == {
        "ready": False, "error": "event_pass_fixture_unavailable"}


def test_serve_registers_158_and_159_without_starting_listener(bundle, tmp_path, monkeypatch):
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
    assert captured["handlers"][158].__self__ is bundle.event_passes
    assert captured["handlers"][159].__self__ is bundle.event_passes
    assert captured["state_initializer"].__self__ is bundle
    assert captured["initial_state"]["event_pass_state"] == {"schema_version": 1, "passes": {}}
    assert not directory.exists()
