from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .admin_command import AdminCommand, CommandContext, CommandParams, CommandResult, required_service, requires
from .admin_gates import require_no_active_battle
from .admin_registry import CommandRegistry
from .admin_views_tools import Identifier
from .native_numbers import INT32_MAX
from .tower_service import SOURCES, TowerService
from .tower_state import read_state


BATTLE = "battle"
TOWERS, PRESENTS = "towers", "presents"
GATE_SOURCE = "tower command"
RESET_PHRASE = "RESET TOWER {mission_id}"
REWARDS_REPAY = ("Reset tiers count as never paid, so the next result, redelivery or threshold crossing mails their "
                 "rewards again")
STILL_DEFERRED = "Tiers whose presents cannot be delivered now stay deferred and retry with the next tower result"
NOTHING_OPEN = "No tower is open and settled, so no day was added"
Score = Annotated[int, Field(ge=0, le=INT32_MAX)]
Stock = Annotated[int, Field(ge=0, le=INT32_MAX)]
Source = Literal[SOURCES]


class FloorBestParams(CommandParams):
    quest_id: Identifier
    score: Score


class StackParams(CommandParams):
    mission_id: Identifier
    stack: Stock


class SettleParams(CommandParams):
    pass


class RedeliverParams(CommandParams):
    mission_id: Identifier


class ResetParams(CommandParams):
    mission_id: Identifier
    sources: list[Source] = Field(default=list(SOURCES), min_length=1, max_length=len(SOURCES))


def _towers(state: dict, context: CommandContext) -> TowerService:
    towers = required_service(context, TOWERS)
    require_no_active_battle(state, source=GATE_SOURCE)
    return towers


def _record(towers: TowerService, state: dict, mission_id: int) -> dict:
    return read_state(state, towers.catalog)["towers"][str(mission_id)]


def set_floor_best(state: dict, params: FloorBestParams, context: CommandContext) -> CommandResult:
    towers = _towers(state, context)
    mission_id = towers.catalog.floor_tower(params.quest_id).mission_id
    updated = towers.set_floor_best(state, params.quest_id, params.score)
    before, after = (_record(towers, value, mission_id) for value in (state, updated))
    key = str(params.quest_id)
    return CommandResult(updated, {
        "quest_id": params.quest_id, "mission_id": mission_id,
        "floor_best": {"before": before["floors"].get(key, 0), "after": after["floors"].get(key, 0)},
        "total_score": {"before": before["total"], "after": after["total"]},
        "claimed_score_rewards": {"before": before["score_rewards"].get(key, []),
                                  "after": after["score_rewards"].get(key, [])}})


def adjust_stack(state: dict, params: StackParams, context: CommandContext) -> CommandResult:
    towers = _towers(state, context)
    updated = towers.adjust_stack(state, params.mission_id, params.stack)
    before, after = (_record(towers, value, params.mission_id) for value in (state, updated))
    tower = towers.catalog.tower(params.mission_id)
    return CommandResult(updated, {
        "mission_id": params.mission_id, "stack": {"before": before["stack"], "after": after["stack"]},
        "generation_limit": tower.generation_tier(before["total"]).limit})


def settle_now(state: dict, params: SettleParams, context: CommandContext) -> CommandResult:
    towers = _towers(state, context)
    updated, changed, forced = towers.settle_now(state, context.now)
    stacks = {key: record["stack"] for key, record in read_state(updated, towers.catalog)["towers"].items()}
    before = read_state(state, towers.catalog)["towers"]
    held_before, held_after = towers.held(state), towers.held(updated)
    effects = {"refilled_items": {item_id: {"before": held_before.get(item_id, 0), "after": held_after.get(item_id, 0)}
                                  for item_id in changed},
               "stacks": {key: {"before": before.get(key, {}).get("stack", 0), "after": stack}
                          for key, stack in stacks.items()}}
    return CommandResult(updated, effects, () if forced else (NOTHING_OPEN,))


def redeliver_deferred(state: dict, params: RedeliverParams, context: CommandContext) -> CommandResult:
    towers = _towers(state, context)
    updated, mailed, deferred = towers.redeliver_deferred(state, params.mission_id)
    effects = {"mission_id": params.mission_id, "mailed_present_ids": list(mailed),
               "deferred": [dict(entry) for entry in deferred]}
    return CommandResult(updated, effects, (STILL_DEFERRED,) if deferred else ())


def reset_rewards(state: dict, params: ResetParams, context: CommandContext) -> CommandResult:
    towers = _towers(state, context)
    updated, reset = towers.reset_rewards(state, params.mission_id, tuple(params.sources))
    return CommandResult(updated, {"mission_id": params.mission_id, "sources": list(params.sources),
                                   "reset": [dict(entry) for entry in reset]}, (REWARDS_REPAY,))


def reset_phrase(params: ResetParams) -> str:
    return RESET_PHRASE.format(mission_id=params.mission_id)


SET_FLOOR_BEST = AdminCommand("towers.floor_best.set", BATTLE, FloorBestParams, set_floor_best, frozenset({TOWERS}),
                              unavailable=requires(TOWERS))
ADJUST_STACK = AdminCommand("towers.stack.adjust", BATTLE, StackParams, adjust_stack, frozenset({TOWERS}),
                            unavailable=requires(TOWERS))
SETTLE_NOW = AdminCommand("towers.settle_now", BATTLE, SettleParams, settle_now, frozenset({TOWERS}),
                          unavailable=requires(TOWERS))
REDELIVER_DEFERRED = AdminCommand("towers.deferred.redeliver", BATTLE, RedeliverParams, redeliver_deferred,
                                  frozenset({TOWERS, PRESENTS}), unavailable=requires(TOWERS))
RESET_REWARDS = AdminCommand("towers.rewards.reset", BATTLE, ResetParams, reset_rewards, frozenset({TOWERS}),
                             destructive=True, confirm_phrase=reset_phrase, unavailable=requires(TOWERS))
COMMANDS = (SET_FLOOR_BEST, ADJUST_STACK, SETTLE_NOW, REDELIVER_DEFERRED, RESET_REWARDS)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
