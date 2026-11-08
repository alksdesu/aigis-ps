from copy import copy, deepcopy
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.item_summon_tickets import ItemSummonTicketService
import aigis_ps.presents as module
from aigis_ps.summon_tickets import SummonTicketRules
from test_quest_service import bundle


ITEM = "item_smn_ticket_19138"


def ticket(identifier, item=ITEM):
    return {"PresentID": identifier, "Type": 22, "Content": 1, "Description": "ticket", "ItemID": item}


def state(rows):
    return {"bootstrap_schema": 1, "presents_schema": 1, "last_present_id": len(rows),
            "tables": {"presents": rows}, "sentinel": {"keep": [1]}}


@pytest.fixture
def presents(bundle):
    result = copy(bundle.presents)
    result.item_tickets = copy(bundle.item_summon_tickets)
    result.item_tickets.presents = result
    return result


@pytest.mark.parametrize("count", [1, 100, 500])
def test_rows_perform_two_initializer_checks_regardless_of_ticket_count(presents, monkeypatch, count):
    original = presents.players._initialization
    calls = []
    def counted():
        calls.append(1)
        return original()
    monkeypatch.setattr(presents.players, "_initialization", counted)
    data = state([ticket(index + 1) for index in range(count)])
    before = deepcopy(data)
    result = presents.rows(data)
    assert result == data["tables"]["presents"]
    assert len(calls) == 2
    assert data == before and result is not data["tables"]["presents"]


def test_repeated_rows_calls_revalidate_each_request_without_cross_call_cache(presents, monkeypatch):
    original = presents.players._initialization
    calls = []
    def counted():
        calls.append(1)
        return original()
    monkeypatch.setattr(presents.players, "_initialization", counted)
    data = state([ticket(index + 1) for index in range(100)])
    presents.rows(data)
    presents.response(data)
    assert len(calls) == 4


def test_each_non_ticket_row_keeps_existing_validation_without_initializer_dependency(presents, monkeypatch):
    def unexpected():
        pytest.fail("Ordinary mailbox acquired a unit initialization dependency")
    monkeypatch.setattr(presents.players, "_initialization", unexpected)
    data = state([
        {"PresentID": 1, "Type": 1, "Content": 2, "Description": "unit"},
        {"PresentID": 2, "Type": 0, "Content": 1, "Description": "crystal"},
        ticket(3, "gift_0002_ruby"), ticket(4, "item_cook_logbo_00004"),
    ])
    assert len(presents.rows(data)) == 4
    assert presents.rows(state([])) == []


def test_disabled_and_incomplete_ticket_mail_stays_readable_without_validating_pool(presents, monkeypatch):
    original = presents.item_tickets
    service = ItemSummonTicketService(presents.bootstrap, presents.players, presents,
        original.pools, original.catalog, rules=SummonTicketRules(()))
    presents.item_tickets = service
    calls = []
    initialization = presents.players._initialization
    def counted():
        calls.append(1)
        return initialization()
    monkeypatch.setattr(presents.players, "_initialization", counted)
    monkeypatch.setattr(service, "validate_item", lambda *_: pytest.fail("Stored mail must not require an enabled pool"))
    data = state([ticket(1), ticket(2, "item_smn_ticket_10001"), ticket(3, "gift_0002_ruby"),
        {"PresentID": 4, "Type": 16, "Content": 1, "Description": "old ticket", "SummonID": 350}])
    assert len(presents.rows(data)) == 4 and len(calls) == 2
    assert original.catalog.definition("item_smn_ticket_10001").pool_id not in service.rules.enabled_pools


@pytest.mark.parametrize("mutation", ["service", "catalog", "definitions", "pool_version"])
def test_mid_scan_dependency_or_identity_replacement_is_rejected(presents, monkeypatch, mutation):
    description = module._description
    calls = []
    def mutate(value):
        calls.append(1)
        if len(calls) == 50:
            if mutation == "service":
                presents.item_tickets = copy(presents.item_tickets)
            elif mutation == "catalog":
                presents.item_tickets.catalog = copy(presents.item_tickets.catalog)
            elif mutation == "definitions":
                catalog = presents.item_tickets.catalog
                monkeypatch.setattr(catalog, "_definitions", MappingProxyType(dict(catalog.definitions)))
            else:
                monkeypatch.setattr(presents.item_tickets.pools, "_content_version", "0" * 64)
        return description(value)
    monkeypatch.setattr(module, "_description", mutate)
    data = state([ticket(index + 1) for index in range(100)])
    original = deepcopy(data)
    with pytest.raises(ProtocolError) as error:
        presents.rows(data)
    assert error.value.status == 503
    assert error.value.code == "present_state_invalid"
    assert error.value.__cause__.code in {"item_summon_dependencies_changed", "summon_ticket_dependencies_changed"}
    assert data == original


def test_initializer_resource_change_at_final_check_is_rejected(presents, monkeypatch):
    original = presents.players._initialization
    calls = []
    def change():
        calls.append(1)
        return original() if len(calls) == 1 else SimpleNamespace(content_version="0" * 64)
    monkeypatch.setattr(presents.players, "_initialization", change)
    with pytest.raises(ProtocolError) as error:
        presents.rows(state([ticket(index + 1) for index in range(100)]))
    assert error.value.code == "present_state_invalid"
    assert error.value.__cause__.code == "summon_ticket_dependencies_changed"
    assert len(calls) == 2


@pytest.mark.parametrize("field,value", [("Content", 0), ("Content", True), ("PresentID", 1),
    ("PresentID", 1000), ("Type", 999), ("Description", "bad\x01"),
    ("NotificationCategory", -1), ("NotificationCategory", True), ("Unexpected", 1)])
def test_last_row_still_undergoes_full_field_validation(presents, field, value):
    rows = [ticket(index + 1) for index in range(100)]
    rows[-1][field] = value
    data = state(rows)
    before = deepcopy(data)
    with pytest.raises(ProtocolError) as error:
        presents.rows(data)
    assert error.value.code == "present_state_invalid"
    assert data == before


def test_unknown_last_item_is_not_accepted_as_a_ticket(presents):
    rows = [ticket(index + 1) for index in range(100)]
    rows[-1]["ItemID"] = "item_smn_ticket_not_a_known_item"
    with pytest.raises(ProtocolError):
        presents.rows(state(rows))


def test_missing_last_ticket_item_id_is_rejected(presents):
    rows = [ticket(index + 1) for index in range(100)]
    del rows[-1]["ItemID"]
    with pytest.raises(ProtocolError) as error:
        presents.rows(state(rows))
    assert error.value.code == "present_state_invalid"


def test_notification_and_ticket_metadata_are_preserved_in_full_response(presents):
    rows = [ticket(index + 1) for index in range(100)]
    rows[-1]["NotificationCategory"] = 7
    values = {column.name: column.values for column in presents.response(state(rows)).children}
    assert values["A6"] == (ITEM,) * 100
    assert values["A9"] == (0,) * 99 + (7,)


def test_external_ticket_helper_still_validates_every_direct_call(presents, monkeypatch):
    original = presents.item_tickets._ready
    calls = []
    def counted():
        calls.append(1)
        return original()
    monkeypatch.setattr(presents.item_tickets, "_ready", counted)
    presents._item_ticket_definition(ticket(1))
    presents._item_ticket_definition(ticket(2))
    assert len(calls) == 2
