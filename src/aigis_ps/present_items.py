from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .errors import ProtocolError
from .native_numbers import INT32_MIN, int32
from .possessions import ITEM_RESOURCE_LIMIT, MAX_POSSESSION_ITEMS, STORED_ITEM_NAME_BYTES
from .system_literals import command_id


LEGACY_ITEMS = MappingProxyType({
    3: ("gift_0000_bouquet", 999), 4: ("gift_0001_quartz", 999),
    5: ("gift_0002_ruby", 999), 6: ("gift_0003_diamond", 999),
    7: ("trade_item_001000", 9999), 10: ("gift_1000_beer", 999),
    11: ("gift_1001_wine", 999), 12: ("gift_1002_fineLiquor", 999),
    13: ("gift_1003_millenniumWine", 999), 14: ("crystal_item_001000", 9999),
    19: ("trade_item_003000", 999999),
})
PASSIVE_EFFECTS = frozenset((4, 5, 6, 7, 9))
OWNERSHIP_EFFECTS = MappingProxyType({12: 190425180, 13: 1165413830})
LEGACY_EFFECTS = MappingProxyType({kind: 6 if kind in (7, 19) else 7 if kind == 14 else 4
                                  for kind in LEGACY_ITEMS})
FIELDS = {"ItemID": 32, "ID": 1, "Stack": 1, "EffectType": 1,
          "ReceiveAction": 1, "EventID": 32, "ReceiveConfirmation": 1,
          "Category": 1, "CanAllGet": 1, "NoAutoDelete": 1, "InvisibleContaint": 1}
LIMITS = ParseLimits(max_input_bytes=ITEM_RESOURCE_LIMIT, max_decompressed_bytes=ITEM_RESOURCE_LIMIT,
                     max_rows=MAX_POSSESSION_ITEMS)


@dataclass(frozen=True)
class PresentItemDefinition:
    item_id: str
    stack_limit: int
    single_only: bool = False


def _integer(value, minimum=0):
    return int32(value) and value >= minimum


def _digest(content):
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


class PresentItemCatalog:
    def __init__(self, possessions, payload: bytes, *, emblems=None):
        self._possessions = possessions
        self._emblems = emblems
        self._emblem_version = None
        self._emblem_ids = frozenset()
        try:
            if emblems is not None:
                self._emblem_version = emblems.content_version
                self._emblem_ids = emblems.item_ids
            if not isinstance(payload, bytes) or not 0 < len(payload) <= ITEM_RESOURCE_LIMIT:
                raise ValueError("Item definitions must be bounded bytes")
            data = decompress_all4(payload, LIMITS) if payload.startswith(b"ALL4") else payload
            table = parse_table(data, LIMITS)
            fields = {field["name"]: field["type_code"] for field in table["fields"]}
            if len(fields) != len(table["fields"]) or any(fields.get(name) != kind for name, kind in FIELDS.items()):
                raise ValueError("Present item fields differ from the verified contract")
            if emblems is not None and (fields.get("EmblemID") != 1
                    or not isinstance(self._emblem_ids, frozenset) or not self._emblem_ids
                    or emblems.item_resource_sha256 != hashlib.sha256(data).hexdigest()):
                raise ValueError("Emblem and present definitions must share the same item resource")
            rows, identifiers, items = {}, set(), {}
            for source in table["rows"]:
                row = {name: source[name] for name in FIELDS}
                name, identifier, stack = row["ItemID"], row["ID"], row["Stack"]
                if not isinstance(name, str) or not name or len(name.encode("utf-8")) > STORED_ITEM_NAME_BYTES:
                    raise ValueError("Present items require bounded nonempty names")
                if (not _integer(identifier, INT32_MIN) or identifier != command_id(name)
                        or name in rows or identifier in identifiers):
                    raise ValueError("Present item identity is invalid or duplicated")
                if (any(not _integer(row[field]) for field in ("Stack", "EffectType", "ReceiveAction",
                                                              "CanAllGet", "NoAutoDelete", "InvisibleContaint"))
                        or any(not _integer(row[field], INT32_MIN) for field in ("ReceiveConfirmation", "Category"))):
                    raise ValueError("Present item rules must be nonnegative native integers")
                if not isinstance(row["EventID"], str):
                    raise ValueError("Present item event must be a string")
                rows[name] = row
                identifiers.add(identifier)
                ordinary = row["EffectType"] in PASSIVE_EFFECTS and stack > 0
                ownership = (row["EffectType"] in OWNERSHIP_EFFECTS
                    and row["Category"] == OWNERSHIP_EFFECTS[row["EffectType"]] and stack == 1
                    and row["CanAllGet"] == 0 and row["NoAutoDelete"] == 1 and row["InvisibleContaint"] == 0)
                emblem = name in self._emblem_ids
                if emblem and (row["EffectType"] != 10 or row["Category"] != 1078486164 or stack != 1
                        or row["CanAllGet"] != 0 or row["NoAutoDelete"] != 0 or row["InvisibleContaint"] != 0
                        or type(source["EmblemID"]) is not int or source["EmblemID"] != emblems.items[name].emblem_id):
                    raise ValueError("Emblem mail must preserve its verified identity and individual ownership rules")
                if ((ordinary or ownership or emblem) and row["ReceiveAction"] == 0
                        and row["EventID"] == "" and row["ReceiveConfirmation"] == 0):
                    items[name] = PresentItemDefinition(name, stack, single_only=ownership or emblem)
            if not rows:
                raise ValueError("Present items require nonempty definitions")
            if self._emblem_ids - items.keys():
                raise ValueError("Enabled emblem mail is absent from the item resource")
            self.asset_sha256 = hashlib.sha256(data).hexdigest()
            self._rows = MappingProxyType({name: MappingProxyType(row) for name, row in rows.items()})
            self._items = MappingProxyType(items)
            self._skin_item_ids = frozenset(name for name in items if rows[name]["EffectType"] == 13)
            self._legacy = MappingProxyType({kind: PresentItemDefinition(name, min(limit, items[name].stack_limit))
                                             for kind, (name, limit) in LEGACY_ITEMS.items()
                                             if name in items and rows[name]["EffectType"] == LEGACY_EFFECTS[kind]})
            self.supported_types = tuple(sorted((*self._legacy, *((22,) if items else ()))))
            self._check_dependencies()
            for name, row in rows.items():
                self._check_definition(name, row["Stack"])
            content = {"version": 2, "item_resource": self.asset_sha256, "possessions": self.asset_sha256,
                       "legacy": dict(LEGACY_ITEMS), "legacy_effects": dict(LEGACY_EFFECTS),
                       "effects": sorted(PASSIVE_EFFECTS),
                       "ownership_effects": dict(OWNERSHIP_EFFECTS),
                       "rules": {"positive_stack": True, "receive_action": 0, "event": "", "confirmation": 0,
                                 "ownership": {"stack": 1, "can_all_get": 0, "no_auto_delete": 1,
                                               "invisible_count": 0, "single_only": True}}}
            # Receipts committed before emblem mail attached keep the digest of the catalog without it.
            without_emblems = _digest(content)
            if emblems is not None:
                content.update(version=3, emblems=self._emblem_version)
            self.content_version = _digest(content)
            self._replayable_versions = frozenset((self.content_version, without_emblems))
        except ProtocolError:
            raise
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError) as error:
            raise ProtocolError("present_item_catalog_invalid", "Present item resources differ from the verified contract", 503) from error

    def _check_dependencies(self):
        try:
            if (not self._possessions.is_ready()
                    or self._possessions.asset_sha256 != self.asset_sha256
                    or not callable(self._possessions.stack)):
                raise ValueError("Possession definitions changed or are unavailable")
            if self._emblems is not None and (not self._emblems.is_ready()
                    or self._emblems.content_version != self._emblem_version
                    or self._emblems.item_ids != self._emblem_ids
                    or self._emblems.item_resource_sha256 != self.asset_sha256):
                raise ValueError("Emblem mail dependencies changed")
        except (AttributeError, ValueError, TypeError, ProtocolError) as error:
            raise ProtocolError("present_item_dependencies_changed", "Present and possession definitions must match", 503) from error

    def _check_definition(self, name, stack):
        try:
            current = self._possessions.stack(name)
            if not _integer(current) or current != stack:
                raise ValueError("Possession stack differs from the frozen item definition")
        except (AttributeError, ValueError, TypeError, ProtocolError) as error:
            raise ProtocolError("present_item_dependencies_changed", "Present and possession item definitions must match", 503) from error

    def definition(self, kind: int, item_id: str | None = None) -> PresentItemDefinition:
        self._check_dependencies()
        if not _integer(kind) or kind not in (*LEGACY_ITEMS, 22):
            raise ProtocolError("present_item_unsupported", "This present type is not a supported stock item", 501)
        if kind == 22:
            if not isinstance(item_id, str) or item_id not in self._items:
                raise ProtocolError("present_item_unsupported", "This item requires an unsupported present action", 501)
            result = self._items[item_id]
        else:
            if item_id is not None:
                raise ProtocolError("present_item_identity_invalid", "Legacy present types have a fixed item identity")
            if kind not in self._legacy:
                raise ProtocolError("present_item_unsupported", "This legacy present item is unavailable", 501)
            result = self._legacy[kind]
        self._check_definition(result.item_id, self._rows[result.item_id]["Stack"])
        return result

    @property
    def rows(self) -> Mapping[str, Mapping[str, object]]:
        return self._rows

    @property
    def skin_item_ids(self) -> frozenset[str]:
        return self._skin_item_ids

    @property
    def emblem_item_ids(self) -> frozenset[str]:
        return self._emblem_ids

    def replayable_content_versions(self) -> frozenset[str]:
        return self._replayable_versions

    def status(self) -> dict:
        result = {"ready": True, "supported_types": list(self.supported_types), "item_count": len(self._items),
                  "legacy_count": len(self._legacy), "definition_count": len(self._rows),
                  "content_version": self.content_version, "asset_sha256": self.asset_sha256,
                  "passive_effects": sorted(PASSIVE_EFFECTS),
                  "ownership_effects": sorted(OWNERSHIP_EFFECTS),
                  "single_only_count": sum(item.single_only for item in self._items.values()),
                  "skin_item_count": len(self._skin_item_ids),
                  "emblem_item_count": len(self._emblem_ids),
                  "policy": "Ordinary positive-stack items; verified ownership items require Stack 1 and individual claims; no automatic activation"}
        try:
            self._check_dependencies()
            for name, definition in self._items.items():
                self._check_definition(name, definition.stack_limit)
        except ProtocolError as error:
            result.update(ready=False, error=error.code)
        return result
