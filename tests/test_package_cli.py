from argparse import Namespace
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from aigis_ps import cli
from aigis_ps.alxml import Container, loads
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.package_state import package_identity
from aigis_ps.storage import Store
from test_quest_service import bundle, request


EXAMPLE = Path(__file__).resolve().parents[1] / "config/event-pass-revival.example.json"
NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


def arguments(path=None):
    return Namespace(asset_dir=cli.DEFAULT_ASSETS, data_dir=cli.DEFAULT_DATA,
                     resource_dir=cli.DEFAULT_RESOURCES, rules=None, event_pass_revival=path)


@pytest.fixture(scope="module")
def revived():
    result = cli.services(arguments(EXAMPLE))
    assert result.packages is not None, result.package_error
    return result


def initial(bundle):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    return bundle.initialize(bootstrap.initial_state())


def test_default_catalog_is_disabled_but_native_directory_operations_are_registered(bundle):
    assert bundle.package_error is None
    service = bundle.packages
    assert service.catalog.enabled is False
    assert set(service.handlers()) == {46, 47, 50}
    assert service.bootstrap is bundle.bootstrap
    assert service.players is bundle.player_state
    assert service.possessions is bundle.possessions
    assert service.catalog.possessions is bundle.possessions
    assert service.purchases.catalog is service.catalog
    assert service.purchases.possessions is service.possessions
    assert cli.service_readiness(bundle)["packages"]["ready"] is True


def test_revival_enables_package_catalog_from_effective_pass_resources(revived):
    service = revived.packages
    assert service.catalog.enabled is True
    assert service.catalog.offers
    assert revived.package_error is None
    for offer in service.catalog.offers:
        definition = revived.event_passes.catalog.get(offer.normal_pass_id)
        assert offer.item_id == definition.premium_item_id
        assert offer.definition_hash == package_identity(definition.item_id, definition.premium_item_id)
    assert cli.service_readiness(revived)["packages"]["ready"] is True


def test_disabled_initializer_does_not_create_package_domain(bundle):
    original = initial(bundle)
    assert not any(name.startswith("package_") for name in original)
    assert bundle.initialize(original) == original


def test_enabled_initializer_records_existing_ownership_without_reward_changes(revived):
    original = initial(revived)
    offer = revived.packages.catalog.offers[0]
    original = revived.possessions.credit(original, {offer.item_id: 1})
    before = deepcopy(original)
    result = revived.initialize(original)
    entry = result["package_purchases"]["entries"][offer.normal_pass_id]
    assert entry == {"identity": offer.definition_hash, "count": 1, "source": "legacy_owned", "request": None}
    assert result["tables"] == before["tables"]
    assert result["event_pass_state"] == before["event_pass_state"]
    assert result["crystal_spending"] == before["crystal_spending"]
    assert original == before
    assert revived.initialize(result) == result


def test_disabling_revival_preserves_existing_purchase_baseline(bundle, revived):
    original = initial(revived)
    offer = revived.packages.catalog.offers[0]
    original = revived.possessions.credit(original, {offer.item_id: 1})
    original = revived.initialize(original)
    disabled = bundle.initialize(original)
    assert disabled["package_purchases"] == original["package_purchases"]
    assert disabled["tables"]["possessions"] == original["tables"]["possessions"]


@pytest.mark.parametrize("key", ["package_purchases", "package_history", "package_delivery"])
def test_missing_package_service_rejects_even_empty_or_null_saved_domains(bundle, key):
    original = initial(bundle)
    original[key] = None
    before = deepcopy(original)
    broken = replace(bundle, packages=None, package_error="fixture_unavailable")
    with pytest.raises(ProtocolError) as caught:
        broken.initialize(original)
    assert caught.value.code == "package_service_unavailable"
    assert original == before


def test_missing_package_service_allows_legacy_without_package_data(bundle):
    original = initial(bundle)
    broken = replace(bundle, packages=None, package_error="fixture_unavailable")
    assert broken.initialize(original) == original
    assert cli.service_readiness(broken)["packages"] == {"ready": False, "error": "fixture_unavailable"}


def test_missing_package_service_blocks_enabled_revival_readiness(revived):
    broken = replace(revived, packages=None, package_error="fixture_unavailable")
    status = cli.service_readiness(broken)
    assert status["startup_ready"] is False
    assert status["packages"]["error"] == "fixture_unavailable"


def test_catalog_construction_failure_cannot_silently_ignore_enabled_feature(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ProtocolError("fixture_package_catalog", "Unavailable package resource", 503)
    monkeypatch.setattr("aigis_ps.package_catalog.PackageCatalog", unavailable)
    result = cli.services(arguments(EXAMPLE))
    assert result.packages is None
    assert result.package_error == "fixture_package_catalog"
    assert cli.service_readiness(result)["startup_ready"] is False


@pytest.mark.parametrize("number", [46, 47])
def test_disabled_directory_is_empty_and_read_only(bundle, tmp_path, number):
    original = initial(bundle)
    store = Store(tmp_path / "package-directory.sqlite3", original)
    before = store.profile()
    handler = bundle.packages.handlers()[number]
    response = loads(handler(request(store, number, ()), store))
    groups = {child.name: child for child in response.children}
    assert "PSC" in groups and "PPF" in groups and "POSSESSION" in groups
    assert all(not child.values for child in groups["PSC"].children)
    if number == 47:
        assert groups["PSR"] == Container("PSR")
    assert store.profile() == before


def test_serve_wires_all_package_handlers_without_starting_listener(bundle, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "services", lambda _: bundle)
    monkeypatch.setattr(cli, "client_fingerprint", lambda _: bundle.masters.expected_hash)
    monkeypatch.setattr("aigis_ps.app.create_app", lambda *args, **kwargs: captured.update(kwargs) or object())
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    directory = tmp_path / "not-created"
    assert cli.main(["serve", "--data-dir", str(directory)]) == 0
    for number in (46, 47, 50):
        assert captured["handlers"][number].__self__ is bundle.packages
    assert captured["state_initializer"].__self__ is bundle
    assert not directory.exists()
