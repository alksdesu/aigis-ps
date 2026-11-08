from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, dumps
from .business_calendar import NATIVE_DATETIME
from .energy_clock import JST
from .errors import ProtocolError
from .letstd_rules import GROUPS, PARTNER_ORDER, PARTNERS
from .native_numbers import int32
from .possessions import MAX_ITEM_NAME_BYTES


RESOURCE_NAME = "LetsTDRewards.atb"
TEXT_RESOURCE = "SystemText.atb"
FIELDS = MappingProxyType({"ConfigId": 1, "StartDate": 32, "EndDate": 32, "ServiceType": 32,
    "RewardGroupId": 1, "RewardId": 1, "ItemType": 1, "CardId": 1, "GeneralItemID": 32, "Count": 1,
    "SystemTextID": 1, "ReceiveTextIdSingle": 1, "ReceiveTextIdMulti": 1})
TEXT_FIELDS = MappingProxyType({"Recode_Index": 1, "Data_Text": 32})
ITEM, UNIT, FREE_CRYSTAL, GOLD = 1, 2, 3, 4
DATE_FORMAT = NATIVE_DATETIME
MAX_REWARD_ROWS = 4096
MAX_TITLE_BYTES = 1024
POLICY_VERSION = "local-letstd-rewards-1"
LIMITS = ParseLimits(max_input_bytes=16 * 1024 * 1024, max_decompressed_bytes=16 * 1024 * 1024,
                     max_rows=65535, max_fields=64, max_cells=2_000_000)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _text(value, maximum):
    if type(value) is not str or len(value.encode("utf-8")) > maximum:
        raise ValueError("LetsTD text exceeds its native field bound")
    dumps(Container(children=(Column("Value", "S", (value,)),)))
    return value


def _item_types(rewards):
    return {str(kind): count for kind, count in sorted(Counter(reward.item_type for reward in rewards).items())}


def _instant(value):
    if type(value) is not str:
        raise ValueError("LetsTD dates must be native strings")
    parsed = datetime.strptime(value, DATE_FORMAT)
    if parsed.strftime(DATE_FORMAT) != value:
        raise ValueError("LetsTD dates must use the canonical native layout")
    result = parsed.replace(tzinfo=JST)
    result.astimezone(timezone.utc)
    return result


@dataclass(frozen=True)
class LetsTDReward:
    config_id: int
    partner: str
    group: int
    reward_id: int
    item_type: int
    card_id: int
    item_id: str
    count: int
    start: datetime
    end: datetime | None
    text_id: int
    title: str
    receive_single: int
    receive_multi: int
    source_index: int
    definition: str


def _reward(index, row, texts):
    if not all(int32(row[name]) for name, kind in FIELDS.items() if kind == 1):
        raise ValueError("LetsTD integers must be native int32 values")
    partner, group, kind = row["ServiceType"], row["RewardGroupId"], row["ItemType"]
    if partner not in PARTNERS or group not in GROUPS or row["RewardId"] <= 0:
        raise ValueError("LetsTD rows require a native partner, reward group and positive reward identity")
    item_id, card_id, count = _text(row["GeneralItemID"], MAX_ITEM_NAME_BYTES), row["CardId"], row["Count"]
    if (kind == ITEM and (not item_id or card_id != 0) or kind == UNIT and (card_id <= 0 or count != 1)
            or kind in (FREE_CRYSTAL, GOLD) and count <= 0):
        raise ValueError("LetsTD reward shape differs from its item type")
    start = _instant(row["StartDate"])
    end = None if row["EndDate"] == "" else _instant(row["EndDate"])
    text_id = row["SystemTextID"]
    if not 0 <= text_id < len(texts):
        raise ValueError("LetsTD title text index is outside the text table")
    title = _text(texts[text_id]["Data_Text"], MAX_TITLE_BYTES)
    source = {name: row[name] for name in FIELDS}
    return LetsTDReward(row["ConfigId"], partner, group, row["RewardId"], kind, card_id, item_id, count, start, end,
        text_id, title, row["ReceiveTextIdSingle"], row["ReceiveTextIdMulti"], index,
        _digest({"schema": 1, "source": source, "title": title}))


class LetsTDCatalog:
    def __init__(self, asset_reader):
        try:
            if not callable(asset_reader):
                raise ValueError("LetsTD catalog requires a resource reader")
            hashes, snapshots = {}, {}

            def table(name, expected, exact):
                raw = asset_reader(name)
                if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
                    raise ValueError("LetsTD resources must be bounded immutable bytes")
                parsed = parse_table(decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw, LIMITS)
                fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
                if (len(fields) != len(parsed["fields"]) or exact and fields != dict(expected)
                        or any(fields.get(key) != kind for key, kind in expected.items())):
                    raise ValueError("LetsTD resource fields differ from the native schema")
                hashes[name], snapshots[name] = hashlib.sha256(raw).hexdigest(), raw
                return parsed["rows"]

            rows = table(RESOURCE_NAME, FIELDS, True)
            texts = table(TEXT_RESOURCE, TEXT_FIELDS, False)
            if not rows or len(rows) > MAX_REWARD_ROWS:
                raise ValueError("LetsTD rewards must be a bounded nonempty table")
            rewards = [_reward(index, row, texts) for index, row in enumerate(rows)]
            if len({reward.config_id for reward in rewards}) != len(rewards):
                raise ValueError("LetsTD configuration identities must be unique")
            indexed = {(reward.partner, reward.group, reward.reward_id): reward for reward in rewards}
            if len(indexed) != len(rewards):
                raise ValueError("LetsTD partner, group and reward identities must be unique")
            ordered = tuple(sorted(rewards, key=lambda reward: (PARTNER_ORDER[reward.partner], reward.group,
                                                                reward.reward_id)))
            self._rows = ordered
            self._by_key = MappingProxyType(indexed)
            self._groups = MappingProxyType({(partner, group): tuple(reward for reward in ordered
                if (reward.partner, reward.group) == (partner, group)) for partner in PARTNERS for group in GROUPS})
            self.resource_hashes = MappingProxyType(hashes)
            self._resources = MappingProxyType(snapshots)
            self._content_version = _digest({"policy": POLICY_VERSION, "resources": hashes})
            self._snapshot = (self._rows, self._by_key, self._groups, self.resource_hashes, self._resources,
                              self._content_version)
        except (AttributeError, KeyError, TypeError, ValueError, OSError, OverflowError) as error:
            raise ProtocolError("letstd_catalog_invalid", "LetsTD resources differ from the verified native schema",
                                503) from error

    @property
    def rows(self):
        return self._rows

    @property
    def content_version(self):
        return self._content_version

    @property
    def resource_sha256(self):
        return self.resource_hashes[RESOURCE_NAME]

    def _ready(self):
        rows, indexed, groups, hashes, resources, version = self._snapshot
        if (self._rows is not rows or self._by_key is not indexed or self._groups is not groups
                or self.resource_hashes is not hashes or self._resources is not resources
                or self._content_version != version):
            raise ProtocolError("letstd_catalog_changed", "LetsTD catalog changed after construction", 503)

    def _key(self, partner, group):
        self._ready()
        if partner not in PARTNERS or type(group) is not int or group not in GROUPS:
            raise ProtocolError("letstd_group_invalid", "LetsTD groups require a native partner and reward group")
        return partner, group

    def rows_for(self, partner, group):
        return self._groups[self._key(partner, group)]

    def get(self, partner, group, reward_id):
        key = self._key(partner, group)
        if type(reward_id) is not int:
            raise ProtocolError("letstd_reward_invalid", "LetsTD reward identities must be integers")
        return self._by_key.get((*key, reward_id))

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "content_version": self.content_version,
                "resource_hashes": dict(self.resource_hashes), "row_count": len(self.rows),
                "groups": {partner: {name: {"rows": len(self._groups[partner, group]),
                    "item_types": _item_types(self._groups[partner, group])} for group, name in GROUPS.items()}
                    for partner in PARTNERS},
                "item_types": _item_types(self.rows),
                "start_dates": {start: count for start, count in sorted(Counter(
                    reward.start.strftime(DATE_FORMAT) for reward in self.rows).items())},
                "open_ended_rows": sum(reward.end is None for reward in self.rows),
                "time_policy": "StartDate and EndDate are read as JST; an empty EndDate never expires"}
