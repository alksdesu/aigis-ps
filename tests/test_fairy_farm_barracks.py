from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import loads
from aigis_ps.barracks_service import EXTRA_SHIFT, PURCHASE_FIELD
from aigis_ps.errors import ProtocolError
from aigis_ps.fairy_farm import FairyFarmService
from aigis_ps.player_state import BARRACKS_BITS, BARRACKS_SHIFT, LOCK_FLAG, barracks_index
from aigis_ps.storage import Store
from test_fairy_farm import bulk, bulk_result, deposit, fails, mutate, seed, stock, withdraw
from test_quest_service import bundle, request


def build(bundle, barracks):
    return FairyFarmService(bundle.bootstrap, bundle.player_state, bundle.gifts,
        lambda name: bundle.native_cdn.assets.read(name)[0], barracks=barracks)


@pytest.fixture(scope="module")
def farm(bundle):
    return build(bundle, bundle.unit_capacity)


@pytest.fixture
def case(bundle, farm, tmp_path):
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state, units = bundle.player_state.credit_units(state,
        [{"card_id": farm.card_ids[0], "level": 1, "count": 6}])
    state["tables"]["config"][0][PURCHASE_FIELD] = (2 << EXTRA_SHIFT) | 35
    sources = dict(zip((unit["GetIndex"] for unit in units), (2, 0, 1, 2, 0, 1)))
    for unit in state["player_state"]["units"]:
        if unit["GetIndex"] in sources:
            unit["GeneralPurposeFlags"] = (sources[unit["GetIndex"]] << BARRACKS_SHIFT) | 1
    return SimpleNamespace(bundle=bundle, farm=farm, units=units,
                           store=Store(tmp_path / "fairy-barracks.sqlite3", state))


def selected(case, source):
    return [unit for unit in case.store.profile().state["player_state"]["units"]
            if unit["CardID"] in case.farm.card_ids and barracks_index(unit) == source]


def move(case, identities, source):
    def change(state):
        for unit in state["player_state"]["units"]:
            if unit["GetIndex"] in identities:
                unit["GeneralPurposeFlags"] = (unit["GeneralPurposeFlags"] & ~BARRACKS_BITS) | source << BARRACKS_SHIFT
    mutate(case, change)


@pytest.mark.parametrize("source", [0, 1, 2])
@pytest.mark.parametrize("new_flag", [0, 1])
def test_selected_deposit_uses_one_actual_barracks_and_preserves_other_units(case, source, new_flag):
    units = selected(case, source)
    identifiers = {unit["GetIndex"] for unit in units}
    def change(state):
        for unit in state["player_state"]["units"]:
            if unit["GetIndex"] in identifiers:
                unit["GeneralPurposeFlags"] = (source << BARRACKS_SHIFT) | new_flag
    mutate(case, change)
    before = case.store.profile()
    response = case.farm.deposit(deposit(case, units), case.store)
    after = case.store.profile()
    assert stock(case) == len(units)
    assert after.state["player_state"]["units"] == [unit for unit in before.state["player_state"]["units"]
        if unit["GetIndex"] not in identifiers]
    assert after.state["player_state"]["battle"] == before.state["player_state"]["battle"]
    assert after.state["player_state"]["last_unique_id"] == before.state["player_state"]["last_unique_id"]
    assert after.state["tables"]["game_param"] == before.state["tables"]["game_param"]
    assert after.state["tables"]["config"] == before.state["tables"]["config"]
    assert {node.name for node in loads(response).children} == {"SYSTEM", "PFF"}


def test_selected_deposit_rejects_mixed_current_barracks_without_any_credit(case):
    units = [selected(case, 1)[0], selected(case, 2)[0]]
    fails(case, deposit(case, units), "fairy_farm_mixed_barracks")


def test_selected_deposit_still_requires_exact_unique_identity(case):
    unit = dict(selected(case, 2)[0], UniqueID=9999)
    fails(case, deposit(case, [unit]), "fairy_farm_unit_missing")


NONSTANDARD_STATES = [
    ("GeneralPurposeFlags", 0x13), ("GeneralPurposeFlags", 0x15), ("GeneralPurposeFlags", 0x111),
    ("GeneralPurposeFlags", -0x80000000 | 0x11), ("Exp", 1), ("SkillExperience", 1), ("Love", 1),
    ("OrganizationIndex", 2),
]


def change_first(case, units, field, value):
    identifier = units[0]["GetIndex"]
    def change(state):
        next(unit for unit in state["player_state"]["units"] if unit["GetIndex"] == identifier)[field] = value
    mutate(case, change)


@pytest.mark.parametrize("field,value", NONSTANDARD_STATES)
def test_selected_extra_deposit_keeps_only_the_count_of_any_unorganized_state(case, field, value):
    units = selected(case, 1)
    change_first(case, units, field, value)
    case.farm.deposit(deposit(case, units), case.store)
    assert stock(case) == len(units)
    assert not selected(case, 1)


@pytest.mark.parametrize("field,value", NONSTANDARD_STATES)
def test_bulk_ignores_only_barracks_bits_for_the_standard_template(case, field, value):
    units = selected(case, 1)
    change_first(case, units, field, value)
    _, nodes = bulk_result(case, bulk(case, 1))
    assert nodes["RML"].children[0].values == tuple(unit["GetIndex"] for unit in units[1:])
    assert [unit["GetIndex"] for unit in selected(case, 1)] == [units[0]["GetIndex"]]


def test_selected_extra_unit_in_any_squad_remains_protected(case):
    units = selected(case, 2)
    mutate(case, lambda state: state["player_state"]["squads"].append(
        {"Address": 0x07000001, "UnitIndex": units[0]["GetIndex"], "Name": "legacy", "Flags": 0}))
    fails(case, deposit(case, units), "fairy_farm_unit_protected")


@pytest.mark.parametrize("scope", [None, 0, 1, 2])
def test_bulk_scope_reaches_selection_and_remove_response(case, scope):
    before = case.store.profile()
    expected = [unit for unit in before.state["player_state"]["units"] if unit["CardID"] in case.farm.card_ids
                and (scope is None or barracks_index(unit) == scope)]
    mutate(case, lambda state: state["player_state"]["units"].reverse())
    _, nodes = bulk_result(case, bulk(case, scope))
    assert nodes["RML"].children[0].values == tuple(unit["GetIndex"] for unit in expected)
    assert nodes["RML"].children[1].values == tuple(unit["UniqueID"] for unit in expected)
    assert nodes["ERC"].children[0].values == (0,)
    assert len(nodes["PFF"].children[0].values) == 54
    assert stock(case) == len(expected)
    removed = {unit["GetIndex"] for unit in expected}
    after = case.store.profile().state
    assert {unit["GetIndex"] for unit in after["player_state"]["units"]} == {
        unit["GetIndex"] for unit in before.state["player_state"]["units"] if unit["GetIndex"] not in removed}
    assert after["tables"]["config"] == before.state["tables"]["config"]
    assert after["player_state"]["last_unique_id"] == before.state["player_state"]["last_unique_id"]


@pytest.mark.parametrize("scope,first", [(None, 1), (0, 2), (1, 3), (2, 1)])
def test_bulk_full_kind_keeps_global_getindex_order_within_scope(case, scope, first):
    seed(case, case.farm.cap - 1)
    _, nodes = bulk_result(case, bulk(case, scope))
    assert nodes["RML"].children[0].values == (first,)
    assert nodes["ERC"].children[0].values == (1,)
    assert stock(case) == case.farm.cap
    assert len(case.store.profile().state["player_state"]["units"]) == 6


def test_bulk_full_out_of_scope_kind_does_not_affect_selected_scope_or_code(case):
    move(case, [unit["GetIndex"] for unit in case.units], 2)
    seed(case, case.farm.cap)
    before = case.store.profile()
    other = case.farm.card_ids[1]
    state, units = case.bundle.player_state.credit_units(before.state, [{"card_id": other, "level": 1, "count": 1}])
    state["player_state"]["units"][-1]["GeneralPurposeFlags"] = 0x11
    case.store.update_state(state, before.revision)
    _, nodes = bulk_result(case, bulk(case, 1))
    assert nodes["RML"].children[0].values == (units[0]["GetIndex"],)
    assert nodes["ERC"].children[0].values == (0,)
    assert stock(case) == case.farm.cap and stock(case, other) == 1


def test_bulk_skips_protected_and_modified_units_in_extra_barracks(case):
    def change(state):
        state["player_state"]["units"][1]["GeneralPurposeFlags"] |= LOCK_FLAG
        state["player_state"]["units"][3]["GeneralPurposeFlags"] |= 0x100
        state["player_state"]["squads"].append({"Address": 1, "UnitIndex": 4, "Name": "old", "Flags": 0})
    mutate(case, change)
    _, nodes = bulk_result(case)
    assert nodes["RML"].children[0].values == (2, 5, 6)
    assert nodes["ERC"].children[0].values == (0,)
    assert stock(case) == 3


@pytest.mark.parametrize("scope", [None, 1, 2])
def test_bulk_full_inventory_has_empty_remove_list_and_no_eligible_code(case, scope):
    seed(case, case.farm.cap)
    before = case.store.profile()
    _, nodes = bulk_result(case, bulk(case, scope))
    assert nodes["RML"].children[0].values == nodes["RML"].children[1].values == ()
    assert nodes["ERC"].children[0].values == (-1,)
    assert case.store.profile().state["player_state"] == before.state["player_state"]


def test_explicit_locked_barracks_is_rejected_even_when_empty(case):
    move(case, [unit["GetIndex"] for unit in case.units], 0)
    mutate(case, lambda state: state["tables"]["config"][0].update({PURCHASE_FIELD: 1 << EXTRA_SHIFT}))
    fails(case, bulk(case, 2), "fairy_farm_barrack_locked")


@pytest.mark.parametrize("scope", [-1, 3, 8])
def test_bulk_retains_native_scope_bounds(case, scope):
    fails(case, bulk(case, scope), "invalid_request_fields" if scope < 0 else "fairy_farm_barrack_unsupported")


@pytest.mark.parametrize("operation", [123, 124])
def test_units_in_locked_or_unknown_tables_do_not_get_silently_skipped(case, operation):
    move(case, [case.units[0]["GetIndex"]], 8)
    envelope = deposit(case, selected(case, 1)) if operation == 123 else bulk(case)
    fails(case, envelope, "unit_capacity_barracks_unsupported")
    before = case.store.profile()
    assert case.farm.load(request(case.store, 122, ()), case.store)
    assert case.store.profile() == before


@pytest.mark.parametrize("operation", [123, 124])
def test_missing_capacity_keeps_explicit_primary_compatible(case, operation):
    case.farm = build(case.bundle, None)
    envelope = deposit(case, selected(case, 0)) if operation == 123 else bulk(case, 0)
    case.farm.handlers()[operation](envelope, case.store)
    assert stock(case) == 2
    assert {unit["GetIndex"] for unit in case.store.profile().state["player_state"]["units"]} == {0, 1, 3, 4, 6}


@pytest.mark.parametrize("operation,scope", [(123, 1), (123, 2), (124, 1), (124, 2), (124, None)])
def test_missing_capacity_rejects_extra_or_unresolvable_all_scope(case, operation, scope):
    case.farm = build(case.bundle, None)
    envelope = deposit(case, selected(case, scope)) if operation == 123 else bulk(case, scope)
    fails(case, envelope, "fairy_farm_barrack_unsupported")


def test_withdrawal_still_creates_primary_instances(case):
    seed(case, 2)
    before = case.store.profile()
    response = case.farm.withdraw(withdraw(case, count=2), case.store)
    after = case.store.profile().state
    added = after["player_state"]["units"][len(before.state["player_state"]["units"]):]
    assert len(added) == 2 and all(unit["GeneralPurposeFlags"] == 1 for unit in added)
    assert after["player_state"]["units"][:len(before.state["player_state"]["units"])] == before.state["player_state"]["units"]
    assert next(node for node in loads(response).children if node.name == "PPU").children


@pytest.mark.parametrize("operation", [123, 124])
def test_exact_retry_survives_rebuild_with_same_barracks_rules(case, operation):
    envelope = deposit(case, selected(case, 2)) if operation == 123 else bulk(case)
    response = case.farm.handlers()[operation](envelope, case.store)
    committed = case.store.profile()
    reopened = Store(case.store.path)
    service = build(case.bundle, case.bundle.unit_capacity)
    assert service.handlers()[operation](envelope, reopened) == response
    assert reopened.profile() == committed
    case.farm = build(case.bundle, None)
    fails(case, envelope, "fairy_farm_retry_stale")


def test_legacy_policy_receipt_cannot_replay_under_new_scope_rules(case):
    envelope = bulk(case)
    case.farm.bulk_deposit(envelope, case.store)
    legacy = {"master": case.bundle.gifts.catalog.snapshot.identifier,
              "client": case.bundle.gifts.catalog.snapshot.client_hash,
              "version": case.bundle.gifts.catalog.resource_version,
              "resources": case.farm._hashes, "cap": case.farm.cap, "cards": case.farm._cards}
    content = hashlib.sha256(json.dumps(legacy, sort_keys=True, allow_nan=False).encode()).hexdigest()
    assert content != case.farm.content_version
    current = case.store.profile()
    current.state["fairy_farm_history"]["receipts"][-1].update(content=content, revision=current.revision + 1)
    case.store.update_state(current.state, current.revision)
    fails(case, envelope, "fairy_farm_retry_stale")


def test_selected_deposit_rechecks_source_after_competing_move(case, monkeypatch):
    units = selected(case, 1)
    update = case.store.update_state
    competing = []
    def conflict(state, revision):
        if not competing:
            current = case.store.profile()
            current.state["player_state"]["units"][units[0]["GetIndex"]]["GeneralPurposeFlags"] = 0x21
            update(current.state, current.revision)
            competing.append(case.store.profile())
        return update(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    with pytest.raises(ProtocolError) as caught:
        case.farm.deposit(deposit(case, units), case.store)
    assert caught.value.code == "fairy_farm_mixed_barracks"
    assert case.store.profile() == competing[0]


def test_bulk_rechecks_scope_after_competing_move(case, monkeypatch):
    update = case.store.update_state
    competing = []
    def conflict(state, revision):
        if not competing:
            current = case.store.profile()
            current.state["player_state"]["units"][3]["GeneralPurposeFlags"] = 0x21
            update(current.state, current.revision)
            competing.append(True)
        return update(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    _, nodes = bulk_result(case, bulk(case, 1))
    assert nodes["RML"].children[0].values == (6,)
    assert stock(case) == 1
    assert any(unit["GetIndex"] == 3 for unit in case.store.profile().state["player_state"]["units"])


def test_concurrent_all_scope_retry_commits_once(case):
    envelope = bulk(case)
    revision = case.store.profile().revision
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: case.farm.bulk_deposit(envelope, case.store), range(2)))
    assert replies[0] == replies[1]
    assert stock(case) == 6 and case.store.profile().revision == revision + 1


@pytest.mark.parametrize("operation", [123, 124])
def test_response_preflight_failure_leaves_extra_instances_and_stock_unchanged(case, monkeypatch, operation):
    import aigis_ps.fairy_farm as module
    encode = module.encode_response
    def reject(profile, xml):
        if operation in profile.registration_keys:
            raise ProtocolError("test_capacity", "capacity")
        return encode(profile, xml)
    monkeypatch.setattr(module, "encode_response", reject)
    envelope = deposit(case, selected(case, 2)) if operation == 123 else bulk(case)
    fails(case, envelope, "test_capacity")


def test_barracks_content_is_frozen_and_changes_invalidate_writes(case):
    capacity = SimpleNamespace(content_version=case.bundle.unit_capacity.content_version,
                               usage=case.bundle.unit_capacity.usage)
    case.farm = build(case.bundle, capacity)
    assert case.farm.content_version == build(case.bundle, capacity).content_version
    capacity.content_version = "f" * 64
    fails(case, bulk(case), "fairy_farm_resources_changed")


@pytest.mark.parametrize("capacity", [object(), SimpleNamespace(content_version="bad", usage=lambda _: None),
                                     SimpleNamespace(content_version="a" * 64, usage=None)])
def test_invalid_barracks_dependency_is_explicit(bundle, capacity):
    with pytest.raises(ProtocolError) as caught:
        build(bundle, capacity)
    assert caught.value.code == "fairy_farm_resources_invalid"
