from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from .alxml import Column, Container, dumps
from .business_calendar import NATIVE_DATETIME
from .errors import ProtocolError
from .native_numbers import INT32_MAX
from .protocol import NATIVE_CELL_BYTES


MAX_BACKGROUNDS = 4096
MAX_CONFIG_BYTES = NATIVE_CELL_BYTES
MAX_CONFIG_ID_BYTES = MAX_CONFIG_BYTES
# Rows saved under the earlier 256-byte bound keep loading; only new selections must fit the native cell.
STORED_CONFIG_ID_BYTES = 256
_FIELDS = (("CardID", "I"), ("ConfigID", "S"), ("SaveTime", "S"))


def _card_id(value):
    if type(value) is not int or not 0 <= value <= INT32_MAX:
        raise ValueError("Background card identifiers must be nonnegative int32 values")


def _config_id(value, limit):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit:
        raise ValueError("Background configuration names must be bounded nonempty strings")
    dumps(Container(children=(Column("ConfigID", "S", (value,)),)))


def _save_time(value):
    try:
        parsed = datetime.strptime(value, NATIVE_DATETIME)
        if parsed.strftime(NATIVE_DATETIME) != value:
            raise ValueError("Noncanonical save time")
    except (TypeError, ValueError) as error:
        raise ValueError("Background save times must use the native JST wall-clock format") from error


def read_backgrounds(state: dict) -> list[dict]:
    return _backgrounds(state, STORED_CONFIG_ID_BYTES)


def _backgrounds(state: dict, config_id_bytes: int) -> list[dict]:
    try:
        if not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1:
            raise ValueError("Unsupported background storage")
        tables = state.get("tables")
        rows = tables.get("backgrounds") if isinstance(tables, dict) else None
        if not isinstance(rows, list) or len(rows) > MAX_BACKGROUNDS:
            raise ValueError("Missing or oversized background collection")
        if "backgrounds_schema" in state:
            version = state["backgrounds_schema"]
            if type(version) is not int or version != 1:
                raise ValueError("Unsupported background schema")
        elif rows:
            raise ValueError("Nonempty backgrounds require a known schema")
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {name for name, _ in _FIELDS}:
                raise ValueError("Background rows require CardID, ConfigID and SaveTime")
            _card_id(row["CardID"])
            if row["CardID"] in seen:
                raise ValueError("Background card identifiers must be distinct")
            seen.add(row["CardID"])
            _config_id(row["ConfigID"], config_id_bytes)
            _save_time(row["SaveTime"])
        return deepcopy(rows)
    except (ValueError, TypeError) as error:
        raise ProtocolError("background_state_invalid", "Stored backgrounds are invalid or unsupported", 503) from error


def background_response(state: dict, card_ids=None) -> Container:
    rows = read_backgrounds(state)
    if card_ids is not None:
        try:
            if not isinstance(card_ids, (list, tuple)) or len(card_ids) > MAX_BACKGROUNDS:
                raise ValueError("Background selection must be a bounded list")
            for identifier in card_ids:
                _card_id(identifier)
            if len(card_ids) != len(set(card_ids)):
                raise ValueError("Background selection identifiers must be distinct")
        except (ValueError, TypeError) as error:
            raise ProtocolError("background_selection_invalid", "Background selection is invalid") from error
        indexed = {row["CardID"]: row for row in rows}
        if set(card_ids) - indexed.keys():
            raise ProtocolError("background_not_found", "Selected background has no stored value", 409)
        rows = [indexed[identifier] for identifier in card_ids]
    return Container("PRM", tuple(Column(name, kind, tuple(row[name] for row in rows)) for name, kind in _FIELDS))


def merge_backgrounds(state: dict, updates: list[dict]) -> dict:
    existing = read_backgrounds(state)
    try:
        updates = _backgrounds({"bootstrap_schema": 1, "backgrounds_schema": 1,
                                "tables": {"backgrounds": updates}}, MAX_CONFIG_ID_BYTES)
    except ProtocolError as error:
        raise ProtocolError("background_updates_invalid", "Background updates must contain valid distinct complete rows") from error
    merged = {row["CardID"]: row for row in existing}
    for row in updates:
        merged[row["CardID"]] = row
    if len(merged) > MAX_BACKGROUNDS:
        raise ProtocolError("background_capacity", "Background collection exceeds its supported capacity", 409)
    updated = deepcopy(state)
    if updates:
        updated["backgrounds_schema"] = 1
        updated["tables"]["backgrounds"] = list(merged.values())
    return updated
