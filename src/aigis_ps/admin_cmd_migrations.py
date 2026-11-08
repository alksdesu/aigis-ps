from __future__ import annotations

from collections import Counter
from datetime import date
from enum import StrEnum
from http import HTTPStatus
from typing import Any

from pydantic import Field

from .admin_cmd_economy import MONTH_PATTERN, PRESENTS, RANK_REWARDS
from .admin_cmd_settings import MISSIONS_DOMAIN, PROGRESS
from .admin_command import (AdminCommand, AdminError, CommandContext, CommandParams, CommandResult, required_service,
                            requires)
from .admin_fields import (COMEBACK_LOGINS, DAILY_GIFTS, EVENT_LOGINS, EVENT_PASSES, LOGIN_REWARDS, MISSIONS,
                           PURCHASED_LOGINS)
from .admin_registry import CommandRegistry
from .admin_views_events import EVENTS
from .admin_views_tools import Identifier, ItemName
from .business_calendar import month_key
from .comeback_login_service import RESOLVE_OPERATION as COMEBACK_LOGIN_REPAIR
from .comeback_login_state import read_comeback_logins
from .daily_gift_service import RESOLVE_OPERATION as DAILY_GIFT_REPAIR
from .daily_gift_state import PENDING as GIFT_PENDING, read_daily_gifts
from .errors import ProtocolError
from .event_login_service import CAPACITY_ERRORS, RESOLVE_OPERATION as EVENT_LOGIN_REPAIR
from .event_login_state import read_event_logins, voided_delivery
from .event_pass_service import REBIND_OPERATION as EVENT_PASS_REPAIR, pass_identity
from .event_pass_state import ARCHIVED_KEY, TRACKS, read_pass_journal, rebind_definition
from .login_reward_service import (REBASE_OPERATION, STAMP_DESCRIPTION, STAMP_GAPS,
                                   STAMP_RESOLVE_OPERATION as STAMP_REPAIR)
from .login_reward_state import read_login_rewards, rebase_beginner
from .login_stamp_pending import read_pending_stamps, rebind_stamp, release_stamp
from .presents import PendingAction
from .purchased_login_service import RESOLVE_OPERATION as PURCHASED_LOGIN_REPAIR
from .purchased_login_state import read_purchased_logins
from .rank_reward_state import PENDING as RANK_PENDING, read_rank_rewards
from .rank_rewards import RESOLVE_OPERATION as RANK_REWARD_REPAIR


EVENT_PASS, DAILY_GIFT = "event_pass", "daily_gift"
EVENT_LOGIN, COMEBACK_LOGIN, PURCHASED_LOGIN = "event_login", "comeback_login", "purchased_login"
MISSION_REBUILD_UNNEEDED = "mission_rebuild_unneeded"
STAMP_RESOLVE_UNNEEDED = "login_stamp_resolve_unneeded"
STAMP_DEFINITION_MISSING = "login_stamp_definition_missing"
MISSIONS_PHRASE = "MIGRATE MISSIONS"
BEGINNER_PHRASE = "MIGRATE BEGINNER LOGIN REWARDS"
EVENT_PASS_PHRASE = "MIGRATE EVENT PASS"
RANK_FIELDS = ("definition", "status", "reason", "mail_ids")
GIFT_FIELDS = ("definition", "status", "reason", "delivery_day", "mail_ids")
STAMP_FIELDS = ("definition", "reason")
ACTION_HINT = "redeliver mails the current definition now; void drops the pending reward for good"
COUNT_HINT = "redeliver mails the current definition now; void drops the pending reward for good and uses up its count"
SCHEDULE_HINT = ("redeliver mails the current definition now and moves the ordinals still to come to it; "
                 "void drops the pending reward for good and uses up its ordinal")
STILL_BUNDLED = "Other ticket mail still holds several tickets; each one is claimable only after its own split"
STILL_CHANGED = "Pending rewards the current catalog no longer defines keep their old definition; void them"
STILL_PENDING = "Rewards that could not be mailed now stay pending under the current definition"
SCHEDULE_STILL_CHANGED = ("Later ordinals keep a definition the current catalog changed or lacks; "
                          "resolve each one once it is pending")
BEGINNER_CAPPED = "Beginner rewards claimed beyond the new total stay claimed; no further ones are given"


class SlotStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    VOIDED = "voided"


class SplitParams(CommandParams):
    present_id: Identifier = Field(description="PresentID of a ticket mail that holds several tickets")


class RebuildParams(CommandParams):
    pass


class RankResolutionParams(CommandParams):
    rank: Identifier = Field(description="Player rank whose pending rewards keep an earlier definition")
    action: PendingAction = Field(description=ACTION_HINT)


class RebaseParams(CommandParams):
    pass


class EventPassParams(CommandParams):
    pass_id: ItemName = Field(description="Event pass ID, which is its normal pass item ID")


class GiftResolutionParams(CommandParams):
    gift_id: Identifier = Field(description="Daily gift ID whose pending gifts keep an earlier definition")
    action: PendingAction = Field(description=ACTION_HINT)


class EventLoginResolutionParams(CommandParams):
    campaign_id: Identifier = Field(description="Event login campaign whose pending reward keeps an earlier definition")
    action: PendingAction = Field(description=COUNT_HINT)


class ComebackResolutionParams(CommandParams):
    round_id: Identifier = Field(description="Comeback round ID whose pending reward keeps an earlier definition")
    action: PendingAction = Field(description=SCHEDULE_HINT)


class PurchasedResolutionParams(CommandParams):
    purchase_id: Identifier = Field(description="Purchase ID whose pending daily reward keeps an earlier definition")
    action: PendingAction = Field(description=SCHEDULE_HINT)


class StampResolutionParams(CommandParams):
    month: str = Field(pattern=MONTH_PATTERN,
                       description="YYYY-MM of the claim days whose pending login stamps keep an earlier definition")
    action: PendingAction = Field(description=ACTION_HINT)


def changes(before: dict, after: dict | None, fields: tuple[str, ...]) -> dict:
    return {"before": {name: before[name] for name in fields},
            "after": None if after is None else {name: after[name] for name in fields}}


def login_slot(record: dict, ordinal: int) -> dict:
    pending = record["pending"]
    if pending is not None and pending["ordinal"] == ordinal:
        return {"status": SlotStatus.PENDING, "definition": pending["definition"], "reason": pending["reason"],
                "delivery_day": None, "mail_ids": []}
    delivery = record["deliveries"][str(ordinal)]
    return {"status": SlotStatus.VOIDED if voided_delivery(delivery) else SlotStatus.DELIVERED,
            "definition": delivery["definition"], "reason": None, "delivery_day": delivery["delivery_day"],
            "mail_ids": list(delivery["mail_ids"])}


def slot_effects(key: dict, action: PendingAction, before: dict, after: dict, grants: list[dict]) -> dict:
    ordinal = before["pending"]["ordinal"]
    return {**key, "action": action.value, "ordinal": ordinal, "earned_day": before["pending"]["earned_day"],
            "before": login_slot(before, ordinal), "after": login_slot(after, ordinal),
            "mail_ids": [mail for grant in grants for mail in grant["mail_ids"]]}


def slot_warnings(effects: dict) -> tuple[str, ...]:
    return (STILL_PENDING,) if effects["after"]["status"] == SlotStatus.PENDING else ()


def schedule_result(updated: dict, key: dict, action: PendingAction, before: dict, after: dict, grants: list[dict],
                    stale: tuple[int, ...]) -> CommandResult:
    rebound = [ordinal for ordinal, (old, new) in enumerate(zip(before["reward_definitions"],
                                                                after["reward_definitions"]), 1) if old != new]
    effects = {**slot_effects(key, action, before, after, grants), "rebound_ordinals": rebound,
               "still_changed": list(stale)}
    return CommandResult(updated, effects, (*slot_warnings(effects), *((SCHEDULE_STILL_CHANGED,) if stale else ())))


def split_ticket_mail(state: dict, params: SplitParams, context: CommandContext) -> CommandResult:
    presents = required_service(context, PRESENTS)
    updated, pieces = presents.split_ticket_mail(state, params.present_id)
    remaining = presents.bundled_tickets(updated)
    return CommandResult(updated, {
        "present_id": params.present_id, "type": pieces[0]["Type"], "tickets": len(pieces),
        "present_ids": [row["PresentID"] for row in pieces], "still_bundled": list(remaining)},
        (STILL_BUNDLED,) if remaining else ())


def split_phrase(params: SplitParams) -> str:
    return f"MIGRATE TICKET MAIL {params.present_id}"


def rebuild_missions(state: dict, params: RebuildParams, context: CommandContext) -> CommandResult:
    missions = required_service(context, MISSIONS)
    updated, report = missions.progress.rebuild_changed(missions.initialize(state), context.now)
    if not report:
        raise AdminError(MISSION_REBUILD_UNNEEDED, "Every saved mission row already follows its current definition")
    return CommandResult(updated, {"actions": dict(sorted(Counter(row["action"] for row in report).items())),
                                   "rows": report})


def missions_phrase(params: RebuildParams) -> str:
    return MISSIONS_PHRASE


def resolve_rank_rewards(state: dict, params: RankResolutionParams, context: CommandContext) -> CommandResult:
    rewards = required_service(context, RANK_REWARDS)
    updated, mail_ids = rewards.resolve_changed(state, params.rank, params.action)
    before, after = (read_rank_rewards(value)["entries"] for value in (state, updated))
    resolved = [{"bonus_id": int(key), **changes(entry, after.get(key), RANK_FIELDS)}
                for key, entry in sorted(before.items(), key=lambda item: int(item[0]))
                if entry["rank"] == params.rank and after.get(key) != entry]
    stale = rewards.changed_rewards(updated, params.rank)
    waiting = any(item["after"] is not None and item["after"]["status"] == RANK_PENDING for item in resolved)
    return CommandResult(updated, {"rank": params.rank, "action": params.action.value, "resolved": resolved,
                                   "mail_ids": list(mail_ids), "still_changed": list(stale)},
                         (*((STILL_CHANGED,) if stale else ()), *((STILL_PENDING,) if waiting else ())))


def rank_phrase(params: RankResolutionParams) -> str:
    return f"MIGRATE RANK {params.rank} REWARDS {params.action.value.upper()}"


def rebase_login_rewards(state: dict, params: RebaseParams, context: CommandContext) -> CommandResult:
    total = required_service(context, LOGIN_REWARDS).beginner_total
    before = read_login_rewards(state)
    updated = rebase_beginner(state, beginner_count=total)
    after = read_login_rewards(updated)
    return CommandResult(updated, {
        "eligible": after["beginner_eligible"],
        "total": {"before": before["beginner_total"], "after": after["beginner_total"]},
        "claimed": {"before": before["beginner_claimed"], "after": after["beginner_claimed"]}},
        (BEGINNER_CAPPED,) if after["beginner_claimed"] < before["beginner_claimed"] else ())


def beginner_phrase(params: RebaseParams) -> str:
    return BEGINNER_PHRASE


def rebind_event_pass(state: dict, params: EventPassParams, context: CommandContext) -> CommandResult:
    identity = pass_identity(required_service(context, EVENT_PASSES).catalog.get(params.pass_id))
    updated = rebind_definition(state, params.pass_id, identity)
    before, after = (read_pass_journal(value, params.pass_id) for value in (state, updated))
    return CommandResult(updated, {
        "pass_id": params.pass_id, "identity": {"before": before["definition"], "after": identity},
        "baseline": {track: after[f"baseline_{track}"] for track in TRACKS},
        "archived_thresholds": {track: sorted(map(int, before["deliveries"][track])) for track in TRACKS},
        "archived_identities": len(after[ARCHIVED_KEY])})


def event_pass_phrase(params: EventPassParams) -> str:
    return EVENT_PASS_PHRASE


def resolve_daily_gift(state: dict, params: GiftResolutionParams, context: CommandContext) -> CommandResult:
    updated, grants = required_service(context, DAILY_GIFTS).resolve_changed(state, params.gift_id, params.action,
                                                                              context.now)
    before, after = (read_daily_gifts(value)["gifts"][str(params.gift_id)] for value in (state, updated))
    resolved = [{"earned_day": day, **changes(record, after[day], GIFT_FIELDS)}
                for day, record in sorted(before.items()) if after[day] != record]
    waiting = (params.action == PendingAction.REDELIVER
               and any(item["after"]["status"] == GIFT_PENDING for item in resolved))
    return CommandResult(updated, {"gift_id": params.gift_id, "action": params.action.value, "resolved": resolved,
                                   "mail_ids": [mail for grant in grants for mail in grant["mail_ids"]]},
                         (STILL_PENDING,) if waiting else ())


def daily_gift_phrase(params: GiftResolutionParams) -> str:
    return f"MIGRATE DAILY GIFT {params.gift_id} {params.action.value.upper()}"


def resolve_event_login(state: dict, params: EventLoginResolutionParams, context: CommandContext) -> CommandResult:
    updated, grants = required_service(context, EVENT_LOGINS).resolve_changed(state, params.campaign_id, params.action,
                                                                               context.now)
    before, after = (read_event_logins(value)["campaigns"][str(params.campaign_id)] for value in (state, updated))
    effects = slot_effects({"campaign_id": params.campaign_id}, params.action, before, after, grants)
    return CommandResult(updated, effects, slot_warnings(effects))


def event_login_phrase(params: EventLoginResolutionParams) -> str:
    return f"MIGRATE EVENT LOGIN {params.campaign_id} {params.action.value.upper()}"


def resolve_comeback_login(state: dict, params: ComebackResolutionParams, context: CommandContext) -> CommandResult:
    comebacks = required_service(context, COMEBACK_LOGINS)
    updated, grants = comebacks.resolve_changed(state, params.round_id, params.action, context.now)
    before, after = (read_comeback_logins(value)["rounds"][str(params.round_id)] for value in (state, updated))
    return schedule_result(updated, {"round_id": params.round_id}, params.action, before, after, grants,
                           comebacks.changed_schedule(updated, params.round_id))


def comeback_phrase(params: ComebackResolutionParams) -> str:
    return f"MIGRATE COMEBACK ROUND {params.round_id} {params.action.value.upper()}"


def resolve_purchased_login(state: dict, params: PurchasedResolutionParams, context: CommandContext) -> CommandResult:
    purchases = required_service(context, PURCHASED_LOGINS)
    updated, grants = purchases.resolve_changed(state, params.purchase_id, params.action, context.now)
    before, after = (read_purchased_logins(value)["purchases"][str(params.purchase_id)] for value in (state, updated))
    return schedule_result(updated, {"purchase_id": params.purchase_id}, params.action, before, after, grants,
                           purchases.changed_schedule(updated, params.purchase_id))


def purchased_phrase(params: PurchasedResolutionParams) -> str:
    return f"MIGRATE PURCHASED LOGIN {params.purchase_id} {params.action.value.upper()}"


def current_stamp(rewards: Any, stamp: dict) -> Any:
    # As in the console view, a position the current catalog cannot resolve has no current definition.
    try:
        return rewards.catalog.stamp(stamp["grade"], stamp["month"], stamp["ordinal"])
    except ProtocolError:
        return None


def mail_stamp(rewards: Any, state: dict, stamp: dict, current: Any) -> tuple[dict, list[int]]:
    try:
        mailed, rows = rewards.presents.enqueue(state, [current.to_present_reward(STAMP_DESCRIPTION)])
    except ProtocolError as failure:
        # 115 holds the same failures, so the stamp waits for its next release under the current definition.
        if failure.status != HTTPStatus.NOT_IMPLEMENTED and failure.code not in STAMP_GAPS | CAPACITY_ERRORS:
            raise
        return rebind_stamp(state, stamp["day"], current.definition, failure.code), []
    return release_stamp(mailed, stamp["day"])[0], [row["PresentID"] for row in rows]


def held_stamp(state: dict, day: str) -> dict | None:
    return next((stamp for stamp in read_pending_stamps(state) if stamp["day"] == day), None)


def resolve_login_stamps(state: dict, params: StampResolutionParams, context: CommandContext) -> CommandResult:
    rewards = required_service(context, LOGIN_REWARDS)
    frozen = [(stamp, current_stamp(rewards, stamp)) for stamp in read_pending_stamps(state)
              if stamp["definition"] is not None and month_key(date.fromisoformat(stamp["day"])) == params.month]
    changed = [(stamp, current) for stamp, current in frozen
               if current is None or current.definition != stamp["definition"]]
    if not changed:
        raise AdminError(STAMP_RESOLVE_UNNEEDED, "No pending login stamp of this month has a changed definition")
    voiding = params.action == PendingAction.VOID
    stale = [] if voiding else [stamp["day"] for stamp, current in changed if current is None]
    if len(stale) == len(changed):
        raise AdminError(STAMP_DEFINITION_MISSING, "The current catalog no longer defines these stamps; void them")
    updated, resolved = state, []
    for stamp, current in changed:
        if voiding:
            updated, mail_ids = release_stamp(updated, stamp["day"])[0], []
        elif current is None:
            continue
        else:
            updated, mail_ids = mail_stamp(rewards, updated, stamp, current)
        resolved.append({"day": stamp["day"], **changes(stamp, held_stamp(updated, stamp["day"]), STAMP_FIELDS),
                         "mail_ids": mail_ids})
    waiting = any(item["after"] is not None for item in resolved)
    return CommandResult(updated, {"month": params.month, "action": params.action.value, "resolved": resolved,
                                   "mail_ids": [mail for item in resolved for mail in item["mail_ids"]],
                                   "still_changed": stale},
                         (*((STILL_CHANGED,) if stale else ()), *((STILL_PENDING,) if waiting else ())))


def stamps_phrase(params: StampResolutionParams) -> str:
    return f"MIGRATE LOGIN STAMPS {params.month} {params.action.value.upper()}"


SPLIT_TICKET_MAIL = AdminCommand("presents.split_ticket_mail", PRESENTS, SplitParams, split_ticket_mail,
                                 frozenset({PRESENTS}), destructive=True, confirm_phrase=split_phrase)
REBUILD_MISSIONS = AdminCommand("missions.rebuild_changed", PROGRESS, RebuildParams, rebuild_missions,
                                frozenset({MISSIONS_DOMAIN}), destructive=True, confirm_phrase=missions_phrase,
                                unavailable=requires(MISSIONS))
RESOLVE_RANK_REWARDS = AdminCommand(RANK_REWARD_REPAIR, PROGRESS, RankResolutionParams,
                                    resolve_rank_rewards, frozenset({RANK_REWARDS, PRESENTS}), destructive=True,
                                    confirm_phrase=rank_phrase, unavailable=requires(RANK_REWARDS))
REBASE_BEGINNER = AdminCommand(REBASE_OPERATION, EVENTS, RebaseParams, rebase_login_rewards,
                               frozenset({LOGIN_REWARDS}), destructive=True, confirm_phrase=beginner_phrase,
                               unavailable=requires(LOGIN_REWARDS))
REBIND_EVENT_PASS = AdminCommand(EVENT_PASS_REPAIR, EVENTS, EventPassParams, rebind_event_pass,
                                 frozenset({EVENT_PASS}), destructive=True, confirm_phrase=event_pass_phrase,
                                 unavailable=requires(EVENT_PASSES))
RESOLVE_DAILY_GIFT = AdminCommand(DAILY_GIFT_REPAIR, EVENTS, GiftResolutionParams, resolve_daily_gift,
                                  frozenset({DAILY_GIFT, PRESENTS}), destructive=True, confirm_phrase=daily_gift_phrase,
                                  unavailable=requires(DAILY_GIFTS))
RESOLVE_EVENT_LOGIN = AdminCommand(EVENT_LOGIN_REPAIR, EVENTS, EventLoginResolutionParams,
                                   resolve_event_login, frozenset({EVENT_LOGIN, PRESENTS}), destructive=True,
                                   confirm_phrase=event_login_phrase, unavailable=requires(EVENT_LOGINS))
RESOLVE_COMEBACK_LOGIN = AdminCommand(COMEBACK_LOGIN_REPAIR, EVENTS, ComebackResolutionParams,
                                      resolve_comeback_login, frozenset({COMEBACK_LOGIN, PRESENTS}), destructive=True,
                                      confirm_phrase=comeback_phrase, unavailable=requires(COMEBACK_LOGINS))
RESOLVE_PURCHASED_LOGIN = AdminCommand(PURCHASED_LOGIN_REPAIR, EVENTS, PurchasedResolutionParams,
                                       resolve_purchased_login, frozenset({PURCHASED_LOGIN, PRESENTS}),
                                       destructive=True, confirm_phrase=purchased_phrase,
                                       unavailable=requires(PURCHASED_LOGINS))
RESOLVE_LOGIN_STAMPS = AdminCommand(STAMP_REPAIR, EVENTS, StampResolutionParams,
                                    resolve_login_stamps, frozenset({LOGIN_REWARDS, PRESENTS}), destructive=True,
                                    confirm_phrase=stamps_phrase, unavailable=requires(LOGIN_REWARDS))
COMMANDS = (SPLIT_TICKET_MAIL, REBUILD_MISSIONS, RESOLVE_RANK_REWARDS, REBASE_BEGINNER, REBIND_EVENT_PASS,
            RESOLVE_DAILY_GIFT, RESOLVE_EVENT_LOGIN, RESOLVE_COMEBACK_LOGIN, RESOLVE_PURCHASED_LOGIN,
            RESOLVE_LOGIN_STAMPS)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
