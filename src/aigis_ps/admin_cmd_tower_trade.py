from __future__ import annotations

from pydantic import Field

from .admin_command import AdminCommand, CommandContext, CommandParams, CommandResult, required_service, requires
from .admin_registry import CommandRegistry
from .admin_views_events import EVENTS, TOWER_TRADES
from .native_numbers import INT32_MAX
from .tower_trade_state import REBIND_OPERATION


TOWER_TRADE = "tower_trade"
UNREAD_REASON = ("Tower exchange counts reach the client only through 65 and 69 on the exchange screen; the command "
                 "re-reads the rewritten history and its purchase receipts inside the same transaction")
STILL_REUSED = ("Other saved tower products still name earlier catalog identities; 65 and 69 refuse until each "
                "is rebound")


class RebindParams(CommandParams):
    product_id: int = Field(ge=1, le=INT32_MAX,
                            description="Tower exchange product ID, the ID of its first catalog row")
    carry: bool = Field(description="Keep this month's purchase count; otherwise the month starts from zero")


def rebind_product(state: dict, params: RebindParams, context: CommandContext) -> CommandResult:
    trades = required_service(context, TOWER_TRADES)
    updated = trades.rebind_product(state, context.revision, params.product_id, params.carry)
    history = trades.history
    before, after = (history.stored(value)[params.product_id] for value in (state, updated))
    period, remaining = history.period(context.now), history.reused(updated)
    return CommandResult(updated, {
        "product_id": params.product_id, "carry": params.carry, "period": period,
        "source": {"before": before.source, "after": after.source},
        "saved_period": {"before": before.period, "after": after.period},
        "count": {"before": before.current(period), "after": after.current(period)},
        "lifetime": {"before": before.lifetime, "after": after.lifetime},
        "still_reused": list(remaining)}, (STILL_REUSED,) if remaining else ())


def rebind_phrase(params: RebindParams) -> str:
    return f"REBIND {params.product_id}"


REBIND_PRODUCT = AdminCommand(REBIND_OPERATION, EVENTS, RebindParams, rebind_product, frozenset({TOWER_TRADE}),
                              destructive=True, confirm_phrase=rebind_phrase,
                              preflight_none={TOWER_TRADE: UNREAD_REASON}, unavailable=requires(TOWER_TRADES))
COMMANDS = (REBIND_PRODUCT,)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
