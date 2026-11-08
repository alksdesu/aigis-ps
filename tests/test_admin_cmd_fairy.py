from collections.abc import Callable
from copy import copy, deepcopy
from dataclasses import dataclass, replace
from functools import cache, partial
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aigis_ps import admin_cmd_fairy
from aigis_ps.admin_cmd_fairy import COMMANDS, FAIRY_FARM
from aigis_ps.admin_cmd_storage import STORAGE
from aigis_ps.admin_command import AdminError, missing_service
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, discover
from aigis_ps.alxml import Column, Container
from aigis_ps.barracks_service import EXTRA_SHIFT, PURCHASE_FIELD
from aigis_ps.errors import ProtocolError
from aigis_ps.fairy_farm import (BULK_DEPOSIT, DEPOSIT, HISTORY_NAMESPACE as FAIRY_HISTORY, LOAD, WITHDRAW,
                                 FairyFarmService)
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.player_state import BARRACKS_BITS, BARRACKS_SHIFT, LOCK_FLAG, MAX_PLAYER_UNITS, PRINCE_CARD_IDS
from aigis_ps.unit_reference import FIRST_BARRACKS
from test_admin_cmd_storage import (GIFT_LEDGER, OTHER_KEY, UNKNOWN_SCHEMA, UNOWNED_INDEX, asset_reader, canonical,
                                    commit, console, envelope, initialized, ledgers, native_state, outcome, planned,
                                    preview_error, refusal, squad_row, unit_at, without)
from test_admin_seams import MemoryStore
from test_quest_service import bundle


GOLDEN = Path(__file__).resolve().parent / "golden" / "p22b"
CHANGED_RULES = "f" * 64
BONDED_LOVE = 150


@dataclass(frozen=True)
class FairyServices:
    bundle: Any
    farm: FairyFarmService


def warehouse(bundle: Any, barracks: Any, players: Any = None) -> FairyFarmService:
    return FairyFarmService(bundle.bootstrap, bundle.player_state if players is None else players, bundle.gifts,
                            asset_reader(bundle), barracks=barracks)


def fairy_services(bundle: Any) -> FairyServices:
    # Production wiring hands the warehouse the verified barracks capacity, so extra barracks deposits are reachable.
    return FairyServices(bundle, warehouse(bundle, bundle.unit_capacity))


def primary_only(services: FairyServices) -> FairyFarmService:
    return warehouse(services.bundle, None)


def changed_rules(services: FairyServices) -> FairyFarmService:
    capacity = services.bundle.unit_capacity
    rules = SimpleNamespace(content_version=capacity.content_version, usage=capacity.usage)
    farm = warehouse(services.bundle, rules)
    rules.content_version = CHANGED_RULES
    return farm


def full_roster(services: FairyServices) -> FairyFarmService:
    players = copy(services.bundle.player_state)
    players.capacity_provider = lambda state: 0
    return warehouse(services.bundle, services.bundle.unit_capacity, players)


def ordinary_card(services: FairyServices) -> int:
    return next(identifier for identifier in sorted(services.bundle.gifts.catalog.card_ids)
                if identifier not in services.farm.card_ids and identifier not in PRINCE_CARD_IDS)


def bonded_card(services: FairyServices) -> int:
    catalog = services.bundle.gifts.catalog
    return next(identifier for identifier in sorted(catalog.card_ids)
                if identifier not in PRINCE_CARD_IDS and catalog.card(identifier)["BonusType3"])


def place(unit: dict, barracks: int) -> None:
    unit["GeneralPurposeFlags"] = (unit["GeneralPurposeFlags"] & ~BARRACKS_BITS) | barracks << BARRACKS_SHIFT


Amount = int | Callable[[FairyFarmService], int]


def amount(value: Amount, farm: FairyFarmService) -> int:
    return value(farm) if callable(value) else value


def full(farm: FairyFarmService) -> int:
    return farm.cap


def nearly_full(farm: FairyFarmService) -> int:
    return farm.cap - 1


def beyond_capacity(farm: FairyFarmService) -> int:
    return farm.cap + 1


@dataclass
class FairySave:
    state: dict
    card_id: int
    ordinary_id: int
    fairies: list[dict]
    others: list[dict]


@dataclass(frozen=True)
class FairyCase:
    operation: int
    count: int = 2
    ordinary: int = 0
    stock: Amount = 0
    extra: int = 0
    placed: tuple[tuple[int, int], ...] = ()
    prepare: Callable[[FairyServices, FairySave], None] | None = None
    select: Callable[[FairySave], tuple[tuple[int, int], ...]] | None = None
    scope: int | None = None
    drawn: Amount = 1
    card: Callable[[FairySave], int] | None = None
    columns: Callable[[FairySave], tuple] | None = None
    farm: Callable[[FairyServices], FairyFarmService] | None = None
    user_key: str | None = None
    replay: bool = False


def fairy_save(services: FairyServices, case: FairyCase, farm: FairyFarmService) -> FairySave:
    players = services.bundle.player_state
    card_id, ordinary_id = farm.card_ids[0], ordinary_card(services)
    state = players.initialize(services.bundle.bootstrap.initial_state())
    state["tables"]["config"][0][PURCHASE_FIELD] |= case.extra << EXTRA_SHIFT
    state, added = players.credit_units(state, [{"card_id": identifier, "level": 1, "count": count}
                                                for identifier, count in ((card_id, case.count), (ordinary_id, case.ordinary))
                                                if count])
    for position, barracks in case.placed:
        place(unit_at(state, added[position]["GetIndex"]), barracks)
    stock = amount(case.stock, farm)
    if stock:
        state["fairy_farm_schema"] = 1
        state["tables"]["fairy_farm"] = [{"CardID": card_id, "Count": stock}]
    units = [unit_at(state, created["GetIndex"]) for created in added]
    save = FairySave(state, card_id, ordinary_id, units[:case.count], units[case.count:])
    if case.prepare is not None:
        case.prepare(services, save)
    return save


def pairs(units: list[dict]) -> tuple[tuple[int, int], ...]:
    return tuple((unit["GetIndex"], unit["UniqueID"]) for unit in units)


def unowned(save: FairySave) -> tuple[tuple[int, int], ...]:
    return ((UNOWNED_INDEX, UNOWNED_INDEX + 1),)


def mismatched(save: FairySave) -> tuple[tuple[int, int], ...]:
    return ((save.fairies[0]["GetIndex"], save.fairies[1]["UniqueID"]),)


def repeated(save: FairySave) -> tuple[tuple[int, int], ...]:
    return pairs(save.fairies[:1]) * 2


def ordinary_units(save: FairySave) -> tuple[tuple[int, int], ...]:
    return pairs(save.others)


def unowned_then_fairies(save: FairySave) -> tuple[tuple[int, int], ...]:
    return (*unowned(save), *pairs(save.fairies))


def fairies_then_ordinary(save: FairySave) -> tuple[tuple[int, int], ...]:
    return pairs(save.fairies + save.others)


def ordinary_kind(save: FairySave) -> int:
    return save.ordinary_id


def extra_field(save: FairySave) -> tuple:
    return (Column("Extra", "I", (0,)),)


def textual_index(save: FairySave) -> tuple:
    return (Container("BRK", (Column("IDX", "S", (str(FIRST_BARRACKS),)),)),)


def textual_card(save: FairySave) -> tuple:
    return (Container("Draw", (Column("CardID", "S", (str(save.card_id),)), Column("Count", "I", (1,)))),)


def organized_first(services: FairyServices, save: FairySave) -> None:
    save.state["player_state"]["squads"].append(squad_row(save.fairies[0]["GetIndex"]))


def organized_last(services: FairyServices, save: FairySave) -> None:
    save.state["player_state"]["squads"].append(squad_row(save.fairies[-1]["GetIndex"]))


def organized_ordinary(services: FairyServices, save: FairySave) -> None:
    save.state["player_state"]["squads"].append(squad_row(save.others[0]["GetIndex"]))


def locked_and_grown_first(services: FairyServices, save: FairySave) -> None:
    unit = unit_at(save.state, save.fairies[0]["GetIndex"])
    unit.update(GeneralPurposeFlags=unit["GeneralPurposeFlags"] | LOCK_FLAG, Exp=1, Love=1)


def locked_organized_and_grown(services: FairyServices, save: FairySave) -> None:
    locked, organized, grown = (unit_at(save.state, unit["GetIndex"]) for unit in save.fairies[:3])
    locked["GeneralPurposeFlags"] |= LOCK_FLAG
    save.state["player_state"]["squads"].append(squad_row(organized["GetIndex"]))
    grown["Exp"] = 1


def second_kind(services: FairyServices, save: FairySave) -> None:
    other = services.farm.card_ids[1]
    save.state, _ = services.bundle.player_state.credit_units(save.state, [{"card_id": other, "level": 1, "count": 2}])


def bonded_unit(services: FairyServices, save: FairySave) -> None:
    save.state, added = services.bundle.player_state.credit_units(
        save.state, [{"card_id": bonded_card(services), "level": 1, "count": 1}])
    unit_at(save.state, added[0]["GetIndex"])["Love"] = BONDED_LOVE


def unknown_schema(services: FairyServices, save: FairySave) -> None:
    save.state["fairy_farm_schema"] = UNKNOWN_SCHEMA


def legacy_battle(services: FairyServices, save: FairySave) -> None:
    save.state["quest_state"] = {"active": {"sequence": 1}}


def exhausted_identity(services: FairyServices, save: FairySave) -> None:
    save.state["player_state"]["last_unique_id"] = INT32_MAX


def fairy_columns(case: FairyCase, save: FairySave, farm: FairyFarmService) -> tuple:
    if case.columns is not None:
        return case.columns(save)
    if case.operation == DEPOSIT:
        selected = pairs(save.fairies) if case.select is None else case.select(save)
        return (Container("LST", (Column("A7", "I", tuple(index for index, _ in selected)),
                                  Column("AB", "I", tuple(unique for _, unique in selected)))),)
    if case.operation == BULK_DEPOSIT:
        return () if case.scope is None else (Container("BRK", (Column("IDX", "I", (case.scope,)),)),)
    if case.operation == WITHDRAW:
        card_id = save.card_id if case.card is None else case.card(save)
        return (Container("Draw", (Column("CardID", "I", (card_id,)), Column("Count", "I", (amount(case.drawn, farm),)))),)
    return ()


def fairy_snapshot(state: dict) -> dict:
    player = state["player_state"]
    return {"units": player["units"], "battle": player["battle"], "last_unique_id": player.get("last_unique_id"),
            "fairy_farm": state["tables"].get("fairy_farm"), "fairy_farm_schema": state.get("fairy_farm_schema"),
            "ledgers": ledgers(state, (GIFT_LEDGER,))}


def fairy_outcome(services: FairyServices, case: FairyCase) -> dict:
    farm = services.farm if case.farm is None else case.farm(services)
    save = fairy_save(services, case, farm)
    store = MemoryStore(save.state)
    message = envelope(store, case.operation, fairy_columns(case, save, farm), case.user_key)
    handler = farm.handlers()[case.operation]
    return outcome(lambda: handler(message, store), store, fairy_snapshot, case.replay)


LOAD_CASES = {
    "load_empty": FairyCase(LOAD, count=0),
    "load_stock": FairyCase(LOAD, count=0, stock=12),
    "load_ignores_barracks_rule_changes": FairyCase(LOAD, count=0, farm=changed_rules),
    "load_unknown_schema": FairyCase(LOAD, count=0, prepare=unknown_schema),
    "load_business_fields": FairyCase(LOAD, count=0, columns=extra_field),
    "load_wrong_identity": FairyCase(LOAD, count=0, user_key=OTHER_KEY),
}
DEPOSIT_CASES = {
    "deposit": FairyCase(DEPOSIT, replay=True),
    "deposit_adds_to_stock": FairyCase(DEPOSIT, stock=3),
    "deposit_keeps_only_the_count_of_locked_or_grown_fairies": FairyCase(DEPOSIT, prepare=locked_and_grown_first),
    "deposit_keeps_trust_history": FairyCase(DEPOSIT, prepare=bonded_unit),
    "deposit_extra_barracks": FairyCase(DEPOSIT, extra=1, placed=((0, 1), (1, 1))),
    "deposit_extra_barracks_without_rules": FairyCase(DEPOSIT, extra=1, placed=((0, 1), (1, 1)), farm=primary_only),
    "deposit_first_barracks_without_rules": FairyCase(DEPOSIT, farm=primary_only),
    "deposit_unit_in_locked_barracks": FairyCase(DEPOSIT, extra=1, placed=((0, 2), (1, 2))),
    "deposit_mixed_barracks": FairyCase(DEPOSIT, extra=2, placed=((0, 1), (1, 2))),
    "deposit_organized": FairyCase(DEPOSIT, prepare=organized_first),
    "deposit_unowned": FairyCase(DEPOSIT, select=unowned),
    "deposit_mismatched_identity": FairyCase(DEPOSIT, select=mismatched),
    "deposit_ordinary_unit": FairyCase(DEPOSIT, ordinary=1, select=ordinary_units),
    "deposit_full": FairyCase(DEPOSIT, stock=nearly_full),
    "deposit_repeated_rows": FairyCase(DEPOSIT, select=repeated),
    "deposit_unowned_before_organized": FairyCase(DEPOSIT, prepare=organized_first, select=unowned_then_fairies),
    "deposit_mixed_before_organized": FairyCase(DEPOSIT, extra=2, placed=((0, 1), (1, 2)), prepare=organized_last),
    "deposit_organized_before_card_check": FairyCase(DEPOSIT, ordinary=1, prepare=organized_ordinary,
                                                     select=ordinary_units),
    "deposit_card_check_before_capacity": FairyCase(DEPOSIT, ordinary=1, stock=full, select=fairies_then_ordinary),
    "deposit_unknown_schema": FairyCase(DEPOSIT, prepare=unknown_schema),
    "deposit_identity_before_schema": FairyCase(DEPOSIT, prepare=unknown_schema, user_key=OTHER_KEY),
    "deposit_rule_change_before_identity": FairyCase(DEPOSIT, farm=changed_rules, user_key=OTHER_KEY),
    "deposit_rows_before_identity": FairyCase(DEPOSIT, select=repeated, user_key=OTHER_KEY),
}
BULK_DEPOSIT_CASES = {
    "bulk": FairyCase(BULK_DEPOSIT, replay=True),
    "bulk_first_barracks": FairyCase(BULK_DEPOSIT, scope=FIRST_BARRACKS),
    "bulk_skips_locked_organized_and_grown": FairyCase(BULK_DEPOSIT, count=4, prepare=locked_organized_and_grown),
    "bulk_fills_to_capacity": FairyCase(BULK_DEPOSIT, stock=nearly_full),
    "bulk_full_stock": FairyCase(BULK_DEPOSIT, stock=full, replay=True),
    "bulk_fills_other_kinds": FairyCase(BULK_DEPOSIT, stock=full, prepare=second_kind),
    "bulk_without_fairies": FairyCase(BULK_DEPOSIT, count=0, ordinary=1),
    "bulk_extra_barracks": FairyCase(BULK_DEPOSIT, extra=2, placed=((0, 1), (1, 2)), scope=1),
    "bulk_every_barracks": FairyCase(BULK_DEPOSIT, extra=2, placed=((0, 1), (1, 2))),
    "bulk_locked_barracks": FairyCase(BULK_DEPOSIT, extra=1, scope=2),
    "bulk_unit_in_locked_barracks": FairyCase(BULK_DEPOSIT, extra=1, placed=((0, 2),)),
    "bulk_every_barracks_without_rules": FairyCase(BULK_DEPOSIT, extra=1, placed=((0, 1),), farm=primary_only),
    "bulk_extra_barracks_without_rules": FairyCase(BULK_DEPOSIT, scope=1, farm=primary_only),
    "bulk_first_barracks_without_rules": FairyCase(BULK_DEPOSIT, extra=1, placed=((0, 1),), scope=FIRST_BARRACKS,
                                                   farm=primary_only),
    "bulk_negative_index": FairyCase(BULK_DEPOSIT, scope=-1),
    "bulk_index_beyond_range": FairyCase(BULK_DEPOSIT, scope=3),
    "bulk_textual_index": FairyCase(BULK_DEPOSIT, columns=textual_index),
    "bulk_unknown_schema": FairyCase(BULK_DEPOSIT, prepare=unknown_schema),
    "bulk_identity_before_schema": FairyCase(BULK_DEPOSIT, prepare=unknown_schema, user_key=OTHER_KEY),
    "bulk_rule_change_before_identity": FairyCase(BULK_DEPOSIT, farm=changed_rules, user_key=OTHER_KEY),
    "bulk_index_before_identity": FairyCase(BULK_DEPOSIT, scope=-1, user_key=OTHER_KEY),
}
WITHDRAW_CASES = {
    "withdraw": FairyCase(WITHDRAW, count=0, stock=2, drawn=2, replay=True),
    "withdraw_keeps_remaining_stock": FairyCase(WITHDRAW, count=0, stock=3),
    "withdraw_empty": FairyCase(WITHDRAW, count=0),
    "withdraw_short": FairyCase(WITHDRAW, count=0, stock=1, drawn=2),
    "withdraw_ordinary_card": FairyCase(WITHDRAW, count=0, stock=1, card=ordinary_kind),
    "withdraw_zero": FairyCase(WITHDRAW, count=0, stock=1, drawn=0),
    "withdraw_beyond_capacity": FairyCase(WITHDRAW, count=0, stock=full, drawn=beyond_capacity),
    "withdraw_textual_card": FairyCase(WITHDRAW, count=0, stock=1, columns=textual_card),
    "withdraw_roster_full": FairyCase(WITHDRAW, count=0, stock=1, farm=full_roster),
    "withdraw_roster_before_stock": FairyCase(WITHDRAW, count=0, farm=full_roster),
    "withdraw_legacy_battle": FairyCase(WITHDRAW, count=0, stock=1, prepare=legacy_battle),
    "withdraw_identity_exhausted": FairyCase(WITHDRAW, count=0, stock=1, prepare=exhausted_identity),
    "withdraw_unknown_schema": FairyCase(WITHDRAW, count=0, prepare=unknown_schema),
    "withdraw_rule_change_before_identity": FairyCase(WITHDRAW, count=0, stock=1, farm=changed_rules,
                                                      user_key=OTHER_KEY),
    "withdraw_identity_before_stock": FairyCase(WITHDRAW, count=0, user_key=OTHER_KEY),
    "withdraw_request_before_identity": FairyCase(WITHDRAW, count=0, card=ordinary_kind, user_key=OTHER_KEY),
}
SUITES = {"load": LOAD_CASES, "deposit": DEPOSIT_CASES, "bulk-deposit": BULK_DEPOSIT_CASES, "withdraw": WITHDRAW_CASES}
# Each suite records the native refusals its extraction must keep reachable with the same code and priority.
GOLDEN_REFUSALS = {
    "load": frozenset(("fairy_farm_state_invalid", "invalid_request_fields", "invalid_identity")),
    "deposit": frozenset(("fairy_farm_unit_missing", "fairy_farm_barrack_unsupported", "unit_capacity_barracks_unsupported",
                          "fairy_farm_mixed_barracks", "fairy_farm_unit_protected", "fairy_farm_card_unknown",
                          "fairy_farm_quantity", "invalid_request_fields", "fairy_farm_state_invalid", "invalid_identity",
                          "fairy_farm_resources_changed")),
    "bulk-deposit": frozenset(("fairy_farm_barrack_locked", "fairy_farm_barrack_unsupported",
                               "unit_capacity_barracks_unsupported", "invalid_request_fields", "fairy_farm_state_invalid",
                               "invalid_identity", "fairy_farm_resources_changed")),
    "withdraw": frozenset(("fairy_farm_quantity", "invalid_request_fields", "unit_roster_capacity",
                           "quest_reservation_unavailable", "fairy_farm_state_invalid", "fairy_farm_resources_changed",
                           "invalid_identity")),
}


@cache
def recorded(suite: str) -> dict:
    return json.loads((GOLDEN / f"{suite}.json").read_bytes())["cases"]


@pytest.fixture(scope="module")
def goldens(bundle):
    return fairy_services(bundle)


def test_golden_covers_every_case_and_native_refusal():
    for suite, cases in SUITES.items():
        assert recorded(suite).keys() == cases.keys()
        codes = {result["error"]["code"] for result in recorded(suite).values() if "error" in result}
        assert GOLDEN_REFUSALS[suite] <= codes, suite
        replays = [result["replay"] for result in recorded(suite).values() if "replay" in result]
        assert all(replay["revisions"] == 0 for replay in replays) and len(replays) == sum(
            case.replay for case in cases.values())
    assert all(result["revisions"] == 0 for result in recorded("load").values())


@pytest.mark.parametrize("suite,name", [(suite, name) for suite, cases in SUITES.items() for name in cases])
def test_warehouse_requests_match_the_recorded_golden(goldens, suite, name):
    assert canonical(fairy_outcome(goldens, SUITES[suite][name])) == canonical(recorded(suite)[name])


FAIRY_OPERATIONS = ("fairy.adjust_stock", "fairy.withdraw")
STOCK_READBACK = {"fairy": [LOAD]}
WITHDRAW_READBACK = {"fairy": [LOAD], "squads": [39, 72], "units": [41]}
WITHDRAWN, STOCKED = 2, 3


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


@pytest.fixture(scope="module")
def registry():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_fairy.register(commands)
    return commands


def console_services(services: FairyServices, **changes: Any) -> Any:
    return replace(services.bundle, **{FAIRY_FARM: services.farm, **changes})


def saved(services: FairyServices, case: FairyCase) -> FairySave:
    return initialized(services, fairy_save(services, case, services.farm))


def stock_of(farm: FairyFarmService, state: dict, card_id: int) -> int:
    return next(row["Count"] for row in farm.read(state) if row["CardID"] == card_id)


def handler_refusal(farm: FairyFarmService, operation: int, state: dict, columns: tuple) -> tuple[str, int]:
    store = MemoryStore(state)
    return refusal(partial(farm.handlers()[operation], envelope(store, operation, columns), store))


def test_fairy_commands_register_against_the_preflight_table(registry):
    assert [command.operation for command in registry] == list(FAIRY_OPERATIONS)
    catalog = discover(PREFLIGHT_DOMAINS)
    assert set(FAIRY_OPERATIONS) <= {command.operation for command in catalog.commands}
    assert "admin_cmd_fairy" in catalog.modules
    assert {command.operation: sorted(command.touched) for command in COMMANDS} == {
        "fairy.adjust_stock": ["fairy"], "fairy.withdraw": ["fairy", "squads", "units"]}
    assert all(not command.writes and not command.preflight_none for command in COMMANDS)
    assert {command.operation: command.destructive_by_params for command in COMMANDS if command.destructive} == {
        "fairy.adjust_stock": True}
    assert {command.domain for command in COMMANDS} == {STORAGE}
    with pytest.raises(ValueError):
        admin_cmd_fairy.register(registry)


@pytest.mark.parametrize("operation, params", [
    ("fairy.adjust_stock", {"card_id": 1, "delta": 1}),
    ("fairy.adjust_stock", {"card_id": INT32_MAX, "delta": 0}),
    ("fairy.adjust_stock", {"card_id": INT32_MAX, "delta": True}),
    ("fairy.adjust_stock", {"card_id": INT32_MAX, "delta": 1.0}),
    ("fairy.adjust_stock", {"card_id": INT32_MAX, "delta": INT32_MAX + 1}),
    ("fairy.adjust_stock", {"card_id": INT32_MAX}),
    ("fairy.withdraw", {"card_id": 1}),
    ("fairy.withdraw", {"card_id": INT32_MAX, "count": 0}),
    ("fairy.withdraw", {"card_id": INT32_MAX, "count": MAX_PLAYER_UNITS + 1}),
    ("fairy.withdraw", {"card_id": str(INT32_MAX)}),
    ("fairy.withdraw", {"card_id": INT32_MAX, "barracks": FIRST_BARRACKS}),
])
def test_parameters_are_strict(registry, operation, params):
    with pytest.raises(AdminError) as caught:
        registry.get(operation).parse(params)
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


def test_stock_phrase_names_the_change(registry):
    command = registry.get("fairy.adjust_stock")
    assert command.phrase(command.parse({"card_id": INT32_MAX, "delta": -WITHDRAWN})) == (
        f"ADJUST FAIRY STOCK {INT32_MAX} BY -{WITHDRAWN}")


def test_withdrawal_equals_125_without_its_receipt(goldens, preflight, registry):
    case = FairyCase(WITHDRAW, count=0, stock=STOCKED, drawn=WITHDRAWN)
    save = saved(goldens, case)
    expected = native_state(goldens.farm.withdraw, WITHDRAW, fairy_columns(case, save, goldens.farm), save.state)
    store, subject = console(console_services(goldens), save.state, preflight)
    plan = planned(subject, registry, "fairy.withdraw", {"card_id": save.card_id, "count": WITHDRAWN})
    assert plan.preflight["readback"] == WITHDRAW_READBACK and plan.confirmations == ()
    effects = plan.steps[0].effects
    assert effects["stock"] == {"CardID": save.card_id, "before": STOCKED, "after": STOCKED - WITHDRAWN}
    assert [unit["CardID"] for unit in effects["withdrawn"]] == [save.card_id] * WITHDRAWN
    commit(subject, plan)
    assert FAIRY_HISTORY not in store.state
    assert canonical(store.state) == canonical(without(expected, FAIRY_HISTORY))


def test_withdrawn_fairies_are_new_standard_instances(goldens, preflight, registry):
    case = FairyCase(DEPOSIT, count=1, prepare=locked_and_grown_first)
    save = saved(goldens, case)
    deposited = native_state(goldens.farm.deposit, DEPOSIT, fairy_columns(case, save, goldens.farm), save.state)
    store, subject = console(console_services(goldens), deposited, preflight)
    plan = planned(subject, registry, "fairy.withdraw", {"card_id": save.card_id})
    commit(subject, plan)
    drawn, = plan.steps[0].effects["withdrawn"]
    unit, original = unit_at(store.state, drawn["GetIndex"]), save.fairies[0]
    assert unit["GetIndex"] > original["GetIndex"] and unit["UniqueID"] > original["UniqueID"]
    assert {**unit, "GetIndex": 0, "UniqueID": 0} == goldens.farm.material(save.card_id)


def test_console_commits_keep_the_native_receipts(goldens, preflight, registry):
    case = FairyCase(DEPOSIT, stock=1)
    save = saved(goldens, case)
    native = MemoryStore(save.state)
    message = envelope(native, DEPOSIT, fairy_columns(case, save, goldens.farm))
    goldens.farm.deposit(message, native)
    deposited = native.profile()
    store, subject = console(console_services(goldens), deposited.state, preflight)
    commit(subject, planned(subject, registry, "fairy.withdraw", {"card_id": save.card_id, "count": WITHDRAWN}))
    plan = planned(subject, registry, "fairy.adjust_stock", {"card_id": save.card_id, "delta": WITHDRAWN})
    assert plan.confirmations == ()
    commit(subject, plan)
    assert store.state[FAIRY_HISTORY] == deposited.state[FAIRY_HISTORY]
    # Console commits move the revision, so the deposited body is refused as stale instead of applied twice.
    native.update_state(store.state, deposited.revision)
    assert refusal(partial(goldens.farm.deposit, message, native)) == ("fairy_farm_retry_stale", 409)
    assert canonical(native.profile().state) == canonical(store.state)


def test_withdrawals_follow_the_native_bounds(goldens, preflight, registry):
    save = saved(goldens, FairyCase(WITHDRAW, count=0, stock=1))
    _, subject = console(console_services(goldens), save.state, preflight)
    preview_error(subject, registry, "fairy.withdraw", {"card_id": save.card_id, "count": WITHDRAWN},
                  "fairy_farm_quantity", 409)
    preview_error(subject, registry, "fairy.withdraw", {"card_id": save.card_id, "count": beyond_capacity(goldens.farm)},
                  "invalid_request_fields", 400)
    preview_error(subject, registry, "fairy.withdraw", {"card_id": save.ordinary_id}, "invalid_request_fields", 400)


def test_withdrawals_keep_the_native_roster_checks(goldens, preflight, registry):
    save = saved(goldens, FairyCase(WITHDRAW, count=0, stock=1))
    _, subject = console(console_services(goldens, **{FAIRY_FARM: full_roster(goldens)}), save.state, preflight)
    preview_error(subject, registry, "fairy.withdraw", {"card_id": save.card_id}, "unit_roster_capacity", 409)
    legacy_battle(goldens, save)
    _, subject = console(console_services(goldens), save.state, preflight)
    preview_error(subject, registry, "fairy.withdraw", {"card_id": save.card_id}, "quest_reservation_unavailable", 409)


@pytest.mark.parametrize("stock, delta", [(0, 5), (STOCKED, -STOCKED), (full, -1)],
                         ids=["credit", "empty", "full_shrinks"])
def test_stock_adjustments_change_only_the_count(goldens, preflight, registry, stock, delta):
    save = saved(goldens, FairyCase(WITHDRAW, count=0, stock=stock))
    before = amount(stock, goldens.farm)
    store, subject = console(console_services(goldens), save.state, preflight)
    plan = planned(subject, registry, "fairy.adjust_stock", {"card_id": save.card_id, "delta": delta})
    assert plan.preflight["readback"] == STOCK_READBACK
    assert dict(plan.steps[0].effects) == {"CardID": save.card_id, "before": before, "after": before + delta}
    phrases = (f"ADJUST FAIRY STOCK {save.card_id} BY {delta:+d}",) if delta < 0 else ()
    assert [item.phrase for item in plan.confirmations] == list(phrases)
    committed = commit(subject, plan, phrases)
    assert (committed.result["backup_before_commit"] is None) == (not phrases) and len(store.backups) == len(phrases)
    assert FAIRY_HISTORY not in store.state
    assert stock_of(goldens.farm, store.state, save.card_id) == before + delta and store.state["fairy_farm_schema"] == 1
    assert canonical(store.state["player_state"]) == canonical(save.state["player_state"])


def test_stock_decreases_need_their_phrase(goldens, preflight, registry):
    save = saved(goldens, FairyCase(WITHDRAW, count=0, stock=STOCKED))
    store, subject = console(console_services(goldens), save.state, preflight)
    plan = planned(subject, registry, "fairy.adjust_stock", {"card_id": save.card_id, "delta": -WITHDRAWN})
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert caught.value.code == "admin_confirmation_required"
    assert (store.revision, store.backups) == (0, []) and stock_of(goldens.farm, store.state, save.card_id) == STOCKED


def test_stock_adjustments_keep_the_warehouse_bounds(goldens, preflight, registry):
    save = saved(goldens, FairyCase(WITHDRAW, count=0, stock=1))
    _, subject = console(console_services(goldens), save.state, preflight)
    preview_error(subject, registry, "fairy.adjust_stock", {"card_id": save.card_id, "delta": goldens.farm.cap},
                  "fairy_farm_quantity", 409)
    preview_error(subject, registry, "fairy.adjust_stock", {"card_id": save.card_id, "delta": -WITHDRAWN},
                  "fairy_farm_quantity", 409)
    preview_error(subject, registry, "fairy.adjust_stock", {"card_id": save.ordinary_id, "delta": 1},
                  "fairy_farm_changes_invalid", 400)


def test_fairy_commands_need_the_warehouse(goldens, preflight, registry):
    save = saved(goldens, FairyCase(WITHDRAW, count=0, stock=1))
    _, subject = console(console_services(goldens, **{FAIRY_FARM: None}), save.state, preflight)
    for operation, params in (("fairy.withdraw", {"card_id": save.card_id}),
                              ("fairy.adjust_stock", {"card_id": save.card_id, "delta": 1})):
        error = preview_error(subject, registry, operation, params, "admin_command_unavailable", 503)
        assert str(error) == missing_service(FAIRY_FARM)


def test_status_lists_the_native_operations_in_order(goldens):
    assert goldens.farm.status()["implemented_operations"] == [LOAD, DEPOSIT, BULK_DEPOSIT, WITHDRAW]
    assert list(goldens.farm.handlers()) == [LOAD, DEPOSIT, BULK_DEPOSIT, WITHDRAW]


def test_count_kernels_report_and_apply_the_same_counts(goldens):
    save = fairy_save(goldens, FairyCase(WITHDRAW, count=0, stock=STOCKED), goldens.farm)
    before = deepcopy(save.state)
    counts = goldens.farm.changed_counts(save.state, {save.card_id: -1})
    assert list(counts) == list(goldens.farm.card_ids)
    assert counts[save.card_id] == STOCKED - 1 and sum(counts.values()) == STOCKED - 1
    assert save.state == before
    updated = goldens.farm.adjust(save.state, {save.card_id: -1})
    assert updated["tables"]["fairy_farm"] == [{"CardID": card_id, "Count": count} for card_id, count in counts.items()]
    assert updated["fairy_farm_schema"] == 1 and save.state == before


def test_count_kernels_refuse_unknown_cards_and_empty_changes(goldens):
    save = fairy_save(goldens, FairyCase(WITHDRAW, count=0, stock=STOCKED), goldens.farm)
    before = deepcopy(save.state)
    for changes in ({save.ordinary_id: 1}, {INT32_MAX: 1}, {save.card_id: 0}, {save.card_id: True}, {True: 1},
                    {save.card_id: 1.0}, {save.card_id: INT32_MAX + 1}, [(save.card_id, 1)]):
        for kernel in (goldens.farm.changed_counts, goldens.farm.adjust):
            with pytest.raises(ProtocolError) as caught:
                kernel(save.state, changes)
            assert (caught.value.code, caught.value.status) == ("fairy_farm_changes_invalid", 400), changes
    assert save.state == before


def test_count_kernels_keep_the_capacity_and_debits_keep_their_refusals(goldens):
    save = fairy_save(goldens, FairyCase(WITHDRAW, count=0, stock=STOCKED), goldens.farm)
    for changes in ({save.card_id: -STOCKED - 1}, {save.card_id: goldens.farm.cap - STOCKED + 1}):
        with pytest.raises(ProtocolError) as caught:
            goldens.farm.changed_counts(save.state, changes)
        assert (caught.value.code, caught.value.status) == ("fairy_farm_quantity", 409)
    for costs in ({save.card_id: 0}, {save.card_id: -1}, {save.ordinary_id: 1}, {True: 1}, []):
        with pytest.raises(ProtocolError) as caught:
            goldens.farm.debit(save.state, costs)
        assert caught.value.code == "fairy_farm_cost_invalid", costs
    with pytest.raises(ProtocolError) as caught:
        goldens.farm.debit(save.state, {save.card_id: STOCKED + 1})
    assert (caught.value.code, caught.value.status) == ("fairy_farm_quantity", 409)


def test_public_kernels_validate_inputs_without_the_request_parser(goldens):
    deposit = fairy_save(goldens, FairyCase(DEPOSIT), goldens.farm)
    stored = fairy_save(goldens, FairyCase(WITHDRAW, count=0, stock=STOCKED), goldens.farm)
    (index, identity), = pairs(deposit.fairies[:1])
    calls = (lambda: goldens.farm.deposit_state(deposit.state, 0, ((index, identity), (index, identity))),
             lambda: goldens.farm.deposit_state(deposit.state, 0, ()),
             lambda: goldens.farm.deposit_state(deposit.state, 0, ((index, 0),)),
             lambda: goldens.farm.deposit_state(deposit.state, 0, ((index, True),)),
             lambda: goldens.farm.withdraw_state(stored.state, stored.card_id, 0),
             lambda: goldens.farm.withdraw_state(stored.state, stored.card_id, beyond_capacity(goldens.farm)),
             lambda: goldens.farm.withdraw_state(stored.state, stored.card_id, True),
             lambda: goldens.farm.withdraw_state(stored.state, stored.ordinary_id))
    for call in calls:
        with pytest.raises(ProtocolError) as caught:
            call()
        assert caught.value.code == "invalid_request_fields"


@pytest.mark.parametrize("name", ["deposit_keeps_trust_history", "deposit_extra_barracks"])
def test_deposit_kernel_returns_the_123_transition(goldens, name):
    case = DEPOSIT_CASES[name]
    save = fairy_save(goldens, case, goldens.farm)
    before = deepcopy(save.state)
    updated = goldens.farm.deposit_state(save.state, 0, pairs(save.fairies))
    assert save.state == before
    native = native_state(goldens.farm.deposit, DEPOSIT, fairy_columns(case, save, goldens.farm), save.state)
    assert canonical(updated) == canonical(without(native, FAIRY_HISTORY))


def test_withdrawal_kernel_returns_the_125_transition(goldens):
    case = WITHDRAW_CASES["withdraw"]
    save = fairy_save(goldens, case, goldens.farm)
    before = deepcopy(save.state)
    updated, added = goldens.farm.withdraw_state(save.state, save.card_id, amount(case.drawn, goldens.farm))
    assert save.state == before
    native = native_state(goldens.farm.withdraw, WITHDRAW, fairy_columns(case, save, goldens.farm), save.state)
    assert canonical(updated) == canonical(without(native, FAIRY_HISTORY))
    assert [unit["GetIndex"] for unit in added] == [unit["GetIndex"] for unit in native["player_state"]["units"][-len(added):]]


def test_public_kernels_keep_the_handler_gates_and_their_order(goldens):
    deposit = fairy_save(goldens, FairyCase(DEPOSIT, stock=1, prepare=organized_first), goldens.farm)
    stored = fairy_save(goldens, FairyCase(WITHDRAW, count=0, stock=1), goldens.farm)
    columns = {DEPOSIT: fairy_columns(FairyCase(DEPOSIT), deposit, goldens.farm),
               WITHDRAW: fairy_columns(FairyCase(WITHDRAW, count=0), stored, goldens.farm)}
    for save in (deposit, stored):
        unknown_schema(goldens, save)
    readable = {DEPOSIT: deposit.state, WITHDRAW: stored.state}
    broken = deepcopy(readable)
    for state in broken.values():
        state["player_state"]["schema_version"] = UNKNOWN_SCHEMA
    selected, changed = pairs(deposit.fairies), changed_rules(goldens)
    # Each save also fails every later gate, so only the earliest gate decides the refusal.
    for farm, states, expected in ((changed, broken, ("fairy_farm_resources_changed", 503)),
                                   (goldens.farm, broken, ("player_state_schema", 503)),
                                   (full_roster(goldens), readable, ("fairy_farm_state_invalid", 503))):
        kernels = {DEPOSIT: partial(farm.deposit_state, states[DEPOSIT], 0, selected),
                   WITHDRAW: partial(farm.withdraw_state, states[WITHDRAW], stored.card_id)}
        for operation, kernel in kernels.items():
            native = handler_refusal(farm, operation, states[operation], columns[operation])
            assert refusal(kernel) == native == expected
    for kernel in (partial(changed.deposit_state, broken[DEPOSIT], 0, selected * 2),
                   partial(changed.withdraw_state, broken[WITHDRAW], stored.card_id, 0)):
        assert refusal(kernel) == ("invalid_request_fields", 400)
