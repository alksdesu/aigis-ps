from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
import zlib

from .al_format import decompress_all4, parse_table
from .errors import ProtocolError
from .native_commands import ABSENT_TABLE, LIMITS, STATUS_LIMIT, NativeCommandIndex
from .native_numbers import int32
from .system_literals import command_id, decode_command
from .universal_flags import MAX_FLAG_NAME_BYTES, flag_hash


HOME_TRIGGER = "HomeTrigger.atb"
RECOMMEND_MENU = "RecommendMenuConfig.atb"
MISSION_PORTAL = "CfgMissionPortalConfigTable.atb"
MISSION_HISTORY = "CfgMissionHistoryTable.atb"
ROTATION_TRIGGER = "RotationTriggerConfig.atb"
FILTER_COMMAND = "FilterCommand.atb"
FILTER_GROUP = "FilterGroup.atb"
SKIN_SHOP = "SkinShop.atb"
PAYMENT_BONUS = "PaymentBonusConfig.atb"
INT32, STRING = "int32", "string_ref"
SOURCE_FIELDS = MappingProxyType({
    HOME_TRIGGER: {"Expression": STRING, "Cmnd": STRING},
    RECOMMEND_MENU: {"CompleteExpression": STRING, "AfterCommand": STRING, "UniversalFlag": STRING},
    MISSION_PORTAL: {"FlagName": STRING},
    MISSION_HISTORY: {"GroupId": INT32},
    ROTATION_TRIGGER: {"Command": STRING},
    FILTER_COMMAND: {"Expression": STRING},
    FILTER_GROUP: {"DisplayExpression": STRING, "ExecuteExpression": STRING},
    SKIN_SHOP: {"PurchaseFlag": INT32},
    PAYMENT_BONUS: {"ConfigId": INT32},
})
# The shared command key is the big-endian CRC32 of this name; 95D220 and 956F60 key their columns the same way.
COMMAND_KEY = "OnCommand"
RECOMMEND_KEY = "Recommend"
COMMAND_COLUMN = "_Cmnd"
SCRIPT_COLUMNS = (
    (HOME_TRIGGER, "Expression", "HomeTrigger"), (HOME_TRIGGER, "Cmnd", "HomeTrigger"),
    (RECOMMEND_MENU, "CompleteExpression", RECOMMEND_KEY), (RECOMMEND_MENU, "AfterCommand", RECOMMEND_KEY),
    (ROTATION_TRIGGER, "Command", COMMAND_KEY), (FILTER_COMMAND, "Expression", "FilterCommand"),
    (FILTER_GROUP, "DisplayExpression", "FilterGroup"), (FILTER_GROUP, "ExecuteExpression", "FilterGroup"),
)
SCRIPT, MISSION_PORTAL_FAMILY, HISTORY_GROUP, RECOMMENDED_READ = "script", "mission_portal", "history_group", "recommend"
FAMILIES = (SCRIPT, MISSION_PORTAL_FAMILY, HISTORY_GROUP, RECOMMENDED_READ)
READ_VALUE = 1
WRITE_COMMAND = "SetUniversalFlags"
NAME_ARGUMENT, VALUE_ARGUMENT, ARGUMENT_SEPARATOR = "Flags", "Value", "="
# 94ABC0 keeps the native spelling of this prefix and appends the decimal GroupId.
HISTORY_FLAG = "AppeardHistoryGroupFlag_{}"
SKIN_PURCHASE_PREFIX = "PurchasedSkin_"
PAYMENT_BONUS_PREFIX = "PaymentBonusObtained:id="
PROTECTED_PREFIXES = (SKIN_PURCHASE_PREFIX, PAYMENT_BONUS_PREFIX)
EMPTY_NAME, OVERSIZED_NAME, NONMONOTONIC_VALUE = "empty_name", "name_exceeds_native_cell", "nonmonotonic_value"
CATALOG_INVALID = "universal_flags_catalog_invalid"
CATALOG_CONFLICT = "universal_flags_catalog_conflict"
RULE_VERSION = "universal-flags-v1"
RULES = MappingProxyType({
    SCRIPT: "literal_name_with_its_nonzero_literal_value", MISSION_PORTAL_FAMILY: "nonempty_FlagName_value_1",
    HISTORY_GROUP: "prefix_and_decimal_GroupId_value_1", RECOMMENDED_READ: "every_decoded_UniversalFlag_value_1",
    "protected": "prefixes_and_SkinShop_PaymentBonus_hashes", "name_bytes": MAX_FLAG_NAME_BYTES,
})
_HEX = re.compile(r"(?:[0-9A-Fa-f]{2})*\Z")
# Scripts separate statements only with tabs and line breaks; other control characters mean the key or table drifted.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_LITERAL = r'"([^"\\\x00-\x1f]*)"'
_WRITER = re.compile(r"(?<![0-9A-Za-z_]){}(?![0-9A-Za-z_])".format(re.escape(WRITE_COMMAND)), re.ASCII)
_LITERAL_WRITE = re.compile(r"{}\s*\(\s*{}\s*,\s*{}\s*\)".format(re.escape(WRITE_COMMAND), _LITERAL, _LITERAL), re.ASCII)
_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")


def _unpack(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
        raise ValueError("Universal-flag resources must be bounded immutable bytes")
    return decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw


def _rows(data, fields):
    table = parse_table(data, LIMITS)
    columns = {field["name"]: field["type_name"] for field in table["fields"]}
    if len(columns) != len(table["fields"]) or any(columns.get(name) != kind for name, kind in fields.items()):
        raise ValueError("Universal-flag resource fields differ from the native contract")
    return table["rows"]


def _text(value):
    if type(value) is not str or _CONTROL.search(value):
        raise ValueError("Universal-flag resource text must be printable")
    return value


def _decoded(encoded, key):
    if key == COMMAND_KEY:
        return _text(decode_command(encoded))
    if type(encoded) is not str or not _HEX.fullmatch(encoded):
        raise ValueError("Encoded resource text must be hexadecimal")
    mask = zlib.crc32(key.encode("utf-8")).to_bytes(4, "big")
    data = bytes.fromhex(encoded)
    return _text(bytes(value ^ mask[index % len(mask)] for index, value in enumerate(data)).decode("utf-8"))


def _literal_write(parts):
    arguments = dict(part.split(ARGUMENT_SEPARATOR, 1) for part in parts if ARGUMENT_SEPARATOR in part)
    if arguments.keys() != {NAME_ARGUMENT, VALUE_ARGUMENT} or not _INTEGER.fullmatch(arguments[VALUE_ARGUMENT]):
        return None
    value = int(arguments[VALUE_ARGUMENT])
    return (arguments[NAME_ARGUMENT], value) if int32(value) else None


def _script_writes(text):
    for occurrence in _WRITER.finditer(text):
        call = _LITERAL_WRITE.match(text, occurrence.start())
        yield None if call is None else _literal_write(call.groups())


@dataclass(frozen=True)
class FlagSource:
    family: str
    table: str
    column: str
    row: int

    def describe(self) -> dict:
        return {"family": self.family, "table": self.table, "column": self.column, "row": self.row}


@dataclass(frozen=True)
class FlagDefinition:
    name: str
    values: frozenset[int]
    sources: tuple[FlagSource, ...]

    @property
    def families(self) -> frozenset[str]:
        return frozenset(source.family for source in self.sources)


class UniversalFlagsCatalog:
    def __init__(self, asset_reader: Callable[[str], bytes]):
        try:
            if not callable(asset_reader):
                raise ValueError("Universal flags require a resource reader")
            rows, digests = {}, {}
            for name, fields in SOURCE_FIELDS.items():
                raw = asset_reader(name)
                rows[name] = _rows(_unpack(raw), fields)
                digests[name] = hashlib.sha256(raw).hexdigest()
            self._values, self._sources, self._excluded, self._dynamic = {}, {}, [], []
            self._derive_scripts(asset_reader, rows)
            self._derive_tables(rows)
            self._derive_protected(rows)
            self._definitions = MappingProxyType({name: FlagDefinition(name, frozenset(values), tuple(self._sources[name]))
                                                  for name, values in self._values.items()})
            self._digests = MappingProxyType(digests)
            version = {"rule": RULE_VERSION, "rules": dict(RULES), "scripts": [list(column) for column in SCRIPT_COLUMNS],
                       "resources": digests, "commands": self._commands.content_version}
            self._content_version = hashlib.sha256(json.dumps(version, sort_keys=True).encode()).hexdigest()
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError) as error:
            raise ProtocolError(CATALOG_INVALID, "Universal-flag resources differ from the verified contract", 503) from error
        self._check_conflicts()

    def _derive_scripts(self, asset_reader, rows):
        tables = {}

        def read(name):
            tables[name] = _unpack(asset_reader(name))
            return tables[name]

        self._commands = NativeCommandIndex(read)
        if self._commands.invalid or any(code != ABSENT_TABLE for code in self._commands.missing.values()):
            raise ValueError("Universal-flag command tables are unreadable")
        cells = [(table, column, key, number, row[column]) for table, column, key in SCRIPT_COLUMNS
                 for number, row in enumerate(rows[table])]
        cells += [(table, COMMAND_COLUMN, COMMAND_KEY, number, row[COMMAND_COLUMN]) for table in self._commands.loaded
                  for number, row in enumerate(parse_table(tables[table], LIMITS)["rows"])]
        for table, column, key, number, encoded in cells:
            for write in _script_writes(_decoded(encoded, key)):
                source = FlagSource(SCRIPT, table, column, number)
                if write is None:
                    self._dynamic.append(source)
                else:
                    self._admit(*write, source)

    def _derive_tables(self, rows):
        for number, row in enumerate(rows[MISSION_PORTAL]):
            # AA0D70 marks an entry without FlagName as already read, so 775680 never writes it.
            if _text(row["FlagName"]):
                self._admit(row["FlagName"], READ_VALUE, FlagSource(MISSION_PORTAL_FAMILY, MISSION_PORTAL, "FlagName", number))
        groups = {}
        for number, row in enumerate(rows[MISSION_HISTORY]):
            if not int32(row["GroupId"]):
                raise ValueError("History groups require native integers")
            groups.setdefault(row["GroupId"], number)
        for group, number in groups.items():
            self._admit(HISTORY_FLAG.format(group), READ_VALUE, FlagSource(HISTORY_GROUP, MISSION_HISTORY, "GroupId", number))
        for number, row in enumerate(rows[RECOMMEND_MENU]):
            self._admit(_decoded(row["UniversalFlag"], RECOMMEND_KEY), READ_VALUE,
                        FlagSource(RECOMMENDED_READ, RECOMMEND_MENU, "UniversalFlag", number))

    def _derive_protected(self, rows):
        purchases = {row["PurchaseFlag"] for row in rows[SKIN_SHOP]}
        payments = {row["ConfigId"] for row in rows[PAYMENT_BONUS]}
        if not all(map(int32, purchases | payments)):
            raise ValueError("Protected flag sources require native integers")
        # 924440 appends the decimal ConfigId to the payment bonus prefix.
        self._payment_names = tuple(sorted(f"{PAYMENT_BONUS_PREFIX}{config}" for config in payments))
        self._skin_purchases = len(purchases)
        self._protected = frozenset(purchases | set(map(flag_hash, self._payment_names)))

    def _admit(self, name, value, source):
        reason = (EMPTY_NAME if not name else OVERSIZED_NAME if len(name.encode("utf-8")) > MAX_FLAG_NAME_BYTES
                  else NONMONOTONIC_VALUE if value == 0 else None)
        if reason is not None:
            self._excluded.append((reason, name, source))
            return
        flag_hash(_text(name))
        self._values.setdefault(name, set()).add(value)
        self._sources.setdefault(name, []).append(source)

    def _check_conflicts(self):
        hashes = set(map(flag_hash, self._definitions))
        if len(hashes) != len(self._definitions) or any(map(self.protected, self._definitions)):
            raise ProtocolError(CATALOG_CONFLICT, "Writable universal flags collide with each other or with protected flags", 503)

    @property
    def definitions(self):
        return self._definitions

    @property
    def content_version(self) -> str:
        return self._content_version

    def get(self, name: str) -> FlagDefinition | None:
        return self._definitions.get(name) if type(name) is str else None

    def protected(self, name: str) -> bool:
        return type(name) is not str or name.startswith(PROTECTED_PREFIXES) or command_id(name) in self._protected

    def is_ready(self) -> bool:
        # Construction refuses unverified resources, so every built catalog is ready.
        return True

    def status(self) -> dict:
        families = {family: sorted(name for name, definition in self._definitions.items() if family in definition.families)
                    for family in FAMILIES}
        return {"ready": self.is_ready(), "name_count": len(self._definitions),
                "families": {family: {"count": len(names), "names": names[:STATUS_LIMIT]}
                             for family, names in families.items()},
                "protected": {"prefixes": list(PROTECTED_PREFIXES), "skin_purchase_count": self._skin_purchases,
                              "payment_bonus_names": list(self._payment_names)},
                "dynamic_writers": {"count": len(self._dynamic),
                                    "positions": [source.describe() for source in self._dynamic[:STATUS_LIMIT]]},
                "excluded_writes": {"count": len(self._excluded),
                                    "entries": [{**source.describe(), "reason": reason,
                                                 "name": name if reason != OVERSIZED_NAME else None}
                                                for reason, name, source in self._excluded[:STATUS_LIMIT]]},
                "script_columns": [list(column) for column in SCRIPT_COLUMNS],
                "command_tables": len(self._commands.loaded), "absent_command_tables": sorted(self._commands.missing),
                "command_content_version": self._commands.content_version,
                "resource_sha256": dict(self._digests), "content_version": self._content_version,
                "conditions": "Client-side rank, account-day and date conditions are not rechecked"}
