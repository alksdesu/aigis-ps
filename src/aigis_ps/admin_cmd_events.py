from __future__ import annotations

from typing import Annotated

from pydantic import Field, model_validator

from .admin_cmd_economy import PRESENTS
from .admin_command import (AdminCommand, CommandContext, CommandParams, CommandResult, require_distinct,
                            required_service, requires)
from .admin_fields import LOGIN_REWARDS, SUGOROKU
from .admin_registry import CommandRegistry
from .admin_views_account import GAME_PARAM
from .admin_views_events import EVENTS
from .crystal_spending import read_spending
from .login_cooking_state import migrate_cooking, read_cooking
from .login_reward_service import COOKING_MIGRATION, COOKING_MIGRATION_PHRASE as COOKING_PHRASE
from .monthly_capacity import read_capacity
from .native_numbers import INT32_MAX
from .presents import MAX_PRESENTS
from .sugoroku_service import MAX_REWARDS, BoardClaim, BoardSelection


LOGIN_COOKING = "login_cooking"
REWARD_DESCRIPTION = "description"
NOTHING_CLAIMABLE = "Nothing was claimed: no selected reward is reached and unclaimed on this month's board"
REWARDS_SKIPPED = "Rewards already claimed or absent from this month's board were skipped"
REWARDS_UNSUPPORTED = "Rewards whose delivery is unsupported stay reached and unclaimed, as the game leaves them"
COOKING_UNCHANGED = "The cooking ledger already uses the running border and needs no quota recount this month"

RewardId = Annotated[int, Field(ge=1, le=INT32_MAX)]


class BoardClaimParams(CommandParams):
    ids: list[RewardId] = Field(min_length=1, max_length=MAX_REWARDS)

    @model_validator(mode="after")
    def _distinct(self) -> BoardClaimParams:
        require_distinct(self.ids, "Reward IDs")
        return self


class CookingMigrationParams(CommandParams):
    pass


def mail_count(context: CommandContext, state: dict) -> int:
    return len(required_service(context, PRESENTS).rows(state))


def claimed_reward(claim: BoardClaim, identifier: int, reward: dict) -> dict:
    detail = {name: value for name, value in reward.items() if name != REWARD_DESCRIPTION}
    return {"reward_id": identifier, **detail, "mail_ids": list(claim.deliveries[identifier])}


def board_effects(context: CommandContext, selection: BoardSelection, claim: BoardClaim | None) -> dict:
    shown, candidates = set(selection.shown), set(selection.candidates)
    after = selection.state if claim is None else claim.state
    return {"period": read_spending(selection.state)["period"],
            "claimed": [] if claim is None else [claimed_reward(claim, identifier, reward)
                                                 for identifier, reward in zip(claim.granted, claim.rewards)],
            "already_claimed": [identifier for identifier in selection.shown if identifier not in candidates],
            "unavailable": [identifier for identifier in selection.requested if identifier not in shown],
            "unsupported": [] if claim is None else list(claim.unsupported),
            "mailbox": {"before": mail_count(context, selection.state), "after": mail_count(context, after),
                        "capacity": MAX_PRESENTS},
            "next_month_capacity": {"before": read_capacity(selection.state)["next"],
                                    "after": read_capacity(after)["next"]}}


def claim_board_rewards(state: dict, params: BoardClaimParams, context: CommandContext) -> CommandResult:
    board = required_service(context, SUGOROKU)
    selection = board.select_rewards(state, params.ids, context.now)
    # Like 63, a claim that finds nothing to deliver saves nothing, not even the month synchronization.
    if not selection.candidates:
        return CommandResult(state, board_effects(context, selection, None), (NOTHING_CLAIMABLE,))
    claim = board.grant_rewards(selection, context.now)
    warnings = (*((REWARDS_SKIPPED,) if selection.skipped else ()),
                *((REWARDS_UNSUPPORTED,) if claim.unsupported else ()))
    return CommandResult(claim.state, board_effects(context, selection, claim), warnings)


def running_border(context: CommandContext) -> int:
    return required_service(context, LOGIN_REWARDS).cooking_border


def migrate_login_cooking(state: dict, params: CookingMigrationParams, context: CommandContext) -> CommandResult:
    before, spending = read_cooking(state), read_spending(state)
    updated = migrate_cooking(state, context.now, border=running_border(context))
    after = read_cooking(updated)
    return CommandResult(updated, {
        "period": after["period"], "spending_period": spending["period"], "previous_spent": spending["previous_spent"],
        "baseline_source": after["baseline_source"], "issued": after["issued"],
        "border": {"before": before["border"], "after": after["border"]},
        "quota": {"before": before["quota"], "after": after["quota"]},
        "remaining": max(0, after["quota"] - after["issued"])}, (COOKING_UNCHANGED,) if after == before else ())


def cooking_phrase(params: CookingMigrationParams) -> str:
    return COOKING_PHRASE


CLAIM_BOARD = AdminCommand("sugoroku.claim", EVENTS, BoardClaimParams, claim_board_rewards,
                           frozenset({SUGOROKU, PRESENTS, GAME_PARAM}), unavailable=requires(SUGOROKU))
MIGRATE_COOKING = AdminCommand(COOKING_MIGRATION, EVENTS, CookingMigrationParams, migrate_login_cooking,
                               frozenset({LOGIN_COOKING}), destructive=True, confirm_phrase=cooking_phrase,
                               unavailable=requires(LOGIN_REWARDS))
COMMANDS = (CLAIM_BOARD, MIGRATE_COOKING)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
