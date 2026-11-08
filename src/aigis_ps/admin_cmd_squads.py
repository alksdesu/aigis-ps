from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from typing import Annotated

from pydantic import AfterValidator, Field, create_model, model_validator

from .admin_cmd_settings import CONFIG, SETTINGS
from .admin_cmd_units import SQUADS, UNITS, UnitParams, distinct_units, summary
from .admin_command import (AdminCommand, AdminError, CommandContext, CommandParams, CommandResult, require_distinct,
                            required_service, requires)
from .admin_registry import CommandRegistry
from .alxml import Column, Container, ValueType, dumps
from .native_numbers import INT32_MAX, INT32_MIN
from .player_state import (EMPTY_SQUAD_SLOT, LOCK_FLAG, MAX_PLAYER_UNITS, MAX_SQUAD_NAME_BYTES, MAX_SQUAD_ROWS,
                           MAX_SQUAD_SLOT, RESERVED_SQUAD_GROUP, SQUAD_TEAM_MASK, squad_address, squad_group,
                           squad_position)
from .unit_actions import AUTO_SELECT_FIELDS, SORT_BYTE_LIMITS, SORT_FIELDS


PLAYER_STATE, CURRENT_SQUAD, UNIT_ACTIONS, BOOTSTRAP = "player_state", "current_squad", "unit_actions", "bootstrap"
TEXT_PROBE = "Text"
LOCK_MISSING = "admin_unit_lock_missing"
METADATA_REQUIRED = "admin_squad_metadata_required"
CURRENT_SQUAD_REASON = "Squad saves need the verified team views and title catalog of the current client"
# The client rebuilds squads from members with a positive GetIndex only, so the prince's 0 never shows in a team.
UNSHOWN_MEMBER = 0


def native_text(limit: int) -> AfterValidator:
    def check(text: str) -> str:
        if len(text.encode("utf-8")) > limit:
            raise ValueError(f"At most {limit} UTF-8 bytes fit the native field")
        # Game requests arrive as XML, so text the serializer refuses could never reach the field from the client.
        dumps(Container(children=(Column(TEXT_PROBE, ValueType.STRING, (text,)),)))
        return text

    return AfterValidator(check)


class SquadMember(CommandParams):
    team: int = Field(ge=0, le=SQUAD_TEAM_MASK)
    slot: int = Field(ge=0, le=MAX_SQUAD_SLOT)
    get_index: int | None = Field(default=None, ge=0, le=INT32_MAX, description="An empty slot when null")


class SquadMetadata(CommandParams):
    name: Annotated[str, native_text(MAX_SQUAD_NAME_BYTES)]
    title: int = Field(ge=0, le=INT32_MAX, description="Physical PlayerTitle row")


class SquadParams(CommandParams):
    group: int = Field(ge=0, lt=RESERVED_SQUAD_GROUP)
    members: list[SquadMember] | None = Field(default=None, min_length=1, max_length=MAX_SQUAD_ROWS,
                                              description="Every slot the squad keeps; slots left out are removed")
    metadata: SquadMetadata | None = None
    select: bool = Field(default=False, description="Make the squad the current ordinary or raid team")

    @model_validator(mode="after")
    def _consistent(self) -> SquadParams:
        if self.members is None and self.metadata is None and not self.select:
            raise ValueError("A squad save sets members, metadata or the current selection")
        if self.members is not None:
            require_distinct(((member.team, member.slot) for member in self.members), "Member slots")
            require_distinct(((member.team, member.get_index) for member in self.members if member.get_index is not None),
                             "Units within one team")
        return self


def squad_rows(player: dict, group: int) -> list[dict]:
    return [row for row in player["squads"] if squad_group(row) == group]


def member(row: dict) -> dict:
    team, slot = squad_position(row)
    return {"team": team, "slot": slot, "GetIndex": None if row["UnitIndex"] == EMPTY_SQUAD_SLOT else row["UnitIndex"]}


def squad_summary(player: dict, group: int, column: str) -> dict:
    rows = squad_rows(player, group)
    return {"selected": player["battle"][column] == group,
            "metadata": [{"name": name, "title": title} for name, title in sorted({(row["Name"], row["Flags"]) for row in rows})],
            "members": [member(row) for row in rows]}


def stored_metadata(player: dict, group: int) -> tuple[str, int]:
    found = {(row["Name"], row["Flags"]) for row in squad_rows(player, group)}
    if len(found) != 1:
        message = "Members of a squad without one stored name and title need metadata"
        raise AdminError(METADATA_REQUIRED, message, HTTPStatus.UNPROCESSABLE_ENTITY,
                         fields=({"loc": ["metadata"], "type": METADATA_REQUIRED, "msg": message},))
    return found.pop()


def save_squad(state: dict, params: SquadParams, context: CommandContext) -> CommandResult:
    players = required_service(context, PLAYER_STATE)
    squads = required_service(context, CURRENT_SQUAD, CURRENT_SQUAD_REASON)
    column = squads.column(params.group)
    # Saving any part would otherwise close the pending legacy import that still holds the old members.
    squads.require_imported(state)
    updated = state
    if params.metadata is not None:
        updated = squads.set_metadata(updated, params.group, params.metadata.name, params.metadata.title)
    if params.members is not None:
        name, title = stored_metadata(players.validate(updated, references=False), params.group)
        rows = [{"Address": squad_address(params.group, member.team, member.slot),
                 "UnitIndex": EMPTY_SQUAD_SLOT if member.get_index is None else member.get_index,
                 "Name": name, "Flags": title} for member in params.members]
        # Only a client that still lists consumed units gets their slots emptied; console members must be owned.
        updated = players.replace_squads(updated, rows, vacate_consumed=False).state
    if params.select:
        updated = squads.select(updated, params.group)
    warnings = tuple(f"Team {member.team} slot {member.slot} holds GetIndex {UNSHOWN_MEMBER}, which the client leaves out"
                     for member in params.members or () if member.get_index == UNSHOWN_MEMBER)
    before, after = (players.validate(current, references=False) for current in (state, updated))
    return CommandResult(updated, {"group": params.group, "column": column,
                                   "before": squad_summary(before, params.group, column),
                                   "after": squad_summary(after, params.group, column)}, warnings)


class LockChoice(UnitParams):
    locked: bool


class LockParams(CommandParams):
    units: list[LockChoice] = Field(min_length=1, max_length=MAX_PLAYER_UNITS)

    @model_validator(mode="after")
    def _distinct(self) -> LockParams:
        distinct_units(self.units)
        return self


def locked(unit: dict) -> bool:
    return bool(unit["GeneralPurposeFlags"] & LOCK_FLAG)


def set_locks(state: dict, params: LockParams, context: CommandContext) -> CommandResult:
    owned = {unit["GetIndex"]: unit for unit in required_service(context, PLAYER_STATE).validate(state)["units"]}
    missing = [choice.get_index for choice in params.units
               if choice.get_index not in owned or owned[choice.get_index]["UniqueID"] != choice.unique_id]
    if missing:
        raise AdminError(LOCK_MISSING, "Lock selection does not match an owned unit", details={"get_index": missing})
    change = required_service(context, UNIT_ACTIONS).set_locks(state, {choice.get_index: choice.locked
                                                                       for choice in params.units})
    return CommandResult(change.state, {"units": [
        {"unit": summary(owned[unit["GetIndex"]]), "locked": {"before": locked(owned[unit["GetIndex"]]), "after": locked(unit)}}
        for unit in change.units]})


SortParams = create_model("SortParams", __base__=CommandParams,
                          **{wire: (Annotated[str, native_text(SORT_BYTE_LIMITS[wire])], Field(title=name))
                             for wire, name in SORT_FIELDS.items()})
AutoSelectParams = create_model("AutoSelectParams", __base__=CommandParams,
                                **{wire: (int, Field(ge=INT32_MIN, le=INT32_MAX, title=name))
                                   for wire, name in AUTO_SELECT_FIELDS.items()})
SORT_WRITES = frozenset(f"{CONFIG}.{name}" for name in SORT_FIELDS.values())
AUTO_SELECT_WRITES = frozenset(f"{CONFIG}.{name}" for name in AUTO_SELECT_FIELDS.values())


def preference_changes(context: CommandContext, state: dict, aliases: Mapping[str, str], preferences: dict) -> list[dict]:
    before = required_service(context, BOOTSTRAP).table_rows(state, CONFIG)[0]
    return [{"field": name, "wire": wire, "before": before[name], "after": preferences[wire]}
            for wire, name in aliases.items() if before[name] != preferences[wire]]


def set_sort(state: dict, params: CommandParams, context: CommandContext) -> CommandResult:
    preferences = params.model_dump()
    changes = preference_changes(context, state, SORT_FIELDS, preferences)
    return CommandResult(required_service(context, UNIT_ACTIONS).set_sort(state, preferences), {"fields": changes})


def set_auto_select(state: dict, params: CommandParams, context: CommandContext) -> CommandResult:
    preferences = params.model_dump()
    changes = preference_changes(context, state, AUTO_SELECT_FIELDS, preferences)
    return CommandResult(required_service(context, UNIT_ACTIONS).set_auto_select(state, preferences), {"fields": changes})


SQUAD_SAVE = AdminCommand("squads.save", UNITS, SquadParams, save_squad, frozenset({SQUADS}),
                          unavailable=requires(CURRENT_SQUAD, CURRENT_SQUAD_REASON))
SET_LOCK = AdminCommand("units.set_lock", UNITS, LockParams, set_locks, frozenset({UNITS}), unavailable=requires(UNIT_ACTIONS))
SORT = AdminCommand("config.sort.set", SETTINGS, SortParams, set_sort, frozenset({CONFIG}), writes=SORT_WRITES,
                    unavailable=requires(UNIT_ACTIONS))
AUTO_SELECT = AdminCommand("config.auto_select.set", SETTINGS, AutoSelectParams, set_auto_select, frozenset({CONFIG}),
                           writes=AUTO_SELECT_WRITES, unavailable=requires(UNIT_ACTIONS))
COMMANDS = (SQUAD_SAVE, SET_LOCK, SORT, AUTO_SELECT)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
