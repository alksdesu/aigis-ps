from __future__ import annotations

from typing import Any

from pydantic import Field

from .admin_cmd_storage import STORAGE, StockDelta, removes_stock
from .admin_cmd_units import SQUADS, UNITS, summary
from .admin_command import AdminCommand, CommandContext, CommandParams, CommandResult, required_service, requires
from .admin_registry import CommandRegistry
from .native_numbers import INT32_MAX
from .player_state import MAX_PLAYER_UNITS


FAIRY_FARM, FAIRY_DOMAIN = "fairy_farm", "fairy"


class FairyStockParams(CommandParams):
    card_id: int = Field(gt=1, le=INT32_MAX)
    delta: StockDelta


class FairyWithdrawParams(CommandParams):
    card_id: int = Field(gt=1, le=INT32_MAX)
    count: int = Field(default=1, ge=1, le=MAX_PLAYER_UNITS)


def fairy_count(farm: Any, state: dict, card_id: int) -> int:
    return next((row["Count"] for row in farm.read(state) if row["CardID"] == card_id), 0)


def fairy_stock(farm: Any, before: dict, after: dict, card_id: int) -> dict:
    return {"CardID": card_id, "before": fairy_count(farm, before, card_id), "after": fairy_count(farm, after, card_id)}


def adjust_fairy_stock(state: dict, params: FairyStockParams, context: CommandContext) -> CommandResult:
    farm = required_service(context, FAIRY_FARM)
    updated = farm.adjust(state, {params.card_id: params.delta})
    return CommandResult(updated, fairy_stock(farm, state, updated, params.card_id))


def fairy_stock_phrase(params: FairyStockParams) -> str:
    return f"ADJUST FAIRY STOCK {params.card_id} BY {params.delta:+d}"


def withdraw_fairies(state: dict, params: FairyWithdrawParams, context: CommandContext) -> CommandResult:
    farm = required_service(context, FAIRY_FARM)
    updated, added = farm.withdraw_state(state, params.card_id, params.count)
    return CommandResult(updated, {"withdrawn": [summary(unit) for unit in added],
                                   "stock": fairy_stock(farm, state, updated, params.card_id)})


FAIRY_ADJUST_STOCK = AdminCommand("fairy.adjust_stock", STORAGE, FairyStockParams, adjust_fairy_stock,
                                  frozenset({FAIRY_DOMAIN}), destructive=removes_stock,
                                  confirm_phrase=fairy_stock_phrase, unavailable=requires(FAIRY_FARM))
FAIRY_WITHDRAW = AdminCommand("fairy.withdraw", STORAGE, FairyWithdrawParams, withdraw_fairies,
                              frozenset({FAIRY_DOMAIN, UNITS, SQUADS}), unavailable=requires(FAIRY_FARM))
COMMANDS = (FAIRY_ADJUST_STOCK, FAIRY_WITHDRAW)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
