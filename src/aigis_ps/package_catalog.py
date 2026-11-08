from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_archive, parse_table
from .alxml import Column, Container, dumps
from .business_calendar import NATIVE_DATETIME
from .energy_clock import JST, instant
from .errors import ProtocolError
from .native_numbers import int32
from .package_state import package_identity
from .package_text_overlay import (CAPTION_KEY, DESCRIPTION_KEY, IMPLANT_RESOURCE,
    SYSTEM_TEXT_RESOURCE, build_package_text_overlay, local_package_id)
from .protocol import NATIVE_CELL_BYTES
from .system_literals import command_id


LIMITS = ParseLimits(max_input_bytes=32 * 1024 * 1024, max_decompressed_bytes=32 * 1024 * 1024,
                     max_rows=65535, max_fields=128)
ITEM_RESOURCE = "ItemConfigTable.atb"
PASS_RESOURCE = "EventPassConfig.atb"
MENU_RESOURCE = "CfgMenuList.atb"
TEXTURE_RESOURCE = "CfgTextureList.atb"
SHOP_RESOURCE = "ShopMenu.aar"
COMMAND_RESOURCE = "OnCommandMenuShop.atb"
BUTTON_NAME = "Shop_MnObjCommonPackage"
BUTTON_MEMBER = "MnObjCommonPackage.aod"
SCENE = 2
SKU_PREFIX = "local_"
# PSC.SkuID is a native row cell, so a longer SKU comes back truncated in 50 and never matches its offer.
SKU_TOO_LONG = "sku_exceeds_native_cell"
# Native getters dereference absent slots; WidgetID extends both row vectors through slot 34.
PSC_FIELDS = (("RecordID", "I"), ("PackageID", "I"), ("PackageDisplayScene", "I"),
    ("SkuID", "S"), ("Price", "I"), ("PackageRepeatCount", "I"),
    ("PackageCaption", "I"), ("PackageDescription", "I"), ("PackageIconPath", "S"),
    ("PackageExpression", "S"), ("BtnPackObjectID", "I"), ("PackageFlag", "I"),
    ("OpenDays", "S"), ("CloseDays", "S"), ("DisplayIndex", "I"),
    ("PackageItemType", "I"), ("PackageItemNum", "I"), ("ItemID", "S"),
    ("PackagePayCrystalNum", "I"), ("PackageFreeCrystalNum", "I"), ("PackageCardID", "I"),
    ("PackageUnitLevel", "I"), ("PackageUnitSkillLevel", "I"), ("PackageUnitCost", "I"),
    ("PresentPackageID", "I"), ("PresentIconTextureID", "I"), ("PackageSummonConfigID", "I"),
    ("LoginBonusID", "I"), ("AppealIconHome", "I"), ("AppealIconShop", "I"),
    ("PackageTextuerID", "S"), ("PackageUnitClassChange", "I"), ("WidgetID", "I"))
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _version(content, passes):
    return _hash(json.dumps({**content, "passes": passes}, sort_keys=True).encode("utf-8"))


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _integer(value, minimum=0):
    return int32(value) and value >= minimum


@dataclass(frozen=True)
class PackageOffer:
    normal_pass_id: str
    item_id: str
    package_id: int
    flag_id: int
    sku: str
    start_at: datetime
    end_at: datetime
    caption_key: int | None
    description_key: int | None
    icon_path: str
    definition_hash: str


class PackageCatalog:
    def __init__(self, event_pass_catalog, possessions, asset_reader, *, enabled=False):
        self.event_pass_catalog, self.possessions, self.enabled = event_pass_catalog, possessions, enabled
        try:
            if type(enabled) is not bool or not callable(asset_reader):
                raise ValueError("Package configuration requires a boolean and resource reader")
            self._source_catalog, self._source_possessions = event_pass_catalog, possessions
            self._enabled = enabled
            self._pass_version = event_pass_catalog.content_version
            self._item_version = possessions.asset_sha256
            if any(type(value) is not str or not _HASH.fullmatch(value)
                   for value in (self._pass_version, self._item_version)):
                raise ValueError("Packages require frozen dependency fingerprints")
            self._passes = tuple(event_pass_catalog.passes)
            if not self._passes or len(self._passes) > 256:
                raise ValueError("Packages require a bounded nonempty pass catalog")
            hashes, payloads = {}, {}

            def resource(name):
                raw = asset_reader(name)
                if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
                    raise ValueError("Package resources require bounded immutable bytes")
                payloads[name] = raw
                data = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
                hashes[name] = _hash(data)
                return data

            def table(name, expected):
                parsed = parse_table(resource(name), LIMITS)
                fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
                if len(fields) != len(parsed["fields"]) or any(fields.get(key) != value for key, value in expected.items()):
                    raise ValueError("Package resource columns differ from the verified schema")
                return parsed["rows"]

            pass_rows = table(PASS_RESOURCE, {"ID": 1, "NormalPassItemID": 32})
            first = {}
            for row in pass_rows:
                if row["NormalPassItemID"]:
                    if row["NormalPassItemID"] in first:
                        raise ValueError("Package pass identities are duplicated")
                    first[row["NormalPassItemID"]] = row["ID"]
            if (set(first) != {entry.item_id for entry in self._passes}
                    or hashes[PASS_RESOURCE] != event_pass_catalog.resource_hashes[PASS_RESOURCE]):
                raise ValueError("Package and pass resources differ")
            items = {}
            for row in table(ITEM_RESOURCE, {"ItemID": 32, "ID": 1, "EffectType": 1, "Stack": 1,
                    "Category": 1, "TextureName": 1, "CanPresent": 1, "CanAllGet": 1,
                    "ReceiveAction": 1, "ReceiveConfirmation": 1, "EventID": 32}):
                if row["ItemID"] in items or row["ID"] != command_id(row["ItemID"]):
                    raise ValueError("Item identities are inconsistent or duplicated")
                items[row["ItemID"]] = row
            if hashes[ITEM_RESOURCE] != self._item_version:
                raise ValueError("Package and inventory resources differ")
            identifiers = tuple(local_package_id(first[entry.item_id]) for entry in self._passes)
            if len(set(identifiers)) != len(identifiers):
                raise ValueError("Package identifiers are duplicated")
            self._button_id = None
            text_indices = {}
            textures = {}
            if enabled:
                overlay = build_package_text_overlay(resource(IMPLANT_RESOURCE), resource(SYSTEM_TEXT_RESOURCE), identifiers)
                if overlay.replacements:
                    raise ValueError("Local package text must be part of the served resource view")
                text_indices = dict(overlay.text_indices)
                menus = table(MENU_RESOURCE, {"_CfgID": 1, "_ResID": 1})
                buttons = [row["_ResID"] for row in menus if row["_CfgID"] == command_id(BUTTON_NAME)]
                if len(buttons) != 1 or not _integer(buttons[0], 1):
                    raise ValueError("The native package button template is missing or ambiguous")
                self._button_id = buttons[0]
                shop = resource(SHOP_RESOURCE)
                archive = parse_archive(shop, LIMITS)
                members = [member for member in archive.members if member.name == BUTTON_MEMBER]
                if len(members) != 1 or shop[members[0].offset:members[0].offset + 4] != b"ALOD":
                    raise ValueError("The native package button layout is unavailable")
                for row in table(TEXTURE_RESOURCE, {"_CfgID": 1, "_ResID": 1}):
                    textures.setdefault(row["_CfgID"], []).append(row["_ResID"])
                table(COMMAND_RESOURCE, {"_ID": 1})
            offers, offered, excluded = [], [], []
            for entry, identifier in zip(self._passes, identifiers):
                item = items.get(entry.premium_item_id)
                expected = {"EffectType": 14, "Stack": 1, "Category": -425086357, "CanPresent": 0,
                            "CanAllGet": 0, "ReceiveAction": 0, "ReceiveConfirmation": 0}
                if (not entry.premium_item_id or item is None
                        or any(type(item[key]) is not int or item[key] != value for key, value in expected.items())
                        or item["EventID"] != "" or possessions.stack(entry.premium_item_id) != 1
                        or entry.start_at.tzinfo is None or entry.end_at.tzinfo is None
                        or entry.start_at.microsecond or entry.end_at.microsecond or entry.start_at > entry.end_at):
                    raise ValueError("Premium pass acquisition semantics are not supported")
                sku = SKU_PREFIX + entry.premium_item_id
                if len(sku.encode("utf-8")) > NATIVE_CELL_BYTES:
                    excluded.append((entry.item_id, entry.premium_item_id, len(sku.encode("utf-8"))))
                    continue
                if enabled:
                    matches = textures.get(item["TextureName"], ())
                    if len(matches) != 1 or not _integer(matches[0], 1):
                        raise ValueError("Premium pass item icon is missing or ambiguous")
                offers.append(PackageOffer(entry.item_id, entry.premium_item_id, identifier, identifier,
                    sku, entry.start_at, entry.end_at,
                    text_indices.get(CAPTION_KEY), text_indices.get(DESCRIPTION_KEY), "",
                    package_identity(entry.item_id, entry.premium_item_id)))
                offered.append(entry)
            self._offers = tuple(offers)
            self._offer_passes = tuple(offered)
            self._excluded = tuple(excluded)
            self._by_sku = MappingProxyType({offer.sku: offer for offer in offers})
            self._by_normal_id = MappingProxyType({offer.normal_pass_id: offer for offer in offers})
            if len(self._by_sku) != len(offers) or len(self._by_normal_id) != len(offers):
                raise ValueError("Package item identities are duplicated")
            self._resource_hashes = MappingProxyType(hashes)
            self._resource_payloads = MappingProxyType(payloads)
            content = {"policy": "local-free-premium-pass-v1", "enabled": enabled, "resources": hashes,
                "scene": SCENE, "button": self._button_id, "offer_ids": identifiers, "price": 0, "repeat": 1,
                "item_type": 9, "psc_fields": PSC_FIELDS, "window": "status_1_start_inclusive_end_exclusive",
                "banner": "native_template_default"}
            self._content_version = _version(content, self._pass_version)
            self._legacy_window_content_version = _version(content, event_pass_catalog.legacy_window_content_version)
            self._ready()
        except (AttributeError, KeyError, TypeError, ValueError, OSError, ProtocolError) as error:
            raise ProtocolError("package_catalog_invalid", "Local package resources are unavailable or inconsistent", 503) from error

    @property
    def offers(self):
        return self._offers

    @property
    def by_sku(self):
        return self._by_sku

    @property
    def by_normal_id(self):
        return self._by_normal_id

    @property
    def content_version(self):
        return self._content_version

    @property
    def legacy_window_content_version(self):
        return self._legacy_window_content_version

    def _ready(self):
        try:
            self.event_pass_catalog._ready()
            # Reads by_id because get() would repeat the catalog check above once per pass.
            by_id = self.event_pass_catalog.by_id
            if (self.enabled is not self._enabled or self.event_pass_catalog is not self._source_catalog
                    or self.possessions is not self._source_possessions
                    or self.event_pass_catalog.content_version != self._pass_version
                    or tuple(self.event_pass_catalog.passes) != self._passes
                    or any(by_id.get(entry.item_id) != entry for entry in self._passes)
                    or self.possessions.asset_sha256 != self._item_version
                    or not self.possessions.is_ready()
                    or any(self.possessions.stack(offer.item_id) != 1 for offer in self._offers)):
                raise ValueError("Package dependencies changed")
        except (AttributeError, KeyError, TypeError, ValueError, OSError, ProtocolError) as error:
            raise ProtocolError("package_catalog_changed", "Local package definitions changed after setup", 503) from error

    def get(self, sku):
        self._ready()
        if type(sku) is not str or sku not in self._by_sku:
            raise ProtocolError("package_unknown", "This package is not in the local catalog", 404)
        return self._by_sku[sku]

    def available(self, now):
        self._ready()
        if not self._enabled:
            return ()
        current = instant(now).astimezone(JST)
        return tuple(offer for offer, entry in zip(self._offers, self._offer_passes)
                     if entry.status == 1 and offer.start_at <= current < offer.end_at)

    def response(self, now):
        rows = []
        for offer in self.available(now):
            row = {name: "" if kind == "S" else 0 for name, kind in PSC_FIELDS}
            row.update({"RecordID": 1, "PackageID": offer.package_id, "PackageDisplayScene": SCENE,
                "SkuID": offer.sku, "Price": 0, "PackageRepeatCount": 1,
                "PackageCaption": offer.caption_key, "PackageDescription": offer.description_key,
                "PackageIconPath": offer.icon_path, "PackageExpression": "", "BtnPackObjectID": self._button_id,
                "PackageFlag": offer.flag_id, "OpenDays": offer.start_at.astimezone(JST).strftime(NATIVE_DATETIME),
                "CloseDays": offer.end_at.astimezone(JST).strftime(NATIVE_DATETIME),
                "DisplayIndex": offer.package_id, "PackageItemType": 9, "PackageItemNum": 1, "ItemID": offer.item_id})
            rows.append(row)
        result = Container("PSC", tuple(Column(name, kind, tuple(row[name] for row in rows)) for name, kind in PSC_FIELDS))
        dumps(Container(children=(result,)))
        return result

    def status(self):
        try:
            self._ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        return {"ready": ready, "error": error, "enabled": self._enabled, "offer_count": len(self._offers),
                "excluded_offers": [{"normal_pass_id": normal, "item_id": item, "reason": SKU_TOO_LONG,
                                     "sku_bytes": size, "limit": NATIVE_CELL_BYTES} for normal, item, size in self._excluded],
                "content_version": self.content_version, "resource_hashes": dict(self._resource_hashes),
                "policy": "Local free premium ownership, once per normal/premium identity",
                "item_type": 9, "scene": SCENE, "banner": "Native template default; item details use ItemConfig icon",
                "native_tested": False}
