from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from types import MappingProxyType

from .errors import ProtocolError
from .event_login_catalog import EventLoginCampaign, EventLoginReward
from .native_numbers import int32


REWARD_FIELDS = ("ordinal", "config_id", "item_type", "item_id", "item_param", "param1",
                 "evolution_count", "pause_period")
MAX_BANDS = 256


def _integer(value, minimum=0):
    return int32(value) and value >= minimum


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class ComebackLoginBand:
    campaign_id: int
    pause_period: int
    rewards: tuple[EventLoginReward, ...]
    max_ordinal: int
    definition_version: str

    def rewards_for(self, ordinal):
        if not _integer(ordinal, 1):
            raise ProtocolError("comeback_login_ordinal_invalid", "Comeback login ordinal must be a positive integer")
        return tuple(reward for reward in self.rewards if reward.ordinal == ordinal)


class ComebackLoginCatalog:
    def __init__(self, event_catalog, *, campaign_id=6):
        self.event_catalog = event_catalog
        try:
            event_catalog._ready()
            if not _integer(campaign_id, 1):
                raise ValueError("Comeback campaign ID must be a positive integer")
            campaign = event_catalog.get(campaign_id)
            if (not isinstance(campaign, EventLoginCampaign) or campaign.login_bonus_type != 1
                    or not _integer(campaign.expiration, 1) or campaign.start_date is None):
                raise ValueError("Comeback campaign requires Type1, a source start and positive duration")
            grouped = defaultdict(list)
            last_ordinals, config_ids = {}, set()
            for reward in campaign.rewards:
                if (not isinstance(reward, EventLoginReward) or not _integer(reward.pause_period)
                        or not _integer(reward.ordinal, 1) or not _integer(reward.config_id, 1)
                        or reward.config_id in config_ids):
                    raise ValueError("Comeback reward groups are invalid or duplicated")
                last_ordinal = last_ordinals.get(reward.pause_period, 0)
                if reward.ordinal not in (last_ordinal, last_ordinal + 1):
                    raise ValueError("Comeback reward days must be contiguous within each band")
                grouped[reward.pause_period].append(reward)
                last_ordinals[reward.pause_period] = reward.ordinal
                config_ids.add(reward.config_id)
            if not grouped or 0 not in grouped or len(grouped) > MAX_BANDS:
                raise ValueError("Comeback reward bands require a bounded set with a zero fallback")
            bands = {}
            for pause, rewards in sorted(grouped.items()):
                values = [{field: getattr(reward, field) for field in REWARD_FIELDS} for reward in rewards]
                fingerprint = _digest({"campaign_id": campaign_id, "pause_period": pause, "rewards": values})
                bands[pause] = ComebackLoginBand(campaign_id, pause, tuple(rewards), rewards[-1].ordinal, fingerprint)
            self.campaign_id = campaign_id
            self.start_date, self.end_date, self.expiration = campaign.start_date, campaign.end_date, campaign.expiration
            self._bands = MappingProxyType(bands)
            self._source = event_catalog, event_catalog.content_version, campaign
            self._snapshot = self.campaign_id, self.start_date, self.end_date, self.expiration, self._bands
            self._content_version = _digest({"policy": "max_pause_lte_gap_source_duration_v1",
                "campaign_id": campaign_id, "source": event_catalog.content_version,
                "start": self.start_date.isoformat(), "end": self.end_date.isoformat() if self.end_date else None,
                "expiration": self.expiration, "bands": {key: band.definition_version for key, band in bands.items()}})
        except (AttributeError, ValueError, TypeError, KeyError, ProtocolError) as error:
            raise ProtocolError("comeback_login_catalog_invalid", "Comeback login resources are unavailable or inconsistent", 503) from error

    @property
    def bands(self):
        return self._bands

    @property
    def content_version(self):
        return self._content_version

    def _ready(self):
        catalog, version, campaign = self._source
        catalog._ready()
        if (self.event_catalog is not catalog or catalog.content_version != version
                or catalog.get(self.campaign_id) is not campaign
                or (self.campaign_id, self.start_date, self.end_date, self.expiration) != self._snapshot[:4]
                or self._bands is not self._snapshot[4]):
            raise ProtocolError("comeback_login_catalog_changed", "Comeback login definitions changed after setup", 503)

    def require_band(self, pause_period):
        self._ready()
        if not _integer(pause_period) or pause_period not in self._bands:
            raise ProtocolError("comeback_login_band_unknown", "Comeback reward band is not in the source catalog", 503)
        return self._bands[pause_period]

    def choose(self, absence_days):
        self._ready()
        if not _integer(absence_days):
            raise ProtocolError("comeback_login_absence_invalid", "Comeback absence must be a nonnegative int32")
        return self._bands[max(pause for pause in self._bands if pause <= absence_days)]

    def is_available(self, day):
        self._ready()
        if type(day) is not date:
            raise ProtocolError("comeback_login_day_invalid", "Comeback availability requires a logical calendar day")
        return self.start_date <= day and (self.end_date is None or day < self.end_date)

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "campaign_id": self.campaign_id,
                "start_date": self.start_date.isoformat(),
                "end_date": self.end_date.isoformat() if self.end_date else None,
                "expiration": self.expiration, "content_version": self.content_version,
                "bands": {pause: {"days": band.max_ordinal, "rewards": len(band.rewards)}
                          for pause, band in self._bands.items()},
                "band_policy": "Greatest source PausePeriod not exceeding absence days",
                "window_policy": "Local start-inclusive end-exclusive logical dates; source duration unchanged"}
