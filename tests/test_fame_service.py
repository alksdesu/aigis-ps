from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from datetime import datetime
import json
import logging
from types import SimpleNamespace

import pytest

import aigis_ps.fame_service as fame_module
from aigis_ps.alxml import Column, Container, loads
from aigis_ps.barracks_service import EXTRA_SHIFT, PURCHASE_FIELD
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.evolution_catalog import EvolutionCatalog
from aigis_ps.evolution_materials import EvolutionMaterials
from aigis_ps.fame_catalog import FameCatalog
from aigis_ps.fame_service import FameService
from aigis_ps.fame_state import FameRepository
from aigis_ps.mission_service import MissionService
from aigis_ps.player_state import BARRACKS_BITS, BARRACKS_SHIFT, LOCK_FLAG, MAX_PLAYER_UNITS, UNIT_FIELDS
from aigis_ps.request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER
from aigis_ps.storage import Store
from test_fame_catalog import deposit_menu, with_deposit_menu
from test_quest_service import bundle, request, start_request
from test_training_service import unfarmed


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
RESENDS = "the native client shows a communication error and resends the same request until the server accepts it"


@pytest.fixture(scope="module")
def catalog(bundle):
    return FameCatalog(bundle.training.catalog, bundle.evolution.catalog,
                       lambda name: bundle.native_cdn.assets.read(name)[0])


@pytest.fixture(scope="module")
def unfarmed_catalog(bundle, catalog, unfarmed):
    evolution = catalog.evolution
    materials = EvolutionMaterials(unfarmed.cards, unfarmed.classes, unfarmed.rarities)
    return FameCatalog(unfarmed, EvolutionCatalog(unfarmed, evolution.possessions, materials, rules=evolution.rules),
                       lambda name: bundle.native_cdn.assets.read(name)[0])


def fame_service(bundle, fame):
    repository = FameRepository(fame)
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    bootstrap.fame_provider = repository.response
    missions = MissionService(bootstrap, bundle.player_state, bundle.presents, bundle.missions.catalog,
        skins=bundle.unit_detail.skin_catalog, fame=fame, fame_status=bundle.card_status)
    return FameService(bootstrap, bundle.player_state, repository, fame, bundle.card_status,
        bundle.gifts, bundle.evolution_rewards, missions, barracks=bundle.unit_capacity)


@pytest.fixture
def make_case(bundle, catalog, tmp_path):
    serial = 0

    def build(*, count=1, card_id=17, stages=None, registered=False, source=0, rank=50, fame=None):
        nonlocal serial
        serial += 1
        fame = catalog if fame is None else fame
        service = fame_service(bundle, fame)
        repository, bootstrap, missions = service.repository, service.bootstrap, service.missions
        state = missions.initialize(bundle.player_state.initialize(bootstrap.initial_state()))
        state["tables"]["game_param"][0].update(Rank=rank,
            Experience=sum(row["NextExp"] for row in bootstrap.rank_rows[:rank - 1]))
        state["tables"]["config"][0][PURCHASE_FIELD] |= source << EXTRA_SHIFT
        state, added = bundle.player_state.credit_units(state, [{"card_id": card_id, "level": 1, "count": count}])
        stages = stages or (fame.stages(card_id)[0],) * count
        assert len(stages) == count
        selected = []
        for created, stage in zip(added, stages):
            saved = next(row for row in state["player_state"]["units"] if row["GetIndex"] == created["GetIndex"])
            saved.update(fame.maximum_values(card_id, stage))
            saved.update(Famed=registered, GeneralPurposeFlags=saved["GeneralPurposeFlags"] | (source << BARRACKS_SHIFT))
            assert fame.eligible(saved)
            selected.append(deepcopy(saved))
        if registered:
            state = bundle.card_status.update(state, [{"CardID": card_id, **{f"B{stage}": 123456 for stage in stages}}])
        path = tmp_path / f"fame-{serial}.sqlite3"
        return SimpleNamespace(bundle=bundle, catalog=fame, repository=repository, bootstrap=bootstrap,
            missions=missions, service=service, store=Store(path, state), path=path, selected=selected)

    return build


def message(case, operation=149, *, indices=None, card_id=None, stage=None, tick=1000, columns=None):
    if columns is None:
        if operation == 151:
            unit = case.selected[0]
            columns = (Container("WITHDRAW", (Column("CardId", "I", (unit["CardID"] if card_id is None else card_id,)),
                Column("EvolutionId", "I", (unit["ClassChangeCount"] if stage is None else stage,)))),)
        else:
            indices = tuple(unit["GetIndex"] for unit in case.selected) if indices is None else tuple(indices)
            group, name = ("FAMERS", "UnitId") if operation == 149 else ("DEPOSIT", "InviteId")
            columns = (Container(group, (Column(name, "I", indices),)),)
    return request(case.store, operation, columns, tick=tick)


def call(case, envelope):
    return case.service.handlers()[envelope.profile.registration_keys[0]](envelope, case.store)


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


def logged(caplog):
    return [record.getMessage() for record in caplog.records]


def fail(case, envelope, code=None):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        call(case, envelope)
    if code is not None:
        assert caught.value.code == code
    assert case.store.profile() == before
    return caught.value


def nodes(response):
    return {node.name: node for node in loads(response).children}


def snapshot(case):
    return {row["ID"]: row for row in case.missions.progress.snapshot(case.store.profile().state, NOW)}


def stock(case, amount=1):
    before = case.store.profile()
    unit = case.selected[0]
    state = case.repository.adjust(before.state, {(unit["CardID"], unit["ClassChangeCount"]): amount})
    case.store.update_state(state, before.revision)


def test_registration_updates_full_units_preserves_status_and_counts_unique_stage(make_case):
    case = make_case(count=2)
    card = case.selected[0]["CardID"]
    stage = case.selected[0]["ClassChangeCount"]
    before = case.store.profile()
    state = case.bundle.card_status.update(before.state, [{"CardID": card, "A1": 19, "A3": -8,
        "A5": 7, "A7": 3, "B0": 23456}])
    case.store.update_state(state, before.revision)
    before = case.store.profile()
    envelope = message(case)
    response = call(case, envelope)
    after = case.store.profile()
    data = nodes(response)
    expected = deepcopy(case.selected)
    for unit in expected:
        unit.update(Famed=True, CanFame=True)
    assert data["PPU"].children == tuple(Column(alias, "B" if name == "CanFame" else "I",
        tuple(int(row[name]) if name == "Famed" else row[name] for row in expected)) for name, alias in UNIT_FIELDS)
    assert data["FameRegister"].children == (Column("InviteId", "I", (1, 2)), Column("CardId", "I", (card, card)))
    status = case.bundle.card_status.read(after.state)[0]
    assert {key: status[key] for key in ("A1", "A3", "A5", "A7", "B0")} == {
        "A1": 19, "A3": -8, "A5": 7, "A7": 3, "B0": 23456}
    assert status[f"B{stage}"] == int(NOW.timestamp())
    assert sum(bool(status[f"B{i}"]) for i in range(5)) == 2
    progress = snapshot(case)
    assert all(progress[d.condition_id]["Progress"] == 1 for d in case.missions.catalog.definitions
               if d.condition_type == 10 and d.condition_id in progress)
    assert after.state["tables"]["presents"] == before.state["tables"]["presents"]
    assert after.state["tables"]["crystals"] == before.state["tables"]["crystals"]
    assert after.state["tables"]["game_param"] == before.state["tables"]["game_param"]
    assert case.repository.read(after.state) == []
    assert call(case, envelope) == response and case.store.profile() == after
    reopened = Store(case.path)
    assert case.service.register(envelope, reopened) == response and reopened.profile() == after


def test_two_second_awakening_branches_remain_distinct_registration_events(make_case):
    case = make_case(count=2, stages=(3, 4))
    call(case, message(case))
    row = case.bundle.card_status.read(case.store.profile().state)[0]
    assert row["B3"] == row["B4"] == int(NOW.timestamp())
    assert row["B0"] == row["B1"] == row["B2"] == 0
    progress = snapshot(case)
    assert all(progress[d.condition_id]["Progress"] == min(2, d.in_hallof_fame_sum)
               for d in case.missions.catalog.definitions if d.condition_type == 10 and d.condition_id in progress)


def test_support_registration_counts_both_total_and_support_missions(make_case, catalog):
    card = next(identifier for identifier in sorted(catalog.support_card_ids & catalog.card_ids)
                if catalog.stages(identifier))
    case = make_case(card_id=card)
    call(case, message(case))
    progress = snapshot(case)
    relevant = [definition for definition in case.missions.catalog.definitions
                if definition.condition_type in (10, 16) and definition.condition_id in progress]
    assert {definition.condition_type for definition in relevant} == {10, 16}
    assert all(progress[definition.condition_id]["Progress"] == 1 for definition in relevant)


def test_old_stage_is_not_redated_or_counted_again(make_case):
    case = make_case(registered=True)
    mutate(case, lambda state: state["player_state"]["units"][1].update(Famed=False))
    call(case, message(case))
    assert case.bundle.card_status.read(case.store.profile().state)[0][f"B{case.selected[0]['ClassChangeCount']}"] == 123456
    progress = snapshot(case)
    assert all(progress[d.condition_id]["Progress"] == 0 for d in case.missions.catalog.definitions
               if d.condition_type in (10, 16) and d.condition_id in progress)


def test_registration_allows_locked_and_organized_primary_units(make_case):
    case = make_case()
    def setup(state):
        state["player_state"]["units"][1]["GeneralPurposeFlags"] |= LOCK_FLAG
        state["player_state"]["squads"].append({"Address": 0x70000, "UnitIndex": 1, "Name": "old", "Flags": 0})
    mutate(case, setup)
    before = case.store.profile()
    call(case, message(case))
    after = case.store.profile().state
    assert after["player_state"]["units"][1]["GeneralPurposeFlags"] == before.state["player_state"]["units"][1]["GeneralPurposeFlags"]
    assert after["player_state"]["squads"] == before.state["player_state"]["squads"]


@pytest.mark.parametrize("source", [1, 2])
def test_registration_refuses_a_batch_left_only_in_extra_barracks(make_case, caplog, source):
    case = make_case(source=source)
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    fail(case, message(case), "fame_registration_barracks")
    assert logged(caplog) == [f"Fame operation 149 refused with fame_registration_barracks; {RESENDS}"]


def registered_units(case):
    return {unit["GetIndex"]: unit for unit in case.store.profile().state["player_state"]["units"]}


@pytest.mark.parametrize("kind,index,code", [
    ("ineligible", 2, "fame_ineligible"), ("missing", 99, "fame_unit_missing"),
    ("prince", 0, "fame_unit_protected"), ("extra_barrack", 2, "fame_registration_barracks")])
def test_registration_keeps_qualifying_units_and_logs_each_divergent_one(make_case, caplog, kind, index, code):
    case = make_case(count=2, source=1 if kind == "extra_barrack" else 0)
    if kind == "ineligible":
        mutate(case, lambda state: state["player_state"]["units"][2].update(Exp=0))
    elif kind == "extra_barrack":
        mutate(case, lambda state: state["player_state"]["units"][1].update(GeneralPurposeFlags=5))
    before = registered_units(case)
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    response = call(case, message(case, indices=[1, index]))
    assert nodes(response)["FameRegister"].children == (
        Column("InviteId", "I", (1,)), Column("CardId", "I", (case.selected[0]["CardID"],)))
    expected = deepcopy(before)
    expected[1].update(Famed=True, CanFame=True)
    assert registered_units(case) == expected
    stage = case.selected[0]["ClassChangeCount"]
    assert case.bundle.card_status.read(case.store.profile().state)[0][f"B{stage}"] == int(NOW.timestamp())
    assert logged(caplog) == [f"Fame registration 149 left {index}:{code} unregistered; "
                              "the native client lists them again until they qualify"]


def test_registration_without_a_qualifying_unit_is_refused_with_the_first_cause(make_case, caplog):
    case = make_case(count=2)
    mutate(case, lambda state: state["player_state"]["units"][1].update(Exp=0))
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    fail(case, message(case, indices=[1, 99]), "fame_ineligible")
    assert logged(caplog) == [f"Fame operation 149 refused with fame_ineligible; {RESENDS}"]


def test_roster_pointing_at_a_locked_barracks_still_refuses_the_whole_registration(make_case, caplog):
    case = make_case(count=2)
    mutate(case, lambda state: state["player_state"]["units"][2].update(GeneralPurposeFlags=0x14))
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    fail(case, message(case), "unit_capacity_barracks_unsupported")
    assert logged(caplog) == [f"Fame operation 149 refused with unit_capacity_barracks_unsupported; {RESENDS}"]


def test_already_registered_instances_are_reported_again_without_new_events(make_case, caplog):
    case = make_case(count=2)
    mutate(case, lambda state: state["player_state"]["units"][2].update(Famed=True))
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    response = call(case, message(case))
    assert nodes(response)["FameRegister"].children[0].values == (1, 2)
    assert all(unit["Famed"] for index, unit in registered_units(case).items() if index)
    assert logged(caplog) == []
    again = case.store.profile()
    call(case, message(case, tick=1001))
    after = case.store.profile().state
    assert after["tables"]["card_status"] == again.state["tables"]["card_status"]
    assert after["mission_state"] == again.state["mission_state"]


def test_a_batch_of_registered_instances_alone_writes_no_history(make_case, caplog):
    case = make_case(registered=True)
    card_id, stage = case.selected[0]["CardID"], case.selected[0]["ClassChangeCount"]
    other = next(value for value in range(5) if value != stage)
    def diverge(state):
        state["player_state"]["units"][1]["Exp"] = 0
        status = next(row for row in state["tables"]["card_status"] if row["CardID"] == card_id)
        status.update({f"B{stage}": 0, f"B{other}": 123456})
    mutate(case, diverge)
    before = case.store.profile().state
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    response = call(case, message(case))
    assert nodes(response)["FameRegister"].children[0].values == (1,)
    after = case.store.profile().state
    assert after["tables"]["card_status"] == before["tables"]["card_status"]
    assert after["mission_state"] == before["mission_state"]
    assert after["player_state"]["units"] == before["player_state"]["units"]
    assert logged(caplog) == []


def test_classic_class_change_registers_its_own_stage_without_a_storage_bucket(make_case, catalog, caplog):
    card = 8
    job = catalog.growth.classes[catalog.growth.cards.card(card)["InitClassID"]]["JobChange"]
    case = make_case(card_id=card)
    mutate(case, lambda state: state["player_state"]["units"][1].update(ClassChangeCount=1, ClassID=job))
    response = call(case, message(case))
    assert nodes(response)["FameRegister"].children[0].values == (1,)
    row = case.bundle.card_status.read(case.store.profile().state)[0]
    assert (row["CardID"], row["B0"], row["B1"]) == (card, 0, int(NOW.timestamp()))
    assert not catalog.registered([row], card, 1)
    progress = snapshot(case)
    assert all(progress[d.condition_id]["Progress"] == 1 for d in case.missions.catalog.definitions
               if d.condition_type == 10 and d.condition_id in progress)
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    fail(case, message(case, 150, tick=1001), "fame_stage_unavailable")
    assert logged(caplog) == [f"Fame operation 150 refused with fame_stage_unavailable; {RESENDS}"]


@pytest.mark.parametrize("operation", [149, 150, 151])
def test_all_operations_require_rank_fifty(make_case, operation):
    case = make_case(registered=operation != 149, rank=49)
    if operation == 151:
        stock(case)
    fail(case, message(case, operation), "fame_rank_required")


@pytest.mark.parametrize("operation,columns", [
    (149, ()), (149, (Container("FAMERS"),)),
    (149, (Container("FAMERS", (Column("UnitId", "I", ()),)),)),
    (149, (Container("FAMERS", (Column("UnitId", "I", (1, 1)),)),)),
    (149, (Container("FAMERS", (Column("UnitId", "I", (-1,)),)),)),
    (149, (Container("FAMERS", (Column("UnitId", "S", ("1",)),)),)),
    (149, (Container("FAMERS", (Column("UnitId", "I", (1,)), Column("AB", "I", (2,)))),)),
    (150, (Container("DEPOSIT", (Column("InviteId", "I", ()),)),)),
    (150, (Container("DEPOSIT", (Column("InviteId", "I", tuple(range(1, 12))),)),)),
    (150, (Container("DEPOSIT", (Column("InviteID", "I", (1,)),)),)),
    (151, (Container("WITHDRAW", (Column("CardId", "I", (17,)),)),)),
    (151, (Container("WITHDRAW", (Column("CardId", "I", (17,)), Column("EvolutionId", "I", (3,)), Column("NUM", "I", (1,)))),)),
    (151, (Container("WITHDRAW", (Column("CardId", "I", (17, 17)), Column("EvolutionId", "I", (3,)))),)),
    (151, (Container("WITHDRAW", (Column("CardId", "I", (17,)), Column("EvolutionId", "I", (5,)))),)),
])
def test_strict_native_request_shapes_never_write(make_case, operation, columns):
    case = make_case()
    fail(case, message(case, operation, columns=columns), "invalid_request_fields")


def test_registration_batch_is_not_restricted_to_deposit_ten_unit_limit(make_case):
    case = make_case(count=11)
    response = call(case, message(case))
    assert len(nodes(response)["FameRegister"].children[0].values) == 11


def test_deposit_uses_history_not_instance_famed_and_allows_lock(make_case):
    case = make_case(registered=True)
    def setup(state):
        state["player_state"]["units"][1]["Famed"] = False
        state["player_state"]["units"][1]["GeneralPurposeFlags"] |= LOCK_FLAG
    mutate(case, setup)
    before = case.store.profile()
    response = call(case, message(case, 150))
    after = case.store.profile().state
    stage = case.selected[0]["ClassChangeCount"]
    assert len(after["player_state"]["units"]) == 1
    assert after["player_state"]["last_unique_id"] == before.state["player_state"]["last_unique_id"]
    assert after["player_state"]["battle"] == before.state["player_state"]["battle"]
    assert after["tables"]["card_status"] == before.state["tables"]["card_status"]
    assert case.repository.read(after)[0][f"Count{stage}"] == 1
    assert "gift_state" in after and "evolution_rewards" in after
    assert all(not column.values for column in nodes(response)["PPU"].children)
    assert set(nodes(response)) == {"SYSTEM", "PPU", "FameDepository"}


@pytest.mark.parametrize("source", [1, 2])
@pytest.mark.parametrize("mixed", [False, True])
def test_deposit_rejects_extra_and_mixed_barracks_atomically(make_case, source, mixed):
    case = make_case(registered=True, source=source, count=2 if mixed else 1)
    if mixed:
        mutate(case, lambda state: state["player_state"]["units"][1].update(GeneralPurposeFlags=5))
    fail(case, message(case, 150), "fame_deposit_barracks")


@pytest.mark.parametrize("kind", ["organized", "unregistered", "over_capacity"])
def test_deposit_rejects_entire_batch_without_erasing_history(make_case, kind):
    case = make_case(count=2, registered=kind != "unregistered")
    if kind == "organized":
        mutate(case, lambda state: state["player_state"]["squads"].append(
            {"Address": 0x70000, "UnitIndex": 2, "Name": "old", "Flags": 0}))
    elif kind == "over_capacity":
        stock(case, case.catalog.deposit_limit - 1)
    fail(case, message(case, 150))


def test_registration_deposit_withdrawal_is_one_way_identity_and_retains_rewards(make_case):
    case = make_case()
    original = case.selected[0]
    call(case, message(case, 149))
    call(case, message(case, 150, tick=1001))
    before = case.store.profile()
    envelope = message(case, 151, tick=1002)
    response = call(case, envelope)
    after = case.store.profile()
    added = after.state["player_state"]["units"][-1]
    assert added["GetIndex"] > original["GetIndex"] and added["UniqueID"] > original["UniqueID"]
    maximum = case.catalog.maximum_values(added["CardID"], added["ClassChangeCount"])
    assert {key: added[key] for key in maximum} == maximum
    assert case.catalog.eligible(added) and added["Famed"]
    assert added["GeneralPurposeFlags"] & BARRACKS_BITS == 0
    assert case.repository.read(after.state)[0][f"Count{added['ClassChangeCount']}"] == 0
    assert after.state["tables"]["card_status"] == before.state["tables"]["card_status"]
    assert after.state["tables"]["presents"] == before.state["tables"]["presents"]
    assert after.state["mission_state"] == before.state["mission_state"]
    assert nodes(response)["PBP"].children == (Column("A1", "S", ("UX",)), Column("A2", "I", (added["GetIndex"],)))
    assert len(nodes(response)["PPU"].children) == 21
    assert case.service.withdraw(envelope, Store(case.path)) == response
    assert case.store.profile() == after


@pytest.mark.parametrize("kind", ["empty", "stage_one", "identity_capacity", "native_capacity", "legacy_active"])
def test_withdrawal_failures_do_not_decrement_stock(make_case, monkeypatch, kind):
    case = make_case(registered=True)
    if kind != "empty":
        stock(case)
    if kind == "identity_capacity":
        mutate(case, lambda state: state["player_state"].update(last_unique_id=0x7fffffff))
    elif kind == "native_capacity":
        monkeypatch.setattr(case.bundle.player_state, "capacity_provider", lambda state: 0)
    elif kind == "legacy_active":
        mutate(case, lambda state: state.update(quest_state={"active": {"sequence": 1}}))
    fail(case, message(case, 151, stage=1 if kind == "stage_one" else None))


@pytest.mark.parametrize("operation", [149, 150, 151])
@pytest.mark.parametrize("failure_profile", [41, 137, "current"])
def test_preflight_errors_are_atomic(make_case, monkeypatch, operation, failure_profile):
    case = make_case(registered=operation != 149)
    if operation == 151:
        stock(case)
    original = fame_module.encode_response
    number = operation if failure_profile == "current" else failure_profile
    def broken(profile, payload):
        if profile.registration_keys == (number,):
            raise ProtocolError("test_response_failure", "Preflight failed", 503)
        return original(profile, payload)
    monkeypatch.setattr(fame_module, "encode_response", broken)
    fail(case, message(case, operation), "test_response_failure")


def test_mission_hook_cannot_change_unrelated_player_state(make_case, monkeypatch):
    case = make_case()
    def broken(before, updated, now):
        result = deepcopy(updated)
        result["tables"]["game_param"][0]["Money"] += 1
        return result
    monkeypatch.setattr(case.missions, "record_fame", broken)
    fail(case, message(case), "fame_hook_invalid")


@pytest.mark.parametrize("mutation", ["identity", "missing_field"])
def test_withdrawal_template_requires_exact_nonidentity_fields(make_case, monkeypatch, mutation):
    case = make_case(registered=True)
    stock(case)
    original = case.catalog.maximum_values
    def broken(card, stage):
        result = original(card, stage)
        if mutation == "identity":
            result["GetIndex"] = 99
        else:
            result.pop("Famed")
        return result
    monkeypatch.setattr(case.catalog, "maximum_values", broken)
    fail(case, message(case, 151), "fame_template_invalid")


def test_old_receipt_cannot_reapply_registration_after_deposit(make_case):
    case = make_case()
    original = message(case)
    call(case, original)
    call(case, message(case, 150, tick=1001))
    fail(case, original, "fame_retry_stale")


def test_receipt_replays_after_mission_skins_attach(make_case):
    case = make_case()
    bundle = case.bundle
    plain = MissionService(case.bootstrap, bundle.player_state, bundle.presents, bundle.missions.catalog,
                           fame=case.catalog, fame_status=bundle.card_status)
    earlier = FameService(case.bootstrap, bundle.player_state, case.repository, case.catalog, bundle.card_status,
                          bundle.gifts, bundle.evolution_rewards, plain, barracks=bundle.unit_capacity)
    envelope = message(case)
    response = earlier.register(envelope, case.store)
    saved = case.store.profile()
    assert earlier.content_version != case.service.content_version
    assert call(case, envelope) == response
    assert case.store.profile() == saved
    overwrite(case, lambda state: state["fame_history"]["receipts"][-1].update(content="f" * 64))
    fail(case, envelope, "fame_retry_stale")


@pytest.mark.parametrize("operation", [149, 150, 151])
def test_receipts_replay_after_the_growth_fairy_farm_attaches(make_case, catalog, unfarmed_catalog, operation):
    assert unfarmed_catalog.content_version in catalog.replayable_content_versions() - {catalog.content_version}
    case = make_case(registered=operation != 149, fame=unfarmed_catalog)
    if operation == 151:
        stock(case)
    envelope = message(case, operation)
    response = call(case, envelope)
    saved = case.store.profile()
    earlier, case.service = case.service, fame_service(case.bundle, catalog)
    assert earlier.content_version != case.service.content_version
    assert call(case, envelope) == response
    assert case.store.profile() == saved


def test_receipt_pairing_a_catalog_with_another_repository_digest_is_stale(make_case, catalog, unfarmed_catalog):
    case = make_case(fame=unfarmed_catalog)
    envelope = message(case)
    call(case, envelope)
    earlier, case.service = case.service, fame_service(case.bundle, catalog)
    forged = case.service._content(earlier.missions.content_version, earlier.catalog.content_version,
                                   case.service.repository.content_version)
    overwrite(case, lambda state: state["fame_history"]["receipts"][-1].update(content=forged))
    fail(case, envelope, "fame_retry_stale")


@pytest.mark.parametrize("operation", [149, 150, 151])
def test_well_formed_but_incorrect_cached_response_is_rejected(make_case, operation):
    case = make_case(registered=operation != 149)
    if operation == 151:
        stock(case)
    envelope = message(case, operation)
    call(case, envelope)
    saved = case.store.profile()
    state = deepcopy(saved.state)
    receipt = state["fame_history"]["receipts"][-1]
    receipt["response"] = "<DA><PPU /></DA>"
    receipt["revision"] = saved.revision + 1
    case.store.update_state(state, saved.revision)
    fail(case, envelope, "fame_history_invalid")


@pytest.mark.parametrize("operation", [6, True, "150"])
def test_retained_receipt_with_unknown_operation_is_not_trusted(make_case, operation):
    case = make_case(count=2)
    call(case, message(case, indices=[1]))
    mutate(case, lambda state: state["fame_history"]["receipts"][0].update(operation=operation))
    fail(case, message(case, indices=[2], tick=1001), "fame_history_invalid")


def test_evicted_same_second_request_remains_consumed(make_case):
    case = make_case(count=HISTORY_LIMIT + 1)
    envelopes = [message(case, indices=[unit["GetIndex"]]) for unit in case.selected]
    for envelope in envelopes:
        call(case, envelope)
    history = case.store.profile().state["fame_history"]
    assert len(history["receipts"]) == HISTORY_LIMIT and len(history["processed_keys"]) == HISTORY_LIMIT + 1
    fail(case, envelopes[0], "fame_request_stale")


def test_client_time_is_not_an_order_for_fame_requests(make_case):
    case = make_case(count=3)
    first = message(case, indices=[1], tick=0x7fffffff)
    call(case, first)
    call(case, message(case, indices=[2], tick=-0x80000000))
    call(case, message(case, indices=[3], tick=1))
    assert all(unit["Famed"] for index, unit in registered_units(case).items() if index)
    history = case.store.profile().state["fame_history"]
    assert set(history) == {"schema_version", "receipts", "processed_keys"} and len(history["processed_keys"]) == 3
    assert all("client_time" not in receipt for receipt in history["receipts"])
    fail(case, first, "fame_retry_stale")


def test_status_reports_the_shared_ledger_and_menu_batch_limit(make_case, catalog):
    status = make_case().service.status()
    assert status["ready"] and status["maximum_deposit_batch"] == catalog.deposit_batch_limit == 10
    assert (status["history_limit"], status["processed_request_limit"], status["request_order"]) == (
        HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER)


def test_deposit_accepts_the_full_menu_batch(make_case, catalog):
    case = make_case(count=catalog.deposit_batch_limit, registered=True)
    call(case, message(case, 150))
    assert [unit["GetIndex"] for unit in case.store.profile().state["player_state"]["units"]] == [0]
    stage = case.selected[0]["ClassChangeCount"]
    assert case.repository.read(case.store.profile().state)[0][f"Count{stage}"] == catalog.deposit_batch_limit


def test_service_and_catalog_must_agree_on_the_deposit_batch_limit(make_case):
    case = make_case(registered=True)
    case.service.deposit_batch_limit += 1
    assert case.service.status()["ready"] is False
    fail(case, message(case, 150), "fame_dependencies_changed")


def test_deposit_request_size_follows_the_menu_multi_select(make_case, bundle, caplog):
    narrow = with_deposit_menu(bundle, deposit_menu(bundle, lambda command: command.replace(
        '"MaxMultiSelect=10"', '"MaxMultiSelect=2"')))
    case = make_case(count=3, registered=True, fame=narrow)
    assert case.service.status()["maximum_deposit_batch"] == 2
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    fail(case, message(case, 150), "invalid_request_fields")
    assert logged(caplog) == []
    call(case, message(case, 150, indices=[1, 2]))
    assert [unit["GetIndex"] for unit in case.store.profile().state["player_state"]["units"]] == [0, 3]


@pytest.mark.parametrize("operation", [150, 151])
def test_refused_storage_operations_are_logged(make_case, caplog, operation):
    case = make_case()
    if operation == 151:
        stock(case)
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    fail(case, message(case, operation), "fame_registration_required")
    assert logged(caplog) == [f"Fame operation {operation} refused with fame_registration_required; {RESENDS}"]


def test_malformed_requests_are_not_reported_as_native_refusals(make_case, caplog):
    case = make_case()
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    fail(case, message(case, 150, columns=(Container("DEPOSIT", (Column("InviteId", "I", ()),)),)),
         "invalid_request_fields")
    assert logged(caplog) == []


def test_legacy_watermark_history_upgrades_and_replays(make_case):
    case = make_case(count=2)
    envelope = message(case, indices=[1], tick=2000)
    response = call(case, envelope)
    def legacy(state):
        receipt, = state["fame_history"]["receipts"]
        state["fame_history"] = {"schema_version": 1, "last_client_time": 2000, "seen_keys": [receipt["request"]],
                                 "receipts": [{**receipt, "client_time": 2000}]}
    overwrite(case, legacy)
    before = case.store.profile()
    assert call(case, envelope) == response
    assert case.store.profile() == before
    call(case, message(case, indices=[2], tick=1000))
    history = case.store.profile().state["fame_history"]
    assert history["schema_version"] == 2 and len(history["processed_keys"]) == 2


def test_concurrent_withdrawals_cannot_duplicate_last_unit(make_case):
    case = make_case(registered=True)
    stock(case)
    before = case.store.profile()
    envelopes = [message(case, 151, tick=tick) for tick in (1000, 1001)]
    def invoke(envelope):
        try:
            return call(case, envelope)
        except ProtocolError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, envelopes))
    assert sum(isinstance(result, bytes) for result in results) == 1
    after = case.store.profile()
    assert after.revision == before.revision + 1
    assert len(after.state["player_state"]["units"]) == len(before.state["player_state"]["units"]) + 1
    assert case.repository.read(after.state)[0][f"Count{case.selected[0]['ClassChangeCount']}"] == 0


def test_legacy_over_limit_bucket_can_shrink_but_not_receive_more(make_case):
    case = make_case(registered=True)
    card, stage = case.selected[0]["CardID"], case.selected[0]["ClassChangeCount"]
    def over_limit(state):
        state["fame_depository_schema"] = 1
        state["tables"]["fame_depository"] = [{"CardId": card,
            **{f"Count{index}": case.catalog.deposit_limit + 1 if index == stage else 0 for index in range(5)}}]
    mutate(case, over_limit)
    fail(case, message(case, 150), "fame_stock_capacity")
    call(case, message(case, 151))
    assert case.repository.read(case.store.profile().state)[0][f"Count{stage}"] == case.catalog.deposit_limit


def test_deleting_legacy_highest_identity_does_not_allow_reuse(make_case):
    case = make_case(registered=True)
    def legacy(state):
        player = state["player_state"]
        player.pop("last_unique_id", None)
        player["units"][1]["UniqueID"] = 900
        player["battle"]["UX"] = 100
    mutate(case, legacy)
    call(case, message(case, 150))
    assert case.store.profile().state["player_state"]["last_unique_id"] == 900
    call(case, message(case, 151, tick=1001))
    unit = case.store.profile().state["player_state"]["units"][-1]
    assert unit["UniqueID"] == 901 and unit["GetIndex"] == 101


def test_maximum_template_is_checked_against_active_reward_reservations(make_case, monkeypatch):
    from aigis_ps.quest_service import QuestService
    case = make_case(registered=True)
    stock(case)
    mutate(case, lambda state: state["tables"]["quest_records"].append({"QuestID": 1001, "MaxRank": 3,
        "AchieveFlag": 0x01000000, "RemainAssault": 0, "FirstDefeatDate": 0, "Shown": False}))
    quest = QuestService(case.bundle.quests.catalog, case.bootstrap, case.bundle.player_state,
                         case.bundle.possessions, clock=lambda: NOW, randbelow=lambda _: 0)
    quest.start(start_request(quest, case.store, 1002), case.store)
    before = case.store.profile()
    original = case.bundle.player_state.credit_units
    checked = []
    def credit(state, grants):
        player = state["player_state"]
        if (state.get("quest_state", {}).get("active") is None
                and len(player["units"]) == len(before.state["player_state"]["units"]) + 1
                and player["units"][-1]["Famed"]):
            checked.append(deepcopy(player["units"][-1]))
            raise ProtocolError("test_final_reservation", "Maximum template reservation failed", 409)
        return original(state, grants)
    monkeypatch.setattr(case.bundle.player_state, "credit_units", credit)
    fail(case, message(case, 151, tick=2000), "test_final_reservation")
    assert len(checked) == 1
    assert checked[0]["Exp"] == case.catalog.maximum_values(17, case.selected[0]["ClassChangeCount"])["Exp"]


@pytest.mark.parametrize("missing", ["catalog", "missions", "barracks"])
def test_missing_dependencies_fail_explicitly_without_writing(make_case, missing):
    case = make_case()
    inputs = {name: getattr(case.service, name) for name in (
        "bootstrap", "players", "repository", "catalog", "card_status", "gifts",
        "evolution_rewards", "missions", "barracks")}
    inputs[missing] = None
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        FameService(**inputs)
    assert caught.value.code == "fame_dependencies_invalid" and caught.value.status == 503
    assert case.store.profile() == before


def test_readiness_asks_missions_without_building_their_status(make_case, monkeypatch):
    case = make_case()
    monkeypatch.setattr(case.missions, "status", lambda: pytest.fail("Fame readiness built the full mission status"))
    case.service._ready()
    def unready():
        raise ProtocolError("mission_dependencies_changed", "Mission dependencies changed after setup", 503)
    monkeypatch.setattr(case.missions, "_ready", unready)
    assert (case.service.status()["ready"], case.service.status()["error"]) == (False, "fame_dependencies_changed")
    fail(case, message(case), "fame_dependencies_changed")


def test_readiness_asks_catalog_and_repository_without_building_their_status(make_case, monkeypatch):
    case = make_case()
    for dependency in (case.catalog, case.repository):
        monkeypatch.setattr(dependency, "status", lambda: pytest.fail("Fame readiness built a dependency status"))
    assert case.service.is_ready()
    monkeypatch.setattr(case.repository, "is_ready", lambda: False)
    assert not case.service.is_ready()
    fail(case, message(case), "fame_dependencies_changed")


def test_removed_dependency_is_reported_by_status_and_blocks_transaction(make_case):
    case = make_case()
    case.service.missions = None
    assert case.service.status()["ready"] is False
    fail(case, message(case), "fame_dependencies_changed")


def test_imported_stock_without_registration_cannot_create_a_famed_unit(make_case):
    case = make_case()
    stock(case)
    fail(case, message(case, 151), "fame_registration_required")


def test_full_technical_roster_can_register_when_native_wire_capacity_allows(make_case):
    case = make_case()
    def populate(state):
        player = state["player_state"]
        template = player["units"][1]
        player["units"] = [player["units"][0], *({**template, "GetIndex": index, "UniqueID": index + 1}
                                                 for index in range(1, MAX_PLAYER_UNITS))]
        player["battle"]["UX"] = MAX_PLAYER_UNITS - 1
        player["last_unique_id"] = MAX_PLAYER_UNITS
    mutate(case, populate)
    selected = tuple(range(1, MAX_PLAYER_UNITS))
    response = call(case, message(case, indices=selected))
    root = loads(response, limits=fame_module.FAME_LIMITS)
    groups = {node.name: node for node in root.children}
    assert len(response) > 256 * 1024
    assert groups["FameRegister"].children[0].values == selected
    assert all(unit["Famed"] for unit in case.store.profile().state["player_state"]["units"][1:])
