from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType

from .local_io import deserialize, read_bounded
from .native_numbers import int32
from .presents import MAX_PRESENTS


SCHEMA_VERSION = 1
MAX_RULE_BYTES = 256 * 1024
MAX_CODES = 4096
# The native keypad (737470) types only digits and lowercase letters, at most 18 of them.
MAX_CODE_LENGTH = 18
CODE_PATTERN = re.compile(rf"[0-9a-z]{{1,{MAX_CODE_LENGTH}}}\Z")
# 737390 inserts a hyphen before the 6th, 11th and 16th entered characters.
CODE_GROUP = 5
FILE_FIELDS = frozenset(("schema_version", "codes"))
CODE_FIELDS = frozenset(("code", "rewards", "limit", "single_use", "window"))
REQUIRED_CODE_FIELDS = frozenset(("code", "rewards"))
WINDOW_FIELDS = frozenset(("start_date", "end_date"))


def canonical_code(value) -> str:
    if type(value) is not str or not value.isascii():
        raise ValueError("Serial codes must be ASCII text")
    code = value.replace("-", "").lower()
    if not CODE_PATTERN.fullmatch(code):
        raise ValueError(f"Serial codes need 1 to {MAX_CODE_LENGTH} letters or digits")
    return code


def entered_form(code: str) -> str:
    return "-".join(code[index:index + CODE_GROUP] for index in range(0, len(code), CODE_GROUP))


def _date(value):
    if type(value) is not str:
        raise ValueError("Serial code windows require ISO calendar strings")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Serial code windows require canonical YYYY-MM-DD strings")
    return parsed


def _field(key, item):
    if type(key) is not str or type(item) not in (str, int):
        return False
    try:
        key.encode("utf-8")
        if type(item) is str:
            item.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _reward(value):
    if not isinstance(value, Mapping) or not value or not all(_field(key, item) for key, item in value.items()):
        raise ValueError("Serial code rewards are flat objects of Unicode text and integer fields")
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class SerialCode:
    code: str
    rewards: tuple
    limit: int = 1
    single_use: bool = False
    window: Mapping | None = None

    def __post_init__(self):
        object.__setattr__(self, "code", canonical_code(self.code))
        if not isinstance(self.rewards, (list, tuple)) or not 0 < len(self.rewards) <= MAX_PRESENTS:
            raise ValueError("Serial codes grant a bounded, nonempty reward list")
        object.__setattr__(self, "rewards", tuple(_reward(reward) for reward in self.rewards))
        if not int32(self.limit) or self.limit < 1:
            raise ValueError("Serial code limits must be positive int32 values")
        if type(self.single_use) is not bool:
            raise ValueError("Serial code single_use must be a boolean")
        if self.single_use and self.limit != 1:
            raise ValueError("Single-use serial codes allow exactly one redemption")
        if self.window is not None:
            if not isinstance(self.window, Mapping) or set(self.window) != WINDOW_FIELDS:
                raise ValueError("Serial code windows require exactly start_date and end_date")
            start, end = _date(self.window["start_date"]), _date(self.window["end_date"])
            if start > end:
                raise ValueError("Serial code windows must not end before they start")
            object.__setattr__(self, "window", MappingProxyType({"start_date": start.isoformat(),
                                                                 "end_date": end.isoformat()}))

    def open_on(self, day: date) -> bool:
        return (self.window is None
                or date.fromisoformat(self.window["start_date"]) <= day <= date.fromisoformat(self.window["end_date"]))

    def reward_list(self) -> list[dict]:
        return [dict(reward) for reward in self.rewards]

    def to_dict(self):
        return {"code": self.code, "rewards": self.reward_list(), "limit": self.limit, "single_use": self.single_use,
                "window": None if self.window is None else dict(self.window)}


def _entry(value):
    if isinstance(value, SerialCode):
        return value
    if not isinstance(value, Mapping) or not REQUIRED_CODE_FIELDS <= set(value) <= CODE_FIELDS:
        raise ValueError("Serial code entries name code and rewards, optionally limit, single_use and window")
    return SerialCode(**value)


@dataclass(frozen=True)
class SerialCodeRules:
    codes: tuple[SerialCode, ...] = ()

    def __post_init__(self):
        if not isinstance(self.codes, (list, tuple)) or len(self.codes) > MAX_CODES:
            raise ValueError("Serial code tables must be bounded sequences")
        entries = tuple(sorted((_entry(value) for value in self.codes), key=lambda entry: entry.code))
        if len({entry.code for entry in entries}) != len(entries):
            raise ValueError("Serial codes must stay distinct without hyphens and letter case")
        object.__setattr__(self, "codes", entries)

    def get(self, code: str) -> SerialCode | None:
        return next((entry for entry in self.codes if entry.code == code), None)

    def to_dict(self):
        return {"schema_version": SCHEMA_VERSION, "codes": [entry.to_dict() for entry in self.codes]}

    @property
    def content_version(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()


def load_serial_code_rules(path):
    if path is None:
        return SerialCodeRules()
    values = deserialize(read_bounded(Path(path), MAX_RULE_BYTES).decode("utf-8-sig"))
    if not isinstance(values, dict) or set(values) - FILE_FIELDS:
        raise ValueError("Serial code configuration contains unknown fields")
    if type(values.get("schema_version")) is not int or values["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Serial code configuration requires schema_version 1")
    return SerialCodeRules(codes=values.get("codes", ()))
