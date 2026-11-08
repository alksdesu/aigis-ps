from __future__ import annotations

from datetime import datetime
from http import HTTPStatus
from typing import Any

from pydantic import Field, model_validator

from .admin_cmd_economy import MONTH_PATTERN, PRESENTS
from .admin_command import (COMMAND_UNAVAILABLE, AdminCommand, AdminError, CommandContext, CommandParams, CommandResult,
                            counted, missing_service, require_distinct, required_service, requires)
from .admin_fields import EXPIRY_NOTIFIER, SUGOROKU, TRANSACTIONS, VOTE_COOKING, VOTES
from .admin_registry import CommandRegistry
from .admin_views_account import GAME_PARAM
from .admin_views_events import EVENTS
from .admin_views_tools import ItemName
from .business_calendar import business_month
from .monthly_capacity import STAMINA_FIELDS, establish_baseline, legacy_extensions, read_capacity
from .vote_cooking_service import PENDING as COOKING_PENDING
from .vote_expiry import MAX_RECORDS as MAX_EXPIRY_RECORDS, SETTLED as EXPIRY_SETTLED
from .vote_state import adopt_current as adopt_vote_state


VOTE = "vote"
VOTE_GROUP_UNKNOWN = "vote_group_unknown"
REWARD_ADOPTION_UNNEEDED = "vote_rewards_adopt_unneeded"
COOKING_ADOPTION_UNNEEDED = "vote_cooking_adopt_unneeded"
VOTE_ADOPTION_UNNEEDED = "vote_definition_adopt_unneeded"
ADOPT_VOTE_OPERATION = "votes.adopt_vote_definition"
ADOPT_REWARDS_OPERATION = "votes.adopt_reward_definition"
TRANSACTIONS_REASON = "This generation's vote service has no transaction services"
REOPEN_NOUN = "VOTE TICKET"
REWARDS_PHRASE = "MIGRATE VOTE REWARDS"
COOKING_PHRASE = "MIGRATE VOTE COOKING"
DEFINITION_PHRASE = "MIGRATE VOTE DEFINITION"
NO_CURRENT_GROUP = ("No current vote group uses a reopened ticket, so its reappeared stock stays held and never "
                    "converts")
NOTICE_DROPPED = "A reopened conversion was never announced by a 116, so its exchange popup is no longer shown"
STILL_REAPPEARED = ("Other completed tickets still hold reappeared stock; reopen them together with these, "
                    "or the restart check refuses the save")
VOTES_STILL_BOUND = ("Saved candidate totals still bind an earlier definition, so 57 refuses those candidates "
                     f"until {ADOPT_VOTE_OPERATION} adopts them")
REWARDS_STILL_BOUND = ("The reward ledger still binds an earlier definition, so 57 refuses this vote "
                       f"until {ADOPT_REWARDS_OPERATION} adopts it")
COOKING_STILL_PENDING = "Cooking the mailbox could not hold stays pending under the current definition; 115 retries it"
STAMINA_CLEARED = "The monthly ledger keeps no stamina extension, so the legacy stamina values were cleared"
MONTHS_PENDING = "Months after the baseline turn on the next 33, 34 or 35; board actions wait for that refresh"


class ReopenParams(CommandParams):
    item_ids: list[ItemName] = Field(min_length=1, max_length=MAX_EXPIRY_RECORDS,
                                     description="Vote ticket item IDs whose completed expiry saw their stock reappear; "
                                                 "tickets that reappeared together reopen together")

    @model_validator(mode="after")
    def _distinct(self) -> ReopenParams:
        require_distinct(self.item_ids, "Vote ticket item IDs")
        return self


class RewardAdoptionParams(CommandParams):
    pass


class CookingAdoptionParams(CommandParams):
    pass


class VoteAdoptionParams(CommandParams):
    item_id: ItemName = Field(description="Vote ticket item ID naming the current vote group whose saved votes adopt "
                                          "its definition")


class BaselineParams(CommandParams):
    period: str = Field(pattern=MONTH_PATTERN, description="YYYY-MM in which the save held its legacy extended "
                                                           "charisma; not after the JST business month")


def voting_unavailable(services: Any) -> str | None:
    votes = getattr(services, VOTES, None)
    if votes is None:
        return missing_service(VOTES)
    return None if getattr(votes, TRANSACTIONS, None) is not None else TRANSACTIONS_REASON


def vote_transactions(context: CommandContext) -> Any:
    found = getattr(required_service(context, VOTES), TRANSACTIONS, None)
    if found is None:
        raise AdminError(COMMAND_UNAVAILABLE, TRANSACTIONS_REASON, HTTPStatus.SERVICE_UNAVAILABLE)
    return found


def catalog_group(voting: Any, item_id: str) -> Any:
    return next((group for group in voting.catalog.groups if group.item_id == item_id), None)


def stale_votes(voting: Any, state: dict) -> list[str]:
    definitions = {group.item_id: voting.rewards.group_definition(group) for group in voting.catalog.groups}
    return sorted({row["item_id"] for row in adopt_vote_state(state, definitions)[1]})


def reopened_ticket(voting: Any, notifier: Any, state: dict, item_id: str, report: dict, now: datetime) -> dict:
    record, group = report["archived"], catalog_group(voting, item_id)
    return {"item_id": item_id, "stock": voting.possessions.counts(state)[item_id],
            "archived": {"status": record["status"], "amount": record["amount"],
                         "gold": record["amount"] * record["rate"], "mail_ids": list(record["mail_ids"])},
            "announced": None if notifier is None or record["status"] != EXPIRY_SETTLED else report["notice"] is not None,
            "group_id": None if group is None else group.group_id,
            "converts_on_next_observation": group is not None and voting.catalog.rules.enabled and now >= group.vote_end}


def reopen_tickets(state: dict, params: ReopenParams, context: CommandContext) -> CommandResult:
    voting = vote_transactions(context)
    notifier = getattr(context.services, EXPIRY_NOTIFIER, None)
    # The save stays refused while any reappeared ticket remains, so tickets that reappeared together reopen in one step.
    updated, tickets = state, []
    for item_id in params.item_ids:
        updated, report = voting.expiry.reopen_settled(updated, item_id, context.now)
        tickets.append(reopened_ticket(voting, notifier, updated, item_id, report, context.now))
    remaining = report["reappeared"]
    return CommandResult(updated, {"tickets": tickets, "still_reappeared": remaining}, (
        *((NO_CURRENT_GROUP,) if any(ticket["group_id"] is None for ticket in tickets) else ()),
        *((NOTICE_DROPPED,) if any(ticket["announced"] is False for ticket in tickets) else ()),
        *((STILL_REAPPEARED,) if remaining else ())))


def reopen_phrase(params: ReopenParams) -> str:
    return f"MIGRATE {counted(len(params.item_ids), REOPEN_NOUN)}"


def adopt_rewards(state: dict, params: RewardAdoptionParams, context: CommandContext) -> CommandResult:
    voting = vote_transactions(context)
    updated, report = voting.rewards.adopt_current(state)
    if not report["items"] and report["assault"] is None:
        raise AdminError(REWARD_ADOPTION_UNNEEDED, "Every bound vote reward already follows the current definition")
    bound = stale_votes(voting, updated)
    return CommandResult(updated, {**report, "votes_still_bound": bound}, (VOTES_STILL_BOUND,) if bound else ())


def rewards_phrase(params: RewardAdoptionParams) -> str:
    return REWARDS_PHRASE


def adopt_cooking(state: dict, params: CookingAdoptionParams, context: CommandContext) -> CommandResult:
    updated, adopted = required_service(context, VOTE_COOKING).adopt_current(state, context.now)
    if not adopted:
        raise AdminError(COOKING_ADOPTION_UNNEEDED,
                         "Every tracked or pending vote cooking already follows the current definition")
    waiting = any(row["status"]["after"] == COOKING_PENDING for row in adopted)
    return CommandResult(updated, {"adopted": adopted, "mail_ids": [mail for row in adopted for mail in row["mail_ids"]]},
                         (COOKING_STILL_PENDING,) if waiting else ())


def cooking_phrase(params: CookingAdoptionParams) -> str:
    return COOKING_PHRASE


def adopt_votes(state: dict, params: VoteAdoptionParams, context: CommandContext) -> CommandResult:
    voting = vote_transactions(context)
    group = catalog_group(voting, params.item_id)
    if group is None:
        raise AdminError(VOTE_GROUP_UNKNOWN, "The current vote catalog has no group for this ticket",
                         HTTPStatus.UNPROCESSABLE_ENTITY)
    updated, adopted = adopt_vote_state(state, {params.item_id: voting.rewards.group_definition(group)})
    # Frozen rankings share one source fingerprint, so the vote that finds them stale refreezes every group.
    updated, refrozen = voting.finalists.adopt_current(updated)
    if not adopted and not refrozen:
        raise AdminError(VOTE_ADOPTION_UNNEEDED, "The saved votes and rankings already follow the current definition")
    rewards = voting.rewards.adopt_current(updated)[1]
    bound = rewards["assault"] is not None or any(item["item_id"] == params.item_id for item in rewards["items"])
    return CommandResult(updated, {"item_id": params.item_id, "group_id": group.group_id,
                                   "card_ids": [row["card_id"] for row in adopted], "refrozen": list(refrozen)},
                         (REWARDS_STILL_BOUND,) if bound else ())


def definition_phrase(params: VoteAdoptionParams) -> str:
    return DEFINITION_PHRASE


def establish_capacity(state: dict, params: BaselineParams, context: CommandContext) -> CommandResult:
    legacy = legacy_extensions(state)
    updated = establish_baseline(state, params.period, context.now)
    ledger, business = read_capacity(updated), business_month(context.now)
    cleared = any(legacy[name] for name in STAMINA_FIELDS)
    return CommandResult(updated, {
        "period": ledger["period"], "business_month": business, "current": ledger["current"], "next": ledger["next"],
        "stamina": {name: {"before": legacy[name], "after": 0} for name in STAMINA_FIELDS}},
        (*((STAMINA_CLEARED,) if cleared else ()), *((MONTHS_PENDING,) if ledger["period"] != business else ())))


def capacity_phrase(params: BaselineParams) -> str:
    return f"MIGRATE CAPACITY {params.period}"


REOPEN_SETTLED = AdminCommand("votes.reopen_settled", VOTES, ReopenParams, reopen_tickets, frozenset({VOTE}),
                              destructive=True, confirm_phrase=reopen_phrase, unavailable=voting_unavailable)
ADOPT_REWARDS = AdminCommand(ADOPT_REWARDS_OPERATION, VOTES, RewardAdoptionParams, adopt_rewards,
                             frozenset({VOTE}), destructive=True, confirm_phrase=rewards_phrase,
                             unavailable=voting_unavailable)
ADOPT_COOKING = AdminCommand("votes.adopt_cooking_definition", VOTES, CookingAdoptionParams, adopt_cooking,
                             frozenset({VOTE, PRESENTS}), destructive=True, confirm_phrase=cooking_phrase,
                             unavailable=requires(VOTE_COOKING))
ADOPT_VOTES = AdminCommand(ADOPT_VOTE_OPERATION, VOTES, VoteAdoptionParams, adopt_votes, frozenset({VOTE}),
                           destructive=True, confirm_phrase=definition_phrase, unavailable=voting_unavailable)
ESTABLISH_CAPACITY = AdminCommand("sugoroku.establish_capacity_baseline", EVENTS, BaselineParams, establish_capacity,
                                  frozenset({SUGOROKU, GAME_PARAM}), destructive=True, confirm_phrase=capacity_phrase,
                                  unavailable=requires(SUGOROKU))
COMMANDS = (REOPEN_SETTLED, ADOPT_REWARDS, ADOPT_COOKING, ADOPT_VOTES, ESTABLISH_CAPACITY)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
