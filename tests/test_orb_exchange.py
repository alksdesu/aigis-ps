from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import FrozenInstanceError
from itertools import permutations
import hashlib
import json
import logging
from pathlib import Path
import struct
from types import SimpleNamespace
import zlib

import pytest

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.errors import ProtocolError
from aigis_ps.orb_exchange import HISTORY_LIMIT, ORB_CATEGORY, OrbExchangeService
from aigis_ps.possessions import MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES, PossessionService
from aigis_ps.request_ledger import LEGACY_SCHEMA_VERSION, PROCESSED_LIMIT, SCHEMA_VERSION
from aigis_ps.storage import Store
from test_al_format import schema_bytes
from test_present_items import item_table, row as item_row
from test_quest_service import bundle, request


ITEMS = tuple(f"evolve_orb_10000{index}" for index in range(1, 8))
GIFT = "gift_0002_ruby"
STORED_ORB = "evolve_orb_".ljust(STORED_ITEM_NAME_BYTES, "9")
ITEM_SCHEMA = (("ItemID", 32), ("ID", 1), ("Category", 1), ("Stack", 1))


@pytest.fixture(scope="module")
def service(bundle):
    return OrbExchangeService(bundle.bootstrap, bundle.player_state, bundle.possessions,
                              lambda name: bundle.native_cdn.assets.read(name)[0])


@pytest.fixture
def case(bundle, service, tmp_path):
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state["tables"]["game_param"][0]["Money"] = 1000000
    state["tables"]["possessions"] = [{"ItemID": name, "Count": 100} for name in ITEMS]
    state["tables"]["possessions"].append({"ItemID": GIFT, "Count": 9})
    return SimpleNamespace(service=service, bundle=bundle, store=Store(tmp_path / "orbs.sqlite3", state))


def message(case, item=ITEMS[0], base=ITEMS[1], *, tick=1000, columns=None, number=139):
    columns = (Column("ITEM", "S", (item,)), Column("BASE", "S", (base,))) if columns is None else columns
    return request(case.store, number, columns, tick=tick)


def counts(state):
    return {row["ItemID"]: row["Count"] for row in state["tables"]["possessions"]}


def change(case, callback):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    callback(state)
    case.store.update_state(state, profile.revision)


def overwrite(case, callback):
    state = case.store.profile().state
    callback(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def fail(case, envelope, code):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.exchange(envelope, case.store)
    assert caught.value.code == code
    assert case.store.profile() == before
    return caught.value


def config_table(definitions):
    fields = [("ItemID", 0, 1, b"\0" * 8), ("ItemName", 4, 32, b""), ("Category", 8, 1, b"\0" * 8),
              ("Price", 12, 1, b"\0" * 8), ("AmountOfRequire", 16, 1, b"\0" * 8),
              ("AmountOfCreate", 20, 1, b"\0" * 8)]
    schema = schema_bytes(fields, 24)
    rows, pool = bytearray(), bytearray()
    for name, price, required, created in definitions:
        identifier = (zlib.crc32(name.encode()) ^ 0x80000000) - 0x80000000
        rows += struct.pack("<iIiiii", identifier, len(pool), 2051995164, price, required, created)
        pool += name.encode() + b"\0"
    start = 28 + len(schema)
    return struct.pack("<4sBBHHHIII4s", b"ALTB", 1, 0x14, len(definitions), 28, start, 24,
                       len(pool), start + len(rows), b"orbs") + schema + rows + pool


def configured(case, definitions):
    raw = config_table(definitions)
    return OrbExchangeService(case.bundle.bootstrap, case.bundle.player_state, case.bundle.possessions,
                              lambda name: raw if name == "ItemOrbExchangeConfig.atb" else
                              case.bundle.native_cdn.assets.read(name)[0])


def test_actual_definitions_and_frozen_quote(case):
    assert {row.item_id for row in case.service.definitions} == set(ITEMS)
    assert all((row.price, row.amount_required, row.amount_created, row.stack) == (15000, 5, 1, 999)
               for row in case.service.definitions)
    before = case.store.profile()
    quote = case.service.quote(before.state, ITEMS[0], ITEMS[1])
    assert (quote.money_after, quote.item_after, quote.base_after) == (985000, 101, 95)
    with pytest.raises(FrozenInstanceError):
        quote.price = 1
    assert case.store.profile() == before
    assert case.service.handlers() == {139: case.service.exchange}
    assert case.service.status()["definitions"] == 7


@pytest.mark.parametrize("item,base", tuple(permutations(ITEMS, 2)))
def test_all_42_directions_and_exact_response(case, item, base):
    before = case.store.profile()
    response = case.service.exchange(message(case, item, base), case.store)
    after = case.store.profile()
    expected = deepcopy(before.state)
    expected["tables"]["game_param"][0]["Money"] -= 15000
    for row in expected["tables"]["possessions"]:
        row["Count"] += 1 if row["ItemID"] == item else -5 if row["ItemID"] == base else 0
    assert {key: value for key, value in after.state.items() if key != "orb_exchange_history"} == expected
    tree = {node.name: node for node in loads(response).children}
    assert set(tree) == {"SYSTEM", "PGP", "POSSESSION"}
    assert tree["PGP"].children == (Column("A1", "I", (985000,)),)
    assert tree["POSSESSION"].children == (Column("ItemID", "S", (base, item)), Column("Count", "I", (95, 101)),
                                           Column("Enabled", "I", (0, 0)))


def test_target_price_create_and_source_required_are_independent(case):
    custom = configured(case, [(ITEMS[0], 321, 17, 2), (ITEMS[1], 987, 7, 4)])
    quote = custom.quote(case.store.profile().state, ITEMS[0], ITEMS[1])
    assert (quote.price, quote.amount_required, quote.amount_created) == (321, 7, 2)
    assert custom.content_version != case.service.content_version
    custom.exchange(message(case), case.store)
    state = case.store.profile().state
    assert state["tables"]["game_param"][0]["Money"] == 999679
    assert (counts(state)[ITEMS[0]], counts(state)[ITEMS[1]]) == (102, 93)


@pytest.mark.parametrize("item,base,code", [(ITEMS[0], ITEMS[0], "orb_exchange_same_item"),
    ("", ITEMS[1], "orb_exchange_item_unknown"), (ITEMS[0], "", "orb_exchange_item_unknown"),
    ("evolve_orb_000001", ITEMS[1], "orb_exchange_item_unknown"),
    (ITEMS[0], GIFT, "orb_exchange_item_unknown"), ("unknown", ITEMS[1], "orb_exchange_item_unknown")])
def test_invalid_item_pairs_never_write(case, item, base, code):
    fail(case, message(case, item, base), code)


@pytest.mark.parametrize("columns", [(), (Column("ITEM", "S", (ITEMS[0],)),),
    (Column("ITEM", "I", (1,)), Column("BASE", "S", (ITEMS[1],))),
    (Column("ITEM", "S", (ITEMS[0], ITEMS[2])), Column("BASE", "S", (ITEMS[1],))),
    (Column("ITEM", "S", (ITEMS[0],)), Column("BASE", "S", ())),
    (Container("ITEM"), Column("BASE", "S", (ITEMS[1],))),
    (Column("ITEM", "S", (ITEMS[0],)), Column("BASE", "S", (ITEMS[1],)), Column("NUM", "I", (1,))),
    (Column("ITEM", "S", (ITEMS[0],)), Column("BASE", "S", (ITEMS[1],)), Column("PRICE", "I", (1,)))])
def test_strict_request_shape(case, columns):
    fail(case, message(case, columns=columns), "invalid_request_fields")


def test_wrong_profile_rejected(case):
    fail(case, message(case, number=10), "invalid_request_fields")


@pytest.mark.parametrize("money,source,target,code", [(14999, 100, 100, "orb_exchange_money_insufficient"),
    (15000, 100, 100, "orb_exchange_native_balance"), (1000000, 4, 100, "orb_exchange_material_insufficient"),
    (1000000, 100, 999, "orb_exchange_stack_exceeded"), (1000000, 100, 0x7FFFFFFF, "orb_exchange_stack_exceeded")])
def test_insufficient_balances_and_overflow_are_atomic(case, money, source, target, code):
    def update(state):
        state["tables"]["game_param"][0]["Money"] = money
        state["tables"]["possessions"][0]["Count"] = target
        state["tables"]["possessions"][1]["Count"] = source
    change(case, update)
    fail(case, message(case), code)


def test_boundary_zero_source_and_full_target_are_returned(case):
    def update(state):
        state["tables"]["game_param"][0]["Money"] = 15001
        state["tables"]["possessions"][0]["Count"] = 998
        state["tables"]["possessions"][1]["Count"] = 5
    change(case, update)
    response = case.service.exchange(message(case), case.store)
    tree = {node.name: node for node in loads(response).children}
    assert tree["POSSESSION"].children[1].values == (0, 999)
    assert case.store.profile().state["tables"]["game_param"][0]["Money"] == 1


def test_exact_money_can_reach_zero_when_player_experience_remains(case):
    change(case, lambda state: state["tables"]["game_param"][0].update(Money=15000, Experience=1))
    case.service.exchange(message(case), case.store)
    wallet = case.store.profile().state["tables"]["game_param"][0]
    assert wallet["Money"] == 0 and wallet["Experience"] == 1


def test_exact_retry_survives_reopen_without_second_debit(case):
    envelope = message(case)
    response = case.service.exchange(envelope, case.store)
    before = case.store.profile()
    reopened = Store(case.store.path)
    assert case.service.exchange(envelope, reopened) == response
    assert reopened.profile() == before


def test_stale_revision_and_new_content_reject_old_receipts(case):
    envelope = message(case)
    case.service.exchange(envelope, case.store)
    other = configured(case, [(name, 15001, 5, 1) for name in ITEMS])
    before = case.store.profile()
    with pytest.raises(ProtocolError, match="changed after"):
        other.exchange(envelope, case.store)
    assert case.store.profile() == before
    change(case, lambda state: state.update(other_change=1))
    fail(case, envelope, "orb_exchange_retry_stale")


def test_evicted_same_second_request_cannot_run_again(case):
    pairs = tuple(permutations(ITEMS, 2))
    first = message(case, *pairs[0])
    for pair in pairs[:HISTORY_LIMIT + 1]:
        case.service.exchange(message(case, *pair), case.store)
    history = case.store.profile().state["orb_exchange_history"]
    assert len(history["receipts"]) == HISTORY_LIMIT and len(history["processed_keys"]) == HISTORY_LIMIT + 1
    fail(case, first, "orb_exchange_request_stale")


def test_client_clock_rollback_is_a_new_exchange_while_exact_repeats_stay_rejected(case):
    for tick in (0x7FFFFFFF, -0x80000000, 0):
        case.service.exchange(message(case, tick=tick), case.store)
    fail(case, message(case, tick=0x7FFFFFFF), "orb_exchange_retry_stale")
    state = case.store.profile().state
    assert (counts(state)[ITEMS[0]], counts(state)[ITEMS[1]]) == (103, 85)
    assert state["orb_exchange_history"]["schema_version"] == SCHEMA_VERSION
    assert len(state["orb_exchange_history"]["processed_keys"]) == 3


def test_legacy_watermark_history_upgrades_and_keeps_replaying(case):
    envelope = message(case, tick=5000)
    response = case.service.exchange(envelope, case.store)

    def legacy(state):
        receipts = [{**entry, "client_time": 5000} for entry in state["orb_exchange_history"]["receipts"]]
        state["orb_exchange_history"] = {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": receipts,
                                         "last_client_time": 5000, "seen_keys": [receipts[-1]["request"]]}
    overwrite(case, legacy)
    before = case.store.profile()
    assert case.service.exchange(envelope, case.store) == response
    assert case.store.profile() == before
    case.service.exchange(message(case, ITEMS[2], ITEMS[3], tick=4999), case.store)
    history = case.store.profile().state["orb_exchange_history"]
    assert history["schema_version"] == SCHEMA_VERSION
    assert history["processed_keys"] == [entry["request"] for entry in history["receipts"]]
    assert all("client_time" not in entry for entry in history["receipts"])


def test_full_legacy_second_upgrades_and_accepts_an_earlier_client_time(case):
    keys = [hashlib.sha256(str(index).encode()).hexdigest() for index in range(PROCESSED_LIMIT)]
    change(case, lambda state: state.update(orb_exchange_history={"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [],
                                                               "last_client_time": 1000, "seen_keys": keys}))
    envelope = message(case, tick=999)
    response = case.service.exchange(envelope, case.store)
    assert case.service.exchange(envelope, case.store) == response
    case.service.exchange(message(case, ITEMS[2], ITEMS[3]), case.store)
    history = case.store.profile().state["orb_exchange_history"]
    assert len(history["processed_keys"]) == PROCESSED_LIMIT and history["processed_keys"][:-2] == keys[2:]


def test_concurrent_identical_request_commits_once(case):
    envelope = message(case)
    revision = case.store.profile().revision
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: case.service.exchange(envelope, case.store), range(2)))
    assert results[0] == results[1]
    assert case.store.profile().revision == revision + 1


def test_concurrent_distinct_requests_preserve_both_debits(case):
    envelopes = [message(case), message(case, ITEMS[2], ITEMS[3])]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda envelope: case.service.exchange(envelope, case.store), envelopes))
    state = case.store.profile().state
    assert state["tables"]["game_param"][0]["Money"] == 970000
    assert [counts(state)[name] for name in ITEMS[:4]] == [101, 95, 101, 95]


@pytest.mark.parametrize("registration", [137, 139])
def test_each_response_preflight_failure_does_not_debit(case, monkeypatch, registration):
    from aigis_ps import orb_exchange
    original = orb_exchange.encode_response
    def encode(profile, data):
        if profile.registration_keys == (registration,):
            raise ProtocolError("test_capacity", "Synthetic capacity failure", 503)
        return original(profile, data)
    monkeypatch.setattr(orb_exchange, "encode_response", encode)
    fail(case, message(case), "test_capacity")


def test_credit_failure_after_pure_debit_does_not_change_store(case, monkeypatch):
    def rejected(*_args):
        raise ProtocolError("test_credit", "Synthetic credit failure", 503)
    monkeypatch.setattr(case.service.possessions, "credit", rejected)
    fail(case, message(case), "test_credit")


def test_three_cas_failures_do_not_commit(case, monkeypatch):
    calls = []
    def conflict(*args):
        calls.append(args)
        raise ValueError("conflict")
    monkeypatch.setattr(case.store, "update_state", conflict)
    fail(case, message(case), "profile_changed")
    assert len(calls) == 3


def test_cas_conflict_requotes_fresh_wallet_and_preserves_other_changes(case, monkeypatch):
    original = case.store.update_state
    calls = []
    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            current = case.store.profile()
            concurrent = deepcopy(current.state)
            concurrent["tables"]["game_param"][0]["Money"] -= 123
            concurrent["other_transaction"] = {"preserve": True}
            original(concurrent, current.revision)
            raise ValueError("conflict")
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    case.service.exchange(message(case), case.store)
    state = case.store.profile().state
    assert calls == [0, 1]
    assert state["tables"]["game_param"][0]["Money"] == 984877
    assert state["other_transaction"] == {"preserve": True}
    assert (counts(state)[ITEMS[0]], counts(state)[ITEMS[1]]) == (101, 95)


@pytest.mark.parametrize("history", [None, {}, {"schema_version": True, "receipts": [], "last_client_time": None, "seen_keys": []},
    {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [], "last_client_time": True, "seen_keys": []},
    {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [], "last_client_time": None, "seen_keys": ["a" * 64]},
    {"schema_version": SCHEMA_VERSION, "receipts": [], "processed_keys": ["a" * 64, "a" * 64]},
    {"schema_version": SCHEMA_VERSION, "receipts": [], "processed_keys": [], "seen_keys": []}])
def test_bad_history_is_not_reset(case, history):
    change(case, lambda state: state.update(orb_exchange_history=history))
    fail(case, message(case), "orb_exchange_history_invalid")


def test_native_balance_refusal_is_logged_because_the_client_resends_it(case, caplog):
    change(case, lambda state: state["tables"]["game_param"][0].update(Money=15000, Experience=0))
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        fail(case, message(case), "orb_exchange_native_balance")
    assert [record.getMessage() for record in caplog.records if record.name == "aigis_ps"] == [
        "Orb exchange 139 refused with orb_exchange_native_balance; the native client shows a communication "
        "error and resends the same request until the server accepts it"]


@pytest.mark.parametrize("definitions", [[], [(ITEMS[0], 15000, 5, 1)] * 2,
    [(ITEMS[0], 0, 5, 1)], [(ITEMS[0], 15000, -1, 1)], [(ITEMS[0], 15000, 5, 0)],
    [("evolve_orb_000001", 15000, 5, 1)], [("unknown", 15000, 5, 1)]])
def test_invalid_or_non_orb_catalog_definitions_fail(case, definitions):
    with pytest.raises(ProtocolError) as caught:
        configured(case, definitions)
    assert caught.value.code == "orb_exchange_catalog_invalid" and caught.value.status == 503


def stored_name_exchange(case):
    items = item_table([item_row(GIFT), *(item_row(name, Category=ORB_CATEGORY) for name in (*ITEMS, STORED_ORB))],
                       ITEM_SCHEMA)
    payloads = {"ItemConfigTable.atb": items,
                "ItemOrbExchangeConfig.atb": config_table([(name, 15000, 5, 1) for name in (ITEMS[0], STORED_ORB)])}
    possessions = PossessionService(Path("."), asset_reader=payloads.__getitem__)
    bootstrap = copy(case.bundle.bootstrap)
    bootstrap.possession_provider = possessions.startup_response
    service = OrbExchangeService(bootstrap, case.bundle.player_state, possessions, payloads.__getitem__)
    return SimpleNamespace(service=service, bundle=case.bundle, store=case.store, possessions=possessions)


def committed_content(service, possessions):
    content = {"policy": 2, "resources": service.status()["resource_sha256"],
               "possession_definitions": possessions.asset_sha256}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def test_targets_past_the_native_cell_are_listed_without_changing_the_content(case):
    stored = stored_name_exchange(case)
    status = stored.service.status()
    assert len(STORED_ORB.encode()) == STORED_ITEM_NAME_BYTES > MAX_ITEM_NAME_BYTES
    assert status["ready"] is True and status["definitions"] == 2
    assert status["unsupported_item_names"] == [STORED_ORB]
    assert f"{MAX_ITEM_NAME_BYTES}-byte native cell" in status["name_policy"]
    assert stored.service.content_version == committed_content(stored.service, stored.possessions)
    assert case.service.status()["unsupported_item_names"] == []
    assert case.service.content_version == committed_content(case.service, case.bundle.possessions)


@pytest.mark.parametrize("owned", [False, True])
def test_targets_past_the_native_cell_are_refused_before_any_write(case, owned):
    stored = stored_name_exchange(case)
    if owned:
        change(case, lambda state: state["tables"]["possessions"].append({"ItemID": STORED_ORB, "Count": 100}))
    state = case.store.profile().state
    with pytest.raises(ProtocolError) as quoted:
        stored.service.quote(state, STORED_ORB, ITEMS[0])
    error = fail(stored, message(stored, STORED_ORB, ITEMS[0]), "orb_exchange_item_unsupported")
    assert (quoted.value.code, quoted.value.status, error.status, str(error)) == (
        "orb_exchange_item_unsupported", 501, 501, "Exchange target name exceeds the native cell")
    if not owned:
        with pytest.raises(ProtocolError) as credited:
            stored.possessions.credit(state, {STORED_ORB: 1})
        assert (credited.value.code, credited.value.status) == ("possession_item_unreplayable", 503)


def test_stored_source_names_past_the_native_cell_stay_exchangeable(case):
    stored = stored_name_exchange(case)
    change(case, lambda state: state["tables"]["possessions"].append({"ItemID": STORED_ORB, "Count": 100}))
    quote = stored.service.quote(case.store.profile().state, ITEMS[0], STORED_ORB)
    assert (quote.base_id, quote.item_after, quote.base_after) == (STORED_ORB, 101, 95)
    envelope = message(stored, ITEMS[0], STORED_ORB)
    response = stored.service.exchange(envelope, case.store)
    saved = case.store.profile()
    assert (counts(saved.state)[ITEMS[0]], counts(saved.state)[STORED_ORB]) == (101, 95)
    assert {node.name: node for node in loads(response).children}["POSSESSION"].children[:2] == (
        Column("ItemID", "S", (STORED_ORB, ITEMS[0])), Column("Count", "I", (95, 101)))
    assert stored.service.exchange(envelope, case.store) == response and case.store.profile() == saved


def test_invalid_possession_state_never_becomes_zero_balance(case):
    change(case, lambda state: state["tables"]["possessions"][1].update(Count=True))
    fail(case, message(case), "possession_state_invalid")


def test_corrupt_receipt_xml_rejected(case):
    envelope = message(case)
    case.service.exchange(envelope, case.store)
    profile = case.store.profile()
    state = deepcopy(profile.state)
    receipt = state["orb_exchange_history"]["receipts"][0]
    receipt.update(response="<broken>", revision=profile.revision + 1)
    case.store.update_state(state, profile.revision)
    fail(case, envelope, "orb_exchange_history_invalid")
