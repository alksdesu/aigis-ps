from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import logging
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.errors import ProtocolError
from aigis_ps.evolution_service import EVOLUTION_FIELDS, OPTIONAL_SOURCES, EvolutionService
from aigis_ps.fairy_farm import FairyFarmService
from aigis_ps.player_state import BARRACKS_SHIFT, SKILL_SWITCH_FLAG
from aigis_ps.request_ledger import HISTORY_LIMIT, LEGACY_SCHEMA_VERSION, PROCESSED_LIMIT, SCHEMA_VERSION
from aigis_ps.storage import Store
from test_quest_service import bundle, request


RESENDS = "the native client shows a communication error and resends the same request until the server accepts it"


class FixedCatalog:
    content_version = "a" * 64
    absent_versions = ()
    price = 400

    def __init__(self, possessions, orb):
        self.possessions, self.orb = possessions, orb
        self.costs = {orb: 2}
        self.change = None

    def quote(self, target, materials, operation, branch):
        values = ({"ClassID": target["ClassID"], "ClassChangeCount": 2, "Exp": 1000, "_DisplayType": 0}
                  if operation == 7 else {"SettingSkillID": 123,
                                         "GeneralPurposeFlags": target["GeneralPurposeFlags"] | SKILL_SWITCH_FLAG,
                                         "SkillEvolved": 1})
        quote = SimpleNamespace(price=self.price, items=self.costs, values=values,
                                material_assignment=tuple(range(len(materials))), reward_card_id=None)
        if self.change:
            self.change(quote)
        return quote

    def replayable_content_versions(self):
        return frozenset((self.content_version, *self.absent_versions))

    def status(self):
        return {"ready": True}


@pytest.fixture
def case(bundle, tmp_path):
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    initial, units = bundle.player_state.credit_units(initial, [
        {"card_id": 3, "level": 1, "count": 1}, {"card_id": 2, "level": 1, "count": 4}])
    orb = next(name for name, stack in bundle.possessions._definitions.items() if name.startswith("evolve_orb_") and stack >= 10)
    initial = bundle.possessions.credit(initial, {orb: 10})
    store = Store(tmp_path / "evolution.sqlite3", initial)
    catalog = FixedCatalog(bundle.possessions, orb)
    service = EvolutionService(bundle.bootstrap, bundle.player_state, catalog, bundle.gifts,
                               fairy_farm=bundle.fairy_farm, silver_house=bundle.silver_house)
    return SimpleNamespace(store=store, service=service, catalog=catalog, units=units, bundle=bundle, orb=orb)


def columns(units):
    rows = [(unit.get("StorageType", 0), unit.get("StorageIndex", 0), unit.get("GetIndex", 0),
             unit.get("UniqueID", 0), unit["CardID"], unit.get("GetIndex", 0), unit.get("UniqueID", 0)) for unit in units]
    return tuple(Column(name, "I", tuple(row[i] for row in rows)) for i, name in enumerate(
        ("STORAGETYPE", "STORAGEINDEX", "INVITEID", "UNIQUEID", "CARDID", "A7", "AB")))


def message(case, *, operation=7, tick=1000, base=None, materials=None, store=None):
    if materials is None:
        materials = case.units[1:] if operation == 7 else case.units[1:2]
    children = [Container("BASE", columns((base or case.units[0],)))]
    if materials:
        children.append(Container("APPEND", columns(materials)))
    if operation == 9:
        children.append(Column("TYPE", "I", (1,)))
    return request(store or case.store, operation, tuple(children), tick=tick)


def mutate(case, action):
    current = case.store.profile()
    state = deepcopy(current.state)
    action(state)
    case.store.update_state(state, current.revision)


def rewrite(case, action):
    state = deepcopy(case.store.profile().state)
    action(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state, ensure_ascii=True),))


def fails(case, envelope, code):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.evolve(envelope, case.store)
    assert caught.value.code == code
    assert case.store.profile() == before
    return caught.value


def counts(case):
    return {row["ItemID"]: row["Count"] for row in case.store.profile().state["tables"]["possessions"]}


def test_handlers_only_register_first_and_skill_awakening(case):
    handlers = case.service.handlers()
    assert set(handlers) == {7, 8} and handlers[7] is handlers[8]
    status = case.service.status()
    assert status["implemented_operations"] == [7, 8]
    assert status["native_exclusions"] == ["Additional barrack targets, which the native target menus never select"]
    assert status["unsupported"] == ["Additional barrack materials", "Second awakening reward mail"]
    assert fails(case, message(case, operation=9), "evolution_operation_unsupported").status == 501


@pytest.mark.parametrize("operation", [7, 8])
def test_atomic_money_orbs_materials_and_absolute_target_response(case, operation):
    before = case.store.profile()
    response = case.service.evolve(message(case, operation=operation), case.store)
    after = case.store.profile()
    assert after.revision == before.revision + 1
    assert after.state["tables"]["game_param"][0]["Money"] == 600 and counts(case)[case.orb] == 8
    remaining = after.state["player_state"]["units"]
    assert len(remaining) == (2 if operation == 7 else 5)
    expected = case.catalog.quote(case.units[0], [], operation, 0).values
    target = next(row for row in remaining if row["GetIndex"] == 1)
    assert {key: target[key] for key in expected} == expected
    for name in ("CardID", "UniqueID", "GetIndex", "Love", "WatchLoveEv", "CostModMixValue", "ClassChangeStatusMod",
                 "SkillLevel", "SkillExperience", "CostExperience"):
        assert target[name] == case.units[0][name]
    assert after.state["player_state"]["battle"] == before.state["player_state"]["battle"]
    assert after.state["player_state"]["last_unique_id"] == 6
    for table, rows in before.state["tables"].items():
        if table not in ("game_param", "possessions"):
            assert after.state["tables"][table] == rows
    data = {child.name: child for child in loads(response).children}
    assert set(data) == {"SYSTEM", "PGP", "PPU", "POSSESSION"}
    assert data["PGP"].children[1].values == ("600",)
    assert {column.name: column.values for column in data["PPU"].children} == {
        "A7": (1,), **{alias: (expected[name],) for name, alias in EVOLUTION_FIELDS[operation].items()}}
    assert data["POSSESSION"].children[0].values == (case.orb,)
    assert data["POSSESSION"].children[1].values == (8,)


def test_deleted_highest_id_is_not_reused_in_legacy_state(case):
    mutate(case, lambda state: state["player_state"].pop("last_unique_id"))
    case.service.evolve(message(case), case.store)
    _, granted = case.bundle.player_state.credit_units(case.store.profile().state,
                                                     [{"card_id": 2, "level": 1, "count": 1}])
    assert granted[0]["UniqueID"] == 7 and granted[0]["GetIndex"] == 6


def test_locked_and_organized_target_is_preserved_and_allowed(case):
    def prepare(state):
        state["player_state"]["units"][1]["GeneralPurposeFlags"] |= 2
        state["player_state"]["squads"].append({"Address": 0x10000, "UnitIndex": 1, "Name": "target", "Flags": 0})
    mutate(case, prepare)
    before = case.store.profile().state["player_state"]["squads"]
    case.service.evolve(message(case, operation=8), case.store)
    after = case.store.profile().state["player_state"]
    assert after["units"][1]["GeneralPurposeFlags"] == 7 and after["squads"] == before


@pytest.mark.parametrize("protection", ["locked", "squad", "prince"])
def test_material_protection_preserves_all_balances(case, protection):
    materials = None
    if protection == "locked":
        mutate(case, lambda state: state["player_state"]["units"][2].update(GeneralPurposeFlags=3))
    elif protection == "squad":
        mutate(case, lambda state: state["player_state"]["squads"].append(
            {"Address": 0x70000, "UnitIndex": 2, "Name": "", "Flags": 0}))
    else:
        materials = [case.store.profile().state["player_state"]["units"][0]]
    fails(case, message(case, operation=8, materials=materials), "evolution_material_protected")


@pytest.mark.parametrize("field", ["UniqueID", "CardID", "GetIndex"])
def test_seven_column_identity_requires_actual_owned_instance(case, field):
    material = {**case.units[1], field: 999999}
    fails(case, message(case, operation=8, materials=[material]), "evolution_unit_missing")


@pytest.mark.parametrize("target", [True, False])
def test_actual_ae_barracks_cannot_be_disguised_by_wire_index_zero(case, target):
    index = 1 if target else 2
    mutate(case, lambda state: state["player_state"]["units"][index].update(GeneralPurposeFlags=1 << BARRACKS_SHIFT | 1))
    fails(case, message(case, operation=8), "evolution_storage_unsupported")


def test_target_in_additional_barracks_is_refused_until_it_returns(case):
    envelope = message(case, operation=8)
    mutate(case, lambda state: state["player_state"]["units"][1].update(GeneralPurposeFlags=1 << BARRACKS_SHIFT | 1))
    fails(case, envelope, "evolution_storage_unsupported")
    mutate(case, lambda state: state["player_state"]["units"][1].update(GeneralPurposeFlags=1))
    case.service.evolve(envelope, case.store)
    assert case.store.profile().state["player_state"]["units"][1]["SkillEvolved"] == 1


def test_nonzero_wire_storage_index_and_warehouse_target_are_unsupported(case):
    fails(case, message(case, base={**case.units[0], "StorageIndex": 1}), "evolution_storage_unsupported")
    fails(case, message(case, base={"StorageType": 1, "CardID": 2}), "evolution_storage_unsupported")


def test_target_as_material_and_duplicate_materials_fail_before_transaction(case):
    before = case.store.profile()
    for materials in ([case.units[0]], [case.units[1], case.units[1]]):
        with pytest.raises(ProtocolError):
            case.service.evolve(message(case, operation=8, materials=materials), case.store)
        assert case.store.profile() == before


@pytest.mark.parametrize("price,code", [(1001, "evolution_money_insufficient"), (1000, "evolution_native_balance")])
def test_insufficient_money_and_native_zero_balance_rollback(case, price, code):
    case.catalog.price = price
    fails(case, message(case), code)


def test_exact_money_and_orb_balance_can_be_spent_when_startup_remains_valid(case):
    mutate(case, lambda state: state["tables"]["game_param"][0].update(Experience=1))
    case.catalog.price, case.catalog.costs = 1000, {case.orb: 10}
    data = loads(case.service.evolve(message(case), case.store))
    assert case.store.profile().state["tables"]["game_param"][0]["Money"] == 0
    assert counts(case)[case.orb] == 0
    assert next(child for child in data.children if child.name == "POSSESSION").children[1].values == (0,)


def test_insufficient_orbs_preserve_money_materials_and_gift_journal(case):
    case.catalog.costs = {case.orb: 11}
    fails(case, message(case), "possession_insufficient")


def add_stock(case, *, fairy=3, silver=3):
    silver_id = next(identifier for identifier in case.bundle.silver_house._card_ids
                     if case.bundle.gifts.catalog.card(identifier)["Rare"] == 3
                     and case.bundle.gifts.catalog.card(identifier)["InitClassID"] // 100 > 0)
    def add(state):
        state["fairy_farm_schema"] = state["silver_house_schema"] = 1
        state["tables"]["fairy_farm"] = [{"CardID": 136, "Count": fairy}]
        state["tables"]["silver_house"] = [{"CardID": silver_id, "Count": silver}]
    mutate(case, add)
    return silver_id


def test_mixed_warehouse_quantities_are_debited_without_unregistered_response(case):
    silver_id = add_stock(case)
    materials = [case.units[1], {"StorageType": 1, "CardID": silver_id},
                 {"StorageType": 1, "CardID": silver_id}, {"StorageType": 2, "CardID": 136}]
    response = case.service.evolve(message(case, materials=materials), case.store)
    after = case.store.profile().state
    assert case.bundle.silver_house.read(after) == [{"CardID": silver_id, "Count": 1}]
    assert next(row for row in case.bundle.fairy_farm.read(after) if row["CardID"] == 136)["Count"] == 2
    assert len(after["player_state"]["units"]) == 5
    assert not {"PFF", "PSH"} & {child.name for child in loads(response).children}


@pytest.mark.parametrize("shortage,code", [("fairy", "fairy_farm_quantity"), ("silver", "silver_house_quantity")])
def test_any_warehouse_shortage_rolls_back_whole_transaction(case, shortage, code):
    silver_id = add_stock(case, **{shortage: 0})
    materials = [case.units[1], case.units[2], {"StorageType": 1, "CardID": silver_id}, {"StorageType": 2, "CardID": 136}]
    fails(case, message(case, materials=materials), code)


def test_warehouse_shortage_logs_only_the_short_cards_without_changing_the_refusal(case, caplog):
    silver_id = add_stock(case, fairy=1, silver=1)
    mutate(case, lambda state: state["tables"]["fairy_farm"].append({"CardID": 234, "Count": 1}))
    materials = [{"StorageType": 1, "CardID": silver_id}, {"StorageType": 2, "CardID": 136},
                 {"StorageType": 2, "CardID": 136}, {"StorageType": 2, "CardID": 234}]
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        fails(case, message(case, materials=materials), "fairy_farm_quantity")
    assert [record.getMessage() for record in caplog.records if record.name == "aigis_ps"] == [
        "Evolution 7 selected 2 of card 136 from storage type 2, which holds 1",
        f"Evolution 7 refused with fairy_farm_quantity; {RESENDS}"]


def test_shortage_of_a_card_without_a_stock_row_logs_an_empty_holding(case, caplog):
    silver_id = add_stock(case)
    mutate(case, lambda state: state["tables"].update(silver_house=[]))
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        fails(case, message(case, operation=8, materials=[{"StorageType": 1, "CardID": silver_id}]),
              "silver_house_quantity")
    assert [record.getMessage() for record in caplog.records if record.name == "aigis_ps"] == [
        f"Evolution 8 selected 1 of card {silver_id} from storage type 1, which holds 0",
        f"Evolution 8 refused with silver_house_quantity; {RESENDS}"]


@pytest.mark.parametrize("storage_type,code", [(1, "silver_house_card_ineligible"), (2, "fairy_farm_card_unknown")])
def test_warehouse_type_must_name_the_warehouse_that_stores_the_card(case, storage_type, code):
    silver_id = add_stock(case)
    card_id = 136 if storage_type == 1 else silver_id
    fails(case, message(case, operation=8, materials=[{"StorageType": storage_type, "CardID": card_id}]), code)


def test_unconfigured_warehouse_is_explicitly_unsupported(case):
    case.service.fairy_farm = None
    fails(case, message(case, operation=8, materials=[{"StorageType": 2, "CardID": 136}]), "evolution_storage_unsupported")


def test_exact_retry_survives_store_and_service_reopen(case):
    envelope = message(case)
    response = case.service.evolve(envelope, case.store)
    before = case.store.profile()
    service = EvolutionService(case.bundle.bootstrap, case.bundle.player_state, case.catalog, case.bundle.gifts,
                               fairy_farm=case.bundle.fairy_farm, silver_house=case.bundle.silver_house)
    reopened = Store(case.store.path)
    assert service.evolve(envelope, reopened) == response
    assert reopened.profile() == before


def test_exact_retry_is_charged_once_under_concurrency(case):
    envelope = message(case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: case.service.evolve(envelope, case.store), range(2)))
    assert responses[0] == responses[1] and case.store.profile().revision == 1
    assert counts(case)[case.orb] == 8


def test_receipt_requires_same_revision_and_content(case):
    envelope = message(case)
    case.service.evolve(envelope, case.store)
    case.catalog.content_version = "b" * 64
    fails(case, envelope, "evolution_retry_stale")
    case.catalog.content_version = "a" * 64
    mutate(case, lambda state: state["tables"]["game_param"][0].update(Money=700))
    fails(case, envelope, "evolution_retry_stale")


def optional_sources(bundle):
    return {"fairy_farm": bundle.fairy_farm, "silver_house": bundle.silver_house,
            "rewards": bundle.evolution_rewards, "barracks": bundle.unit_capacity}


@pytest.mark.parametrize("names", [*((name,) for name in OPTIONAL_SOURCES), OPTIONAL_SOURCES])
def test_receipt_replays_after_optional_sources_attach(case, names):
    sources = optional_sources(case.bundle)
    earlier = EvolutionService(case.bundle.bootstrap, case.bundle.player_state, case.catalog, case.bundle.gifts,
                               **{name: source for name, source in sources.items() if name not in names})
    attached = EvolutionService(case.bundle.bootstrap, case.bundle.player_state, case.catalog, case.bundle.gifts,
                                **sources)
    envelope = message(case)
    response = earlier.evolve(envelope, case.store)
    saved = case.store.profile()
    assert attached._content_version() != earlier._content_version()
    assert attached.evolve(envelope, case.store) == response
    assert case.store.profile() == saved


@pytest.mark.parametrize("name", ["fairy_farm", "silver_house"])
def test_receipt_of_a_removed_source_is_stale(case, name):
    envelope = message(case)
    case.service.evolve(envelope, case.store)
    setattr(case.service, name, None)
    fails(case, envelope, "evolution_retry_stale")


def test_forged_receipt_digest_is_stale(case):
    envelope = message(case)
    case.service.evolve(envelope, case.store)
    rewrite(case, lambda state: state["evolution_history"]["receipts"][-1].update(content="f" * 64))
    fails(case, envelope, "evolution_retry_stale")


def unbarracked_fairy_farm(bundle):
    return FairyFarmService(bundle.bootstrap, bundle.player_state, bundle.gifts,
                            lambda name: bundle.native_cdn.assets.read(name)[0])


@pytest.mark.parametrize("nested", [{"fairy_farm"}, {"catalog"}, {"fairy_farm", "catalog"}],
                         ids=["fairy_farm", "catalog", "both"])
def test_receipt_replays_after_nested_optional_inputs_attach(case, nested):
    bundle = case.bundle
    assert bundle.fairy_farm.barracks is not None
    farm = unbarracked_fairy_farm(bundle) if "fairy_farm" in nested else bundle.fairy_farm
    earlier = EvolutionService(bundle.bootstrap, bundle.player_state, case.catalog, bundle.gifts, fairy_farm=farm)
    attached = EvolutionService(bundle.bootstrap, bundle.player_state, case.catalog, bundle.gifts,
                                **optional_sources(bundle))
    if "catalog" in nested:
        case.catalog.content_version = "b" * 64
    envelope = message(case)
    response = earlier.evolve(envelope, case.store)
    saved = case.store.profile()
    case.catalog.content_version, case.catalog.absent_versions = "a" * 64, ("b" * 64,)
    assert attached.evolve(envelope, case.store) == response
    assert case.store.profile() == saved


@pytest.mark.parametrize("nested", [{"catalog": "c" * 64}, {"fairy": "c" * 64}], ids=["catalog", "fairy_farm"])
def test_forged_nested_digest_is_stale(case, nested):
    envelope = message(case)
    case.service.evolve(envelope, case.store)
    case.catalog.absent_versions = ("b" * 64,)
    forged = case.service._content_version(**nested)
    rewrite(case, lambda state: state["evolution_history"]["receipts"][-1].update(content=forged))
    fails(case, envelope, "evolution_retry_stale")


def test_replay_still_validates_current_unit_schema(case):
    envelope = message(case)
    case.service.evolve(envelope, case.store)
    profile = case.store.profile()
    bad = deepcopy(profile.state)
    bad["player_state"]["units"][0]["Love"] = True
    fake_store = SimpleNamespace(profile=lambda: replace(profile, state=bad))
    with pytest.raises(ProtocolError) as error:
        case.service.evolve(envelope, fake_store)
    assert error.value.code == "player_units_invalid"


def test_authentication_is_required_even_for_a_cached_request(case, tmp_path):
    envelope = message(case)
    case.service.evolve(envelope, case.store)
    other = Store(tmp_path / "other.sqlite3", case.store.profile().state)
    with pytest.raises(ProtocolError):
        case.service.evolve(message(case, store=other), case.store)


def test_client_clock_rollback_midnight_and_same_second_requests_are_accepted(case):
    case.catalog.price, case.catalog.costs = 1, {}
    tick = 1793548799
    for material, moment in zip(case.units[1:5], (tick, tick, tick + 1, tick)):
        case.service.evolve(message(case, operation=8, materials=[material], tick=moment), case.store)
    history = case.store.profile().state["evolution_history"]
    assert history["schema_version"] == SCHEMA_VERSION and len(history["processed_keys"]) == 4
    assert all("client_time" not in receipt for receipt in history["receipts"])


def test_native_signed_client_times_never_order_requests(case):
    case.catalog.price, case.catalog.costs = 1, {}
    for material, tick in zip(case.units[1:4], (0x7FFFFFFF, -0x80000000, 0x7FFFFFFF)):
        case.service.evolve(message(case, operation=8, materials=[material], tick=tick), case.store)
    assert len(case.store.profile().state["evolution_history"]["receipts"]) == 3


def test_same_second_history_retains_evicted_request_keys(case):
    current = case.store.profile()
    updated, materials = case.bundle.player_state.credit_units(current.state,
        [{"card_id": 2, "level": 1, "count": HISTORY_LIMIT + 1}])
    case.store.update_state(updated, current.revision)
    case.catalog.price, case.catalog.costs = 1, {}
    first = message(case, operation=8, materials=materials[:1])
    for material in materials:
        case.service.evolve(message(case, operation=8, materials=[material]), case.store)
    history = case.store.profile().state["evolution_history"]
    assert len(history["receipts"]) == HISTORY_LIMIT and len(history["processed_keys"]) == HISTORY_LIMIT + 1
    fails(case, first, "evolution_request_stale")


def test_full_legacy_second_upgrades_and_accepts_an_earlier_client_time(case):
    seen = [hashlib.sha256(str(index).encode()).hexdigest() for index in range(PROCESSED_LIMIT)]
    mutate(case, lambda state: state.update(evolution_history={"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [],
                                                             "last_client_time": 1000, "seen_keys": seen}))
    case.service.evolve(message(case, tick=999), case.store)
    history = case.store.profile().state["evolution_history"]
    assert history["schema_version"] == SCHEMA_VERSION and history["processed_keys"][:-1] == seen[1:]
    mutate(case, lambda state: state["evolution_history"].update(schema_version=True))
    fails(case, message(case, tick=1001), "evolution_history_invalid")


def test_legacy_receipt_replays_after_upgrade(case):
    case.catalog.price, case.catalog.costs = 1, {}
    envelope = message(case, operation=8, materials=case.units[1:2], tick=5000)
    response = case.service.evolve(envelope, case.store)
    receipt = case.store.profile().state["evolution_history"]["receipts"][0]
    rewrite(case, lambda state: state.update(evolution_history={"schema_version": LEGACY_SCHEMA_VERSION,
        "receipts": [{**receipt, "client_time": 5000}], "last_client_time": 5000, "seen_keys": [receipt["request"]]}))
    assert case.service.evolve(envelope, case.store) == response
    case.service.evolve(message(case, operation=8, materials=case.units[2:3], tick=4999), case.store)
    history = case.store.profile().state["evolution_history"]
    assert history["processed_keys"] == [receipt["request"], history["receipts"][-1]["request"]]


@pytest.mark.parametrize("operation", [7, 8])
def test_refused_awakening_is_logged_because_the_native_client_resends_it(case, caplog, operation):
    mutate(case, lambda state: state["player_state"]["units"][2].update(GeneralPurposeFlags=3))
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        fails(case, message(case, operation=operation), "evolution_material_protected")
    assert [record.getMessage() for record in caplog.records if record.name == "aigis_ps"] == [
        f"Evolution {operation} refused with evolution_material_protected; {RESENDS}"]


@pytest.mark.parametrize("journal", [{}, None, {"schema_version": 1, "receipts": []},
    {"schema_version": 2, "receipts": [], "last_client_time": None, "seen_keys": []},
    {"schema_version": 1, "receipts": [], "last_client_time": None, "seen_keys": ["a" * 64]},
    {"schema_version": 2, "receipts": [], "processed_keys": [True]}])
def test_unknown_history_schema_is_not_silently_reset(case, journal):
    mutate(case, lambda state: state.update(evolution_history=journal))
    fails(case, message(case), "evolution_history_invalid")


@pytest.mark.parametrize("operation", [6, True, "8"])
def test_retained_receipt_with_unknown_operation_is_not_trusted(case, operation):
    case.catalog.price, case.catalog.costs = 1, {}
    case.service.evolve(message(case, operation=8, materials=case.units[1:2]), case.store)
    mutate(case, lambda state: state["evolution_history"]["receipts"][0].update(operation=operation))
    fails(case, message(case, operation=8, materials=case.units[2:3], tick=1001), "evolution_history_invalid")


@pytest.mark.parametrize("broken", ["price", "identity", "reward", "assignment", "skill_clear"])
def test_invalid_quote_fails_before_any_cost_or_deletion(case, broken):
    def change(quote):
        if broken == "price":
            quote.price = True
        elif broken == "identity":
            quote.values["UniqueID"] = 100
        elif broken == "reward":
            quote.reward_card_id = 3
        elif broken == "assignment":
            quote.material_assignment = (True,)
        else:
            quote.values["SkillEvolved"] = 0
    case.catalog.change = change
    fails(case, message(case, operation=8), "evolution_result_invalid")


@pytest.mark.parametrize("boundary", ["units", "fairy", "silver", "receipt"])
def test_all_response_preflights_precede_commit(case, boundary, monkeypatch):
    silver_id = add_stock(case)
    materials = [case.units[1], case.units[2], {"StorageType": 1, "CardID": silver_id}, {"StorageType": 2, "CardID": 136}]
    def reject(*args, **kwargs):
        raise ProtocolError("test_capacity", "test capacity failure")
    if boundary == "units":
        monkeypatch.setattr(case.bundle.player_state, "unit_response", reject)
    elif boundary == "fairy":
        monkeypatch.setattr(case.bundle.fairy_farm, "response", reject)
    elif boundary == "silver":
        monkeypatch.setattr(case.bundle.silver_house, "response", reject)
    else:
        monkeypatch.setattr("aigis_ps.evolution_service.RESPONSE_LIMIT", 1)
    fails(case, message(case, materials=materials), "evolution_response_capacity" if boundary == "receipt" else "test_capacity")


def test_lost_cas_rechecks_protection_and_preserves_competing_change(case, monkeypatch):
    update = case.store.update_state
    attempts = []
    def conflict(state, revision):
        attempts.append(revision)
        if len(attempts) == 1:
            current = case.store.profile()
            current.state["player_state"]["units"][2]["GeneralPurposeFlags"] |= 2
            current.state["tables"]["game_param"][0]["Money"] = 1200
            update(current.state, current.revision)
        return update(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    with pytest.raises(ProtocolError) as error:
        case.service.evolve(message(case), case.store)
    assert error.value.code == "evolution_material_protected"
    after = case.store.profile().state
    assert after["tables"]["game_param"][0]["Money"] == 1200 and counts(case)[case.orb] == 10
    assert len(after["player_state"]["units"]) == 6 and "evolution_history" not in after


def test_successful_cas_retry_preserves_new_balance_and_unrelated_state(case, monkeypatch):
    update = case.store.update_state
    attempted = []
    def conflict(state, revision):
        attempted.append(revision)
        if len(attempted) == 1:
            current = case.store.profile()
            current.state["tables"]["game_param"][0]["Money"] = 1200
            current.state["concurrent_marker"] = {"value": 7}
            update(current.state, current.revision)
        return update(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    case.service.evolve(message(case), case.store)
    after = case.store.profile()
    assert attempted == [0, 1] and after.revision == 2
    assert after.state["concurrent_marker"] == {"value": 7}
    assert after.state["tables"]["game_param"][0]["Money"] == 800 and counts(case)[case.orb] == 8
    assert after.state["evolution_history"]["receipts"][0]["revision"] == 2


def test_quote_receives_isolated_unit_snapshots(case, monkeypatch):
    quote = case.catalog.quote
    def calculation(target, materials, operation, branch):
        target["Love"] = 999
        materials[0]["Exp"] = 999
        return quote(target, materials, operation, branch)
    monkeypatch.setattr(case.catalog, "quote", calculation)
    case.service.evolve(message(case), case.store)
    assert case.store.profile().state["player_state"]["units"][1]["Love"] == 0


def test_whole_native_evolution_response_capacity_failure_does_not_commit(case, monkeypatch):
    from aigis_ps.protocol import encode_response
    def bounded(profile, xml):
        if profile.registration_keys == (7,):
            raise ProtocolError("response_too_large", "test native buffer")
        return encode_response(profile, xml)
    monkeypatch.setattr("aigis_ps.evolution_service.encode_response", bounded)
    fails(case, message(case), "response_too_large")


@pytest.mark.parametrize("response", ["<broken>", "\ud800"])
def test_corrupt_persisted_response_cannot_bypass_validation_on_retry(case, response):
    envelope = message(case)
    case.service.evolve(envelope, case.store)
    current = case.store.profile()
    state = deepcopy(current.state)
    state["evolution_history"]["receipts"][0].update(response=response, revision=current.revision + 1)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=?, revision=revision+1 WHERE id=1",
                           (json.dumps(state, ensure_ascii=True),))
    fails(case, envelope, "evolution_history_invalid")


def test_cas_exhaustion_returns_conflict_without_writing(case, monkeypatch):
    def reject(state, revision):
        raise ValueError("conflict")
    monkeypatch.setattr(case.store, "update_state", reject)
    fails(case, message(case), "profile_changed")


def test_deleted_legacy_affection_history_is_frozen_without_rewards(case):
    card_id = next(card for card in case.bundle.gifts.catalog.card_ids
                   if case.bundle.gifts.catalog.card(card)["BonusType3"])
    current = case.store.profile()
    state, added = case.bundle.player_state.credit_units(current.state, [{"card_id": card_id, "level": 1, "count": 1}])
    state["player_state"]["units"][-1].update(Love=150, WatchLoveEv=3)
    case.store.update_state(state, current.revision)
    case.service.evolve(message(case, operation=8, materials=added), case.store)
    after = case.store.profile().state
    assert after["gift_state"]["claims"][str(card_id)] == [1, 100, 150]
    assert after["tables"]["presents"] == current.state["tables"]["presents"]
