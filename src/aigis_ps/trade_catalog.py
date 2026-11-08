from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import re
from types import MappingProxyType

from .al_format import MAX_RESOURCE_BYTES, ParseLimits, decompress_all4, parse_table
from .barracks_service import EXTRA_SHIFT, MAXIMUM_EXTRA_BARRACKS, PURCHASE_FIELD
from .bootstrap import MAX_QUEST_RECORDS
from .crystal_wallet import wallet_totals
from .energy_clock import JST
from .errors import ProtocolError
from .fame_state import owned_fame_cards
from .native_clock import NATIVE_EPOCH
from .native_numbers import INT32_MIN, UINT32_MASK, int32
from .player_state import MAX_PLAYER_UNITS, barracks_index
from .possessions import MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES
from .quest_modes import CLEAR_FLAG
from .system_literals import command_id


RESOURCE_LIMITS = ParseLimits(max_input_bytes=MAX_RESOURCE_BYTES, max_decompressed_bytes=MAX_RESOURCE_BYTES)
REGISTRATIONS = MappingProxyType({1: 104, 2: 105, 4: 107, 5: 109, 6: 110, 7: 111, 8: 112, 9: 113})
SHOP_RESOURCE = "Shop2ItemV2.atb"
MAX_EARLIER_RESOURCES = 16
PRODUCT_UNKNOWN = "trade_product_unknown"
# The client reads Shop2ItemV2 cells by column position, so the contract keeps the native order.
SHOP_FIELDS = MappingProxyType({
    "ShopID": 1, "ProductName": 32, "CardID": 1, "ProductType": 1, "TargetRarity": 32, "GainExp": 1,
    "Type": 1, "ConsumeItemID": 32, "Data_Restrict": 1, "StartDateTime": 9, "EndDateTime": 9,
    "ID_SystemText": 1, "ID_IconTexture": 1, "Type_Catalog": 1, "TradeCount": 1, "RequiredValue": 1,
    "ItemID": 32, "AddingCount": 1, "ShopType": 1, "ResetType": 1, "DisclosureQuestIdList": 32,
    "BuyConditionItemID": 32, "CategoryType": 1, "SingleBuyOnly": 1, "UnitBaseLevel": 1,
    "UnitCostModify": 1, "UnitEvolutionCount": 1, "UnitSkillLevel": 1, "UnitSkillEvolution": 1,
    "UnitInitTrust": 1,
})
_HASH = re.compile(r"[a-f0-9]{64}\Z")


@dataclass(frozen=True)
class TradeProduct:
    shop_id: int
    product_type: int
    trade_type: int
    shop_type: int
    card_id: int
    item_id: str
    adding_count: int
    data_restrict: int
    reset_type: int
    tiers: tuple[tuple[int, int], ...]
    start_at: datetime | None
    end_at: datetime | None
    disclosure_quest_ids: tuple[int, ...]
    buy_condition_item_id: str
    single_buy_only: bool
    unit_base_level: int
    unit_cost_modify: int
    unit_evolution_count: int
    unit_skill_level: int
    unit_skill_evolution: int
    unit_init_trust: int
    currency_kind: str | None
    currency_item_id: str | None
    registration: int | None
    product_name: str
    target_rarity: str
    gain_exp: int
    category_type: int
    type_catalog: int
    consume_item_id: str

    @property
    def ordinary(self) -> bool:
        return self.shop_type == 0 and self.product_type in (1, 3) and self.trade_type in REGISTRATIONS

    @property
    def variable_price(self) -> bool:
        return len({price for _, price in self.tiers}) > 1


@dataclass(frozen=True)
class TradeQuote:
    product: TradeProduct
    quantity: int
    exchange_count: int
    unit_price: int
    total_price: int
    amount: int
    currency_kind: str
    currency_item_id: str | None
    registration: int
    ownership_discount: int
    currency_balance: int


def _integer(value, minimum=0):
    if not int32(value) or value < minimum:
        raise ValueError("Expected a bounded native integer")
    return value


def _text(value, maximum=4096):
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum or "\0" in value:
        raise ValueError("Expected bounded resource text")
    return value


def _digest(content) -> str:
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def native_milliseconds(value) -> int:
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(part) is not int or not 0 <= part <= UINT32_MASK for part in value)):
        raise ValueError("Shop dates require two native uint32 words")
    return value[0] | value[1] << 32


def native_moment(milliseconds: int) -> datetime | None:
    if not milliseconds:
        return None
    try:
        return NATIVE_EPOCH + timedelta(milliseconds=milliseconds)
    except OverflowError as error:
        raise ValueError("Shop date is outside the supported calendar") from error


def product_headers(counts) -> tuple[int, ...]:
    # 819320 folds each following row into the open product while its TradeCount keeps rising.
    return tuple(index for index, count in enumerate(counts) if not index or count <= counts[index - 1])


def _date(value):
    return native_moment(native_milliseconds(value))


def _quest_ids(text):
    text = _text(text).strip()
    if not text:
        return ()
    parts = text.removesuffix(",").split(",")
    if any(not re.fullmatch(r"[0-9]{1,10}", part.strip()) for part in parts):
        raise ValueError("Quest disclosure requires a literal integer list")
    result = tuple(_integer(int(part), 1) for part in parts)
    if len(set(result)) != len(result):
        raise ValueError("Quest disclosure identifiers must be distinct")
    return result


def _state_error(message):
    raise ProtocolError("trade_state_invalid", message, 503)


class TradeCatalog:
    def __init__(self, cards, possessions, asset_reader, *, resource_version,
                 earlier_resources: Callable[[], Mapping[str, Mapping[str, str]]] | None = None):
        self.possessions = possessions
        hashes = {}

        def table(name, required, *, exact=False):
            raw = asset_reader(name)
            if not isinstance(raw, bytes) or len(raw) > RESOURCE_LIMITS.max_input_bytes:
                raise ValueError("Trade resource must be bounded bytes")
            payload = decompress_all4(raw, RESOURCE_LIMITS) if raw.startswith(b"ALL4") else raw
            parsed = parse_table(payload, RESOURCE_LIMITS)
            fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
            if (len(fields) != len(parsed["fields"]) or any(fields.get(name) != kind for name, kind in required.items())
                    or (exact and list(fields.items()) != list(required.items()))):
                raise ValueError("Trade resource schema differs from the verified contract")
            hashes[name] = hashlib.sha256(raw).hexdigest()
            return parsed["rows"], hashlib.sha256(payload).hexdigest()

        try:
            if not callable(asset_reader) or not _text(resource_version, 128) or resource_version != cards.resource_version:
                raise ValueError("Trade and card resources require the same fixed version")
            if earlier_resources is not None and not callable(earlier_resources):
                raise ValueError("Earlier trade resource identities require a provider")
            if not possessions.is_ready() or not isinstance(possessions.asset_sha256, str) or not _HASH.fullmatch(possessions.asset_sha256):
                raise ValueError("Verified possession definitions are required")
            card_ids = tuple(_integer(value, 1) for value in cards.card_ids)
            if not card_ids or len(card_ids) > 65535 or len(set(card_ids)) != len(card_ids):
                raise ValueError("Card identifiers must be bounded and distinct")
            self._card_ids = frozenset(card_ids)
            item_rows, item_hash = table("ItemConfigTable.atb", {"ItemID": 32, "ID": 1, "Stack": 1})
            if item_hash != possessions.asset_sha256:
                raise ValueError("Trade and possession item resources differ")
            items, item_names = {}, {}
            for row in item_rows:
                name = _text(row["ItemID"], STORED_ITEM_NAME_BYTES)
                identifier, stack = _integer(row["ID"], INT32_MIN), _integer(row["Stack"])
                if (not name or name in items or identifier in item_names or identifier != command_id(name)
                        or possessions.resolve_hash(identifier) != name or possessions.stack(name) != stack):
                    raise ValueError("Trade item identities or stacks are inconsistent")
                items[name], item_names[identifier] = stack, name
            inventory_rows, _ = table("InventoryConfig.atb", {"ConfigID": 1, "ShopType": 1, "TradeType": 1, "ConsumeItemID": 1})
            inventory, seen = {}, set()
            for row in inventory_rows:
                identifier = _integer(row["ConfigID"], 1)
                key = (_integer(row["ShopType"]), _integer(row["TradeType"]))
                currency = _integer(row["ConsumeItemID"], INT32_MIN)
                if identifier in seen:
                    raise ValueError("Inventory configuration identities are duplicated")
                seen.add(identifier)
                inventory.setdefault(key, []).append(currency)
            specialty_rows, _ = table("UnitSpecialty.atb", {"ID_Card": 1, "Type_Specialty": 1, "Value_Specialty": 1,
                "Value_Param1": 1, "Value_Param2": 1, "Value_Param3": 1, "Value_Param4": 1, "Command": 32})
            discounts, specialty_ids, current = {}, set(), None
            for row in specialty_rows:
                identifier = _integer(row["ID_Card"])
                if identifier:
                    if identifier in specialty_ids:
                        raise ValueError("Specialty card groups are duplicated")
                    current = identifier
                    specialty_ids.add(identifier)
                if current is None:
                    raise ValueError("Specialty continuation has no owning card")
                kind, value = _integer(row["Type_Specialty"]), _integer(row["Value_Specialty"], INT32_MIN)
                if kind == 5:
                    discounts.setdefault(current, value)
            shop_rows, _ = table(SHOP_RESOURCE, SHOP_FIELDS, exact=True)
            prices = []
            for row in shop_rows:
                for name, kind in SHOP_FIELDS.items():
                    if kind == 1:
                        _integer(row[name], INT32_MIN)
                    elif kind == 32:
                        _text(row[name])
                    else:
                        _date(row[name])
                prices.append((_integer(row["TradeCount"], 1), _integer(row["RequiredValue"])))
            headers = product_headers([count for count, _ in prices])
            groups = [(dict(shop_rows[start]), prices[start:end])
                      for start, end in zip(headers, (*headers[1:], len(prices)))]
            products = {}
            for row, tiers in groups:
                identifier = _integer(row["ShopID"], 1)
                if identifier in products:
                    raise ValueError("Shop identifiers are duplicated after tier grouping")
                kind, trade_type, shop_type = (_integer(row[name]) for name in ("ProductType", "Type", "ShopType"))
                candidates = set(inventory.get((shop_type, trade_type), ()))
                if trade_type == 9:
                    candidates &= {command_id(row["ConsumeItemID"])}
                currency_kind, currency_item = None, None
                if trade_type in (7, 8):
                    currency_kind = "money" if trade_type == 7 else "crystals"
                elif len(candidates) > 1:
                    raise ValueError("Inventory matches disagree on the trade currency")
                elif candidates and next(iter(candidates)) in item_names:
                    currency_kind, currency_item = "possession", item_names[next(iter(candidates))]
                start, end = _date(row["StartDateTime"]), _date(row["EndDateTime"])
                if start is not None and end is not None and start >= end:
                    raise ValueError("Shop time window is empty or inverted")
                reset_type = _integer(row["ResetType"])
                if reset_type not in (0, 1, 2) or row["SingleBuyOnly"] not in (0, 1):
                    raise ValueError("Shop reset or selection metadata is unsupported")
                products[identifier] = TradeProduct(
                    shop_id=identifier, product_type=kind, trade_type=trade_type, shop_type=shop_type,
                    card_id=_integer(row["CardID"]), item_id=row["ItemID"],
                    adding_count=max(_integer(row["AddingCount"], INT32_MIN), 1),
                    data_restrict=_integer(row["Data_Restrict"]), reset_type=reset_type, tiers=tuple(tiers),
                    start_at=start, end_at=end, disclosure_quest_ids=_quest_ids(row["DisclosureQuestIdList"]),
                    buy_condition_item_id=row["BuyConditionItemID"], single_buy_only=bool(row["SingleBuyOnly"]),
                    unit_base_level=_integer(row["UnitBaseLevel"]),
                    unit_cost_modify=_integer(row["UnitCostModify"], INT32_MIN),
                    unit_evolution_count=_integer(row["UnitEvolutionCount"]),
                    unit_skill_level=_integer(row["UnitSkillLevel"]),
                    unit_skill_evolution=_integer(row["UnitSkillEvolution"]),
                    unit_init_trust=_integer(row["UnitInitTrust"]),
                    currency_kind=currency_kind, currency_item_id=currency_item,
                    registration=REGISTRATIONS.get(trade_type) if shop_type == 0 and kind in (1, 3) else None,
                    product_name=row["ProductName"], target_rarity=row["TargetRarity"],
                    gain_exp=_integer(row["GainExp"]), category_type=_integer(row["CategoryType"]),
                    type_catalog=_integer(row["Type_Catalog"]), consume_item_id=row["ConsumeItemID"])
            if not products:
                raise ValueError("Trade products are empty")
            self._products, self._discounts, self._items = (MappingProxyType(value) for value in (products, discounts, items))
            # Credit may open a new possession row, and new rows must fit the native cell.
            self._unsupported_items = frozenset(product.item_id for product in products.values()
                if product.ordinary and product.product_type == 3 and product.item_id in items
                and len(product.item_id.encode("utf-8")) > MAX_ITEM_NAME_BYTES)
            self._item_hash, self._hashes = item_hash, MappingProxyType(hashes)
            self.resource_version = resource_version
            content = {"policy": 1, "master": cards.snapshot.identifier, "client": cards.snapshot.client_hash,
                "master_sha256": hashlib.sha256(cards.snapshot.get_xml("CardData")).hexdigest(),
                "resource_version": resource_version, "hashes": hashes, "possession_hash": item_hash}
            self._content = MappingProxyType(content)
            self.content_version = _digest(content)
        except (AttributeError, KeyError, TypeError, ValueError, OSError, OverflowError) as error:
            raise ProtocolError("trade_catalog_invalid", "Trade resources do not satisfy the verified contract", 503) from error
        self._earlier_resources, self._replayable = earlier_resources, None

    @property
    def products(self) -> tuple[TradeProduct, ...]:
        return tuple(self._products.values())

    def product(self, shop_id: int) -> TradeProduct:
        if not int32(shop_id) or shop_id not in self._products:
            raise ProtocolError(PRODUCT_UNKNOWN, "Trade product is not in the verified catalog")
        return self._products[shop_id]

    def is_ready(self) -> bool:
        return self.possessions.is_ready() and self.possessions.asset_sha256 == self._item_hash

    def content_version_for(self, resource_version: str, changed_sha256: Mapping[str, str]) -> str:
        if (not _text(resource_version, 128) or not isinstance(changed_sha256, Mapping)
                or not set(changed_sha256) <= set(self._hashes)
                or any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in changed_sha256.values())):
            raise ValueError("Earlier trade resource identities are invalid")
        return _digest({**self._content, "resource_version": resource_version,
                        "hashes": {**self._hashes, **changed_sha256}})

    def replayable_content_versions(self) -> frozenset[str]:
        # Earlier identities are resolved on the first replay, because naming them rebuilds a whole resource index.
        if self._replayable is None:
            try:
                earlier = {} if self._earlier_resources is None else self._earlier_resources()
                if not isinstance(earlier, Mapping) or len(earlier) > MAX_EARLIER_RESOURCES:
                    raise ValueError("Earlier trade resource identities must be a bounded mapping")
                versions = [self.content_version_for(version, changed) for version, changed in earlier.items()]
            except (TypeError, ValueError, OSError, RecursionError) as error:
                raise ProtocolError("trade_catalog_invalid", "Earlier trade resource identities are unavailable",
                                    503) from error
            self._replayable = frozenset((self.content_version, *versions))
        return self._replayable

    def status(self):
        ordinary = tuple(product for product in self.products if product.ordinary)
        return {"ready": self.is_ready(),
                "products": len(self._products), "ordinary_products": len(ordinary),
                "product_types": dict(Counter(product.product_type for product in self.products)),
                "registrations": sorted(REGISTRATIONS.values()), "resource_version": self.resource_version,
                "resource_sha256": dict(self._hashes), "content_version": self.content_version,
                "unresolved_currency": [product.shop_id for product in ordinary if product.currency_kind is None],
                "unresolved_unit": [product.shop_id for product in ordinary if product.product_type == 1 and product.card_id not in self._card_ids],
                "unresolved_item": [product.shop_id for product in ordinary if product.product_type == 3 and product.item_id not in self._items],
                "unsupported_item_names": sorted(self._unsupported_items),
                "reset_policy": "Caller supplies the effective exchange count; no cycle is inferred",
                "unit_capacity": "Transaction service validates unit space and complete responses",
                "fame_depository": "Any positive verified stage count supplies ownership", "native_tested": False}

    def _state(self, state):
        if not self.is_ready():
            raise ProtocolError("trade_catalog_changed", "Possession definitions changed after trade setup", 503)
        if (not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int
                or state["bootstrap_schema"] != 1 or not isinstance(state.get("tables"), dict)):
            _state_error("Trade quote requires supported persisted tables")
        tables = state["tables"]
        return tables, self.possessions.counts(state)

    def _owned(self, state, card_id):
        tables = state["tables"]
        fame = owned_fame_cards(state, self._card_ids)
        rows = tables.get("config")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            _state_error("Ownership discount requires one configuration row")
        flags = rows[0].get(PURCHASE_FIELD)
        if not int32(flags) or (flags & UINT32_MASK) >> EXTRA_SHIFT > MAXIMUM_EXTRA_BARRACKS:
            _state_error("Ownership discount requires supported barracks flags")
        extra = (flags & UINT32_MASK) >> EXTRA_SHIFT
        player = state.get("player_state")
        if (not isinstance(player, dict) or type(player.get("schema_version")) is not int or player["schema_version"] != 1
                or not isinstance(player.get("units"), list) or len(player["units"]) > MAX_PLAYER_UNITS):
            _state_error("Ownership discount requires a bounded player roster")
        owned = False
        for unit in player["units"]:
            if (not isinstance(unit, dict) or not int32(unit.get("CardID")) or unit["CardID"] not in self._card_ids
                    or not int32(unit.get("GeneralPurposeFlags"))):
                _state_error("Ownership discount found an invalid unit row")
            owned |= unit["CardID"] == card_id and barracks_index(unit) <= extra
        silver, schema = tables.get("silver_house", []), state.get("silver_house_schema")
        if (not isinstance(silver, list) or len(silver) > 65535
                or (schema is not None and (type(schema) is not int or schema != 1)) or (silver and schema != 1)):
            _state_error("Ownership discount requires a supported silver warehouse")
        seen = set()
        for row in silver:
            if (not isinstance(row, dict) or set(row) != {"CardID", "Count"}
                    or not int32(row["CardID"]) or row["CardID"] not in self._card_ids or row["CardID"] in seen
                    or not int32(row["Count"]) or row["Count"] < 0):
                _state_error("Silver warehouse rows are invalid or duplicated")
            seen.add(row["CardID"])
            owned |= row["CardID"] == card_id
        return owned or card_id in fame

    @staticmethod
    def _disclosed(tables, product):
        if not product.disclosure_quest_ids:
            return
        rows = tables.get("quest_records")
        if not isinstance(rows, list) or len(rows) > MAX_QUEST_RECORDS:
            _state_error("Trade disclosure requires bounded quest records")
        records = {}
        for row in rows:
            if (not isinstance(row, dict) or not int32(row.get("QuestID")) or row["QuestID"] <= 0
                    or row["QuestID"] in records or not int32(row.get("AchieveFlag"))):
                _state_error("Trade disclosure quest records are invalid")
            records[row["QuestID"]] = row["AchieveFlag"]
        if any(not records.get(identifier, 0) & CLEAR_FLAG for identifier in product.disclosure_quest_ids):
            raise ProtocolError("trade_quest_required", "All product disclosure quests must be completed", 409)

    def quote(self, state, shop_id, quantity, exchange_count, now, registration) -> TradeQuote:
        product = self.product(shop_id)
        if not product.ordinary:
            raise ProtocolError("trade_product_unsupported", "This product requires a separate native transaction", 501)
        if product.product_type == 3 and product.item_id in self._unsupported_items:
            raise ProtocolError("trade_product_unsupported", "Product reward name exceeds the native cell", 501)
        return self._quote_product(state, product, quantity, exchange_count, now, registration)

    def _quote_product(self, state, product, quantity, exchange_count, now, registration) -> TradeQuote:
        if not int32(registration) or registration != product.registration:
            raise ProtocolError("trade_registration_mismatch", "Trade operation does not match the product currency")
        if (not int32(quantity) or quantity <= 0 or not int32(exchange_count) or exchange_count < 0
                or not int32(exchange_count + quantity)):
            raise ProtocolError("trade_quantity_invalid", "Trade quantity and effective count must be bounded integers")
        try:
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("Clock has no timezone")
            clock = now.astimezone(JST)
        except (ValueError, OverflowError) as error:
            raise ProtocolError("trade_time_invalid", "Trade quote requires a valid timezone-aware clock") from error
        if ((product.start_at is not None and clock < product.start_at)
                or (product.end_at is not None and clock >= product.end_at)):
            raise ProtocolError("trade_product_closed", "Product is outside its native trading window", 409)
        if product.buy_condition_item_id:
            raise ProtocolError("trade_condition_unsupported", "This product has an unsupported purchase condition", 501)
        if quantity > 1 and (product.single_buy_only or product.variable_price):
            raise ProtocolError("trade_single_purchase", "This product only permits one item per request", 409)
        if product.data_restrict and exchange_count + quantity > product.data_restrict:
            raise ProtocolError("trade_limit_reached", "Trade quantity exceeds the effective purchase limit", 409)
        tables, holdings = self._state(state)
        self._disclosed(tables, product)
        if ((product.product_type == 1 and product.card_id not in self._card_ids)
                or (product.product_type == 3 and product.item_id not in self._items)):
            raise ProtocolError("trade_reward_unavailable", "Product reward is absent from the verified definitions", 503)
        ordinal = exchange_count + 1
        price = next((value for end, value in product.tiers if ordinal <= end), product.tiers[-1][1])
        modifier = 0
        if product.trade_type == 2 and product.product_type == 1 and product.card_id in self._discounts:
            if self._owned(state, product.card_id):
                modifier = self._discounts[product.card_id]
        price += modifier
        total, amount = price * quantity, product.adding_count * quantity
        if any(not int32(value) or value < 0 for value in (price, total, amount)):
            raise ProtocolError("trade_amount_invalid", "Trade amount exceeds the native integer range", 409)
        if product.currency_kind is None:
            raise ProtocolError("trade_currency_unavailable", "Product has no current verified currency mapping", 501)
        if product.currency_kind == "crystals":
            balance = wallet_totals(state)["total"]
        elif product.currency_kind == "money":
            game = tables.get("game_param")
            if (not isinstance(game, list) or len(game) != 1 or not isinstance(game[0], dict)
                    or not int32(game[0].get("Money")) or game[0]["Money"] < 0):
                _state_error("Trade quote requires a nonnegative gold balance")
            balance = game[0]["Money"]
        else:
            balance = holdings.get(product.currency_item_id, 0)
        if balance < total:
            raise ProtocolError("trade_currency_insufficient", "Trade price exceeds the available currency", 409)
        if product.product_type == 3:
            after = holdings.get(product.item_id, 0) + amount
            if not int32(after) or after > self._items[product.item_id]:
                raise ProtocolError("trade_item_capacity", "Trade reward exceeds its verified item stack", 409)
        return TradeQuote(product, quantity, exchange_count, price, total, amount, product.currency_kind,
                          product.currency_item_id, registration, modifier, balance)
