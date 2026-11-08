from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import hashlib
import json
import re
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .errors import ProtocolError, caught
from .native_numbers import INT32_MIN, int32
from .player_state import (EMPTY_SQUAD_SLOT, LOCK_FLAG, MAX_PLAYER_UNITS, MAX_SQUAD_ROWS, PRINCE_CARD_IDS,
                           barracks_index)
from .public_tables import MASTER_LIMITS, BytesDigest
from .system_literals import command_id, decode_command, rating_command_id
from .trade_catalog import TradeProduct


PAYMENT_ITEM = "trade_item_001000"
PAYMENT_HASH = command_id(PAYMENT_ITEM)
REGISTRATION = 106
LIMITS = ParseLimits(max_input_bytes=64 * 1024 * 1024, max_decompressed_bytes=64 * 1024 * 1024,
                     max_rows=65535, max_fields=16)
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _integer(value, minimum=0):
    if not int32(value) or value < minimum:
        raise ValueError("Expected a bounded native integer")
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _state_error(message):
    raise ProtocolError("silver_trade_state_invalid", message, 503)


class SilverTradeCatalog:
    def __init__(self, base_catalog, cards, asset_reader):
        self.base, self.cards = base_catalog, cards
        self._card_digest = BytesDigest()
        try:
            if not callable(asset_reader) or not isinstance(base_catalog.content_version, str) or not _HASH.fullmatch(base_catalog.content_version):
                raise ValueError("Silver trade requires a verified base catalog and resource reader")
            self._base_version = base_catalog.content_version
            self._resource_version = cards.resource_version
            if base_catalog.resource_version != self._resource_version:
                raise ValueError("Silver trade resources use different versions")
            self._master_identity, rare = self._card_data()
            self._rarities = MappingProxyType(rare)
            self._card_signature = _digest(rare)
            hashes = {}

            def table(name, schema):
                raw = asset_reader(name)
                if not isinstance(raw, bytes) or not 0 < len(raw) <= LIMITS.max_input_bytes:
                    raise ValueError("Silver trade resources must be bounded bytes")
                hashes[name] = hashlib.sha256(raw).hexdigest()
                data = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
                parsed = parse_table(data, LIMITS)
                fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
                if fields != schema or len(fields) != len(parsed["fields"]):
                    raise ValueError("Silver trade resource fields differ from the native contract")
                return parsed["rows"]

            groups, current = {}, None
            for row in table("ShopCatalogCofig.atb", {"ID_Config": 1, "ID_Class": 1}):
                identifier = _integer(row["ID_Config"])
                class_id = _integer(row["ID_Class"])
                if identifier:
                    current = groups.setdefault(identifier, [])
                if current is not None:
                    current.append(_integer(class_id, 1))
            self._groups = MappingProxyType({key: tuple(value) for key, value in groups.items()})
            commands = {}
            for row in table("OnCommand.atb", {"_ID": 1, "_Cmnd": 32}):
                identifier = _integer(row["_ID"], INT32_MIN)
                if not isinstance(row["_Cmnd"], str):
                    raise ValueError("Rating commands require encoded text")
                commands.setdefault(identifier, []).append(row["_Cmnd"])
            owners, restricted = {}, set()
            for card_id in rare:
                identifier = rating_command_id(card_id)
                if identifier in owners:
                    raise ValueError("Rating command identifiers collide")
                owners[identifier] = card_id
                entries = commands.get(identifier, ())
                if len(entries) > 1 or (entries and decode_command(entries[0]).strip()):
                    restricted.add(card_id)
            self._rating_restricted = frozenset(restricted)
            products, sources = {}, {}
            for product in base_catalog.products:
                if not isinstance(product, TradeProduct):
                    raise ValueError("Base catalog returned an invalid product")
                if product.trade_type != 3 or product.shop_type != 0 or product.product_type != 1:
                    continue
                if product.shop_id in products:
                    raise ValueError("Silver trade product identifiers are duplicated")
                if product.card_id not in rare or not self._groups.get(product.type_catalog):
                    raise ValueError("Silver trade product references missing cards or class conditions")
                sources[product.shop_id] = product
                products[product.shop_id] = replace(product, currency_kind="possession",
                    currency_item_id=PAYMENT_ITEM, registration=REGISTRATION)
            self._sources = MappingProxyType(sources)
            self._products = MappingProxyType(products)
            self._hashes = MappingProxyType(hashes)
            self._check_payment()
            self.content_version = _digest({"policy": 1, "base": self._base_version,
                "master": self._master_identity, "cards": self._card_signature,
                "resources": hashes, "resource_version": self._resource_version,
                "payment": PAYMENT_ITEM, "registration": REGISTRATION, "quantity": 1})
            self._check_dependencies()
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError) as error:
            raise ProtocolError("silver_trade_catalog_invalid", "Silver trade resources are invalid or inconsistent", 503) from error

    def _card_data(self):
        raw = self.cards.snapshot.get_xml("CardData")
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MASTER_LIMITS.max_bytes:
            raise ValueError("CardData must be bounded immutable bytes")
        identifiers = self.cards.card_ids
        if not isinstance(identifiers, (list, tuple)) or not 1 <= len(identifiers) <= 65535:
            raise ValueError("Card identifiers must be a bounded sequence")
        rare = {}
        for identifier in identifiers:
            _integer(identifier, 1)
            row = self.cards.card(identifier)
            if not isinstance(row, Mapping) or _integer(row.get("CardID"), 1) != identifier or identifier in rare:
                raise ValueError("Card identifiers do not match their resource rows")
            rare[identifier] = _integer(row.get("Rare"))
        identity = (self.cards.snapshot.identifier, self.cards.snapshot.client_hash, self._card_digest(raw))
        if any(not isinstance(value, str) or not value for value in identity):
            raise ValueError("CardData snapshot identity is unavailable")
        return identity, rare

    def _check_payment(self):
        possessions = self.base.possessions
        if (not possessions.is_ready() or possessions.resolve_hash(PAYMENT_HASH) != PAYMENT_ITEM
                or _integer(possessions.stack(PAYMENT_ITEM), 1) <= 0):
            raise ValueError("Silver trade payment does not match the verified item definition")

    def _check_dependencies(self):
        try:
            if (self.base.content_version != self._base_version or not self.base.is_ready()
                    or self.base.resource_version != self._resource_version or self.cards.resource_version != self._resource_version):
                raise ValueError("Base trade resources changed")
            identity, rare = self._card_data()
            if identity != self._master_identity or _digest(rare) != self._card_signature:
                raise ValueError("CardData changed after silver trade setup")
            if any(self.base.product(key) != value for key, value in self._sources.items()):
                raise ValueError("Base silver trade products changed")
            self._check_payment()
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError) as error:
            raise ProtocolError("silver_trade_content_changed", "Silver trade dependencies changed or are unavailable", 503) from error

    @property
    def products(self):
        self._check_dependencies()
        return tuple(self._products.values())

    def product(self, shop_id):
        self._check_dependencies()
        self.base.product(shop_id)
        if shop_id not in self._products:
            raise ProtocolError("silver_trade_product_unsupported", "This product is not an ordinary silver-unit exchange", 501)
        product = self._products[shop_id]
        if product.adding_count != 1:
            raise ProtocolError("silver_trade_product_unsupported", "Silver exchanges require a verified single-unit reward", 501)
        return product

    def quote(self, state, shop_id, quantity, exchange_count, now, registration=REGISTRATION):
        if not int32(quantity) or quantity != 1:
            raise ProtocolError("silver_trade_quantity_invalid", "Silver exchanges require exactly one purchase")
        if not int32(registration) or registration != REGISTRATION:
            raise ProtocolError("trade_registration_mismatch", "Silver exchanges require registration 106")
        product = self.product(shop_id)
        return self.base._quote_product(state, product, quantity, exchange_count, now, registration)

    def material(self, player, product, get_index, unique_id, *, state):
        if not isinstance(product, TradeProduct) or product != self.product(product.shop_id):
            raise ProtocolError("silver_trade_product_invalid", "Material selection requires the verified silver trade product")
        if not int32(get_index) or get_index < 0 or not int32(unique_id) or unique_id <= 0:
            raise ProtocolError("silver_trade_identity_invalid", "Silver trade requires a valid GetIndex and UniqueID")
        if not isinstance(state, dict) or state.get("player_state") != player:
            _state_error("Material selection requires the matching complete state")
        overrides = state.get("unit_dynamic_overrides")
        if overrides is not None and not isinstance(overrides, (dict, list)):
            _state_error("Dynamic unit overrides have an invalid shape")
        if overrides:
            raise ProtocolError("silver_trade_overrides_unsupported", "Dynamic unit overrides require a separate verified material rule", 501)
        if (not isinstance(player, dict) or type(player.get("schema_version")) is not int or player["schema_version"] != 1
                or not isinstance(player.get("units"), list) or not 1 <= len(player["units"]) <= MAX_PLAYER_UNITS
                or not isinstance(player.get("squads"), list) or len(player["squads"]) > MAX_SQUAD_ROWS):
            _state_error("Silver trade requires a bounded verified player roster and squads")
        indices, identities, selected = set(), set(), None
        for unit in player["units"]:
            if (not isinstance(unit, dict) or any(not int32(unit.get(name)) for name in
                    ("CardID", "ClassID", "GetIndex", "UniqueID", "GeneralPurposeFlags"))
                    or unit["CardID"] not in self._rarities or unit["ClassID"] <= 0 or unit["GetIndex"] < 0 or unit["UniqueID"] <= 0
                    or unit["GetIndex"] in indices or unit["UniqueID"] in identities):
                _state_error("Material roster identities or native fields are invalid")
            indices.add(unit["GetIndex"])
            identities.add(unit["UniqueID"])
            if unit["GetIndex"] == get_index:
                selected = unit
        if selected is None or selected["UniqueID"] != unique_id:
            raise ProtocolError("silver_trade_material_missing", "Silver trade identity does not match an owned unit", 409)
        organized = set()
        for row in player["squads"]:
            if not isinstance(row, dict) or not int32(row.get("UnitIndex")) or row["UnitIndex"] < EMPTY_SQUAD_SLOT:
                _state_error("Silver trade squad members are invalid")
            if row["UnitIndex"] != EMPTY_SQUAD_SLOT and row["UnitIndex"] not in indices:
                _state_error("A squad member does not reference the owned roster")
            organized.add(row["UnitIndex"])
        if barracks_index(selected):
            raise ProtocolError("silver_trade_storage_unsupported", "Silver exchange materials must be in the first barracks", 501)
        if selected["CardID"] in PRINCE_CARD_IDS or selected["GeneralPurposeFlags"] & LOCK_FLAG or get_index in organized:
            raise ProtocolError("silver_trade_material_protected", "This unit is protected from silver exchange", 409)
        if selected["CardID"] in self._rating_restricted:
            raise ProtocolError("silver_trade_rating_unsupported", "This unit has an unverified rating restriction", 501)
        if (self._rarities[selected["CardID"]] != 2
                or not any(selected["ClassID"] // 100 == class_id // 100 for class_id in self._groups[product.type_catalog])):
            raise ProtocolError("silver_trade_material_ineligible", "The material does not match the required silver class family", 409)
        return selected

    def is_ready(self) -> bool:
        return caught(self._check_dependencies) is None

    def status(self):
        result = {"ready": True, "registration": REGISTRATION, "products": len(self._products),
                  "payment_item": PAYMENT_ITEM, "content_version": self.content_version,
                  "base_content_version": self._base_version, "resource_sha256": dict(self._hashes),
                  "rating_conditions_unavailable": sorted(self._rating_restricted),
                  "material_policy": "One unprotected silver instance in the first barracks; current class family",
                  "quantity": 1, "native_tested": False}
        try:
            self._check_dependencies()
        except ProtocolError as error:
            result.update(ready=False, error=error.code)
        return result
