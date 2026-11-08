from __future__ import annotations

from copy import deepcopy

from .alxml import Column, Container, dumps
from .errors import ProtocolError
from .native_numbers import int32
from .protocol import NATIVE_CELL_BYTES
from .system_literals import command_id


MAX_FLAGS = 65535
MAX_FLAG_NAME_BYTES = NATIVE_CELL_BYTES
# Flags saved under the earlier 512-byte bound keep loading and updating; only new names must fit the native cell.
STORED_FLAG_NAME_BYTES = 512


def _name(value, limit):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit:
        raise ValueError("Flag names must be bounded nonempty strings")
    dumps(Container(children=(Column("ID", "S", (value,)),)))


def _value(value):
    if not int32(value):
        raise ValueError("Flag values must be signed native integers")


def flag_hash(name: str) -> int:
    _name(name, STORED_FLAG_NAME_BYTES)
    return command_id(name)


def read_flags(state: dict) -> list[dict]:
    try:
        if not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1:
            raise ValueError("Unsupported flag storage")
        tables = state.get("tables")
        rows = tables.get("universal_flags") if isinstance(tables, dict) else None
        if not isinstance(rows, list) or len(rows) > MAX_FLAGS:
            raise ValueError("Missing or oversized flag collection")
        if "universal_flags_schema" in state:
            version = state["universal_flags_schema"]
            if type(version) is not int or version != 1:
                raise ValueError("Unsupported flag schema")
        elif rows:
            raise ValueError("Nonempty flags require a known schema")
        hashes = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"ID", "Value"}:
                raise ValueError("Flags require ID and Value")
            key = flag_hash(row["ID"])
            _value(row["Value"])
            if key in hashes:
                raise ValueError("Flag names are duplicated or collide")
            hashes.add(key)
        return deepcopy(rows)
    except (ValueError, TypeError) as error:
        raise ProtocolError("universal_flags_invalid", "Stored universal flags are invalid or unsupported", 503) from error


def flag_value(state: dict, name: str) -> int:
    try:
        key = flag_hash(name)
    except (ValueError, TypeError) as error:
        raise ProtocolError("universal_flag_name_invalid", "Flag lookup requires a valid name") from error
    for row in read_flags(state):
        if flag_hash(row["ID"]) == key:
            if row["ID"] != name:
                raise ProtocolError("universal_flag_name_conflict", "Flag name collides with an existing name", 409)
            return row["Value"]
    return 0


def resolve_flag_name(state: dict, key: int, default_name: str) -> str:
    try:
        _value(key)
        if flag_hash(default_name) != key:
            raise ValueError("Flag name does not match the native key")
    except (ValueError, TypeError) as error:
        raise ProtocolError("universal_flag_key_invalid", "Flag key and default name must agree") from error
    for row in read_flags(state):
        if flag_hash(row["ID"]) == key:
            return row["ID"]
    return default_name


def flags_response(state: dict, names=None) -> Container:
    rows = read_flags(state)
    if names is not None:
        try:
            if not isinstance(names, (tuple, list)) or len(names) > MAX_FLAGS:
                raise ValueError("Flag selection must be a bounded list")
            hashes = [flag_hash(name) for name in names]
            if len(set(hashes)) != len(hashes):
                raise ValueError("Flag selection names must be distinct")
        except (ValueError, TypeError) as error:
            raise ProtocolError("universal_flag_selection_invalid", "Flag selection is invalid") from error
        indexed = {row["ID"]: row for row in rows}
        if set(names) - indexed.keys():
            raise ProtocolError("universal_flag_not_found", "Selected flag has no stored value", 409)
        rows = [indexed[name] for name in names]
    if not rows:
        return Container("GF")
    return Container("GF", (Column("ID", "S", tuple(row["ID"] for row in rows)),
                            Column("Value", "I", tuple(row["Value"] for row in rows))))


def set_flags(state: dict, updates: dict[str, int]) -> dict:
    rows = read_flags(state)
    stored = {row["ID"] for row in rows}
    try:
        if not isinstance(updates, dict) or len(updates) > MAX_FLAGS:
            raise ValueError("Flag updates must be a bounded mapping")
        for name, value in updates.items():
            _name(name, STORED_FLAG_NAME_BYTES if name in stored else MAX_FLAG_NAME_BYTES)
            _value(value)
    except (ValueError, TypeError) as error:
        raise ProtocolError("universal_flag_updates_invalid", "Flag updates require valid names and integer values") from error
    indexed = {flag_hash(row["ID"]): row for row in rows}
    for name, value in updates.items():
        key = flag_hash(name)
        old = indexed.get(key)
        if old is not None and old["ID"] != name:
            raise ProtocolError("universal_flag_name_conflict", "Flag name collides with an existing name", 409)
        indexed[key] = {"ID": name, "Value": value}
    if len(indexed) > MAX_FLAGS:
        raise ProtocolError("universal_flag_capacity", "Flag collection exceeds its supported capacity", 409)
    updated = deepcopy(state)
    if updates:
        updated["universal_flags_schema"] = 1
        updated["tables"]["universal_flags"] = list(indexed.values())
    return updated
