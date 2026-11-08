from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
import json
from threading import Barrier
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

import aigis_ps.request_ledger as ledger
from aigis_ps.alxml import Column, Container, loads
from aigis_ps.errors import ProtocolError
from aigis_ps.protocol import ProtocolRegistry, encode_wire, prepare_request
from aigis_ps.storage import Store
from aigis_ps.view_events import ViewEventService, parse_view_request
from test_quest_service import bundle, request


VALUE = "CardId=56,Id=1,Mode=0,Male=0"


@pytest.fixture
def case(bundle, tmp_path):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.schemas = dict(bootstrap.schemas)
    state = bundle.player_state.initialize(bootstrap.initial_state())
    state, added = bundle.player_state.credit_units(state, [{"card_id": 56, "level": 1, "count": 1}])
    unit = next(row for row in state["player_state"]["units"] if row["UniqueID"] == added[0]["UniqueID"])
    unit.update(Love=100, WatchLoveEv=2)
    state = bundle.card_status.update(state, [{"CardID": 56, "A7": 3}])
    state["sentinel"] = {"preserved": [1]}
    service = ViewEventService(bootstrap, bundle.player_state)
    return SimpleNamespace(service=service, store=Store(tmp_path / "view.sqlite3", state), bundle=bundle)


def message(case, value=VALUE, *, number=23, tick=1000, columns=None, nonce=None):
    if columns is None:
        columns = (Column("F8", "S", (value,)),)
    envelope = request(case.store, number, columns, tick=tick)
    if nonce is not None:
        document = deepcopy(envelope.document)
        document.find("GPVI/V").text = str(nonce)
        for child in list(document):
            if child.tag in ("VN", "ST", "HS"):
                document.remove(child)
        xml = prepare_request(ET.tostring(document, encoding="utf-8"), envelope.profile.st)
        envelope = ProtocolRegistry.load().decode(envelope.profile.route, encode_wire(xml, envelope.profile.request_key))
    return envelope


def fail(case, envelope, code=None):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.save(envelope, case.store)
    if code is not None:
        assert caught.value.code == code
    assert case.store.profile() == before
    return caught.value


def change(case, callback, *, same_revision=False):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    callback(state)
    if same_revision:
        with case.store.connection() as connection:
            connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    else:
        case.store.update_state(state, profile.revision)


@pytest.mark.parametrize("number,mode,male", [(1, 0, 0), (2, 1, 0), (3, 0, 1), (3, 1, 1)])
def test_parser_accepts_current_numeric_fields(number, mode, male):
    assert parse_view_request(f"CardId=56,Id={number},Mode={mode},Male={male}") == {
        "CardId": 56, "Id": number, "Mode": mode, "Male": male}


def test_parser_uses_key_identity_instead_of_field_position():
    assert parse_view_request("Male=0,Mode=0,Id=1,CardId=56") == parse_view_request(VALUE)


@pytest.mark.parametrize("number", [4, 10, 2147483647])
def test_parser_rejects_unconfirmed_episode_numbers(number):
    with pytest.raises(ProtocolError) as caught:
        parse_view_request(f"CardId=56,Id={number},Mode=0,Male=0")
    assert caught.value.code == "view_event_request_invalid"


@pytest.mark.parametrize("value", [
    None, 1, False, [], "", " ", "CardId=56,Id=1,Mode=0", VALUE + ",Unknown=1",
    VALUE + ",Id=1", VALUE.replace("Id=1", "id=1"), VALUE.replace("CardId=56", "CardID=56"),
    VALUE.replace("Id=1", "Id="), VALUE.replace("Id=1", "Id=-1"), VALUE.replace("Id=1", "Id=0"),
    VALUE.replace("Id=1", "Id=1.0"), VALUE.replace("Id=1", "Id=+1"), VALUE.replace("Id=1", "Id=１"),
    VALUE.replace("Id=1", "Id=1=2"), VALUE.replace("Mode=0", "Mode=2"), VALUE.replace("Male=0", "Male=2"),
    VALUE.replace("CardId=56", "CardId=0"), VALUE.replace("CardId=56", "CardId=2147483648"),
    VALUE.replace("CardId=56", "CardId=10000000000"), VALUE + ",", "," + VALUE,
    VALUE.replace(",Id", ", Id"), VALUE + "\n", VALUE + "\x00", VALUE + "\ud800",
    "文" * 86, "x" * 256,
])
def test_parser_rejects_malformed_or_oversized_values(value):
    with pytest.raises(ProtocolError) as caught:
        parse_view_request(value)
    assert caught.value.code == "view_event_request_invalid"


def test_save_only_updates_handoff_and_receipt_without_rewards(case):
    before = case.store.profile()
    value = "Male=0,Mode=0,Id=1,CardId=56"
    response = case.service.save(message(case, value), case.store)
    saved = case.store.profile()
    expected = deepcopy(before.state)
    expected["tables"]["config"][0]["EventViewRequest"] = value
    assert {key: val for key, val in saved.state.items() if key != "view_event_history"} == expected
    assert saved.revision == before.revision + 1
    assert tuple(node.name for node in loads(response).children) == ("SYSTEM",)
    history = saved.state["view_event_history"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION
    assert history["processed_keys"] == [history["receipts"][-1]["request"]]
    assert history["receipts"][-1]["value"] == value


def test_save_clear_save_invalidates_old_clear(case):
    case.service.save(message(case), case.store)
    clear = message(case, "", number=24, tick=1001)
    case.service.save(clear, case.store)
    assert case.store.profile().state["tables"]["config"][0]["EventViewRequest"] == ""
    case.service.save(message(case, tick=1002), case.store)
    fail(case, clear, "view_event_retry_stale")
    assert case.store.profile().state["tables"]["config"][0]["EventViewRequest"] == VALUE


@pytest.mark.parametrize("number,value", [(23, VALUE), (24, "")])
def test_same_value_new_action_commits_while_exact_retry_does_not(case, number, value):
    first = message(case, value, number=number, nonce="first")
    first_response = case.service.save(first, case.store)
    saved = case.store.profile()
    assert case.service.save(first, case.store) == first_response
    assert case.store.profile() == saved
    second = message(case, value, number=number, nonce="second")
    case.service.save(second, case.store)
    current = case.store.profile()
    assert current.revision == saved.revision + 1
    assert len(current.state["view_event_history"]["receipts"]) == 2
    assert len(current.state["view_event_history"]["processed_keys"]) == 2
    fail(case, first, "view_event_retry_stale")


def test_initial_empty_clear_cannot_replay_over_later_save(case):
    clear = message(case, "", number=24)
    before = case.store.profile()
    case.service.save(clear, case.store)
    assert case.store.profile().revision == before.revision + 1
    case.service.save(message(case, tick=1001), case.store)
    fail(case, clear, "view_event_retry_stale")


def test_historical_unknown_value_can_be_cleared(case):
    change(case, lambda state: state["tables"]["config"][0].update(EventViewRequest="retired=unknown"))
    case.service.save(message(case, "", number=24), case.store)
    assert case.store.profile().state["tables"]["config"][0]["EventViewRequest"] == ""


def test_pointer_does_not_require_current_ownership_or_grant_history(case):
    change(case, lambda state: state["player_state"].update(units=[
        unit for unit in state["player_state"]["units"] if unit["CardID"] != 56]))
    before = case.store.profile()
    assert not any(unit["CardID"] == 56 for unit in before.state["player_state"]["units"])
    case.service.save(message(case), case.store)
    after = case.store.profile()
    assert after.state["tables"]["card_status"] == before.state["tables"]["card_status"]
    assert after.state["tables"]["presents"] == before.state["tables"]["presents"]
    assert after.state["player_state"] == before.state["player_state"]
    assert after.state["tables"]["config"][0]["EventViewRequest"] == VALUE


def test_reopen_exact_retry_never_reads_clock(case):
    envelope = message(case)
    payload = case.service.save(envelope, case.store)
    saved = case.store.profile()
    case.service.bootstrap.clock = lambda: pytest.fail("Handoff state must not read a clock")
    reopened = Store(case.store.path)
    service = ViewEventService(case.service.bootstrap, case.bundle.player_state)
    assert service.save(envelope, reopened) == payload
    assert reopened.profile() == saved


@pytest.mark.parametrize("number,value", [(23, ""), (24, VALUE)])
def test_save_and_clear_cannot_exchange_empty_semantics(case, number, value):
    assert fail(case, message(case, value, number=number)).status == 400


@pytest.mark.parametrize("columns", [
    (), (Column("F8", "S", ()),), (Column("F8", "S", (VALUE, VALUE)),),
    (Column("F8", "I", (1,)),), (Column("F8", "B", (True,)),),
    (Column("F8", "S", (VALUE,)), Column("F8", "S", (VALUE,))),
    (Column("F8", "S", (VALUE,)), Column("A4", "I", (1,))),
    (Container("F8", (Column("Value", "S", (VALUE,)),)),),
    (Column("F8", "S", ("文" * 86,)),),
])
def test_protocol_requires_one_bounded_string_and_no_other_business_fields(case, columns):
    fail(case, message(case, columns=columns), "invalid_request_fields")


@pytest.mark.parametrize("kind", ["mixed", "nested", "root", "wrong_operation", "ambiguous_operation"])
def test_protocol_rejects_unexpected_container_or_operation(case, kind):
    envelope = message(case)
    if kind == "mixed":
        envelope.document.find("F8").text = "unexpected"
    elif kind == "nested":
        ET.SubElement(envelope.document.find("F8/V"), "Unexpected")
    elif kind == "root":
        envelope.document.tag = "Other"
    elif kind == "wrong_operation":
        envelope = request(case.store, 21, (Column("F8", "S", (VALUE,)),))
    else:
        envelope = replace(envelope, profile=replace(envelope.profile, registration_keys=(23, 24)))
    fail(case, envelope, "invalid_request_fields")


@pytest.mark.parametrize("field", ["UI", "UK", "MK"])
def test_authentication_failure_never_changes_pointer(case, field):
    envelope = message(case)
    envelope.document.find(field + "/V").text = "different"
    assert fail(case, envelope).status == 403


@pytest.mark.parametrize("kind", [
    "schema", "duplicate", "future_revision", "zero_revision", "nonstring_response", "response", "state_hash",
    "actual_state", "operation", "value", "client_time", "missing_processed", "bad_hash", "clear_nonempty",
])
def test_same_revision_receipt_corruption_is_rejected(case, kind):
    envelope = message(case)
    case.service.save(envelope, case.store)

    def corrupt(state):
        history = state["view_event_history"]
        entry = history["receipts"][0]
        if kind == "schema":
            history["schema_version"] = True
        elif kind == "duplicate":
            history["receipts"].append(deepcopy(entry))
        elif kind == "future_revision":
            entry["revision"] += 1
        elif kind == "zero_revision":
            entry["revision"] = 0
        elif kind == "nonstring_response":
            entry["response"] = None
        elif kind == "response":
            entry["response"] = "<DA/>"
        elif kind == "state_hash":
            entry["state"] = "0" * 64
        elif kind == "actual_state":
            state["sentinel"]["preserved"].append(2)
        elif kind == "operation":
            entry["operation"] = True
        elif kind == "value":
            entry["value"] = "different"
        elif kind == "client_time":
            entry["client_time"] = 1000
        elif kind == "missing_processed":
            history["processed_keys"] = []
        elif kind == "bad_hash":
            entry["request"] = "invalid"
        else:
            entry["operation"] = 24

    change(case, corrupt, same_revision=True)
    fail(case, envelope, "view_event_history_invalid")


def test_old_content_and_later_unrelated_revision_make_receipt_stale(case):
    envelope = message(case)
    case.service.save(envelope, case.store)
    change(case, lambda state: state["view_event_history"]["receipts"][0].update(content="0" * 64), same_revision=True)
    fail(case, envelope, "view_event_retry_stale")
    change(case, lambda state: state["sentinel"]["preserved"].append(2))
    fail(case, envelope, "view_event_retry_stale")


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("target", ["current", "config36"])
def test_response_and_full_config_capacity_failures_are_atomic(case, cached, target):
    envelope = message(case)
    if cached:
        case.service.save(envelope, case.store)
    if target == "current":
        envelope = replace(envelope, profile=replace(envelope.profile, response_limit=1))
    else:
        case.service._configuration_profile = replace(case.service._configuration_profile, response_limit=1)
    fail(case, envelope, "response_too_large")


def test_receipt_byte_limit_is_checked_before_commit(case, monkeypatch):
    monkeypatch.setattr(case.service._ledger, "response_limit", 1)
    fail(case, message(case), "view_event_history_invalid")


def test_schema_fingerprint_change_rejects_new_action_and_retry(case):
    envelope = message(case)
    case.service.save(envelope, case.store)
    schema = case.service.bootstrap.schemas["config"]
    case.service.bootstrap.schemas["config"] = replace(schema, fields=tuple(reversed(schema.fields)))
    fail(case, envelope, "view_event_configuration_changed")
    fail(case, message(case, tick=1001), "view_event_configuration_changed")
    assert case.service.status()["ready"] is False


def test_evicted_request_remains_rejected_inside_the_processed_window(case, monkeypatch):
    monkeypatch.setattr(ledger, "HISTORY_LIMIT", 2)
    monkeypatch.setattr(ledger, "PROCESSED_LIMIT", 3)
    first = message(case, nonce="first")
    case.service.save(first, case.store)
    case.service.save(message(case, nonce="second"), case.store)
    case.service.save(message(case, nonce="third"), case.store)
    history = case.store.profile().state["view_event_history"]
    assert len(history["receipts"]) == 2 and len(history["processed_keys"]) == 3
    fail(case, first, "view_event_request_stale")
    case.service.save(message(case, nonce="fourth"), case.store)
    assert len(case.store.profile().state["view_event_history"]["processed_keys"]) == 3


def test_rolled_back_client_clock_clears_pending_playback(case):
    case.service.save(message(case, tick=0x7FFFFFFF), case.store)
    clear = message(case, "", number=24, tick=-0x80000000)
    case.service.save(clear, case.store)
    case.service.save(message(case, tick=1000, nonce="rolled-back"), case.store)
    case.service.save(message(case, "", number=24, tick=999), case.store)
    assert case.store.profile().state["tables"]["config"][0]["EventViewRequest"] == ""
    fail(case, clear, "view_event_retry_stale")


def test_watermark_history_upgrades_without_rejecting_older_client_time(case):
    envelope = message(case, tick=2000)
    payload = case.service.save(envelope, case.store)

    def legacy(state):
        history = state["view_event_history"]
        state["view_event_history"] = {"schema_version": ledger.LEGACY_SCHEMA_VERSION, "last_client_time": 2000,
            "seen_keys": list(history["processed_keys"]),
            "receipts": [{**entry, "client_time": 2000} for entry in history["receipts"]]}
    change(case, legacy, same_revision=True)
    saved = case.store.profile()
    assert case.service.save(envelope, case.store) == payload
    assert case.store.profile() == saved
    case.service.save(message(case, "", number=24, tick=1000), case.store)
    history = case.store.profile().state["view_event_history"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION
    assert history["processed_keys"] == [entry["request"] for entry in history["receipts"]]
    assert case.store.profile().state["tables"]["config"][0]["EventViewRequest"] == ""


def test_cas_retry_preserves_unrelated_change_without_replaying_other_logic(case, monkeypatch):
    original = case.store.update_state
    attempts = []

    def conflict(state, revision):
        attempts.append(revision)
        if len(attempts) == 1:
            current = case.store.profile()
            changed = deepcopy(current.state)
            changed["sentinel"]["preserved"].append(2)
            original(changed, current.revision)
        return original(state, revision)

    monkeypatch.setattr(case.store, "update_state", conflict)
    case.service.save(message(case), case.store)
    saved = case.store.profile()
    assert len(attempts) == 2
    assert saved.state["sentinel"]["preserved"] == [1, 2]
    assert len(saved.state["view_event_history"]["receipts"]) == 1


def test_cas_retry_aborts_when_another_action_changed_pointer(case, monkeypatch):
    original = case.store.update_state
    concurrent = []

    def conflict(state, revision):
        current = case.store.profile()
        changed = deepcopy(current.state)
        changed["tables"]["config"][0]["EventViewRequest"] = "another=handoff"
        concurrent.append(original(changed, current.revision))
        return original(state, revision)

    monkeypatch.setattr(case.store, "update_state", conflict)
    with pytest.raises(ProtocolError) as caught:
        case.service.save(message(case), case.store)
    assert caught.value.code == "view_event_changed"
    assert len(concurrent) == 1 and case.store.profile() == concurrent[0]
    assert "view_event_history" not in concurrent[0].state


def test_repeated_cas_conflicts_do_not_commit(case, monkeypatch):
    attempts = []

    def conflict(state, revision):
        attempts.append(revision)
        raise ValueError("fixture conflict")

    monkeypatch.setattr(case.store, "update_state", conflict)
    fail(case, message(case), "profile_changed")
    assert len(attempts) == 3


def test_concurrent_same_request_commits_once(case):
    envelope = message(case)
    before = case.store.profile()
    barrier = Barrier(4)

    def call():
        barrier.wait()
        return case.service.save(envelope, case.store)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: call(), range(4)))
    assert all(response == results[0] for response in results)
    saved = case.store.profile()
    assert saved.revision == before.revision + 1
    assert len(saved.state["view_event_history"]["receipts"]) == 1
