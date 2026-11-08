from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, dumps
from .business_calendar import NATIVE_MINUTES_TEXT
from .energy_clock import JST, instant
from .errors import ProtocolError
from .native_clock import native_clock
from .native_numbers import int32
from .possessions import ITEM_RESOURCE_LIMIT, MAX_ITEM_NAME_BYTES, MAX_POSSESSION_ITEMS, STORED_ITEM_NAME_BYTES
from .system_literals import command_id


RESOURCE_NAME = "EventPassConfig.atb"
ITEM_RESOURCE_NAME = "ItemConfigTable.atb"
CATEGORY = -425086357
MAX_TIME_LIMIT_BYTES = 4096
FIELDS = {"ID": 1, "StartDate": 32, "EndDate": 32, "TimeLimit": 32, "Status": 1,
          "RequiredPoint": 1, "NormalPassItemID": 32, "PremiumPassItemID": 32,
          "RewardItemType": 1, "RewardItemParam": 1, "RewardItemID": 32}
ITEM_FIELDS = {"ItemID": 32, "ID": 1, "Category": 1, "EffectType": 1, "Stack": 1,
               "CanPresent": 1, "CanAllGet": 1, "ReceiveAction": 1, "NoAutoDelete": 1,
               "ReceiveConfirmation": 1, "InvisibleContaint": 1, "EventID": 32,
               "SummonCount": 1, "SummonConfigID": 1, "LotteryRuleID": 1}
LIMITS = ParseLimits(max_input_bytes=ITEM_RESOURCE_LIMIT, max_decompressed_bytes=ITEM_RESOURCE_LIMIT,
                     max_rows=65535, max_fields=128)
ELIGIBILITY_RULE = "status_1_and_inclusive_start_end_native_clock"
# Receipts committed while pass windows compared JST instants keep this rule in their digest.
LEGACY_ELIGIBILITY_RULE = "status_1_and_inclusive_start_end_jst"


def _integer(value, minimum=0):
    if not int32(value) or value < minimum:
        raise ValueError("Expected a bounded native integer")
    return value


def _text(value, maximum=STORED_ITEM_NAME_BYTES, *, empty=True):
    if type(value) is not str or (not empty and not value) or len(value.encode("utf-8")) > maximum:
        raise ValueError("Expected bounded resource text")
    dumps(Container(children=(Column("Value", "S", (value,)),)))
    return value


def _version(content, eligibility):
    return hashlib.sha256(json.dumps({**content, "eligibility": eligibility}, sort_keys=True).encode()).hexdigest()


def _date(value):
    _text(value, 32)
    if not value:
        return None
    match = NATIVE_MINUTES_TEXT.fullmatch(value)
    if match is None:
        raise ValueError("Pass dates require complete native timestamps")
    year, _, month, day, hour, minute, second = match.groups()
    return datetime(*(int(part) for part in (year, month, day, hour, minute, second or "0")), tzinfo=JST)


@dataclass(frozen=True)
class EventPassReward:
    reward_id: int
    required_point: int
    premium: bool
    reward_item_type: int
    param: int
    item_id: str
    source_index: int

    @property
    def reward_item_param(self):
        return self.param

    @property
    def reward_item_id(self):
        return self.item_id


@dataclass(frozen=True)
class EventPass:
    item_id: str
    premium_item_id: str
    start_at: datetime
    end_at: datetime
    time_limit: str
    status: int
    rewards: tuple[EventPassReward, ...]

    # 63EFD0 compares the client clock with AACF90's reading of each date, which is that clock's reading of its second.
    @property
    def native_start_at(self):
        return native_clock(self.start_at)

    @property
    def native_end_at(self):
        return native_clock(self.end_at)


class EventPassCatalog:
    def __init__(self, asset_reader, possessions=None):
        self._possessions = possessions
        try:
            if not callable(asset_reader):
                raise ValueError("Event passes require a resource reader")
            hashes = {}

            def table(name, expected, *, exact=False):
                raw = asset_reader(name)
                if type(raw) is not bytes or not 0 < len(raw) <= ITEM_RESOURCE_LIMIT:
                    raise ValueError("Pass resources must be bounded immutable bytes")
                data = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
                parsed = parse_table(data, LIMITS)
                fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
                if (len(fields) != len(parsed["fields"]) or any(fields.get(name) != kind for name, kind in expected.items())
                        or (exact and fields != expected)):
                    raise ValueError("Pass fields differ from the verified native schema")
                hashes[name] = hashlib.sha256(data).hexdigest()
                return parsed["rows"]

            items, identifiers = {}, set()
            for row in table(ITEM_RESOURCE_NAME, ITEM_FIELDS):
                name = _text(row["ItemID"], empty=False)
                identifier = row["ID"]
                if type(identifier) is not int or identifier != command_id(name) or name in items or identifier in identifiers:
                    raise ValueError("Item identities are invalid or duplicated")
                _integer(row["Stack"])
                items[name] = row
                identifiers.add(identifier)
            if not items or len(items) > MAX_POSSESSION_ITEMS:
                raise ValueError("Pass item definitions must be bounded and nonempty")

            def verify_item(name, premium):
                row = items.get(name)
                if row is None:
                    raise ValueError("Pass refers to an unknown item")
                expected = {"EffectType": 14 if premium else 0, "Category": CATEGORY,
                            "Stack": 1 if premium else 999, "CanPresent": 0, "CanAllGet": 0,
                            "ReceiveAction": 0, "NoAutoDelete": 0, "ReceiveConfirmation": 0,
                            "InvisibleContaint": 0, "SummonCount": 0, "SummonConfigID": 0,
                            "LotteryRuleID": 0}
                if any(type(row[key]) is not int or row[key] != value for key, value in expected.items()):
                    raise ValueError("Pass inventory semantics differ from the verified contract")
                if _text(row["EventID"], 512):
                    raise ValueError("Pass inventory must not invoke an event")

            groups, seen_rewards, pass_items = [], set(), set()
            start_at = end_at = None
            time_limit = ""
            status = point = 0
            current = None
            for index, row in enumerate(table(RESOURCE_NAME, FIELDS, exact=True)):
                reward_id = _integer(row["ID"], 1)
                if reward_id in seen_rewards:
                    raise ValueError("Pass reward identifiers are duplicated")
                seen_rewards.add(reward_id)
                start_at = _date(row["StartDate"]) or start_at
                end_at = _date(row["EndDate"]) or end_at
                time_limit = _text(row["TimeLimit"], MAX_TIME_LIMIT_BYTES) or time_limit
                status = _integer(row["Status"]) or status
                point = _integer(row["RequiredPoint"]) or point
                normal = _text(row["NormalPassItemID"])
                premium = _text(row["PremiumPassItemID"])
                if start_at is None or end_at is None or start_at > end_at or not status or not point:
                    raise ValueError("Pass rows require inherited dates, status and a positive threshold")
                metadata = (start_at, end_at, time_limit, status)
                if normal:
                    if normal in pass_items:
                        raise ValueError("Pass groups are duplicated")
                    verify_item(normal, False)
                    pass_items.add(normal)
                    current = {"item_id": normal, "premium_item_id": "", "metadata": metadata, "rewards": []}
                    groups.append(current)
                if current is None:
                    raise ValueError("Pass continuation precedes its group")
                if current["metadata"] != metadata:
                    raise ValueError("Mixed availability within one pass is unsupported")
                if premium:
                    verify_item(premium, True)
                    if current["premium_item_id"] not in ("", premium):
                        raise ValueError("A pass cannot mix premium ownership items")
                    current["premium_item_id"] = premium
                kind = _integer(row["RewardItemType"])
                param = _integer(row["RewardItemParam"], 1)
                item_id = _text(row["RewardItemID"])
                if kind not in (0, 1, 2, 22):
                    raise ValueError("Pass reward type is not verified")
                if kind == 22:
                    if item_id not in items:
                        raise ValueError("Pass reward item is unknown")
                elif item_id:
                    raise ValueError("Currency and unit rewards require only a positive parameter")
                current["rewards"].append(EventPassReward(reward_id, point, bool(premium), kind, param, item_id, index))
            if not groups:
                raise ValueError("Event pass definitions are empty")
            premium_items = [group["premium_item_id"] for group in groups if group["premium_item_id"]]
            if len(set(premium_items)) != len(premium_items) or pass_items.intersection(premium_items):
                raise ValueError("Pass ownership items must not be shared by different groups")
            self._passes = tuple(EventPass(group["item_id"], group["premium_item_id"],
                *group["metadata"], tuple(group["rewards"])) for group in groups)
            self._by_id = MappingProxyType({entry.item_id: entry for entry in self._passes})
            self._resource_hashes = MappingProxyType(hashes)
            self._stacks = MappingProxyType({name: items[name]["Stack"] for name in pass_items | set(premium_items)})
            self._ready()
            content = {"schema": 1, "resources": hashes, "grouping": "normal_nonempty_header",
                       "inherit": "dates_time_limit_status_point", "premium": "per_row",
                       "time_limit": "display_text", "maximum_time_limit_bytes": MAX_TIME_LIMIT_BYTES,
                       "mixed_availability": "unsupported", "reward_types": [0, 1, 2, 22]}
            self._content_version = _version(content, ELIGIBILITY_RULE)
            self._legacy_window_content_version = _version(content, LEGACY_ELIGIBILITY_RULE)
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError, ProtocolError) as error:
            raise ProtocolError("event_pass_catalog_invalid", "Event pass resources differ from the verified contract", 503) from error

    @property
    def passes(self):
        return self._passes

    @property
    def by_id(self):
        return self._by_id

    @property
    def content_version(self):
        return self._content_version

    @property
    def legacy_window_content_version(self):
        return self._legacy_window_content_version

    def replayable_content_versions(self):
        return frozenset((self._content_version, self._legacy_window_content_version))

    @property
    def resource_hashes(self):
        return self._resource_hashes

    def _ready(self):
        if self._possessions is None:
            return
        try:
            if (not self._possessions.is_ready()
                    or self._possessions.asset_sha256 != self._resource_hashes[ITEM_RESOURCE_NAME]
                    or any(self._possessions.stack(name) != stack for name, stack in self._stacks.items())):
                raise ValueError("Possession resource changed")
        except (AttributeError, KeyError, TypeError, ValueError, ProtocolError) as error:
            raise ProtocolError("event_pass_catalog_changed", "Pass and possession definitions must remain identical", 503) from error

    def get(self, item_id):
        self._ready()
        if type(item_id) is not str or item_id not in self._by_id:
            raise ProtocolError("event_pass_unknown", "Event pass is not in the current catalog", 501)
        return self._by_id[item_id]

    @staticmethod
    def _within(entry, current):
        return entry.status == 1 and entry.native_start_at <= current <= entry.native_end_at

    def is_eligible(self, entry, now):
        self._ready()
        if not isinstance(entry, EventPass) or self._by_id.get(entry.item_id) != entry:
            raise ProtocolError("event_pass_unknown", "Event pass does not belong to this catalog", 501)
        return self._within(entry, native_clock(instant(now)))

    def eligible(self, now):
        self._ready()
        current = native_clock(instant(now))
        return tuple(entry for entry in self._passes if self._within(entry, current))

    def status(self):
        self._ready()
        return {"ready": True, "pass_count": len(self._passes),
                "reward_count": sum(len(entry.rewards) for entry in self._passes),
                "status_counts": dict(Counter(entry.status for entry in self._passes)),
                "resource_hashes": dict(self._resource_hashes), "content_version": self.content_version,
                "eligibility": "Status 1 and inclusive StartDate/EndDate on the whole-second JST client clock",
                "time_limit": "Display metadata; not a claim cutoff",
                "inventory": "Normal Count is accumulated points; positive premium Count unlocks premium rewards",
                "name_policy": (f"Item names up to {STORED_ITEM_NAME_BYTES} bytes stay readable; new pass, inventory and "
                                f"mail rows need the {MAX_ITEM_NAME_BYTES}-byte native cell"),
                "native_tested": False}
