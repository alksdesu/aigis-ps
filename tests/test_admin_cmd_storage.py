from collections.abc import Callable
from copy import copy, deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cache, partial
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import uuid

import pytest

from aigis_ps import admin_cmd_storage
from aigis_ps.admin_cmd_storage import (COMMANDS, FAME_DEPOSIT_LIMIT, FAME_UNIT_MISSING, NOTHING_MOVED,
                                        NOTHING_REGISTERED, REGISTRATION_WRITES, SILVER_WRITES_REASON, SLOT_WRITES)
from aigis_ps.admin_command import AdminError, PlanStore, missing_service
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck
from aigis_ps.admin_diff import json_digest
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, discover
from aigis_ps.alxml import Column, Container
from aigis_ps.barracks_service import (EXTRA_SHIFT, FIRST_PURCHASE_RANK, HISTORY_NAMESPACE as BARRACKS_HISTORY,
                                       MAXIMUM_EXTRA_BARRACKS, MAXIMUM_TRANSFER, PURCHASE, PURCHASE_FIELD, RETAINED_BITS,
                                       TRANSFER, BarracksService)
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.fame_catalog import STAGES
from aigis_ps.fame_service import (DEPOSIT as FAME_DEPOSIT, HISTORY_NAMESPACE as FAME_HISTORY, REGISTER,
                                   WITHDRAW as FAME_WITHDRAW, FameService)
from aigis_ps.fame_state import COUNT_FIELDS
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.player_state import BARRACKS_BITS, BARRACKS_SHIFT, LOCK_FLAG, PRINCE_CARD_IDS
from aigis_ps.silver_house import (DEPOSIT as SILVER_DEPOSIT, HISTORY_NAMESPACE as SILVER_HISTORY, LOAD,
                                   WITHDRAW as SILVER_WITHDRAW, SilverHouseService)
from aigis_ps.unit_grants import MAX_GRANTS
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_admin_seams import MemoryStore
from test_quest_service import bundle, request


GOLDEN = Path(__file__).resolve().parent / "golden" / "p22a"
NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
PRINCE_INDEX, UNOWNED_INDEX = 0, 4000
OTHER_KEY = "c" * 32
CHANGED_CONTENT = "e" * 64
FAME_CARD, BARRACKS_CARD = 17, 7
REGISTERED_AT = 123456
BARRACKS_UNITS = 11
# Paid and free buckets interleave by date, so a purchase shows the free-first and oldest-first spending order.
STARTING_CRYSTALS = ((False, 50, 700000), (True, 15, 700002), (False, 20, 700003), (True, 5, 700001))
OTHER_GROUP_ADDRESS = 0x03020001
SILVER_LEVEL_FLOOR = 30
UNKNOWN_SCHEMA = 2
GIFT_LEDGER, AWAKENING_LEDGER, SPENDING_LEDGER = "gift_state", "evolution_rewards", "crystal_spending"


def unit_at(state: dict, index: int) -> dict:
    return next(unit for unit in state["player_state"]["units"] if unit["GetIndex"] == index)


def squad_row(index: int, address: int = 0) -> dict:
    return {"Address": address, "UnitIndex": index, "Name": "", "Flags": 0}


def clocked(bootstrap: Any) -> Any:
    result = copy(bootstrap)
    result.clock = lambda: NOW
    return result


def asset_reader(bundle: Any) -> Callable[[str], bytes]:
    return lambda name: bundle.native_cdn.assets.read(name)[0]


def ranked(bundle: Any, state: dict, rank: int) -> None:
    state["tables"]["game_param"][0].update(Rank=rank,
                                            Experience=sum(row["NextExp"] for row in bundle.bootstrap.rank_rows[:rank - 1]))


def ledgers(state: dict, names: tuple[str, ...]) -> dict:
    return {name: json_digest(state[name]) for name in names if name in state}


@dataclass(frozen=True)
class StorageServices:
    bundle: Any
    bootstrap: Any
    fame: FameService
    barracks: BarracksService
    silver: SilverHouseService


def storage_services(bundle: Any) -> StorageServices:
    # Fixed clocks keep registration stamps and the monthly spending projection inside the recorded bytes stable.
    bootstrap = clocked(bundle.bootstrap)
    fame = FameService(bootstrap, bundle.player_state, bundle.fame_repository, bundle.fame_repository.catalog,
                       bundle.card_status, bundle.gifts, bundle.evolution_rewards, bundle.missions,
                       barracks=bundle.unit_capacity)
    barracks = BarracksService(bootstrap, bundle.player_state, bundle.unit_capacity, asset_reader(bundle))
    silver = SilverHouseService(bootstrap, bundle.player_state, bundle.gifts.catalog, bundle.gifts, asset_reader(bundle))
    return StorageServices(bundle, bootstrap, fame, barracks, silver)


def envelope(store: MemoryStore, number: int, columns: tuple, user_key: str | None = None):
    sender = store if user_key is None else SimpleNamespace(profile=lambda: replace(store.profile(), user_key=user_key))
    return request(sender, number, columns)


def attempt(call: Callable[[], bytes], store: MemoryStore, snapshot: Callable[[dict], dict]) -> dict:
    before = store.profile()
    try:
        result = {"response": call().decode("utf-8")}
    except ProtocolError as error:
        result = {"error": {"code": error.code, "message": str(error), "status": error.status}}
    after = store.profile()
    return {**result, "revisions": after.revision - before.revision, "save": snapshot(after.state)}


def outcome(call: Callable[[], bytes], store: MemoryStore, snapshot: Callable[[dict], dict], replay: bool) -> dict:
    first = attempt(call, store, snapshot)
    return {**first, "replay": attempt(call, store, snapshot)} if replay else first


@dataclass
class FameSave:
    state: dict
    units: list[dict]
    stage: int


@dataclass(frozen=True)
class FameCase:
    operation: int
    count: int = 1
    registered: bool = False
    source: int = 0
    rank_shortfall: int = 0
    stock: int = 0
    stage: int | None = None
    prepare: Callable[[StorageServices, FameSave], None] | None = None
    select: Callable[[FameSave], tuple[int, ...]] | None = None
    user_key: str | None = None
    replay: bool = False


def fame_save(services: StorageServices, case: FameCase) -> FameSave:
    bundle, fame = services.bundle, services.fame
    players = bundle.player_state
    state = fame.missions.initialize(players.initialize(services.bootstrap.initial_state()))
    ranked(bundle, state, fame.catalog.rank_min - case.rank_shortfall)
    state["tables"]["config"][0][PURCHASE_FIELD] |= case.source << EXTRA_SHIFT
    state, added = players.credit_units(state, [{"card_id": FAME_CARD, "level": 1, "count": case.count}])
    stage = fame.catalog.stages(FAME_CARD)[0]
    for created in added:
        unit = unit_at(state, created["GetIndex"])
        unit.update(fame.catalog.maximum_values(FAME_CARD, stage))
        unit.update(Famed=case.registered, GeneralPurposeFlags=unit["GeneralPurposeFlags"] | case.source << BARRACKS_SHIFT)
    if case.registered:
        state = bundle.card_status.update(state, [{"CardID": FAME_CARD, f"B{stage}": REGISTERED_AT}])
    if case.stock:
        state = fame.repository.adjust(state, {(FAME_CARD, stage): case.stock})
    save = FameSave(state, [unit_at(state, created["GetIndex"]) for created in added], stage)
    if case.prepare is not None:
        case.prepare(services, save)
    return save


def every(save: FameSave) -> tuple[int, ...]:
    return tuple(unit["GetIndex"] for unit in save.units)


def prince(save: FameSave) -> tuple[int, ...]:
    return (PRINCE_INDEX,)


def unowned(save: FameSave) -> tuple[int, ...]:
    return (UNOWNED_INDEX,)


def repeated(save: FameSave) -> tuple[int, ...]:
    return (save.units[0]["GetIndex"],) * 2


def untrained_last(services: StorageServices, save: FameSave) -> None:
    unit_at(save.state, save.units[-1]["GetIndex"])["Exp"] = 0


def organized_first(services: StorageServices, save: FameSave) -> None:
    save.state["player_state"]["squads"].append(squad_row(save.units[0]["GetIndex"]))


def exhausted_identity(services: StorageServices, save: FameSave) -> None:
    save.state["player_state"]["last_unique_id"] = INT32_MAX


def legacy_battle(services: StorageServices, save: FameSave) -> None:
    save.state["quest_state"] = {"active": {"sequence": 1}}


def locked_first(services: StorageServices, save: FameSave) -> None:
    unit_at(save.state, save.units[0]["GetIndex"])["GeneralPurposeFlags"] |= LOCK_FLAG


def unfamed_last(services: StorageServices, save: FameSave) -> None:
    unit_at(save.state, save.units[-1]["GetIndex"])["Famed"] = False


def full_bucket(services: StorageServices, save: FameSave) -> None:
    save.state = services.fame.repository.adjust(save.state, {(FAME_CARD, save.stage): services.fame.catalog.deposit_limit})


def fame_selection(case: FameCase, save: FameSave) -> tuple[int, ...]:
    return (every if case.select is None else case.select)(save)


def withdrawn_stage(case: FameCase, save: FameSave) -> int:
    return save.stage if case.stage is None else case.stage


def fame_columns(case: FameCase, save: FameSave) -> tuple:
    if case.operation == FAME_WITHDRAW:
        return (Container("WITHDRAW", (Column("CardId", "I", (FAME_CARD,)),
                                       Column("EvolutionId", "I", (withdrawn_stage(case, save),)))),)
    group, name = ("FAMERS", "UnitId") if case.operation == REGISTER else ("DEPOSIT", "InviteId")
    return (Container(group, (Column(name, "I", fame_selection(case, save)),)),)


def fame_snapshot(state: dict) -> dict:
    player, tables = state["player_state"], state["tables"]
    return {"units": player["units"], "battle": player["battle"], "last_unique_id": player.get("last_unique_id"),
            "card_status": [row for row in tables.get("card_status", []) if row["CardID"] == FAME_CARD],
            "fame_depository": tables.get("fame_depository"), "fame_depository_schema": state.get("fame_depository_schema"),
            "ledgers": ledgers(state, (GIFT_LEDGER, AWAKENING_LEDGER))}


def fame_outcome(services: StorageServices, case: FameCase) -> dict:
    save = fame_save(services, case)
    store = MemoryStore(save.state)
    message = envelope(store, case.operation, fame_columns(case, save), case.user_key)
    handler = services.fame.handlers()[case.operation]
    return outcome(lambda: handler(message, store), store, fame_snapshot, case.replay)


FAME_CASES = {
    "register": FameCase(REGISTER, replay=True),
    "register_two_instances": FameCase(REGISTER, count=2),
    "register_reports_famed_again": FameCase(REGISTER, registered=True, replay=True),
    "register_fresh_instance_of_registered_stage": FameCase(REGISTER, count=2, registered=True, prepare=unfamed_last),
    "register_skips_untrained": FameCase(REGISTER, count=2, prepare=untrained_last),
    "register_untrained": FameCase(REGISTER, prepare=untrained_last),
    "register_extra_barracks": FameCase(REGISTER, source=1),
    "register_prince": FameCase(REGISTER, select=prince),
    "register_unowned": FameCase(REGISTER, select=unowned),
    "register_repeated_selection": FameCase(REGISTER, select=repeated),
    "register_rank_required": FameCase(REGISTER, rank_shortfall=1),
    "register_identity_before_rank": FameCase(REGISTER, rank_shortfall=1, user_key=OTHER_KEY),
    "deposit": FameCase(FAME_DEPOSIT, registered=True, replay=True),
    "deposit_two_instances": FameCase(FAME_DEPOSIT, count=2, registered=True),
    "deposit_keeps_locks_allowed": FameCase(FAME_DEPOSIT, registered=True, prepare=locked_first),
    "deposit_unregistered": FameCase(FAME_DEPOSIT),
    "deposit_untrained_before_registration": FameCase(FAME_DEPOSIT, prepare=untrained_last),
    "deposit_organized": FameCase(FAME_DEPOSIT, registered=True, prepare=organized_first),
    "deposit_extra_barracks": FameCase(FAME_DEPOSIT, registered=True, source=1),
    "deposit_full_bucket": FameCase(FAME_DEPOSIT, registered=True, prepare=full_bucket),
    "deposit_prince": FameCase(FAME_DEPOSIT, registered=True, select=prince),
    "deposit_rank_required": FameCase(FAME_DEPOSIT, registered=True, rank_shortfall=1),
    "withdraw": FameCase(FAME_WITHDRAW, registered=True, stock=1, replay=True),
    "withdraw_keeps_remaining_stock": FameCase(FAME_WITHDRAW, registered=True, stock=2),
    "withdraw_empty": FameCase(FAME_WITHDRAW, registered=True),
    "withdraw_unregistered": FameCase(FAME_WITHDRAW, stock=1),
    "withdraw_stage_one": FameCase(FAME_WITHDRAW, registered=True, stock=1, stage=1),
    "withdraw_identity_exhausted": FameCase(FAME_WITHDRAW, registered=True, stock=1, prepare=exhausted_identity),
    "withdraw_legacy_battle": FameCase(FAME_WITHDRAW, registered=True, stock=1, prepare=legacy_battle),
    "withdraw_rank_before_stock": FameCase(FAME_WITHDRAW, registered=True, rank_shortfall=1),
}


@dataclass
class BarracksSave:
    state: dict
    units: list[dict]


@dataclass(frozen=True)
class BarracksCase:
    operation: int
    extra: int = 0
    low: int = 0
    rank: int = FIRST_PURCHASE_RANK
    crystals: tuple[tuple[bool, int, int], ...] = STARTING_CRYSTALS
    placed: tuple[tuple[int, int], ...] = ()
    destination: int = 1
    select: Callable[[BarracksSave], list[dict]] | None = None
    prepare: Callable[[BarracksSave], None] | None = None
    columns: tuple | None = None
    cap: tuple[int, int] | None = None
    replay: bool = False


class LimitedCapacity:
    def __init__(self, capacity: Any, barracks: int, cap: int):
        self.capacity, self.barracks, self.cap = capacity, barracks, cap
        self.content_version = capacity.content_version

    def usage(self, state: dict):
        current = self.capacity.usage(state)
        capacities = list(current.capacities)
        if self.barracks < len(capacities):
            capacities[self.barracks] = self.cap
        return replace(current, capacities=tuple(capacities))


def barracks_save(services: StorageServices, case: BarracksCase) -> BarracksSave:
    bundle = services.bundle
    players = bundle.player_state
    state = players.initialize(services.bootstrap.initial_state())
    state, added = players.credit_units(state, [{"card_id": BARRACKS_CARD, "level": 1, "count": BARRACKS_UNITS}])
    ranked(bundle, state, case.rank)
    state["tables"]["crystals"] = [{"FreeFlag": free, "Num": amount, "Date": date} for free, amount, date in case.crystals]
    state["tables"]["config"][0][PURCHASE_FIELD] = case.extra << EXTRA_SHIFT | case.low
    for position, barracks in case.placed:
        unit = unit_at(state, added[position]["GetIndex"])
        unit["GeneralPurposeFlags"] = (unit["GeneralPurposeFlags"] & ~BARRACKS_BITS) | barracks << BARRACKS_SHIFT
    save = BarracksSave(state, [unit_at(state, created["GetIndex"]) for created in added])
    if case.prepare is not None:
        case.prepare(save)
    return save


def first_two(save: BarracksSave) -> list[dict]:
    return save.units[:2]


def first_ten(save: BarracksSave) -> list[dict]:
    return save.units[:10]


def prince_row(save: BarracksSave) -> list[dict]:
    return [unit_at(save.state, PRINCE_INDEX)]


def mismatched(save: BarracksSave) -> list[dict]:
    return [dict(save.units[0], UniqueID=save.units[1]["UniqueID"])]


def repeated_rows(save: BarracksSave) -> list[dict]:
    return save.units[:1] * 2


def organized_elsewhere(save: BarracksSave) -> None:
    save.state["player_state"]["squads"].append(squad_row(save.units[0]["GetIndex"], OTHER_GROUP_ADDRESS))


def organized_first_unit(save: BarracksSave) -> None:
    save.state["player_state"]["squads"].append(squad_row(save.units[0]["GetIndex"]))


def battle_without_reservations(save: BarracksSave) -> None:
    save.state["quest_state"] = {"active": {"sequence": 1}}


def textual_purchases(save: BarracksSave) -> None:
    save.state["tables"]["config"][0][PURCHASE_FIELD] = "0"


def transferred(case: BarracksCase, save: BarracksSave) -> list[dict]:
    return save.units[:1] if case.select is None else case.select(save)


def barracks_columns(case: BarracksCase, save: BarracksSave) -> tuple:
    if case.columns is not None:
        return case.columns
    if case.operation == PURCHASE:
        return ()
    selected = transferred(case, save)
    return (Column("DEST", "I", (case.destination,)), Container("BASE", (
        Column("A7", "I", tuple(unit["GetIndex"] for unit in selected)),
        Column("AB", "I", tuple(unit["UniqueID"] for unit in selected)))))


def barracks_snapshot(state: dict) -> dict:
    tables = state["tables"]
    return {"barracks": [[unit["GetIndex"], unit["GeneralPurposeFlags"]] for unit in state["player_state"]["units"]],
            "purchases": tables["config"][0][PURCHASE_FIELD], "crystals": tables["crystals"],
            "game_param2": tables["game_param2"], "spending": state.get(SPENDING_LEDGER)}


def barracks_outcome(services: StorageServices, case: BarracksCase) -> dict:
    bundle, service = services.bundle, services.barracks
    if case.cap is not None:
        service = BarracksService(service.bootstrap, bundle.player_state, LimitedCapacity(bundle.unit_capacity, *case.cap),
                                  asset_reader(bundle))
    save = barracks_save(services, case)
    store = MemoryStore(save.state)
    message = envelope(store, case.operation, barracks_columns(case, save))
    handler = service.handlers()[case.operation]
    return outcome(lambda: handler(message, store), store, barracks_snapshot, case.replay)


BARRACKS_CASES = {
    "purchase": BarracksCase(PURCHASE, replay=True),
    "purchase_keeps_low_bits": BarracksCase(PURCHASE, low=0xFFFF),
    "purchase_second": BarracksCase(PURCHASE, extra=1),
    "purchase_second_without_rank": BarracksCase(PURCHASE, extra=1, rank=1),
    "purchase_limit": BarracksCase(PURCHASE, extra=2),
    "purchase_limit_before_rank": BarracksCase(PURCHASE, extra=2, rank=1),
    "purchase_rank_required": BarracksCase(PURCHASE, rank=FIRST_PURCHASE_RANK - 1),
    "purchase_crystals_insufficient": BarracksCase(PURCHASE, crystals=((True, 29, 1),)),
    "purchase_flags_textual": BarracksCase(PURCHASE, prepare=textual_purchases),
    "purchase_parameters": BarracksCase(PURCHASE, columns=(Column("PRICE", "I", (0,)),)),
    "transfer": BarracksCase(TRANSFER, extra=2, replay=True),
    "transfer_ten": BarracksCase(TRANSFER, extra=2, destination=2, select=first_ten),
    "transfer_back": BarracksCase(TRANSFER, extra=2, placed=((0, 1),), destination=0),
    "transfer_back_organized_legacy": BarracksCase(TRANSFER, extra=2, placed=((0, 1),), destination=0,
                                                   prepare=organized_first_unit),
    "transfer_noop": BarracksCase(TRANSFER, extra=2, placed=((0, 1),), replay=True),
    "transfer_locked": BarracksCase(TRANSFER),
    "transfer_locked_before_identity": BarracksCase(TRANSFER, select=mismatched),
    "transfer_mixed_sources": BarracksCase(TRANSFER, extra=2, placed=((0, 1),), destination=0, select=first_two),
    "transfer_extra_to_extra": BarracksCase(TRANSFER, extra=2, placed=((0, 1),), destination=2),
    "transfer_prince": BarracksCase(TRANSFER, extra=2, select=prince_row),
    "transfer_organized": BarracksCase(TRANSFER, extra=2, prepare=organized_elsewhere),
    "transfer_mismatched_identity": BarracksCase(TRANSFER, extra=2, select=mismatched),
    "transfer_capacity": BarracksCase(TRANSFER, extra=2, cap=(1, 0)),
    "transfer_battle_without_reservations": BarracksCase(TRANSFER, extra=2, prepare=battle_without_reservations),
    "transfer_repeated_identity": BarracksCase(TRANSFER, extra=2, select=repeated_rows),
    "transfer_destination_range": BarracksCase(TRANSFER, extra=2, destination=3),
}


@dataclass
class SilverSave:
    state: dict
    units: list[dict]
    card_id: int


@dataclass(frozen=True)
class SilverCase:
    operation: int
    rarity: int = 0
    level: int = 1
    count: int = 1
    stock: int = 0
    prepare: Callable[[SilverSave], None] | None = None
    select: Callable[[SilverSave], tuple[tuple[int, int], ...]] | None = None
    card: Callable[[SilverSave], int] | None = None
    full_roster: bool = False
    replay: bool = False


def silver_card(bundle: Any, rarity: int) -> int:
    catalog = bundle.gifts.catalog
    return next(identifier for identifier in sorted(catalog.card_ids)
                if identifier not in PRINCE_CARD_IDS and catalog.card(identifier)["Rare"] == rarity
                and catalog.card(identifier)["InitClassID"] // 100
                and bundle.player_state.classes[catalog.card(identifier)["InitClassID"]]["MaxLevel"] >= SILVER_LEVEL_FLOOR)


def silver_save(services: StorageServices, case: SilverCase) -> SilverSave:
    bundle = services.bundle
    players = bundle.player_state
    card_id = silver_card(bundle, case.rarity)
    state = players.initialize(services.bootstrap.initial_state())
    added = []
    if case.count:
        state, added = players.credit_units(state, [{"card_id": card_id, "level": case.level, "count": case.count}])
    if case.stock:
        state["silver_house_schema"] = 1
        state["tables"]["silver_house"] = [{"CardID": card_id, "Count": case.stock}]
    save = SilverSave(state, [unit_at(state, created["GetIndex"]) for created in added], card_id)
    if case.prepare is not None:
        case.prepare(save)
    return save


def pairs(units: list[dict]) -> tuple[tuple[int, int], ...]:
    return tuple((unit["GetIndex"], unit["UniqueID"]) for unit in units)


def unowned_pair(save: SilverSave) -> tuple[tuple[int, int], ...]:
    return ((UNOWNED_INDEX, UNOWNED_INDEX + 1),)


def unowned_then_owned(save: SilverSave) -> tuple[tuple[int, int], ...]:
    return (*unowned_pair(save), *pairs(save.units))


def repeated_pair(save: SilverSave) -> tuple[tuple[int, int], ...]:
    return pairs(save.units) * 2


def default_card(save: SilverSave) -> int:
    return 1


def locked_last(save: SilverSave) -> None:
    unit_at(save.state, save.units[-1]["GetIndex"])["GeneralPurposeFlags"] |= LOCK_FLAG


def organized_last(save: SilverSave) -> None:
    save.state["player_state"]["squads"].append(squad_row(save.units[-1]["GetIndex"]))


def evolved_last(save: SilverSave) -> None:
    unit_at(save.state, save.units[-1]["GetIndex"])["ClassChangeCount"] = 1


def deposited(case: SilverCase, save: SilverSave) -> tuple[tuple[int, int], ...]:
    return pairs(save.units) if case.select is None else case.select(save)


def withdrawn_card(case: SilverCase, save: SilverSave) -> int:
    return save.card_id if case.card is None else case.card(save)


def silver_columns(case: SilverCase, save: SilverSave) -> tuple:
    if case.operation == LOAD:
        return ()
    if case.operation == SILVER_WITHDRAW:
        return (Container("Draw", (Column("CardID", "I", (withdrawn_card(case, save),)),)),)
    selected = deposited(case, save)
    return (Container("LST", (Column("A7", "I", tuple(index for index, _ in selected)),
                              Column("AB", "I", tuple(unique for _, unique in selected)))),)


def silver_snapshot(state: dict) -> dict:
    player = state["player_state"]
    return {"units": player["units"], "battle": player["battle"], "last_unique_id": player.get("last_unique_id"),
            "silver_house": state["tables"].get("silver_house"), "silver_house_schema": state.get("silver_house_schema"),
            "ledgers": ledgers(state, (GIFT_LEDGER,))}


def silver_service(services: StorageServices, case: SilverCase) -> SilverHouseService:
    if not case.full_roster:
        return services.silver
    bundle = services.bundle
    players = copy(bundle.player_state)
    players.capacity_provider = lambda state: 0
    return SilverHouseService(services.silver.bootstrap, players, bundle.gifts.catalog, bundle.gifts, asset_reader(bundle))


def silver_outcome(services: StorageServices, case: SilverCase) -> dict:
    service = silver_service(services, case)
    save = silver_save(services, case)
    store = MemoryStore(save.state)
    message = envelope(store, case.operation, silver_columns(case, save))
    handler = service.handlers()[case.operation]
    return outcome(lambda: handler(message, store), store, silver_snapshot, case.replay)


SILVER_CASES = {
    "load_empty": SilverCase(LOAD, count=0),
    "load_stock": SilverCase(LOAD, count=0, stock=12),
    "deposit": SilverCase(SILVER_DEPOSIT, replay=True),
    "deposit_reward": SilverCase(SILVER_DEPOSIT, rarity=2, level=30),
    "deposit_two_rewards": SilverCase(SILVER_DEPOSIT, rarity=3, level=20, count=2),
    "deposit_below_reward_level": SilverCase(SILVER_DEPOSIT, rarity=2, level=29),
    "deposit_adds_to_stock": SilverCase(SILVER_DEPOSIT, stock=3),
    "deposit_locked": SilverCase(SILVER_DEPOSIT, prepare=locked_last),
    "deposit_organized": SilverCase(SILVER_DEPOSIT, prepare=organized_last),
    "deposit_evolved": SilverCase(SILVER_DEPOSIT, prepare=evolved_last),
    "deposit_unowned": SilverCase(SILVER_DEPOSIT, select=unowned_pair),
    "deposit_unowned_before_locked": SilverCase(SILVER_DEPOSIT, prepare=locked_last, select=unowned_then_owned),
    "deposit_full": SilverCase(SILVER_DEPOSIT, rarity=2, level=30, count=2, stock=199),
    "deposit_repeated": SilverCase(SILVER_DEPOSIT, select=repeated_pair),
    "withdraw": SilverCase(SILVER_WITHDRAW, count=0, stock=2, replay=True),
    "withdraw_last": SilverCase(SILVER_WITHDRAW, count=0, stock=1),
    "withdraw_empty": SilverCase(SILVER_WITHDRAW, count=0),
    "withdraw_default_card": SilverCase(SILVER_WITHDRAW, count=0, stock=1, card=default_card),
    "withdraw_roster_full": SilverCase(SILVER_WITHDRAW, count=0, stock=1, full_roster=True),
}
SUITES = {"fame": (FAME_CASES, fame_outcome), "barracks": (BARRACKS_CASES, barracks_outcome),
          "silver-house": (SILVER_CASES, silver_outcome)}
# Each suite records the native refusals its extraction must keep reachable with the same code and priority.
GOLDEN_REFUSALS = {
    "fame": frozenset(("fame_ineligible", "fame_registration_barracks", "fame_unit_protected", "fame_unit_missing",
                       "fame_rank_required", "invalid_identity", "invalid_request_fields", "fame_registration_required",
                       "fame_deposit_barracks", "fame_stock_capacity", "fame_stock_insufficient", "fame_stage_unavailable",
                       "unit_roster_capacity", "quest_reservation_unavailable")),
    "barracks": frozenset(("barracks_purchase_limit", "barracks_rank_required", "crystal_insufficient",
                           "invalid_request_fields", "barracks_locked", "barracks_mixed_sources",
                           "barracks_direction_unsupported", "barracks_prince_protected", "barracks_organized",
                           "barracks_unit_missing", "barracks_capacity_exceeded", "quest_reservation_unavailable",
                           "unit_capacity_state_invalid")),
    "silver-house": frozenset(("silver_house_unit_protected", "silver_house_unit_ineligible", "silver_house_unit_missing",
                               "silver_house_quantity", "invalid_request_fields", "unit_roster_capacity")),
}


@cache
def recorded(suite: str) -> dict:
    return json.loads((GOLDEN / f"{suite}.json").read_bytes())["cases"]


def canonical(value: object) -> str:
    # Plain equality treats True as 1, so recorded unit flags are compared through JSON text.
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@pytest.fixture(scope="module")
def goldens(bundle):
    return storage_services(bundle)


def test_golden_covers_every_case_and_native_refusal():
    for suite, (cases, _) in SUITES.items():
        assert recorded(suite).keys() == cases.keys()
        codes = {result["error"]["code"] for result in recorded(suite).values() if "error" in result}
        assert GOLDEN_REFUSALS[suite] <= codes, suite
        assert any("replay" in result and result["replay"]["revisions"] == 0 for result in recorded(suite).values())


@pytest.mark.parametrize("suite,name", [(suite, name) for suite, (cases, _) in SUITES.items() for name in cases])
def test_storage_requests_match_the_recorded_golden(goldens, suite, name):
    cases, run = SUITES[suite]
    assert canonical(run(goldens, cases[name])) == canonical(recorded(suite)[name])


STORAGE_OPERATIONS = ("barracks.grant_slot", "barracks.transfer", "fame.adjust_stock", "fame.deposit", "fame.register",
                      "fame.withdraw", "silver.adjust_stock", "silver.withdraw")
REGISTRATION_READBACK = {"card_status": [51], "missions": [54], "units": [41]}
FAME_DEPOSIT_READBACK = {"fame": [137], "units": [41]}
FAME_WITHDRAW_READBACK = {"fame": [137], "squads": [39, 72], "units": [41]}
FAME_STOCK_READBACK = {"fame": [137]}
SLOT_READBACK = {"config": [36]}
TRANSFER_READBACK = {"units": [41]}
SILVER_STOCK_READBACK = {"silver": [126]}
SILVER_WITHDRAW_READBACK = {"silver": [126], "squads": [39, 72], "units": [41]}
INELIGIBLE_SILVER_RARITY = 4
LEGACY_SILVER_STOCK = 999
WITHDRAWN_TWICE = 2
UNIT = {"get_index": 1, "unique_id": 2}


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


@pytest.fixture(scope="module")
def registry():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_storage.register(commands)
    return commands


def console_services(services: StorageServices, **changes: Any) -> Any:
    return replace(services.bundle, **{"fame": services.fame, "barracks": services.barracks,
                                       "silver_house": services.silver, **changes})


def console(services: Any, state: dict, preflight: GenerationPreflight):
    store = FakeStore(state)
    generation = FakeGeneration(id="gen-storage", bundle=services, clock=NOW)
    return store, CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(), FakeRuntime(generation),
                                 backup_directory=Path("backups"))


def initialized(services: StorageServices, save: Any) -> Any:
    # Console commits must survive a restart, so their saves start from the initialized form a restart keeps.
    save.state = services.bundle.initialize(save.state)
    return save


def planned(subject: CommitPipeline, registry: CommandRegistry, operation: str, params: dict):
    plan = subject.preview(registry.batch([{"operation": operation, "params": params}]))
    assert plan.committable, plan.describe()
    return plan


def commit(subject: CommitPipeline, plan, confirmations: tuple[str, ...] = ()):
    return subject.commit(plan_id=plan.plan_id, plan_digest=plan.plan_digest, expected_revision=plan.expected_revision,
                          op_id=str(uuid.uuid4()), confirmations=confirmations)


def preview_error(subject: CommitPipeline, registry: CommandRegistry, operation: str, params: dict, code: str,
                  status: int) -> AdminError:
    with pytest.raises(AdminError) as caught:
        subject.preview(registry.batch([{"operation": operation, "params": params}]))
    assert (caught.value.code, caught.value.status) == (code, status)
    return caught.value


def selection(units: list[dict]) -> list[dict]:
    return [{"get_index": unit["GetIndex"], "unique_id": unit["UniqueID"]} for unit in units]


def native_state(handler: Callable, number: int, columns: tuple, state: dict) -> dict:
    store = MemoryStore(state)
    handler(envelope(store, number, columns), store)
    return store.profile().state


def without(state: dict, key: str) -> dict:
    return {name: value for name, value in state.items() if name != key}


def fame_bucket(state: dict, stage: int) -> int:
    return next((row[COUNT_FIELDS[stage]] for row in state["tables"].get("fame_depository", [])
                 if row["CardId"] == FAME_CARD), 0)


def test_storage_commands_register_against_the_preflight_table(registry):
    assert [command.operation for command in registry] == list(STORAGE_OPERATIONS)
    catalog = discover(PREFLIGHT_DOMAINS)
    assert set(STORAGE_OPERATIONS) <= {command.operation for command in catalog.commands}
    assert "admin_cmd_storage" in catalog.modules
    assert {command.operation: sorted(command.touched) for command in COMMANDS} == {
        "fame.register": ["card_status", "missions", "units"],
        "fame.deposit": ["evolution_rewards", "fame", "gifts", "units"],
        "fame.withdraw": ["evolution_rewards", "fame", "gifts", "squads", "units"],
        "fame.adjust_stock": ["fame"], "barracks.grant_slot": ["config"], "barracks.transfer": ["units"],
        "silver.adjust_stock": ["silver"], "silver.withdraw": ["silver", "squads", "units"]}
    assert all(not command.preflight_none.keys() & PREFLIGHT_DOMAINS.keys() for command in COMMANDS)
    assert {command.operation for command in COMMANDS if command.destructive is True} == {"fame.deposit"}
    assert {command.operation for command in COMMANDS if command.destructive_by_params} == {
        "fame.adjust_stock", "silver.adjust_stock"}
    assert {command.domain for command in COMMANDS} == {"storage"}
    with pytest.raises(ValueError):
        admin_cmd_storage.register(registry)


def test_registration_stamps_and_the_slot_word_have_exactly_one_writer(registry):
    assert REGISTRATION_WRITES == {f"card_status.B{stage}" for stage in STAGES}
    assert SLOT_WRITES == {f"config.{PURCHASE_FIELD}"}
    expected = {**dict.fromkeys(REGISTRATION_WRITES, "fame.register"), **dict.fromkeys(SLOT_WRITES, "barracks.grant_slot")}
    assert dict(registry.field_owners()) == expected
    owners = discover(PREFLIGHT_DOMAINS).commands.field_owners()
    assert {field: owner for field, owner in owners.items() if field in expected} == expected


@pytest.mark.parametrize("operation, params", [
    ("fame.register", {"units": []}),
    ("fame.register", {"units": [UNIT, UNIT]}),
    ("fame.register", {"units": [UNIT, {"get_index": 3, "unique_id": 2}]}),
    ("fame.deposit", {"units": [{"get_index": -1, "unique_id": 2}]}),
    ("fame.withdraw", {"card_id": FAME_CARD, "stage": max(STAGES) + 1}),
    ("fame.withdraw", {"card_id": 0, "stage": min(STAGES)}),
    ("fame.withdraw", {"card_id": FAME_CARD, "stage": min(STAGES), "count": 0}),
    ("fame.withdraw", {"card_id": FAME_CARD, "stage": min(STAGES), "count": MAX_GRANTS + 1}),
    ("fame.adjust_stock", {"card_id": FAME_CARD, "stage": min(STAGES), "delta": 0}),
    ("fame.adjust_stock", {"card_id": FAME_CARD, "stage": min(STAGES), "delta": True}),
    ("fame.adjust_stock", {"card_id": FAME_CARD, "stage": min(STAGES)}),
    ("barracks.grant_slot", {"count": 1}),
    ("barracks.transfer", {"destination": MAXIMUM_EXTRA_BARRACKS + 1, "units": [UNIT]}),
    ("barracks.transfer", {"destination": 1, "units": []}),
    ("barracks.transfer", {"destination": 1, "units": [{"get_index": index, "unique_id": index + 1}
                                                       for index in range(MAXIMUM_TRANSFER + 1)]}),
    ("silver.adjust_stock", {"card_id": 1, "delta": 1}),
    ("silver.adjust_stock", {"card_id": BARRACKS_CARD, "delta": 1.0}),
    ("silver.withdraw", {"card_id": BARRACKS_CARD, "count": -1}),
])
def test_parameters_are_strict(registry, operation, params):
    with pytest.raises(AdminError) as caught:
        registry.get(operation).parse(params)
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


def test_destructive_phrases_name_the_change(registry):
    deposit, fame_stock, silver_stock = (registry.get(name) for name in
                                         ("fame.deposit", "fame.adjust_stock", "silver.adjust_stock"))
    assert deposit.phrase(deposit.parse({"units": [UNIT, {"get_index": 3, "unique_id": 4}]})) == "DEPOSIT 2 UNITS"
    assert fame_stock.phrase(fame_stock.parse({"card_id": FAME_CARD, "stage": 3, "delta": -2})) == "ADJUST FAME STOCK 17/3 BY -2"
    assert silver_stock.phrase(silver_stock.parse({"card_id": 8, "delta": 5})) == "ADJUST SILVER STOCK 8 BY +5"


@pytest.mark.parametrize("operation, params", [("fame.adjust_stock", {"card_id": FAME_CARD, "stage": 3}),
                                               ("silver.adjust_stock", {"card_id": 8})])
def test_stock_adjustments_are_destructive_only_when_they_remove_stock(registry, operation, params):
    command = registry.get(operation)
    assert command.is_destructive(command.parse({**params, "delta": -1}))
    assert not command.is_destructive(command.parse({**params, "delta": 1}))


def test_registration_equals_149_without_its_receipt(goldens, preflight, registry):
    case = FameCase(REGISTER, count=2)
    save = initialized(goldens, fame_save(goldens, case))
    expected = native_state(goldens.fame.register, REGISTER, fame_columns(case, save), save.state)
    store, subject = console(console_services(goldens), save.state, preflight)
    plan = planned(subject, registry, "fame.register", {"units": selection(save.units)})
    assert plan.preflight["readback"] == REGISTRATION_READBACK
    effects = plan.steps[0].effects
    assert [unit["GetIndex"] for unit in effects["registered"]] == [unit["GetIndex"] for unit in save.units]
    assert effects["registrations"] == [{"CardID": FAME_CARD, "stage": save.stage, "registered_at": int(NOW.timestamp())}]
    assert effects["already_registered"] == [] and effects["skipped"] == [] and plan.steps[0].warnings == ()
    commit(subject, plan)
    assert FAME_HISTORY not in store.state
    assert canonical(store.state) == canonical(without(expected, FAME_HISTORY))


def test_registration_skips_units_that_fail_their_own_check(goldens, preflight, registry):
    save = initialized(goldens, fame_save(goldens, FameCase(REGISTER, count=2, prepare=untrained_last)))
    _, subject = console(console_services(goldens), save.state, preflight)
    step = planned(subject, registry, "fame.register", {"units": selection(save.units)}).steps[0]
    skipped = save.units[-1]["GetIndex"]
    assert [unit["GetIndex"] for unit in step.effects["registered"]] == [save.units[0]["GetIndex"]]
    assert step.effects["skipped"] == [{"GetIndex": skipped, "code": "fame_ineligible"}]
    assert step.warnings == (f"GetIndex {skipped} stays unregistered: fame_ineligible",)


def test_registered_units_are_reported_again_without_new_stamps(goldens, preflight, registry):
    save = initialized(goldens, fame_save(goldens, FameCase(REGISTER, registered=True)))
    _, subject = console(console_services(goldens), save.state, preflight)
    step = planned(subject, registry, "fame.register", {"units": selection(save.units)}).steps[0]
    assert step.effects["registered"] == [] and step.effects["registrations"] == []
    assert [unit["GetIndex"] for unit in step.effects["already_registered"]] == [save.units[0]["GetIndex"]]
    assert step.warnings == (NOTHING_REGISTERED,)


def test_fame_commands_keep_the_native_rank_gate(goldens, preflight, registry):
    save = initialized(goldens, fame_save(goldens, FameCase(REGISTER, registered=True, stock=1, rank_shortfall=1)))
    _, subject = console(console_services(goldens), save.state, preflight)
    for operation, params in (("fame.register", {"units": selection(save.units)}),
                              ("fame.deposit", {"units": selection(save.units)}),
                              ("fame.withdraw", {"card_id": FAME_CARD, "stage": save.stage})):
        preview_error(subject, registry, operation, params, "fame_rank_required", 409)


def test_fame_selections_must_match_the_owned_instance(goldens, preflight, registry):
    save = initialized(goldens, fame_save(goldens, FameCase(REGISTER)))
    unit = save.units[0]
    _, subject = console(console_services(goldens), save.state, preflight)
    stale = [{"get_index": unit["GetIndex"], "unique_id": unit["UniqueID"] + 1}]
    error = preview_error(subject, registry, "fame.register", {"units": stale}, FAME_UNIT_MISSING, 409)
    assert error.details["GetIndex"] == [unit["GetIndex"]]


def test_deposit_equals_150_and_needs_its_phrase(goldens, preflight, registry):
    case = FameCase(FAME_DEPOSIT, count=2, registered=True)
    save = initialized(goldens, fame_save(goldens, case))
    expected = native_state(goldens.fame.deposit, FAME_DEPOSIT, fame_columns(case, save), save.state)
    store, subject = console(console_services(goldens), save.state, preflight)
    plan = planned(subject, registry, "fame.deposit", {"units": selection(save.units)})
    assert plan.preflight["readback"] == FAME_DEPOSIT_READBACK
    assert [item.phrase for item in plan.confirmations] == ["DEPOSIT 2 UNITS"]
    assert plan.steps[0].effects["stock"] == [{"CardId": FAME_CARD, "stage": save.stage, "before": 0, "after": 2}]
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert caught.value.code == "admin_confirmation_required" and store.revision == 0
    commit(subject, plan, ("DEPOSIT 2 UNITS",))
    assert len(store.backups) == 1 and FAME_HISTORY not in store.state
    assert canonical(store.state) == canonical(without(expected, FAME_HISTORY))


def test_deposit_takes_at_most_the_native_menu_batch(goldens, preflight, registry):
    limit = goldens.fame.deposit_batch_limit
    save = initialized(goldens, fame_save(goldens, FameCase(FAME_DEPOSIT, count=limit + 1, registered=True)))
    _, subject = console(console_services(goldens), save.state, preflight)
    error = preview_error(subject, registry, "fame.deposit", {"units": selection(save.units)},
                          FAME_DEPOSIT_LIMIT, 422)
    assert error.fields[0]["loc"] == ["units"]
    planned(subject, registry, "fame.deposit", {"units": selection(save.units[:limit])})


def test_oversized_deposits_are_refused_before_the_rank_gate_as_150_parses_them_first(goldens, preflight, registry):
    limit = goldens.fame.deposit_batch_limit
    save = initialized(goldens, fame_save(goldens, FameCase(FAME_DEPOSIT, count=limit + 1, registered=True,
                                                            rank_shortfall=1)))
    _, subject = console(console_services(goldens), save.state, preflight)
    preview_error(subject, registry, "fame.deposit", {"units": selection(save.units)}, FAME_DEPOSIT_LIMIT, 422)
    preview_error(subject, registry, "fame.deposit", {"units": selection(save.units[:limit])},
                  "fame_rank_required", 409)


def test_withdrawal_equals_151_without_its_receipt(goldens, preflight, registry):
    case = FameCase(FAME_WITHDRAW, registered=True, stock=1)
    save = initialized(goldens, fame_save(goldens, case))
    expected = native_state(goldens.fame.withdraw, FAME_WITHDRAW, fame_columns(case, save), save.state)
    store, subject = console(console_services(goldens), save.state, preflight)
    plan = planned(subject, registry, "fame.withdraw", {"card_id": FAME_CARD, "stage": save.stage})
    assert plan.preflight["readback"] == FAME_WITHDRAW_READBACK
    assert plan.steps[0].effects["stock"] == {"CardId": FAME_CARD, "stage": save.stage, "before": 1, "after": 0}
    commit(subject, plan)
    assert canonical(store.state) == canonical(without(expected, FAME_HISTORY))


def test_withdrawal_counts_repeat_the_native_rules_for_each_instance(goldens, preflight, registry):
    save = initialized(goldens, fame_save(goldens, FameCase(FAME_WITHDRAW, registered=True, stock=WITHDRAWN_TWICE)))
    params = {"card_id": FAME_CARD, "stage": save.stage, "count": WITHDRAWN_TWICE}
    _, subject = console(console_services(goldens), save.state, preflight)
    effects = planned(subject, registry, "fame.withdraw", params).steps[0].effects
    assert len({unit["UniqueID"] for unit in effects["withdrawn"]}) == WITHDRAWN_TWICE
    assert effects["stock"]["after"] == 0
    short = initialized(goldens, fame_save(goldens, FameCase(FAME_WITHDRAW, registered=True, stock=1)))
    _, subject = console(console_services(goldens), short.state, preflight)
    preview_error(subject, registry, "fame.withdraw", params, "fame_stock_insufficient", 409)


def test_fame_stock_grows_only_where_151_would_withdraw_it(goldens, preflight, registry):
    save = initialized(goldens, fame_save(goldens, FameCase(FAME_DEPOSIT, registered=True)))
    store, subject = console(console_services(goldens), save.state, preflight)
    plan = planned(subject, registry, "fame.adjust_stock", {"card_id": FAME_CARD, "stage": save.stage, "delta": 3})
    assert plan.preflight["readback"] == FAME_STOCK_READBACK
    assert dict(plan.steps[0].effects) == {"CardId": FAME_CARD, "stage": save.stage, "before": 0, "after": 3}
    assert plan.confirmations == ()
    outcome = commit(subject, plan)
    assert outcome.result["backup_before_commit"] is None and store.backups == []
    assert fame_bucket(store.state, save.stage) == 3 and FAME_HISTORY not in store.state


@pytest.mark.parametrize("case, change, code, status", [
    (FameCase(FAME_DEPOSIT), {"delta": 1}, "fame_registration_required", 409),
    (FameCase(FAME_DEPOSIT, registered=True), {"delta": -1}, "fame_stock_insufficient", 409),
    (FameCase(FAME_DEPOSIT, registered=True, prepare=full_bucket), {"delta": 1}, "fame_stock_capacity", 409),
    (FameCase(FAME_DEPOSIT, registered=True), {"stage": min(STAGES), "delta": 1}, "fame_stage_unavailable", 409),
], ids=["unregistered", "below_zero", "full_bucket", "unstored_stage"])
def test_fame_stock_adjustments_keep_the_repository_rules(goldens, preflight, registry, case, change, code, status):
    save = initialized(goldens, fame_save(goldens, case))
    _, subject = console(console_services(goldens), save.state, preflight)
    preview_error(subject, registry, "fame.adjust_stock", {"card_id": FAME_CARD, "stage": save.stage, **change},
                  code, status)


def test_fame_stock_only_holds_hall_of_fame_cards_and_full_buckets_may_shrink(goldens, preflight, registry):
    save = initialized(goldens, fame_save(goldens, FameCase(FAME_DEPOSIT, registered=True, prepare=full_bucket)))
    outsider = next(card for card in sorted(goldens.bundle.gifts.catalog.card_ids)
                    if card not in goldens.fame.catalog.card_ids | PRINCE_CARD_IDS)
    store, subject = console(console_services(goldens), save.state, preflight)
    preview_error(subject, registry, "fame.adjust_stock", {"card_id": outsider, "stage": save.stage, "delta": -1},
                  "fame_changes_invalid", 400)
    plan = planned(subject, registry, "fame.adjust_stock", {"card_id": FAME_CARD, "stage": save.stage, "delta": -1})
    assert plan.steps[0].effects["after"] == goldens.fame.catalog.deposit_limit - 1
    phrase = f"ADJUST FAME STOCK {FAME_CARD}/{save.stage} BY -1"
    assert [item.phrase for item in plan.confirmations] == [phrase]
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert caught.value.code == "admin_confirmation_required" and store.backups == []
    outcome = commit(subject, plan, (phrase,))
    assert outcome.result["backup_before_commit"] is not None and len(store.backups) == 1
    assert fame_bucket(store.state, save.stage) == goldens.fame.catalog.deposit_limit - 1


def test_slot_grants_skip_the_shop_price_and_rank(goldens, preflight, registry):
    save = initialized(goldens, barracks_save(goldens, BarracksCase(PURCHASE, low=RETAINED_BITS, rank=1)))
    store, subject = console(console_services(goldens), save.state, preflight)
    plan = planned(subject, registry, "barracks.grant_slot", {})
    assert plan.preflight["readback"] == SLOT_READBACK
    capacities = plan.steps[0].effects["capacities"]
    assert (len(capacities["before"]), len(capacities["after"])) == (1, 2)
    commit(subject, plan)
    expected = json.loads(canonical(save.state))
    expected["tables"]["config"][0][PURCHASE_FIELD] = 1 << EXTRA_SHIFT | RETAINED_BITS
    assert canonical(store.state) == canonical(expected) and BARRACKS_HISTORY not in store.state


def test_slot_grants_stop_at_the_verified_barracks(goldens, preflight, registry):
    save = initialized(goldens, barracks_save(goldens, BarracksCase(PURCHASE, extra=MAXIMUM_EXTRA_BARRACKS)))
    _, subject = console(console_services(goldens), save.state, preflight)
    preview_error(subject, registry, "barracks.grant_slot", {}, "barracks_purchase_limit", 409)


TRANSFERS = {"out": BarracksCase(TRANSFER, extra=2),
             "ten": BarracksCase(TRANSFER, extra=2, destination=2, select=first_ten),
             "back": BarracksCase(TRANSFER, extra=2, placed=((0, 1),), destination=0)}


@pytest.mark.parametrize("name", TRANSFERS)
def test_transfers_equal_118_without_its_receipt(goldens, preflight, registry, name):
    case = TRANSFERS[name]
    save = initialized(goldens, barracks_save(goldens, case))
    expected = native_state(goldens.barracks.transfer, TRANSFER, barracks_columns(case, save), save.state)
    selected = transferred(case, save)
    store, subject = console(console_services(goldens), save.state, preflight)
    plan = planned(subject, registry, "barracks.transfer", {"destination": case.destination, "units": selection(selected)})
    assert plan.preflight["readback"] == TRANSFER_READBACK
    assert [(move["GetIndex"], move["to"]) for move in plan.steps[0].effects["moved"]] == [
        (unit["GetIndex"], case.destination) for unit in selected]
    commit(subject, plan)
    assert canonical(store.state) == canonical(without(expected, BARRACKS_HISTORY))


def test_transfers_into_the_current_barracks_change_nothing(goldens, preflight, registry):
    save = initialized(goldens, barracks_save(goldens, BarracksCase(TRANSFER, extra=2, placed=((0, 1),))))
    _, subject = console(console_services(goldens), save.state, preflight)
    plan = planned(subject, registry, "barracks.transfer", {"destination": 1, "units": selection(save.units[:1])})
    assert plan.steps[0].warnings == (NOTHING_MOVED,)
    assert plan.state_sha256_before == plan.state_sha256_after


def test_transfers_keep_the_native_refusals(goldens, preflight, registry):
    locked = initialized(goldens, barracks_save(goldens, BarracksCase(TRANSFER)))
    _, subject = console(console_services(goldens), locked.state, preflight)
    preview_error(subject, registry, "barracks.transfer", {"destination": 1, "units": selection(locked.units[:1])},
                  "barracks_locked", 409)
    save = initialized(goldens, barracks_save(goldens, BarracksCase(TRANSFER, extra=2)))
    _, subject = console(console_services(goldens), save.state, preflight)
    preview_error(subject, registry, "barracks.transfer", {"destination": 1, "units": selection(prince_row(save))},
                  "barracks_prince_protected", 409)
    preview_error(subject, registry, "barracks.transfer", {"destination": 1, "units": selection(mismatched(save))},
                  "barracks_unit_missing", 409)


def test_silver_withdrawal_equals_128_without_its_receipt(goldens, preflight, registry):
    case = SilverCase(SILVER_WITHDRAW, count=0, stock=2)
    save = initialized(goldens, silver_save(goldens, case))
    expected = native_state(goldens.silver.withdraw, SILVER_WITHDRAW, silver_columns(case, save), save.state)
    store, subject = console(console_services(goldens), save.state, preflight)
    plan = planned(subject, registry, "silver.withdraw", {"card_id": save.card_id})
    assert plan.preflight["readback"] == SILVER_WITHDRAW_READBACK
    assert plan.steps[0].effects["stock"] == {"CardID": save.card_id, "before": 2, "after": 1}
    commit(subject, plan)
    assert canonical(store.state) == canonical(without(expected, SILVER_HISTORY))


def test_silver_withdrawal_counts_take_new_instances_from_the_stock(goldens, preflight, registry):
    save = initialized(goldens, silver_save(goldens, SilverCase(SILVER_WITHDRAW, count=0, stock=WITHDRAWN_TWICE)))
    _, subject = console(console_services(goldens), save.state, preflight)
    effects = planned(subject, registry, "silver.withdraw",
                      {"card_id": save.card_id, "count": WITHDRAWN_TWICE}).steps[0].effects
    assert [unit["CardID"] for unit in effects["withdrawn"]] == [save.card_id] * WITHDRAWN_TWICE
    assert effects["stock"]["after"] == 0
    preview_error(subject, registry, "silver.withdraw", {"card_id": save.card_id, "count": WITHDRAWN_TWICE + 1},
                  "silver_house_quantity", 409)


@pytest.mark.parametrize("stock, delta, after", [(0, 5, 5), (3, -3, 0), (LEGACY_SILVER_STOCK, -1, LEGACY_SILVER_STOCK - 1)],
                         ids=["credit", "empty", "legacy_over_cap"])
def test_silver_stock_adjustments_change_only_the_count(goldens, preflight, registry, stock, delta, after):
    save = initialized(goldens, silver_save(goldens, SilverCase(SILVER_WITHDRAW, count=0, stock=stock)))
    store, subject = console(console_services(goldens), save.state, preflight)
    plan = planned(subject, registry, "silver.adjust_stock", {"card_id": save.card_id, "delta": delta})
    assert plan.preflight["readback"] == SILVER_STOCK_READBACK
    assert dict(plan.steps[0].effects) == {"CardID": save.card_id, "before": stock, "after": after}
    phrases = (f"ADJUST SILVER STOCK {save.card_id} BY {delta:+d}",) if delta < 0 else ()
    assert [item.phrase for item in plan.confirmations] == list(phrases)
    outcome = commit(subject, plan, phrases)
    assert (outcome.result["backup_before_commit"] is None) == (not phrases) and len(store.backups) == len(phrases)
    assert goldens.silver.read(store.state) == [{"CardID": save.card_id, "Count": after}]
    assert canonical(store.state["player_state"]) == canonical(save.state["player_state"])
    assert SILVER_HISTORY not in store.state


def test_silver_stock_credits_follow_the_deposit_material_rules(goldens, preflight, registry):
    save = initialized(goldens, silver_save(goldens, SilverCase(SILVER_WITHDRAW, count=0, stock=1)))
    _, subject = console(console_services(goldens), save.state, preflight)
    preview_error(subject, registry, "silver.adjust_stock", {"card_id": save.card_id, "delta": goldens.silver.cap},
                  "silver_house_quantity", 409)
    preview_error(subject, registry, "silver.adjust_stock",
                  {"card_id": silver_card(goldens.bundle, INELIGIBLE_SILVER_RARITY), "delta": 1},
                  "silver_house_card_ineligible", 409)
    preview_error(subject, registry, "silver.adjust_stock", {"card_id": save.card_id, "delta": -2},
                  "silver_house_quantity", 409)
    preview_error(subject, registry, "silver.adjust_stock", {"card_id": INT32_MAX, "delta": -1},
                  "silver_house_changes_invalid", 400)


def test_silver_commands_need_the_writable_warehouse(goldens, preflight, registry):
    bundle = goldens.bundle
    save = initialized(goldens, silver_save(goldens, SilverCase(SILVER_WITHDRAW, count=0, stock=1)))
    read_only = SilverHouseService(bundle.bootstrap, bundle.player_state, bundle.gifts.catalog)
    for warehouse, reason in ((read_only, SILVER_WRITES_REASON), (None, missing_service("silver_house"))):
        _, subject = console(console_services(goldens, silver_house=warehouse), save.state, preflight)
        for operation, params in (("silver.withdraw", {"card_id": save.card_id}),
                                  ("silver.adjust_stock", {"card_id": save.card_id, "delta": 1})):
            error = preview_error(subject, registry, operation, params, "admin_command_unavailable", 503)
            assert str(error) == reason


def test_public_kernels_validate_selections_without_the_request_parser(goldens):
    fame = fame_save(goldens, FameCase(FAME_DEPOSIT, registered=True))
    index = fame.units[0]["GetIndex"]
    barracks = barracks_save(goldens, BarracksCase(TRANSFER, extra=2))
    pair = (barracks.units[0]["GetIndex"], barracks.units[0]["UniqueID"])
    silver = silver_save(goldens, SilverCase(SILVER_DEPOSIT))
    stored = (silver.units[0]["GetIndex"], silver.units[0]["UniqueID"])
    calls = (lambda: goldens.fame.deposit_units(fame.state, 0, (index, index)),
             lambda: goldens.fame.register_units(fame.state, (), NOW),
             lambda: goldens.fame.withdraw_unit(fame.state, 0, FAME_CARD, max(STAGES) + 1),
             lambda: goldens.barracks.transfer_units(barracks.state, MAXIMUM_EXTRA_BARRACKS + 1, (pair,)),
             lambda: goldens.barracks.transfer_units(barracks.state, 1, (pair, pair)),
             lambda: goldens.barracks.transfer_units(barracks.state, 1, ()),
             lambda: goldens.silver.deposit_state(silver.state, 0, (stored, stored)),
             lambda: goldens.silver.withdraw_state(silver.state, silver.card_id, 0),
             lambda: goldens.silver.withdraw_state(silver.state, 1))
    for call in calls:
        with pytest.raises(ProtocolError) as caught:
            call()
        assert caught.value.code == "invalid_request_fields"


def test_silver_count_kernels_report_and_apply_the_same_counts(goldens):
    save = silver_save(goldens, SilverCase(SILVER_WITHDRAW, count=0, stock=3))
    before = deepcopy(save.state)
    assert goldens.silver.changed_counts(save.state, {save.card_id: -1}) == {save.card_id: 2}
    assert save.state == before
    updated = goldens.silver.adjust(save.state, {save.card_id: -1})
    assert goldens.silver.read(updated) == [{"CardID": save.card_id, "Count": 2}] and save.state == before


def test_silver_count_kernels_refuse_unknown_cards_and_empty_changes(goldens):
    save = silver_save(goldens, SilverCase(SILVER_WITHDRAW, count=0, stock=3))
    before = deepcopy(save.state)
    for changes in ({INT32_MAX: 1}, {INT32_MAX: 0}, {save.card_id: 0}, {save.card_id: True}, {True: 1},
                    {save.card_id: 1.0}, [(save.card_id, 1)]):
        for kernel in (goldens.silver.changed_counts, goldens.silver.adjust):
            with pytest.raises(ProtocolError) as caught:
                kernel(save.state, changes)
            assert caught.value.code == "silver_house_changes_invalid", changes
    assert save.state == before


def refusal(call: Callable[[], Any]) -> tuple[str, int]:
    with pytest.raises(ProtocolError) as caught:
        call()
    return caught.value.code, caught.value.status


def damaged_copy(state: dict, damage: Callable[[dict], Any]) -> dict:
    copied = deepcopy(state)
    damage(copied)
    return copied


def newer_player_schema(state: dict) -> None:
    state["player_state"]["schema_version"] = UNKNOWN_SCHEMA


def negative_money(state: dict) -> None:
    state["tables"]["game_param"][0]["Money"] = -1


def changed_after_setup(service: Any, dependency: str) -> Any:
    changed = copy(service)
    setattr(changed, dependency, SimpleNamespace(content_version=CHANGED_CONTENT))
    return changed


def silver_handler_refusal(service: SilverHouseService, case: SilverCase, save: SilverSave,
                           state: dict) -> tuple[str, int]:
    store = MemoryStore(state)
    # Read-only warehouses register no 127/128 handlers, so the handler methods are called directly.
    handler = {SILVER_DEPOSIT: service.deposit, SILVER_WITHDRAW: service.withdraw}[case.operation]
    return refusal(partial(handler, envelope(store, case.operation, silver_columns(case, save)), store))


def silver_kernel_refusal(service: SilverHouseService, case: SilverCase, save: SilverSave,
                          state: dict) -> tuple[str, int]:
    if case.operation == SILVER_WITHDRAW:
        return refusal(partial(service.withdraw_state, state, withdrawn_card(case, save)))
    return refusal(partial(service.deposit_state, state, 0, deposited(case, save)))


SILVER_GATES = {
    "deposit_protected": (SilverCase(SILVER_DEPOSIT, prepare=organized_last), ("silver_house_unit_protected", 409)),
    "deposit_ineligible": (SilverCase(SILVER_DEPOSIT, prepare=evolved_last), ("silver_house_unit_ineligible", 409)),
    "withdraw_roster_full": (SilverCase(SILVER_WITHDRAW, count=0, full_roster=True), ("unit_roster_capacity", 409)),
}
UNPARSABLE = {SILVER_DEPOSIT: {"select": repeated_pair}, SILVER_WITHDRAW: {"card": default_card}}


@pytest.mark.parametrize("name", SILVER_GATES)
def test_silver_kernels_keep_the_handler_gates_and_their_order(goldens, name):
    case, unit_gate = SILVER_GATES[name]
    save, service = silver_save(goldens, case), silver_service(goldens, case)
    read_only = SilverHouseService(goldens.bootstrap, goldens.bundle.player_state, goldens.bundle.gifts.catalog)
    unparsable = replace(case, **UNPARSABLE[case.operation])
    damaged = damaged_copy(save.state, lambda state: state.update(silver_house_schema=UNKNOWN_SCHEMA))
    broken = damaged_copy(damaged, newer_player_schema)
    # Each save also fails every later gate, so only the earliest gate decides the refusal.
    for warehouse, variant, state, expected in ((read_only, unparsable, broken, ("silver_house_dependencies_unavailable", 503)),
                                                (service, unparsable, broken, ("invalid_request_fields", 400)),
                                                (service, case, broken, ("player_state_schema", 503)),
                                                (service, case, damaged, ("silver_house_state_invalid", 503)),
                                                (service, case, save.state, unit_gate)):
        native = silver_handler_refusal(warehouse, variant, save, state)
        assert silver_kernel_refusal(warehouse, variant, save, state) == native == expected


def fame_handler_refusal(service: FameService, case: FameCase, save: FameSave, state: dict) -> tuple[str, int]:
    store = MemoryStore(state)
    handler = service.handlers()[case.operation]
    return refusal(partial(handler, envelope(store, case.operation, fame_columns(case, save)), store))


def fame_kernel_refusal(service: FameService, case: FameCase, save: FameSave, state: dict) -> tuple[str, int]:
    def kernel() -> None:
        # Console commands pass admit before the kernel of their operation.
        service.admit(state)
        if case.operation == REGISTER:
            service.register_units(state, fame_selection(case, save), NOW)
        elif case.operation == FAME_DEPOSIT:
            service.deposit_units(state, 0, fame_selection(case, save))
        else:
            service.withdraw_unit(state, 0, FAME_CARD, withdrawn_stage(case, save))
    return refusal(kernel)


FAME_GATES = {
    "register_prince": (FameCase(REGISTER, select=prince), ("fame_unit_protected", 409)),
    "deposit_organized": (FameCase(FAME_DEPOSIT, registered=True, prepare=organized_first), ("fame_unit_protected", 409)),
    "withdraw_empty": (FameCase(FAME_WITHDRAW, registered=True), ("fame_stock_insufficient", 409)),
}


@pytest.mark.parametrize("name", FAME_GATES)
def test_fame_kernels_keep_the_handler_gates_and_their_order(goldens, name):
    case, unit_gate = FAME_GATES[name]
    save, fame = fame_save(goldens, case), goldens.fame
    unstatused = damaged_copy(save.state, lambda state: state.update(card_status_schema=UNKNOWN_SCHEMA))
    unstocked = damaged_copy(unstatused, lambda state: state.update(fame_depository_schema=UNKNOWN_SCHEMA))
    short = damaged_copy(unstocked, lambda state: ranked(goldens.bundle, state, fame.catalog.rank_min - 1))
    unloadable = damaged_copy(short, negative_money)
    broken = damaged_copy(unloadable, newer_player_schema)
    # Each save also fails every later gate, so only the earliest gate decides the refusal.
    for service, state, expected in ((changed_after_setup(fame, "barracks"), broken, ("fame_dependencies_changed", 503)),
                                     (fame, broken, ("player_state_schema", 503)),
                                     (fame, unloadable, ("invalid_player_state", 503)),
                                     (fame, short, ("fame_rank_required", 409)),
                                     (fame, unstocked, ("fame_state_invalid", 503)),
                                     (fame, unstatused, ("card_status_schema_unavailable", 503)),
                                     (fame, save.state, unit_gate)):
        native = fame_handler_refusal(service, case, save, state)
        assert fame_kernel_refusal(service, case, save, state) == native == expected


def transfer_handler_refusal(service: BarracksService, case: BarracksCase, save: BarracksSave,
                             state: dict) -> tuple[str, int]:
    store = MemoryStore(state)
    return refusal(partial(service.transfer, envelope(store, TRANSFER, barracks_columns(case, save)), store))


def transfer_kernel_refusal(service: BarracksService, case: BarracksCase, save: BarracksSave,
                            state: dict) -> tuple[str, int]:
    return refusal(partial(service.transfer_units, state, case.destination, pairs(transferred(case, save))))


TRANSFER_GATES = {
    "locked": (BarracksCase(TRANSFER), ("barracks_locked", 409)),
    "prince": (BarracksCase(TRANSFER, extra=2, select=prince_row), ("barracks_prince_protected", 409)),
    "organized": (BarracksCase(TRANSFER, extra=2, prepare=organized_elsewhere), ("barracks_organized", 409)),
}


def transfer_gates(barracks: BarracksService, case: BarracksCase, save: BarracksSave,
                   unit_gate: tuple[str, int]) -> tuple[tuple, ...]:
    changed = changed_after_setup(barracks, "capacity")
    unmeasured = damaged_copy(save.state, lambda state: textual_purchases(replace(save, state=state)))
    unloadable = damaged_copy(unmeasured, negative_money)
    broken = damaged_copy(unloadable, newer_player_schema)
    # Each save also fails every later gate, so only the earliest gate decides the refusal.
    return ((changed, case, broken, ("barracks_catalog_changed", 503)),
            (barracks, case, broken, ("player_state_schema", 503)),
            (barracks, case, unloadable, ("invalid_player_state", 503)),
            (barracks, case, unmeasured, ("unit_capacity_state_invalid", 503)),
            (barracks, case, save.state, unit_gate))


@pytest.mark.parametrize("name", TRANSFER_GATES)
def test_transfer_kernel_keeps_the_handler_gates_and_their_order(goldens, name):
    case, unit_gate = TRANSFER_GATES[name]
    save = barracks_save(goldens, case)
    gates = transfer_gates(goldens.barracks, case, save, unit_gate)
    changed, _, broken, _ = gates[0]
    unparsable = replace(case, destination=MAXIMUM_EXTRA_BARRACKS + 1)
    for service, variant, state, expected in ((changed, unparsable, broken, ("invalid_request_fields", 400)), *gates):
        native = transfer_handler_refusal(service, variant, save, state)
        assert transfer_kernel_refusal(service, variant, save, state) == native == expected


@pytest.mark.parametrize("name", TRANSFER_GATES)
def test_transfer_command_keeps_the_handler_gates_and_their_order(goldens, preflight, registry, name):
    case, unit_gate = TRANSFER_GATES[name]
    save = barracks_save(goldens, case)
    params = {"destination": case.destination, "units": selection(transferred(case, save))}
    for service, _, state, expected in transfer_gates(goldens.barracks, case, save, unit_gate):
        _, subject = console(console_services(goldens, barracks=service), state, preflight)
        preview_error(subject, registry, "barracks.transfer", params, *expected)


def test_kernels_return_the_handler_transitions(goldens):
    case = FameCase(REGISTER, count=2, prepare=untrained_last)
    save = fame_save(goldens, case)
    before = deepcopy(save.state)
    change = goldens.fame.register_units(save.state, every(save), NOW)
    assert save.state == before
    native = native_state(goldens.fame.register, REGISTER, fame_columns(case, save), save.state)
    assert canonical(change.state) == canonical(without(native, FAME_HISTORY))
    assert change.skipped == ((save.units[-1]["GetIndex"], "fame_ineligible"),)
    slot = barracks_save(goldens, BarracksCase(PURCHASE, extra=1, low=RETAINED_BITS))
    before = deepcopy(slot.state)
    granted = goldens.barracks.grant_slot(slot.state)["tables"]["config"][0][PURCHASE_FIELD]
    assert slot.state == before
    bought = native_state(goldens.barracks.purchase, PURCHASE, (), slot.state)["tables"]["config"][0][PURCHASE_FIELD]
    assert granted == bought == MAXIMUM_EXTRA_BARRACKS << EXTRA_SHIFT | RETAINED_BITS
