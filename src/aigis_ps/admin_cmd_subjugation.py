from __future__ import annotations

from http import HTTPStatus

from .admin_command import (AdminCommand, AdminError, COMMAND_UNAVAILABLE, CommandContext, CommandParams,
                            CommandResult, first_unavailable, missing_service, required_service, requires)
from .admin_registry import CommandRegistry
from .admin_views_tools import Identifier
from .quest_service import JOURNAL_KEY, journal as read_journal
from .subjugation_rules import (RECORDS_BELOW, ScorePair, redeliver, reset_progress, resync_watermark, scores_view,
                                watermark, watermark_mismatch)


BATTLE = "battle"
QUESTS, PRESENTS = "quests", "presents"
QUEST_RECORDS = "quest_records"
NO_CATALOG = "This generation has no subjugation catalog"
RESET_PHRASE = "RESET SUBJUGATION {mission_id}"
NOTHING_OWED = "The watermark already covers the mission records, so nothing was mailed"
REDELIVERY_RISK = "Cleared records and a cleared watermark let future settlements mail every ladder tier again"
LOWERED_REDELIVERY_RISK = ("A watermark lowered to the records lets future settlements mail the ladder tiers between "
                           "them and the previous watermark again")


class MissionParams(CommandParams):
    mission_id: Identifier


def _subjugation_unavailable(services) -> str | None:
    quests = getattr(services, QUESTS, None)
    if quests is None:
        return missing_service(QUESTS)
    return None if getattr(quests, "subjugation", None) is not None else NO_CATALOG


def _catalog(context: CommandContext):
    catalog = getattr(required_service(context, QUESTS), "subjugation", None)
    if catalog is None:
        raise AdminError(COMMAND_UNAVAILABLE, NO_CATALOG, HTTPStatus.SERVICE_UNAVAILABLE)
    return catalog


def _require_idle(state: dict, mission_id: int) -> None:
    active = read_journal(state)["active"]
    if isinstance(active, dict) and active.get("mission_id") == mission_id:
        raise AdminError("quest_battle_active",
                         f"Mission {mission_id} has a battle in progress; settle or abort it first")


def _journaled(state: dict) -> dict:
    return {**state, JOURNAL_KEY: read_journal(state)}


def _effects(mission_id: int, before: ScorePair | None, after: ScorePair | None, **extra) -> dict:
    return {"mission_id": mission_id, "watermark": {"before": scores_view(before), "after": scores_view(after)}, **extra}


def resync(state: dict, params: MissionParams, context: CommandContext) -> CommandResult:
    catalog = _catalog(context)
    _require_idle(state, params.mission_id)
    prepared = _journaled(state)
    before = watermark(prepared, params.mission_id)
    updated, recorded = resync_watermark(catalog, prepared, params.mission_id)
    lowered = RECORDS_BELOW in watermark_mismatch(before, recorded).values()
    return CommandResult(updated, _effects(params.mission_id, before, recorded),
                         (LOWERED_REDELIVERY_RISK,) if lowered else ())


def redeliver_owed(state: dict, params: MissionParams, context: CommandContext) -> CommandResult:
    catalog = _catalog(context)
    presents = required_service(context, PRESENTS)
    _require_idle(state, params.mission_id)
    prepared = _journaled(state)
    before = watermark(prepared, params.mission_id)
    updated, mailed, reached = redeliver(catalog, prepared, params.mission_id, presents)
    effects = _effects(params.mission_id, before, reached, mailed=mailed)
    return CommandResult(updated, effects, () if mailed else (NOTHING_OWED,))


def reset(state: dict, params: MissionParams, context: CommandContext) -> CommandResult:
    catalog = _catalog(context)
    _require_idle(state, params.mission_id)
    before = watermark(state, params.mission_id)
    updated, cleared = reset_progress(catalog, state, params.mission_id)
    return CommandResult(updated, _effects(params.mission_id, before, None, cleared_quests=cleared),
                         (REDELIVERY_RISK,))


def reset_phrase(params: MissionParams) -> str:
    return RESET_PHRASE.format(mission_id=params.mission_id)


SUBJUGATION_PRESENTS = first_unavailable(_subjugation_unavailable, requires(PRESENTS))
WATERMARK_RESYNC = AdminCommand("subjugation.watermark.resync", BATTLE, MissionParams, resync,
                                frozenset({QUEST_RECORDS, JOURNAL_KEY}), unavailable=_subjugation_unavailable)
REWARDS_REDELIVER = AdminCommand("subjugation.rewards.redeliver", BATTLE, MissionParams, redeliver_owed,
                                 frozenset({QUEST_RECORDS, JOURNAL_KEY, PRESENTS}), unavailable=SUBJUGATION_PRESENTS)
PROGRESS_RESET = AdminCommand("subjugation.progress.reset", BATTLE, MissionParams, reset,
                              frozenset({QUEST_RECORDS, JOURNAL_KEY}), destructive=True, confirm_phrase=reset_phrase,
                              unavailable=_subjugation_unavailable)
COMMANDS = (WATERMARK_RESYNC, REWARDS_REDELIVER, PROGRESS_RESET)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
