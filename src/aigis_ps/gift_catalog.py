from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
from importlib.resources import files
import json
import re
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .errors import ProtocolError
from .masterdata import MasterSnapshot
from .native_numbers import INT32_MIN, int32
from .possessions import STORED_ITEM_NAME_BYTES, PossessionService
from .public_tables import validate_public_master


RESOURCE_LIMITS = ParseLimits(max_input_bytes=64 * 1024 * 1024, max_decompressed_bytes=64 * 1024 * 1024)


@dataclass(frozen=True)
class GiftDefinition:
    item_id: str
    item_hash: int
    category: int
    gender: int
    rarities: tuple[int, ...]
    add_value: int
    bonus_ratio: int
    bonus_value: int
    minimum: int
    maximum: int


@dataclass(frozen=True)
class TrustReward:
    id: int
    condition: int
    rarity: int
    reward_type: int
    reward_id: str
    amount: int


def _integer(value, minimum=INT32_MIN):
    if not int32(value) or value < minimum:
        raise ValueError("Expected a bounded native integer")
    return value


def _rarities(text):
    if not isinstance(text, str) or len(text) > 4096:
        raise ValueError("Invalid rarity restriction")
    parts = text.removesuffix(",").split(",")
    if not parts or any(not re.fullmatch(r"-?[0-9]{1,10}", part) for part in parts):
        raise ValueError("Rarity restrictions require comma-separated integers")
    result = tuple(_integer(int(part), -1) for part in parts)
    if len(set(result)) != len(result):
        raise ValueError("Rarity restriction contains duplicate values")
    return result


class GiftCatalog:
    def __init__(self, snapshot: MasterSnapshot, asset_reader: Callable[[str], bytes],
                 possessions: PossessionService, *, resource_version: str):
        if not isinstance(snapshot, MasterSnapshot) or not callable(asset_reader):
            raise ValueError("A fixed master snapshot and resource reader are required")
        if not isinstance(resource_version, str) or not 1 <= len(resource_version) <= 128:
            raise ValueError("A bounded resource version is required")
        self.snapshot = snapshot
        self.resource_version = resource_version
        self._asset_reader = asset_reader
        self._hashes = {}
        self._gifts = {}
        self._gift_hashes = {}
        self._rewards = {}
        self._presents = {}
        self._cards = {}
        try:
            schemas = json.loads(files("aigis_ps").joinpath("data/master-schemas.json").read_text("utf-8"))
            schema_fields = {name: [field["name"] for field in schema["fields"]] for name, schema in schemas["tables"].items()}
            cards, _ = validate_public_master("CardData", snapshot.get_xml("CardData"), schema_fields)
            names = [column.name for column in cards.children]
            for values in zip(*(column.values for column in cards.children)):
                row = dict(zip(names, values))
                self._cards[_integer(row["CardID"], 1)] = MappingProxyType(row)
            for row in self._table("ItemConfigTable.atb"):
                if _integer(row["EffectType"]) != 4:
                    continue
                item_hash = _integer(row["ID"])
                item_id = row["ItemID"]
                if possessions.resolve_hash(item_hash) != item_id or item_id in self._gifts or item_hash in self._gift_hashes:
                    raise ValueError("Gift identity differs from the verified item catalog")
                definition = GiftDefinition(item_id, item_hash, _integer(row["Category"]),
                    _integer(row["UsageRestrictionInGender"], 0), _rarities(row["UsageRestrictionInRareCSV"]),
                    _integer(row["TrustAddValue"], 0), _integer(row["TrustBonusRatio"], 0),
                    _integer(row["TrustBonusValue"], 0), _integer(row["TrustMinScope"], 0),
                    _integer(row["TrustMaxScope"], 0))
                if definition.minimum >= definition.maximum:
                    raise ValueError("Gift trust range is empty or inverted")
                self._gifts[item_id] = definition
                self._gift_hashes[item_hash] = definition
            for row in self._table("PresentUnitData.atb"):
                identifier = _integer(row["PresentID"], 0)
                _integer(row["CardID"], 1)
                if identifier in self._presents:
                    raise ValueError("Present definitions contain duplicate identifiers")
                self._presents[identifier] = MappingProxyType(dict(row))
            for row in self._table("TrustRewardConfigTable.atb"):
                reward = TrustReward(_integer(row["ID"], 1), _integer(row["TrustCondition"], 1),
                    _integer(row["Rare"], 0), _integer(row["RewardType"], 0), row["RewardID"], _integer(row["Amount"], 1))
                if (reward.id in self._rewards or not isinstance(reward.reward_id, str)
                        or len(reward.reward_id.encode("utf-8")) > STORED_ITEM_NAME_BYTES):
                    raise ValueError("Trust reward identifiers are invalid or duplicated")
                if reward.reward_type == 1:
                    if not re.fullmatch(r"[0-9]{1,10}", reward.reward_id):
                        raise ValueError("Unit trust reward must reference a PresentID")
                    present = self._presents.get(int(reward.reward_id))
                    if present is None or present.get("CardID") not in self._cards:
                        raise ValueError("Unit trust reward does not resolve to a known present and card")
                self._rewards[reward.id] = reward
            if not self._gifts or not self._rewards or not self._cards:
                raise ValueError("Gift resources or card data are empty")
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise ProtocolError("gift_catalog_invalid", "Gift resources do not satisfy the verified table contract", 503) from error

    def _table(self, name):
        raw = self._asset_reader(name)
        if not isinstance(raw, bytes) or len(raw) > RESOURCE_LIMITS.max_input_bytes:
            raise ValueError("Gift resource payload must be bounded bytes")
        self._hashes[name] = hashlib.sha256(raw).hexdigest()
        payload = decompress_all4(raw, RESOURCE_LIMITS) if raw.startswith(b"ALL4") else raw
        return parse_table(payload, RESOURCE_LIMITS)["rows"]

    @property
    def gifts(self) -> tuple[GiftDefinition, ...]:
        return tuple(self._gifts.values())

    @property
    def card_ids(self) -> tuple[int, ...]:
        return tuple(self._cards)

    @property
    def rewards(self) -> tuple[TrustReward, ...]:
        return tuple(self._rewards.values())

    def gift(self, item: str | int) -> GiftDefinition:
        value = self._gifts.get(item) if type(item) is str else self._gift_hashes.get(item) if type(item) is int else None
        if value is None:
            raise ProtocolError("gift_item_unknown", "Item does not reference a verified trust gift")
        return value

    def card(self, card_id: int) -> Mapping:
        value = self._cards.get(card_id) if type(card_id) is int else None
        if value is None:
            raise ProtocolError("gift_card_unknown", "Gift target does not reference a verified card")
        return value

    def present(self, present_id: int) -> Mapping:
        value = self._presents.get(present_id) if type(present_id) is int else None
        if value is None:
            raise ProtocolError("gift_present_unknown", "Trust reward does not reference a verified present")
        return value

    def status(self):
        return {"master_snapshot": self.snapshot.identifier, "resource_version": self.resource_version,
                "gift_definitions": len(self._gifts), "trust_rewards": len(self._rewards), "cards": len(self._cards),
                "categories": dict(Counter(gift.category for gift in self._gifts.values())),
                "resource_sha256": dict(self._hashes),
                "scope": "Verified definitions; category visibility, limits and reward execution require business contracts"}
