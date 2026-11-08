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


MAX_RULE_BYTES = 32768
MAX_CAMPAIGNS = 256


def _identifier(value):
    if type(value) is not int or not 0 < value <= INT32_MAX:
        raise ValueError("Event login campaign IDs must be positive int32 values")
    return value


def _date(value):
    if type(value) is not str:
        raise ValueError("Event login dates require ISO calendar strings")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value or parsed.toordinal() <= 1:
        raise ValueError("Event login dates require canonical YYYY-MM-DD strings")
    return parsed


@dataclass(frozen=True)
class EventLoginRules:
    enabled: bool = True
    campaign_ids: tuple[int, ...] | None = None
    revivals: Mapping = field(default_factory=dict)

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Event login enabled must be a boolean")
        identifiers = self.campaign_ids
        if identifiers is not None:
            if not isinstance(identifiers, (tuple, list)) or len(identifiers) > MAX_CAMPAIGNS:
                raise ValueError("Event login campaign selection must be a bounded sequence")
            normalized = tuple(_identifier(value) for value in identifiers)
            if len(set(normalized)) != len(normalized):
                raise ValueError("Event login campaign IDs must not repeat")
            object.__setattr__(self, "campaign_ids", tuple(sorted(normalized)))
        if not isinstance(self.revivals, Mapping) or len(self.revivals) > MAX_CAMPAIGNS:
            raise ValueError("Event login revivals require a bounded mapping")
        normalized = {}
        for key, value in self.revivals.items():
            if type(key) is str:
                if not key.isascii() or not key.isdecimal() or str(int(key)) != key:
                    raise ValueError("Revival campaign keys require canonical positive integers")
                key = int(key)
            key = _identifier(key)
            if key in normalized or not isinstance(value, Mapping) or set(value) != {"start_date", "end_date"}:
                raise ValueError("Revival campaign windows require exactly start_date and end_date")
            start, end = _date(value["start_date"]), _date(value["end_date"])
            if start >= end:
                raise ValueError("Revival campaign start must precede its end")
            normalized[key] = MappingProxyType({"start_date": start.isoformat(), "end_date": end.isoformat()})
        object.__setattr__(self, "revivals", MappingProxyType(dict(sorted(normalized.items()))))

    def to_dict(self):
        return {"enabled": self.enabled,
                "campaign_ids": list(self.campaign_ids) if self.campaign_ids is not None else None,
                "revivals": {str(key): dict(value) for key, value in self.revivals.items()}}

    @property
    def content_version(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()


def load_event_login_rules(path) -> EventLoginRules:
    if path is None:
        return EventLoginRules()
    data = deserialize(read_bounded(Path(path), MAX_RULE_BYTES).decode("utf-8-sig"))
    if not isinstance(data, dict) or set(data) - {"enabled", "campaign_ids", "revivals"}:
        raise ValueError("Event login rules contain unknown fields")
    return EventLoginRules(**data)
