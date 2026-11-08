from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import logging
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.errors import ProtocolError
from aigis_ps.fairy_farm import FairyFarmService
from aigis_ps.player_state import UNIT_BOOLEANS, UNIT_FIELDS
from aigis_ps.storage import Store
from test_image_aliases import command_table, encode
from test_quest_service import bundle, request


@pytest.fixture(scope="module")
def farm(bundle):
    return FairyFarmService(bundle.bootstrap, bundle.player_state, bundle.gifts,
                            lambda name: bundle.native_cdn.assets.read(name)[0])


@pytest.fixture(scope="module")
def barracks_farm(bundle):
    return FairyFarmService(bundle.bootstrap, bundle.player_state, bundle.gifts,
                            lambda name: bundle.native_cdn.assets.read(name)[0], barracks=bundle.unit_capacity)


@pytest.fixture
def case(bundle, farm, tmp_path):
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state, units = bundle.player_state.credit_units(state, [{"card_id": farm.card_ids[0], "level": 1, "count": 2}])
    return SimpleNamespace(bundle=bundle, farm=farm, store=Store(tmp_path / "warehouse.sqlite3", state), units=units)


def mutate(case, change):
    current = case.store.profile()
    state = deepcopy(current.state)
    change(state)
    case.store.update_state(state, current.revision)


def overwrite(case, change):
    state = case.store.profile().state
    change(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def seed(case, count, identifier=None):
    identifier = identifier or case.farm.card_ids[0]
    mutate(case, lambda state: (state.update(fairy_farm_schema=1), state["tables"].update(
        fairy_farm=[{"CardID": identifier, "Count": count}])))


def deposit(case, units=None, tick=1000):
    units = case.units if units is None else units
    return request(case.store, 123, (Container("LST", (
        Column("A7", "I", tuple(unit["GetIndex"] for unit in units)),
        Column("AB", "I", tuple(unit["UniqueID"] for unit in units)))),), tick=tick)


def withdraw(case, count=1, tick=1000, identifier=None):
    return request(case.store, 125, (Container("Draw", (
        Column("CardID", "I", (identifier or case.farm.card_ids[0],)), Column("Count", "I", (count,)))),), tick=tick)


def stock(case, identifier=None):
    return next(row["Count"] for row in case.farm.read(case.store.profile().state)
                if row["CardID"] == (identifier or case.farm.card_ids[0]))


def fails(case, message, code, action=None):
    before = case.store.profile()
    handler = action or case.farm.handlers()[message.profile.registration_keys[0]]
    with pytest.raises(ProtocolError) as caught:
        handler(message, case.store)
    assert caught.value.code == code
    assert case.store.profile() == before


def test_real_definitions_and_native_virtual_templates_match_grants(case):
    assert len(case.farm.card_ids) == 54
    assert case.farm.cap == 600
    assert set(case.farm.handlers()) == {122, 123, 124, 125}
    for identifier in case.farm.card_ids:
        virtual = case.farm.material(identifier)
        assert virtual["UniqueID"] == virtual["GetIndex"] == 0
        state, granted = case.bundle.player_state.credit_units(case.store.profile().state,
            [{"card_id": identifier, "level": 1, "count": 1}])
        expected = {**granted[0], "UniqueID": 0, "GetIndex": 0}
        assert virtual == expected
        assert set(virtual) == {name for name, _ in UNIT_FIELDS}
        assert all(type(virtual[name]) is bool for name in UNIT_BOOLEANS)


def test_empty_legacy_read_is_complete_without_writing(case):
    before = case.store.profile()
    rows = case.farm.read(before.state)
    assert len(rows) == 54 and all(row["Count"] == 0 for row in rows)
    rows[0]["Count"] = 90
    assert case.farm.read(before.state)[0]["Count"] == 0
    response = case.farm.load(request(case.store, 122, ()), case.store)
    data = {child.name: child for child in loads(response).children}
    assert set(data) == {"SYSTEM", "PFF"}
    assert len(data["PFF"].children[0].values) == 54
    assert case.store.profile() == before


@pytest.mark.parametrize("change", [
    lambda state: state.update(fairy_farm_schema=True),
    lambda state: state.update(fairy_farm_schema=2),
    lambda state: state["tables"].update(fairy_farm={}),
    lambda state: state["tables"].update(fairy_farm=[{"CardID": 54, "Count": 1}]),
])
def test_unknown_legacy_schema_preserves_data(case, change):
    state = deepcopy(case.store.profile().state)
    change(state)
    original = deepcopy(state)
    with pytest.raises(ProtocolError, match="schema"):
        case.farm.read(state)
    assert state == original


@pytest.mark.parametrize("rows", [
    [{"CardID": 54, "Count": True}], [{"CardID": True, "Count": 1}],
    [{"CardID": 54, "Count": -1}], [{"CardID": 54, "Count": 601}],
    [{"CardID": 1, "Count": 0}], [{"CardID": 54, "Count": 1, "Extra": 0}],
    [{"CardID": 54, "Count": 0}, {"CardID": 54, "Count": 1}],
])
def test_invalid_inventory_rows_are_not_normalized_away(case, rows):
    state = deepcopy(case.store.profile().state)
    state["fairy_farm_schema"] = 1
    state["tables"]["fairy_farm"] = rows
    with pytest.raises(ProtocolError):
        case.farm.read(state)


def test_sparse_rows_and_selected_zero_counts_are_explicit(case):
    seed(case, 2)
    state = case.store.profile().state
    selected = case.farm.response(state, list(case.farm.card_ids[:2]))
    assert selected.children[1].values == (2, 0)
    assert len(case.farm.read(state)) == 54
    assert case.farm.response(state, []).children[0].values == ()


@pytest.mark.parametrize("selected", [[1], [True], [54, 54], "54", [2 ** 40]])
def test_bad_response_selection_is_rejected(case, selected):
    with pytest.raises(ProtocolError):
        case.farm.response(case.store.profile().state, selected)


def test_debit_is_pure_and_empty_debit_does_not_create_schema(case):
    before = case.store.profile().state
    empty = case.farm.debit(before, {})
    assert empty == before and empty is not before
    seed(case, 2)
    before = case.store.profile().state
    updated = case.farm.debit(before, {54: 2})
    assert updated["tables"]["fairy_farm"][0]["Count"] == 0
    assert before["tables"]["fairy_farm"][0]["Count"] == 2
    assert "fairy_farm_history" not in updated


@pytest.mark.parametrize("costs", [{54: 0}, {54: -1}, {54: True}, {True: 1}, {1: 1}, {54: 2 ** 40}, []])
def test_debit_rejects_invalid_inputs(case, costs):
    with pytest.raises(ProtocolError):
        case.farm.debit(case.store.profile().state, costs)


def test_deposit_and_withdraw_are_atomic_and_preserve_id_highwater(case):
    before = case.store.profile()
    sent = case.farm.deposit(deposit(case), case.store)
    after = case.store.profile()
    assert stock(case) == 2
    assert [unit["CardID"] for unit in after.state["player_state"]["units"]] == [1]
    assert after.state["player_state"]["last_unique_id"] == 3
    assert after.state["player_state"]["battle"] == before.state["player_state"]["battle"]
    assert set(child.name for child in loads(sent).children) == {"SYSTEM", "PFF"}
    drawn = case.farm.withdraw(withdraw(case, count=2, tick=1001), case.store)
    data = {child.name: child for child in loads(drawn).children}
    assert set(data) == {"SYSTEM", "PBP", "PPU", "PFF"}
    assert data["PBP"].children[1].values == (4,)
    assert stock(case) == 0
    units = case.store.profile().state["player_state"]["units"][1:]
    assert [unit["UniqueID"] for unit in units] == [4, 5]
    assert {column.name for column in data["PPU"].children} == {alias for _, alias in UNIT_FIELDS}
    assert after.state["tables"]["game_param"] == before.state["tables"]["game_param"]


def test_deposit_protects_organized_units(case):
    mutate(case, lambda state: state["player_state"]["squads"].append(
        {"Address": 0, "UnitIndex": case.units[0]["GetIndex"], "Name": "", "Flags": 0}))
    fails(case, deposit(case), "fairy_farm_unit_protected")


@pytest.mark.parametrize("field,value", [("GeneralPurposeFlags", 3), ("Exp", 1), ("SkillLevel", 2), ("Love", 1),
                                         ("GeneralPurposeFlags", 5)])
def test_selected_deposit_accepts_locked_or_grown_fairies_like_the_native_selection(case, field, value):
    mutate(case, lambda state: state["player_state"]["units"][1].update({field: value}))
    before = case.store.profile()
    case.farm.deposit(deposit(case), case.store)
    after = case.store.profile().state
    assert stock(case) == 2
    assert after["player_state"]["units"] == before.state["player_state"]["units"][:1]
    assert after["player_state"]["last_unique_id"] == 3


def test_capacity_and_missing_stock_fail_without_any_write(case):
    seed(case, 599)
    fails(case, deposit(case), "fairy_farm_quantity")
    seed(case, 0)
    fails(case, withdraw(case), "fairy_farm_quantity")


def test_mismatched_identity_and_duplicate_request_rows_fail(case):
    units = [{**case.units[0], "UniqueID": 999}]
    fails(case, deposit(case, units), "fairy_farm_unit_missing")
    fails(case, deposit(case, case.units * 2), "invalid_request_fields")


def test_receipts_survive_restart_and_no_exact_retry_reapplies(case):
    message = deposit(case)
    response = case.farm.deposit(message, case.store)
    saved = case.store.profile()
    reopened = Store(case.store.path)
    new = FairyFarmService(case.bundle.bootstrap, case.bundle.player_state, case.bundle.gifts,
                          lambda name: case.bundle.native_cdn.assets.read(name)[0])
    assert new.deposit(message, reopened) == response
    assert reopened.profile() == saved
    case.farm.withdraw(withdraw(case, tick=1001), case.store)
    fails(case, message, "fairy_farm_retry_stale")


def test_new_seconds_allow_repeated_withdrawals_and_old_receipt_eviction_cannot_reapply(case):
    seed(case, 600)
    oldest = withdraw(case, count=1, tick=1000)
    case.farm.withdraw(oldest, case.store)
    for tick in range(1001, 1018):
        case.farm.withdraw(withdraw(case, count=1, tick=tick), case.store)
    assert stock(case) == 582
    assert len(case.store.profile().state["fairy_farm_history"]["receipts"]) == 16
    fails(case, oldest, "fairy_farm_request_stale")


def test_evicted_body_remains_processed_inside_the_digest_window(case):
    seed(case, 600)
    oldest = withdraw(case, count=1)
    for count in range(1, 18):
        case.farm.withdraw(withdraw(case, count=count), case.store)
    history = case.store.profile().state["fairy_farm_history"]
    assert len(history["receipts"]) == 16 and len(history["processed_keys"]) == 17
    fails(case, oldest, "fairy_farm_request_stale")


def test_clock_rollback_accepts_new_bodies_for_every_operation(case):
    seed(case, 5)
    case.farm.withdraw(withdraw(case, tick=2000), case.store)
    case.farm.deposit(deposit(case, tick=1500), case.store)
    case.farm.withdraw(withdraw(case, tick=1499), case.store)
    case.farm.withdraw(withdraw(case, tick=-0x80000000), case.store)
    assert stock(case) == 4
    _, data = bulk_result(case, bulk(case, tick=-1))
    assert data["ERC"].children[0].values == (0,)
    assert stock(case) == 7
    history = case.store.profile().state["fairy_farm_history"]
    assert set(history) == {"schema_version", "receipts", "processed_keys"}
    assert all("client_time" not in receipt for receipt in history["receipts"])


def test_legacy_watermark_history_upgrades_replays_and_accepts_older_time(case):
    seed(case, 5)
    message = withdraw(case, tick=2000)
    response = case.farm.withdraw(message, case.store)
    def legacy(state):
        receipt, = state["fairy_farm_history"]["receipts"]
        state["fairy_farm_history"] = {"schema_version": 1, "last_client_time": 2000, "seen_keys": [receipt["request"]],
                                       "receipts": [{**receipt, "client_time": 2000}]}
    overwrite(case, legacy)
    before = case.store.profile()
    assert case.farm.withdraw(message, case.store) == response
    assert case.store.profile() == before
    case.farm.withdraw(withdraw(case, tick=1000), case.store)
    history = case.store.profile().state["fairy_farm_history"]
    assert history["schema_version"] == 2 and len(history["processed_keys"]) == 2
    assert stock(case) == 3


def test_evicted_selected_deposit_replay_fails_on_identity(case, monkeypatch):
    import aigis_ps.request_ledger as ledger
    monkeypatch.setattr(ledger, "HISTORY_LIMIT", 1)
    monkeypatch.setattr(ledger, "PROCESSED_LIMIT", 1)
    seed(case, 5)
    first = deposit(case, case.units[:1])
    case.farm.deposit(first, case.store)
    case.farm.withdraw(withdraw(case, tick=1001), case.store)
    fails(case, first, "fairy_farm_unit_missing")
    assert stock(case) == 5


def test_refused_deposit_is_logged_because_the_native_client_resends_it(case, caplog):
    seed(case, 599)
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        fails(case, deposit(case), "fairy_farm_quantity")
    assert [record.getMessage() for record in caplog.records if record.name == "aigis_ps"] == [
        "Fairy warehouse 123 refused with fairy_farm_quantity; the native client shows a communication error "
        "over its cleared stock view and resends the same request until the server accepts it"]


def test_concurrent_duplicate_withdrawal_grants_once(case):
    seed(case, 5)
    message = withdraw(case, count=2)
    revision = case.store.profile().revision
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: case.farm.withdraw(message, case.store), range(2)))
    assert results[0] == results[1]
    assert stock(case) == 3 and case.store.profile().revision == revision + 1


def test_different_same_second_operations_both_commit(case):
    seed(case, 5)
    messages = [withdraw(case, count=count) for count in (1, 2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda message: case.farm.withdraw(message, case.store), messages))
    assert stock(case) == 2


def test_cas_conflict_rechecks_new_squad_membership(case, monkeypatch):
    original = case.store.update_state
    attempts = []
    def conflict(state, revision):
        attempts.append(revision)
        if len(attempts) == 1:
            current = case.store.profile()
            current.state["player_state"]["squads"].append(
                {"Address": 0, "UnitIndex": case.units[0]["GetIndex"], "Name": "", "Flags": 0})
            original(current.state, current.revision)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    with pytest.raises(ProtocolError) as caught:
        case.farm.deposit(deposit(case), case.store)
    assert caught.value.code == "fairy_farm_unit_protected"
    assert len(case.store.profile().state["player_state"]["units"]) == 3 and stock(case) == 0


@pytest.mark.parametrize("operation", ["deposit", "withdraw"])
def test_unit_response_preflight_failure_is_atomic(case, monkeypatch, operation):
    seed(case, 2)
    def reject(*args, **kwargs):
        raise ProtocolError("test_capacity", "capacity")
    monkeypatch.setattr(case.bundle.player_state, "unit_response", reject)
    message = deposit(case) if operation == "deposit" else withdraw(case)
    fails(case, message, "test_capacity")


def test_deposit_preserves_nonmaterial_trust_history_and_card_status(case):
    identifier = next(i for i in case.bundle.gifts.catalog.card_ids
                      if case.bundle.gifts.catalog.card(i)["BonusType3"])
    current = case.store.profile()
    state, _ = case.bundle.player_state.credit_units(current.state, [{"card_id": identifier, "level": 1, "count": 1}])
    state["player_state"]["units"][-1].update(Love=150, WatchLoveEv=3)
    state = case.bundle.card_status.update(state, [{"CardID": identifier, "A7": 3, "B0": 12345}])
    case.store.update_state(state, current.revision)
    case.farm.deposit(deposit(case), case.store)
    saved = case.store.profile().state
    assert saved["gift_state"]["claims"][str(identifier)] == [1, 100, 150]
    assert case.bundle.card_status.read(saved) == case.bundle.card_status.read(state)


@pytest.mark.parametrize("command,expected", [("", 99), ('SetSystemConfigParam("FairyFarmLeaveMax", 17);', 17)])
def test_capacity_uses_verified_literal_and_native_fallback(bundle, command, expected):
    def reader(name):
        if name == "OnSystemCommand.atb":
            return command_table([(1, encode(command))])
        return bundle.native_cdn.assets.read(name)[0]
    assert FairyFarmService(bundle.bootstrap, bundle.player_state, bundle.gifts, reader).cap == expected


@pytest.mark.parametrize("command", ['SetSystemConfigParam("FairyFarmLeaveMax", 0);',
    'SetSystemConfigParam("FairyFarmLeaveMax", 1.5);', 'if (true) SetSystemConfigParam("FairyFarmLeaveMax", 600);',
    'SetSystemConfigParam("FairyFarmLeaveMax", 1); SetSystemConfigParam("FairyFarmLeaveMax", 2);'])
def test_ambiguous_or_invalid_capacity_is_not_assumed(bundle, command):
    reader = lambda name: command_table([(1, encode(command))]) if name == "OnSystemCommand.atb" else bundle.native_cdn.assets.read(name)[0]
    with pytest.raises(ProtocolError, match="resources"):
        FairyFarmService(bundle.bootstrap, bundle.player_state, bundle.gifts, reader)


def test_boolean_growth_resource_is_not_accepted_as_level_one(bundle, monkeypatch):
    class_id = bundle.gifts.catalog.card(54)["InitClassID"]
    monkeypatch.setitem(bundle.player_state._classes, class_id, {**bundle.player_state._classes[class_id], "MaxLevel": True})
    with pytest.raises(ProtocolError, match="resources"):
        FairyFarmService(bundle.bootstrap, bundle.player_state, bundle.gifts,
                         lambda name: bundle.native_cdn.assets.read(name)[0])


def test_changed_content_rejects_saved_success(case, monkeypatch):
    message = deposit(case)
    case.farm.deposit(message, case.store)
    monkeypatch.setattr(case.farm, "content_version", "a" * 64)
    fails(case, message, "fairy_farm_retry_stale")


@pytest.mark.parametrize("operation", ["deposit", "withdraw"])
def test_receipt_committed_without_barracks_rules_replays_once_they_attach(case, barracks_farm, operation):
    seed(case, 2)
    message = deposit(case) if operation == "deposit" else withdraw(case)
    response = getattr(case.farm, operation)(message, case.store)
    committed = case.store.profile()
    reopened = Store(case.store.path)
    assert getattr(barracks_farm, operation)(message, reopened) == response
    assert reopened.profile() == committed


def test_forged_receipt_digest_stays_stale_after_barracks_rules_attach(case, barracks_farm):
    message = deposit(case)
    case.farm.deposit(message, case.store)
    overwrite(case, lambda state: state["fairy_farm_history"]["receipts"][-1].update(content="f" * 64))
    fails(case, message, "fairy_farm_retry_stale", barracks_farm.deposit)


def test_receipt_committed_with_barracks_rules_is_stale_once_they_detach(case, barracks_farm):
    message = deposit(case)
    barracks_farm.deposit(message, case.store)
    fails(case, message, "fairy_farm_retry_stale")


def test_replayable_versions_add_only_the_configuration_without_barracks_rules(farm, barracks_farm):
    assert farm.replayable_content_versions() == {farm.content_version}
    versions = barracks_farm.replayable_content_versions()
    assert len(versions) == 2 and versions == {barracks_farm.content_version, farm.content_version}


@pytest.mark.parametrize("extra", [(Column("Extra", "I", (0,)),), (Column("PFF", "I", (0,)),)])
def test_load_rejects_unrecognized_business_fields(case, extra):
    fails(case, request(case.store, 122, extra), "invalid_request_fields")


def test_wrong_account_cannot_read_warehouse(case):
    message = request(case.store, 122, ())
    message.document.find("UK/V").text = "wrong-account"
    fails(case, message, "invalid_identity")


def test_wrong_inventory_type_and_zero_withdrawal_fail(case):
    message = request(case.store, 125, (Container("Draw", (
        Column("CardID", "S", ("54",)), Column("Count", "I", (1,)))),))
    fails(case, message, "invalid_request_fields")
    fails(case, withdraw(case, count=0), "invalid_request_fields")


def bulk(case, index=None, tick=1000):
    columns = () if index is None else (Container("BRK", (Column("IDX", "I", (index,)),)),)
    return request(case.store, 124, columns, tick=tick)


def bulk_result(case, message=None):
    response = case.farm.bulk_deposit(message or bulk(case), case.store)
    data = {child.name: child for child in loads(response).children}
    return response, data


@pytest.mark.parametrize("index", [None, 0])
def test_bulk_deposit_removes_only_reported_units_and_returns_complete_stock(case, index):
    before = case.store.profile()
    _, data = bulk_result(case, bulk(case, index))
    assert set(data) == {"SYSTEM", "PFF", "RML", "ERC"}
    assert data["RML"].children[0].values == tuple(unit["GetIndex"] for unit in case.units)
    assert data["RML"].children[1].values == tuple(unit["UniqueID"] for unit in case.units)
    assert data["ERC"].children[0].values == (0,)
    assert len(data["PFF"].children[0].values) == 54 and stock(case) == 2
    after = case.store.profile()
    assert [unit["CardID"] for unit in after.state["player_state"]["units"]] == [1]
    assert after.state["player_state"]["battle"] == before.state["player_state"]["battle"]
    assert after.state["player_state"]["last_unique_id"] == 3
    assert after.state["tables"]["game_param"] == before.state["tables"]["game_param"]


@pytest.mark.parametrize("index,code", [(-1, "invalid_request_fields"), (1, "fairy_farm_barrack_unsupported"),
                                         (2, "fairy_farm_barrack_unsupported")])
def test_bulk_rejects_unmodeled_or_non_native_barrack_indices(case, index, code):
    fails(case, bulk(case, index), code)


def test_bulk_capacity_partially_accepts_in_stable_identity_order(case):
    seed(case, 599)
    mutate(case, lambda state: state["player_state"]["units"].reverse())
    _, data = bulk_result(case)
    assert data["ERC"].children[0].values == (1,)
    assert data["RML"].children[0].values == (case.units[0]["GetIndex"],)
    assert stock(case) == 600
    remaining = {unit["GetIndex"] for unit in case.store.profile().state["player_state"]["units"]}
    assert remaining == {0, case.units[1]["GetIndex"]}


def test_bulk_full_inventory_returns_no_eligible_code_and_empty_remove_list(case):
    seed(case, 600)
    before = case.store.profile()
    message = bulk(case)
    wire, data = bulk_result(case, message)
    assert data["ERC"].children[0].values == (-1,)
    assert data["RML"].children[0].values == data["RML"].children[1].values == ()
    saved = case.store.profile()
    assert saved.state["player_state"] == before.state["player_state"]
    assert stock(case) == 600
    assert case.farm.bulk_deposit(message, case.store) == wire
    assert case.store.profile() == saved


@pytest.mark.parametrize("protection", ["locked", "squad", "growth", "flags"])
def test_bulk_skips_protected_units_and_continues_other_eligible_units(case, protection):
    def change(state):
        unit = state["player_state"]["units"][1]
        if protection == "squad":
            state["player_state"]["squads"].append({"Address": 0, "UnitIndex": unit["GetIndex"], "Name": "", "Flags": 0})
        else:
            field, value = {"locked": ("GeneralPurposeFlags", 3), "growth": ("Exp", 1),
                            "flags": ("GeneralPurposeFlags", 5)}[protection]
            unit[field] = value
    mutate(case, change)
    before = case.store.profile()
    _, data = bulk_result(case)
    assert data["ERC"].children[0].values == (0,)
    assert data["RML"].children[0].values == (case.units[1]["GetIndex"],)
    assert stock(case) == 1
    assert case.store.profile().state["player_state"]["units"][1] == before.state["player_state"]["units"][1]


def test_bulk_mixed_kinds_fills_available_kind_and_leaves_full_kind(case):
    seed(case, 600)
    before = case.store.profile()
    state, extra = case.bundle.player_state.credit_units(before.state, [{"card_id": 55, "level": 1, "count": 2},
                                                                    {"card_id": 3, "level": 1, "count": 1}])
    case.store.update_state(state, before.revision)
    _, data = bulk_result(case)
    assert data["ERC"].children[0].values == (1,)
    assert data["RML"].children[0].values == tuple(unit["GetIndex"] for unit in extra[:2])
    assert stock(case, 54) == 600 and stock(case, 55) == 2
    assert extra[-1] in case.store.profile().state["player_state"]["units"]


def test_bulk_empty_receipt_cannot_retarget_units_added_after_it(case):
    mutate(case, lambda state: state["player_state"].update(units=state["player_state"]["units"][:1]))
    message = bulk(case)
    _, data = bulk_result(case, message)
    assert data["ERC"].children[0].values == (-1,)
    current = case.store.profile()
    state, _ = case.bundle.player_state.credit_units(current.state, [{"card_id": 54, "level": 1, "count": 1}])
    case.store.update_state(state, current.revision)
    fails(case, message, "fairy_farm_retry_stale")
    bulk_result(case, bulk(case, tick=1001))
    assert stock(case) == 1


def test_concurrent_duplicate_bulk_returns_same_result_once(case):
    message = bulk(case)
    revision = case.store.profile().revision
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: case.farm.bulk_deposit(message, case.store), range(2)))
    assert replies[0] == replies[1]
    assert stock(case) == 2 and case.store.profile().revision == revision + 1


def test_bulk_capacity_preflight_failure_preserves_every_domain(case, monkeypatch):
    import aigis_ps.fairy_farm as module
    original = module.encode_response
    def reject(profile, xml):
        if profile.registration_keys == (124,):
            raise ProtocolError("bulk_capacity_test", "capacity")
        return original(profile, xml)
    monkeypatch.setattr(module, "encode_response", reject)
    fails(case, bulk(case), "bulk_capacity_test")


@pytest.mark.parametrize("columns", [
    (Container("BRK", ()),),
    (Container("BRK", (Column("IDX", "S", ("0",)),)),),
    (Container("LST", (Column("A7", "I", (1,)),)),),
    (Container("BRK", (Column("IDX", "I", (0,)), Column("Unknown", "I", (0,)))),),
])
def test_bulk_rejects_malformed_business_fields(case, columns):
    fails(case, request(case.store, 124, columns), "invalid_request_fields")
