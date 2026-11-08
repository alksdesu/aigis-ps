from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Annotated, Any

from pydantic import AfterValidator, Field, model_validator

from .admin_command import (AdminCommand, AdminError, CommandContext, CommandParams, CommandResult, counted,
                            require_distinct, required_service, requires, requires_all)
from .admin_diff import json_digest
from .admin_fields import EVOLUTION_REWARDS, GIFTS, OVERRIDES_KEY, UNIT_OVERRIDES
from .admin_registry import CommandRegistry
from .errors import ProtocolError
from .native_numbers import INT32_MAX
from .player_state import MAX_PLAYER_UNITS
from .storage import serialize
from .unit_grants import MAX_GRANTS, UnitGrants
from .unit_growth_edit import GROWTH_FIELDS, GrowthEdit, UnitGrowthEditor
from .unit_reference import BARRACKS_STORAGE, FIRST_BARRACKS, UnitReference
from .unit_removal import Protection, UnitIdentity, UnitRemoval, UnitsProtected


UNITS, SQUADS = "units", "squads"
UNIT_SUBJECT = "Unit"
GRANT_ERRORS, REMOVAL_ERRORS, GROWTH_ERRORS = "admin_unit_grant", "admin_unit_removal", "admin_unit_growth"
OVERRIDES_ABSENT = "admin_unit_overrides_absent"
UNIT_SUMMARY = ("GetIndex", "UniqueID", "CardID", "ClassID", "ClassChangeCount")
CAPACITY_REASON = "Unit grants need the verified barracks capacity"
INITIAL_STAGES = (0, 2)
ABSENT, EMPTY, ENTRIES, INVALID = "absent", "empty", "entries", "invalid"
OVERRIDE_COLLECTIONS = (dict, list)
# Training refuses truthy values while 106 and 113 also refuse non-collections, so only null or empty passes all three.
BLOCKING_SHAPES = frozenset((ENTRIES, INVALID))
OVERRIDES_PHRASE = "REMOVE UNIT OVERRIDES"
NOTHING_BLOCKED = "The removed overrides were empty, so they blocked no growth flow"


def summary(unit: dict) -> dict:
    return {name: unit[name] for name in UNIT_SUMMARY}


def protection_names(found: Iterable[Protection]) -> list[str]:
    return [str(item) for item in found]


def protected_rows(protected: Mapping[int, Iterable[Protection]]) -> list[dict]:
    return [{"GetIndex": index, "protections": protection_names(found)} for index, found in protected.items()]


@contextmanager
def protection_details(refusal: type[ProtocolError]) -> Iterator[None]:
    try:
        yield
    except refusal as error:
        raise AdminError(error.code, str(error), error.status,
                         details={"protected": protected_rows(error.protected)}) from error


@dataclass(frozen=True)
class OverrideReport:
    shape: str
    entries: int | None = None
    bytes: int | None = None
    sha256: str | None = None

    @property
    def present(self) -> bool:
        return self.shape != ABSENT

    @property
    def blocking(self) -> bool:
        return self.shape in BLOCKING_SHAPES

    def to_dict(self) -> dict:
        return {"present": self.present, "blocking": self.blocking, **asdict(self)}


def override_shape(value: Any) -> str:
    collection = isinstance(value, OVERRIDE_COLLECTIONS)
    if value is None or (collection and not value):
        return EMPTY
    return ENTRIES if collection else INVALID


def override_report(state: dict) -> OverrideReport:
    if OVERRIDES_KEY not in state:
        return OverrideReport(ABSENT)
    value = state[OVERRIDES_KEY]
    return OverrideReport(override_shape(value), len(value) if isinstance(value, OVERRIDE_COLLECTIONS) else None,
                          len(serialize(value).encode("utf-8")), json_digest(value))


def _initial_stage(value: int) -> int:
    if value not in INITIAL_STAGES:
        raise ValueError(f"New units start at stage {' or '.join(map(str, INITIAL_STAGES))}")
    return value


InitialStage = Annotated[int, AfterValidator(_initial_stage), Field(json_schema_extra={"enum": list(INITIAL_STAGES)})]


class InitialParams(CommandParams):
    card_id: int = Field(gt=1, le=INT32_MAX)
    level: int = Field(ge=0, le=INT32_MAX)
    evolution_count: InitialStage = 0


class GrantParams(InitialParams):
    count: int = Field(default=1, ge=1, le=MAX_GRANTS)


class UnitParams(CommandParams):
    get_index: int = Field(ge=0, le=INT32_MAX)
    unique_id: int = Field(ge=1, le=INT32_MAX)

    def identity(self) -> UnitIdentity:
        return UnitIdentity(self.get_index, self.unique_id)


class CardUnitParams(UnitParams):
    card_id: int = Field(ge=1, le=INT32_MAX)

    def reference(self, barracks: int = FIRST_BARRACKS) -> UnitReference:
        return UnitReference(BARRACKS_STORAGE, barracks, self.get_index, self.unique_id, self.card_id)


def distinct_indexes(indexes: Iterable[int], subject: str = UNIT_SUBJECT) -> None:
    require_distinct(indexes, f"{subject} GetIndex values")


def distinct_units(units: Iterable[UnitParams], subject: str = UNIT_SUBJECT) -> None:
    units = tuple(units)
    distinct_indexes((unit.get_index for unit in units), subject)
    require_distinct((unit.unique_id for unit in units), f"{subject} UniqueID values")


class RemoveParams(CommandParams):
    units: list[UnitParams] = Field(min_length=1, max_length=MAX_PLAYER_UNITS)

    @model_validator(mode="after")
    def _distinct(self) -> RemoveParams:
        distinct_units(self.units)
        return self


class GrowthParams(UnitParams):
    experience: int | None = Field(default=None, ge=0, le=INT32_MAX)
    love: int | None = Field(default=None, ge=0, le=INT32_MAX)
    skill_level: int | None = Field(default=None, ge=1, le=INT32_MAX)

    @model_validator(mode="after")
    def _edits_something(self) -> GrowthParams:
        if self.experience is None and self.love is None and self.skill_level is None:
            raise ValueError("A growth edit sets experience, love or skill_level")
        return self


class OverrideRemovalParams(CommandParams):
    pass


def grant(state: dict, params: GrantParams, context: CommandContext) -> CommandResult:
    grants = UnitGrants(required_service(context, "player_state"), error_prefix=GRANT_ERRORS)
    updated, added = grants.grant(state, [params.model_dump()])
    return CommandResult(updated, {"initialization_version": grants.initialization_version,
                                   "added": [summary(unit) for unit in added]})


def preview_initial(state: dict, params: InitialParams, context: CommandContext) -> CommandResult:
    players = required_service(context, "player_state")
    initial = players.initial_unit_values(params.card_id, params.level, evolution_count=params.evolution_count)
    return CommandResult(state, {"initialization_version": players.initialization_version, "initial": asdict(initial)})


def remove(state: dict, params: RemoveParams, context: CommandContext) -> CommandResult:
    removal = UnitRemoval(required_service(context, "player_state"), required_service(context, GIFTS),
                          required_service(context, EVOLUTION_REWARDS), error_prefix=REMOVAL_ERRORS)
    with protection_details(UnitsProtected):
        result = removal.remove(state, context.revision, [unit.identity() for unit in params.units])
    return CommandResult(result.state, {"removed": [summary(unit) for unit in result.removed],
                                        "last_unique_id": result.state["player_state"]["last_unique_id"]})


def removal_phrase(params: RemoveParams) -> str:
    return f"REMOVE {counted(len(params.units), 'UNIT')}"


def edit_growth(state: dict, params: GrowthParams, context: CommandContext) -> CommandResult:
    editor = UnitGrowthEditor(required_service(context, "player_state"), required_service(context, "training").catalog,
                              required_service(context, GIFTS), error_prefix=GROWTH_ERRORS)
    result = editor.edit(state, context.revision, params.identity(),
                         GrowthEdit(params.experience, params.love, params.skill_level))
    changes = {name: {"before": result.before[name], "after": result.after[name]}
               for name in GROWTH_FIELDS if result.before[name] != result.after[name]}
    return CommandResult(result.state, {"unit": summary(result.after), "changes": changes,
                                        "limits": editor.limits(result.after).to_dict()}, result.warnings)


def remove_overrides(state: dict, params: OverrideRemovalParams, context: CommandContext) -> CommandResult:
    report = override_report(state)
    if not report.present:
        raise AdminError(OVERRIDES_ABSENT, f"The save has no {OVERRIDES_KEY} to remove")
    updated = {key: value for key, value in state.items() if key != OVERRIDES_KEY}
    return CommandResult(updated, {"removed": report.to_dict()}, () if report.blocking else (NOTHING_BLOCKED,))


def overrides_phrase(params: OverrideRemovalParams) -> str:
    return OVERRIDES_PHRASE


GRANT = AdminCommand("units.grant", UNITS, GrantParams, grant, frozenset({UNITS, SQUADS}),
                     unavailable=requires("unit_capacity", CAPACITY_REASON))
PREVIEW_INITIAL = AdminCommand("units.preview_initial", UNITS, InitialParams, preview_initial, frozenset({UNITS}))
REMOVE = AdminCommand("units.remove", UNITS, RemoveParams, remove, frozenset({UNITS, GIFTS, EVOLUTION_REWARDS}),
                      destructive=True, confirm_phrase=removal_phrase,
                      unavailable=requires_all(GIFTS, EVOLUTION_REWARDS))
EDIT_GROWTH = AdminCommand("units.edit_growth", UNITS, GrowthParams, edit_growth, frozenset({UNITS, GIFTS}),
                           unavailable=requires_all("training", GIFTS))
REMOVE_OVERRIDES = AdminCommand("units.overrides.remove", UNITS, OverrideRemovalParams, remove_overrides,
                                frozenset({UNIT_OVERRIDES}), destructive=True, confirm_phrase=overrides_phrase)
COMMANDS = (GRANT, PREVIEW_INITIAL, REMOVE, EDIT_GROWTH, REMOVE_OVERRIDES)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
