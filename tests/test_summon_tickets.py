from copy import copy, deepcopy
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from xml.etree import ElementTree as ET
import json
import logging

import pytest

import aigis_ps.request_ledger as ledger
from aigis_ps.alxml import Column, Container, loads
from aigis_ps.errors import ProtocolError
from aigis_ps.storage import Store
from aigis_ps.summon_tickets import SummonTicketRules, SummonTicketService
from test_quest_service import bundle, request


@pytest.fixture(scope="module")
def ticket_base(bundle):
    assert bundle.summon_pools is not None
    return SummonTicketService(bundle.bootstrap, bundle.player_state, bundle.presents, bundle.summon_pools)


@pytest.fixture
def case(bundle, ticket_base, tmp_path):
    bootstrap, players, presents = copy(bundle.bootstrap), copy(bundle.player_state), copy(bundle.presents)
    service = copy(ticket_base)
    service.bootstrap, service.players, service.presents = bootstrap, players, presents
    service.grants = copy(service.grants)
    service.grants.players = players
    service.grants.units = copy(service.grants.units)
    service.grants.units.players = players
    calls = []
    def random(total):
        calls.append(total)
        return 0
    service.randbelow = random
    presents.bootstrap, presents.players, presents.tickets = bootstrap, players, service
    initial = players.initialize(bootstrap.initial_state())
    initial["sentinel"] = {"preserve": [1]}
    initial, mail = presents.enqueue(initial, [
        {"kind": "summon_ticket", "pool_id": 350, "count": 2, "description": "ticket"},
        {"kind": "free_crystal", "amount": 1, "description": "ordinary"}])
    return SimpleNamespace(service=service, presents=presents, players=players, calls=calls,
        store=Store(tmp_path / "tickets.sqlite3", initial), mail=mail, pool=service.validate_pool(350))


def message(case, selected=None, *, number=1, tick=1000):
    selected = (case.mail[0]["PresentID"],) if selected is None else selected
    return request(case.store, number, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", tuple(selected)),)),)),), tick=tick)


def fail(case, envelope=None, selected=None, code=None):
    envelope = message(case) if envelope is None else envelope
    selected = (case.mail[0]["PresentID"],) if selected is None else selected
    before = case.store.profile()
    with pytest.raises(ProtocolError) as error:
        case.service.receive(envelope, case.store, selected)
    assert case.store.profile() == before
    if code:
        assert error.value.code == code
    return error.value


def change(case, callback, *, revision=True):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    callback(state)
    if revision:
        case.store.update_state(state, profile.revision)
    else:
        with case.store.connection() as connection:
            connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def test_real_pool_validates_every_candidate_before_claim(ticket_base):
    pool = ticket_base.validate_pool(350)
    assert len(pool.candidates) == 508
    assert len(ticket_base._initial_values[350]) == 508
    assert ticket_base.status()["ready"]
    assert ticket_base.status()["enabled_pools"] == [350]


def test_single_ticket_is_atomically_replaced_by_one_new_unit(case):
    before = case.store.profile()
    envelope = message(case)
    result = case.service.receive(envelope, case.store, (case.mail[0]["PresentID"],))
    after = case.store.profile()
    candidate = next(row for row in case.pool.candidates if row.weight > 0)
    new_unit = after.state["player_state"]["units"][-1]
    assert new_unit["CardID"] == candidate.card_id
    assert new_unit["GetIndex"] == before.state["player_state"]["battle"]["UX"] + 1
    assert after.state["player_state"]["units"][:-1] == before.state["player_state"]["units"]
    assert after.state["tables"]["presents"] == before.state["tables"]["presents"][1:]
    assert after.state["last_present_id"] == before.state["last_present_id"]
    assert after.state["tables"]["crystals"] == before.state["tables"]["crystals"]
    assert after.state["tables"]["possessions"] == before.state["tables"]["possessions"]
    assert after.state["sentinel"] == before.state["sentinel"]
    assert after.revision == before.revision + 1 and len(case.calls) == 1
    groups = loads(result).children
    assert tuple(row.name for row in groups) == ("SYSTEM", "PBP", "PPU")
    assert groups[1] == Container("PBP", (Column("A1", "S", ("UX",)), Column("A2", "I", (new_unit["GetIndex"],))))
    assert len(groups[2].children) == 21
    assert all(len(column.values) == 1 for column in groups[2].children)


def test_exact_retry_and_reopened_service_reuse_unit_and_do_not_draw(case):
    envelope = message(case)
    first = case.service.receive(envelope, case.store, (1,))
    before = case.store.profile()
    service = SummonTicketService(case.service.bootstrap, case.players, case.presents, case.service.pools,
        randbelow=lambda _: pytest.fail("Retry drew again"))
    reopened = Store(case.store.path)
    assert service.matches_request(reopened.profile(), envelope, (1,)) is True
    assert service.receive(envelope, reopened, (1,)) == first
    assert reopened.profile() == before
    assert len(case.calls) == 1


def test_aliases_one_and_two_share_profile_and_replay(case):
    one, two = message(case, number=1), message(case, number=2)
    assert one.profile == two.profile and one.xml == two.xml
    result = case.service.receive(one, case.store, (1,))
    assert case.service.receive(two, case.store, (1,)) == result
    assert len(case.calls) == 1


def test_present_adapter_dispatches_ticket_and_preserves_normal_mail(case):
    envelope = message(case)
    assert case.service.matches_request(case.store.profile(), envelope, (1,))
    assert not case.service.matches_request(case.store.profile(), message(case, (3,)), (3,))
    result = case.presents.receive(envelope, case.store)
    assert tuple(group.name for group in loads(result).children) == ("SYSTEM", "PBP", "PPU")
    assert case.presents.receive(envelope, case.store) == result


def test_repeated_card_is_another_instance_without_currency_compensation(case):
    before = case.store.profile()
    case.service.receive(message(case, (1,), tick=1000), case.store, (1,))
    case.service.receive(message(case, (2,), tick=1001), case.store, (2,))
    after = case.store.profile()
    one, two = after.state["player_state"]["units"][-2:]
    assert one["CardID"] == two["CardID"]
    assert one["UniqueID"] != two["UniqueID"] and one["GetIndex"] != two["GetIndex"]
    assert after.state["tables"]["possessions"] == before.state["tables"]["possessions"]
    assert after.state["tables"]["game_param"] == before.state["tables"]["game_param"]


@pytest.mark.parametrize("selected,number,code", [((1,), 117, "summon_ticket_batch_unsupported"),
    ((1, 2), 1, "summon_ticket_single_required"), ((1, 3), 1, "summon_ticket_single_required"),
    ((3,), 1, "summon_ticket_invalid"), ((999,), 1, "present_not_found")])
def test_unsupported_claim_scope_is_rejected_before_random(case, selected, number, code):
    fail(case, message(case, selected, number=number), selected, code)
    assert not case.calls


@pytest.mark.parametrize("field", ["UI", "UK", "MK"])
def test_authentication_precedes_random(case, field):
    envelope = message(case)
    envelope.document.find(field + "/V").text = "wrong"
    assert fail(case, envelope).status == 403
    assert not case.calls


@pytest.mark.parametrize("mode", ["extra", "wrong_type", "duplicate", "nested", "selection_mismatch"])
def test_strict_request_parser(case, mode):
    envelope = message(case)
    selected = (1,)
    if mode == "extra":
        ET.SubElement(envelope.document, "Amount")
    elif mode == "wrong_type":
        envelope.document.find("RT/PT2/PresentID").set("T", "S")
    elif mode == "duplicate":
        envelope.document.find("RT/PT2").append(deepcopy(envelope.document.find("RT/PT2/PresentID")))
    elif mode == "nested":
        ET.SubElement(envelope.document.find("RT/PT2/PresentID/V"), "x")
    else:
        selected = (2,)
    fail(case, envelope, selected, "invalid_request_fields")
    assert not case.calls


def test_capacity_failure_precedes_random_and_preserves_ticket(case):
    case.players.capacity_provider = lambda _: 0
    fail(case, code="unit_roster_capacity")
    assert not case.calls


def test_reserved_reward_capacity_is_not_consumed(case):
    change(case, lambda state: state.update(quest_state={"active": {"unit_reservations": [
        {"card_id": 17, "level": 1, "count": 4096}]}}))
    fail(case, code="unit_roster_capacity")
    assert not case.calls


def test_legacy_active_reservation_failure_precedes_random(case):
    change(case, lambda state: state.update(quest_state={"active": {}}))
    fail(case, code="quest_reservation_unavailable")
    assert not case.calls


def test_cas_retry_reuses_draw_and_keeps_concurrent_changes(case, monkeypatch):
    original = case.store.update_state
    attempts = []
    def concurrent(state, revision):
        attempts.append(1)
        if len(attempts) == 1:
            profile = case.store.profile()
            concurrent_state = deepcopy(profile.state)
            concurrent_state["sentinel"]["preserve"].append(2)
            original(concurrent_state, profile.revision)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", concurrent)
    case.service.receive(message(case), case.store, (1,))
    assert len(attempts) == 2 and len(case.calls) == 1
    assert case.store.profile().state["sentinel"]["preserve"] == [1, 2]


def test_three_failed_cas_attempts_do_not_commit_or_redraw(case, monkeypatch):
    attempts = []
    def conflict(*args):
        attempts.append(1)
        raise ValueError("conflict")
    monkeypatch.setattr(case.store, "update_state", conflict)
    fail(case, code="profile_changed")
    assert len(attempts) == 3 and len(case.calls) == 1


def test_ticket_changes_during_cas_cannot_receive_frozen_result(case, monkeypatch):
    original = case.store.update_state
    attempts = []
    def conflict(state, revision):
        attempts.append(1)
        if len(attempts) == 1:
            profile = case.store.profile()
            updated = deepcopy(profile.state)
            updated["tables"]["presents"][0]["Description"] = "changed"
            original(updated, revision)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    with pytest.raises(ProtocolError) as error:
        case.service.receive(message(case), case.store, (1,))
    assert error.value.code == "summon_ticket_changed"
    assert len(case.calls) == 1
    assert len(case.store.profile().state["tables"]["presents"]) == 3


@pytest.mark.parametrize("number", [1, 41, 42])
def test_encoding_failure_rolls_back_whole_claim(case, number):
    case.service._profiles = dict(case.service._profiles)
    case.service._profiles[number] = replace(case.service._profiles[number], response_limit=1)
    fail(case, code="response_too_large")


def test_later_state_change_rejects_exact_old_receipt(case):
    envelope = message(case)
    case.service.receive(envelope, case.store, (1,))
    change(case, lambda state: state["sentinel"]["preserve"].append(2))
    assert case.service.matches_request(case.store.profile(), envelope, (1,))
    fail(case, envelope, code="summon_ticket_retry_stale")
    assert len(case.calls) == 1


def test_refusals_are_logged_with_their_request_number(case, caplog):
    envelope = message(case)
    case.service.receive(envelope, case.store, (1,))
    change(case, lambda state: state["sentinel"]["preserve"].append(2))
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        fail(case, envelope, code="summon_ticket_retry_stale")
    assert any("Summon ticket claim 1 refused with summon_ticket_retry_stale" in line for line in caplog.messages)


def test_client_clock_rollback_claims_new_tickets_and_keeps_exact_replays_stale(case):
    first = message(case, (1,), tick=0x7FFFFFFF)
    case.service.receive(first, case.store, (1,))
    case.service.receive(message(case, (2,), tick=-0x80000000), case.store, (2,))
    assert len(case.calls) == 2
    history = case.store.profile().state["summon_ticket_history"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION and len(history["processed_keys"]) == 2
    assert all("client_time" not in entry for entry in history["receipts"])
    fail(case, first, (1,), "summon_ticket_retry_stale")
    assert len(case.calls) == 2


def test_evicted_receipt_repeat_still_routes_to_the_ticket_and_stays_stale(case, monkeypatch, caplog):
    monkeypatch.setattr(ledger, "HISTORY_LIMIT", 1)
    first = message(case, (1,), tick=1000)
    case.service.receive(first, case.store, (1,))
    case.service.receive(message(case, (2,), tick=1001), case.store, (2,))
    assert case.service.matches_request(case.store.profile(), first, (1,))
    assert not case.service.matches_request(case.store.profile(), message(case, (3,), tick=1002), (3,))
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        fail(case, first, (1,), "summon_ticket_request_stale")
        before = case.store.profile()
        with pytest.raises(ProtocolError) as error:
            case.presents.receive(first, case.store)
    assert error.value.code == "summon_ticket_request_stale"
    assert case.store.profile() == before and len(case.calls) == 2
    assert any("request 1/2" in line and "evicted" in line for line in caplog.messages)


def test_same_second_claims_are_distinct_requests(case):
    case.service.receive(message(case, (1,), tick=1000), case.store, (1,))
    case.service.receive(message(case, (2,), tick=1000), case.store, (2,))
    assert len(case.calls) == 2
    assert len(case.store.profile().state["summon_ticket_history"]["processed_keys"]) == 2


def test_watermark_history_upgrades_and_keeps_replaying_its_receipt(case):
    envelope = message(case, tick=3000)
    payload = case.service.receive(envelope, case.store, (1,))
    def legacy(state):
        history = state["summon_ticket_history"]
        state["summon_ticket_history"] = {"schema_version": ledger.LEGACY_SCHEMA_VERSION, "last_client_time": 3000,
            "seen_keys": list(history["processed_keys"]),
            "receipts": [{**entry, "client_time": 3000} for entry in history["receipts"]]}
    change(case, legacy, revision=False)
    before = case.store.profile()
    assert case.service.matches_request(before, envelope, (1,))
    assert case.service.receive(envelope, case.store, (1,)) == payload
    assert case.store.profile() == before
    case.service.receive(message(case, (2,), tick=1000), case.store, (2,))
    history = case.store.profile().state["summon_ticket_history"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION
    assert history["processed_keys"] == [entry["request"] for entry in history["receipts"]]
    assert len(case.calls) == 2


@pytest.mark.parametrize("field,value", [("state", "0" * 64), ("response", "broken"), ("candidate_index", 9999),
    ("mail_id", 2), ("client_time", 0), ("profile", "0" * 64)])
def test_corrupt_receipt_cannot_replay_another_result(case, field, value):
    envelope = message(case)
    case.service.receive(envelope, case.store, (1,))
    change(case, lambda state: state["summon_ticket_history"]["receipts"][0].update({field: value}), revision=False)
    fail(case, envelope, code="summon_ticket_history_invalid")
    assert len(case.calls) == 1


def test_rules_and_initializer_changes_are_detected(case, monkeypatch):
    case.service.rules = SummonTicketRules(())
    fail(case, code="summon_ticket_dependencies_changed")
    assert not case.calls


def test_rule_changes_invalidate_cached_claim(case):
    envelope = message(case)
    case.service.receive(envelope, case.store, (1,))
    case.service.rules = SummonTicketRules(())
    fail(case, envelope, code="summon_ticket_dependencies_changed")
    assert len(case.calls) == 1


def test_initializer_version_change_prevents_random(case, monkeypatch):
    initializer = case.players._initialization()
    replacement = SimpleNamespace(content_version="0" * 64, values=initializer.values)
    monkeypatch.setattr(case.players, "_initialization", lambda: replacement)
    fail(case, code="summon_ticket_dependencies_changed")
    assert not case.calls


def test_player_initialization_version_change_prevents_random(case):
    stand_in = SimpleNamespace(unit_initializer=case.players.unit_initializer, initialization_version="0" * 64)
    case.service.players = case.service.grants.players = case.service.grants.units.players = stand_in
    assert case.service.is_ready() is False
    assert case.service.status()["error"] == "summon_ticket_dependencies_changed"
    assert fail(case, code="summon_ticket_dependencies_changed").status == 503
    assert not case.calls


def test_readiness_matches_status_without_building_the_pool_status(case):
    pools = case.service.pools = case.service.grants.pools = copy(case.service.pools)
    pools.status = lambda: pytest.fail("Readiness built the full pool status")
    assert case.service.is_ready() is case.service.status()["ready"] is True
    pools.is_ready = lambda: False
    assert case.service.is_ready() is case.service.status()["ready"] is False
    assert case.service.status()["error"] == "summon_ticket_dependencies_changed"
    assert fail(case, code="summon_ticket_dependencies_changed").status == 503
    assert not case.calls


def test_non_object_save_is_refused_before_any_draw(case):
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps([]),))
    assert fail(case, code="player_state_schema").status == 503
    assert not case.calls


@pytest.mark.parametrize("field,value", [("Love", 100), ("SkillLevel", 2), ("GeneralPurposeFlags", 5)])
def test_grant_helper_cannot_add_unverified_initial_values(case, monkeypatch, field, value):
    original = case.players.credit_units
    def corrupt(state, grants):
        updated, added = original(state, grants)
        added[0][field] = value
        updated["player_state"]["units"][-1][field] = value
        return updated, added
    monkeypatch.setattr(case.players, "credit_units", corrupt)
    fail(case, code="summon_ticket_result_invalid")


def test_incomplete_or_disabled_pools_do_not_downgrade(case):
    with pytest.raises(ProtocolError) as error:
        case.service.validate_pool(1)
    assert error.value.code == "summon_ticket_pool_disabled"
    with pytest.raises(ProtocolError) as error:
        SummonTicketService(case.service.bootstrap, case.players, case.presents, case.service.pools,
                            rules=SummonTicketRules((1,)))
    assert error.value.code == "summon_pool_incomplete"


@pytest.mark.parametrize("value", [True, -1, None, 1.5])
def test_bad_random_source_rolls_back_claim(case, value):
    case.service.randbelow = lambda _: value
    fail(case, code="summon_random_invalid")


@pytest.mark.parametrize("value", [[350, 350], [True], [0], [-1], [2**31], "350", None])
def test_rules_reject_invalid_pool_lists(value):
    with pytest.raises(ValueError):
        SummonTicketRules(value)


def test_rules_convert_json_list_to_immutable_tuple():
    source = [350, 200]
    rules = SummonTicketRules(source)
    assert rules.enabled_pools == (200, 350)
    source.append(1)
    assert rules.enabled_pools == (200, 350)
    with pytest.raises(FrozenInstanceError):
        rules.enabled_pools = ()
