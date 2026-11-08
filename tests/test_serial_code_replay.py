from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from aigis_ps import serial_code_service
from aigis_ps.present_items import PresentItemCatalog
from aigis_ps.serial_code_service import (
    HISTORY_NAMESPACE, ITEM_CATALOG, PRESENT_DEPENDENCIES, PRESENTS_ADDED, USED_UNIQUE_CODE)
from test_bootstrap import asset_root
from test_emblem_catalog import components as emblem_components
from test_player_state import assets, growth_setup
from test_presents import presents, state
from test_serial_code_service import code, redeem, redemption, service_for, status_of, store


FORGED = hashlib.sha256(b"forged").hexdigest()


def fingerprinted(name):
    version = hashlib.sha256(name.encode()).hexdigest()
    return SimpleNamespace(content_version=version, replayable_content_versions=lambda: frozenset({version}))


def attach(presents, monkeypatch, names):
    for name in names:
        monkeypatch.setattr(presents, name, fingerprinted(name))


def receipt_content(store):
    return store.profile().state[HISTORY_NAMESPACE]["receipts"][-1]["content"]


def replace_without_revision(store, state):
    with store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


@pytest.fixture
def item_catalogs():
    case = emblem_components()
    payload = case.payloads["ItemConfigTable.atb"]
    return SimpleNamespace(plain=PresentItemCatalog(case.possessions, payload),
                           mailed=PresentItemCatalog(case.possessions, payload, emblems=case.catalog))


def test_the_light_mailbox_starts_without_any_optional_dependency(presents):
    assert all(getattr(presents, name) is None for name in PRESENT_DEPENDENCIES)


@pytest.mark.parametrize("names", [*((name,) for name in PRESENT_DEPENDENCIES), PRESENT_DEPENDENCIES])
def test_receipt_replays_after_presents_dependencies_attach(presents, store, monkeypatch, names):
    request = redemption(store)
    first = redeem(service_for(presents, code()), store, request=request)
    committed = store.profile()
    attach(presents, monkeypatch, names)
    attached = service_for(presents, code())
    assert attached.content_version != receipt_content(store)
    assert redeem(attached, store, request=request) == first
    assert store.profile() == committed
    assert attached.status()["answers"]["presents_added"] == 1


def test_receipt_replays_after_more_dependencies_join_the_ones_it_had(presents, store, monkeypatch):
    attach(presents, monkeypatch, PRESENT_DEPENDENCIES[:2])
    request = redemption(store)
    first = redeem(service_for(presents, code()), store, request=request)
    committed = store.profile()
    attach(presents, monkeypatch, PRESENT_DEPENDENCIES[2:])
    assert redeem(service_for(presents, code()), store, request=request) == first
    assert store.profile() == committed


def test_unchanged_dependencies_replay_without_masking(presents, store, monkeypatch):
    attach(presents, monkeypatch, PRESENT_DEPENDENCIES)
    service = service_for(presents, code())
    request = redemption(store)
    first = redeem(service, store, request=request)
    monkeypatch.setattr(serial_code_service, "combinations",
                        lambda *args: pytest.fail("the committed dependencies are current"))
    assert redeem(service, store, request=request) == first


@pytest.mark.parametrize("name", PRESENT_DEPENDENCIES)
def test_receipt_of_a_removed_dependency_answers_the_current_status(presents, store, monkeypatch, name):
    attach(presents, monkeypatch, (name,))
    request = redemption(store)
    assert status_of(redeem(service_for(presents, code()), store, request=request)) == PRESENTS_ADDED
    committed = store.profile()
    monkeypatch.setattr(presents, name, None)
    assert status_of(redeem(service_for(presents, code()), store, request=request)) == USED_UNIQUE_CODE
    assert store.profile() == committed


def test_digest_outside_every_configuration_answers_the_current_status(presents, store, monkeypatch):
    request = redemption(store)
    redeem(service_for(presents, code()), store, request=request)
    forged = deepcopy(store.profile().state)
    forged[HISTORY_NAMESPACE]["receipts"][-1]["content"] = FORGED
    replace_without_revision(store, forged)
    before = store.profile()
    attach(presents, monkeypatch, PRESENT_DEPENDENCIES)
    assert status_of(redeem(service_for(presents, code()), store, request=request)) == USED_UNIQUE_CODE
    assert store.profile() == before


@pytest.mark.parametrize("later", [(), PRESENT_DEPENDENCIES[1:]], ids=["alone", "with-other-dependencies"])
def test_receipt_replays_after_emblem_mail_attaches(presents, store, monkeypatch, item_catalogs, later):
    monkeypatch.setattr(presents, ITEM_CATALOG, item_catalogs.plain)
    request = redemption(store)
    first = redeem(service_for(presents, code()), store, request=request)
    committed = store.profile()
    monkeypatch.setattr(presents, ITEM_CATALOG, item_catalogs.mailed)
    attach(presents, monkeypatch, later)
    rebuilt = service_for(presents, code())
    assert rebuilt.content_version != receipt_content(store)
    assert redeem(rebuilt, store, request=request) == first
    assert store.profile() == committed
    assert rebuilt.status()["answers"]["presents_added"] == 1


def test_receipt_with_emblem_mail_answers_the_current_status_once_it_detaches(presents, store, monkeypatch,
                                                                              item_catalogs):
    monkeypatch.setattr(presents, ITEM_CATALOG, item_catalogs.mailed)
    request = redemption(store)
    assert status_of(redeem(service_for(presents, code()), store, request=request)) == PRESENTS_ADDED
    committed = store.profile()
    monkeypatch.setattr(presents, ITEM_CATALOG, item_catalogs.plain)
    assert status_of(redeem(service_for(presents, code()), store, request=request)) == USED_UNIQUE_CODE
    assert store.profile() == committed


def test_forged_digest_answers_the_current_status_after_emblem_mail_attaches(presents, store, monkeypatch,
                                                                            item_catalogs):
    monkeypatch.setattr(presents, ITEM_CATALOG, item_catalogs.plain)
    request = redemption(store)
    redeem(service_for(presents, code()), store, request=request)
    forged = deepcopy(store.profile().state)
    forged[HISTORY_NAMESPACE]["receipts"][-1]["content"] = FORGED
    replace_without_revision(store, forged)
    before = store.profile()
    monkeypatch.setattr(presents, ITEM_CATALOG, item_catalogs.mailed)
    assert status_of(redeem(service_for(presents, code()), store, request=request)) == USED_UNIQUE_CODE
    assert store.profile() == before
