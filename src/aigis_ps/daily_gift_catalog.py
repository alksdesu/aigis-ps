from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, dumps
from .daily_gift_rules import DailyGiftRules, MAX_GIFTS
from .errors import ProtocolError
from .native_numbers import int32
from .possessions import STORED_ITEM_NAME_BYTES


RESOURCE_NAME = "DailyGiftConfig.atb"
TEXT_RESOURCE = "SystemText.atb"
FIELDS = {name: 1 for name in ("ID_DailyGift", "ID_SystemText", "Days_Begin", "Days_End", "ID_Gift",
    "Type_Gift", "Type_Item", "Param_Gift", "TypeCondition", "ParamCondition")}
FIELDS["ID_Item"] = 32
LIMITS = ParseLimits(max_input_bytes=16 * 1024 * 1024, max_decompressed_bytes=16 * 1024 * 1024,
                     max_rows=65535, max_fields=64, max_cells=2_000_000)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _source_date(value):
    if type(value) is not int or value <= 0:
        raise ValueError("Daily gift dates require positive YYYYMMDD integers")
    year, rest = divmod(value, 10000)
    month, day = divmod(rest, 100)
    return date(year, month, day)


def _text(value, maximum):
    if type(value) is not str or len(value.encode("utf-8")) > maximum:
        raise ValueError("Daily gift text exceeds its native field bound")
    dumps(Container(children=(Column("Value", "S", (value,)),)))
    return value


@dataclass(frozen=True)
class DailyGiftReward:
    gift_entry_id: int
    gift_type: int
    item_type: int
    parameter: int
    item_id: str
    condition_type: int
    condition_parameter: int
    source_index: int = -1


@dataclass(frozen=True)
class DailyGiftCampaign:
    gift_id: int
    start_date: date
    end_date: date
    description: str
    rewards: tuple[DailyGiftReward, ...]
    source_start_date: date
    source_end_date: date
    source_index: int
    definition_version: str


class DailyGiftCatalog:
    def __init__(self, asset_reader, rules=None):
        try:
            self.rules = DailyGiftRules() if rules is None else rules
            if not isinstance(self.rules, DailyGiftRules) or not callable(asset_reader):
                raise ValueError("Daily gift catalog requires verified rules and a resource reader")
            hashes, snapshots = {}, {}

            def table(name, expected):
                raw = asset_reader(name)
                if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
                    raise ValueError("Daily gift resources must be bounded immutable bytes")
                parsed = parse_table(decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw, LIMITS)
                fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
                if len(fields) != len(parsed["fields"]) or any(fields.get(key) != kind for key, kind in expected.items()):
                    raise ValueError("Daily gift resource fields differ from the native schema")
                if name == RESOURCE_NAME and fields != FIELDS:
                    raise ValueError("Daily gift resource contains unknown fields")
                hashes[name], snapshots[name] = hashlib.sha256(raw).hexdigest(), raw
                return parsed["rows"]

            rows = table(RESOURCE_NAME, FIELDS)
            texts = table(TEXT_RESOURCE, {"Recode_Index": 1, "Data_Text": 32})
            if not rows:
                raise ValueError("Daily gift source must not be empty")
            groups, current, entry_key = {}, None, 0
            for index, row in enumerate(rows):
                if any(not int32(row[name]) for name, kind in FIELDS.items() if kind == 1):
                    raise ValueError("Daily gift integers must be native int32 values")
                identifier, entry_id, text_id = row["ID_DailyGift"], row["ID_Gift"], row["ID_SystemText"]
                if identifier < 0 or entry_id < 0:
                    raise ValueError("Daily gift identities must be nonnegative")
                item_id = _text(row["ID_Item"], STORED_ITEM_NAME_BYTES)
                if identifier and identifier != current:
                    if identifier in groups:
                        raise ValueError("Daily gift groups must not reappear after another group")
                    if len(groups) >= MAX_GIFTS:
                        raise ValueError("Daily gift campaign count exceeds its bound")
                    if not 0 <= text_id < len(texts):
                        raise ValueError("Daily gift physical text index is invalid")
                    start, end = _source_date(row["Days_Begin"]), _source_date(row["Days_End"])
                    if start > end:
                        raise ValueError("Daily gift start must not follow its end")
                    description = _text(texts[text_id]["Data_Text"], 4096)
                    groups[identifier] = {"header": (start, end, text_id), "description": description,
                                          "index": index, "rows": [], "rewards": []}
                    current, entry_key = identifier, 0
                if current is None:
                    raise ValueError("Daily gift continuation requires an outer group header")
                if entry_id:
                    entry_key = entry_id
                group = groups[current]
                group["rows"].append(dict(row))
                group["rewards"].append(DailyGiftReward(entry_key, row["Type_Gift"], row["Type_Item"],
                    row["Param_Gift"], item_id, row["TypeCondition"], row["ParamCondition"], index))
            requested = set(self.rules.gift_ids or ()) | set(self.rules.revivals)
            if requested - groups.keys():
                raise ValueError("Daily gift configuration references unknown source IDs")
            campaigns = []
            for identifier, group in sorted(groups.items()):
                source_start, source_end, _ = group["header"]
                window = self.rules.revivals.get(identifier)
                start = date.fromisoformat(window["start_date"]) if window else source_start
                end = date.fromisoformat(window["end_date"]) if window else source_end
                campaigns.append(DailyGiftCampaign(identifier, start, end, group["description"],
                    tuple(sorted(group["rewards"], key=lambda value: value.gift_entry_id)), source_start, source_end,
                    group["index"], _digest(group["rows"])))
            self._campaigns = tuple(campaigns)
            self._by_id = MappingProxyType({campaign.gift_id: campaign for campaign in campaigns})
            self.resource_hashes = MappingProxyType(hashes)
            self._resources = MappingProxyType(snapshots)
            self._content_version = _digest({"policy": "local_daily_gift_inclusive_dates_v1", "resources": hashes,
                                            "rules": self.rules.to_dict()})
            self._snapshot = self.rules, self._campaigns, self._by_id, self.resource_hashes, self._resources, self._content_version
        except (AttributeError, KeyError, TypeError, ValueError, OSError, OverflowError) as error:
            raise ProtocolError("daily_gift_catalog_invalid", "Daily gift resource or configuration is invalid", 503) from error

    @property
    def campaigns(self):
        return self._campaigns

    @property
    def by_id(self):
        return self._by_id

    @property
    def content_version(self):
        return self._content_version

    def _ready(self):
        rules, campaigns, indexed, hashes, resources, version = self._snapshot
        if (self.rules is not rules or self._campaigns is not campaigns or self._by_id is not indexed
                or self.resource_hashes is not hashes or self._resources is not resources or self._content_version != version):
            raise ProtocolError("daily_gift_catalog_changed", "Daily gift catalog changed after construction", 503)

    def get(self, gift_id):
        self._ready()
        if not int32(gift_id) or gift_id <= 0:
            raise ProtocolError("daily_gift_id_invalid", "Daily gift ID must be a positive integer", 400)
        if gift_id not in self.by_id:
            raise ProtocolError("daily_gift_unknown", "Daily gift is absent from the source", 404)
        return self.by_id[gift_id]

    def active(self, day, gift_ids=None):
        self._ready()
        if type(day) is not date:
            raise ProtocolError("daily_gift_day_invalid", "Daily gifts require a logical calendar day", 400)
        selected = None
        if gift_ids is not None:
            if not isinstance(gift_ids, (tuple, list)) or len(gift_ids) > MAX_GIFTS:
                raise ProtocolError("daily_gift_selection_invalid", "Daily gift selection must be a bounded sequence", 400)
            selected = {self.get(identifier).gift_id for identifier in gift_ids}
            if len(selected) != len(gift_ids):
                raise ProtocolError("daily_gift_selection_invalid", "Daily gift IDs must not repeat", 400)
        if not self.rules.enabled:
            return ()
        configured = None if self.rules.gift_ids is None else set(self.rules.gift_ids)
        return tuple(campaign for campaign in self.campaigns if campaign.start_date <= day <= campaign.end_date
                     and (selected is None or campaign.gift_id in selected)
                     and (configured is None or campaign.gift_id in configured))

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "content_version": self.content_version,
                "resource_hashes": dict(self.resource_hashes), "rules": self.rules.to_dict(),
                "campaign_count": len(self.campaigns), "reward_count": sum(len(c.rewards) for c in self.campaigns),
                "revival_count": len(self.rules.revivals),
                "group_policy": "Contiguous outer groups; zero identities inherit, repeated inner keys append rewards",
                "window_policy": "Local rule: start <= JST04 logical day <= end; no historical catch-up"}
