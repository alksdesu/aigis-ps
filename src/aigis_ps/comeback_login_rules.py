from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json

from .local_io import deserialize, read_bounded
from .native_numbers import INT32_MAX


MAX_RULE_BYTES = 16384


@dataclass(frozen=True)
class ComebackLoginRules:
    enabled: bool = True
    min_absence_days: int = 30

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Comeback login enabled must be a boolean")
        if type(self.min_absence_days) is not int or not 1 <= self.min_absence_days <= INT32_MAX:
            raise ValueError("Comeback login absence threshold must be a positive int32")

    def to_dict(self):
        return asdict(self)

    @property
    def content_version(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_comeback_login_rules(path):
    if path is None:
        return ComebackLoginRules()
    values = deserialize(read_bounded(path, MAX_RULE_BYTES).decode("utf-8-sig"))
    if not isinstance(values, dict) or set(values) - ComebackLoginRules.__dataclass_fields__.keys():
        raise ValueError("Comeback login configuration contains unknown fields")
    return ComebackLoginRules(**values)
