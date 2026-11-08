from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import logging
import re
from typing import TYPE_CHECKING

from .alxml import Column, Container, dumps
from .errors import ProtocolError
from .native_numbers import int32
from .protocol import NATIVE_CELL_BYTES, ProtocolRegistry, RequestEnvelope, encode_response
from .request_fields import fields, invalid, registered, values
from .storage import Store
from .system_literals import command_id

if TYPE_CHECKING:
    from .bootstrap import BootstrapService


MAX_FILTER_GROUPS = 1024
MAX_SELECTIONS = 4096
MAX_SAVE_BYTES = 65536
MAX_NAME_BYTES = 4096
_INTEGER = re.compile(r"-?[0-9]{1,10}\Z")
log = logging.getLogger("aigis_ps")


def name_hash(name: str) -> int:
    return command_id(name)


def _replayable(row: dict) -> dict:
    name = row["FilterName"] if len(row["FilterName"].encode("utf-8")) <= NATIVE_CELL_BYTES else ""
    selection = row["SaveValue"]
    if len(selection) > NATIVE_CELL_BYTES:
        selection = selection[:max(selection.rfind(",", 0, NATIVE_CELL_BYTES + 1), 0)]
    return {**row, "FilterName": name, "SaveValue": selection}


def _validate_selection(value: str):
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_SAVE_BYTES:
        raise ValueError("Filter selection exceeds the local string limit")
    if value == "":
        return
    parts = value.split(",")
    if len(parts) > MAX_SELECTIONS:
        raise ValueError("Filter selection exceeds the local item limit")
    identifiers = []
    for part in parts:
        if not _INTEGER.fullmatch(part):
            raise ValueError("Filter selections require comma-separated int32 identifiers")
        number = int(part)
        if not int32(number):
            raise ValueError("Filter selection is outside int32")
        identifiers.append(number)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Filter selection contains duplicate identifiers")


def _validate_name(name: str):
    if not isinstance(name, str) or len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise ValueError("Filter name exceeds the local string limit")
    dumps(Container(children=(Column("FilterName", "S", (name,)),)))


def parse_filters(envelope: RequestEnvelope) -> list[dict]:
    request = registered(envelope, {26}, {"Filter"}, {"Filter"})
    columns = fields(request["Filter"], {"FilterName", "FilterNameHash", "SaveValue"})
    if not columns:
        return []
    if not {"FilterNameHash", "SaveValue"} <= columns.keys():
        invalid("Filter rows require hashes and selections")
    hashes = values(columns["FilterNameHash"], "I", maximum=MAX_FILTER_GROUPS)
    selections = values(columns["SaveValue"], "S", maximum=MAX_FILTER_GROUPS, max_bytes=MAX_SAVE_BYTES)
    if len(hashes) != len(selections) or len(hashes) != len(set(hashes)):
        invalid("Filter hashes and selections must form distinct aligned rows")
    names = values(columns["FilterName"], "S", maximum=len(hashes), max_bytes=MAX_NAME_BYTES) if "FilterName" in columns else ()
    named = {}
    try:
        for name in names:
            _validate_name(name)
            if not name:
                continue
            identifier = name_hash(name)
            if identifier not in hashes or identifier in named:
                raise ValueError("Filter name does not identify one unique submitted group")
            named[identifier] = name
        for value in selections:
            _validate_selection(value)
    except ValueError as error:
        invalid(str(error))
    return [{"FilterNameHash": identifier, "SaveValue": selection, "FilterName": named.get(identifier, "")}
            for identifier, selection in zip(hashes, selections)]


def read_filters(state: dict) -> list[dict]:
    try:
        if not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1:
            raise ValueError("Missing bootstrap state")
        tables = state.get("tables")
        rows = tables.get("filter_settings") if isinstance(tables, dict) else None
        if not isinstance(rows, list) or len(rows) > MAX_FILTER_GROUPS:
            raise ValueError("Missing or oversized filter collection")
        version = state.get("filter_settings_schema")
        if version is not None and (type(version) is not int or version != 1):
            raise ValueError("Unsupported filter schema")
        if rows and version is None:
            raise ValueError("Nonempty filter collection requires a known schema")
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"FilterName", "FilterNameHash", "SaveValue"}:
                raise ValueError("Invalid filter row structure")
            identifier = row["FilterNameHash"]
            if not int32(identifier) or identifier in seen:
                raise ValueError("Invalid or duplicate filter hash")
            seen.add(identifier)
            _validate_name(row["FilterName"])
            _validate_selection(row["SaveValue"])
            if row["FilterName"] and name_hash(row["FilterName"]) != identifier:
                raise ValueError("Stored filter name and hash disagree")
        return deepcopy(rows)
    except (ValueError, TypeError) as error:
        raise ProtocolError("filter_state_invalid", "Persisted filter settings are missing, invalid or unsupported", 503) from error


def filter_response(state: dict) -> Container:
    rows = [_replayable(row) for row in read_filters(state)]
    return Container("PlayerFilterParam", tuple(Column(name, kind, tuple(row[name] for row in rows))
        for name, kind in (("FilterName", "S"), ("FilterNameHash", "I"), ("SaveValue", "S"))))


def merge_filters(state: dict, updates: list[dict]) -> dict:
    existing = read_filters(state)
    try:
        updates = read_filters({"bootstrap_schema": 1, "filter_settings_schema": 1,
                                "tables": {"filter_settings": updates}})
    except ProtocolError as error:
        raise ProtocolError("filter_updates_invalid", "Filter updates must contain valid distinct group rows") from error
    merged = {row["FilterNameHash"]: row for row in existing}
    for row in updates:
        identifier = row["FilterNameHash"]
        old = merged.get(identifier)
        name = row["FilterName"]
        if old and old["FilterName"]:
            if name and name != old["FilterName"]:
                raise ProtocolError("filter_name_conflict", "Filter names collide at the same native hash", 409)
            name = old["FilterName"]
        merged[identifier] = {**row, "FilterName": name}
    if len(merged) > MAX_FILTER_GROUPS:
        raise ProtocolError("filter_capacity", "Filter settings exceed the local group limit", 409)
    updated = deepcopy(state)
    if not updates:
        return updated
    updated["filter_settings_schema"] = 1
    updated["tables"]["filter_settings"] = list(merged.values())
    read_filters(updated)
    return updated


class FilterService:
    def __init__(self, bootstrap: BootstrapService):
        self.bootstrap = bootstrap
        self._startup_profile = ProtocolRegistry.load().by_registration[137]

    def handlers(self):
        return {26: self.save}

    def status(self):
        return {"implemented_operations": [26], "schema_version": 1,
                "save_policy": "Replace submitted groups; keep absent groups; empty selection clears only its group",
                "reload_policy": f"Full PlayerFilterParam snapshot through operation 137; selections keep whole identifiers "
                                 f"within {NATIVE_CELL_BYTES} bytes and longer names reload empty",
                "local_limits": {"groups": MAX_FILTER_GROUPS, "selections_per_group": MAX_SELECTIONS,
                                 "name_bytes": MAX_NAME_BYTES, "selection_bytes": MAX_SAVE_BYTES,
                                 "reload_bytes": NATIVE_CELL_BYTES}}

    def save(self, envelope: RequestEnvelope, store: Store) -> bytes:
        rows = parse_filters(envelope)
        trimmed = [row["FilterNameHash"] for row in rows if _replayable(row) != row]
        if trimmed:
            log.warning("Filter groups %s exceed the %d-byte native cell; startup reloads them trimmed",
                        trimmed, NATIVE_CELL_BYTES)
        for _ in range(3):
            profile = self.bootstrap._authenticate(envelope, store)
            updated = merge_filters(profile.state, rows)
            startup_response = self.bootstrap._configuration_response(replace(profile, state=updated))
            encode_response(self._startup_profile, startup_response)
            response = dumps(Container(children=(self.bootstrap._system(profile), Container("PGC"))))
            encode_response(envelope.profile, response)
            if updated == profile.state:
                return response
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Filter settings changed while saving", 409)
