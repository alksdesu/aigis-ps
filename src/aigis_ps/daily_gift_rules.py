from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
import hashlib
import json
from pathlib import Path
from types import MappingProxyType

from .local_io import deserialize, read_bounded
from .native_numbers import INT32_MAX


MAX_GIFTS = 4096
MAX_RULE_BYTES = 256 * 1024


def _identifier(value):
    if type(value) is not int or not 0 < value <= INT32_MAX:
        raise ValueError("Daily gift IDs must be positive int32 values")
    return value


def _date(value):
    if type(value) is not str:
        raise ValueError("Daily gift dates require ISO calendar strings")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Daily gift dates require canonical YYYY-MM-DD strings")
    return parsed


@dataclass(frozen=True)
class DailyGiftRules:
    enabled: bool = True
    gift_ids: tuple[int, ...] | None = None
    revivals: Mapping = field(default_factory=dict)

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Daily gift enabled must be a boolean")
        if self.gift_ids is not None:
            if not isinstance(self.gift_ids, (tuple, list)) or len(self.gift_ids) > MAX_GIFTS:
                raise ValueError("Daily gift selection must be a bounded sequence")
            identifiers = tuple(_identifier(value) for value in self.gift_ids)
            if len(set(identifiers)) != len(identifiers):
                raise ValueError("Daily gift IDs must not repeat")
            object.__setattr__(self, "gift_ids", tuple(sorted(identifiers)))
        if not isinstance(self.revivals, Mapping) or len(self.revivals) > MAX_GIFTS:
            raise ValueError("Daily gift revivals require a bounded mapping")
        normalized = {}
        for key, value in self.revivals.items():
            if type(key) is str:
                if not key.isascii() or not key.isdecimal() or str(int(key)) != key:
                    raise ValueError("Daily gift revival keys require canonical positive integers")
                key = int(key)
            key = _identifier(key)
            if key in normalized or not isinstance(value, Mapping) or set(value) != {"start_date", "end_date"}:
                raise ValueError("Daily gift revivals require exactly start_date and end_date")
            start, end = _date(value["start_date"]), _date(value["end_date"])
            if start > end:
                raise ValueError("Daily gift revival start must not follow its end")
            normalized[key] = MappingProxyType({"start_date": start.isoformat(), "end_date": end.isoformat()})
        object.__setattr__(self, "revivals", MappingProxyType(dict(sorted(normalized.items()))))

    def to_dict(self):
        return {"enabled": self.enabled,
                "gift_ids": list(self.gift_ids) if self.gift_ids is not None else None,
                "revivals": {str(key): dict(value) for key, value in self.revivals.items()}}

    @property
    def content_version(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()


def load_daily_gift_rules(path):
    if path is None:
        return DailyGiftRules()
    data = deserialize(read_bounded(Path(path), MAX_RULE_BYTES).decode("utf-8-sig"))
    if not isinstance(data, dict) or set(data) - {"enabled", "gift_ids", "revivals"}:
        raise ValueError("Daily gift rules contain unknown fields")
    return DailyGiftRules(**data)
