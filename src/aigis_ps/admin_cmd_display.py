from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import Field, create_model, model_validator

from .admin_cmd_settings import CONFIG, SETTINGS
from .admin_cmd_units import UNITS, UnitParams, summary
from .admin_command import (BOOLEAN_HINT, AdminCommand, AdminError, CommandContext, CommandParams, CommandResult,
                            require_distinct, required_service, requires)
from .admin_registry import CommandRegistry
from .blessing_service import FILTER_FIELD
from .config_state import configuration_row
from .native_numbers import INT32_MAX
from .player_state import DISPLAY_FIELD
from .possessions import MAX_POSSESSION_ITEMS
from .view_events import EPISODES, EVENT_VIEW_FIELD, NO_REQUEST, REQUEST_KEYS


POSSESSIONS = "possessions"
ENABLED = "Enabled"
DISPLAY_TARGET_MISSING = "admin_unit_display_missing"
FILTER_WRITES = frozenset({f"{CONFIG}.{FILTER_FIELD}"})
EVENT_VIEW_WRITES = frozenset({f"{CONFIG}.{EVENT_VIEW_FIELD}"})
VIEW_EVENT_GROUP = "view_event"
NOTHING_PENDING = "No event playback was pending, so nothing was cleared"
REQUEST_HINTS = {"Id": f"Episode {' / '.join(map(str, EPISODES))}", "Mode": BOOLEAN_HINT, "Male": BOOLEAN_HINT}


class DisplayParams(UnitParams):
    display: int = Field(ge=0, le=INT32_MAX, title=DISPLAY_FIELD)


def display_target(players: Any, state: dict, params: UnitParams) -> dict:
    unit = next((unit for unit in players.validate(state)["units"] if unit["GetIndex"] == params.get_index), None)
    if unit is None or unit["UniqueID"] != params.unique_id:
        raise AdminError(DISPLAY_TARGET_MISSING, "Display target does not match an owned unit")
    return unit


def set_display(state: dict, params: DisplayParams, context: CommandContext) -> CommandResult:
    unit = display_target(required_service(context, "player_state"), state, params)
    # 10 also switches skills relative to the saved flags; the console sets only the absolute display.
    updated = required_service(context, "unit_detail").set_display(state, params.get_index, params.display)
    return CommandResult(updated, {"unit": summary(unit),
                                   "display": {"before": unit[DISPLAY_FIELD], "after": params.display}})


class BlessingSetting(CommandParams):
    ItemID: str = Field(min_length=1)
    Enable: int = Field(description=BOOLEAN_HINT)


class BlessingParams(CommandParams):
    FI: int | None = Field(default=None, title=FILTER_FIELD)
    settings: list[BlessingSetting] = Field(default_factory=list, max_length=MAX_POSSESSION_ITEMS)

    @model_validator(mode="after")
    def _changes_something(self) -> BlessingParams:
        require_distinct((setting.ItemID for setting in self.settings), "Blessing items")
        if self.FI is None and not self.settings:
            raise ValueError("A blessing change sets FI or at least one blessing")
        return self


def enabled_flags(possessions: Any, state: dict, items: Iterable[str]) -> dict[str, int]:
    selected = possessions.response(state, list(items), include_enabled=True)
    columns = {column.name: column.values for column in selected.children}
    return dict(zip(columns.get("ItemID", ()), columns.get(ENABLED, ())))


def set_blessings(state: dict, params: BlessingParams, context: CommandContext) -> CommandResult:
    chosen = {setting.ItemID: setting.Enable for setting in params.settings}
    updated = required_service(context, "blessings").set_settings(state, params.FI, chosen)
    possessions = required_service(context, POSSESSIONS)
    before, after = enabled_flags(possessions, state, chosen), enabled_flags(possessions, updated, chosen)
    return CommandResult(updated, {
        "filter": {"before": configuration_row(state)[FILTER_FIELD], "after": configuration_row(updated)[FILTER_FIELD]},
        "settings": [{"ItemID": item, "before": before[item], "after": after[item]} for item in chosen]})


# Fields follow the native key order, so the saved text matches what the client writes.
EventViewParams = create_model("EventViewParams", __base__=CommandParams,
                               **{key: (int, Field(description=REQUEST_HINTS.get(key))) for key in REQUEST_KEYS})


class ClearParams(CommandParams):
    pass


def request_text(params: CommandParams) -> str:
    return ",".join(f"{key}={value}" for key, value in params.model_dump().items())


def pending_request(state: dict) -> str:
    return configuration_row(state)[EVENT_VIEW_FIELD]


def set_event_view(state: dict, params: CommandParams, context: CommandContext) -> CommandResult:
    value = request_text(params)
    updated = required_service(context, "view_events").set_request(state, value)
    return CommandResult(updated, {"before": pending_request(state), "after": value})


def clear_event_view(state: dict, params: ClearParams, context: CommandContext) -> CommandResult:
    updated = required_service(context, "view_events").clear_request(state)
    before = pending_request(state)
    return CommandResult(updated, {"before": before, "after": NO_REQUEST},
                         (NOTHING_PENDING,) if before == NO_REQUEST else ())


SET_DISPLAY = AdminCommand("units.set_display", UNITS, DisplayParams, set_display, frozenset({UNITS}),
                           unavailable=requires("unit_detail"))
SET_BLESSINGS = AdminCommand("blessings.set", SETTINGS, BlessingParams, set_blessings, frozenset({CONFIG, POSSESSIONS}),
                             writes=FILTER_WRITES, unavailable=requires("blessings"))
SET_EVENT_VIEW = AdminCommand("view_event.set", SETTINGS, EventViewParams, set_event_view, frozenset({CONFIG}),
                              writes=EVENT_VIEW_WRITES, unavailable=requires("view_events"), group=VIEW_EVENT_GROUP)
CLEAR_EVENT_VIEW = AdminCommand("view_event.clear", SETTINGS, ClearParams, clear_event_view, frozenset({CONFIG}),
                                writes=EVENT_VIEW_WRITES, unavailable=requires("view_events"), group=VIEW_EVENT_GROUP)
COMMANDS = (SET_DISPLAY, SET_BLESSINGS, SET_EVENT_VIEW, CLEAR_EVENT_VIEW)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
