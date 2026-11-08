from __future__ import annotations

from calendar import monthrange
from collections import Counter
from dataclasses import dataclass, field, fields
from datetime import datetime
import hashlib
import json
import re
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, dumps
from .business_calendar import NATIVE_SECONDS_TEXT
from .energy_clock import JST, instant
from .errors import ProtocolError
from .native_numbers import int32
from .present_items import LEGACY_ITEMS


LIMITS = ParseLimits(max_input_bytes=8 * 1024 * 1024, max_decompressed_bytes=8 * 1024 * 1024)
GRADE_FIELDS = {name: 1 for name in ("ConfigId", "_GradeID", "_GradeNameTextID", "_UseBorderCount",
                                   "_MenuTagID", "_MenuPattern", "CookGainBorder")}
GRADE_FIELDS.update(_Name=32, _ResName=32, OpenDate=32)
STAMP_FIELDS = {name: 1 for name in ("StampID", "ItemType", "ItemParam", "_Param1", "EvolutionCount")}
STAMP_FIELDS["ItemID"] = 33
BEGINNER_FIELDS = {name: 1 for name in ("_ID", "_Type", "_PresentID", "_SummonConfigID", "_Count")}
_RESOURCE = re.compile(r"[A-Za-z0-9_\-]+\.atb\Z")


def _text(value, maximum=512, *, empty=False):
    if type(value) is not str or not empty and not value or len(value.encode("utf-8")) > maximum:
        raise ValueError("Expected bounded text")
    dumps(Container(children=(Column("Value", "S", (value,)),)))
    return value


def _date(value):
    _text(value, empty=True)
    if not value:
        return None
    match = NATIVE_SECONDS_TEXT.fullmatch(value)
    if match is None:
        raise ValueError("Grade opening time requires a complete native timestamp")
    year, _, month, day, hour, minute, second = match.groups()
    return datetime(*(int(part) for part in (year, month, day, hour, minute, second)), tzinfo=JST)


@dataclass(frozen=True)
class LoginReward:
    type: int
    item_id: str
    amount: int
    param1: int
    evolution_count: int
    source: str = "stamp"
    record_id: int = 0
    present_id: int = 0
    unsupported_reason: str | None = field(init=False)

    def __post_init__(self):
        if any(not int32(value) for value in (self.type, self.amount, self.param1, self.evolution_count,
                                               self.record_id, self.present_id)):
            raise ValueError("Reward values must be signed native integers")
        _text(self.item_id, 31, empty=True)
        if self.source not in ("stamp", "beginner"):
            raise ValueError("Unknown login reward source")
        reason = None
        if self.type == -1:
            if (self.amount, self.param1, self.evolution_count, self.item_id, self.present_id) != (-1, 0, 0, "", 0):
                raise ValueError("Invalid no-reward placeholder")
            reason = "placeholder"
        elif self.amount <= 0:
            reason = "nonpositive_reward"
        elif self.source == "beginner" and (self.type != 0 or self.present_id or self.param1 or self.item_id):
            reason = "unverified_beginner_shape"
        elif self.evolution_count != 0:
            reason = "unverified_evolution_count"
        elif self.type not in (0, 1, 2, 16, 22, *LEGACY_ITEMS):
            reason = "unverified_reward_type"
        elif self.type == 22 and not self.item_id or self.type != 22 and self.item_id:
            reason = "unexpected_item_identity"
        elif self.type == 16 and self.param1 <= 0 or self.type != 16 and self.param1 != 0:
            reason = "unexpected_reward_parameter"
        elif self.present_id != 0:
            reason = "unexpected_present_parameter"
        object.__setattr__(self, "unsupported_reason", reason)

    @property
    def definition(self) -> str:
        # Only resource values count, so a code change in what is supported never looks like a new reward.
        values = {item.name: getattr(self, item.name) for item in fields(self) if item.init}
        return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def to_present_reward(self, description: str) -> dict:
        if self.unsupported_reason is not None:
            raise ProtocolError("login_reward_unsupported", f"Login reward is unsupported: {self.unsupported_reason}", 501)
        try:
            _text(description, 4096, empty=True)
        except (ValueError, TypeError) as error:
            raise ProtocolError("login_reward_description_invalid", "Login reward description is invalid") from error
        if self.type == 0:
            reward = {"kind": "free_crystal", "amount": self.amount}
        elif self.type == 1:
            reward = {"kind": "unit", "template_id": self.amount, "count": 1}
        elif self.type == 2:
            reward = {"kind": "gold", "amount": self.amount}
        elif self.type == 16:
            reward = {"kind": "summon_ticket", "pool_id": self.param1, "count": self.amount}
        elif self.type == 22:
            reward = {"kind": "item", "item_id": self.item_id, "amount": self.amount}
        else:
            reward = {"kind": "legacy_item", "present_type": self.type, "amount": self.amount}
        return {**reward, "description": description}


@dataclass(frozen=True)
class LoginGrade:
    config_id: int
    grade_id: int
    name: str
    minimum_spend: int
    resource_name: str
    open_at: datetime | None
    cook_gain_border: int
    name_text_id: int
    menu_tag_id: int
    menu_pattern: int


class LoginRewardCatalog:
    def __init__(self, asset_reader, *, resource_version: str | None = None):
        try:
            if not callable(asset_reader):
                raise ValueError("Login rewards require a resource reader")
            if resource_version is not None:
                _text(resource_version, 128)
            hashes, parsed = {}, {}

            def table(name, expected):
                if name in parsed:
                    return parsed[name]
                raw = asset_reader(name)
                if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
                    raise ValueError("Login reward resources must be bounded immutable bytes")
                data = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
                result = parse_table(data, LIMITS)
                fields = {entry["name"]: entry["type_code"] for entry in result["fields"]}
                if len(fields) != len(result["fields"]) or any(fields.get(key) != kind for key, kind in expected.items()):
                    raise ValueError("Login reward resource schema differs from the native contract")
                if expected is STAMP_FIELDS and next(entry["size"] for entry in result["fields"] if entry["name"] == "ItemID") != 32:
                    raise ValueError("Stamp item identities require the native 32-byte field")
                for row in result["rows"]:
                    for key, kind in expected.items():
                        if kind == 1 and not int32(row[key]):
                            raise ValueError("Login reward fields require signed native integers")
                        if kind in (32, 33):
                            _text(row[key], 31 if kind == 33 else 512, empty=True)
                hashes[name] = hashlib.sha256(raw).hexdigest()
                parsed[name] = result["rows"]
                return result["rows"]

            grades, config_ids, grade_ids, names = [], set(), set(), set()
            for row in table("StampCardConfig.atb", GRADE_FIELDS):
                identifier, grade, name = row["ConfigId"], row["_GradeID"], _text(row["_Name"])
                resource = _text(row["_ResName"])
                if (identifier <= 0 or grade < 0 or identifier in config_ids or grade in grade_ids or name in names
                        or row["_UseBorderCount"] < 0 or row["CookGainBorder"] < 0 or row["_MenuPattern"] < 0
                        or _RESOURCE.fullmatch(resource) is None or resource in ("StampCardConfig.atb", "BeginnerBonus.atb")):
                    raise ValueError("Grade metadata is invalid or duplicated")
                grades.append(LoginGrade(identifier, grade, name, row["_UseBorderCount"], resource, _date(row["OpenDate"]),
                    row["CookGainBorder"], row["_GradeNameTextID"], row["_MenuTagID"], row["_MenuPattern"]))
                config_ids.add(identifier)
                grade_ids.add(grade)
                names.add(name)
            grades.sort(key=lambda row: row.grade_id)
            if (not grades or grades[0].grade_id != 0 or grades[0].minimum_spend != 0
                    or any(left.minimum_spend >= right.minimum_spend for left, right in zip(grades, grades[1:]))):
                raise ValueError("Grades require increasing spend thresholds starting at zero")
            stamps, months = {}, {}
            for grade in grades:
                entries, available = {}, set()
                for row in table(grade.resource_name, STAMP_FIELDS):
                    month, ordinal = divmod(row["StampID"], 100)
                    if not 1 <= month <= 12 or not 1 <= ordinal <= 31 or (month, ordinal) in entries:
                        raise ValueError("Stamp identity is invalid or duplicated")
                    reward = LoginReward(row["ItemType"], row["ItemID"], row["ItemParam"], row["_Param1"],
                                         row["EvolutionCount"], record_id=row["StampID"])
                    if ordinal > monthrange(2000, month)[1] and reward.type != -1:
                        raise ValueError("A nonexistent calendar day cannot carry a reward")
                    entries[month, ordinal] = reward
                    available.add(month)
                if not entries:
                    raise ValueError("A grade cannot have an empty stamp resource")
                stamps[grade.grade_id] = MappingProxyType(entries)
                months[grade.grade_id] = tuple(sorted(available))
            beginners = {}
            for row in table("BeginnerBonus.atb", BEGINNER_FIELDS):
                identifier = row["_ID"]
                if identifier <= 0 or identifier in beginners:
                    raise ValueError("Beginner identity is invalid or duplicated")
                beginners[identifier] = LoginReward(row["_Type"], "", row["_Count"], row["_SummonConfigID"], 0,
                    source="beginner", record_id=identifier, present_id=row["_PresentID"])
            if not beginners:
                raise ValueError("Beginner rewards are empty")
            self._grades = tuple(grades)
            self._by_grade = MappingProxyType({grade.grade_id: grade for grade in grades})
            self._stamps = MappingProxyType(stamps)
            self._months = MappingProxyType(months)
            self._beginners = MappingProxyType(beginners)
            self._hashes = MappingProxyType(hashes)
            self._resource_version = resource_version
            content = {"rule": "login-reward-catalog-v1", "resources": hashes, "version": resource_version,
                "opening": "strict_before_now_jst", "delivery": "external_present_capabilities",
                "beginner": "verified_type0_only", "unknown_reward": "preserve_and_report_unsupported"}
            self._content_version = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError, ProtocolError) as error:
            raise ProtocolError("login_reward_catalog_invalid", "Login reward resources differ from the verified contract", 503) from error

    @property
    def grades(self) -> tuple[LoginGrade, ...]:
        return self._grades

    @property
    def by_grade(self):
        return self._by_grade

    @property
    def months(self):
        return self._months

    @property
    def content_version(self) -> str:
        return self._content_version

    def grade(self, previous_spend: int, now: datetime) -> int:
        if not int32(previous_spend) or previous_spend < 0:
            raise ProtocolError("login_reward_spend_invalid", "Previous-month spend must be a nonnegative native integer")
        try:
            current = instant(now).astimezone(JST)
        except (ProtocolError, ValueError, TypeError, OverflowError) as error:
            raise ProtocolError("login_reward_clock_invalid", "Login reward clock is invalid") from error
        eligible = [row.grade_id for row in self._grades if previous_spend >= row.minimum_spend
                    and (row.open_at is None or row.open_at < current)]
        if not eligible:
            raise ProtocolError("login_reward_grade_unavailable", "No stamp grade is open", 501)
        return eligible[-1]

    def stamp(self, grade: int, month: int, ordinal: int) -> LoginReward:
        if not int32(grade) or grade not in self._by_grade:
            raise ProtocolError("login_reward_grade_unknown", "Stamp grade is not in the catalog")
        if not int32(month) or not 1 <= month <= 12 or not int32(ordinal) or not 1 <= ordinal <= 31:
            raise ProtocolError("login_reward_position_invalid", "Stamp month and ordinal are invalid")
        if month not in self._months[grade]:
            raise ProtocolError("login_reward_month_unavailable", "This month has no stamp resource", 501)
        reward = self._stamps[grade].get((month, ordinal))
        if reward is None:
            raise ProtocolError("login_reward_position_unavailable", "This stamp position has no reward definition", 501)
        return reward

    def beginner(self, ordinal: int) -> LoginReward:
        if not int32(ordinal) or ordinal <= 0:
            raise ProtocolError("login_reward_position_invalid", "Beginner ordinal is invalid")
        if ordinal not in self._beginners:
            raise ProtocolError("login_reward_position_unavailable", "This beginner reward is not defined", 501)
        return self._beginners[ordinal]

    def is_ready(self) -> bool:
        # Construction refuses unverified resources, so every built catalog is ready.
        return True

    def status(self) -> dict:
        rewards = [reward for entries in self._stamps.values() for reward in entries.values()]
        return {"ready": self.is_ready(), "grade_count": len(self._grades), "stamp_count": len(rewards),
            "beginner_count": len(self._beginners), "months_by_grade": dict(self._months),
            "unsupported_reasons": dict(Counter(reward.unsupported_reason for reward in rewards if reward.unsupported_reason)),
            "ticket_pools": sorted({reward.param1 for reward in rewards if reward.type == 16}),
            "resource_hashes": dict(self._hashes), "resource_version": self._resource_version,
            "content_version": self.content_version, "opening": "strict_before_now_jst",
            "delivery_capabilities": "Validated by the present service; pool completeness is not inferred here"}
