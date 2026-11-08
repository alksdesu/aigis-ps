from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from types import MappingProxyType

from .al_format import MAX_RESOURCE_BYTES, ParseLimits, decompress_all4, parse_table
from .errors import ProtocolError
from .native_numbers import INT32_MIN
from .possessions import MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES
from .public_tables import BytesDigest
from .system_literals import command_id
from .trade_catalog import _date, _integer, _quest_ids, _text


FIELDS = MappingProxyType({
    "ID": 1, "TradeCount": 1, "Limitation": 1, "CardID": 1, "DiscloseEmblemIDs": 32,
    "DiscloseGeneralItemID": 32, "GeneralItemID": 32, "RequiredGeneralItemID": 32,
    "RequiredValue": 1, "StartDateTime": 9, "EndDateTime": 9, "GeneralTextID": 32,
    "ConfirmationTextID": 32, "CompleteTextID": 32, "CategoryType": 1, "SingleBuyOnly": 1,
})
ITEM_FIELDS = {"ItemID": 32, "ID": 1, "Stack": 1, "EffectType": 1}
UI_FIELDS = {"MissionID": 1, "CrystalSpriteNode": 32, "CrystalNameNode": 32, "CrystalValueNode": 32}
LIMITS = ParseLimits(max_input_bytes=16 * 1024 * 1024, max_decompressed_bytes=16 * 1024 * 1024,
                    max_rows=65535, max_fields=128, max_cells=2_000_000)
MAX_MASTER_BYTES = MAX_RESOURCE_BYTES


@dataclass(frozen=True)
class TowerTradeProduct:
    shop_id: int
    card_id: int
    item_id: str
    currency_item_id: str
    tiers: tuple[tuple[int, int], ...]
    limitation: int
    start_at: datetime | None
    end_at: datetime | None
    disclose_emblem_ids: tuple[int, ...]
    disclose_item_id: str
    category_type: int
    single_buy_only: bool
    item_effect_type: int | None
    item_stack: int | None
    unsupported_reason: str | None
    source_row: int
    general_text_id: str
    confirmation_text_id: str
    complete_text_id: str

    @property
    def kind(self):
        return "unit" if self.card_id else "item"


class TowerTradeCatalog:
    def __init__(self, asset_reader, possessions, cards, *, emblems=None):
        self.possessions, self.cards, self.emblems = possessions, cards, emblems
        self._card_digest = BytesDigest()
        try:
            if not callable(asset_reader) or not possessions.is_ready():
                raise ValueError("Tower exchange requires verified resource and inventory providers")
            self.resource_version = _text(cards.resource_version, 128)
            self._card_ids = frozenset(_integer(identifier, 1) for identifier in cards.card_ids)
            if not self._card_ids or len(self._card_ids) != len(cards.card_ids):
                raise ValueError("Tower exchange requires distinct card identities")
            raw_cards = cards.snapshot.get_xml("CardData")
            if type(raw_cards) is not bytes or not 0 < len(raw_cards) <= MAX_MASTER_BYTES:
                raise ValueError("Tower exchange requires a bounded immutable card master")
            self._master_hash = self._card_digest(raw_cards)
            resources, hashes = {}, {}

            def table(name, expected, *, exact=False):
                raw = asset_reader(name)
                if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
                    raise ValueError("Tower resource must be bounded immutable bytes")
                decoded = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
                result = parse_table(decoded, LIMITS)
                fields = {field["name"]: field["type_code"] for field in result["fields"]}
                if (len(fields) != len(result["fields"]) or any(fields.get(key) != kind for key, kind in expected.items())
                        or exact and fields != expected):
                    raise ValueError("Tower resource fields differ from the verified schema")
                resources[name], hashes[name] = raw, hashlib.sha256(raw).hexdigest()
                return result["rows"], hashlib.sha256(decoded).hexdigest()

            item_rows, item_hash = table("ItemConfigTable.atb", ITEM_FIELDS)
            if item_hash != possessions.asset_sha256:
                raise ValueError("Tower and possession definitions must share the same source")
            items, identifiers = {}, set()
            for row in item_rows:
                name = _text(row["ItemID"], STORED_ITEM_NAME_BYTES)
                identifier = _integer(row["ID"], INT32_MIN)
                stack, effect = _integer(row["Stack"]), _integer(row["EffectType"])
                if (not name or name in items or identifier in identifiers or identifier != command_id(name)
                        or possessions.resolve_hash(identifier) != name or possessions.stack(name) != stack):
                    raise ValueError("Tower item identities or stack limits are inconsistent")
                items[name] = (stack, effect)
                identifiers.add(identifier)
            rows, _ = table("TowerTradeConfig.atb", FIELDS, exact=True)
            ui_rows, _ = table("TowerTradeUIConfig.atb", UI_FIELDS, exact=True)
            missions, variants = [], {}
            for row in ui_rows:
                mission = _integer(row["MissionID"], 1)
                if mission in missions or mission > 999999:
                    raise ValueError("Tower UI missions must be distinct six-digit identifiers")
                missions.append(mission)
                for name in UI_FIELDS.keys() - {"MissionID"}:
                    if not _text(row[name], 256):
                        raise ValueError("Tower UI nodes must be nonempty")
                variant, _ = table(f"TowerTradeConfig{mission:06d}.atb", FIELDS, exact=True)
                if variant and variant != rows:
                    raise ValueError("A tower-specific catalog differs from the verified shared catalog")
                variants[mission] = len(variant)
            if not rows or not missions:
                raise ValueError("Tower exchange requires products and UI categories")
            currencies = tuple(f"tower_crystal_{mission:06d}" for mission in missions)
            if any(name not in items or items[name][0] <= 0 or items[name][1] != 6 for name in currencies):
                raise ValueError("Tower currencies require verified positive-stack trade items")
            groups = []
            for index, row in enumerate(rows):
                for name, kind in FIELDS.items():
                    if kind == 1:
                        _integer(row[name])
                    elif kind == 32:
                        _text(row[name], 4096)
                    else:
                        _date(row[name])
                if row["SingleBuyOnly"] not in (0, 1):
                    raise ValueError("Tower single-purchase metadata must be boolean")
                tier = (_integer(row["TradeCount"], 1), _integer(row["RequiredValue"], 1))
                if row["ID"]:
                    if tier[0] != 1:
                        raise ValueError("Tower product tiers must begin at the first purchase")
                    groups.append((index, row, [tier]))
                else:
                    if (not groups or tier[0] <= groups[-1][2][-1][0] or row["CardID"] or row["GeneralItemID"]
                            or row["RequiredGeneralItemID"] or row["DiscloseEmblemIDs"] or row["DiscloseGeneralItemID"]
                            or row["Limitation"] or any(row["StartDateTime"]) or any(row["EndDateTime"])):
                        raise ValueError("Tower price continuation has no unambiguous owning product")
                    groups[-1][2].append(tier)
            self._emblem_version = None if emblems is None else emblems.content_version
            emblem_ids = frozenset() if emblems is None else frozenset(title.index for title in emblems.titles)
            products = {}
            for index, row, tiers in groups:
                identifier, card_id, item_id = row["ID"], row["CardID"], row["GeneralItemID"]
                if identifier in products or bool(card_id) == bool(item_id):
                    raise ValueError("Tower product identity or reward is ambiguous")
                if card_id and card_id not in self._card_ids or item_id and item_id not in items:
                    raise ValueError("Tower reward is missing its canonical definition")
                currency, category = row["RequiredGeneralItemID"], row["CategoryType"]
                if not 1 <= category <= len(currencies) or currency != currencies[category - 1]:
                    raise ValueError("Tower category and currency definitions disagree")
                disclose = _quest_ids(row["DiscloseEmblemIDs"])
                if emblems is not None and set(disclose) - emblem_ids:
                    raise ValueError("Tower disclosure references an unknown title")
                disclose_item = row["DiscloseGeneralItemID"]
                if disclose_item and disclose_item not in items:
                    raise ValueError("Tower disclosure references an unknown item")
                start, end = _date(row["StartDateTime"]), _date(row["EndDateTime"])
                if start is not None and end is not None and start >= end:
                    raise ValueError("Tower time window is empty or inverted")
                stack, effect = items[item_id] if item_id else (None, None)
                unsupported = None
                if effect == 2:
                    unsupported = "summon_pool_unverified"
                elif item_id and (stack <= 0 or effect not in (4, 10)):
                    unsupported = "item_delivery_unverified"
                elif len(item_id.encode("utf-8")) > MAX_ITEM_NAME_BYTES:
                    # Credit may open a new possession row, and new rows must fit the native cell.
                    unsupported = "item_name_exceeds_native_cell"
                elif effect == 10 and (emblems is None or item_id not in emblems.item_ids):
                    unsupported = "emblem_catalog_unavailable"
                products[identifier] = TowerTradeProduct(identifier, card_id, item_id, currency, tuple(tiers),
                    row["Limitation"], start, end, disclose, disclose_item, category, bool(row["SingleBuyOnly"]),
                    effect, stack, unsupported, index, row["GeneralTextID"], row["ConfirmationTextID"], row["CompleteTextID"])
            self.products = tuple(products.values())
            self.by_id, self.currencies = MappingProxyType(products), currencies
            self.source_hashes, self._resources = MappingProxyType(hashes), MappingProxyType(resources)
            self._variants = MappingProxyType(variants)
            self._items = MappingProxyType(items)
            self._item_hash = item_hash
            self.content_version = hashlib.sha256(json.dumps({"schema": 1, "resources": hashes,
                "master": self._master_hash, "resource_version": self.resource_version,
                "emblems": self._emblem_version, "unsupported": {str(key): product.unsupported_reason
                    for key, product in products.items() if product.unsupported_reason}}, sort_keys=True).encode()).hexdigest()
            self._snapshot = tuple(getattr(self, name) for name in self._snapshot_names())
            self._ready()
        except (AttributeError, KeyError, IndexError, TypeError, ValueError, OSError, OverflowError, ProtocolError) as error:
            raise ProtocolError("tower_trade_catalog_invalid", "Tower exchange resources differ from the verified contract", 503) from error

    @staticmethod
    def _snapshot_names():
        return ("possessions", "cards", "emblems", "products", "by_id", "currencies", "source_hashes", "_resources",
                "_variants", "_items", "resource_version", "content_version")

    def _ready(self):
        try:
            if (any(getattr(self, name) is not value for name, value in zip(self._snapshot_names(), self._snapshot))
                    or not self.possessions.is_ready()
                    or self.possessions.asset_sha256 != self._item_hash
                    or self.cards.resource_version != self.resource_version
                    or frozenset(self.cards.card_ids) != self._card_ids
                    or self._card_digest(self.cards.snapshot.get_xml("CardData")) != self._master_hash
                    or any(self.possessions.stack(name) != stack for name, (stack, _) in self._items.items())):
                raise ValueError("Tower exchange definitions changed")
            if self.emblems is not None and (not self.emblems.is_ready()
                    or self.emblems.content_version != self._emblem_version
                    or self.emblems.item_resource_sha256 != self._item_hash):
                raise ValueError("Tower title definitions changed")
        except (AttributeError, KeyError, TypeError, ValueError, ProtocolError) as error:
            raise ProtocolError("tower_trade_catalog_changed", "Tower exchange definitions changed after setup", 503) from error

    def product(self, shop_id):
        self._ready()
        if type(shop_id) is not int or shop_id not in self.by_id:
            raise ProtocolError("tower_trade_product_unknown", "Tower product is absent from the current catalog", 404)
        return self.by_id[shop_id]

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "products": len(self.products),
            "kinds": dict(Counter(product.kind for product in self.products)), "currencies": list(self.currencies),
            "unsupported": {product.shop_id: product.unsupported_reason for product in self.products if product.unsupported_reason},
            "variants": dict(self._variants), "source_hashes": dict(self.source_hashes),
            "content_version": self.content_version, "native_tested": False}
