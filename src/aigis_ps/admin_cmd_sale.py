from __future__ import annotations

from pydantic import Field, model_validator

from .admin_cmd_units import UNITS, CardUnitParams, distinct_units, summary
from .admin_command import (AdminCommand, CommandContext, CommandParams, CommandResult, counted, required_service,
                            requires_all)
from .admin_fields import EVOLUTION_REWARDS, GIFTS
from .admin_registry import CommandRegistry
from .player_state import MAX_PLAYER_UNITS
from .sale_protocol import MANUAL_SALE
from .unit_sale import UnitSale
from .wallet_money import money_balance


SALES, GAME_PARAM, POSSESSIONS = "sales", "game_param", "possessions"
SALE_ERRORS = "admin_sale"
SALE_SUBJECT = "Sold"
GOLD_DISCARDED = "Gold beyond the shared limit was discarded, as game-side credits do"


class SellParams(CommandParams):
    units: list[CardUnitParams] = Field(min_length=1, max_length=MAX_PLAYER_UNITS)

    @model_validator(mode="after")
    def _distinct(self) -> SellParams:
        distinct_units(self.units, SALE_SUBJECT)
        return self


def sell(state: dict, params: SellParams, context: CommandContext) -> CommandResult:
    catalog = required_service(context, SALES).catalog
    kernel = UnitSale(required_service(context, "player_state"), required_service(context, POSSESSIONS),
                      required_service(context, GIFTS), required_service(context, EVOLUTION_REWARDS), catalog,
                      error_prefix=SALE_ERRORS)
    # The console sells on the player's behalf, so it follows the manual sale list rather than the material collector.
    result = kernel.sell(state, context.revision, [unit.reference() for unit in params.units], MANUAL_SALE)
    credited = result.balance - money_balance(state).money
    discarded = result.quote.gold - credited
    effects = {"sold": [summary(unit) for unit in result.sold],
               "gold": {"quoted": result.quote.gold, "credited": credited, "discarded": discarded,
                        "balance": result.balance},
               "rainbow": {"item_id": catalog.rainbow_item, "credited": result.quote.rainbow},
               "last_unique_id": result.state["player_state"]["last_unique_id"]}
    return CommandResult(result.state, effects, (GOLD_DISCARDED,) if discarded else ())


def sale_phrase(params: SellParams) -> str:
    return f"SELL {counted(len(params.units), 'UNIT')}"


SELL = AdminCommand("units.sell", UNITS, SellParams, sell,
                    frozenset({UNITS, GAME_PARAM, POSSESSIONS, GIFTS, EVOLUTION_REWARDS}),
                    destructive=True, confirm_phrase=sale_phrase,
                    unavailable=requires_all(SALES, GIFTS, EVOLUTION_REWARDS))
COMMANDS = (SELL,)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
