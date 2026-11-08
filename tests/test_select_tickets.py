from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
import json
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.errors import ProtocolError
import aigis_ps.request_ledger as ledger
from aigis_ps.select_tickets import SelectTicketService
from aigis_ps.storage import Store
from test_quest_service import bundle, request


@pytest.fixture
def case(bundle, tmp_path):
    assert bundle.selection_tickets is not None, bundle.selection_ticket_error
    bootstrap, players, presents = copy(bundle.bootstrap), copy(bundle.player_state), copy(bundle.presents)
    service = copy(bundle.selection_tickets)
    service.bootstrap, service.players, service.presents = bootstrap, players, presents
    service.catalog = copy(bundle.selection_tickets.catalog)
    service.grants = copy(bundle.selection_tickets.grants)
    service.grants.players = players
    service.grants._initial_values = dict(service.grants._initial_values)
    service._initial_values = dict(service._initial_values)
    service._profiles = dict(service._profiles)
    presents.bootstrap, presents.players, presents.selection_tickets = bootstrap, players, service
    state = players.initialize(bootstrap.initial_state())
    state["sentinel"] = {"preserve": [1]}
    state, mails = presents.enqueue(state, [
        {"kind": "selection_ticket", "config_id": 1, "count": 2, "description": "choose"},
        {"kind": "selection_ticket", "config_id": 190, "count": 1, "description": "level50"},
        {"kind": "free_crystal", "amount": 1, "description": "ordinary"}])
    return SimpleNamespace(bootstrap=bootstrap, players=players, presents=presents, service=service,
                           config=service.validate_config(1), mails=mails,
                           store=Store(tmp_path / "select-tickets.sqlite3", state))


def message(case, ids=(1,), *, select_id=1, card_id=None, number=1, tick=1000):
    if card_id is None:
        mail = next((row for row in case.mails if row["PresentID"] == ids[0]), case.mails[0])
        config = case.service.validate_config(mail.get("SelectionConfigID", 1))
        card_id = config.candidates[max(0, min(select_id - 1, len(config.candidates) - 1))].card_id
    return request(case.store, number, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", tuple(ids)), Column("SelectID", "I", (select_id,)),
        Column("SelectCardID", "I", (card_id,)))),)),), tick=tick)


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


def test_selected_first_candidate_is_not_random_and_has_complete_identity(case, monkeypatch):
    monkeypatch.setattr("secrets.randbelow", lambda _: pytest.fail("selection must not sample"))
    before = case.store.profile()
    envelope = message(case)
    result = case.presents.receive(envelope, case.store)
    after = case.store.profile()
    groups = {node.name: node for node in loads(result).children}
    assert tuple(groups) == ("SYSTEM", "PBP", "PPU")
    columns = {node.name: node for node in groups["PPU"].children}
    assert len(columns) == 21 and all(len(column.values) == 1 for column in columns.values())
    assert columns["A1"].values == (case.config.candidates[0].card_id,)
    assert columns["A7"].values == (1,) and columns["AB"].values == (2,)
    assert after.state["player_state"]["units"][:-1] == before.state["player_state"]["units"]
    assert after.state["player_state"]["battle"]["UX"] == 1
    assert after.state["player_state"]["last_unique_id"] == 2
    for key in ("game_param", "crystals", "possessions"):
        assert after.state["tables"][key] == before.state["tables"][key]
    assert after.revision == before.revision + 1
    assert [row["PresentID"] for row in case.presents.rows(after.state)] == [2, 3, 4]


def test_last_candidate_uses_original_one_based_order(case):
    selected = len(case.config.candidates)
    case.service.receive(message(case, select_id=selected), case.store, (1,))
    unit = case.store.profile().state["player_state"]["units"][-1]
    assert unit["CardID"] == case.config.candidates[selected - 1].card_id


def test_level50_configuration_keeps_growth_and_does_not_use_config_as_index(case):
    candidate = case.service.validate_config(190).candidates[0]
    assert candidate.level == 50
    case.service.receive(message(case, (3,), select_id=1), case.store, (3,))
    unit = case.store.profile().state["player_state"]["units"][-1]
    initial = case.players.initial_unit_values(candidate.card_id, 50, evolution_count=candidate.evolution_count)
    assert unit["CardID"] == candidate.card_id and unit["Exp"] == initial.experience > 0


@pytest.mark.parametrize("select_id", [0, -1, 100000])
def test_out_of_range_selection_is_rejected(case, select_id):
    error = fail(case, message(case, select_id=select_id, card_id=case.config.candidates[0].card_id))
    assert error.code in ("invalid_request_fields", "select_ticket_choice_invalid")


def test_index_and_card_id_must_refer_to_same_candidate(case):
    assert len(case.config.candidates) >= 2
    fail(case, message(case, select_id=1, card_id=case.config.candidates[1].card_id),
         code="select_ticket_choice_invalid")


def test_candidate_from_different_ticket_configuration_is_rejected(case):
    other = next(candidate for candidate in case.service.validate_config(190).candidates
                 if candidate.card_id != case.config.candidates[0].card_id)
    fail(case, message(case, select_id=1, card_id=other.card_id), code="select_ticket_choice_invalid")


def test_second_instance_of_same_card_is_kept_without_compensation(case):
    before = case.store.profile()
    case.service.receive(message(case), case.store, (1,))
    case.service.receive(message(case, (2,), tick=1001), case.store, (2,))
    units = case.store.profile().state["player_state"]["units"][-2:]
    assert units[0]["CardID"] == units[1]["CardID"]
    assert (units[0]["GetIndex"], units[0]["UniqueID"]) != (units[1]["GetIndex"], units[1]["UniqueID"])
    assert case.store.profile().state["tables"]["possessions"] == before.state["tables"]["possessions"]


def test_exact_reopen_retry_does_not_allocate_another_unit(case):
    envelope = message(case)
    result = case.service.receive(envelope, case.store, (1,))
    service = SelectTicketService(case.bootstrap, case.players, case.presents, case.service.catalog)
    reopened = Store(case.store.path)
    before = reopened.profile()
    assert service.matches_request(before, envelope, (1,))
    assert service.receive(envelope, reopened, (1,)) == result
    assert reopened.profile() == before


def test_profile_alias_two_has_identical_retry_identity(case):
    one, two = message(case), message(case, number=2)
    assert one.profile == two.profile and one.xml == two.xml
    result = case.service.receive(one, case.store, (1,))
    assert case.service.receive(two, case.store, (1,)) == result


def test_routing_does_not_take_ordinary_mail(case):
    assert case.service.matches_request(case.store.profile(), message(case), (1,))
    assert not case.service.matches_request(case.store.profile(), message(case, (4,)), (4,))


@pytest.mark.parametrize("ids,number,code", [((1, 2), 1, "select_ticket_single_required"),
    ((1, 4), 1, "select_ticket_single_required"), ((1,), 117, "select_ticket_batch_unsupported"),
    ((4,), 1, "select_ticket_mail_unsupported"), ((999,), 1, "present_not_found")])
def test_wrong_scope_does_not_consume_any_mail(case, ids, number, code):
    fail(case, message(case, ids, number=number), ids, code)


@pytest.mark.parametrize("field", ["UI", "UK", "MK"])
def test_identity_is_authenticated(case, field):
    envelope = message(case)
    envelope.document.find(field + "/V").text = "bad"
    assert fail(case, envelope).status == 403


@pytest.mark.parametrize("field", ["SelectID", "SelectCardID"])
def test_selection_fields_are_required_typed_scalar_in_pt2(case, field):
    envelope = message(case)
    node = envelope.document.find("RT/PT2/" + field)
    node.set("T", "S")
    fail(case, envelope, code="invalid_request_fields")


def test_zero_capacity_prevents_credit(case, monkeypatch):
    case.players.capacity_provider = lambda _: 0
    monkeypatch.setattr(case.players, "credit_units", lambda *_: pytest.fail("full roster reached credit"))
    fail(case, code="unit_roster_capacity")


def test_active_reward_reservation_keeps_last_identity_free(case):
    def reserve(state):
        state["player_state"]["battle"]["UX"] = 0x7FFFFFFF - 1
        state["quest_state"] = {"active": {"unit_reservations": [{"card_id": 17, "level": 1, "count": 1}]}}
    change(case, reserve)
    fail(case, code="unit_roster_capacity")


def test_full_technical_roster_cannot_use_fake_business_capacity(case):
    candidate = case.config.candidates[0]
    initial = case.service.grants.initial_values(candidate.card_id, candidate.level, candidate.evolution_count)
    def fill(state):
        player = state["player_state"]
        player["units"].extend(case.service.grants.expected_unit(candidate.card_id, initial, index, index + 1)
                               for index in range(1, 4096))
        player["battle"]["UX"] = 4095
        player["last_unique_id"] = 4096
    change(case, fill)
    case.players.capacity_provider = lambda _: 10000
    fail(case, code="unit_roster_capacity")


def test_legacy_multiple_quantity_mail_is_preserved_without_guessing(case):
    change(case, lambda state: state["tables"]["presents"][0].update(Content=2))
    fail(case, code="select_ticket_mail_unsupported")


@pytest.mark.parametrize("target", ["get_index", "unique_id"])
def test_global_identity_exhaustion_is_atomic(case, target):
    def exhaust(state):
        if target == "get_index":
            state["player_state"]["battle"]["UX"] = 0x7FFFFFFF
        else:
            state["player_state"]["last_unique_id"] = 0x7FFFFFFF
    change(case, exhaust)
    fail(case, code="unit_roster_capacity")


@pytest.mark.parametrize("number", [1, 41, 42])
def test_response_capacity_failure_preserves_ticket_and_roster(case, number):
    case.service._profiles[number] = replace(case.service._profiles[number], response_limit=1)
    fail(case, code="response_too_large")


def test_cas_retry_preserves_original_choice_and_concurrent_write(case, monkeypatch):
    original = case.store.update_state
    attempts = []
    def save(state, revision):
        attempts.append(revision)
        if len(attempts) == 1:
            profile = case.store.profile()
            concurrent = deepcopy(profile.state)
            concurrent["sentinel"]["preserve"].append(2)
            original(concurrent, profile.revision)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", save)
    case.service.receive(message(case, select_id=2), case.store, (1,))
    state = case.store.profile().state
    assert len(attempts) == 2
    assert state["sentinel"]["preserve"] == [1, 2]
    assert state["player_state"]["units"][-1]["CardID"] == case.config.candidates[1].card_id
    assert len(state["player_state"]["units"]) == 2


def test_three_cas_failures_commit_nothing(case, monkeypatch):
    attempts = []
    def save(*_):
        attempts.append(1)
        raise ValueError("conflict")
    monkeypatch.setattr(case.store, "update_state", save)
    fail(case, code="profile_changed")
    assert len(attempts) == 3


def test_changed_mail_during_cas_aborts_frozen_choice(case, monkeypatch):
    original = case.store.update_state
    attempts = []
    def save(state, revision):
        attempts.append(1)
        if len(attempts) == 1:
            profile = case.store.profile()
            changed = deepcopy(profile.state)
            changed["tables"]["presents"][0]["Description"] = "changed"
            original(changed, profile.revision)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", save)
    with pytest.raises(ProtocolError) as caught:
        case.service.receive(message(case), case.store, (1,))
    assert caught.value.code == "select_ticket_changed"
    assert len(case.store.profile().state["player_state"]["units"]) == 1
    assert len(case.presents.rows(case.store.profile().state)) == 4


def test_changed_catalog_during_cas_aborts(case, monkeypatch):
    original = case.store.update_state
    attempts = []
    def save(state, revision):
        attempts.append(1)
        if len(attempts) == 1:
            profile = case.store.profile()
            changed = deepcopy(profile.state)
            changed["sentinel"]["preserve"].append(2)
            original(changed, profile.revision)
            monkeypatch.setattr(case.service.catalog, "_content_version", "0" * 64)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", save)
    with pytest.raises(ProtocolError) as caught:
        case.service.receive(message(case), case.store, (1,))
    assert caught.value.code == "select_ticket_dependencies_changed"
    assert len(case.store.profile().state["player_state"]["units"]) == 1


def test_changed_card_master_refuses_claims_without_building_catalog_status(case, monkeypatch):
    envelope, catalog = message(case), case.service.catalog
    monkeypatch.setattr(catalog, "status", lambda: pytest.fail("Readiness must not build the catalog status"))
    assert case.service.validate_config(1).config_id == 1
    monkeypatch.setattr(catalog, "_cards", SimpleNamespace(card_ids=catalog._cards.card_ids,
                                                           snapshot=SimpleNamespace(get_xml=lambda _: b"changed-master")))
    error = fail(case, envelope, code="select_ticket_dependencies_changed")
    assert error.status == 503 and error.__cause__.code == "select_ticket_catalog_changed"
    with pytest.raises(ProtocolError) as caught:
        case.service.validate_config(1)
    assert caught.value.code == "select_ticket_dependencies_changed"


def test_parallel_identical_requests_share_single_commit(case, monkeypatch):
    original = case.store.update_state
    barrier, lock, attempts = Barrier(2), Lock(), []
    def save(state, revision):
        with lock:
            attempts.append(revision)
            wait = len(attempts) <= 2
        if wait:
            barrier.wait(timeout=30)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", save)
    before = case.store.profile()
    envelope = message(case)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outputs = list(executor.map(lambda _: case.service.receive(envelope, case.store, (1,)), range(2)))
    assert outputs[0] == outputs[1]
    assert case.store.profile().revision == before.revision + 1
    assert len(case.store.profile().state["player_state"]["units"]) == 2


def test_later_revision_makes_old_successful_request_stale(case):
    envelope = message(case)
    case.service.receive(envelope, case.store, (1,))
    change(case, lambda state: state["sentinel"]["preserve"].append(2))
    fail(case, envelope, code="select_ticket_retry_stale")


def test_same_second_new_ticket_works_but_evicted_old_body_cannot_repeat(case, monkeypatch, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    monkeypatch.setattr(ledger, "HISTORY_LIMIT", 1)
    first = message(case)
    case.service.receive(first, case.store, (1,))
    case.service.receive(message(case, (2,)), case.store, (2,))
    fail(case, first, code="select_ticket_request_stale")
    fail(case, first, code="select_ticket_request_stale", adapter=True)
    assert len(case.store.profile().state["player_state"]["units"]) == 3
    assert any("request 1/2" in record.getMessage() and record.levelname == "WARNING" for record in caplog.records)


def test_rolled_back_client_clock_still_claims_later_tickets(case):
    case.service.receive(message(case, tick=0x7FFFFFFF), case.store, (1,))
    case.service.receive(message(case, (2,), tick=-0x80000000), case.store, (2,))
    case.service.receive(message(case, (3,), tick=0x7FFFFFFE), case.store, (3,))
    assert len(case.store.profile().state["player_state"]["units"]) == 4
    assert len(case.store.profile().state["select_ticket_history"]["processed_keys"]) == 3


def test_watermark_history_upgrades_and_replays_its_receipt(case):
    envelope = message(case, tick=2000)
    response = case.service.receive(envelope, case.store, (1,))

    def legacy(state):
        history = state["select_ticket_history"]
        state["select_ticket_history"] = {"schema_version": ledger.LEGACY_SCHEMA_VERSION, "last_client_time": 2000,
            "seen_keys": list(history["processed_keys"]),
            "receipts": [{**entry, "client_time": 2000} for entry in history["receipts"]]}
    change(case, legacy, revision=False)
    saved = case.store.profile()
    assert case.service.receive(envelope, case.store, (1,)) == response
    assert case.store.profile() == saved
    case.service.receive(message(case, (2,), tick=1000), case.store, (2,))
    history = case.store.profile().state["select_ticket_history"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION
    assert history["processed_keys"] == [entry["request"] for entry in history["receipts"]]


@pytest.mark.parametrize("field,value", [("response", "bad"), ("state", "0" * 64), ("profile", "0" * 64),
    ("mail_id", 2), ("select_id", 2), ("card_id", 1), ("config_id", 99999), ("client_time", 0)])
def test_corrupt_receipt_does_not_return_or_grant_another_result(case, field, value):
    envelope = message(case)
    case.service.receive(envelope, case.store, (1,))
    change(case, lambda state: state["select_ticket_history"]["receipts"][0].update({field: value}), revision=False)
    error = fail(case, envelope)
    assert error.status in (409, 501, 503)


@pytest.mark.parametrize("field,value", [("Love", 100), ("SkillLevel", 2), ("GeneralPurposeFlags", 5),
                                       ("CanFame", 0), ("UniqueID", 3)])
def test_corrupt_cached_unit_is_not_replayed(case, field, value):
    envelope = message(case)
    case.service.receive(envelope, case.store, (1,))
    change(case, lambda state: state["select_ticket_history"]["receipts"][0]["unit"].update({field: value}), revision=False)
    fail(case, envelope, code="select_ticket_history_invalid")


@pytest.mark.parametrize("field,value", [("Love", 100), ("SkillLevel", 2), ("GeneralPurposeFlags", 5)])
def test_underlying_credit_cannot_add_unverified_fields(case, monkeypatch, field, value):
    original = case.players.credit_units
    def corrupt(state, grants):
        updated, added = original(state, grants)
        added[0][field] = value
        updated["player_state"]["units"][-1][field] = value
        return updated, added
    monkeypatch.setattr(case.players, "credit_units", corrupt)
    fail(case, code="select_ticket_result_invalid")


def test_underlying_credit_failure_does_not_consume_ticket(case, monkeypatch):
    def reject(state, grants):
        state["tables"]["presents"].clear()
        raise ProtocolError("unit_roster_capacity", "Injected failure", 409)
    monkeypatch.setattr(case.players, "credit_units", reject)
    fail(case, code="unit_roster_capacity")


def test_initialization_dependency_change_is_rejected(case, monkeypatch):
    original = case.players._initialization()
    monkeypatch.setattr(case.players, "_initialization", lambda: SimpleNamespace(
        content_version="0" * 64, values=original.values))
    envelope = request(case.store, 1, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", (1,)), Column("SelectID", "I", (1,)),
        Column("SelectCardID", "I", (case.config.candidates[0].card_id,)))),)),))
    fail(case, envelope, code="select_ticket_dependencies_changed")


def test_unknown_legacy_configuration_cannot_be_silently_replaced(case):
    change(case, lambda state: state["tables"]["presents"][0].update(SelectionConfigID=99999))
    envelope = request(case.store, 1, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", (1,)), Column("SelectID", "I", (1,)),
        Column("SelectCardID", "I", (case.config.candidates[0].card_id,)))),)),))
    assert fail(case, envelope).status in (501, 503)
