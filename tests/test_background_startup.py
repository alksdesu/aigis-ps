from copy import deepcopy

import pytest

from aigis_ps.alxml import Column, Container
from aigis_ps.background_state import background_response
from aigis_ps.bootstrap import BootstrapService
from aigis_ps.cli import service_readiness
from aigis_ps.errors import ProtocolError
from test_bootstrap import asset_root, service, store
from test_quest_service import bundle, request, nodes


def test_background_provider_preserves_complete_historical_rows(service, store):
    before = store.profile()
    before.state["backgrounds_schema"] = 1
    before.state["tables"]["backgrounds"] = [
        {"CardID": 90, "ConfigID": "old_resource_name", "SaveTime": "2025/12/31 23:59:59"}]
    saved = store.update_state(before.state, before.revision)
    service.background_provider = background_response
    response = service.configuration_database(request(store, 137, ()), store)
    assert nodes(response)["PRM"] == Container("PRM", (
        Column("CardID", "I", (90,)), Column("ConfigID", "S", ("old_resource_name",)),
        Column("SaveTime", "S", ("2025/12/31 23:59:59",)),
    ))
    assert store.profile() == saved


@pytest.mark.parametrize("value", [None, Column("PRM", "S", ()), Container("Other"), Container("PRM", index=1)])
def test_background_provider_invalid_shape_does_not_change_save(service, store, value):
    before = store.profile()
    service.background_provider = lambda _: value
    with pytest.raises(ProtocolError, match="Background provider must return PRM"):
        service.configuration_database(request(store, 137, ()), store)
    assert store.profile() == before


def test_background_provider_requires_callable(asset_root):
    with pytest.raises(ValueError, match="Background provider must be callable"):
        BootstrapService(asset_root, background_provider=False)


def test_cli_background_catalog_and_initialization_are_consistent(bundle):
    report = service_readiness(bundle)
    assert report["backgrounds"]["ready"] is True
    assert report["backgrounds"]["implemented_operations"] == [135]
    assert report["backgrounds"]["catalog"]["selectable_count"] == 43
    state = bundle.initialize(bundle.bootstrap.initial_state())
    assert "backgrounds_schema" not in state
    assert state["tables"]["backgrounds"] == []
    before = deepcopy(state)
    assert bundle.initialize(state) == before
    assert state == before
    state["tables"]["backgrounds"].append(
        {"CardID": 0, "ConfigID": "system_fix", "SaveTime": "2026/11/01 12:00:00"})
    with pytest.raises(ProtocolError) as caught:
        bundle.initialize(state)
    assert caught.value.code == "background_state_invalid"

