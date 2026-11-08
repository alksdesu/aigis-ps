from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from types import MappingProxyType

from .al_format import decompress_all4, parse_table
from .alxml import Column, Container, dumps
from .business_calendar import NATIVE_DATETIME, NATIVE_MINUTES_TEXT
from .energy_clock import JST, instant
from .event_pass_catalog import FIELDS, MAX_TIME_LIMIT_BYTES
from .local_io import deserialize, read_bounded
from .mission_catalog import MISSION_FIELDS
from .native_numbers import INT32_MAX, int32
from .possessions import MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES
from .table_cell_patch import LIMITS, MAX_BYTES, patch_table_cells


PASS_RESOURCE = "EventPassConfig.atb"
MISSION_RESOURCE = "GloryConditionConfig.atb"
MAX_RULES_BYTES = 16384
# Item names follow the stored bound EventPassCatalog reads, so revival accepts the same table.
PASS_TEXT_BYTES = MappingProxyType({"StartDate": MAX_ITEM_NAME_BYTES, "EndDate": MAX_ITEM_NAME_BYTES,
                                    "TimeLimit": MAX_TIME_LIMIT_BYTES, "NormalPassItemID": STORED_ITEM_NAME_BYTES,
                                    "PremiumPassItemID": STORED_ITEM_NAME_BYTES, "RewardItemID": STORED_ITEM_NAME_BYTES})


def _text(value, maximum, *, empty=False):
    if type(value) is not str or (not empty and not value) or len(value.encode("utf-8")) > maximum:
        raise ValueError("Revival text must be bounded UTF-8")
    dumps(Container(children=(Column("Value", "S", (value,)),)))
    return value


def _integer(value, minimum=1):
    if not int32(value) or value < minimum:
        raise ValueError("Revival values must be bounded native integers")
    return value


def _date(value):
    _text(value, 64)
    match = NATIVE_MINUTES_TEXT.fullmatch(value)
    if match is not None:
        year, _, month, day, hour, minute, second = match.groups()
        result = datetime(*(int(part) for part in (year, month, day, hour, minute, second or "0")), tzinfo=JST)
    else:
        if "T" not in value:
            raise ValueError("Revival dates require complete native or ISO timestamps")
        try:
            result = instant(datetime.fromisoformat(value)).astimezone(JST)
        except OverflowError as error:
            raise ValueError("Revival date is outside the supported calendar") from error
    if result.microsecond:
        raise ValueError("Revival dates require whole-second precision")
    return result


def _date_text(value):
    return value.strftime(NATIVE_DATETIME)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class EventPassRevivalRules:
    pass_id: str
    mission_points: Mapping[int | str, int]
    start_at: str | None = None
    end_at: str | None = None
    display_text: str = "ローカル復刻"

    def __post_init__(self):
        _text(self.pass_id, MAX_ITEM_NAME_BYTES)
        _text(self.display_text, MAX_TIME_LIMIT_BYTES)
        if not isinstance(self.mission_points, Mapping) or not 1 <= len(self.mission_points) <= 65535:
            raise ValueError("Revival requires a bounded nonempty mission mapping")
        points = {}
        for key, value in self.mission_points.items():
            if type(key) is str and re.fullmatch(r"[1-9][0-9]*", key):
                key = int(key)
            key = _integer(key)
            if key in points:
                raise ValueError("Revival mission identities are duplicated")
            points[key] = _integer(value)
        if sum(points.values()) > INT32_MAX:
            raise ValueError("Revival daily points exceed int32")
        object.__setattr__(self, "mission_points", MappingProxyType(dict(sorted(points.items()))))
        for name in ("start_at", "end_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _date_text(_date(value)))
        if self.start_at is not None and self.end_at is not None and _date(self.start_at) > _date(self.end_at):
            raise ValueError("Revival availability dates are reversed")

    @property
    def content_version(self):
        return _hash({"schema": 1, "pass_id": self.pass_id, "mission_points": dict(self.mission_points),
                      "start_at": self.start_at, "end_at": self.end_at, "display_text": self.display_text})

    def to_dict(self):
        return {"pass_id": self.pass_id, "mission_points": {str(key): value for key, value in self.mission_points.items()},
                "start_at": self.start_at, "end_at": self.end_at, "display_text": self.display_text}


def load_revival_rules(path):
    if path is None:
        return None
    values = deserialize(read_bounded(path, MAX_RULES_BYTES))
    if (not isinstance(values, dict) or set(values) - EventPassRevivalRules.__dataclass_fields__.keys()
            or not {"pass_id", "mission_points"} <= set(values)):
        raise ValueError("Revival configuration requires known fields and a pass with mission points")
    return EventPassRevivalRules(**values)


@dataclass(frozen=True)
class MissionRevivalChange:
    condition_id: int
    source_index: int
    base_row: Mapping
    effective_row: Mapping
    unchanged_fields_sha256: str


@dataclass(frozen=True)
class EventPassRevivalResult:
    replacements: Mapping[str, bytes]
    metadata: Mapping
    pass_id: str | None
    mission_points: Mapping[int, int]
    rules_hash: str | None
    source_hashes: Mapping[str, str]
    effective_hashes: Mapping[str, str]
    mission_changes: tuple[MissionRevivalChange, ...]


def _table(payload, expected):
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_BYTES:
        raise ValueError("Revival resources must be bounded immutable bytes")
    data = decompress_all4(payload, LIMITS) if payload.startswith(b"ALL4") else payload
    parsed = parse_table(data, LIMITS)
    fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
    if fields != expected or len(fields) != len(parsed["fields"]) or not parsed["rows"]:
        raise ValueError("Revival resource schema differs from the verified contract")
    return parsed


def _passes(rows):
    groups, derived, identifiers = {}, [], set()
    name = ""
    start = end = None
    display, status, point = "", 0, 0
    for index, row in enumerate(rows):
        identifier = _integer(row["ID"])
        if identifier in identifiers:
            raise ValueError("Event pass reward identities are duplicated")
        identifiers.add(identifier)
        for field, maximum in PASS_TEXT_BYTES.items():
            _text(row[field], maximum, empty=True)
        start = _date(row["StartDate"]) if row["StartDate"] else start
        end = _date(row["EndDate"]) if row["EndDate"] else end
        display = row["TimeLimit"] or display
        status = _integer(row["Status"], 0) or status
        point = _integer(row["RequiredPoint"], 0) or point
        if row["NormalPassItemID"]:
            name = row["NormalPassItemID"]
            if name in groups:
                raise ValueError("Event pass groups are duplicated")
            groups[name] = []
        if not name or start is None or end is None or start > end or not status or not point:
            raise ValueError("Event pass inheritance lacks valid initial values")
        _integer(row["RewardItemType"], 0)
        _integer(row["RewardItemParam"], 0)
        value = (name, start, end, display, status, point, row["PremiumPassItemID"])
        derived.append(value)
        groups[name].append(index)
    return groups, derived


def build_event_pass_revival(event_pass_bytes: bytes, glory_bytes: bytes,
                            rules: EventPassRevivalRules | None) -> EventPassRevivalResult:
    payloads = {PASS_RESOURCE: event_pass_bytes, MISSION_RESOURCE: glory_bytes}
    if any(type(value) is not bytes or not 0 < len(value) <= MAX_BYTES for value in payloads.values()):
        raise ValueError("Revival resources must be bounded immutable bytes")
    source_hashes = {name: hashlib.sha256(value).hexdigest() for name, value in payloads.items()}
    if rules is None:
        return EventPassRevivalResult(_freeze({}), _freeze({"enabled": False}), None, _freeze({}), None,
                                      _freeze(source_hashes), _freeze(source_hashes), ())
    if not isinstance(rules, EventPassRevivalRules):
        raise ValueError("Revival requires verified rules")
    _text(rules.display_text, MAX_TIME_LIMIT_BYTES)
    passes = _table(event_pass_bytes, FIELDS)
    missions = _table(glory_bytes, MISSION_FIELDS)
    groups, before = _passes(passes["rows"])
    if rules.pass_id not in groups:
        raise ValueError("Selected event pass does not exist")
    selected = groups[rules.pass_id]
    first = selected[0]
    start = _date(rules.start_at) if rules.start_at is not None else before[first][1]
    end = _date(rules.end_at) if rules.end_at is not None else before[first][2]
    if start > end:
        raise ValueError("Revival availability dates are reversed")
    if any(value[0] != rules.pass_id and value[4] == 1 and start <= value[2] and value[1] <= end for value in before):
        raise ValueError("Revival conflicts with another active event pass")
    pass_updates = {}
    target_values = {"StartDate": _date_text(start), "EndDate": _date_text(end),
                     "TimeLimit": rules.display_text, "Status": 1}
    for index in selected:
        row = passes["rows"][index]
        pass_updates[index] = {name: value for name, value in target_values.items() if index == first or row[name]}
    mission_indices = {}
    for index, row in enumerate(missions["rows"]):
        identifier = _integer(row["ConditionID"])
        if identifier in mission_indices:
            raise ValueError("Mission identities are duplicated")
        mission_indices[identifier] = index
    mission_updates = {}
    for identifier, points in rules.mission_points.items():
        if identifier not in mission_indices:
            raise ValueError("Selected daily mission does not exist")
        index = mission_indices[identifier]
        row = missions["rows"][index]
        if (row["PeriodType"], row["DispTabType"], row["DisplayType"]) != (1, 1, 0):
            raise ValueError("Revival points may only bind existing visible daily missions")
        binding = row["EventPassItemID"], row["EventPassPoint"]
        if binding not in (("", 0), (rules.pass_id, points)):
            raise ValueError("Mission already has a different event pass binding")
        mission_updates[index] = {"EventPassItemID": rules.pass_id, "EventPassPoint": points}
    effective = {PASS_RESOURCE: patch_table_cells(event_pass_bytes, pass_updates),
                 MISSION_RESOURCE: patch_table_cells(glory_bytes, mission_updates)}
    final_pass = _table(effective[PASS_RESOURCE], FIELDS)
    final_mission = _table(effective[MISSION_RESOURCE], MISSION_FIELDS)
    _, after = _passes(final_pass["rows"])
    for index, (old, new) in enumerate(zip(before, after)):
        if index in selected:
            if new != (old[0], start, end, rules.display_text, 1, old[5], old[6]):
                raise ValueError("Revival did not replace the selected pass consistently")
        elif old != new:
            raise ValueError("Revival would change inheritance in another pass")
    changes = []
    for identifier, points in rules.mission_points.items():
        index = mission_indices[identifier]
        old, new = missions["rows"][index], final_mission["rows"][index]
        expected = {**old, "EventPassItemID": rules.pass_id, "EventPassPoint": points}
        if new != expected:
            raise ValueError("Revival changed unrelated mission fields")
        base = {key: value for key, value in old.items() if key not in ("EventPassItemID", "EventPassPoint")}
        changes.append(MissionRevivalChange(identifier, index, _freeze(old), _freeze(new), _hash(base)))
    metadata = {"enabled": True, "pass_id": rules.pass_id, "pass_source_index": first,
        "reward_count": len(selected), "start_at": _date_text(start), "end_at": _date_text(end),
        "display_text": rules.display_text, "mission_ids": tuple(rules.mission_points),
        "daily_points": sum(rules.mission_points.values()), "policy": "explicit_local_revival"}
    return EventPassRevivalResult(_freeze({name: value for name, value in effective.items() if value != payloads[name]}),
        _freeze(metadata), rules.pass_id, rules.mission_points, rules.content_version, _freeze(source_hashes),
        _freeze({name: hashlib.sha256(value).hexdigest() for name, value in effective.items()}), tuple(changes))
