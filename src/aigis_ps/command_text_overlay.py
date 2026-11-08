from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import functools
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import NamedTuple

from .al_format import parse_table
from .entrust_catalog import GATE_SCAN_TYPES, MENU_GATE, TYPE_GATE, UNGATED_TYPES
from .local_io import deserialize, read_bounded
from .native_commands import (LIMITS, REGISTERED_TABLES, REQUIRED_COLUMNS, CommandAmbiguous, CommandForm, CommandHit,
                              NativeCommandIndex, command_form, form_of)
from .native_numbers import int32
from .quest_modes import MISSION_CATEGORIES
from .system_literals import decode_command, encode_command
from .table_cell_patch import MAX_STRING_BYTES, patch_table_cells


MAX_RULE_BYTES = 64 * 1024
MAX_OVERRIDES = 256
SCHEMA_VERSION = 1
RULE_FIELDS = frozenset(("tables",))
ID_FIELD, COMMAND_FIELD = REQUIRED_COLUMNS
ROW_MARK = "#"
CLOSING_POLICY = "overrides_never_open_a_command_or_entrust_gate_further"
AMBIGUOUS = "ambiguous"
# A gate lets the client in unconditionally, per mission tag, or not at all; unsupported text stays shut on the server.
OPENNESS = MappingProxyType({CommandForm.OPEN_MISSION_TAG: 1, CommandForm.LITERAL_TRUE: 2})
TABLE_ORDER = MappingProxyType({name: position for position, name in enumerate(REGISTERED_TABLES)})
_IDENTIFIER = re.compile(r"(?:0|-?[1-9][0-9]{0,9})\Z")
_ROW = re.compile(re.escape(ROW_MARK) + r"(?:0|[1-9][0-9]{0,4})\Z")
# Native scripts separate statements only with tabs and line breaks; the script catalogs read anything else as drift.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


class RowKey(NamedTuple):
    positional: bool
    value: int


def parse_row_key(key) -> RowKey:
    if type(key) is str and _ROW.fullmatch(key):
        return RowKey(True, int(key[len(ROW_MARK):]))
    if type(key) is str and _IDENTIFIER.fullmatch(key) and int32(int(key)):
        return RowKey(False, int(key))
    raise ValueError("Command rows are keyed by a canonical _ID or by #row")


def _text(value) -> str:
    if type(value) is not str or _CONTROL.search(value):
        raise ValueError("Command texts must be text without control characters other than tabs and line breaks")
    try:
        encoded = encode_command(value)
    except ValueError as error:
        raise ValueError("Command texts must encode as UTF-8") from error
    if len(encoded) > MAX_STRING_BYTES:
        raise ValueError(f"Encoded command texts must fit one {MAX_STRING_BYTES}-byte native string cell")
    if decode_command(encoded) != value:
        raise ValueError("Command texts must decode back to themselves")
    return value


def _row_text(table: str, key: str, value) -> str:
    try:
        return _text(value)
    except ValueError as error:
        raise ValueError(f"Command text override {table} {key}: {error}") from error


def _decoded(encoded) -> str | None:
    try:
        return decode_command(encoded)
    except ValueError:
        return None


def _openness(state: str) -> int:
    return OPENNESS.get(state, 0)


@dataclass(frozen=True)
class CommandTextOverrides:
    tables: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.tables, Mapping):
            raise ValueError("Command text overrides require a mapping of command tables")
        normalized, count = {}, 0
        for name, rows in self.tables.items():
            if type(name) is not str or name not in TABLE_ORDER:
                raise ValueError(f"Command text overrides name an unregistered command table: {name}")
            if not isinstance(rows, Mapping):
                raise ValueError(f"Command text overrides of {name} require a row mapping")
            count += len(rows)
            if count > MAX_OVERRIDES:
                raise ValueError(f"Command text overrides hold at most {MAX_OVERRIDES} rows")
            if rows:
                normalized[name] = MappingProxyType({key: _row_text(name, key, rows[key])
                                                     for key in sorted(rows, key=parse_row_key)})
        object.__setattr__(self, "tables", MappingProxyType({name: normalized[name]
                                                             for name in sorted(normalized, key=TABLE_ORDER.get)}))

    @property
    def count(self) -> int:
        return sum(map(len, self.tables.values()))

    def to_dict(self) -> dict:
        return {"tables": {name: dict(rows) for name, rows in self.tables.items()}}

    @property
    def content_version(self) -> str:
        return _digest({"schema": SCHEMA_VERSION, **self.to_dict()})


def load_command_text_overrides(path) -> CommandTextOverrides | None:
    if path is None:
        return None
    values = deserialize(read_bounded(Path(path), MAX_RULE_BYTES).decode("utf-8-sig"))
    if not isinstance(values, dict) or set(values) != RULE_FIELDS:
        raise ValueError("Command text overrides require exactly tables")
    tables = values["tables"]
    if not isinstance(tables, dict) or not all(isinstance(rows, dict) for rows in tables.values()):
        raise ValueError("Command text override tables must be JSON objects of rows")
    return CommandTextOverrides(tables)


@dataclass(frozen=True)
class CommandOverride:
    table: str
    key: str
    row: int
    identifier: int
    before: CommandForm
    after: CommandForm
    changed: bool

    def to_dict(self) -> dict:
        return {"table": self.table, "key": self.key, "row": self.row, "id": self.identifier,
                "before": self.before, "after": self.after, "changed": self.changed}


@dataclass(frozen=True)
class GateChange:
    gate: str
    before: str
    after: str

    def to_dict(self) -> dict:
        return {"gate": self.gate, "before": self.before, "after": self.after}


@dataclass(frozen=True)
class CommandTextOverlay:
    replacements: Mapping[str, bytes]
    rules: CommandTextOverrides | None = None
    rows: tuple[CommandOverride, ...] = ()
    gates: tuple[GateChange, ...] = ()
    source_sha256: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    effective_sha256: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def targets(self) -> tuple[str, ...]:
        return tuple(self.rules.tables) if self.rules is not None else ()

    @property
    def metadata(self) -> dict:
        rules = self.rules
        return {"schema_version": SCHEMA_VERSION, "rules": rules.to_dict() if rules is not None else None,
                "rules_hash": rules.content_version if rules is not None else None, "policy": CLOSING_POLICY,
                "tables": {name: {"source_sha256": self.source_sha256[name],
                                  "effective_sha256": self.effective_sha256[name]} for name in self.targets},
                "rows": [row.to_dict() for row in self.rows], "entrust_gates": [gate.to_dict() for gate in self.gates]}


def _locate(name: str, rows: list[dict], keys) -> dict[str, int]:
    positions = {}
    for index, row in enumerate(rows):
        positions.setdefault(row[ID_FIELD], []).append(index)
    located, owners = {}, {}
    for key in keys:
        positional, value = parse_row_key(key)
        matches = ([value] if value < len(rows) else []) if positional else positions.get(value, [])
        if len(matches) != 1:
            raise ValueError(f"Command text override {key} matches {len(matches)} rows of {name}"
                             + ("; address one by #row" if matches else ""))
        index = matches[0]
        if index in owners:
            raise ValueError(f"Command text overrides {owners[index]} and {key} address one row of {name}")
        owners[index], located[key] = key, index
    return located


def _confirm(name: str, source: list[dict], payload: bytes, texts: Mapping[int, str]) -> str:
    result = parse_table(payload, LIMITS)
    if len(result["rows"]) != len(source):
        raise ValueError(f"Command text overrides changed the table layout: {name}")
    for index, (before, after) in enumerate(zip(source, result["rows"])):
        expected = dict(before)
        if index in texts:
            if _decoded(after[COMMAND_FIELD]) != texts[index]:
                raise ValueError(f"Command text overrides did not decode back from {name}")
            expected[COMMAND_FIELD] = after[COMMAND_FIELD]
        if after != expected:
            raise ValueError(f"Command text overrides changed cells outside the configured rows: {name}")
    return hashlib.sha256(payload).hexdigest()


def _gate_state(resolve: Callable[[], CommandHit | None]) -> str:
    try:
        return form_of(resolve())
    except CommandAmbiguous:
        return AMBIGUOUS


def _menu_gate(index: NativeCommandIndex, category: int) -> CommandHit | None:
    return index.resolve(index.quest_menu(category).chain, MENU_GATE.format(category))


def _entrust_gates(index: NativeCommandIndex) -> dict[str, str]:
    gates = {}
    for quest_type in GATE_SCAN_TYPES:
        if quest_type not in UNGATED_TYPES:
            name = TYPE_GATE.format(quest_type)
            gates[name] = _gate_state(functools.partial(index.lookup, name))
    # A rewritten menu route can move IsEntrust onto another scope, so menus are judged through their chains.
    for category in MISSION_CATEGORIES:
        gates[MENU_GATE.format(category)] = _gate_state(functools.partial(_menu_gate, index, category))
    return gates


def build_command_text_overlay(read_resource: Callable[[str], bytes],
                               rules: CommandTextOverrides | None) -> CommandTextOverlay:
    if rules is None:
        return CommandTextOverlay(MappingProxyType({}))
    if not isinstance(rules, CommandTextOverrides) or CommandTextOverrides(**rules.to_dict()) != rules:
        raise ValueError("Command text overlay requires verified overrides")
    if not rules.tables:
        return CommandTextOverlay(MappingProxyType({}), rules)
    if not callable(read_resource):
        raise ValueError("Command text overlay requires a resource reader")

    # One snapshot of every table keeps the before and after gate checks on the same bytes.
    @functools.cache
    def base(name: str) -> bytes:
        return read_resource(name)

    before = NativeCommandIndex(base)
    overrides, opened, replacements, effective_sha256 = [], [], {}, {}
    for name, texts in rules.tables.items():
        failure = before.missing.get(name) or before.invalid.get(name)
        if failure is not None:
            raise ValueError(f"Command text overrides target an unavailable command table: {name} ({failure})")
        rows = parse_table(base(name), LIMITS)["rows"]
        changed = {}
        for key, index in _locate(name, rows, texts).items():
            original = _decoded(rows[index][COMMAND_FIELD])
            record = CommandOverride(name, key, index, rows[index][ID_FIELD], command_form(original),
                                     command_form(texts[key]), original != texts[key])
            if _openness(record.after) > _openness(record.before):
                opened.append(f"{name} {key}")
            if record.changed:
                changed[index] = texts[key]
            overrides.append(record)
        effective = base(name)
        if changed:
            effective = replacements[name] = patch_table_cells(effective, {
                index: {COMMAND_FIELD: encode_command(text)} for index, text in changed.items()})
        effective_sha256[name] = _confirm(name, rows, effective, changed)
    gates = ()
    if replacements:
        after = NativeCommandIndex(lambda name: replacements[name] if name in replacements else base(name))
        previous, current = _entrust_gates(before), _entrust_gates(after)
        gates = tuple(GateChange(gate, state, current[gate]) for gate, state in previous.items() if state != current[gate])
        opened.extend(change.gate for change in gates if _openness(change.after) > _openness(change.before))
    if opened:
        raise ValueError("Command text overrides may only close native gates; these would open further: "
                         + ", ".join(opened))
    return CommandTextOverlay(MappingProxyType(replacements), rules, tuple(overrides), gates,
                              MappingProxyType({name: before.source_sha256[name] for name in rules.tables}),
                              MappingProxyType(effective_sha256))
