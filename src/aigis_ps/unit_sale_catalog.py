from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .errors import ProtocolError
from .native_numbers import INT32_MIN, int32
from .player_state import MAX_PLAYER_UNITS
from .system_literals import command_id, decode_command, rating_command_id


AUTO_RESOURCE = "OnCommandMenuUnitRetire.atb"
AUTO_COMMAND = "CMenuUnitRetireList::DoAutoSailExcute"
RATING_RESOURCE = "OnCommand.atb"
RAINBOW_ITEM = "trade_item_003000"
RAINBOW_HASH = command_id(RAINBOW_ITEM)
LIMITS = ParseLimits(max_input_bytes=64 * 1024 * 1024, max_decompressed_bytes=64 * 1024 * 1024)
_AUTO_CALL = re.compile(r'\s*CMenuTaskSendMessage\s*\(\s*0\s*,\s*"AutoSailExcute"\s*'
                        r'((?:,\s*[0-9]{1,10}\s*)+)\)\s*;\s*')


@dataclass(frozen=True)
class SaleQuote:
    gold: int
    rainbow: int


def _integer(value, minimum=0):
    if not int32(value) or value < minimum:
        raise ValueError("Sale values require bounded native integers")
    return value


class UnitSaleCatalog:
    def __init__(self, cards, possessions, asset_reader):
        hashes, prices = {}, {}
        try:
            if not callable(asset_reader):
                raise ValueError("A resource reader is required")
            for card_id in cards.card_ids:
                _integer(card_id, 1)
                if card_id in prices or len(prices) >= 65536:
                    raise ValueError("Sale card identifiers are duplicated or exceed the catalog bound")
                row = cards.card(card_id)
                if _integer(row["CardID"], 1) != card_id:
                    raise ValueError("Sale card identity does not match its lookup key")
                prices[card_id] = SaleQuote(_integer(row["SellPrice"]), _integer(row["_TradePoint"]))
            if not prices:
                raise ValueError("Sale card definitions are empty")
            auto_commands = self._commands(AUTO_RESOURCE, asset_reader, hashes)
            matches = auto_commands.get(command_id(AUTO_COMMAND), ())
            if len(matches) != 1:
                raise ValueError("Automatic sale requires one unambiguous command")
            match = _AUTO_CALL.fullmatch(decode_command(matches[0]))
            if match is None:
                raise ValueError("Automatic sale command is not a literal card list")
            auto_ids = tuple(_integer(int(value), 1) for value in re.findall(r"[0-9]+", match[1]))
            if len(auto_ids) > MAX_PLAYER_UNITS or len(set(auto_ids)) != len(auto_ids) or set(auto_ids) - prices.keys():
                raise ValueError("Automatic sale identifiers are duplicated, excessive or unknown")
            commands = self._commands(RATING_RESOURCE, asset_reader, hashes)
            restricted, owners = set(), {}
            for card_id in prices:
                key = rating_command_id(card_id)
                if key in owners:
                    raise ValueError("Sale rating command identifiers collide")
                owners[key] = card_id
                entries = commands.get(key, ())
                if len(entries) > 1 or (entries and decode_command(entries[0]).strip()):
                    restricted.add(card_id)
            if possessions.resolve_hash(RAINBOW_HASH) != RAINBOW_ITEM:
                raise ValueError("Rainbow currency hash does not match its canonical item")
            _integer(possessions.stack(RAINBOW_ITEM), 1)
            possession_hash = possessions.asset_sha256
            if not isinstance(possession_hash, str) or re.fullmatch(r"[0-9a-f]{64}", possession_hash) is None:
                raise ValueError("Rainbow currency requires a verified item resource")
            self._prices = MappingProxyType(prices)
            self._auto_card_ids = auto_ids
            self._rating_restricted = frozenset(restricted)
            self._hashes = MappingProxyType(hashes)
            self._master_snapshot = cards.snapshot.identifier
            self._resource_version = cards.resource_version
            content = {"master": self._master_snapshot, "client": cards.snapshot.client_hash,
                       "version": self._resource_version, "resources": hashes, "possessions": possession_hash,
                       "card_master_sha256": hashlib.sha256(cards.snapshot.get_xml("CardData")).hexdigest(),
                       "prices": {card_id: (price.gold, price.rainbow) for card_id, price in prices.items()}}
            self.content_version = hashlib.sha256(json.dumps(content, sort_keys=True, allow_nan=False).encode()).hexdigest()
        except (AttributeError, KeyError, TypeError, ValueError, OSError) as error:
            raise ProtocolError("unit_sale_catalog_invalid", "Sale resources do not satisfy the verified contract", 503) from error

    @staticmethod
    def _commands(name, asset_reader, hashes):
        raw = asset_reader(name)
        if not isinstance(raw, bytes) or len(raw) > LIMITS.max_input_bytes:
            raise ValueError("Sale command resource must be bounded bytes")
        hashes[name] = hashlib.sha256(raw).hexdigest()
        payload = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
        table = parse_table(payload, LIMITS)
        fields = {field["name"]: field["type_code"] for field in table["fields"]}
        if len(fields) != len(table["fields"]) or fields != {"_ID": 1, "_Cmnd": 32}:
            raise ValueError("Sale command resource has an incompatible schema")
        commands = {}
        for row in table["rows"]:
            key, encoded = _integer(row["_ID"], INT32_MIN), row["_Cmnd"]
            if not isinstance(encoded, str):
                raise ValueError("Sale commands require text payloads")
            commands.setdefault(key, []).append(encoded)
        return commands

    @property
    def auto_card_ids(self):
        return self._auto_card_ids

    @property
    def rainbow_item(self):
        return RAINBOW_ITEM

    def _price(self, card_id):
        if not int32(card_id) or card_id not in self._prices:
            raise ProtocolError("unit_sale_card_unknown", "Sale requires a verified card definition", 503)
        return self._prices[card_id]

    def check_rating(self, card_id):
        self._price(card_id)
        if card_id in self._rating_restricted:
            raise ProtocolError("unit_sale_rating_unsupported", "This card requires an unsupported sale rating condition", 501)

    def quote(self, units) -> SaleQuote:
        if (not isinstance(units, Sequence) or isinstance(units, (str, bytes))
                or len(units) > MAX_PLAYER_UNITS):
            raise ProtocolError("unit_sale_units_invalid", "Sale quote requires a bounded sequence of unit instances")
        gold = rainbow = 0
        for unit in units:
            if not isinstance(unit, Mapping) or "CardID" not in unit:
                raise ProtocolError("unit_sale_units_invalid", "Sale quote requires unit instances with CardID")
            price = self._price(unit["CardID"])
            gold += price.gold
            rainbow += price.rainbow
            if not int32(gold) or not int32(rainbow):
                raise ProtocolError("unit_sale_amount_overflow", "Sale proceeds exceed the native int32 range", 409)
        return SaleQuote(gold, rainbow)

    def status(self):
        return {"ready": True, "content_version": self.content_version, "cards": len(self._prices),
                "master_snapshot": self._master_snapshot, "resource_version": self._resource_version,
                "resource_sha256": dict(self._hashes), "auto_card_ids": list(self.auto_card_ids),
                "rainbow_item": self.rainbow_item, "rating_conditions_unavailable": sorted(self._rating_restricted)}
