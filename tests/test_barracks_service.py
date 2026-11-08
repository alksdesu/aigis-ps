from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import logging
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.barracks_service import EXTRA_SHIFT, PRINCE_RULE_COMMAND, PURCHASE_FIELD, BarracksService
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import BARRACKS_BITS, BARRACKS_SHIFT, UNIT_BOOLEANS, UNIT_FIELDS, barracks_index
from aigis_ps.request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER
from aigis_ps.storage import Store
from aigis_ps.system_literals import rating_command_id
from test_image_aliases import command_table, encode
from test_quest_service import bundle, request


@pytest.fixture(scope="module")
def service(bundle):
    return BarracksService(bundle.bootstrap, bundle.player_state, bundle.unit_capacity,
                           lambda name: bundle.native_cdn.assets.read(name)[0])


@pytest.fixture
def case(bundle, service, tmp_path):
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state, added = bundle.player_state.credit_units(state, [{"card_id": 7, "level": 1, "count": 11}])
    state["tables"]["game_param"][0].update(Rank=25,
        Experience=sum(row["NextExp"] for row in bundle.bootstrap.rank_rows[:24]))
    state["tables"]["crystals"] = [
        {"FreeFlag": False, "Num": 50, "Date": 700000, "source": "paid-old"},
        {"FreeFlag": True, "Num": 15, "Date": 700002, "source": "free-new"},
        {"FreeFlag": False, "Num": 20, "Date": 700003, "source": "paid-new"},
        {"FreeFlag": True, "Num": 5, "Date": 700001, "source": "free-old"},
    ]
    return SimpleNamespace(bundle=bundle, service=service, store=Store(tmp_path / "barracks.sqlite3", state),
                           added=added, path=tmp_path / "barracks.sqlite3")


def mutate(case, action):
    before = case.store.profile()
    state = deepcopy(before.state)
    action(state)
    case.store.update_state(state, before.revision)


def overwrite(case, action):
    state = case.store.profile().state
    action(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def unlock(case, count=2, low=35):
    mutate(case, lambda state: state["tables"]["config"][0].update({PURCHASE_FIELD: (count << EXTRA_SHIFT) | low}))


def move_saved(case, units, destination, other=0):
    indices = {row["GetIndex"] for row in units}
    mutate(case, lambda state: [row.update(GeneralPurposeFlags=other | (destination << BARRACKS_SHIFT))
        for row in state["player_state"]["units"] if row["GetIndex"] in indices])


def message(case, destination=1, units=None, *, tick=1000, columns=None, number=118):
    selected = case.added[:1] if units is None else units
    if columns is None:
        columns = () if number == 17 else (
            Column("DEST", "I", (destination,)), Container("BASE", (
                Column("A7", "I", tuple(row["GetIndex"] for row in selected)),
                Column("AB", "I", tuple(row["UniqueID"] for row in selected)))))
    return request(case.store, number, columns, tick=tick)


def execute(case, envelope):
    return case.service.handlers()[envelope.profile.registration_keys[0]](envelope, case.store)


def fails(case, envelope, code):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        execute(case, envelope)
    assert caught.value.code == code
    assert case.store.profile() == before


def nodes(xml):
    return {node.name: node for node in loads(xml).children}


def test_verified_status_and_handlers(case):
    assert set(case.service.handlers()) == {17, 118}
    status = case.service.status()
    assert status["ready"] and status["crystal_price"] == 30
    assert status["first_purchase_rank"] == 25 and status["maximum_extra_barracks"] == 2
    assert status["directions"] == [[0, 1], [0, 2], [1, 0], [2, 0]]
    assert status["history_limit"] == HISTORY_LIMIT and status["processed_request_limit"] == PROCESSED_LIMIT
    assert status["request_order"] == REQUEST_ORDER and "same_second_limit" not in status
    assert status["refusals"] == ("Logged, because the native client shows a communication error and resends the "
                                  "refused request until it is accepted; 118 has already moved its rows locally")


def test_purchase_free_then_paid_oldest_and_complete_wallet_snapshots(case):
    unlock(case, 0, 65535)
    before = case.store.profile()
    reply = nodes(execute(case, message(case, number=17)))
    after = case.store.profile()
    assert set(reply) == {"SYSTEM", "PPC", "PFC", "PGC", "PGP2"}
    assert after.state["tables"]["config"][0][PURCHASE_FIELD] == 131071
    assert [row["Num"] for row in after.state["tables"]["crystals"]] == [40, 0, 20, 0]
    assert [row["source"] for row in after.state["tables"]["crystals"]] == [
        row["source"] for row in before.state["tables"]["crystals"]]
    assert reply["PGC"].children == (Column("A1", "S", ("A5",)), Column("A2", "S", ("131071",)))
    assert reply["PFC"].children == (Column("UserID", "I", (int(after.player_id),) * 2),
        Column("Date", "I", (700002, 700001)), Column("Num", "I", (0, 0)))
    assert reply["PPC"].children[-1] == Column("Num", "I", (40, 20))
    expected = deepcopy(before.state)
    expected["tables"]["config"][0][PURCHASE_FIELD] = 131071
    expected["tables"]["game_param2"][0]["StampCardGrade"] = 30
    expected.pop("crystal_spending", None)
    for row, number in zip(expected["tables"]["crystals"], (40, 0, 20, 0)):
        row["Num"] = number
    assert {key: value for key, value in after.state.items() if key not in ("barracks_history", "crystal_spending")} == expected


def test_purchase_second_and_limit(case):
    execute(case, message(case, number=17))
    execute(case, message(case, number=17, tick=1001))
    assert case.store.profile().state["tables"]["config"][0][PURCHASE_FIELD] == 2 << EXTRA_SHIFT
    fails(case, message(case, number=17, tick=1002), "barracks_purchase_limit")


@pytest.mark.parametrize("rank,extra,success", [(24, 0, False), (25, 0, True), (1, 1, True)])
def test_first_purchase_only_rank_gate(case, rank, extra, success):
    unlock(case, extra, 0)
    mutate(case, lambda state: state["tables"]["game_param"][0].update(Rank=rank,
        Experience=sum(row["NextExp"] for row in case.bundle.bootstrap.rank_rows[:rank - 1])))
    if success:
        execute(case, message(case, number=17))
    else:
        fails(case, message(case, number=17), "barracks_rank_required")


def test_insufficient_or_invalid_wallet_never_unlocks(case):
    mutate(case, lambda state: state["tables"].update(crystals=[{"FreeFlag": True, "Num": 29, "Date": 1}]))
    fails(case, message(case, number=17), "crystal_insufficient")
    mutate(case, lambda state: state["tables"]["crystals"][0].update(Num=True))
    fails(case, message(case, number=17), "invalid_crystal_state")


def test_barracks_bits_are_the_native_storage_nibble():
    assert BARRACKS_BITS == 0xF0
    assert barracks_index({"GeneralPurposeFlags": BARRACKS_BITS}) == 15
    assert barracks_index({"GeneralPurposeFlags": ~BARRACKS_BITS}) == 0


@pytest.mark.parametrize("source,destination", [(0, 1), (0, 2), (1, 0), (2, 0)])
def test_four_directions_preserve_every_other_field_and_state(case, source, destination):
    unlock(case)
    selected = case.added[:10]
    flags = -0x80000000 | 0x705
    move_saved(case, selected, source, flags)
    before = case.store.profile()
    reply = nodes(execute(case, message(case, destination, selected)))
    after = case.store.profile()
    expected = deepcopy(before.state)
    indices = {unit["GetIndex"] for unit in selected}
    for unit in expected["player_state"]["units"]:
        if unit["GetIndex"] in indices:
            unit["GeneralPurposeFlags"] = (unit["GeneralPurposeFlags"] & ~BARRACKS_BITS) | destination << BARRACKS_SHIFT
    assert {key: value for key, value in after.state.items() if key != "barracks_history"} == expected
    assert set(reply) == {"SYSTEM", "PPU"}
    updated = {row["GetIndex"]: row for row in after.state["player_state"]["units"]}
    rows = () if destination else selected
    assert reply["PPU"].children == tuple(Column(alias, "B" if name in UNIT_BOOLEANS else "I",
        tuple(updated[row["GetIndex"]][name] for row in rows)) for name, alias in UNIT_FIELDS)


def test_noop_keeps_all_values_and_receipt_prevents_later_replay(case):
    unlock(case)
    move_saved(case, case.added[:1], 1)
    before = case.store.profile()
    envelope = message(case)
    first = execute(case, envelope)
    committed = case.store.profile()
    assert {key: value for key, value in committed.state.items() if key != "barracks_history"} == before.state
    assert execute(case, envelope) == first and case.store.profile() == committed
    move_saved(case, case.added[:1], 0)
    fails(case, envelope, "barracks_retry_stale")


@pytest.mark.parametrize("source,destination", [(1, 2), (2, 1)])
def test_no_direct_extra_to_extra(case, source, destination):
    unlock(case)
    move_saved(case, case.added[:1], source)
    fails(case, message(case, destination), "barracks_direction_unsupported")


def test_locked_destination_and_mixed_sources(case):
    fails(case, message(case), "barracks_locked")
    unlock(case)
    move_saved(case, case.added[:1], 1)
    fails(case, message(case, 0, case.added[:2]), "barracks_mixed_sources")


def test_prince_and_all_squad_groups_block_leaving(case):
    unlock(case)
    prince = next(row for row in case.store.profile().state["player_state"]["units"] if row["CardID"] == 1)
    fails(case, message(case, units=[prince]), "barracks_prince_protected")
    mutate(case, lambda state: state["player_state"]["squads"].append(
        {"Address": 0x03020001, "UnitIndex": case.added[0]["GetIndex"], "Flags": 0, "Name": "other group"}))
    fails(case, message(case), "barracks_organized")


def test_organized_legacy_extra_unit_can_return_without_squad_changes(case):
    unlock(case)
    move_saved(case, case.added[:1], 1)
    mutate(case, lambda state: state["player_state"]["squads"].append(
        {"Address": 1, "UnitIndex": case.added[0]["GetIndex"], "Flags": 2, "Name": "legacy"}))
    squads = case.store.profile().state["player_state"]["squads"]
    execute(case, message(case, 0))
    assert case.store.profile().state["player_state"]["squads"] == squads


def test_exact_id_pairs_and_missing_units(case):
    unlock(case)
    fake = dict(case.added[0], UniqueID=case.added[1]["UniqueID"])
    fails(case, message(case, units=[fake]), "barracks_unit_missing")
    fake = dict(case.added[0], GetIndex=99999)
    fails(case, message(case, units=[fake]), "barracks_unit_missing")


@pytest.mark.parametrize("columns", [(), (Column("DEST", "I", (1,)),),
    (Column("DEST", "I", (3,)), Container("BASE")),
    (Column("DEST", "B", (True,)), Container("BASE")),
    (Column("DEST", "I", (1, 2)), Container("BASE")),
    (Column("DEST", "I", (1,)), Container("BASE")),
    (Column("DEST", "I", (1,)), Container("BASE", (Column("A7", "I", (1,)), Column("AB", "S", ("1",))))),
    (Column("DEST", "I", (1,)), Container("BASE", (Column("A7", "I", (1,)), Column("AB", "I", (1,)), Column("DEST", "I", (1,))))),
    (Column("DEST", "I", (1,)), Container("BASE", (Column("A7", "I", (1, 1)), Column("AB", "I", (1, 2))))),
    (Column("DEST", "I", (1,)), Container("BASE", (Column("A7", "I", (1, 2)), Column("AB", "I", (1, 1))))),
    (Column("DEST", "I", (1,)), Container("BASE", (Column("A7", "I", (-1,)), Column("AB", "I", (1,))))),
    (Column("DEST", "I", (1,)), Container("BASE", (Column("A7", "I", (1,)), Column("AB", "I", (0,))))),
    (Column("DEST", "I", (1,)), Container("BASE", (Column("A7", "I", (1, 2)), Column("AB", "I", (1,))))),
])
def test_transfer_strict_shape(case, columns):
    fails(case, message(case, columns=columns), "invalid_request_fields")


def test_maximum_ten_and_no_purchase_parameters(case):
    fails(case, message(case, units=case.added), "invalid_request_fields")
    for columns in ((Column("PRICE", "I", (0,)),), (Column("DEST", "I", (1,)),), (Container("BASE"),)):
        fails(case, message(case, number=17, columns=columns), "invalid_request_fields")


def limited(case, destination, cap):
    original = case.bundle.unit_capacity

    def usage(state):
        current = original.usage(state)
        capacities = list(current.capacities)
        if destination < len(capacities):
            capacities[destination] = cap
        return replace(current, capacities=tuple(capacities))

    capacity = SimpleNamespace(content_version=original.content_version, usage=usage)
    case.service = BarracksService(case.bundle.bootstrap, case.bundle.player_state, capacity,
                                   lambda name: case.bundle.native_cdn.assets.read(name)[0])


def test_last_slot_is_allowed_next_entire_batch_is_rejected(case):
    unlock(case)
    limited(case, 1, 1)
    execute(case, message(case))
    fails(case, message(case, units=case.added[1:3], tick=1001), "barracks_capacity_exceeded")


def test_overfull_source_can_be_reduced(case):
    unlock(case)
    limited(case, 0, 1)
    execute(case, message(case))
    assert barracks_index(case.store.profile().state["player_state"]["units"][-11]) == 1


@pytest.mark.parametrize("operation", [17, 118])
def test_exact_retry_survives_reopen_and_never_reapplies(case, operation):
    if operation == 118:
        unlock(case)
    envelope = message(case, number=operation)
    response = execute(case, envelope)
    committed = case.store.profile()
    case.store = Store(case.path)
    case.service = BarracksService(case.bundle.bootstrap, case.bundle.player_state, case.bundle.unit_capacity,
                                   lambda name: case.bundle.native_cdn.assets.read(name)[0])
    assert execute(case, envelope) == response
    assert case.store.profile() == committed


def test_replay_after_unrelated_revision_is_stale(case):
    envelope = message(case, number=17)
    execute(case, envelope)
    mutate(case, lambda state: state.update(other_progress={"retained": True}))
    fails(case, envelope, "barracks_retry_stale")


def barracks_of(case, unit):
    row = next(row for row in case.store.profile().state["player_state"]["units"] if row["GetIndex"] == unit["GetIndex"])
    return barracks_index(row)


def test_client_time_is_not_an_order_across_the_signed_boundary(case):
    unlock(case)
    first = message(case, tick=0x7FFFFFFF)
    execute(case, first)
    execute(case, message(case, 0, tick=-0x80000000))
    execute(case, message(case, tick=0x7FFFFFFE))
    assert barracks_of(case, case.added[0]) == 1
    fails(case, first, "barracks_retry_stale")


def test_daily_boundary_and_clock_rollback_keep_only_exact_replay_protection(case):
    unlock(case)
    envelope = message(case, tick=86399)
    execute(case, envelope)
    execute(case, message(case, 0, tick=86400))
    fails(case, envelope, "barracks_retry_stale")
    execute(case, message(case, tick=86398))
    assert barracks_of(case, case.added[0]) == 1
    history = case.store.profile().state["barracks_history"]
    assert set(history) == {"schema_version", "receipts", "processed_keys"}
    assert all("client_time" not in receipt for receipt in history["receipts"])


def test_history_eviction_still_rejects_old_request(case):
    unlock(case)
    first = message(case, tick=1000)
    for index in range(HISTORY_LIMIT + 1):
        execute(case, message(case, 1 if index % 2 == 0 else 0, tick=1000 + index))
    history = case.store.profile().state["barracks_history"]
    assert len(history["receipts"]) == HISTORY_LIMIT and len(history["processed_keys"]) == HISTORY_LIMIT + 1
    fails(case, first, "barracks_request_stale")


def test_saturated_legacy_second_no_longer_blocks_new_requests(case):
    seen = [hashlib.sha256(str(index).encode()).hexdigest() for index in range(PROCESSED_LIMIT)]
    mutate(case, lambda state: state.update(barracks_history={"schema_version": 1, "receipts": [],
        "last_client_time": 1000, "seen_keys": seen}))
    execute(case, message(case, number=17))
    history = case.store.profile().state["barracks_history"]
    assert history["schema_version"] == 2 and len(history["processed_keys"]) == PROCESSED_LIMIT
    assert history["processed_keys"][:-1] == seen[1:]


@pytest.mark.parametrize("operation", [17, 118])
def test_legacy_watermark_receipt_upgrades_and_replays(case, operation):
    if operation == 118:
        unlock(case)
    envelope = message(case, number=operation, tick=2000)
    response = execute(case, envelope)
    def legacy(state):
        receipt, = state["barracks_history"]["receipts"]
        state["barracks_history"] = {"schema_version": 1, "last_client_time": 2000, "seen_keys": [receipt["request"]],
                                     "receipts": [{**receipt, "client_time": 2000}]}
    overwrite(case, legacy)
    before = case.store.profile()
    assert execute(case, envelope) == response
    assert case.store.profile() == before
    execute(case, message(case, 0 if operation == 118 else 1, number=operation, tick=1000))
    history = case.store.profile().state["barracks_history"]
    assert history["schema_version"] == 2 and len(history["processed_keys"]) == 2


def test_refused_transfer_is_logged_because_the_client_already_moved_the_rows(case, caplog):
    unlock(case)
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    fails(case, message(case, units=[dict(case.added[0], GetIndex=99999)]), "barracks_unit_missing")
    assert [record.getMessage() for record in caplog.records] == [
        "Barracks transfer 118 refused with barracks_unit_missing; the native client already moved the rows "
        "locally, shows a communication error and resends the same request until the server accepts it"]
    caplog.clear()
    fails(case, message(case, number=17, columns=(Column("PRICE", "I", (0,)),)), "invalid_request_fields")
    execute(case, message(case))
    assert caplog.records == []


def test_refused_purchase_is_logged_because_the_client_resends_it(case, caplog):
    unlock(case)
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    fails(case, message(case, number=17), "barracks_purchase_limit")
    assert [record.getMessage() for record in caplog.records] == [
        "Barracks purchase 17 refused with barracks_purchase_limit; the native client shows a communication error "
        "and resends the same request until the server accepts it"]


@pytest.mark.parametrize("history", [None, {}, {"schema_version": True},
    {"schema_version": 1, "receipts": [{}], "last_client_time": 1, "seen_keys": []},
    {"schema_version": 1, "receipts": [], "last_client_time": None, "seen_keys": ["0" * 64]},
    {"schema_version": 2, "receipts": [], "processed_keys": ["not-a-digest"]},
    {"schema_version": 2, "receipts": [], "processed_keys": [], "last_client_time": 1}])
def test_invalid_history_never_spends(case, history):
    mutate(case, lambda state: state.update(barracks_history=history))
    fails(case, message(case, number=17), "barracks_history_invalid")


@pytest.mark.parametrize("operation", [17, 118])
def test_parallel_identical_requests_commit_once(case, operation):
    if operation == 118:
        unlock(case)
    envelope = message(case, number=operation)
    revision = case.store.profile().revision
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: execute(case, envelope), range(2)))
    assert replies[0] == replies[1]
    assert case.store.profile().revision == revision + 1


def test_cas_retries_preserve_concurrent_progress(case, monkeypatch):
    real = case.store.update_state
    calls = []

    def conflict_once(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            current = case.store.profile()
            concurrent = deepcopy(current.state)
            concurrent["progress_after_snapshot"] = [1, 2, 3]
            real(concurrent, current.revision)
            raise ValueError("changed")
        return real(state, revision)

    monkeypatch.setattr(case.store, "update_state", conflict_once)
    execute(case, message(case, number=17))
    assert len(calls) == 2
    assert case.store.profile().state["progress_after_snapshot"] == [1, 2, 3]
    assert [row["Num"] for row in case.store.profile().state["tables"]["crystals"]] == [40, 0, 20, 0]


def test_three_conflicts_leave_input_unchanged(case, monkeypatch):
    calls = []

    def conflict(state, revision):
        calls.append(revision)
        raise ValueError("changed")

    monkeypatch.setattr(case.store, "update_state", conflict)
    fails(case, message(case, number=17), "profile_changed")
    assert len(calls) == 3


@pytest.mark.parametrize("operation,rejected", [(17, 17), (17, 36), (17, 41), (118, 118), (118, 41)])
def test_all_output_preflights_precede_commit(case, monkeypatch, operation, rejected):
    import aigis_ps.barracks_service as module

    unlock(case, 1)
    original = module.encode_response

    def oversized(profile, xml):
        if profile.registration_keys == (rejected,):
            raise ProtocolError("fixture_capacity", "test capacity", 503)
        return original(profile, xml)

    monkeypatch.setattr(module, "encode_response", oversized)
    fails(case, message(case, number=operation), "fixture_capacity")


def test_receipt_bound_precedes_commit(case, monkeypatch):
    monkeypatch.setattr("aigis_ps.barracks_service.RESPONSE_LIMIT", 1)
    fails(case, message(case, number=17), "barracks_response_capacity")


@pytest.mark.parametrize("field", ["UI", "UK", "MK"])
def test_authentication_is_required(case, field):
    envelope = message(case, number=17)
    envelope.document.find(f"{field}/V").text = "other"
    fails(case, envelope, "invalid_method_key" if field == "MK" else "invalid_identity")


@pytest.mark.parametrize("resource", ["OnCommandMenuUnitHouse.atb", "OnCommandMenuShop.atb"])
def test_changed_menu_resources_fail_closed(case, resource):
    with pytest.raises(ProtocolError) as caught:
        BarracksService(case.bundle.bootstrap, case.bundle.player_state, case.bundle.unit_capacity,
            lambda name: b"changed" if name == resource else case.bundle.native_cdn.assets.read(name)[0])
    assert caught.value.code == "barracks_catalog_invalid"


def test_nonempty_rating_command_blocks_only_affected_card(case):
    key = rating_command_id(7)
    raw = command_table([(key, encode("1;"))])
    case.service = BarracksService(case.bundle.bootstrap, case.bundle.player_state, case.bundle.unit_capacity,
        lambda name: raw if name == "OnCommand.atb" else case.bundle.native_cdn.assets.read(name)[0])
    unlock(case)
    fails(case, message(case), "barracks_rating_unsupported")


def test_replay_validates_current_roster_and_dependencies(case, monkeypatch):
    envelope = message(case, number=17)
    execute(case, envelope)
    original = case.service.capacity
    case.service.capacity = SimpleNamespace(content_version="f" * 64)
    fails(case, envelope, "barracks_catalog_changed")
    case.service.capacity = original

    def unavailable(state, **_):
        raise ProtocolError("fixture_roster", "unavailable", 503)

    monkeypatch.setattr(case.bundle.player_state, "validate", unavailable)
    fails(case, envelope, "fixture_roster")


def test_transfer_preflights_reserved_rewards_without_granting_or_business_cap(case, monkeypatch):
    unlock(case)
    limited(case, 0, 1)
    reservation = [{"card_id": 7, "level": 1, "count": 2}]
    mutate(case, lambda state: state.update(quest_state={"active": {
        "unit_reservations": reservation, "other_frozen_data": [1, 2]}}))
    before = case.store.profile().state
    original = case.bundle.player_state.credit_units
    calls = []

    def trial(state, grants):
        assert state["quest_state"]["active"] is None
        assert grants == reservation
        calls.append(deepcopy(state))
        return original(state, grants)

    monkeypatch.setattr(case.bundle.player_state, "credit_units", trial)
    execute(case, message(case))
    after = case.store.profile().state
    assert len(calls) == 1
    assert after["quest_state"] == before["quest_state"]
    assert len(after["player_state"]["units"]) == len(before["player_state"]["units"])
    assert after["player_state"]["battle"] == before["player_state"]["battle"]
    assert after["player_state"]["last_unique_id"] == before["player_state"]["last_unique_id"]


def test_reserved_response_capacity_failure_rolls_back_transfer(case, monkeypatch):
    unlock(case)
    mutate(case, lambda state: state.update(quest_state={"active": {
        "unit_reservations": [{"card_id": 7, "level": 1, "count": 2}]}}))

    def oversized(state, grants):
        assert state["quest_state"]["active"] is None
        raise ProtocolError("unit_roster_capacity", "Reserved response is oversized", 409)

    monkeypatch.setattr(case.bundle.player_state, "credit_units", oversized)
    fails(case, message(case), "unit_roster_capacity")


def test_unsupported_prince_extension_does_not_reclassify_units_silently(case):
    raw = command_table([(PRINCE_RULE_COMMAND, encode("1;"))])
    case.service = BarracksService(case.bundle.bootstrap, case.bundle.player_state, case.bundle.unit_capacity,
        lambda name: raw if name == "OnCommand.atb" else case.bundle.native_cdn.assets.read(name)[0])
    unlock(case)
    fails(case, message(case), "barracks_prince_rule_unsupported")
