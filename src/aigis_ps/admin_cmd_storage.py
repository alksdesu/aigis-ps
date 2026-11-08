from __future__ import annotations

from http import HTTPStatus
from typing import Annotated, Any

from pydantic import AfterValidator, Field, model_validator

from .admin_cmd_settings import CONFIG, MISSIONS_DOMAIN
from .admin_cmd_stories import CARD_STATUS
from .admin_cmd_units import SQUADS, UNITS, UnitParams, distinct_units, summary
from .admin_command import (AdminCommand, AdminError, CommandContext, CommandParams, CommandResult, counted,
                            missing_service, required_service, requires)
from .admin_fields import EVOLUTION_REWARDS, GIFTS
from .admin_registry import CommandRegistry
from .admin_views_settings import REGISTRATION_FIELDS
from .admin_views_units import unit_flags
from .barracks_service import MAXIMUM_EXTRA_BARRACKS, MAXIMUM_TRANSFER, PURCHASE_FIELD
from .fame_catalog import STAGES
from .fame_state import COUNT_FIELDS
from .native_numbers import INT32_MAX
from .player_state import MAX_PLAYER_UNITS
from .unit_grants import MAX_GRANTS


STORAGE = "storage"
FAME, BARRACKS, SILVER_HOUSE = "fame", "barracks", "silver_house"
FAME_DOMAIN, SILVER_DOMAIN = "fame", "silver"
REGISTRATION_STAGES = dict(zip(STAGES, REGISTRATION_FIELDS, strict=True))
REGISTRATION_WRITES = frozenset(f"{CARD_STATUS}.{field}" for field in REGISTRATION_FIELDS)
SLOT_WRITES = frozenset({f"{CONFIG}.{PURCHASE_FIELD}"})
FAME_UNIT_MISSING, FAME_DEPOSIT_LIMIT = "admin_fame_unit_missing", "admin_fame_deposit_limit"
SILVER_WRITES_REASON = "This generation's silver warehouse lacks the verified menu resources or the trust reward ledger"
NOTHING_REGISTERED = "Every registrable selection was already registered, so no registration time changed"
NOTHING_MOVED = "The selected units were already in the destination barracks"


def _nonzero(value: int) -> int:
    if value == 0:
        raise ValueError("A stock adjustment changes the count")
    return value


StockDelta = Annotated[int, Field(ge=-INT32_MAX, le=INT32_MAX), AfterValidator(_nonzero)]
FameStage = Annotated[int, Field(ge=min(STAGES), le=max(STAGES))]


def removes_stock(params: Any) -> bool:
    # Only a decrease can lose stored units, so an increase needs neither a phrase nor a backup.
    return params.delta < 0


class FameUnitsParams(CommandParams):
    units: list[UnitParams] = Field(min_length=1, max_length=MAX_PLAYER_UNITS)

    @model_validator(mode="after")
    def _distinct(self) -> FameUnitsParams:
        distinct_units(self.units)
        return self


class FameWithdrawParams(CommandParams):
    card_id: int = Field(gt=0, le=INT32_MAX)
    stage: FameStage
    count: int = Field(default=1, ge=1, le=MAX_GRANTS)


class FameStockParams(CommandParams):
    card_id: int = Field(gt=0, le=INT32_MAX)
    stage: FameStage
    delta: StockDelta


class GrantSlotParams(CommandParams):
    pass


class TransferParams(CommandParams):
    destination: int = Field(ge=0, le=MAXIMUM_EXTRA_BARRACKS)
    units: list[UnitParams] = Field(min_length=1, max_length=MAXIMUM_TRANSFER)

    @model_validator(mode="after")
    def _distinct(self) -> TransferParams:
        distinct_units(self.units)
        return self


class SilverWithdrawParams(CommandParams):
    card_id: int = Field(gt=1, le=INT32_MAX)
    count: int = Field(default=1, ge=1, le=MAX_GRANTS)


class SilverStockParams(CommandParams):
    card_id: int = Field(gt=1, le=INT32_MAX)
    delta: StockDelta


def units_by_index(players: Any, state: dict) -> dict[int, dict]:
    return {unit["GetIndex"]: unit for unit in players.validate(state)["units"]}


def owned_selection(players: Any, state: dict, units: list[UnitParams]) -> tuple[int, ...]:
    owned = units_by_index(players, state)
    stale = [unit.get_index for unit in units
             if unit.get_index not in owned or owned[unit.get_index]["UniqueID"] != unit.unique_id]
    if stale:
        raise AdminError(FAME_UNIT_MISSING, "Fame selection does not match an owned unit", details={"GetIndex": stale})
    return tuple(unit.get_index for unit in units)


def fame_count(repository: Any, state: dict, card_id: int, stage: int) -> int:
    return next((row[COUNT_FIELDS[stage]] for row in repository.read(state) if row["CardId"] == card_id), 0)


def fame_stock(repository: Any, before: dict, after: dict, card_id: int, stage: int) -> dict:
    return {"CardId": card_id, "stage": stage, "before": fame_count(repository, before, card_id, stage),
            "after": fame_count(repository, after, card_id, stage)}


def registrations(card_status: Any, before: dict, after: dict) -> list[dict]:
    stored = {row["CardID"]: row for row in card_status.read(before)}
    return [{"CardID": row["CardID"], "stage": stage, "registered_at": row[field]}
            for row in card_status.read(after) for stage, field in REGISTRATION_STAGES.items()
            if row[field] != stored.get(row["CardID"], {}).get(field, 0)]


def register_fame(state: dict, params: FameUnitsParams, context: CommandContext) -> CommandResult:
    fame = required_service(context, FAME)
    fame.admit(state)
    owned = units_by_index(fame.players, state)
    change = fame.register_units(state, owned_selection(fame.players, state, params.units), context.now)
    saved = units_by_index(fame.players, change.state)
    newly = [index for index in change.units if not owned[index]["Famed"]]
    effects = {"registered": [summary(saved[index]) for index in newly],
               "already_registered": [summary(saved[index]) for index in change.units if owned[index]["Famed"]],
               "registrations": registrations(fame.card_status, state, change.state),
               "skipped": [{"GetIndex": index, "code": code} for index, code in change.skipped]}
    # Like 149, units that fail their own check stay unregistered without blocking the rest of the selection.
    warnings = tuple(f"GetIndex {index} stays unregistered: {code}" for index, code in change.skipped)
    return CommandResult(change.state, effects, warnings + (() if newly else (NOTHING_REGISTERED,)))


def deposit_fame(state: dict, params: FameUnitsParams, context: CommandContext) -> CommandResult:
    fame = required_service(context, FAME)
    limit = fame.deposit_batch_limit
    # 150 refuses an oversized selection while parsing, before any of its account gates.
    if len(params.units) > limit:
        message = f"One fame deposit takes at most {limit} units, as the native deposit menu does"
        raise AdminError(FAME_DEPOSIT_LIMIT, message, HTTPStatus.UNPROCESSABLE_ENTITY,
                         fields=({"loc": ["units"], "type": FAME_DEPOSIT_LIMIT, "msg": message},))
    fame.admit(state)
    owned = units_by_index(fame.players, state)
    indices = owned_selection(fame.players, state, params.units)
    change = fame.deposit_units(state, context.revision, indices)
    buckets = dict.fromkeys((owned[index]["CardID"], owned[index]["ClassChangeCount"]) for index in indices)
    return CommandResult(change.state, {
        "deposited": [summary(owned[index]) for index in indices],
        "stock": [fame_stock(fame.repository, state, change.state, card_id, stage) for card_id, stage in buckets],
        "last_unique_id": change.state["player_state"]["last_unique_id"]})


def deposit_phrase(params: FameUnitsParams) -> str:
    return f"DEPOSIT {counted(len(params.units), 'UNIT')}"


def withdraw_fame(state: dict, params: FameWithdrawParams, context: CommandContext) -> CommandResult:
    fame = required_service(context, FAME)
    fame.admit(state)
    updated, added = state, []
    # Each new instance is one 151, so every step rechecks stock, roster room and the active battle reservations.
    for _ in range(params.count):
        change = fame.withdraw_unit(updated, context.revision, params.card_id, params.stage)
        updated = change.state
        added.append(units_by_index(fame.players, updated)[change.units[0]])
    return CommandResult(updated, {"withdrawn": [summary(unit) for unit in added],
                                   "stock": fame_stock(fame.repository, state, updated, params.card_id, params.stage)})


def adjust_fame_stock(state: dict, params: FameStockParams, context: CommandContext) -> CommandResult:
    fame = required_service(context, FAME)
    updated = fame.adjust_stock(state, {(params.card_id, params.stage): params.delta})
    return CommandResult(updated, fame_stock(fame.repository, state, updated, params.card_id, params.stage))


def fame_stock_phrase(params: FameStockParams) -> str:
    return f"ADJUST FAME STOCK {params.card_id}/{params.stage} BY {params.delta:+d}"


def grant_barracks_slot(state: dict, params: GrantSlotParams, context: CommandContext) -> CommandResult:
    barracks = required_service(context, BARRACKS)
    updated = barracks.grant_slot(state)
    return CommandResult(updated, {"capacities": {"before": list(barracks.capacity.usage(state).capacities),
                                                  "after": list(barracks.capacity.usage(updated).capacities)}})


def transfer_barracks_units(state: dict, params: TransferParams, context: CommandContext) -> CommandResult:
    barracks = required_service(context, BARRACKS)
    references = tuple((unit.get_index, unit.unique_id) for unit in params.units)
    updated, moved = barracks.transfer_units(state, params.destination, references)
    owned = units_by_index(barracks.players, state)
    moves = [{**summary(unit), "from": unit_flags(owned[unit["GetIndex"]]["GeneralPurposeFlags"])["barracks"],
              "to": params.destination} for unit in moved]
    return CommandResult(updated, {"moved": moves},
                         (NOTHING_MOVED,) if all(move["from"] == move["to"] for move in moves) else ())


def silver_writable(services: Any) -> str | None:
    silver = getattr(services, SILVER_HOUSE, None)
    if silver is None:
        return missing_service(SILVER_HOUSE)
    return None if silver.status()["write_ready"] else SILVER_WRITES_REASON


def silver_count(silver: Any, state: dict, card_id: int) -> int:
    return next((row["Count"] for row in silver.read(state) if row["CardID"] == card_id), 0)


def silver_stock(silver: Any, before: dict, after: dict, card_id: int) -> dict:
    return {"CardID": card_id, "before": silver_count(silver, before, card_id), "after": silver_count(silver, after, card_id)}


def adjust_silver_stock(state: dict, params: SilverStockParams, context: CommandContext) -> CommandResult:
    silver = required_service(context, SILVER_HOUSE)
    if params.delta > 0:
        # Added stock must be a card 127 could have stored, so increases follow its material rules.
        silver.material(params.card_id)
    updated = silver.adjust(state, {params.card_id: params.delta})
    return CommandResult(updated, silver_stock(silver, state, updated, params.card_id))


def silver_stock_phrase(params: SilverStockParams) -> str:
    return f"ADJUST SILVER STOCK {params.card_id} BY {params.delta:+d}"


def withdraw_silver(state: dict, params: SilverWithdrawParams, context: CommandContext) -> CommandResult:
    silver = required_service(context, SILVER_HOUSE)
    updated, added = silver.withdraw_state(state, params.card_id, params.count)
    return CommandResult(updated, {"withdrawn": [summary(unit) for unit in added],
                                   "stock": silver_stock(silver, state, updated, params.card_id)})


FAME_LEDGERS = frozenset({GIFTS, EVOLUTION_REWARDS})
FAME_REGISTER = AdminCommand("fame.register", STORAGE, FameUnitsParams, register_fame,
                             frozenset({UNITS, CARD_STATUS, MISSIONS_DOMAIN}), writes=REGISTRATION_WRITES,
                             unavailable=requires(FAME))
FAME_DEPOSIT = AdminCommand("fame.deposit", STORAGE, FameUnitsParams, deposit_fame,
                            frozenset({UNITS, FAME_DOMAIN}) | FAME_LEDGERS, destructive=True,
                            confirm_phrase=deposit_phrase, unavailable=requires(FAME))
FAME_WITHDRAW = AdminCommand("fame.withdraw", STORAGE, FameWithdrawParams, withdraw_fame,
                             frozenset({UNITS, SQUADS, FAME_DOMAIN}) | FAME_LEDGERS, unavailable=requires(FAME))
FAME_ADJUST_STOCK = AdminCommand("fame.adjust_stock", STORAGE, FameStockParams, adjust_fame_stock,
                                 frozenset({FAME_DOMAIN}), destructive=removes_stock, confirm_phrase=fame_stock_phrase,
                                 unavailable=requires(FAME))
BARRACKS_GRANT_SLOT = AdminCommand("barracks.grant_slot", STORAGE, GrantSlotParams, grant_barracks_slot,
                                   frozenset({CONFIG}), writes=SLOT_WRITES, unavailable=requires(BARRACKS))
BARRACKS_TRANSFER = AdminCommand("barracks.transfer", STORAGE, TransferParams, transfer_barracks_units,
                                 frozenset({UNITS}), unavailable=requires(BARRACKS))
SILVER_ADJUST_STOCK = AdminCommand("silver.adjust_stock", STORAGE, SilverStockParams, adjust_silver_stock,
                                   frozenset({SILVER_DOMAIN}), destructive=removes_stock,
                                   confirm_phrase=silver_stock_phrase,
                                   unavailable=silver_writable)
SILVER_WITHDRAW = AdminCommand("silver.withdraw", STORAGE, SilverWithdrawParams, withdraw_silver,
                               frozenset({SILVER_DOMAIN, UNITS, SQUADS}), unavailable=silver_writable)
COMMANDS = (FAME_REGISTER, FAME_DEPOSIT, FAME_WITHDRAW, FAME_ADJUST_STOCK, BARRACKS_GRANT_SLOT, BARRACKS_TRANSFER,
            SILVER_ADJUST_STOCK, SILVER_WITHDRAW)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
