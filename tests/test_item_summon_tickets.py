from copy import copy, deepcopy
from dataclasses import replace
import json
import logging
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.errors import ProtocolError
import aigis_ps.item_summon_tickets as module
from aigis_ps.item_summon_tickets import ItemSummonTicketService
from aigis_ps.possessions import STORED_ITEM_NAME_BYTES
import aigis_ps.request_ledger as ledger
from aigis_ps.storage import Store
from aigis_ps.summon_tickets import SummonTicketRules, SummonTicketService
from test_quest_service import bundle, request


ITEM = "item_smn_ticket_19138"


@pytest.fixture
def case(bundle, tmp_path):
    assert bundle.item_summon_tickets is not None, bundle.item_summon_error
    bootstrap, players, presents = copy(bundle.bootstrap), copy(bundle.player_state), copy(bundle.presents)
    presents.bootstrap, presents.players = bootstrap, players
    calls = []
    def rng(limit):
        calls.append(limit)
        return 0
    service = ItemSummonTicketService(bootstrap, players, presents, bundle.summon_pools,
                                     bundle.item_summon_tickets.catalog, randbelow=rng)
    presents.item_tickets = service
    presents.tickets = SummonTicketService(bootstrap, players, presents, bundle.summon_pools, randbelow=rng)
    state = players.initialize(bootstrap.initial_state())
    state["sentinel"] = {"keep": [1]}
    state, mail = presents.enqueue(state, [
        {"kind": "item", "item_id": ITEM, "amount": 2, "description": "ten draws"},
        {"kind": "summon_ticket", "pool_id": 350, "count": 1, "description": "one draw"},
        {"kind": "free_crystal", "amount": 1, "description": "normal reward"}])
    return SimpleNamespace(bootstrap=bootstrap, players=players, presents=presents, service=service,
                           calls=calls, mail=mail, store=Store(tmp_path / "item-tickets.sqlite3", state))


def message(case, ids=(1,), *, number=1, tick=1000):
    return request(case.store, number, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", tuple(ids)),)),)),), tick=tick)


def fail(case, envelope=None, ids=(1,), code=None, *, adapter=False):
    envelope = message(case, ids) if envelope is None else envelope
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        if adapter:
            case.presents.receive(envelope, case.store)
        else:
            case.service.receive(envelope, case.store, ids)
    assert case.store.profile() == before
    if code is not None:
        assert caught.value.code == code
    return caught.value


def change(case, callback, *, revision=True):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    callback(state)
    if revision:
        case.store.update_state(state, profile.revision)
    else:
        with case.store.connection() as connection:
            connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def test_enabled_item_is_ten_draws_and_enqueue_splits_each_ticket(case):
    assert case.service.status()["enabled_pools"] == list(module.DEFAULT_ITEM_TICKET_POOLS)
    assert case.service.validate_item(ITEM).count == 10
    assert [(row["Type"], row["Content"]) for row in case.mail] == [(22, 1), (22, 1), (16, 1), (0, 1)]
    assert case.mail[0]["ItemID"] == ITEM and case.mail[1]["ItemID"] == ITEM


def test_success_has_ten_contiguous_native_units_and_preserves_old_state(case):
    before = case.store.profile()
    result = case.service.receive(message(case), case.store, (1,))
    after = case.store.profile()
    children = {node.name: node for node in loads(result).children}
    assert tuple(children) == ("SYSTEM", "PBP", "PPU")
    fields = {column.name: column for column in children["PPU"].children}
    assert len(fields) == 21 and all(len(column.values) == 10 for column in fields.values())
    assert fields["A7"].values == tuple(range(1, 11))
    assert fields["AB"].values == tuple(range(2, 12))
    assert len(case.calls) == 10
    assert after.revision == before.revision + 1
    assert after.state["player_state"]["units"][:-10] == before.state["player_state"]["units"]
    assert after.state["player_state"]["battle"]["UX"] == 10
    assert after.state["player_state"]["last_unique_id"] == 11
    assert [row["PresentID"] for row in case.presents.rows(after.state)] == [2, 3, 4]
    assert after.state["tables"]["possessions"] == before.state["tables"]["possessions"]
    assert after.state["tables"]["game_param"] == before.state["tables"]["game_param"]
    assert after.state["sentinel"] == before.state["sentinel"]
    receipt = after.state["item_summon_history"]["receipts"][0]
    assert receipt["units"] == after.state["player_state"]["units"][-10:]
    assert len(set(row["CardID"] for row in receipt["units"])) == 1
    assert len(receipt["candidate_indices"]) == 10


def test_repeated_ticket_adds_ten_new_instances_without_duplicate_compensation(case):
    before = case.store.profile()
    case.service.receive(message(case), case.store, (1,))
    first = deepcopy(case.store.profile().state["player_state"]["units"])
    case.service.receive(message(case, (2,), tick=1001), case.store, (2,))
    state = case.store.profile().state
    assert state["player_state"]["units"][:-10] == first
    assert len({row["UniqueID"] for row in state["player_state"]["units"]}) == 21
    assert len({row["CardID"] for row in state["player_state"]["units"][-20:]}) == 1
    assert state["tables"]["possessions"] == before.state["tables"]["possessions"]
    assert len(case.calls) == 20


def test_exact_retry_after_reopen_is_identical_without_rng(case):
    envelope = message(case)
    result = case.presents.receive(envelope, case.store)
    reopened = Store(case.store.path)
    service = ItemSummonTicketService(case.bootstrap, case.players, case.presents, case.service.pools,
        case.service.catalog, randbelow=lambda _: pytest.fail("replay redrew result"))
    before = reopened.profile()
    assert service.matches_request(before, envelope, (1,))
    assert service.receive(envelope, reopened, (1,)) == result
    assert reopened.profile() == before


def test_alias_registration_two_replays_one(case):
    one, two = message(case), message(case, number=2)
    assert one.profile == two.profile and one.xml == two.xml
    result = case.service.receive(one, case.store, (1,))
    assert case.service.receive(two, case.store, (1,)) == result
    assert len(case.calls) == 10


def test_item_router_does_not_take_type16_or_ordinary_rewards(case):
    assert case.service.matches_request(case.store.profile(), message(case), (1,))
    assert not case.service.matches_request(case.store.profile(), message(case, (3,)), (3,))
    assert not case.service.matches_request(case.store.profile(), message(case, (4,)), (4,))
    case.presents.receive(message(case, (3,)), case.store)
    state = case.store.profile().state
    assert len(case.calls) == 1 and len(state["player_state"]["units"]) == 2
    assert "item_summon_history" not in state and "summon_ticket_history" in state


@pytest.mark.parametrize("ids,number,code", [((1,), 117, "item_summon_batch_unsupported"),
    ((1, 2), 1, "item_summon_single_required"), ((1, 3), 1, "item_summon_single_required"),
    ((1, 4), 1, "item_summon_single_required"), ((3,), 1, "item_summon_mail_unsupported"),
    ((999,), 1, "present_not_found")])
def test_wrong_scope_never_draws(case, ids, number, code):
    fail(case, message(case, ids, number=number), ids, code)
    assert not case.calls


@pytest.mark.parametrize("field", ["UI", "UK", "MK"])
def test_authentication_before_random(case, field):
    envelope = message(case)
    envelope.document.find(field + "/V").text = "bad"
    assert fail(case, envelope).status == 403
    assert not case.calls


@pytest.mark.parametrize("fault", ["extra", "wrong_type", "duplicate", "nested", "mismatch"])
def test_request_fields_are_strict(case, fault):
    envelope = message(case)
    ids = (1,)
    if fault == "extra":
        ET.SubElement(envelope.document, "Pool")
    elif fault == "wrong_type":
        envelope.document.find("RT/PT2/PresentID").set("T", "S")
    elif fault == "duplicate":
        envelope.document.find("RT/PT2").append(deepcopy(envelope.document.find("RT/PT2/PresentID")))
    elif fault == "nested":
        ET.SubElement(envelope.document.find("RT/PT2/PresentID/V"), "Pool")
    else:
        ids = (2,)
    fail(case, envelope, ids, "invalid_request_fields")
    assert not case.calls


@pytest.mark.parametrize("slots", [0, 1, 9])
def test_all_ten_slots_required_before_any_random_draw(case, slots):
    case.players.capacity_provider = lambda _: slots
    fail(case, code="unit_roster_capacity")
    assert not case.calls


def test_exactly_ten_slots_is_enough(case):
    case.players.capacity_provider = lambda _: 10
    case.service.receive(message(case), case.store, (1,))
    assert len(case.calls) == 10


def test_active_battle_reservations_remain_available(case):
    change(case, lambda state: state["player_state"]["battle"].update(UX=0x7FFFFFFF - 10))
    change(case, lambda state: state.update(quest_state={"active": {"unit_reservations": [
        {"card_id": 17, "level": 1, "count": 1}]}}))
    fail(case, code="unit_roster_capacity")
    assert not case.calls


def test_cas_retry_freezes_all_ten_draws_and_preserves_other_write(case, monkeypatch):
    actual = case.store.update_state
    attempts = []
    def save(state, revision):
        attempts.append(1)
        if len(attempts) == 1:
            profile = case.store.profile()
            concurrent = deepcopy(profile.state)
            concurrent["sentinel"]["keep"].append(2)
            actual(concurrent, profile.revision)
        return actual(state, revision)
    monkeypatch.setattr(case.store, "update_state", save)
    case.service.receive(message(case), case.store, (1,))
    assert len(attempts) == 2 and len(case.calls) == 10
    assert case.store.profile().state["sentinel"]["keep"] == [1, 2]


def test_all_failed_cas_attempts_never_redraw_or_consume(case, monkeypatch):
    attempts = []
    def save(*_):
        attempts.append(1)
        raise ValueError("conflict")
    monkeypatch.setattr(case.store, "update_state", save)
    fail(case, code="profile_changed")
    assert len(attempts) == 3 and len(case.calls) == 10


def test_changed_mail_cannot_receive_frozen_result(case, monkeypatch):
    actual = case.store.update_state
    attempts = []
    def save(state, revision):
        attempts.append(1)
        if len(attempts) == 1:
            profile = case.store.profile()
            other = deepcopy(profile.state)
            other["tables"]["presents"][0]["Description"] = "changed"
            actual(other, profile.revision)
        return actual(state, revision)
    monkeypatch.setattr(case.store, "update_state", save)
    with pytest.raises(ProtocolError) as caught:
        case.service.receive(message(case), case.store, (1,))
    assert caught.value.code == "item_summon_changed"
    assert len(case.calls) == 10
    assert len(case.store.profile().state["tables"]["presents"]) == 4
    assert len(case.store.profile().state["player_state"]["units"]) == 1


@pytest.mark.parametrize("number", [1, 41, 42])
def test_output_capacity_failure_is_atomic(case, number):
    case.service._profiles[number] = replace(case.service._profiles[number], response_limit=1)
    fail(case, code="response_too_large")
    assert len(case.calls) == 10


def test_old_receipt_is_rejected_after_new_revision(case):
    envelope = message(case)
    case.service.receive(envelope, case.store, (1,))
    change(case, lambda state: state["sentinel"]["keep"].append(2))
    fail(case, envelope, code="item_summon_retry_stale")
    assert len(case.calls) == 10


def test_status_reports_the_request_ledger_instead_of_a_client_clock(case):
    status = case.service.status()
    assert status["history_limit"] == ledger.HISTORY_LIMIT
    assert status["processed_request_limit"] == ledger.PROCESSED_LIMIT
    assert status["request_order"] == ledger.REQUEST_ORDER and "same_second_limit" not in status


def test_client_clock_rollback_claims_new_tickets_and_keeps_exact_replays_stale(case):
    first = message(case, tick=0x7FFFFFFF)
    case.service.receive(first, case.store, (1,))
    case.service.receive(message(case, (2,), tick=-0x80000000), case.store, (2,))
    history = case.store.profile().state["item_summon_history"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION and len(history["processed_keys"]) == 2
    assert all("client_time" not in entry for entry in history["receipts"])
    fail(case, first, code="item_summon_retry_stale")
    assert len(case.calls) == 20


def test_same_second_claims_are_distinct_requests(case):
    case.service.receive(message(case), case.store, (1,))
    case.service.receive(message(case, (2,)), case.store, (2,))
    assert len(case.calls) == 20
    assert len(case.store.profile().state["item_summon_history"]["processed_keys"]) == 2


def test_evicted_receipt_repeat_is_refused_through_the_service_and_the_mail_adapter(case, monkeypatch, caplog):
    monkeypatch.setattr(ledger, "HISTORY_LIMIT", 1)
    old = message(case)
    case.service.receive(old, case.store, (1,))
    case.service.receive(message(case, (2,), tick=1001), case.store, (2,))
    history = case.store.profile().state["item_summon_history"]
    assert len(history["receipts"]) == 1 and len(history["processed_keys"]) == 2
    assert case.service.matches_request(case.store.profile(), old, (1,))
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        fail(case, old, code="item_summon_ticket_request_stale")
        fail(case, old, code="item_summon_ticket_request_stale", adapter=True)
    assert any("request 1/2" in line and "evicted" in line for line in caplog.messages)
    assert len(case.calls) == 20


def test_watermark_history_upgrades_and_keeps_replaying_its_receipt(case):
    envelope = message(case, tick=3000)
    payload = case.service.receive(envelope, case.store, (1,))

    def legacy(state):
        history = state["item_summon_history"]
        state["item_summon_history"] = {"schema_version": ledger.LEGACY_SCHEMA_VERSION, "last_client_time": 3000,
            "seen_keys": list(history["processed_keys"]),
            "receipts": [{**entry, "client_time": 3000} for entry in history["receipts"]]}
    change(case, legacy, revision=False)
    before = case.store.profile()
    assert case.service.matches_request(before, envelope, (1,))
    assert case.service.receive(envelope, case.store, (1,)) == payload
    assert case.store.profile() == before
    case.service.receive(message(case, (2,), tick=1000), case.store, (2,))
    history = case.store.profile().state["item_summon_history"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION
    assert history["processed_keys"] == [entry["request"] for entry in history["receipts"]]
    assert len(case.calls) == 20


@pytest.mark.parametrize("size,accepted", [(STORED_ITEM_NAME_BYTES, True), (STORED_ITEM_NAME_BYTES + 1, False)])
def test_receipt_item_identity_keeps_the_stored_name_bound(case, size, accepted):
    case.service.receive(message(case), case.store, (1,))
    change(case, lambda state: state["item_summon_history"]["receipts"][0].update(item_id="x" * size), revision=False)
    if accepted:
        case.service.receive(message(case, (2,), tick=1001), case.store, (2,))
        assert len(case.store.profile().state["item_summon_history"]["receipts"]) == 2
    else:
        fail(case, message(case, (2,), tick=1001), (2,), "item_summon_history_invalid")


@pytest.mark.parametrize("field,value", [("state", "0" * 64), ("profile", "0" * 64), ("response", "bad"),
    ("mail_id", 2), ("client_time", 0), ("item_id", "item_smn_ticket_18003"),
    ("candidate_indices", [99999] * 10), ("candidate_indices", [True] * 10), ("pool_id", 350)])
def test_corrupt_receipt_is_not_accepted(case, field, value):
    envelope = message(case)
    case.service.receive(envelope, case.store, (1,))
    change(case, lambda state: state["item_summon_history"]["receipts"][0].update({field: value}), revision=False)
    error = fail(case, envelope)
    assert error.status in (501, 503)
    assert len(case.calls) == 10


@pytest.mark.parametrize("fault", ["love", "identity", "card", "bool", "count", "order"])
def test_corrupt_receipt_units_are_rejected(case, fault):
    envelope = message(case)
    case.service.receive(envelope, case.store, (1,))
    def corrupt(state):
        units = state["item_summon_history"]["receipts"][0]["units"]
        if fault == "love":
            units[0]["Love"] = 1
        elif fault == "identity":
            units[0]["UniqueID"] = units[1]["UniqueID"]
        elif fault == "card":
            units[0]["CardID"] = 17
        elif fault == "bool":
            units[0]["CanFame"] = 0
        elif fault == "count":
            units.pop()
        else:
            units.reverse()
    change(case, corrupt, revision=False)
    fail(case, envelope, code="item_summon_history_invalid")


@pytest.mark.parametrize("value", [True, -1, None, 1.5])
def test_invalid_random_result_is_atomic(case, value):
    case.service.randbelow = lambda _: value
    fail(case, code="summon_random_invalid")


def test_invalid_later_random_result_does_not_keep_partial_draw(case):
    counter = []
    def rng(total):
        counter.append(total)
        return 0 if len(counter) < 6 else total
    case.service.randbelow = rng
    fail(case, code="summon_random_invalid")
    assert len(counter) == 6


@pytest.mark.parametrize("field,value", [("Love", 100), ("SkillLevel", 2), ("GeneralPurposeFlags", 5)])
def test_underlying_grant_cannot_change_unverified_unit_fields(case, monkeypatch, field, value):
    actual = case.players.credit_units
    def corrupt(state, grants):
        updated, units = actual(state, grants)
        units[0][field] = value
        updated["player_state"]["units"][-10][field] = value
        return updated, units
    monkeypatch.setattr(case.players, "credit_units", corrupt)
    fail(case, code="summon_ticket_result_invalid")


@pytest.mark.parametrize("item", ["item_smn_ticket_18003", "item_smn_ticket_18002"])
def test_incomplete_disabled_legacy_tickets_remain_readable_and_unconsumed(case, item):
    change(case, lambda state: state["tables"]["presents"][0].update(ItemID=item))
    state = case.store.profile().state
    listing = case.presents.response(state)
    assert item in next(column.values for column in listing.children if column.name == "A6")
    fail(case, code="summon_ticket_pool_disabled", adapter=True)
    assert not case.calls


def test_enabled_incomplete_pool_is_rejected_at_construction(case):
    with pytest.raises(ProtocolError) as caught:
        ItemSummonTicketService(case.bootstrap, case.players, case.presents, case.service.pools,
                                case.service.catalog, rules=SummonTicketRules((32,)))
    assert caught.value.code == "summon_pool_incomplete"


def test_unknown_action_ticket_stays_readable_but_cannot_be_used(case):
    unsupported = next(row.item_id for row in case.service.catalog.definitions.values() if row.unsupported_reason)
    change(case, lambda state: state["tables"]["presents"][0].update(ItemID=unsupported))
    assert len(case.presents.rows(case.store.profile().state)) == 4
    fail(case, code="item_summon_unsupported", adapter=True)
    assert not case.calls


def test_readiness_follows_the_catalog_check_without_building_its_status(case, monkeypatch):
    with monkeypatch.context() as patched:
        patched.setattr(case.service.catalog, "status", lambda: pytest.fail("Readiness built the full catalog status"))
        case.service._ready()
        assert case.service.validate_item(ITEM).count == 10
    monkeypatch.setattr(case.service.catalog, "_pool_version", "0" * 64)
    assert case.service.catalog.status()["ready"] is False
    status = case.service.status()
    assert (status["ready"], status["error"]) == (False, "item_summon_dependencies_changed")
    fail(case, code="item_summon_dependencies_changed")
    assert not case.calls


def test_changed_rules_invalidate_original_and_cached_requests(case):
    envelope = message(case)
    case.service.receive(envelope, case.store, (1,))
    case.service.rules = SummonTicketRules(())
    fail(case, envelope, code="item_summon_dependencies_changed")
    assert len(case.calls) == 10


def test_initializer_change_prevents_random(case, monkeypatch):
    initial = case.players._initialization()
    monkeypatch.setattr(case.players, "_initialization", lambda: SimpleNamespace(
        content_version="0" * 64, values=initial.values))
    fail(case, code="summon_ticket_dependencies_changed")
    assert not case.calls
