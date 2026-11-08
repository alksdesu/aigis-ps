from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import hashlib
from importlib.resources import files
import json
import re
from types import MappingProxyType
from typing import NamedTuple

from .al_format import ALFormatError, ParseLimits, parse_table
from .asset_service import ASSET_NOT_CACHED, ASSET_NOT_FOUND
from .system_literals import command_id, decode_command


TABLE_LIMIT = 8 * 1024 * 1024
LIMITS = ParseLimits(max_input_bytes=TABLE_LIMIT, max_decompressed_bytes=TABLE_LIMIT)
TABLE_SUFFIX = ".atb"
PATH_SEPARATORS = ("/", "\\")
REQUIRED_COLUMNS = MappingProxyType({"_ID": "int32", "_Cmnd": "string_ref"})
# The native loader skips command tables absent from the resource index.
ABSENT_TABLE = ASSET_NOT_FOUND
TABLE_UNREADABLE = "command_table_unreadable"
TABLE_INVALID = "command_table_invalid"
COLUMNS_INVALID = "command_columns_invalid"
COMMAND_AMBIGUOUS = "native_command_ambiguous"
SCOPE_SEPARATOR = "::"
MENU_ROUTE = "CmnMenu::DoReturn_Type_{}"
# 7213A0/745110 menus look up their vt+0x34 scope first, then fall back through FCMenuQuestTypeList and CmnMenu.
MENU_SCOPE_CHAINS = MappingProxyType({
    "Func_GeneralQuestMenu": ("CMenuQuestGeneral", "FCMenuQuestTypeList", "CmnMenu"),
    "Func_HistoryQuestMenu": ("CHistoryQuestMenu", "FCMenuQuestTypeList", "CmnMenu"),
})
STATUS_LIMIT = 32
CONTENT_SCHEMA = 1
RULES = MappingProxyType({
    "key": "signed_crc32_of_name_over_all_tables", "missing": "skipped_like_absent_native_files",
    "columns": "named_ID_int32_and_Cmnd_string_ref", "duplicates": "ambiguous_suspends_dependents",
    "scope": "first_hit_along_chain", "forms": "ascii_whitespace_between_tokens",
})
_GAP = r"\s*"


def _statement(*tokens: str) -> re.Pattern[str]:
    # Whitespace only separates script tokens; inside a token or string literal it changes the command.
    return re.compile(_GAP + _GAP.join(tokens) + _GAP, re.ASCII)


class CommandForm(StrEnum):
    ABSENT = "absent"
    LITERAL_TRUE = "literal_true"
    LITERAL_FALSE = "literal_false"
    OPEN_MISSION_TAG = "open_mission_tag"
    UNSUPPORTED = "unsupported"


_FORMS = (
    (CommandForm.LITERAL_TRUE, _statement("1", ";")),
    (CommandForm.LITERAL_FALSE, _statement("0", ";")),
    (CommandForm.OPEN_MISSION_TAG, _statement("IsOpenMission", r"\(", '"Tag"', ",", r"\$Tag", r"\)", ";")),
)
_MENU_UPDATER = _statement("SetUpdaterFunc", r"\(", r'"(Func_\w+)"', r"\)", ";")


def command_form(text: str | None) -> CommandForm:
    if isinstance(text, str):
        for form, pattern in _FORMS:
            if pattern.fullmatch(text):
                return form
    return CommandForm.UNSUPPORTED


@dataclass(frozen=True)
class CommandHit:
    name: str
    table: str
    row: int
    identifier: int
    text: str | None

    @property
    def form(self) -> CommandForm:
        return command_form(self.text)


def form_of(hit: CommandHit | None) -> CommandForm:
    return CommandForm.ABSENT if hit is None else hit.form


class CommandAmbiguous(ValueError):
    code = COMMAND_AMBIGUOUS

    def __init__(self, name: str, identifier: int, hits: tuple[CommandHit, ...]):
        super().__init__(f"Native command {name} matches {len(hits)} rows")
        self.name = name
        self.identifier = identifier
        self.hits = hits

    @property
    def positions(self) -> tuple[tuple[str, int], ...]:
        return tuple((hit.table, hit.row) for hit in self.hits)

    def diagnostic(self) -> dict:
        return {"code": self.code, "message": str(self), "name": self.name, "id": self.identifier,
                "occurrences": len(self.hits),
                "positions": [list(position) for position in self.positions[:STATUS_LIMIT]]}


@dataclass(frozen=True)
class MenuRoute:
    category: int
    hit: CommandHit | None
    func: str | None
    chain: tuple[str, ...]


class _Entry(NamedTuple):
    table: str
    row: int
    encoded: str | None


def _decoded(encoded: str | None) -> str | None:
    try:
        return decode_command(encoded)
    except ValueError:
        return None


def _failure_code(error: Exception, default: str) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code else default


def _digested_code(code: str | None) -> str | None:
    # Uncached tables digest under their earlier absent code, so committed content versions stay the same.
    return ABSENT_TABLE if code == ASSET_NOT_CACHED else code


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _names(values: object) -> tuple[str, ...] | None:
    if isinstance(values, str) or not isinstance(values, Iterable):
        return None
    names = tuple(values)
    return names if all(isinstance(name, str) and name for name in names) else None


def _native_table(name: str) -> bool:
    return (len(name) > len(TABLE_SUFFIX) and name.endswith(TABLE_SUFFIX)
            and not any(mark in name for mark in PATH_SEPARATORS))


def _table_names(tables: Iterable[str]) -> tuple[str, ...]:
    names = _names(tables)
    if not names or not all(map(_native_table, names)) or len(set(names)) != len(names):
        raise ValueError("Command tables must be unique native table names")
    return names


def _registered_tables() -> tuple[str, ...]:
    document = json.loads(files("aigis_ps").joinpath("data/command-tables.json").read_text("utf-8"))
    return _table_names(document["tables"])


REGISTERED_TABLES = _registered_tables()


class NativeCommandIndex:
    def __init__(self, read: Callable[[str], bytes], tables: Iterable[str] = REGISTERED_TABLES):
        if not callable(read):
            raise ValueError("A command table reader is required")
        self.tables = _table_names(tables)
        positions: dict[int, list[_Entry]] = {}
        missing, invalid, digests, loaded = {}, {}, {}, []
        for name in self.tables:
            digests[name] = None
            try:
                data = read(name)
            except (KeyError, OSError, ValueError) as error:
                missing[name] = _failure_code(error, TABLE_UNREADABLE)
                continue
            if not isinstance(data, bytes):
                missing[name] = TABLE_UNREADABLE
                continue
            digests[name] = hashlib.sha256(data).hexdigest()
            try:
                rows = self._command_rows(data)
            except ValueError as error:
                invalid[name] = _failure_code(error, TABLE_INVALID)
                continue
            for number, row in enumerate(rows):
                positions.setdefault(row["_ID"], []).append(_Entry(name, number, row["_Cmnd"]))
            loaded.append(name)
        self._positions = {identifier: tuple(entries) for identifier, entries in positions.items()}
        self._duplicates = MappingProxyType({
            identifier: tuple((entry.table, entry.row) for entry in entries)
            for identifier, entries in sorted(self._positions.items()) if len(entries) > 1})
        self.loaded = tuple(loaded)
        self.row_count = sum(map(len, self._positions.values()))
        self.missing = MappingProxyType(missing)
        self.invalid = MappingProxyType(invalid)
        self.source_sha256 = MappingProxyType(digests)
        self.content_version = _digest({"schema": CONTENT_SCHEMA, "rules": dict(RULES), "tables": [
            {"name": name, "sha256": digests[name], "missing": _digested_code(missing.get(name)),
             "invalid": invalid.get(name)}
            for name in self.tables]})

    @staticmethod
    def _command_rows(data: bytes) -> list[dict]:
        parsed = parse_table(data, LIMITS)
        columns = {field["name"]: field["type_name"] for field in parsed["fields"]}
        if any(columns.get(name) != kind for name, kind in REQUIRED_COLUMNS.items()):
            raise ALFormatError("Command tables require _ID int32 and _Cmnd string_ref columns", code=COLUMNS_INVALID)
        return parsed["rows"]

    def lookup(self, name: str) -> CommandHit | None:
        identifier = command_id(name)
        hits = tuple(CommandHit(name, entry.table, entry.row, identifier, _decoded(entry.encoded))
                     for entry in self._positions.get(identifier, ()))
        if len(hits) > 1:
            raise CommandAmbiguous(name, identifier, hits)
        return hits[0] if hits else None

    def resolve(self, scopes: Iterable[str], name: str) -> CommandHit | None:
        chain = _names(scopes)
        if chain is None or not isinstance(name, str):
            raise ValueError("Scoped lookups require a sequence of scope names and a command name")
        for scope in chain:
            hit = self.lookup(f"{scope}{SCOPE_SEPARATOR}{name}")
            if hit is not None:
                return hit
        return None

    def quest_menu(self, category: int) -> MenuRoute:
        if type(category) is not int:
            raise ValueError("Mission categories must be native integers")
        hit = self.lookup(MENU_ROUTE.format(category))
        match = None if hit is None or hit.text is None else _MENU_UPDATER.fullmatch(hit.text)
        func = None if match is None else match.group(1)
        return MenuRoute(category, hit, func, MENU_SCOPE_CHAINS.get(func, ()))

    def duplicates(self) -> Mapping[int, tuple[tuple[str, int], ...]]:
        return self._duplicates

    def status(self) -> dict:
        listed = tuple(self._duplicates.items())[:STATUS_LIMIT]
        return {"tables": len(self.loaded), "rows": self.row_count, "missing": dict(self.missing),
                "invalid": dict(self.invalid), "duplicate_count": len(self._duplicates),
                "duplicates": [{"id": identifier, "occurrences": len(places),
                                "positions": [list(place) for place in places[:STATUS_LIMIT]]}
                               for identifier, places in listed],
                "content_version": self.content_version}
