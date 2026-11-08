from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .errors import ProtocolError
from .native_numbers import int32
from .possessions import ITEM_UNKNOWN as POSSESSION_ITEM_UNKNOWN, STORED_ITEM_NAME_BYTES
from .system_literals import command_id


LIMITS = ParseLimits(max_input_bytes=8 * 1024 * 1024, max_decompressed_bytes=8 * 1024 * 1024)
SKIN_FIELDS = {"ConfigId": 1, "FreeCondition": 1, "ConditionItemId": 32, "SmartPhoneDiff": 5, "Aggregate": 1}
MAX_SKIN_NAME_BYTES = 256


def _name(value, maximum):
    return isinstance(value, str) and len(value.encode("utf-8")) <= maximum and "\0" not in value


@dataclass(frozen=True)
class _Skin:
    display_type: int
    free: bool
    smartphone: bool
    item_id: str | None
    missing_item: bool


class UnitSkinCatalog:
    def __init__(self, cards, possessions, asset_reader):
        self._possessions = possessions
        try:
            if not callable(asset_reader) or not possessions.is_ready():
                raise ValueError("Verified resource reader and possession definitions are required")
            item_hash = possessions.asset_sha256
            if not isinstance(item_hash, str) or re.fullmatch(r"[a-f0-9]{64}", item_hash) is None:
                raise ValueError("Possession definitions need a fixed fingerprint")
            card_data = cards.snapshot.get_xml("CardData")
            if not isinstance(card_data, bytes):
                raise ValueError("Card snapshot must contain serialized CardData")
            raw = asset_reader("SkinPackage.atb")
            if not isinstance(raw, bytes) or len(raw) > LIMITS.max_input_bytes:
                raise ValueError("Skin resource must be bounded bytes")
            table = parse_table(decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw, LIMITS)
            fields = {field["name"]: field["type_code"] for field in table["fields"]}
            if len(fields) != len(table["fields"]) or any(fields.get(name) != kind for name, kind in SKIN_FIELDS.items()):
                raise ValueError("Skin resource fields differ from the native contract")
            packages = {}
            for row in table["rows"]:
                identifier = row["ConfigId"]
                if (not int32(identifier) or identifier in packages or not int32(row["FreeCondition"])
                        or not _name(row["ConditionItemId"], STORED_ITEM_NAME_BYTES) or type(row["SmartPhoneDiff"]) is not bool
                        or not int32(row["Aggregate"])):
                    raise ValueError("Skin definitions contain invalid or duplicated values")
                packages[identifier] = MappingProxyType(dict(row))
            counted, counted_names, missing_counted = {}, set(), set()
            for package in packages.values():
                if not package["Aggregate"]:
                    continue
                item_id = package["ConditionItemId"]
                if package["FreeCondition"] or command_id(item_id) == 0 or item_id in counted_names:
                    raise ValueError("Counted skin packages require distinct item-dependent unlocks")
                counted_names.add(item_id)
                try:
                    resolved = possessions.resolve_hash(command_id(item_id))
                except ProtocolError as error:
                    if error.code != POSSESSION_ITEM_UNKNOWN:
                        raise
                    missing_counted.add(item_id)
                    continue
                stack = possessions.stack(item_id)
                if resolved != item_id or not int32(stack) or stack < 0:
                    raise ValueError("Counted skins require verified nonnegative-stack unlock items")
                counted[item_id] = stack
            links, missing = {}, {}
            for card_id in cards.card_ids:
                if not int32(card_id) or card_id <= 0 or card_id in links or len(links) >= 65536:
                    raise ValueError("Card identities must be bounded and unique")
                card = cards.card(card_id)
                if not isinstance(card, Mapping) or not int32(card.get("CardID")) or card["CardID"] != card_id:
                    raise ValueError("Card identity differs from its snapshot")
                skins = []
                for slot in range(1, 5):
                    name = card[f"ExtendSkin{slot:02}"]
                    if name is None:
                        continue
                    if not _name(name, MAX_SKIN_NAME_BYTES):
                        raise ValueError("Skin references must be bounded strings")
                    if name in ("", "null"):
                        continue
                    package = packages.get(command_id(name))
                    if package is None:
                        raise ValueError("Card references a missing skin definition")
                    item_id = package["ConditionItemId"]
                    free = package["FreeCondition"] != 0 or command_id(item_id) == 0
                    missing_item = False
                    if not free:
                        try:
                            resolved = possessions.resolve_hash(command_id(item_id))
                        except ProtocolError as error:
                            if error.code != POSSESSION_ITEM_UNKNOWN:
                                raise
                            missing_item = True
                            missing.setdefault(item_id, set()).add(card_id)
                        else:
                            if resolved != item_id:
                                raise ValueError("Skin item hash resolves to a different canonical name")
                    skins.append(_Skin(slot + 4, free, package["SmartPhoneDiff"],
                                       None if free else item_id, missing_item))
                links[card_id] = tuple(skins)
            if not links:
                raise ValueError("Card snapshot is empty")
            self._packages = MappingProxyType(packages)
            self._links = MappingProxyType(links)
            self._missing = MappingProxyType({name: tuple(sorted(ids)) for name, ids in missing.items()})
            self._counted_definitions = MappingProxyType(counted)
            self._counted_item_ids = frozenset(counted)
            self._missing_counted = tuple(sorted(missing_counted))
            self._item_hash = item_hash
            self._hashes = MappingProxyType({"CardData": hashlib.sha256(card_data).hexdigest(),
                                             "SkinPackage.atb": hashlib.sha256(raw).hexdigest(),
                                             "ItemConfigTable.atb": item_hash})
            encoded = json.dumps({"rule": "unit-skin-v1", "resources": dict(self._hashes)},
                                 sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.content_version = hashlib.sha256(encoded).hexdigest()
        except (AttributeError, KeyError, TypeError, ValueError, OSError, ProtocolError) as error:
            raise ProtocolError("unit_skin_catalog_invalid", "Skin resources do not satisfy the verified contract", 503) from error

    @property
    def counted_item_ids(self) -> frozenset[str]:
        return self._counted_item_ids

    @property
    def resource_sha256(self) -> Mapping[str, str]:
        return self._hashes

    def is_ready(self) -> bool:
        # Construction refuses unverified resources, so every built catalog is ready.
        return True

    def acquired_items(self, state) -> frozenset[str]:
        if self._possessions.asset_sha256 != self._item_hash or not self._possessions.is_ready():
            raise ProtocolError("unit_skin_catalog_changed", "Skin possession definitions changed after catalog creation", 503)
        for item_id, stack in self._counted_definitions.items():
            if (self._possessions.resolve_hash(command_id(item_id)) != item_id
                    or self._possessions.stack(item_id) != stack):
                raise ProtocolError("unit_skin_catalog_changed", "Counted skin definitions changed after catalog creation", 503)
        counts = self._possessions.counts(state)
        return frozenset(item for item, count in counts.items() if count > 0 and item in self._counted_item_ids)

    def allowed_displays(self, card_id, state, *, smartphone_images: bool) -> frozenset[int]:
        if not int32(card_id) or card_id not in self._links:
            raise ProtocolError("unit_skin_card_unknown", "Skin target is not in the verified card snapshot")
        if type(smartphone_images) is not bool:
            raise ProtocolError("unit_skin_selection_invalid", "Smartphone image selection must be boolean")
        if self._possessions.asset_sha256 != self._item_hash:
            raise ProtocolError("unit_skin_catalog_changed", "Skin possession definitions changed after catalog creation", 503)
        counts = self._possessions.counts(state)
        allowed = set()
        for skin in self._links[card_id]:
            if skin.free or not skin.missing_item and counts.get(skin.item_id, 0) > 0:
                allowed.add(skin.display_type)
                if smartphone_images and skin.smartphone:
                    allowed.add(skin.display_type + 16)
        return frozenset(allowed)

    def status(self):
        return {"ready": self.is_ready(), "packages": len(self._packages), "cards": len(self._links),
                "associated_cards": sum(bool(skins) for skins in self._links.values()),
                "links": sum(len(skins) for skins in self._links.values()),
                "missing_unlock_items": {name: list(ids) for name, ids in self._missing.items()},
                "counted_item_count": len(self._counted_item_ids),
                "missing_counted_items": list(self._missing_counted),
                "acquisition_rule": "Distinct Aggregate skin packages unlocked by positive possession count; free skins excluded",
                "content_version": self.content_version, "resource_sha256": dict(self._hashes),
                "rule": "Original card slots; free or positive verified possession count; no item consumption",
                "missing_item_policy": "Keep only affected skin choices locked"}
