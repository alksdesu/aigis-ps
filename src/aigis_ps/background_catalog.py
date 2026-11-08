from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, dumps
from .background_state import MAX_CONFIG_BYTES
from .business_calendar import NATIVE_SECONDS_TEXT
from .energy_clock import JST, instant
from .errors import ProtocolError, caught
from .guide_service import GUIDE_FIELD
from .native_clock import native_clock, parse_native_date
from .native_commands import ABSENT_TABLE, LIMITS as COMMAND_LIMITS, SCOPE_SEPARATOR, NativeCommandIndex
from .native_numbers import int32
from .player_state import MAX_PLAYER_UNITS, UNIT_BOOLEANS, UNIT_FIELDS
from .public_tables import BytesDigest
from .system_literals import command_id, decode_command


RESOURCE_NAME = "PrivateRoomConfigTable.atb"
LIMITS = ParseLimits(max_input_bytes=64 * 1024 * 1024, max_decompressed_bytes=64 * 1024 * 1024)
FIELDS = {name: 1 for name in ("ID", "TextID", "Category", "BackGround", "RoomID", "BgmID", "CardID")}
FIELDS.update(Begin=32, End=32, DefineName=32)
# 72DDE0 lists these four categories whenever the menu script leaves either filter key unset or empty.
EXECUTABLE_CATEGORIES = frozenset((-1524803586, -1777172756, -1224326894, 463396108))
FIXED_CATEGORY = 463396108
FIXED_NAME = "system_fix"
EXCHANGE_MENU = "CMenuEtcExchangeBg"
# A1D4F0 runs OnPreAssemble then OnAssemble before building the list, each resolved in the menu scope before CmnMenu.
FILTER_SCOPES = (EXCHANGE_MENU, "CmnMenu")
FILTER_COMMANDS = ("OnPreAssemble", "OnAssemble")
FILTER_TYPE, FILTER_ITEM = "FilterType", "FilterItem"
FILTER_COLUMN = "Category"
FILTER_SEPARATOR = ","
SCRIPT_SOURCE, EXECUTABLE_SOURCE = "resource_script", "executable_fallback"
UNKNOWN_NAME, PAGE_BACKGROUND = "unknown_name", "page_background"
FIXED_OUTSIDE_GLOBAL, NON_NORMALIZED_KEY, PRIVATE_ROOM_MISMATCH = (
    "fixed_outside_global", "non_normalized_key", "private_room_mismatch")
AUDIT_KINDS = (UNKNOWN_NAME, PAGE_BACKGROUND, FIXED_OUTSIDE_GLOBAL, NON_NORMALIZED_KEY, PRIVATE_ROOM_MISMATCH)
WINDOW_RULE = "inclusive_native_clock"
# Receipts committed while availability windows compared JST instants keep this rule in their digest.
LEGACY_WINDOW_RULE = "inclusive_jst"
_SET_PARAMETER = "MnObj_SetParam"
_LITERAL_CALL = _SET_PARAMETER + r'\s*\(\s*"([^"\\\x00-\x1f]*)"\s*\)\s*;'
_LITERAL_PARAMETER = re.compile(_LITERAL_CALL, re.ASCII)
# Any other statement could make a filter assignment conditional, so only plain parameter lists are derived.
_PARAMETER_LIST = re.compile(r"(?:\s*{})*\s*".format(_LITERAL_CALL), re.ASCII)
_FILTER_KEY = "(?:{})".format("|".join(map(re.escape, (FILTER_TYPE, FILTER_ITEM))))
# SetSystemStateParam also accepts the CRC32 of the full key, so the numeric spellings count as writes too.
_FILTER_WRITE = re.compile("|".join((
    re.escape(EXCHANGE_MENU + SCOPE_SEPARATOR) + _FILTER_KEY, _FILTER_KEY + r"\s*=",
    *(r"(?<![0-9]){}(?![0-9])".format(abs(command_id(f"{EXCHANGE_MENU}{SCOPE_SEPARATOR}{key}")))
      for key in (FILTER_TYPE, FILTER_ITEM)))))
# A41340 turns numeric FilterItem entries into raw category values instead of name hashes.
_NUMERIC = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


def _unpack(raw, limits):
    if type(raw) is not bytes or not 0 < len(raw) <= limits.max_input_bytes:
        raise ValueError("Background resources must be bounded immutable bytes")
    return decompress_all4(raw, limits) if raw.startswith(b"ALL4") else raw


def _filter_assignments(hit):
    if hit.text is None:
        raise ValueError("Background filter scripts must be decodable")
    calls = tuple(_LITERAL_PARAMETER.finditer(hit.text))
    if hit.text.count(_SET_PARAMETER) != len(calls) or _FILTER_WRITE.search(_LITERAL_PARAMETER.sub("", hit.text)):
        raise ValueError("Background filter scripts must assign the filter through literal parameters")
    assignments = {}
    for call in calls:
        key, separator, value = call.group(1).partition("=")
        if separator and key in (FILTER_TYPE, FILTER_ITEM):
            assignments[key] = value
        elif FILTER_TYPE in key or FILTER_ITEM in key:
            raise ValueError("Background filter parameter names are ambiguous")
    if assignments and not _PARAMETER_LIST.fullmatch(hit.text):
        raise ValueError("Background filter scripts must contain only literal parameter statements")
    return assignments


def _filter_categories(assignments, known):
    kind, items = assignments.get(FILTER_TYPE), assignments.get(FILTER_ITEM)
    names = () if items is None else tuple(item for item in items.split(FILTER_SEPARATOR) if item)
    if kind is None or not names:
        return EXECUTABLE_CATEGORIES, EXECUTABLE_SOURCE
    if kind != FILTER_COLUMN:
        raise ValueError("Background filters must select the Category column")
    if any(name != name.strip() or _NUMERIC.fullmatch(name) or command_id(name) not in known for name in names):
        raise ValueError("Background filter items must name existing categories")
    return frozenset(map(command_id, names)), SCRIPT_SOURCE


def _text(value, *, empty=False):
    if type(value) is not str or not empty and not value or len(value.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ValueError("Expected bounded resource text")
    dumps(Container(children=(Column("Value", "S", (value,)),)))
    return value


def _date(value):
    _text(value, empty=True)
    if not value:
        return None
    match = NATIVE_SECONDS_TEXT.fullmatch(value)
    if match is None:
        raise ValueError("Background dates require a complete native timestamp")
    year, _, month, day, hour, minute, second = match.groups()
    return datetime(*(int(part) for part in (year, month, day, hour, minute, second)), tzinfo=JST)


def _window(opening, closing):
    begin, end = _date(opening), _date(closing)
    if begin is not None and end is not None and begin > end:
        raise ValueError("Background availability window is inverted")
    # 94D730 compares the client clock with AACF90's reading of the same text.
    return parse_native_date(opening), parse_native_date(closing)


def _version(content, window):
    return hashlib.sha256(json.dumps({**content, "window": window}, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class BackgroundDefinition:
    name: str
    card_id: int
    category: int
    begin: datetime | None
    end: datetime | None
    is_fixed: bool


class BackgroundCatalog:
    def __init__(self, cards, asset_reader):
        self._cards = cards
        self._card_digest = BytesDigest()
        try:
            if not callable(asset_reader):
                raise ValueError("Backgrounds require a resource reader")
            self._snapshot = cards.snapshot
            self._master_hash = self._master_digest()
            self._origins = MappingProxyType(self._read_cards())
            raw = asset_reader(RESOURCE_NAME)
            data = _unpack(raw, LIMITS)
            table = parse_table(data, LIMITS)
            fields = {field["name"]: field["type_code"] for field in table["fields"]}
            if len(fields) != len(table["fields"]) or any(fields.get(name) != kind for name, kind in FIELDS.items()):
                raise ValueError("Background resource fields differ from the native contract")
            definitions, names, identifiers = [], set(), set()
            for row in table["rows"]:
                if any(not int32(row[name]) for name, kind in FIELDS.items() if kind == 1):
                    raise ValueError("Background values require signed native integers")
                name = _text(row["DefineName"])
                identifier, card_id = row["ID"], row["CardID"]
                if identifier != command_id(name) or name in names or identifier in identifiers:
                    raise ValueError("Background identities are invalid or duplicated")
                if card_id < 0 or card_id and card_id not in self._origins or row["RoomID"] < 0:
                    raise ValueError("Background references are invalid")
                fixed = name == FIXED_NAME
                if fixed and (row["Category"] != FIXED_CATEGORY or card_id != 0):
                    raise ValueError("The fixed background must preserve its native category and global identity")
                begin, end = _window(row["Begin"], row["End"])
                definitions.append(BackgroundDefinition(name, card_id, row["Category"], begin, end, fixed))
                names.add(name)
                identifiers.add(identifier)
            if not definitions:
                raise ValueError("Background definitions are empty")
            self._definitions = tuple(definitions)
            self._by_name = MappingProxyType({definition.name: definition for definition in definitions})
            self._resource_hash = hashlib.sha256(raw).hexdigest()
            self._decoded_hash = hashlib.sha256(data).hexdigest()
            self._derive_categories(asset_reader, {definition.category for definition in definitions})
            version = {"rule": "background-selection-v2", "resource": self._resource_hash,
                "master": self._master_hash, "origins": dict(self._origins),
                "categories": sorted(self._categories), "category_source": self._category_source,
                "filter_scripts": [[table, row] for _, table, row, _ in self._filter_scripts],
                "commands": self._command_version, "fixed_name": FIXED_NAME, "name_bytes": MAX_CONFIG_BYTES,
                "guide": "get_index_then_one_origin_id_hop"}
            self._content_version = _version(version, WINDOW_RULE)
            self._replayable_versions = frozenset((self._content_version, _version(version, LEGACY_WINDOW_RULE)))
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError, ProtocolError) as error:
            raise ProtocolError("background_catalog_invalid", "Background resources differ from the verified contract", 503) from error

    def _derive_categories(self, asset_reader, known):
        tables = {}

        def read(name):
            data = _unpack(asset_reader(name), COMMAND_LIMITS)
            tables[name] = data
            return data

        commands = NativeCommandIndex(read)
        if commands.invalid or any(code != ABSENT_TABLE for code in commands.missing.values()):
            raise ValueError("Background filter command tables are unreadable")
        hits = tuple(hit for hit in (commands.resolve(FILTER_SCOPES, name) for name in FILTER_COMMANDS) if hit is not None)
        scripts = {(hit.table, hit.row) for hit in hits}
        for name in commands.loaded:
            for number, row in enumerate(parse_table(tables[name], COMMAND_LIMITS)["rows"]):
                if (name, number) not in scripts and _FILTER_WRITE.search(decode_command(row["_Cmnd"])):
                    raise ValueError("Another native command writes the background filter")
        assignments = {}
        for hit in hits:
            assignments.update(_filter_assignments(hit))
        self._categories, self._category_source = _filter_categories(assignments, known)
        self._filter_scripts = tuple((hit.name, hit.table, hit.row, commands.source_sha256[hit.table]) for hit in hits)
        self._command_tables = len(commands.loaded)
        self._absent_tables = tuple(sorted(commands.missing))
        self._command_version = commands.content_version

    @property
    def definitions(self) -> tuple[BackgroundDefinition, ...]:
        return self._definitions

    @property
    def by_name(self):
        return self._by_name

    @property
    def snapshot(self):
        return self._snapshot

    @property
    def content_version(self) -> str:
        return self._content_version

    @property
    def categories(self) -> frozenset[int]:
        return self._categories

    def replayable_content_versions(self) -> frozenset[str]:
        return self._replayable_versions

    def _master_digest(self):
        raw = self._cards.snapshot.get_xml("CardData")
        if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
            raise ValueError("Card snapshot must contain bounded immutable CardData")
        return self._card_digest(raw)

    def _read_cards(self):
        identifiers = tuple(self._cards.card_ids)
        if not 0 < len(identifiers) <= 65535:
            raise ValueError("Card identities require a bounded nonempty collection")
        origins = {}
        for identifier in identifiers:
            if not int32(identifier) or identifier <= 0 or identifier in origins:
                raise ValueError("Card identities are invalid or duplicated")
            row = self._cards.card(identifier)
            if (not isinstance(row, Mapping) or not int32(row.get("CardID")) or row["CardID"] != identifier
                    or not int32(row.get("OriginID")) or row["OriginID"] < 0):
                raise ValueError("Card origin metadata is invalid")
            origins[identifier] = row["OriginID"]
        if any(origin and origin not in origins for origin in origins.values()):
            raise ValueError("Card origin does not exist in the same master")
        return origins

    def _ready(self):
        try:
            if self._master_digest() != self._master_hash or self._read_cards() != self._origins:
                raise ValueError("Background card dependencies changed")
        except (AttributeError, ValueError, KeyError, TypeError, OverflowError, ProtocolError) as error:
            raise ProtocolError("background_catalog_changed", "Background card dependencies changed after setup", 503) from error

    def is_ready(self) -> bool:
        return caught(self._ready) is None

    def get(self, name: str) -> BackgroundDefinition:
        self._ready()
        if type(name) is not str or name not in self._by_name:
            raise ProtocolError("background_unknown", "Background name is not in the verified catalog")
        return self._by_name[name]

    def selection(self, name: str, guide_card_id: int, now: datetime) -> tuple[int, ...]:
        definition = self.get(name)
        if not int32(guide_card_id) or guide_card_id < 0 or guide_card_id and guide_card_id not in self._origins:
            raise ProtocolError("background_guide_invalid", "Background selection requires a verified guide identity")
        if definition.category not in self._categories:
            raise ProtocolError("background_unsupported",
                                "This background belongs to another native page, which displays it without saving", 501)
        try:
            current = native_clock(instant(now))
        except (ProtocolError, ValueError, TypeError, OverflowError) as error:
            raise ProtocolError("background_clock_invalid", "Background selection clock is invalid") from error
        if (definition.begin is not None and current < definition.begin
                or definition.end is not None and current > definition.end):
            raise ProtocolError("background_unavailable", "Background is outside its availability window", 409)
        if definition.card_id and definition.card_id != guide_card_id:
            raise ProtocolError("background_guide_mismatch", "Private background requires the current guide", 409)
        return (0,) if definition.is_fixed or guide_card_id == 0 else (0, guide_card_id)

    def guide_id(self, player_state: dict, configuration_row: dict) -> int:
        self._ready()
        try:
            if (not isinstance(player_state, dict) or not int32(player_state.get("schema_version"))
                    or player_state["schema_version"] != 1 or not isinstance(configuration_row, Mapping)):
                raise ValueError("Background selection requires supported saved state")
            selected = configuration_row.get(GUIDE_FIELD)
            if not int32(selected) or selected < 0:
                raise ValueError("Saved guide index is invalid")
            units = player_state.get("units")
            if not isinstance(units, list) or not 1 <= len(units) <= MAX_PLAYER_UNITS:
                raise ValueError("Saved units require a bounded nonempty roster")
            indices, unique_ids, card_id = set(), set(), 0
            fields = {name for name, _ in UNIT_FIELDS}
            for unit in units:
                if not isinstance(unit, Mapping) or unit.keys() != fields:
                    raise ValueError("Saved unit fields do not match the native contract")
                if any(type(unit[name]) is not bool if name in UNIT_BOOLEANS else not int32(unit[name]) for name in fields):
                    raise ValueError("Saved unit values have invalid types")
                if (unit["CardID"] not in self._origins or unit["ClassID"] <= 0 or unit["UniqueID"] <= 0
                        or any(unit[name] < 0 for name in ("GetIndex", "ClassChangeCount", "Exp", "Love", "SkillLevel",
                                                         "SettingSkillID", "SkillExperience", "CostExperience"))
                        or unit["GetIndex"] in indices or unit["UniqueID"] in unique_ids):
                    raise ValueError("Saved unit identity or growth values are invalid")
                indices.add(unit["GetIndex"])
                unique_ids.add(unit["UniqueID"])
                if unit["GetIndex"] == selected:
                    card_id = unit["CardID"]
            return self._identity(card_id)
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            raise ProtocolError("background_state_invalid", "Saved guide state is invalid", 503) from error

    def _identity(self, card_id):
        return self._origins.get(card_id, 0) or card_id

    def audit(self, rows) -> tuple[dict, ...]:
        self._ready()
        identities = set(map(self._identity, self._origins))
        findings = []
        for row in rows:
            card_id, definition = row["CardID"], self._by_name.get(row["ConfigID"])
            kinds = []
            if definition is None:
                kinds.append(UNKNOWN_NAME)
            elif definition.category not in self._categories:
                kinds.append(PAGE_BACKGROUND)
            if card_id:
                if definition is not None and definition.is_fixed:
                    kinds.append(FIXED_OUTSIDE_GLOBAL)
                if card_id not in identities:
                    kinds.append(NON_NORMALIZED_KEY)
                if definition is not None and definition.card_id not in (0, card_id):
                    kinds.append(PRIVATE_ROOM_MISMATCH)
            if kinds:
                findings.append({"CardID": card_id, "ConfigID": row["ConfigID"], "kinds": kinds})
        return tuple(findings)

    def status(self) -> dict:
        result = {"ready": True, "definition_count": len(self._definitions),
            "selectable_count": sum(row.category in self._categories for row in self._definitions),
            "categories": {"source": self._category_source, "values": sorted(self._categories),
                           "scripts": [{"name": name, "table": table, "row": row, "sha256": digest}
                                       for name, table, row, digest in self._filter_scripts],
                           "command_tables": self._command_tables, "absent_command_tables": list(self._absent_tables),
                           "command_content_version": self._command_version},
            "resource_sha256": {RESOURCE_NAME: self._resource_hash}, "decoded_sha256": self._decoded_hash,
            "master_sha256": self._master_hash, "content_version": self.content_version,
            "guide_identity": "get_index_then_one_origin_id_hop", "window": WINDOW_RULE,
            "scope": "ordinary_home_backgrounds", "other_pages": "display_only_never_saved",
            "native_selection_tested": False}
        try:
            self._ready()
        except ProtocolError as error:
            result.update(ready=False, error=error.code)
        return result
