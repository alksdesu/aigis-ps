from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from .admin_cmd_economy import POINT_GRANT, POSSESSIONS, PRESENTS
from .admin_cmd_events import REWARD_DESCRIPTION, mail_count
from .admin_cmd_migrations import EVENT_PASS, EventPassParams
from .admin_cmd_settings import MISSIONS_DOMAIN, PROGRESS
from .admin_command import (AdminCommand, CommandContext, CommandParams, CommandResult, require_distinct,
                            required_service, requires)
from .admin_fields import EVENT_PASSES, MISSIONS
from .admin_registry import CommandRegistry
from .admin_views_events import numeric
from .admin_views_tools import Amount, Identifier
from .errors import ProtocolError
from .event_pass_service import NOTHING_CLAIMABLE as PASS_NOTHING_CLAIMABLE
from .event_pass_state import TRACKS, read_event_passes, read_pass_journal
from .mission_service import MAX_MISSIONS, MissionClaim, MissionSelection
from .presents import MAX_PRESENTS


NOTHING_CLAIMABLE = "Nothing was claimed: no selected mission is finished with its reward unclaimed"
MISSIONS_SKIPPED = "Missions that are unfinished, already claimed or outside their validity dates were skipped"
REWARDS_UNSUPPORTED = "Missions with unsupported reward delivery stay finished and unclaimed, as the game leaves them"
POINTS_DISCARDED = "EP past the pass item's stack or outside its pass window was discarded, as 58 and 59 discard it"
POINTS_CAPPED = "EP stops at the pass item's stack, so the excess was discarded, as 58 and 59 discard it"
PASS_INACTIVE = "The pass is outside its reward window, so the EP was discarded, as 58 and 59 discard it"
NOTHING_REACHED = "Nothing was claimed: every reward the pass points reach was already received"


class MissionClaimParams(CommandParams):
    ids: list[Identifier] = Field(min_length=1, max_length=MAX_MISSIONS)

    @model_validator(mode="after")
    def _distinct(self) -> MissionClaimParams:
        require_distinct(self.ids, "Mission IDs")
        return self


class PassPointParams(EventPassParams):
    amount: Amount


def mission_reward(identifier: int, reward: dict) -> dict:
    return {"ID": identifier, **{name: value for name, value in reward.items() if name != REWARD_DESCRIPTION}}


def discarded_points(claim: MissionClaim) -> bool:
    return any(entry["capped"] or entry["inactive"] for entry in claim.point_audit)


def mission_effects(context: CommandContext, state: dict, selection: MissionSelection,
                    claim: MissionClaim | None) -> dict:
    candidates, after = set(selection.candidates), state if claim is None else claim.state
    return {"claimed": [] if claim is None else [mission_reward(identifier, reward)
                                                 for identifier, reward in zip(claim.claimed, claim.rewards)],
            "skipped": [dict(row) for identifier, row in selection.rows.items() if identifier not in candidates],
            "unavailable": [identifier for identifier in selection.requested if identifier not in selection.rows],
            "unsupported": [] if claim is None else list(claim.unsupported),
            "mail_ids": [] if claim is None else list(claim.mail_ids),
            "mailbox": {"before": mail_count(context, state), "after": mail_count(context, after),
                        "capacity": MAX_PRESENTS},
            "event_pass_points": [] if claim is None else [dict(entry) for entry in claim.point_audit]}


def claim_missions(state: dict, params: MissionClaimParams, context: CommandContext) -> CommandResult:
    missions = required_service(context, MISSIONS)
    selection = missions.select_missions(state, params.ids, context.now)
    # Like 58 and 59, a claim that finds nothing to deliver saves nothing, not even the progress it settles.
    if not selection.candidates:
        return CommandResult(state, mission_effects(context, state, selection, None), (NOTHING_CLAIMABLE,))
    claim = missions.grant_missions(selection, context.now)
    warnings = (*((MISSIONS_SKIPPED,) if selection.skipped else ()),
                *((REWARDS_UNSUPPORTED,) if claim.unsupported else ()),
                *((POINTS_DISCARDED,) if discarded_points(claim) else ()))
    return CommandResult(claim.state, mission_effects(context, state, selection, claim), warnings)


def grant_pass_points(state: dict, params: PassPointParams, context: CommandContext) -> CommandResult:
    passes = required_service(context, EVENT_PASSES)
    updated, audit = passes.grant_points(state, params.pass_id, params.amount, context.now)
    warnings = (*((POINTS_CAPPED,) if audit["capped"] else ()), *((PASS_INACTIVE,) if audit["inactive"] else ()))
    return CommandResult(updated, {**audit, "stack": passes.possessions.stack(audit["pass_id"])}, warnings)


def received(state: dict, pass_id: str) -> dict:
    row = next((row for row in read_event_passes(state) if row["ID"] == pass_id), None)
    return {track: 0 if row is None else row[field] for track, field in TRACKS.items()}


def deliveries(state: dict, pass_id: str) -> dict:
    journal = read_pass_journal(state, pass_id)
    return {track: {} for track in TRACKS} if journal is None else journal["deliveries"]


def delivered(before: dict, after: dict, pass_id: str) -> list[dict]:
    old, new = deliveries(before, pass_id), deliveries(after, pass_id)
    return [{"track": track, "point": int(point), "reward_ids": record["reward_ids"], "mail_ids": record["mail_ids"]}
            for track in TRACKS for point, record in sorted(new[track].items(), key=numeric)
            if point not in old[track]]


def pass_effects(context: CommandContext, passes: Any, entry: Any, before: dict, after: dict,
                 mail_ids: list[int]) -> dict:
    counts = passes.possessions.counts(before)
    return {"pass_id": entry.item_id, "points": counts.get(entry.item_id, 0),
            "premium": counts.get(entry.premium_item_id, 0) > 0,
            "received": {"before": received(before, entry.item_id), "after": received(after, entry.item_id)},
            "delivered": delivered(before, after, entry.item_id), "mail_ids": list(mail_ids),
            "mailbox": {"before": mail_count(context, before), "after": mail_count(context, after),
                        "capacity": MAX_PRESENTS}}


def claim_pass_rewards(state: dict, params: EventPassParams, context: CommandContext) -> CommandResult:
    passes = required_service(context, EVENT_PASSES)
    entry = passes.catalog.get(params.pass_id)
    passes.require_eligible(entry, context.now)
    try:
        updated, mail_ids = passes.deliver(state, entry)
    except ProtocolError as error:
        if error.code != PASS_NOTHING_CLAIMABLE:
            raise
        # Like 159, a pass with nothing left to deliver saves nothing.
        return CommandResult(state, pass_effects(context, passes, entry, state, state, []), (NOTHING_REACHED,))
    return CommandResult(updated, pass_effects(context, passes, entry, state, updated, mail_ids))


CLAIM_MISSIONS = AdminCommand("missions.claim", PROGRESS, MissionClaimParams, claim_missions,
                              frozenset({MISSIONS_DOMAIN, PRESENTS, POSSESSIONS}), unavailable=requires(MISSIONS))
GRANT_POINTS = AdminCommand(POINT_GRANT, PROGRESS, PassPointParams, grant_pass_points, frozenset({POSSESSIONS}),
                            unavailable=requires(EVENT_PASSES))
CLAIM_PASS = AdminCommand("event_pass.claim", PROGRESS, EventPassParams, claim_pass_rewards,
                          frozenset({EVENT_PASS, PRESENTS}), unavailable=requires(EVENT_PASSES))
COMMANDS = (CLAIM_MISSIONS, GRANT_POINTS, CLAIM_PASS)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
