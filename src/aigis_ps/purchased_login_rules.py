from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json

from .local_io import deserialize, read_bounded
from .native_numbers import INT32_MAX


MAX_RULE_BYTES = 16384
MAX_CAMPAIGNS = 256


@dataclass(frozen=True)
class PurchasedLoginRules:
    enabled: bool = False
    campaign_ids: tuple[int, ...] | None = None
    purchase_limit: int = 1

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Purchased login enabled must be a boolean")
        if type(self.purchase_limit) is not int or not 1 <= self.purchase_limit <= INT32_MAX:
            raise ValueError("Purchased login limit must be a positive int32")
        if self.campaign_ids is not None:
            if (not isinstance(self.campaign_ids, (tuple, list)) or len(self.campaign_ids) > MAX_CAMPAIGNS
                    or any(type(value) is not int or not 0 < value <= INT32_MAX for value in self.campaign_ids)
                    or len(set(self.campaign_ids)) != len(self.campaign_ids)):
                raise ValueError("Purchased login campaigns must be bounded distinct positive integers")
            object.__setattr__(self, "campaign_ids", tuple(sorted(self.campaign_ids)))

    def to_dict(self):
        result = asdict(self)
        result["campaign_ids"] = list(self.campaign_ids) if self.campaign_ids is not None else None
        return result

    @property
    def content_version(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_purchased_login_rules(path):
    if path is None:
        return PurchasedLoginRules()
    values = deserialize(read_bounded(path, MAX_RULE_BYTES).decode("utf-8-sig"))
    if not isinstance(values, dict) or set(values) - PurchasedLoginRules.__dataclass_fields__.keys():
        raise ValueError("Purchased login configuration contains unknown fields")
    return PurchasedLoginRules(**values)
