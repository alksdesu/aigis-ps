from __future__ import annotations

from pydantic import Field

from .admin_cmd_settings import CONFIG, SETTINGS
from .admin_cmd_units import summary
from .admin_command import AdminCommand, CommandContext, CommandParams, CommandResult, required_service, requires
from .admin_registry import CommandRegistry
from .config_state import configuration_row
from .guide_service import GUIDE_FIELD, NO_GUIDE
from .native_numbers import INT32_MAX


GUIDE = "guide"
GUIDE_WRITES = frozenset({f"{CONFIG}.{GUIDE_FIELD}"})
CLEAR_HINT = f"{NO_GUIDE} clears the secretary"
NOTHING_SELECTED = "No secretary was selected, so nothing was cleared"


class GuideParams(CommandParams):
    get_index: int = Field(ge=0, le=INT32_MAX, title=GUIDE_FIELD, description=CLEAR_HINT)


def selection(units: list[dict], index: int) -> dict:
    # The prince holds GetIndex 0, yet a saved 0 means no secretary.
    unit = None if index == NO_GUIDE else next((unit for unit in units if unit["GetIndex"] == index), None)
    return {"GetIndex": index, "unit": None if unit is None else summary(unit)}


def set_guide(state: dict, params: GuideParams, context: CommandContext) -> CommandResult:
    updated = required_service(context, GUIDE).set_selection(state, params.get_index)
    units = required_service(context, "player_state").validate(state)["units"]
    before = configuration_row(state)[GUIDE_FIELD]
    warnings = (NOTHING_SELECTED,) if params.get_index == before == NO_GUIDE else ()
    return CommandResult(updated, {"before": selection(units, before), "after": selection(units, params.get_index)},
                         warnings)


SET_GUIDE = AdminCommand("guide.set", SETTINGS, GuideParams, set_guide, frozenset({CONFIG}), writes=GUIDE_WRITES,
                         unavailable=requires(GUIDE))
COMMANDS = (SET_GUIDE,)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
