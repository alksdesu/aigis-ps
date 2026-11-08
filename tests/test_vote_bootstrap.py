from copy import deepcopy

import pytest

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.bootstrap import BootstrapService
from aigis_ps.errors import ProtocolError
from aigis_ps.storage import Store
from aigis_ps.vote_state import initialize_vote_state, record_vote, vote_history_response
from test_bootstrap import asset_root, service, store, request


def test_constructor_accepts_only_callable_vote_provider(asset_root):
    with pytest.raises(ValueError, match="Vote provider"):
        BootstrapService(asset_root, vote_provider=True)
    bootstrap = BootstrapService(asset_root, vote_provider=vote_history_response)
    assert bootstrap.status()["vote_provider_configured"] is True


def test_137_returns_persisted_complete_vote_history_and_reopens_unchanged(service, store):
    service.vote_provider = vote_history_response
    before = store.profile()
    updated = record_vote(before.state, item_id="item_vote_008001", card_id=2930,
                          group_id=8, count=17, definition="a" * 64)
    store.update_state(updated, before.revision)
    expected = store.profile()
    for source in (store, Store(store.path)):
        response = loads(service.configuration_database(request(137, source), source))
        assert next(child for child in response.children if child.name == "PVH") == vote_history_response(updated)
        assert source.profile() == expected


def test_missing_provider_preserves_empty_contract_and_rejects_nonempty(service, store):
    assert service.vote_provider is None
    assert service.status()["vote_provider_configured"] is False
    response = loads(service.configuration_database(request(137, store), store))
    assert next(child for child in response.children if child.name == "PVH") == Container("PVH")
    profile = store.profile()
    updated = record_vote(profile.state, item_id="item_vote_008001", card_id=1,
                          group_id=8, count=1, definition="a" * 64)
    store.update_state(updated, profile.revision)
    before = store.profile()
    with pytest.raises(ProtocolError) as caught:
        service.configuration_database(request(137, store), store)
    assert caught.value.code == "collection_rows_unavailable"
    assert store.profile() == before


@pytest.mark.parametrize("result", [None, "wrong", Column("PVH", "S", ()),
                                     Container("Other"), Container("PVH", index="1")])
def test_wrong_provider_container_is_rejected_without_save(service, store, result):
    service.vote_provider = lambda _: result
    before = store.profile()
    with pytest.raises(ProtocolError) as caught:
        service.configuration_database(request(137, store), store)
    assert caught.value.code == "vote_provider_invalid"
    assert store.profile() == before


def test_vote_provider_does_not_disable_unrelated_collection_checks(service, store):
    service.vote_provider = vote_history_response
    profile = store.profile()
    updated = deepcopy(profile.state)
    updated["tables"]["recommendations"] = [{"unknown": 1}]
    store.update_state(updated, profile.revision)
    before = store.profile()
    with pytest.raises(ProtocolError) as caught:
        service.configuration_database(request(137, store), store)
    assert caught.value.code == "collection_rows_unavailable"
    assert store.profile() == before


def test_provider_rejects_unknown_nonempty_legacy_rows(service, store):
    service.vote_provider = vote_history_response
    profile = store.profile()
    updated = deepcopy(profile.state)
    updated["tables"]["vote_history"] = [{"unknown": 1}]
    store.update_state(updated, profile.revision)
    before = store.profile()
    with pytest.raises(ProtocolError) as caught:
        service.configuration_database(request(137, store), store)
    assert caught.value.code == "vote_state_invalid"
    assert store.profile() == before


def test_provider_reads_legacy_rows_without_implicitly_saving_baseline(service, store):
    service.vote_provider = vote_history_response
    profile = store.profile()
    updated = record_vote(profile.state, item_id="retired_vote", card_id=1,
                          group_id=91, count=30, definition="a" * 64)
    updated.pop("vote_state")
    store.update_state(updated, profile.revision)
    before = store.profile()
    response = loads(service.configuration_database(request(137, store), store))
    assert next(child for child in response.children if child.name == "PVH") == vote_history_response(updated)
    baseline = initialize_vote_state(updated)
    entry = next(iter(baseline["vote_state"]["entries"].values()))
    assert entry == {"definition": None, "baseline_count": 30, "count": 30}
    assert store.profile() == before
