from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, dumps
from .errors import ProtocolError
from .event_login_rules import MAX_CAMPAIGNS
from .native_numbers import UINT32_MASK, int32


RESOURCE_NAME = "EventStampItemData.atb"
FIELDS = {name: 1 for name in ("LoginBonusID", "Character", "CharacterOffsetX", "CharacterOffsetY", "Message",
    "LoginCount", "ItemType", "ItemParam", "_Param1", "Caption", "LoginBonusType", "Expiration", "ConfigID",
    "EvolutionCount", "PausePeriod", "CharacterEvolutionId", "CharacterCostumeId")}
FIELDS.update({name: 32 for name in ("MenuName", "Background", "Logo", "ItemID", "Icon")})
FIELDS.update(StartDate=7, EndDate=7)
LIMITS = ParseLimits(max_input_bytes=16 * 1024 * 1024, max_decompressed_bytes=16 * 1024 * 1024,
                     max_rows=65535, max_fields=64, max_cells=2_000_000)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _day(value):
    if type(value) is not int or not 0 <= value <= UINT32_MASK:
        raise ValueError("Event login dates require unsigned native day values")
    return date.fromordinal(value + 1) if value else None


@dataclass(frozen=True)
class EventLoginReward:
    ordinal: int
    config_id: int
    item_type: int
    item_id: str
    item_param: int
    param1: int
    evolution_count: int
    pause_period: int
    source_index: int


@dataclass(frozen=True)
class EventLoginCampaign:
    campaign_id: int
    start_date: date | None
    end_date: date | None
    login_bonus_type: int
    expiration: int
    rewards: tuple[EventLoginReward, ...]
    max_ordinal: int
    source_index: int
    header: MappingProxyType
    definition_version: str
    unsupported_reason: str | None

    def rewards_for(self, ordinal: int) -> tuple[EventLoginReward, ...]:
        if not int32(ordinal) or ordinal <= 0:
            raise ProtocolError("event_login_ordinal_invalid", "Event login ordinal must be a positive integer", 400)
        return tuple(reward for reward in self.rewards if reward.ordinal == ordinal and reward.pause_period == 0)


class EventLoginCatalog:
    def __init__(self, asset_reader):
        try:
            if not callable(asset_reader):
                raise ValueError("Event login catalog requires a resource reader")
            raw = asset_reader(RESOURCE_NAME)
            if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
                raise ValueError("Event login resource must be bounded immutable bytes")
            data = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
            table = parse_table(data, LIMITS)
            fields = {field["name"]: field["type_code"] for field in table["fields"]}
            if fields != FIELDS or len(fields) != len(table["fields"]) or not table["rows"]:
                raise ValueError("Event login fields differ from the native schema")
            groups, ids, current, config_ids = [], set(), None, set()
            ordinal = 0
            for index, row in enumerate(table["rows"]):
                for name, kind in FIELDS.items():
                    value = row[name]
                    if kind == 1 and not int32(value):
                        raise ValueError("Event login integers must be native int32 values")
                    if kind == 32:
                        if type(value) is not str or len(value.encode("utf-8")) > 4096:
                            raise ValueError("Event login strings must be bounded text")
                        dumps(Container(children=(Column("Value", "S", (value,)),)))
                    if kind == 7:
                        _day(value)
                identifier = row["LoginBonusID"]
                if identifier < 0 or any(row[key] < 0 for key in ("LoginCount", "PausePeriod", "Expiration", "LoginBonusType")):
                    raise ValueError("Event login identities and counters must be nonnegative")
                if identifier and (current is None or identifier != current["header"]["LoginBonusID"]):
                    if identifier in ids or len(groups) >= MAX_CAMPAIGNS:
                        raise ValueError("Event login campaign headers must be unique and bounded")
                    ids.add(identifier)
                    current = {"header": dict(row), "source_index": index, "rows": [], "rewards": []}
                    groups.append(current)
                    ordinal = 0
                if current is None:
                    raise ValueError("Event login continuation has no campaign header")
                if row["LoginCount"]:
                    ordinal = row["LoginCount"]
                if ordinal <= 0 or row["ConfigID"] <= 0 or row["ConfigID"] in config_ids:
                    raise ValueError("Event login rewards require valid day and unique config identities")
                config_ids.add(row["ConfigID"])
                current["rows"].append(dict(row))
                current["rewards"].append(EventLoginReward(ordinal, row["ConfigID"], row["ItemType"],
                    row["ItemID"], row["ItemParam"], row["_Param1"], row["EvolutionCount"], row["PausePeriod"], index))
            campaigns = []
            for group in groups:
                header, rewards = group["header"], tuple(group["rewards"])
                start, end = _day(header["StartDate"]), _day(header["EndDate"])
                if start is not None and end is not None and start >= end:
                    raise ValueError("Event login campaign start must precede its end")
                reason = None
                if header["LoginBonusType"] != 0:
                    reason = "nonpublic_eligibility"
                elif header["Expiration"] or any(reward.pause_period for reward in rewards):
                    reason = "relative_or_pause_eligibility"
                elif any(reward.evolution_count for reward in rewards):
                    reason = "reward_evolution_unverified"
                elif start is None or end is None:
                    reason = "requires_explicit_window_and_eligibility"
                if header["LoginBonusType"] == 0:
                    ordinals = sorted({reward.ordinal for reward in rewards})
                    if ordinals != list(range(1, len(ordinals) + 1)):
                        raise ValueError("Public event login days must be contiguous from one")
                campaigns.append(EventLoginCampaign(header["LoginBonusID"], start, end,
                    header["LoginBonusType"], header["Expiration"], rewards, max(reward.ordinal for reward in rewards),
                    group["source_index"], MappingProxyType(header), _digest(group["rows"]), reason))
            self._campaigns = tuple(campaigns)
            self._by_id = MappingProxyType({campaign.campaign_id: campaign for campaign in campaigns})
            self.asset_sha256 = hashlib.sha256(data).hexdigest()
            self.resource_sha256 = hashlib.sha256(raw).hexdigest()
            self._content_version = _digest({"policy": "public_fixed_window_start_inclusive_end_exclusive_v1",
                                            "resource": self.asset_sha256})
            self._snapshot = self._campaigns, self._by_id, self.asset_sha256, self.resource_sha256, self._content_version
        except (AttributeError, KeyError, TypeError, ValueError, OSError, OverflowError) as error:
            raise ProtocolError("event_login_catalog_invalid", "Event login resource is invalid or unsupported", 503) from error

    @property
    def campaigns(self):
        return self._campaigns

    @property
    def by_id(self):
        return self._by_id

    @property
    def ordinary_campaigns(self):
        return tuple(campaign for campaign in self.campaigns if campaign.login_bonus_type == 0)

    @property
    def content_version(self):
        return self._content_version

    def _ready(self):
        campaigns, indexed, asset_hash, resource_hash, version = self._snapshot
        if (self._campaigns is not campaigns or self._by_id is not indexed or self.asset_sha256 != asset_hash
                or self.resource_sha256 != resource_hash or self._content_version != version):
            raise ProtocolError("event_login_catalog_changed", "Event login catalog changed after construction", 503)

    def get(self, campaign_id):
        self._ready()
        if not int32(campaign_id) or campaign_id <= 0:
            raise ProtocolError("event_login_campaign_invalid", "Campaign ID must be a positive integer", 400)
        if campaign_id not in self.by_id:
            raise ProtocolError("event_login_campaign_unknown", "Campaign is absent from the event login resource", 404)
        return self.by_id[campaign_id]

    def require_public(self, campaign_id):
        campaign = self.get(campaign_id)
        if campaign.unsupported_reason:
            raise ProtocolError("event_login_campaign_unsupported", campaign.unsupported_reason, 501)
        return campaign

    def active(self, day: date, campaign_ids=None):
        self._ready()
        if type(day) is not date:
            raise ProtocolError("event_login_day_invalid", "Event login selection requires a logical calendar day", 400)
        if campaign_ids is not None:
            if not isinstance(campaign_ids, (tuple, list)) or len(campaign_ids) > MAX_CAMPAIGNS:
                raise ProtocolError("event_login_campaign_invalid", "Campaign selection must be a bounded sequence", 400)
            selected = [self.require_public(identifier) for identifier in campaign_ids]
            if len({campaign.campaign_id for campaign in selected}) != len(selected):
                raise ProtocolError("event_login_campaign_invalid", "Campaign selection must not contain duplicates", 400)
            chosen = {campaign.campaign_id for campaign in selected}
        else:
            chosen = None
        result = []
        for campaign in self.ordinary_campaigns:
            if chosen is not None and campaign.campaign_id not in chosen:
                continue
            if chosen is None and campaign.unsupported_reason is not None:
                continue
            if campaign.start_date is None or campaign.end_date is None:
                continue
            if campaign.start_date <= day < campaign.end_date:
                result.append(self.require_public(campaign.campaign_id))
        return tuple(result)

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "content_version": self.content_version, "resource_sha256": self.resource_sha256,
                "campaign_count": len(self.campaigns), "ordinary_count": len(self.ordinary_campaigns),
                "reward_count": sum(len(campaign.rewards) for campaign in self.campaigns),
                "public_count": sum(campaign.unsupported_reason is None for campaign in self.campaigns),
                "unsupported_campaigns": {campaign.campaign_id: campaign.unsupported_reason
                    for campaign in self.campaigns if campaign.unsupported_reason},
                "window_policy": "Local rule: start <= JST04 logical day < end; missing bounds require explicit eligibility"}
