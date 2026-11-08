from __future__ import annotations

from copy import deepcopy

from .alxml import Column, Container, ValueType, dumps
from .errors import ProtocolError
from .native_numbers import int32


CONFIGURATION_FIELDS = {
    "UseBgm": ("A2", bool),
    "UseSe": ("A3", bool),
    "MenuTutorialFlags": ("A4", int),
    "_GuideUnitIndex": ("C1", int),
    "HaveUnitMaxSizeBuy": ("A5", int),
    "_VolumeBgm": ("C2", int),
    "_VolumeSe": ("C3", int),
    "BattlePlaceMagnetFlag": ("F9", int),
    "BattleAcceleratorFlag": ("FA", int),
    "BattleDClickSkillDisableFlag": ("FD", int),
    "DamageDispEnable": ("F7", int),
    "EventViewRequest": ("F8", str),
    "_FlagSort": ("F1", str),
    "_FlagMissionFilter": ("F4", str),
    "TypeOfSortOrder": ("F5", str),
    "_BattleGodBlessFilter": ("FE", int),
    "AutoSelectConfig": ("F6", int),
    "IntegrationAutoSelectConfig": ("FB", int),
}
CONFIGURATION_ALIASES = {alias: name for name, (alias, _) in CONFIGURATION_FIELDS.items()}
CONFIGURATION_REQUIRED_FIELDS = frozenset((
    "UseBgm", "UseSe", "MenuTutorialFlags", "_VolumeBgm", "_VolumeSe",
    "BattlePlaceMagnetFlag", "BattleAcceleratorFlag", "BattleDClickSkillDisableFlag",
))
CONFIGURATION_RESPONSE_FIELDS = (
    "MenuTutorialFlags", "_VolumeBgm", "_VolumeSe", "BattlePlaceMagnetFlag",
    "BattleAcceleratorFlag", "BattleDClickSkillDisableFlag",
)
SOUND_OPTION_FIELDS = frozenset(("A2", "A3", "C2", "C3", "F9", "FA", "FD"))
CONFIGURATION_STRING_SIZES = {"_FlagSort": 128, "_FlagMissionFilter": 32, "TypeOfSortOrder": 128,
                              "EventViewRequest": 256}
CONFIGURATION_DELTA_FIELDS = frozenset(CONFIGURATION_RESPONSE_FIELDS) | {
    "_FlagSort", "_FlagMissionFilter", "_GuideUnitIndex", "HaveUnitMaxSizeBuy", "_BattleGodBlessFilter",
    "AutoSelectConfig", "IntegrationAutoSelectConfig",
}


def _invalid(message: str) -> ProtocolError:
    return ProtocolError("invalid_configuration_state", message, 503)


def _validate_value(name: str, value: object) -> None:
    field = CONFIGURATION_FIELDS.get(name)
    if field is None:
        raise _invalid(f"Unsupported configuration field: {name}")
    kind = field[1]
    if type(value) is not kind or (kind is int and not int32(value)):
        raise _invalid(f"Invalid configuration value: {name}")
    if kind is str:
        try:
            if len(value.encode("utf-8")) >= CONFIGURATION_STRING_SIZES[name]:
                raise ValueError("String exceeds the native fixed field")
            dumps(Container(children=(Column(name, "S", (value,)),)))
        except ValueError as error:
            raise _invalid(f"Invalid configuration string: {name}") from error


def configuration_row(state: dict) -> dict:
    if not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1:
        raise _invalid("Persisted configuration schema is missing or unsupported")
    tables = state.get("tables")
    rows = tables.get("config") if isinstance(tables, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise _invalid("Expected one persisted configuration row")
    row = rows[0]
    for name in CONFIGURATION_REQUIRED_FIELDS | (CONFIGURATION_FIELDS.keys() & row.keys()):
        _validate_value(name, row.get(name))
    return row


def patch_configuration(state: dict, updates: dict) -> dict:
    configuration_row(state)
    if not isinstance(updates, dict):
        raise _invalid("Configuration updates must be a field mapping")
    for name, value in updates.items():
        _validate_value(name, value)
    updated = deepcopy(state)
    updated["tables"]["config"][0].update(updates)
    return updated


def configuration_delta(row: dict, names: tuple[str, ...]) -> Container:
    if not isinstance(row, dict) or not names or len(names) != len(set(names)):
        raise _invalid("Configuration response requires distinct fields")
    for name in names:
        if name not in CONFIGURATION_DELTA_FIELDS:
            raise _invalid(f"Configuration receiver does not support: {name}")
        _validate_value(name, row.get(name))
    return Container("PGC", (
        Column("A1", ValueType.STRING, tuple(CONFIGURATION_FIELDS[name][0] for name in names)),
        Column("A2", ValueType.STRING, tuple(str(row[name]) for name in names)),
    ))
