from __future__ import annotations

from copy import deepcopy
from typing import Annotated

from pydantic import Field, model_validator

from . import quest_record_admin
from .admin_cmd_economy import POSSESSIONS, PRESENTS, QUESTS, RANK_REWARDS
from .admin_cmd_settings import MISSIONS_DOMAIN
from .admin_command import (AdminCommand, CommandContext, CommandParams, CommandResult, first_unavailable,
                            require_distinct, required_service, requires)
from .admin_fields import MISSIONS
from .admin_registry import CommandRegistry
from .admin_views_tools import Identifier
from .errors import ProtocolError
from .masterdata import SNAPSHOT_ID_LENGTH
from .native_numbers import INT32_MAX, UINT32_MASK
from .present_backlog import PresentBacklog
from .public_tables import validate_public_master
from .quest_record_admin import EDITABLE_FIELDS, MAX_QUEST_RECORDS, QUEST_ID, QUEST_MASTER, UNPLAYED
from .quest_service import JOURNAL_KEY, abort as abort_attempt, journal as read_journal


BATTLE = "battle"
BOOTSTRAP, MASTERS, PLAYER_STATE = "bootstrap", "masters", "player_state"
QUEST_RECORDS, CRYSTALS, UNITS = "quest_records", "crystals", "units"
ABORT_PHRASE = "ABORT BATTLE"
ARCHIVE_PHRASE = "ARCHIVE MISSING RECORDS {snapshot}"
NOTHING_PENDING = "No battle presents are waiting for the mailbox"
STILL_WAITING = "Presents the mailbox could not take stay queued in the battle journal"
NOTHING_MISSING = "Every saved quest record exists in the named master snapshot, so nothing was archived"
RECORDS_SKIPPED = "Archived records absent from the running master snapshot stay archived"
Counter = Annotated[int, Field(ge=0, le=INT32_MAX)]
SnapshotId = Annotated[str, Field(min_length=SNAPSHOT_ID_LENGTH, max_length=SNAPSHOT_ID_LENGTH, pattern=r"^[a-f0-9]+$")]
Stars = Annotated[int, Field(ge=1, le=4)]


class RecordFieldsParams(CommandParams):
    QuestID: Identifier
    MaxRank: Counter | None = None
    AchieveFlag: Counter | None = None
    RemainAssault: Counter | None = None
    FirstDefeatDate: Counter | None = None
    Shown: bool | None = None

    def changes(self) -> dict:
        return {name: getattr(self, name) for name in EDITABLE_FIELDS if getattr(self, name) is not None}


class RecordEditParams(RecordFieldsParams):
    @model_validator(mode="after")
    def _some_change(self) -> RecordEditParams:
        if not self.changes():
            raise ValueError("Record edits name at least one of the five editable record fields")
        return self


class RecordRewardParams(RecordFieldsParams):
    stars: Stars
    hard: bool


class JournalParams(CommandParams):
    pass


class ArchiveParams(CommandParams):
    master_snapshot: SnapshotId


class RestoreParams(CommandParams):
    quest_ids: list[Identifier] = Field(min_length=1, max_length=MAX_QUEST_RECORDS)

    @model_validator(mode="after")
    def _distinct(self) -> RestoreParams:
        require_distinct(self.quest_ids, "Quest IDs")
        return self


def record_effects(quest_id: int, before: dict | None, after: dict) -> dict:
    return {"quest_id": quest_id, "record": {"before": before, "after": after}}


def edit_record(state: dict, params: RecordEditParams, context: CommandContext) -> CommandResult:
    bootstrap = required_service(context, BOOTSTRAP)
    updated, before, after = quest_record_admin.edit_record(state, bootstrap, params.QuestID, params.changes())
    return CommandResult(updated, record_effects(params.QuestID, before, after))


def edit_record_with_rewards(state: dict, params: RecordRewardParams, context: CommandContext) -> CommandResult:
    bootstrap = required_service(context, BOOTSTRAP)
    changes = params.changes()
    if changes:
        updated, before, after = quest_record_admin.edit_record(state, bootstrap, params.QuestID, changes)
    else:
        if params.QuestID not in quest_record_admin.master_quest_ids(bootstrap):
            raise ProtocolError("quest_not_found", "Quest is absent from the fixed master snapshot", 404)
        before = quest_record_admin.quest_records(state, bootstrap).get(params.QuestID)
        updated, after = state, before if before is not None else {QUEST_ID: params.QuestID, **UNPLAYED}
    updated, rewards, warnings = quest_record_admin.grant_clear_rewards(
        updated, quests=required_service(context, QUESTS), missions=required_service(context, MISSIONS),
        rank_rewards=required_service(context, RANK_REWARDS), players=required_service(context, PLAYER_STATE),
        possessions=required_service(context, POSSESSIONS), presents=required_service(context, PRESENTS),
        quest_id=params.QuestID, stars=params.stars, hard=params.hard,
        previous_flags=(before or UNPLAYED)["AchieveFlag"] & UINT32_MASK,
        current_flags=after["AchieveFlag"] & UINT32_MASK, now=context.now, acquisition_day=context.acquisition_day)
    return CommandResult(updated, {**record_effects(params.QuestID, before, after), "rewards": rewards}, warnings)


def abort_battle(state: dict, params: JournalParams, context: CommandContext) -> CommandResult:
    active = read_journal(state)["active"]
    updated = abort_attempt(state, context.now)
    aborted = {} if not isinstance(active, dict) else {
        name: active.get(name) for name in ("quest_id", "sequence", "start_key")}
    return CommandResult(updated, {"aborted": aborted})


def abort_phrase(params: JournalParams) -> str:
    return ABORT_PHRASE


def flush_pending(state: dict, params: JournalParams, context: CommandContext) -> CommandResult:
    presents = required_service(context, PRESENTS)
    journal = read_journal(state)
    pending = journal.get("pending_presents", [])
    if not pending:
        return CommandResult(state, {"delivered": [], "waiting": []}, (NOTHING_PENDING,))
    backlog = PresentBacklog(presents, pending)
    updated, delivered = backlog.flush(deepcopy(state))
    waiting = backlog.entries
    if waiting:
        journal["pending_presents"] = waiting
    else:
        journal.pop("pending_presents", None)
    updated[JOURNAL_KEY] = journal
    return CommandResult(updated, {"delivered": delivered, "waiting": waiting},
                         (STILL_WAITING,) if waiting else ())


def _staged_master_ids(context: CommandContext, identifier: str) -> frozenset[int]:
    snapshot = required_service(context, MASTERS).snapshot(identifier)
    schemas = required_service(context, PLAYER_STATE).master_schemas
    document, _ = validate_public_master(QUEST_MASTER, snapshot.get_xml(QUEST_MASTER), schemas)
    return frozenset(next(column.values for column in document.children if column.name == QUEST_ID))


def archive_missing(state: dict, params: ArchiveParams, context: CommandContext) -> CommandResult:
    bootstrap = required_service(context, BOOTSTRAP)
    updated, moved = quest_record_admin.archive_missing(state, bootstrap,
                                                        _staged_master_ids(context, params.master_snapshot))
    effects = {"master_snapshot": params.master_snapshot, "archived": moved,
               "archive_size": len(quest_record_admin.read_archive(updated))}
    return CommandResult(updated, effects, (NOTHING_MISSING,) if not moved else ())


def archive_phrase(params: ArchiveParams) -> str:
    return ARCHIVE_PHRASE.format(snapshot=params.master_snapshot)


def restore_archived(state: dict, params: RestoreParams, context: CommandContext) -> CommandResult:
    bootstrap = required_service(context, BOOTSTRAP)
    updated, restored, skipped = quest_record_admin.restore_archived(
        state, bootstrap, params.quest_ids, quest_record_admin.master_quest_ids(bootstrap))
    effects = {"restored": restored, "skipped_missing_master": skipped,
               "archive_size": len(quest_record_admin.read_archive(updated))}
    return CommandResult(updated, effects, (RECORDS_SKIPPED,) if skipped else ())


REWARD_SERVICES = first_unavailable(requires(QUESTS), requires(MISSIONS), requires(RANK_REWARDS))
RECORD_EDIT = AdminCommand("quests.record.edit", BATTLE, RecordEditParams, edit_record, frozenset({QUEST_RECORDS}))
RECORD_EDIT_WITH_REWARDS = AdminCommand(
    "quests.record.edit_with_rewards", BATTLE, RecordRewardParams, edit_record_with_rewards,
    frozenset({QUEST_RECORDS, MISSIONS_DOMAIN, RANK_REWARDS, PRESENTS, POSSESSIONS, CRYSTALS, UNITS}),
    unavailable=REWARD_SERVICES)
JOURNAL_ABORT = AdminCommand("quests.journal.abort", BATTLE, JournalParams, abort_battle, frozenset({JOURNAL_KEY}),
                             destructive=True, confirm_phrase=abort_phrase)
JOURNAL_FLUSH = AdminCommand("quests.journal.flush_pending", BATTLE, JournalParams, flush_pending,
                             frozenset({JOURNAL_KEY, PRESENTS}))
RECORDS_ARCHIVE = AdminCommand("quests.records.archive_missing", BATTLE, ArchiveParams, archive_missing,
                               frozenset({QUEST_RECORDS}), destructive=True, confirm_phrase=archive_phrase)
RECORDS_RESTORE = AdminCommand("quests.records.restore_archived", BATTLE, RestoreParams, restore_archived,
                               frozenset({QUEST_RECORDS}))
COMMANDS = (RECORD_EDIT, RECORD_EDIT_WITH_REWARDS, JOURNAL_ABORT, JOURNAL_FLUSH, RECORDS_ARCHIVE, RECORDS_RESTORE)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
