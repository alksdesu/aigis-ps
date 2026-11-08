from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from types import MappingProxyType

from .errors import ProtocolError
from .native_clock import NativeDateUndetermined, native_clock, parse_native_date
from .native_numbers import int32
from .quest_modes import _window_override


CAMPAIGN_RESOURCE = "GrowthCampaignConfig.atb"
CONTENT_SCHEMA = 1
DATE_FIELDS = ("DateOpen", "DateClose")
VALUE_FIELDS = ("QuestID", "Count", "Ratio")
FIELDS = ("MissionID", *DATE_FIELDS, "ItemID", "ID", *VALUE_FIELDS)
STATUS_LIMIT = 256
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
RULES = MappingProxyType({
    "mission": "zero_MissionID_continues_previous_row", "entry": "MissionID_and_ID_last_row_wins",
    "quest": "zero_QuestID_covers_mission", "window": "unset_bound_or_open_le_clock_lt_close",
    "dates": "AACF90_with_client_mktime_bounds", "clock": "AAC970_whole_seconds_float32",
    "unverifiable": "whole_mission",
})


@dataclass(frozen=True)
class CampaignEntry:
    row: int
    mission_id: int
    entry_id: int
    quest_id: int
    opens: datetime | None
    closes: datetime | None
    item_id: str
    count: int
    ratio: int
    overridden: bool = False

    def covers(self, quest_id: int) -> bool:
        return self.quest_id == 0 or self.quest_id == quest_id

    def active(self, now: datetime) -> bool:
        return self.open_at(native_clock(now))

    def open_at(self, clock: datetime) -> bool:
        return self.opens is None or self.closes is None or self.opens <= clock < self.closes


@dataclass(frozen=True)
class CampaignFault:
    code: str
    message: str
    row: int | None = None
    mission_id: int | None = None
    entry_id: int | None = None
    field: str | None = None


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat(timespec="milliseconds")


def _entry_record(entry: CampaignEntry) -> dict:
    return {"row": entry.row, "mission_id": entry.mission_id, "entry_id": entry.entry_id, "quest_id": entry.quest_id,
            "opens": _iso(entry.opens), "closes": _iso(entry.closes), "item_id": entry.item_id, "count": entry.count,
            "ratio": entry.ratio, "overridden": entry.overridden}


def _fault_record(fault: CampaignFault) -> dict:
    return {"code": fault.code, "message": fault.message, "row": fault.row, "mission_id": fault.mission_id,
            "entry_id": fault.entry_id, "field": fault.field}


def _record(result: CampaignEntry | CampaignFault) -> dict:
    return _entry_record(result) if isinstance(result, CampaignEntry) else _fault_record(result)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _source_sha256(value, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or not _SHA256.match(value):
        raise ValueError("Growth campaign sources require a lowercase sha256 digest")
    return value


def _overrides(overrides) -> Mapping[tuple[int, int], tuple[datetime | None, datetime | None]]:
    if overrides is None:
        return MappingProxyType({})
    if not isinstance(overrides, Mapping):
        raise ValueError("Growth campaign overrides must map mission and entry identifiers")
    result = {}
    for key, value in overrides.items():
        if type(key) is not tuple or len(key) != 2 or not all(int32(part) for part in key):
            raise ValueError("Growth campaign overrides require native mission and entry identifiers")
        # The client would read an override from patched date text, so it lands on the same clock grid.
        result[key] = tuple(None if moment is None else native_clock(moment) for moment in _window_override(value))
    return MappingProxyType(dict(sorted(result.items())))


def _freeze(rows) -> tuple[tuple[Mapping, ...], CampaignFault | None]:
    if type(rows) not in (list, tuple):
        return (), CampaignFault("rows_invalid", "Growth campaign rows must be a native table")
    frozen = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            return tuple(frozen), CampaignFault("rows_invalid", "Growth campaign rows must be native records", index)
        frozen.append(MappingProxyType(dict(row)))
    return tuple(frozen), None


class GrowthCampaign:
    def __init__(self, rows: tuple[Mapping, ...], *, source_sha256: str | None, overrides, fault: CampaignFault | None):
        self.source_sha256 = source_sha256
        self.overrides = _overrides(overrides)
        self._rows = rows
        self._source_fault = fault
        self._parsed, parse_fault = self._parse(rows)
        self.unverified = fault or parse_fault
        effective, unkeyed = {}, []
        for result in self._parsed:
            if result.entry_id is None:
                unkeyed.append(result)
            else:
                effective[(result.mission_id, result.entry_id)] = result
        ordered = sorted((*effective.values(), *unkeyed), key=lambda result: result.row)
        self.entries = tuple(result for result in ordered if isinstance(result, CampaignEntry))
        self.invalid_entries = tuple(result for result in ordered if isinstance(result, CampaignFault))
        self.unverified_missions = frozenset(invalid.mission_id for invalid in self.invalid_entries)
        self._orphans = tuple(result.row for result in ordered if result.mission_id == 0)
        self._known = frozenset(effective)
        missions = {}
        for entry in self.entries:
            missions.setdefault(entry.mission_id, []).append(entry)
        self._missions = MappingProxyType({key: tuple(value) for key, value in missions.items()})
        self.content_version = _digest({
            "schema": CONTENT_SCHEMA, "resource": CAMPAIGN_RESOURCE, "source_sha256": self.source_sha256,
            "unverified": None if self.unverified is None else _fault_record(self.unverified),
            "rows": [_record(result) for result in self._parsed], "overrides": self._override_records(),
            "rules": dict(RULES)})

    @classmethod
    def from_rows(cls, rows, *, source_sha256: str, overrides=None) -> GrowthCampaign:
        frozen, fault = _freeze(rows)
        return cls(frozen, source_sha256=_source_sha256(source_sha256), overrides=overrides, fault=fault)

    @classmethod
    def unavailable(cls, reason: str, *, source_sha256: str | None = None, overrides=None) -> GrowthCampaign:
        if type(reason) is not str or not reason:
            raise ValueError("An unavailable growth campaign requires a reason code")
        return cls((), source_sha256=_source_sha256(source_sha256, optional=True), overrides=overrides,
                   fault=CampaignFault(reason, "Growth campaign resource is unavailable"))

    def preview(self, overrides) -> GrowthCampaign:
        return GrowthCampaign(self._rows, source_sha256=self.source_sha256, overrides=overrides,
                              fault=self._source_fault)

    def _parse(self, rows: tuple[Mapping, ...]) -> tuple[tuple[CampaignEntry | CampaignFault, ...], CampaignFault | None]:
        parsed, mission_id = [], 0
        for index, row in enumerate(rows):
            missing = next((name for name in FIELDS if name not in row), None)
            if missing is not None:
                return tuple(parsed), CampaignFault("column_missing", "Growth campaign rows lack a native column",
                                                    index, field=missing)
            if not int32(row["MissionID"]):
                return tuple(parsed), CampaignFault("mission_id_invalid", "MissionID breaks the inherited mission chain",
                                                    index, field="MissionID")
            # 93BB90 keeps the previous row's mission when MissionID is zero.
            mission_id = row["MissionID"] or mission_id
            parsed.append(self._entry(index, mission_id, row))
        return tuple(parsed), None

    def _entry(self, index: int, mission_id: int, row: Mapping) -> CampaignEntry | CampaignFault:
        entry_id = row["ID"]
        if not int32(entry_id):
            return CampaignFault("field_invalid", "ID must be a native int32", index, mission_id, None, "ID")
        for name in VALUE_FIELDS:
            if not int32(row[name]):
                return CampaignFault("field_invalid", f"{name} must be a native int32", index, mission_id, entry_id, name)
        if type(row["ItemID"]) is not str:
            return CampaignFault("field_invalid", "ItemID must be native text", index, mission_id, entry_id, "ItemID")
        override = self.overrides.get((mission_id, entry_id))
        window = override
        if override is None:
            dates = []
            for name in DATE_FIELDS:
                if type(row[name]) is not str:
                    return CampaignFault("field_invalid", f"{name} must be native text", index, mission_id, entry_id, name)
                try:
                    dates.append(parse_native_date(row[name]))
                except NativeDateUndetermined as error:
                    return CampaignFault("date_undetermined", str(error), index, mission_id, entry_id, name)
                except ValueError as error:
                    return CampaignFault("date_unsupported", str(error), index, mission_id, entry_id, name)
            window = tuple(dates)
        return CampaignEntry(index, mission_id, entry_id, row["QuestID"], *window, row["ItemID"], row["Count"],
                             row["Ratio"], override is not None)

    def _override_records(self) -> list[dict]:
        return [{"mission_id": mission_id, "entry_id": entry_id, "opens": _iso(opens), "closes": _iso(closes),
                 "known": (mission_id, entry_id) in self._known}
                for (mission_id, entry_id), (opens, closes) in self.overrides.items()]

    def free(self, mission_id: int, quest_id: int, now: datetime) -> bool:
        if not int32(mission_id) or not int32(quest_id):
            raise ValueError("Growth campaign checks require native mission and quest identifiers")
        clock = native_clock(now)
        if self.unverified is not None:
            raise ProtocolError("entrust_source_invalid",
                                f"Growth campaign windows are unverifiable: {self.unverified.code}", 503)
        if mission_id in self.unverified_missions:
            raise ProtocolError("entrust_source_invalid",
                                f"Growth campaign windows of mission {mission_id} are unverifiable", 503)
        return any(entry.covers(quest_id) and entry.open_at(clock) for entry in self._missions.get(mission_id, ()))

    def status(self, now: datetime | None = None) -> dict:
        clock = None if now is None else native_clock(now)
        return {"resource": CAMPAIGN_RESOURCE, "verified": self.unverified is None,
                "unverified": None if self.unverified is None else _fault_record(self.unverified),
                "source_sha256": self.source_sha256, "content_version": self.content_version,
                "rows": len(self._rows), "parsed_rows": len(self._parsed), "clock": _iso(clock),
                "entry_count": len(self.entries),
                "entries": [{**_entry_record(entry), "active_now": None if clock is None else entry.open_at(clock)}
                            for entry in self.entries[:STATUS_LIMIT]],
                "orphan_rows": list(self._orphans[:STATUS_LIMIT]),
                "invalid_count": len(self.invalid_entries),
                "invalid_entries": [_fault_record(fault) for fault in self.invalid_entries[:STATUS_LIMIT]],
                "unverified_missions": sorted(self.unverified_missions)[:STATUS_LIMIT],
                "overrides": self._override_records()[:STATUS_LIMIT]}
