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
from .business_calendar import NATIVE_SECONDS_TEXT
from .energy_clock import JST, instant
from .errors import ProtocolError, caught
from .native_clock import native_clock, parse_native_date
from .native_numbers import int32
from .possessions import ITEM_RESOURCE_LIMIT, MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES
from .protocol import NATIVE_CELL_BYTES
from .system_literals import command_id


LIMITS = ParseLimits(max_input_bytes=ITEM_RESOURCE_LIMIT, max_decompressed_bytes=ITEM_RESOURCE_LIMIT)
SHOP_FIELDS = {name: 1 for name in ("RangeKey", "ConfigId", "DisplayId", "PurchaseFlag", "Price",
    "SkinId", "ButtonImageId", "ItemId", "SkinCardId")}
SHOP_FIELDS.update(OpenDate=32, CloseDate=32)
PACKAGE_FIELDS = {"ConfigId": 1, "FreeCondition": 1, "ConditionItemId": 32, "Aggregate": 1}
ITEM_FIELDS = {"ItemID": 32, "ID": 1, "EffectType": 1, "Category": 1, "Stack": 1,
    "ReceiveAction": 1, "EventID": 32, "ReceiveConfirmation": 1, "CanAllGet": 1,
    "NoAutoDelete": 1, "InvisibleContaint": 1, "CanPresent": 1}
WINDOW_RULE = "inclusive_native_clock"
# Receipts committed while sale windows compared JST instants keep this rule in their digest.
LEGACY_WINDOW_RULE = "inclusive_jst"
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _text(value, maximum, *, empty=False):
    if (not isinstance(value, str) or not empty and not value
            or len(value.encode("utf-8")) > maximum):
        raise ValueError("Expected bounded resource text")
    dumps(Container(children=(Column("Value", "S", (value,)),)))
    return value


def _date(value):
    _text(value, NATIVE_CELL_BYTES, empty=True)
    if value == "":
        return None
    match = NATIVE_SECONDS_TEXT.fullmatch(value)
    if match is None:
        raise ValueError("Sale dates require a complete native calendar timestamp")
    year, _, month, day, hour, minute, second = match.groups()
    return datetime(*(int(part) for part in (year, month, day, hour, minute, second)), tzinfo=JST)


def _window(opening, closing):
    start, end = _date(opening), _date(closing)
    if start is not None and end is not None and start > end:
        raise ValueError("Sale window is inverted")
    # 63A990 compares the client clock with AACF90's reading of the same text.
    return parse_native_date(opening), parse_native_date(closing)


def _version(content, window):
    return hashlib.sha256(json.dumps({**content, "window": window}, sort_keys=True).encode()).hexdigest()


def _item_matches(item, expected):
    return all(item[name] == value and type(item[name]) is type(value) for name, value in expected.items())


@dataclass(frozen=True)
class SkinProduct:
    shop_id: int
    range_key: int
    config_id: int
    display_id: int
    purchase_flag: int
    flag_name: str
    price: int
    start_at: datetime | None
    end_at: datetime | None
    skin_id: int
    item_id: str
    skin_card_id: int
    supported: bool


class SkinShopCatalog:
    def __init__(self, possessions, skins, asset_reader):
        self._possessions, self._skins = possessions, skins
        try:
            if not callable(asset_reader):
                raise ValueError("Skin shop requires a resource reader")
            hashes, decoded_hashes = {}, {}

            def table(name, expected):
                raw = asset_reader(name)
                if not isinstance(raw, bytes) or not 0 < len(raw) <= ITEM_RESOURCE_LIMIT:
                    raise ValueError("Skin shop resources must be bounded bytes")
                data = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
                parsed = parse_table(data, LIMITS)
                fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
                if (len(fields) != len(parsed["fields"])
                        or any(fields.get(field) != kind for field, kind in expected.items())):
                    raise ValueError("Skin shop resource fields differ from the native contract")
                hashes[name] = hashlib.sha256(raw).hexdigest()
                decoded_hashes[name] = hashlib.sha256(data).hexdigest()
                return parsed["rows"]

            items, item_names = {}, set()
            for row in table("ItemConfigTable.atb", ITEM_FIELDS):
                name, key = _text(row["ItemID"], STORED_ITEM_NAME_BYTES), row["ID"]
                if not int32(key) or key != command_id(name) or key in items or name in item_names:
                    raise ValueError("Item identities are invalid or duplicated")
                items[key] = row
                item_names.add(name)
            packages = {}
            for row in table("SkinPackage.atb", PACKAGE_FIELDS):
                key = row["ConfigId"]
                if (not int32(key) or key == 0 or key in packages or not int32(row["FreeCondition"])
                        or not int32(row["Aggregate"])):
                    raise ValueError("Skin package identities are invalid or duplicated")
                _text(row["ConditionItemId"], STORED_ITEM_NAME_BYTES, empty=True)
                packages[key] = row
            products, seen = [], {name: set() for name in ("RangeKey", "ConfigId", "PurchaseFlag")}
            for row in table("SkinShop.atb", SHOP_FIELDS):
                if any(not int32(row[name]) for name, kind in SHOP_FIELDS.items() if kind == 1):
                    raise ValueError("Shop values require signed native integers")
                if (row["RangeKey"] <= 0 or row["ConfigId"] == 0 or row["PurchaseFlag"] == 0
                        or row["ButtonImageId"] <= 0 or row["SkinCardId"] <= 0 or row["Price"] < 0):
                    raise ValueError("Shop identities or price are invalid")
                for name, values in seen.items():
                    if row[name] in values:
                        raise ValueError("Repeated shop groups require an unsupported multi-item contract")
                    values.add(row[name])
                number = row["ButtonImageId"]
                flag_name = f"PurchasedSkin_{number:04d}"
                if (row["PurchaseFlag"] != command_id(flag_name) or row["ConfigId"] != command_id(f"Skin{number:04d}")
                        or row["SkinId"] != command_id(f"skin_{number:04d}")):
                    raise ValueError("Skin shop names differ from the verified three-way hash pattern")
                package = packages.get(row["SkinId"])
                if package is None or package["FreeCondition"] != 0 or package["Aggregate"] != 1:
                    raise ValueError("Shop item must unlock a verified counted skin package")
                item_name = _text(package["ConditionItemId"], STORED_ITEM_NAME_BYTES)
                if command_id(item_name) != row["ItemId"]:
                    raise ValueError("Shop item differs from the skin unlock item")
                item = items.get(row["ItemId"])
                grantable = item is not None
                if item is not None:
                    expected = {"EffectType": 13, "Category": 1165413830, "Stack": 1,
                        "ReceiveAction": 0, "EventID": "", "ReceiveConfirmation": 0,
                        "CanAllGet": 0, "NoAutoDelete": 1, "InvisibleContaint": 0}
                    zero_stack = {**expected, "Stack": 0, "NoAutoDelete": 0, "CanPresent": 0}
                    grantable = _item_matches(item, expected)
                    if item["ItemID"] != item_name or not (grantable or _item_matches(item, zero_stack)):
                        raise ValueError("Skin item behavior differs from the supported ownership contract")
                # A purchase opens a new possession row, so a longer stored name keeps the product unsupported.
                supported = grantable and len(item_name.encode("utf-8")) <= MAX_ITEM_NAME_BYTES
                links = skins._links.get(row["SkinCardId"])
                if links is None or not any(link.item_id == item_name and not link.free for link in links):
                    raise ValueError("Shop display card has no matching original skin slot")
                start, end = _window(row["OpenDate"], row["CloseDate"])
                products.append(SkinProduct(row["ConfigId"], row["RangeKey"], row["ConfigId"], row["DisplayId"],
                    row["PurchaseFlag"], flag_name, row["Price"], start, end, row["SkinId"], item_name,
                    row["SkinCardId"], supported))
            if not products:
                raise ValueError("Skin shop is empty")
            self._products = tuple(products)
            self._by_id = MappingProxyType({product.shop_id: product for product in products})
            self._hashes = MappingProxyType(hashes)
            self._decoded_hashes = MappingProxyType(decoded_hashes)
            self._skin_version = skins.content_version
            if not isinstance(self._skin_version, str) or _HASH.fullmatch(self._skin_version) is None:
                raise ValueError("Skin definitions require a fixed fingerprint")
            self._ready()
            version = {"rule": "skin-shop-v1", "resources": hashes, "skins": self._skin_version,
                "flag_names": "inferred_pattern_verified_all_rows",
                "wallet_policy": "free_then_paid", "missing_item": "unsupported_product"}
            self.content_version = _version(version, WINDOW_RULE)
            self._replayable_versions = frozenset((self.content_version, _version(version, LEGACY_WINDOW_RULE)))
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError) as error:
            raise ProtocolError("skin_shop_catalog_invalid", "Skin shop resources differ from the verified contract", 503) from error

    @property
    def products(self) -> tuple[SkinProduct, ...]:
        return self._products

    @property
    def by_id(self):
        return self._by_id

    @property
    def resource_sha256(self) -> Mapping[str, str]:
        return self._hashes

    def replayable_content_versions(self) -> frozenset[str]:
        return self._replayable_versions

    def _ready(self):
        try:
            skin_hashes = self._skins.resource_sha256
            if (not self._possessions.is_ready()
                    or self._possessions.asset_sha256 != self._decoded_hashes["ItemConfigTable.atb"]
                    or not self._skins.is_ready() or self._skins.content_version != self._skin_version
                    or skin_hashes["SkinPackage.atb"] != self._hashes["SkinPackage.atb"]
                    or skin_hashes["ItemConfigTable.atb"] != self._decoded_hashes["ItemConfigTable.atb"]):
                raise ValueError("Skin shop dependencies changed")
            for product in self._products:
                if product.supported:
                    stack = self._possessions.stack(product.item_id)
                    if (type(stack) is not int or stack != 1
                            or product.item_id not in self._skins.counted_item_ids
                            or self._possessions.resolve_hash(command_id(product.item_id)) != product.item_id):
                        raise ValueError("Skin ownership definition changed")
        except (AttributeError, ValueError, KeyError, TypeError) as error:
            raise ProtocolError("skin_shop_catalog_changed", "Skin shop dependencies must remain identical", 503) from error

    def is_ready(self) -> bool:
        return caught(self._ready) is None

    def get(self, shop_id: int) -> SkinProduct:
        self._ready()
        if not int32(shop_id) or shop_id == 0 or shop_id not in self._by_id:
            raise ProtocolError("skin_shop_unknown", "ShopId is not a verified skin product")
        return self._by_id[shop_id]

    def is_open(self, product: SkinProduct, now: datetime) -> bool:
        self._ready()
        if not isinstance(product, SkinProduct) or self._by_id.get(product.shop_id) != product:
            raise ProtocolError("skin_shop_unknown", "Sale checks require a product from this catalog")
        try:
            current = native_clock(instant(now))
        except (ValueError, TypeError, OverflowError) as error:
            raise ProtocolError("skin_shop_clock_invalid", "Skin shop clock is invalid") from error
        return ((product.start_at is None or product.start_at <= current)
                and (product.end_at is None or current <= product.end_at))

    def status(self) -> dict:
        result = {"ready": True, "product_count": len(self._products),
            "supported_count": sum(product.supported for product in self._products),
            "unsupported_items": sorted({product.item_id for product in self._products if not product.supported}),
            "resource_sha256": dict(self._hashes), "content_version": self.content_version,
            "flag_name_source": "inferred_pattern_verified_all_rows", "window": WINDOW_RULE,
            "wallet_policy": "local_free_then_paid",
            "name_policy": (f"Purchases need item names within the {MAX_ITEM_NAME_BYTES}-byte native cell; stored "
                            f"names up to {STORED_ITEM_NAME_BYTES} bytes stay listed as unsupported"),
            "native_purchase_tested": False}
        try:
            self._ready()
        except ProtocolError as error:
            result.update(ready=False, error=error.code)
        return result
