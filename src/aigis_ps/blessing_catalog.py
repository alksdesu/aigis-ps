from __future__ import annotations

from collections import Counter
import hashlib
import json
import re
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, dumps
from .errors import ProtocolError, caught
from .native_numbers import int32
from .possessions import ITEM_RESOURCE_LIMIT, MAX_ITEM_NAME_BYTES, MAX_POSSESSION_ITEMS, STORED_ITEM_NAME_BYTES
from .system_literals import command_id, decode_command


CATEGORY = 190425180
FILTERS = MappingProxyType({0: "1;", 1: "$category==1;", 2: "$category==2;", 3: "$category==3;"})
LIMITS = ParseLimits(max_input_bytes=ITEM_RESOURCE_LIMIT, max_decompressed_bytes=ITEM_RESOURCE_LIMIT,
                     max_rows=MAX_POSSESSION_ITEMS)
ITEM_FIELDS = {"ItemID": 32, "ID": 1, "EffectType": 1, "Category": 1, "Stack": 1}
DISPLAY_FIELDS = {"ItemID": 32, "Name": 32, "Category": 1, "Description": 32}
COMMAND_FIELDS = {"_ID": 1, "_Cmnd": 32}
_BIND = re.compile(r'\s*CMenuTask_CallCommand\s*\(\s*"BindFilterID"\s*,\s*([0-9]+)\s*,\s*'
                   r'"([^"\\]*)"\s*,\s*"([^"\\]*)"\s*\)\s*;')
_TAIL = re.compile(r'\s*CMenuTask_CallCommand\s*\(\s*"ApplySavedFilter"\s*\)\s*;'
                   r'\s*CMenuTask_CallCommand\s*\(\s*"UpdateFilterButtons"\s*\)\s*;'
                   r'\s*CMenuTask_CallCommand\s*\(\s*"UpdateCellList"\s*\)\s*;\s*')


def _text(value, maximum, *, empty=False):
    if not isinstance(value, str) or (not empty and not value) or len(value.encode("utf-8")) > maximum:
        raise ValueError("Expected bounded resource text")
    dumps(Container(children=(Column("Value", "S", (value,)),)))
    return value


def _filters(rows):
    identifiers, initializer = set(), None
    for row in rows:
        identifier = row["_ID"]
        if not int32(identifier) or identifier in identifiers:
            raise ValueError("Filter command identities are invalid or duplicated")
        identifiers.add(identifier)
        command = decode_command(row["_Cmnd"])
        if "BindFilterID" not in command:
            continue
        if initializer is not None:
            raise ValueError("Filter bindings must have one initializer")
        initializer = command
    if initializer is None:
        raise ValueError("Filter bindings are unavailable")
    bindings, position = {}, 0
    while match := _BIND.match(initializer, position):
        identifier = int(match.group(1))
        widget, expression = match.group(2), re.sub(r"\s+", "", match.group(3))
        if identifier in bindings or widget != f"CMenuBattleGodBlessList::FilterButton{identifier}":
            raise ValueError("Filter bindings are inconsistent")
        bindings[identifier] = expression
        position = match.end()
    if bindings != FILTERS or _TAIL.fullmatch(initializer[position:]) is None:
        raise ValueError("Filter initialization differs from the verified native contract")
    return frozenset(bindings)


class BlessingCatalog:
    def __init__(self, possessions, asset_reader):
        self._possessions = possessions
        try:
            if not callable(asset_reader):
                raise ValueError("Blessing definitions require a resource reader")
            hashes = {}

            def table(name, expected):
                raw = asset_reader(name)
                if not isinstance(raw, bytes) or not 0 < len(raw) <= ITEM_RESOURCE_LIMIT:
                    raise ValueError("Blessing resources must be bounded immutable bytes")
                data = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
                result = parse_table(data, LIMITS)
                fields = {field["name"]: field["type_code"] for field in result["fields"]}
                if len(fields) != len(result["fields"]) or any(fields.get(name) != kind for name, kind in expected.items()):
                    raise ValueError("Blessing resource fields differ from the native contract")
                hashes[name] = hashlib.sha256(data).hexdigest()
                return result["rows"]

            selected, names, identifiers = set(), set(), set()
            for row in table("ItemConfigTable.atb", ITEM_FIELDS):
                name = _text(row["ItemID"], STORED_ITEM_NAME_BYTES)
                identifier = row["ID"]
                if (not int32(identifier) or identifier != command_id(name) or name in names or identifier in identifiers
                        or any(not int32(row[field]) for field in ("EffectType", "Category", "Stack"))):
                    raise ValueError("Item definitions have invalid or duplicate identities")
                names.add(name)
                identifiers.add(identifier)
                if row["EffectType"] == 12:
                    if row["Category"] != CATEGORY or row["Stack"] != 1:
                        raise ValueError("Blessings require the verified category and unique ownership")
                    selected.add(name)
            # 148 carries names within the native cell, so a longer stored blessing stays owned but not settable.
            settable = frozenset(name for name in selected if len(name.encode("utf-8")) <= MAX_ITEM_NAME_BYTES)
            if not settable:
                raise ValueError("Blessing definitions are empty")
            displays = {}
            for row in table("BattleGodBlessConfigTable.atb", DISPLAY_FIELDS):
                name = _text(row["ItemID"], STORED_ITEM_NAME_BYTES)
                if name in displays or not int32(row["Category"]) or row["Category"] not in (1, 2, 3):
                    raise ValueError("Blessing display identities or categories are invalid")
                displays[name] = MappingProxyType({"category": row["Category"],
                    "name": _text(row["Name"], 256), "description": _text(row["Description"], 4096)})
            if set(displays) != selected:
                raise ValueError("Blessing inventory and display definitions do not match")
            self._filter_values = _filters(table("OnCommandMenuBattleGodBlessList.atb", COMMAND_FIELDS))
            self._item_ids = settable
            self._unsupported_items = frozenset(selected - settable)
            self._displays = MappingProxyType({name: displays[name] for name in settable})
            self._hashes = MappingProxyType(hashes)
            self._ready()
            content = {"version": 1, "resources": hashes, "filters": sorted(self._filter_values),
                       "ownership": "positive_count_including_legacy_overstack", "settings": "native_int_zero_or_one"}
            self.content_version = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError) as error:
            raise ProtocolError("blessing_catalog_invalid", "Blessing resources differ from the verified contract", 503) from error

    @property
    def item_ids(self) -> frozenset[str]:
        return self._item_ids

    @property
    def filter_values(self) -> frozenset[int]:
        return self._filter_values

    def _ready(self):
        try:
            stacks = [self._possessions.stack(name) for name in self._item_ids]
            if (not self._possessions.is_ready()
                    or self._possessions.asset_sha256 != self._hashes["ItemConfigTable.atb"]
                    or any(type(value) is not int or value != 1 for value in stacks)):
                raise ValueError("Possession definitions changed")
        except (AttributeError, TypeError, ValueError) as error:
            raise ProtocolError("blessing_catalog_changed", "Blessing and possession definitions must remain identical", 503) from error

    def is_ready(self) -> bool:
        return caught(self._ready) is None

    def validate(self, state, settings: dict[str, int]) -> None:
        self._ready()
        if not isinstance(settings, dict) or len(settings) > len(self._item_ids):
            raise ProtocolError("blessing_settings_invalid", "Blessing settings require a bounded item mapping")
        for name, value in settings.items():
            if not isinstance(name, str) or name not in self._item_ids:
                raise ProtocolError("blessing_item_unknown", "Setting item is not a verified battle blessing")
            if type(value) is not int or value not in (0, 1):
                raise ProtocolError("blessing_settings_invalid", "Blessing enabled values must be native zero or one")
        counts = self._possessions.counts(state)
        if any(counts.get(name, 0) <= 0 for name in settings):
            raise ProtocolError("blessing_not_owned", "Only owned blessings can be enabled or disabled", 409)

    def status(self) -> dict:
        result = {"ready": True, "item_count": len(self._item_ids), "filters": sorted(self._filter_values),
                  "category_counts": dict(Counter(row["category"] for row in self._displays.values())),
                  "unsupported_items": sorted(self._unsupported_items),
                  "resource_sha256": dict(self._hashes), "content_version": self.content_version,
                  "ownership_policy": "Positive count; existing overstack ownership remains editable",
                  "name_policy": (f"Settable blessings need names within the {MAX_ITEM_NAME_BYTES}-byte native cell; "
                                  f"stored names up to {STORED_ITEM_NAME_BYTES} bytes stay owned"),
                  "native_battle_tested": False}
        try:
            self._ready()
        except ProtocolError as error:
            result.update(ready=False, error=error.code)
        return result
