from __future__ import annotations

from http import HTTPStatus
from typing import Any

from pydantic import Field

from .admin_cmd_economy import POSSESSIONS, QUESTS, TICKET_GRANT
from .admin_command import (COMMAND_UNAVAILABLE, AdminCommand, AdminError, CommandContext, CommandParams, CommandResult,
                            first_unavailable, requires)
from .admin_fields import VOTES
from .admin_registry import CommandRegistry
from .admin_views_tools import Amount, ItemName


TICKETS_REASON = "Vote ticket grants need the verified vote quest tickets"
TICKETS_CAPPED = "Vote tickets stop at their cap, so the excess was discarded, as quest settlement discards it"


class TicketGrantParams(CommandParams):
    item_id: ItemName = Field(description="Ticket of a vote group; the daily vote ticket belongs to none")
    count: Amount


def quest_tickets(services: Any) -> Any:
    quests = getattr(services, QUESTS, None)
    return None if quests is None else quests.event_rewards.vote_quest_tickets


def provider_unavailable(services: Any) -> str | None:
    return None if quest_tickets(services) is not None else TICKETS_REASON


tickets_unavailable = first_unavailable(requires(QUESTS), provider_unavailable)


def grant_vote_tickets(state: dict, params: TicketGrantParams, context: CommandContext) -> CommandResult:
    reason = tickets_unavailable(context.services)
    if reason is not None:
        raise AdminError(COMMAND_UNAVAILABLE, reason, HTTPStatus.SERVICE_UNAVAILABLE)
    tickets = quest_tickets(context.services)
    updated, discarded = tickets.grant(state, params.item_id, params.count)
    before, after = (tickets.possessions.counts(value).get(params.item_id, 0) for value in (state, updated))
    dropped = discarded.get(params.item_id, 0)
    return CommandResult(updated, {"item_id": params.item_id, "requested": params.count, "credited": after - before,
                                   "discarded": dropped, "before": before, "after": after, "cap": tickets.cap},
                         (TICKETS_CAPPED,) if dropped else ())


GRANT_TICKETS = AdminCommand(TICKET_GRANT, VOTES, TicketGrantParams, grant_vote_tickets, frozenset({POSSESSIONS}),
                             unavailable=tickets_unavailable)
COMMANDS = (GRANT_TICKETS,)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
