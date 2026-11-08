from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, dumps
from .business_calendar import NATIVE_SECONDS_TEXT
from .energy_clock import JST, instant
from .errors import ProtocolError
from .native_numbers import int32
from .possessions import MAX_ITEM_NAME_BYTES


RESOURCE_NAME = "SugorokuRewardConfig.atb"
RESOURCE_LIMIT = 8 * 1024 * 1024
LIMITS = ParseLimits(max_input_bytes=RESOURCE_LIMIT, max_decompressed_bytes=RESOURCE_LIMIT,
                     max_rows=65535, max_fields=64)
FIELDS = {"RewardID": 1, "StartDateTime": 32, "EndDateTime": 32, "ConsumeCrystal": 1,
          "RewardType": 1, "GeneralItemID": 32, "CardID": 1, "Count": 1}


def _integer(value, minimum=0):
    if not int32(value) or value < minimum:
        raise ValueError("Expected a bounded native integer")
    return value


def _text(value, maximum, *, empty=True):
    if type(value) is not str or (not value and not empty) or len(value.encode("utf-8")) > maximum:
        raise ValueError("Expected bounded resource text")
    dumps(Container(children=(Column("Value", "S", (value,)),)))
    return value


def _date(value):
    _text(value, 32)
    if not value:
        return None
    match = NATIVE_SECONDS_TEXT.fullmatch(value)
    if match is None:
        raise ValueError("Reward dates require complete native timestamps")
    year, _, month, day, hour, minute, second = match.groups()
    return datetime(*(int(part) for part in (year, month, day, hour, minute, second)), tzinfo=JST)


def _clock(now):
    try:
        return instant(now).astimezone(JST)
    except (ValueError, TypeError, OverflowError, ProtocolError) as error:
        raise ProtocolError("sugoroku_time_invalid", "Reward lookup requires a valid clock instant") from error


@dataclass(frozen=True)
class SugorokuReward:
    reward_id: int
    start_at: datetime | None
    end_at: datetime | None
    consume_crystal: int
    reward_type: int
    item_id: str
    card_id: int
    count: int
    source_order: int
    unsupported_reason: str | None

    @property
    def supported(self) -> bool:
        return self.unsupported_reason is None


class SugorokuCatalog:
    def __init__(self, asset_reader, *, resource_version=None):
        try:
            if not callable(asset_reader):
                raise ValueError("Rewards require a resource reader")
            if resource_version is not None:
                _text(resource_version, 128, empty=False)
            raw = asset_reader(RESOURCE_NAME)
            if type(raw) is not bytes or not 0 < len(raw) <= RESOURCE_LIMIT:
                raise ValueError("Reward resources must be bounded immutable bytes")
            data = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
            parsed = parse_table(data, LIMITS)
            fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
            if fields != FIELDS or len(parsed["fields"]) != len(FIELDS):
                raise ValueError("Reward fields differ from the verified native schema")
            entries, identifiers = [], set()
            for order, row in enumerate(parsed["rows"]):
                reward_id = _integer(row["RewardID"], 1)
                if reward_id in identifiers:
                    raise ValueError("Reward identifiers are duplicated")
                identifiers.add(reward_id)
                threshold = _integer(row["ConsumeCrystal"])
                reward_type = _integer(row["RewardType"])
                card_id = _integer(row["CardID"])
                count = _integer(row["Count"], 1 if reward_type in range(1, 7) else 0)
                item_id = _text(row["GeneralItemID"], MAX_ITEM_NAME_BYTES)
                start_at, end_at = _date(row["StartDateTime"]), _date(row["EndDateTime"])
                if start_at is not None and end_at is not None and start_at >= end_at:
                    raise ValueError("Reward availability window is empty or inverted")
                reason = None
                if reward_type == 1:
                    if not item_id or card_id:
                        raise ValueError("Item rewards require only a general item identifier")
                elif reward_type == 2:
                    if item_id or not card_id:
                        raise ValueError("Unit rewards require only a positive card identifier")
                elif reward_type in (3, 4, 5, 6):
                    if item_id or card_id:
                        raise ValueError("Currency and capacity rewards have no item or card identifier")
                    if reward_type == 3:
                        reason = "unverified_paid_free"
                    elif reward_type == 6:
                        reason = "capacity_delivery_unverified"
                else:
                    reason = "reward_type_unsupported"
                entries.append(SugorokuReward(reward_id, start_at, end_at, threshold, reward_type,
                                               item_id, card_id, count, order, reason))
            if not entries:
                raise ValueError("Reward definitions are empty")
            self._entries = tuple(entries)
            self._by_id = MappingProxyType({entry.reward_id: entry for entry in entries})
            self._resource_version = resource_version
            self._resource_sha256 = hashlib.sha256(raw).hexdigest()
            self._decoded_sha256 = hashlib.sha256(data).hexdigest()
            content = {"schema": 1, "resource_version": resource_version, "resource_sha256": self._resource_sha256,
                "window": "inclusive_start_exclusive_end_jst", "order": "threshold_then_source",
                "known_types": {1: "item", 2: "unit", 3: "unverified_paid_free", 4: "gold",
                                5: "local_next_month_charisma_capacity", 6: "capacity_delivery_unverified"},
                "unknown": "preserve_unsupported", "fulfillment": "external_service"}
            self._content_version = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError, ProtocolError) as error:
            raise ProtocolError("sugoroku_catalog_invalid", "Reward resources differ from the verified contract", 503) from error

    @property
    def entries(self) -> tuple[SugorokuReward, ...]:
        return self._entries

    @property
    def by_id(self):
        return self._by_id

    @property
    def content_version(self) -> str:
        return self._content_version

    @property
    def resource_version(self) -> str | None:
        return self._resource_version

    @property
    def resource_sha256(self) -> str:
        return self._resource_sha256

    def get(self, reward_id: int) -> SugorokuReward:
        if type(reward_id) is not int or reward_id not in self._by_id:
            raise ProtocolError("sugoroku_reward_unknown", "Reward identifier is not in the current catalog")
        return self._by_id[reward_id]

    @staticmethod
    def _within(entry, now):
        return (entry.start_at is None or entry.start_at <= now) and (entry.end_at is None or now < entry.end_at)

    def is_active(self, entry: SugorokuReward, now) -> bool:
        if not isinstance(entry, SugorokuReward) or self._by_id.get(entry.reward_id) != entry:
            raise ProtocolError("sugoroku_reward_unknown", "Reward definition does not belong to this catalog")
        return self._within(entry, _clock(now))

    def active(self, now) -> tuple[SugorokuReward, ...]:
        current = _clock(now)
        return tuple(sorted((entry for entry in self._entries if self._within(entry, current)),
                            key=lambda entry: (entry.consume_crystal, entry.source_order)))

    def is_ready(self) -> bool:
        # Construction refuses unverified resources, so every built catalog is ready.
        return True

    def status(self) -> dict:
        return {"ready": self.is_ready(), "reward_count": len(self._entries), "type_counts": dict(Counter(
            entry.reward_type for entry in self._entries)), "resource_version": self.resource_version,
            "resource_sha256": self.resource_sha256, "decoded_sha256": self._decoded_sha256,
            "content_version": self.content_version, "unsupported_rewards": {
                entry.reward_id: entry.unsupported_reason for entry in self._entries if not entry.supported},
            "time_policy": "JST; inclusive start, exclusive end; naive clocks interpreted as JST",
            "delivery_policy": "Transaction service validates all delivery dependencies before granting",
            "native_tested": False}
