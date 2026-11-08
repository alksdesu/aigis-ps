from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from fractions import Fraction
from itertools import groupby
import math
from operator import itemgetter
import re
from types import MappingProxyType
from typing import NamedTuple

from .alxml import Column, Container
from .errors import ProtocolError
from .masterdata import digest, json_bytes
from .native_calendar import DAYS_PER_WEEK, SUNDAY, native_weekday
from .native_numbers import INT32_MAX, float32, int32


RESOURCE = "DropBonusConfig.atb"
CONTENT_SCHEMA = 1
INT32_FIELDS = ("QuestID", "ItemType", "ItemID", "Operation")
# 904EA0 files columns 4-10 under native weekdays 1-7.
WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
WEEKDAY_FIELDS = tuple(f"{day}Value" for day in WEEKDAYS)
FIELDS = MappingProxyType({**dict.fromkeys(INT32_FIELDS, "int32"), **dict.fromkeys(WEEKDAY_FIELDS, "float32")})
# 905340 names only ItemType 1, through this ItemConfigTable TextID.
ITEM_TYPE_TEXTS = MappingProxyType({1: 1415358870})
# Resources ship only ItemID 0 and Operation 1; the client copies Operation without computing with it.
SUPPORTED_ITEM_ID = 0
SUPPORTED_OPERATION = 1
TABLE_INVALID = "quest_drop_bonus_table_invalid"
INVALID = ("quest_drop_bonus_invalid", 503)
UNSUPPORTED = ("quest_drop_bonus_unsupported", 501)
RULES = MappingProxyType({
    "scope": "quest_id_of_any_quest_type",
    "groups": "first_consecutive_quest_rows_then_first_row_per_item_type",
    "weekday": "jst_native_weekday_sunday_first",
    "credit": "max_of_count_and_floor_float32_times_count_once_per_request",
    "table_fault": "no_bonus_and_reported",
    "row_fault": "quest_closed",
})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class DropBonusUnavailable(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _native_value(value) -> bool:
    if type(value) is not float or not math.isfinite(value) or value < 0:
        return False
    try:
        return float32(value) == value
    except OverflowError:
        return False


@dataclass(frozen=True)
class DropBonus:
    quest_id: int
    item_type: int
    item_id: str
    operation: int
    weekday_values: tuple[float, ...]

    def __post_init__(self) -> None:
        values = self.weekday_values
        if (not all(int32(value) for value in (self.quest_id, self.item_type, self.operation))
                or type(self.item_id) is not str or not self.item_id
                or not isinstance(values, (tuple, list)) or len(values) != DAYS_PER_WEEK
                or not all(_native_value(value) for value in values)):
            raise ValueError("Drop bonuses require native identifiers and seven finite nonnegative float32 values")
        object.__setattr__(self, "weekday_values", tuple(values))

    def value_on(self, moment: datetime) -> float:
        return self.weekday_values[native_weekday(moment) - SUNDAY]


@dataclass(frozen=True)
class DropBonusGrant:
    value: float
    increments: Mapping[str, int]
    children: tuple = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "increments", MappingProxyType(dict(self.increments)))
        object.__setattr__(self, "children", tuple(self.children))

    def receipt(self) -> dict:
        return {"value": self.value, "increments": dict(self.increments)}


@dataclass(frozen=True)
class DropBonusFailure:
    code: str
    status: int
    message: str

    def error(self) -> ProtocolError:
        return ProtocolError(self.code, self.message, self.status)


NO_BONUS = DropBonusGrant(0.0, {})


def apply(value: float, base: int) -> int:
    if type(value) not in (int, float) or not math.isfinite(value) or type(base) is not int or base < 0:
        raise ValueError("Drop bonuses scale a nonnegative count by a finite value")
    if value <= 0:
        return base
    # Credited counts land in int32 columns, so like MoneyUp they stop at INT32_MAX.
    return min(max(base, math.floor(Fraction(value) * base)), INT32_MAX)


def bonus_container(value: float, item_type: int) -> Container:
    # A70160 reads BONUS ItemType and Value; 4522D0 shows the banner only for a nonzero type and a positive value.
    return Container("BONUS", (Column("ItemType", "I", (item_type,)), Column("Value", "F", (value,))))


def bonus_grant(bonus: DropBonus | None, moment: datetime, items: Mapping[str, int]) -> DropBonusGrant:
    # items holds only this request's obtained-slot drops; star, collection and mode rewards are never multiplied.
    if (bonus is not None and not isinstance(bonus, DropBonus)) or not isinstance(items, Mapping):
        raise ValueError("Drop bonus grants require a drop bonus and the obtained item counts")
    if bonus is None:
        return NO_BONUS
    value = bonus.value_on(moment)
    if value <= 0:
        return NO_BONUS
    base = items.get(bonus.item_id, 0)
    increment = apply(value, base) - base
    # A positive day reports BONUS even when the request dropped none of the item.
    return DropBonusGrant(value, {bonus.item_id: increment} if increment else {},
                          (bonus_container(value, bonus.item_type),))


def _sha256(value, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or not _SHA256.match(value):
        raise ValueError("Drop bonus sources require a lowercase sha256 digest")
    return value


def _quest_ids(values) -> frozenset[int]:
    identifiers = frozenset(values) if isinstance(values, Iterable) and not isinstance(values, (str, bytes)) else None
    if identifiers is None or not all(int32(value) for value in identifiers):
        raise ValueError("Drop bonus quests must be native quest identifiers")
    return identifiers


def _item_names(item_rows) -> dict[int, str | None]:
    if not isinstance(item_rows, (list, tuple)):
        raise ValueError("ItemConfigTable rows must be a native table")
    names = {}
    for item_type, text in ITEM_TYPE_TEXTS.items():
        matches = [row.get("ItemID") for row in item_rows
                   if isinstance(row, Mapping) and type(row.get("TextID")) is int and row["TextID"] == text]
        names[item_type] = matches[0] if len(matches) == 1 and type(matches[0]) is str and matches[0] else None
    return names


def _table_rows(parsed) -> tuple[Mapping, ...]:
    try:
        columns = tuple((field["name"], field["type_name"]) for field in parsed["fields"])
        rows = tuple(parsed["rows"])
    except (KeyError, TypeError) as error:
        raise DropBonusUnavailable(TABLE_INVALID, "DropBonusConfig is not a parsed native table") from error
    if columns != tuple(FIELDS.items()):
        raise DropBonusUnavailable(TABLE_INVALID, "DropBonusConfig columns differ from the native layout")
    if any(not isinstance(row, Mapping) or not FIELDS.keys() <= row.keys()
           or not all(int32(row[name]) for name in INT32_FIELDS) for row in rows):
        raise DropBonusUnavailable(TABLE_INVALID, "DropBonusConfig rows do not carry their native columns")
    return rows


def _first_per_item_type(run: tuple[Mapping, ...]) -> tuple[Mapping, ...]:
    kept = {}
    for row in run:
        kept.setdefault(row["ItemType"], row)
    return tuple(kept.values())


def _fault(quest_id: int, rows: tuple[Mapping, ...], names: Mapping[int, str | None]) -> DropBonusFailure | None:
    invalid = next((name for row in rows for name in WEEKDAY_FIELDS if not _native_value(row[name])), None)
    if invalid is not None:
        return DropBonusFailure(*INVALID, f"DropBonusConfig {invalid} of quest {quest_id} is not a nonnegative float32")
    if len(rows) != 1:
        return DropBonusFailure(*UNSUPPORTED, f"DropBonusConfig quest {quest_id} combines {len(rows)} item types")
    item_type, item_id, operation = rows[0]["ItemType"], rows[0]["ItemID"], rows[0]["Operation"]
    if item_type not in ITEM_TYPE_TEXTS or item_id != SUPPORTED_ITEM_ID or operation != SUPPORTED_OPERATION:
        return DropBonusFailure(*UNSUPPORTED, f"DropBonusConfig quest {quest_id} uses ItemType {item_type}, "
                                              f"ItemID {item_id} and Operation {operation}")
    if names[item_type] is None:
        return DropBonusFailure(*UNSUPPORTED, f"ItemConfigTable does not name one item for ItemType {item_type}")
    return None


class _Census(NamedTuple):
    rows: int = 0
    quests: int = 0
    duplicate_rows: int = 0
    ignored_groups: int = 0
    orphan_rows: int = 0


class DropBonusTable:
    def __init__(self, census: _Census, bonuses: Mapping[int, DropBonus], failures: Mapping[int, DropBonusFailure], *,
                 sha256: str | None, error: Mapping[str, str] | None = None):
        self.sha256 = sha256
        self.error = None if error is None else MappingProxyType(dict(error))
        self._census = census
        self._bonuses = MappingProxyType(dict(sorted(bonuses.items())))
        self._failures = MappingProxyType(dict(sorted(failures.items())))
        self._quest_ids = frozenset(self._bonuses.keys() | self._failures.keys())
        self.content_version = digest(json_bytes({
            "schema": CONTENT_SCHEMA, "resource": RESOURCE, "sha256": sha256,
            "error": None if error is None else dict(error), "rules": dict(RULES),
            "bonuses": [asdict(bonus) for bonus in self._bonuses.values()],
            "failures": [[quest_id, failure.code] for quest_id, failure in self._failures.items()]}))

    @classmethod
    def from_table(cls, parsed: Mapping, *, quest_ids: Iterable[int], item_rows, sha256: str) -> DropBonusTable:
        source, known, names = _sha256(sha256), _quest_ids(quest_ids), _item_names(item_rows)
        rows = _table_rows(parsed)
        groups, duplicate_rows, ignored_groups, orphan_rows = {}, 0, 0, 0
        # 905B40 keeps each QuestID's first run of consecutive rows; 9055E0 keeps the first row per ItemType in it.
        for quest_id, members in groupby(rows, key=itemgetter("QuestID")):
            run = tuple(members)
            if quest_id not in known:
                orphan_rows += len(run)
            elif quest_id in groups:
                ignored_groups += 1
            else:
                groups[quest_id] = _first_per_item_type(run)
                duplicate_rows += len(run) - len(groups[quest_id])
        bonuses, failures = {}, {}
        for quest_id, kept in groups.items():
            fault = _fault(quest_id, kept, names)
            if fault is not None:
                failures[quest_id] = fault
                continue
            row = kept[0]
            bonuses[quest_id] = DropBonus(quest_id, row["ItemType"], names[row["ItemType"]], row["Operation"],
                                          tuple(row[name] for name in WEEKDAY_FIELDS))
        census = _Census(len(rows), len({row["QuestID"] for row in rows}), duplicate_rows, ignored_groups, orphan_rows)
        return cls(census, bonuses, failures, sha256=source)

    @classmethod
    def unavailable(cls, code: str, detail: str, *, sha256: str | None = None) -> DropBonusTable:
        if type(code) is not str or not code or type(detail) is not str or not detail:
            raise ValueError("An unavailable drop bonus table requires a reason code and detail")
        return cls(_Census(), {}, {}, sha256=_sha256(sha256, optional=True), error={"code": code, "message": detail})

    @property
    def available(self) -> bool:
        return self.error is None

    @property
    def quest_ids(self) -> frozenset[int]:
        return self._quest_ids

    def lookup(self, quest_id: int) -> DropBonus | None:
        if not int32(quest_id):
            raise ValueError("Drop bonus lookups require a native quest identifier")
        failure = self._failures.get(quest_id)
        if failure is not None:
            raise failure.error()
        return self._bonuses.get(quest_id)

    def status(self) -> dict:
        profiles = Counter((bonus.item_id, bonus.weekday_values) for bonus in self._bonuses.values())
        return {"resource": RESOURCE, "available": self.available,
                "error": None if self.error is None else dict(self.error), "sha256": self.sha256,
                "content_version": self.content_version, **self._census._asdict(),
                "indexed_quests": len(self._quest_ids),
                "failures": {quest_id: failure.code for quest_id, failure in self._failures.items()},
                "weekday_profiles": [{"item_id": item_id, "values": dict(zip(WEEKDAYS, values)), "quests": count}
                                     for (item_id, values), count in profiles.most_common()],
                "rules": dict(RULES)}
