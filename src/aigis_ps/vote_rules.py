from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import MappingProxyType

from .local_io import deserialize, read_bounded
from .native_numbers import INT32_MAX


MAX_RULE_BYTES = 256 * 1024


def revival_instant(value):
    if type(value) is not str or not 1 <= len(value) <= 35:
        raise ValueError("Vote revival start requires an ISO timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.microsecond:
        raise ValueError("Vote revival start requires a timezone and whole seconds")
    if parsed.isoformat(timespec="seconds") != value:
        raise ValueError("Vote revival start requires canonical ISO seconds")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class VoteRules:
    enabled: bool = True
    revival_start: str | None = None
    candidate_policy: str = "present_template"
    candidate_overrides: Mapping = field(default_factory=dict)
    skill_fairy_template_id: int = 6
    required_rank: int = 1
    vote_limit_count: int = 999
    earned_votes: int = 15
    result_display_days: int = 7

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Vote enabled must be a boolean")
        if self.revival_start is not None:
            revival_instant(self.revival_start)
        if type(self.candidate_policy) is not str or self.candidate_policy not in ("present_template", "strict"):
            raise ValueError("Vote candidate policy must be present_template or strict")
        if not isinstance(self.candidate_overrides, Mapping) or len(self.candidate_overrides) > 4096:
            raise ValueError("Vote candidate overrides require a bounded mapping")
        overrides = {}
        for name, identifier in self.candidate_overrides.items():
            if type(name) is not str or not name or len(name.encode("utf-8")) > 512 or name.strip() != name:
                raise ValueError("Vote candidate names must be bounded and canonical")
            if type(identifier) is not int or not 0 < identifier <= INT32_MAX:
                raise ValueError("Vote candidate IDs must be positive native integers")
            overrides[name] = identifier
        if type(self.skill_fairy_template_id) is not int or self.skill_fairy_template_id not in (6, 840):
            raise ValueError("Vote skill fairy requires a verified source template")
        for name, maximum in (("required_rank", INT32_MAX), ("vote_limit_count", 999),
                              ("earned_votes", INT32_MAX), ("result_display_days", 366)):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= maximum:
                raise ValueError("Vote local numeric rules must be bounded positive integers")
        object.__setattr__(self, "candidate_overrides", MappingProxyType(dict(sorted(overrides.items()))))

    def to_dict(self):
        return {"enabled": self.enabled, "revival_start": self.revival_start,
                "candidate_policy": self.candidate_policy,
                "candidate_overrides": dict(self.candidate_overrides),
                "skill_fairy_template_id": self.skill_fairy_template_id, "required_rank": self.required_rank,
                "vote_limit_count": self.vote_limit_count, "earned_votes": self.earned_votes,
                "result_display_days": self.result_display_days}

    @property
    def content_version(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode()).hexdigest()


def load_vote_rules(path):
    if path is None:
        return VoteRules()
    data = deserialize(read_bounded(Path(path), MAX_RULE_BYTES).decode("utf-8-sig"))
    if not isinstance(data, dict) or set(data) - {"enabled", "revival_start", "candidate_policy", "candidate_overrides", "skill_fairy_template_id",
                                              "required_rank", "vote_limit_count", "earned_votes", "result_display_days"}:
        raise ValueError("Vote rules contain unknown fields")
    return VoteRules(**data)
