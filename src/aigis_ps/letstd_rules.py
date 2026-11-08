from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from types import MappingProxyType

from .local_io import deserialize, read_bounded


PARTNERS = ("oshirore", "monmusutd", "rlyeh")
PARTNER_ORDER = MappingProxyType({partner: index for index, partner in enumerate(PARTNERS)})
GROUPS = MappingProxyType({1: "newChallenge", 2: "newPurchase"})
SCHEMA_VERSION = 1
MAX_RULE_BYTES = 8 * 1024
FILE_FIELDS = frozenset(("schema_version", "partners", "native_popup"))


def _group(partner, group):
    if partner not in PARTNERS or type(group) is not int or group not in GROUPS:
        raise ValueError("LetsTD switches exist only for the native partners and reward groups")
    return GROUPS[group]


@dataclass(frozen=True)
class LetsTDRules:
    partners: Mapping = field(default_factory=dict)
    native_popup: bool = False

    def __post_init__(self):
        if type(self.native_popup) is not bool:
            raise ValueError("LetsTD native popup must be a boolean")
        if not isinstance(self.partners, Mapping) or set(self.partners) - set(PARTNERS):
            raise ValueError("LetsTD switches name only the native partners")
        normalized = {}
        for partner in PARTNERS:
            switches = self.partners.get(partner, {})
            if not isinstance(switches, Mapping) or set(switches) - set(GROUPS.values()):
                raise ValueError("LetsTD partner switches name only the native reward groups")
            if any(type(value) is not bool for value in switches.values()):
                raise ValueError("LetsTD reward group switches must be booleans")
            normalized[partner] = MappingProxyType({name: switches.get(name, False) for name in GROUPS.values()})
        object.__setattr__(self, "partners", MappingProxyType(normalized))

    def enabled(self, partner, group):
        name = _group(partner, group)
        return self.partners[partner][name]

    @property
    def enabled_groups(self):
        return tuple((partner, group) for partner in PARTNERS for group in GROUPS if self.enabled(partner, group))

    def to_dict(self):
        return {"schema_version": SCHEMA_VERSION, "native_popup": self.native_popup,
                "partners": {partner: dict(switches) for partner, switches in self.partners.items()}}

    @property
    def content_version(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_letstd_rules(path):
    if path is None:
        return LetsTDRules()
    values = deserialize(read_bounded(Path(path), MAX_RULE_BYTES).decode("utf-8-sig"))
    if not isinstance(values, dict) or set(values) - FILE_FIELDS:
        raise ValueError("LetsTD configuration contains unknown fields")
    if type(values.get("schema_version")) is not int or values["schema_version"] != SCHEMA_VERSION:
        raise ValueError("LetsTD configuration requires schema_version 1")
    return LetsTDRules(**{key: value for key, value in values.items() if key != "schema_version"})
