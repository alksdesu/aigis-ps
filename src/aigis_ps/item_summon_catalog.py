from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, dumps
from .errors import ProtocolError, caught
from .native_numbers import INT32_MAX, int32
from .possessions import ITEM_RESOURCE_LIMIT, MAX_ITEM_NAME_BYTES, MAX_POSSESSION_ITEMS, STORED_ITEM_NAME_BYTES
from .system_literals import command_id, decode_command


CATEGORY = -606354181
MAX_SUMMON_COUNT = 100
LIMITS = ParseLimits(max_input_bytes=ITEM_RESOURCE_LIMIT, max_decompressed_bytes=ITEM_RESOURCE_LIMIT,
                     max_rows=MAX_POSSESSION_ITEMS)
ITEM_FIELDS = {name: 1 for name in ("ID", "EffectType", "Category", "Stack", "CanPresent", "ReceiveAction",
    "InvisibleContaint", "CanAllGet", "NoAutoDelete", "ReceiveConfirmation", "SummonConfigID", "SummonCount",
    "LotteryRuleID", "TextID", "TextIDExplanation")}
ITEM_FIELDS.update(ItemID=32, EventID=32)
RULE_FIELDS = {"RuleID": 1, "RarityIDList": 32, "CardIDList": 32, "RuleType": 1, "EnsuredCount": 1}
RARITY_FIELDS = {"ConfigID": 1, "RarityGroupID": 1}
ENSURED_RULE, UNOWNED_RULE = 1, 2
MAX_RULE_LIST_BYTES = 512
RECEIVE_COMMAND = "CMenuPresentList::DoGetCommonItemType_002"
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_LIST_ENTRY = re.compile(r"[1-9][0-9]{0,9}\Z")


def _text(value, maximum, *, empty=False):
    if type(value) is not str or (not value and not empty) or len(value.encode("utf-8")) > maximum:
        raise ValueError("Expected bounded item text")
    dumps(Container(children=(Column("Value", "S", (value,)),)))
    return value


def _identifiers(text):
    entries = text.split(",")
    if entries[-1] == "":
        entries.pop()
    if not all(_LIST_ENTRY.fullmatch(entry) and int(entry) <= INT32_MAX for entry in entries):
        return None
    return tuple(int(entry) for entry in entries)


@dataclass(frozen=True)
class LotteryRule:
    rule_id: int
    rule_type: int
    rarity_id: int
    rarity_group: int
    ensured_count: int


def _rule(row, rarity_groups):
    reasons = []
    rarities = _identifiers(row["RarityIDList"])
    if row["RuleType"] not in (ENSURED_RULE, UNOWNED_RULE):
        reasons.append("lottery_rule_type_unsupported")
    if _identifiers(row["CardIDList"]) != ():
        reasons.append("lottery_rule_cards_unsupported")
    if rarities is None or len(rarities) != 1 or rarity_groups.get(rarities[0], 0) <= 0:
        reasons.append("lottery_rule_rarity_unsupported")
    if reasons:
        return tuple(reasons)
    return LotteryRule(row["RuleID"], row["RuleType"], rarities[0], rarity_groups[rarities[0]], row["EnsuredCount"])


def _lottery_rule(row, rules):
    if row["LotteryRuleID"] == 0:
        return None, ()
    rule = rules.get(row["LotteryRuleID"], ("lottery_rule_unknown",))
    if not isinstance(rule, LotteryRule):
        return None, rule
    count, ensured = row["SummonCount"], rule.ensured_count
    if not (1 <= ensured <= count if rule.rule_type == ENSURED_RULE else ensured == 0 and count == 1):
        return None, ("lottery_rule_count_unsupported",)
    return rule, ()


def _unsupported(row, rule_reasons):
    reasons = []
    # Ticket mail writes the name into a native cell, so a longer stored name stays identifiable only.
    if len(row["ItemID"].encode("utf-8")) > MAX_ITEM_NAME_BYTES:
        reasons.append("item_name_unsupported")
    expected = {"Category": CATEGORY, "Stack": 0, "CanPresent": 1, "InvisibleContaint": 1,
                "CanAllGet": 0, "NoAutoDelete": 1}
    if any(row[name] != value for name, value in expected.items()):
        reasons.append("item_shape_unsupported")
    if row["ReceiveAction"] != 0:
        reasons.append("receive_action_unsupported")
    reasons.extend(rule_reasons)
    if row["EventID"]:
        reasons.append("event_unsupported")
    if row["SummonConfigID"] <= 0:
        reasons.append("pool_id_unsupported")
    if not 1 <= row["SummonCount"] <= MAX_SUMMON_COUNT:
        reasons.append("summon_count_unsupported")
    return ";".join(reasons) or None


@dataclass(frozen=True)
class ItemSummonDefinition:
    item_id: str
    pool_id: int
    count: int
    unsupported_reason: str | None
    rule: LotteryRule | None = None

    @property
    def supported(self) -> bool:
        return self.unsupported_reason is None


class ItemSummonCatalog:
    def __init__(self, pools, asset_reader):
        self.pools = pools
        try:
            if not callable(asset_reader):
                raise ValueError("Item summons require a resource reader")
            self._pool_version = pools.content_version
            if type(self._pool_version) is not str or not _HASH.fullmatch(self._pool_version):
                raise ValueError("Summon pools require a stable fingerprint")
            hashes, decoded_hashes = {}, {}

            def table(name, expected, *, exact=False):
                raw = asset_reader(name)
                if type(raw) is not bytes or not 0 < len(raw) <= ITEM_RESOURCE_LIMIT:
                    raise ValueError("Item summon resources must be bounded immutable bytes")
                data = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
                parsed = parse_table(data, LIMITS)
                fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
                if (len(fields) != len(parsed["fields"]) or any(fields.get(key) != kind for key, kind in expected.items())
                        or (exact and fields.keys() != expected.keys())):
                    raise ValueError("Item summon fields differ from the native contract")
                hashes[name], decoded_hashes[name] = hashlib.sha256(raw).hexdigest(), hashlib.sha256(data).hexdigest()
                return parsed["rows"]

            rarity_groups = {}
            for row in table("cfgRarity.atb", RARITY_FIELDS):
                identifier, group = row["ConfigID"], row["RarityGroupID"]
                if not int32(identifier) or identifier <= 0 or identifier in rarity_groups or not int32(group) or group < 0:
                    raise ValueError("Rarity identities or groups are invalid")
                rarity_groups[identifier] = group
            rules = {}
            for row in table("LotteryRuleConfig.atb", RULE_FIELDS, exact=True):
                identifier = row["RuleID"]
                if (any(not int32(row[key]) for key, kind in RULE_FIELDS.items() if kind == 1)
                        or identifier <= 0 or identifier in rules):
                    raise ValueError("Lottery rule identities or native integers are invalid")
                for key in ("RarityIDList", "CardIDList"):
                    _text(row[key], MAX_RULE_LIST_BYTES, empty=True)
                rules[identifier] = _rule(row, rarity_groups)

            definitions, metadata, names, identifiers = {}, {}, set(), set()
            for row in table("ItemConfigTable.atb", ITEM_FIELDS):
                name = _text(row["ItemID"], STORED_ITEM_NAME_BYTES)
                identifier = row["ID"]
                if (any(not int32(row[key]) for key, kind in ITEM_FIELDS.items() if kind == 1)
                        or name in names or identifier in identifiers or identifier != command_id(name)):
                    raise ValueError("Item identity or native integer values are invalid")
                _text(row["EventID"], 512, empty=True)
                names.add(name)
                identifiers.add(identifier)
                if row["EffectType"] != 2:
                    continue
                rule, rule_reasons = _lottery_rule(row, rules)
                definitions[name] = ItemSummonDefinition(name, row["SummonConfigID"], row["SummonCount"],
                                                         _unsupported(row, rule_reasons), rule)
                metadata[name] = MappingProxyType({key: row[key] for key in ITEM_FIELDS})
            if not definitions:
                raise ValueError("Item summon definitions are empty")

            commands = {}
            for row in table("OnCommandMenuPresentList.atb", {"_ID": 1, "_Cmnd": 32}):
                identifier = row["_ID"]
                if not int32(identifier) or identifier in commands:
                    raise ValueError("Present command identities are invalid or duplicated")
                commands[identifier] = _text(row["_Cmnd"], 256 * 1024, empty=True)
            command = decode_command(commands[command_id(RECEIVE_COMMAND)])
            if re.fullmatch(r'\s*CMenuTask_CallCommand\s*\(\s*"UseCommonItemSummonTicket"\s*\)\s*;\s*', command) is None:
                raise ValueError("Item summon command has an unverified execution path")
            self._definitions = MappingProxyType(definitions)
            self._source_rows = MappingProxyType(metadata)
            self._item_ids = frozenset(definitions)
            self._lottery_rules = MappingProxyType(rules)
            self._rarity_groups = MappingProxyType(rarity_groups)
            self._hashes = MappingProxyType(hashes)
            self._decoded_hashes = MappingProxyType(decoded_hashes)
            self._ready()
            content = {"schema": 2, "resources": hashes, "pools": self._pool_version,
                "maximum_count": MAX_SUMMON_COUNT, "count_source": "ItemConfig.SummonCount",
                "unknown_shape": "preserve_unsupported", "pool_readiness": "external_transaction",
                "receive_action": 0, "event": "", "lottery_rule": {
                    "types": [ENSURED_RULE, UNOWNED_RULE], "cards": "empty", "rarities": "one_grouped_config",
                    "ensured": "one_to_summon_count", "unowned": "single_draw_without_ensured_count"}}
            self._content_version = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError, ProtocolError) as error:
            raise ProtocolError("item_summon_catalog_invalid", "Item summon resources differ from the verified contract", 503) from error

    @property
    def definitions(self):
        return self._definitions

    @property
    def source_rows(self):
        return self._source_rows

    @property
    def item_ids(self) -> frozenset[str]:
        return self._item_ids

    @property
    def lottery_rules(self):
        return self._lottery_rules

    @property
    def content_version(self) -> str:
        return self._content_version

    @property
    def item_resource_sha256(self) -> str:
        return self._decoded_hashes["ItemConfigTable.atb"]

    def _ready(self):
        try:
            if self.pools.content_version != self._pool_version or not self.pools.is_ready():
                raise ValueError("Item summon pool dependencies changed")
        except (AttributeError, ValueError, TypeError, ProtocolError) as error:
            raise ProtocolError("item_summon_catalog_changed", "Item summon pool definitions changed after setup", 503) from error

    def definition(self, item_id: str) -> ItemSummonDefinition:
        self._ready()
        if type(item_id) is not str or item_id not in self._definitions:
            raise ProtocolError("item_summon_unknown", "Item is not a known summon ticket", 501)
        return self._definitions[item_id]

    def supports_item(self, item_id: str) -> bool:
        self._ready()
        return type(item_id) is str and item_id in self._definitions

    def rarity_group(self, rare: int) -> int | None:
        # Native 953170 looks up cfgRarity at CardData.Rare + 1 and compares the RarityGroupID found there.
        group = self._rarity_groups.get(rare + 1) if int32(rare) and rare >= 0 else None
        return group or None

    def is_ready(self) -> bool:
        return caught(self._ready) is None

    def status(self) -> dict:
        result = {"ready": True, "definition_count": len(self._definitions),
            "supported_shape_count": sum(row.supported for row in self._definitions.values()),
            "count_distribution": dict(Counter(row.count for row in self._definitions.values())),
            "rule_distribution": dict(Counter(row.rule.rule_id for row in self._definitions.values()
                                              if row.supported and row.rule is not None)),
            "unsupported_rules": {identifier: ";".join(rule) for identifier, rule in self._lottery_rules.items()
                                  if not isinstance(rule, LotteryRule)},
            "unsupported": {name: row.unsupported_reason for name, row in self._definitions.items() if not row.supported},
            "content_version": self.content_version, "resource_sha256": dict(self._hashes),
            "item_resource_sha256": self.item_resource_sha256, "pool_content_version": self._pool_version,
            "pool_policy": "Transaction service must explicitly enable and validate complete pools and every candidate",
            "identity_policy": "Known Effect2 remains identifiable when its shape or pool cannot be used",
            "name_policy": (f"Usable tickets need names within the {MAX_ITEM_NAME_BYTES}-byte native cell; "
                            f"stored names up to {STORED_ITEM_NAME_BYTES} bytes stay identifiable"),
            "maximum_count": MAX_SUMMON_COUNT, "native_tested": False}
        try:
            self._ready()
        except ProtocolError as error:
            result.update(ready=False, error=error.code)
        return result
