from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import NamedTuple

from .al_format import decompress_all4, parse_table
from .business_calendar import NATIVE_DATETIME
from .energy_clock import JST, MILLISECOND
from .growth_campaign import (CAMPAIGN_RESOURCE, DATE_FIELDS, RULES as CAMPAIGN_RULES, CampaignEntry, CampaignFault,
                              GrowthCampaign)
from .local_io import deserialize, read_bounded
from .mission_catalog import _date
from .mission_tags import STATE_NAMES, mission_tables
from .native_clock import NATIVE_EPOCH, native_clock, parse_native_date
from .native_numbers import INT32_MAX
from .quest_modes import ENDED, MISSION_CATEGORIES, OPEN, UPCOMING, MissionWindow
from .table_cell_patch import LIMITS, MAX_BYTES, patch_table_cells


MAX_RULE_BYTES = 16 * 1024
MAX_BATCHES = 256
MAX_CAMPAIGNS = MAX_BATCHES
MAX_TIMESTAMP_TEXT = 64
SCHEMA_VERSION = 1
RULE_FIELDS = frozenset(("enabled", "batches"))
CAMPAIGN_FIELD = "growth_campaigns"
WINDOW_FIELDS = frozenset(("start_at", "end_at"))
WINDOW_POLICY = "start_inclusive_end_exclusive_8CB420"
CAMPAIGN_WINDOW_POLICY = CAMPAIGN_RULES["window"]
WINDOW_MISMATCH = "quest_windows_mismatch"
CAMPAIGN_MISMATCH = "growth_campaign_windows_mismatch"
# History or Tower windows also gate menus and other modes, so only these categories may be revived.
ALLOWED_CATEGORIES = (2, 3, 10, 13)
CATEGORY_KINDS = MappingProxyType({category: MISSION_CATEGORIES[category] for category in ALLOWED_CATEGORIES})
KIND_CATEGORIES = MappingProxyType({kind: category for category, kind in CATEGORY_KINDS.items()})
SUBJUGATION_CATEGORY = KIND_CATEGORIES["Subjugation"]
SUBJUGATION_TYPE_FIELD = "SubjugationType"
# 7697A0 sends 143 for SubjugationType 1 and 74DEC0 147 for type 2 as the mission menu loads, unprompted.
COOPERATIVE_REGISTRATIONS = MappingProxyType({1: 143, 2: 147})
CONFIG_TABLES = MappingProxyType({category: mission_tables(category)[0] for category in MISSION_CATEGORIES})
QUEST_LISTS = MappingProxyType({category: mission_tables(category)[1] for category in ALLOWED_CATEGORIES})
START_FIELD, END_FIELD = "StartDateTime", "EndDateTime"
CONFIG_COLUMNS = MappingProxyType({"MissionID": ("int32",), "Enable": ("int32", "bool"),
                                   START_FIELD: ("datetime_raw",), END_FIELD: ("datetime_raw",)})
CATEGORY_COLUMNS = MappingProxyType({
    category: MappingProxyType({**CONFIG_COLUMNS, SUBJUGATION_TYPE_FIELD: ("int32",)})
    if category == SUBJUGATION_CATEGORY else CONFIG_COLUMNS for category in ALLOWED_CATEGORIES})
LIST_COLUMNS = MappingProxyType({"MissionID": ("int32",), "QuestID": ("int32",)})
IDENTITY_COLUMNS = MappingProxyType({"MissionID": ("int32",)})
CAMPAIGN_COLUMNS = MappingProxyType({"MissionID": ("int32",), **dict.fromkeys(DATE_FIELDS, ("string_ref",)),
                                     "QuestID": ("int32",)})
UNSET_DATE = ""
HALF_SECOND = timedelta(milliseconds=500)
_BATCH_KEY = re.compile(r"([A-Za-z]+):([1-9][0-9]{0,9})\Z")
_CAMPAIGN_KEY = re.compile(r"[1-9][0-9]{0,9}\Z")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(JST).isoformat(timespec="seconds")


def _milliseconds(moment: datetime | None) -> int:
    return 0 if moment is None else (moment - NATIVE_EPOCH) // MILLISECOND


def _moment(value) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Mission window bounds require an explicit UTC offset")
    try:
        local = value.astimezone(JST)
    except OverflowError as error:
        raise ValueError("Mission window bounds are outside the native calendar") from error
    if value.microsecond or local.microsecond:
        raise ValueError("Mission window bounds require whole seconds")
    if local <= NATIVE_EPOCH:
        raise ValueError("Mission window bounds must follow the native epoch")
    return local


def _instant(value) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str or not 0 < len(value) <= MAX_TIMESTAMP_TEXT:
        raise ValueError("Mission window bounds require bounded ISO 8601 text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Mission window bounds require ISO 8601 timestamps") from error
    return _moment(parsed)


def _date_text(moment: datetime | None) -> str:
    return UNSET_DATE if moment is None else moment.astimezone(JST).strftime(NATIVE_DATETIME)


def _reading(moment: datetime | None) -> datetime | None:
    # AACF90 reads a whole second through a float32 day fraction, which lands some seconds 4 ms off.
    return parse_native_date(_date_text(moment))


def _named(reading: datetime | None) -> datetime | None:
    if reading is None:
        return None
    second = (reading + HALF_SECOND).replace(microsecond=0)
    if _reading(second) != reading:
        raise ValueError("Native dates must name a whole second of the client calendar")
    return second


def _holds(text, reading: datetime | None) -> bool:
    try:
        return parse_native_date(text) == reading
    except ValueError:
        return False


def parse_batch_key(key) -> tuple[int, int]:
    match = _BATCH_KEY.fullmatch(key) if type(key) is str else None
    if match is None or match[1] not in KIND_CATEGORIES or int(match[2]) > INT32_MAX:
        raise ValueError("Mission window batches are keyed <Kind>:<smallest MissionID> within an allowed category")
    return KIND_CATEGORIES[match[1]], int(match[2])


def batch_key(category: int, mission_id: int) -> str:
    return f"{CATEGORY_KINDS[category]}:{mission_id}"


def parse_campaign_key(key) -> int:
    if type(key) is not str or _CAMPAIGN_KEY.fullmatch(key) is None or int(key) > INT32_MAX:
        raise ValueError("Growth campaign windows are keyed by a positive native MissionID")
    return int(key)


@dataclass(frozen=True)
class Window:
    start: datetime | None
    end: datetime | None

    def to_dict(self) -> dict:
        return {"start_at": _iso(self.start), "end_at": _iso(self.end)}

    def state(self, category: int, now: datetime) -> str:
        return STATE_NAMES[MissionWindow(0, category, True, self.start, self.end).state(now)]


def _rule_window(value) -> Window:
    if isinstance(value, Window):
        start, end = (None if moment is None else _moment(moment) for moment in (value.start, value.end))
    elif isinstance(value, Mapping) and set(value) == WINDOW_FIELDS:
        start, end = _instant(value["start_at"]), _instant(value["end_at"])
    else:
        raise ValueError("Mission windows require exactly start_at and end_at")
    if (start is None) != (end is None):
        raise ValueError("Mission windows give both bounds or neither")
    if start is not None and not start < end:
        raise ValueError("Mission window start must precede its end")
    return Window(start, end)


def _campaign_window(value) -> Window:
    window = _rule_window(value)
    # AACF90 folds a date outside the client's mktime range onto 1970-01-01, so such a bound names another second.
    if any(_named(_reading(bound)) != bound for bound in (window.start, window.end)):
        raise ValueError("Growth campaign windows must stay inside the client calendar, 1970-01-01 to 3001-01-19 JST")
    return window


def _normalized(values, limit: int, parse_window, parse_key, label: str) -> Mapping[str, Window]:
    if not isinstance(values, Mapping) or len(values) > limit:
        raise ValueError(f"{label} require a bounded mapping")
    normalized = {key: parse_window(value) for key, value in values.items()}
    return MappingProxyType({key: normalized[key] for key in sorted(normalized, key=parse_key)})


@dataclass(frozen=True)
class MissionWindowRules:
    enabled: bool = False
    batches: Mapping[str, Window] = field(default_factory=dict)
    growth_campaigns: Mapping[str, Window] = field(default_factory=dict)

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Mission window enabled must be a boolean")
        object.__setattr__(self, "batches", _normalized(self.batches, MAX_BATCHES, _rule_window, parse_batch_key,
                                                        "Mission window batches"))
        object.__setattr__(self, "growth_campaigns", _normalized(self.growth_campaigns, MAX_CAMPAIGNS, _campaign_window,
                                                                 parse_campaign_key, "Growth campaign windows"))

    def to_dict(self) -> dict:
        values = {"enabled": self.enabled, "batches": {key: window.to_dict() for key, window in self.batches.items()}}
        # Omitting an empty section keeps the content version of batch-only rules stable.
        if self.growth_campaigns:
            values[CAMPAIGN_FIELD] = {key: window.to_dict() for key, window in self.growth_campaigns.items()}
        return values

    @property
    def content_version(self) -> str:
        return _digest({"schema": SCHEMA_VERSION, **self.to_dict()})


def load_mission_window_rules(path) -> MissionWindowRules | None:
    if path is None:
        return None
    values = deserialize(read_bounded(Path(path), MAX_RULE_BYTES).decode("utf-8-sig"))
    if not isinstance(values, dict) or not RULE_FIELDS <= set(values) <= RULE_FIELDS | {CAMPAIGN_FIELD}:
        raise ValueError("Mission window rules require exactly enabled and batches, plus an optional growth_campaigns")
    if not isinstance(values["batches"], dict):
        raise ValueError("Mission window batches must be a JSON object")
    if not isinstance(values.get(CAMPAIGN_FIELD, {}), dict):
        raise ValueError("Growth campaign windows must be a JSON object")
    return MissionWindowRules(**values)


@dataclass(frozen=True)
class BatchMember:
    mission_id: int
    name: str | None
    quests: int
    row_index: int

    def to_dict(self) -> dict:
        return {"mission_id": self.mission_id, "name": self.name, "quests": self.quests}


@dataclass(frozen=True)
class MissionBatch:
    key: str
    category: int
    table: str
    members: tuple[BatchMember, ...]
    original: Window

    @property
    def mission_ids(self) -> tuple[int, ...]:
        return tuple(member.mission_id for member in self.members)

    def to_dict(self) -> dict:
        return {"key": self.key, "category": self.category, "kind": CATEGORY_KINDS[self.category],
                "table": self.table, "original": self.original.to_dict(),
                "members": [member.to_dict() for member in self.members]}


@dataclass(frozen=True)
class RevivedBatch:
    batch: MissionBatch
    window: Window

    def to_dict(self) -> dict:
        return {**self.batch.to_dict(), "window": self.window.to_dict()}


@dataclass(frozen=True)
class CampaignMission:
    mission_id: int
    rows: tuple[int, ...]
    quests: tuple[int, ...]
    original: Window | None

    def to_dict(self) -> dict:
        return {"mission_id": self.mission_id, "rows": len(self.rows), "quests": list(self.quests),
                "original": None if self.original is None else self.original.to_dict()}


@dataclass(frozen=True)
class RevivedCampaign:
    mission: CampaignMission
    window: Window

    def to_dict(self) -> dict:
        return {**self.mission.to_dict(), "window": self.window.to_dict()}


@dataclass(frozen=True)
class _Table:
    name: str
    payload: bytes
    fields: tuple
    rows: tuple
    sha256: str


@dataclass(frozen=True)
class _Mission:
    listed: bool
    quests: int
    batch: str | None
    registration: int | None = None


@dataclass(frozen=True)
class _Inventory:
    tables: Mapping[str, _Table]
    batches: Mapping[str, MissionBatch]
    locations: Mapping[int, tuple[str, ...]]
    missions: Mapping[tuple[int, int], _Mission]


def _parse(name: str, payload, columns: Mapping) -> _Table:
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_BYTES:
        raise ValueError(f"Mission window resource must be bounded immutable bytes: {name}")
    try:
        parsed = parse_table(decompress_all4(payload, LIMITS) if payload.startswith(b"ALL4") else payload, LIMITS)
    except ValueError as error:
        raise ValueError(f"Mission window resource is not a native table: {name}") from error
    types = {item["name"]: item["type_name"] for item in parsed["fields"]}
    if any(types.get(column) not in allowed for column, allowed in columns.items()):
        raise ValueError(f"Mission window resource columns differ from the native contract: {name}")
    return _Table(name, payload, tuple(parsed["fields"]), tuple(parsed["rows"]), hashlib.sha256(payload).hexdigest())


def _read(read_resource: Callable[[str], bytes], name: str, columns: Mapping) -> _Table:
    try:
        payload = read_resource(name)
    except (OSError, KeyError, ValueError) as error:
        raise ValueError(f"Mission window resource is unavailable: {name}") from error
    return _parse(name, payload, columns)


def _window(row: Mapping) -> Window:
    return Window(_date(row[START_FIELD]), _date(row[END_FIELD]))


def _name(row: Mapping) -> str | None:
    name = row.get("Name")
    if name is not None and type(name) is not str:
        raise ValueError("Mission names must be native text")
    return name


def _quests(table: _Table) -> dict[int, set[int]]:
    members = {}
    for row in table.rows:
        if row["MissionID"] <= 0 or row["QuestID"] <= 0:
            raise ValueError(f"Mission quest list rows require positive identifiers: {table.name}")
        members.setdefault(row["MissionID"], set()).add(row["QuestID"])
    return members


def _reader(read_resource) -> Callable[[str], bytes]:
    if not callable(read_resource):
        raise ValueError("Mission window revival requires a resource reader")
    return read_resource


def _registration(category: int, row: Mapping) -> int | None:
    return COOPERATIVE_REGISTRATIONS.get(row[SUBJUGATION_TYPE_FIELD]) if category == SUBJUGATION_CATEGORY else None


def _inventory(read_resource) -> _Inventory:
    read_resource = _reader(read_resource)
    configs, locations = {}, {}
    for category, name in CONFIG_TABLES.items():
        configs[category] = _read(read_resource, name, CATEGORY_COLUMNS.get(category, IDENTITY_COLUMNS))
        for row in configs[category].rows:
            locations.setdefault(row["MissionID"], []).append(name)
    batches, missions = {}, {}
    for category in ALLOWED_CATEGORIES:
        config = configs[category]
        quests = _quests(_read(read_resource, QUEST_LISTS[category], LIST_COLUMNS))
        groups = {}
        for index, row in enumerate(config.rows):
            identifier = row["MissionID"]
            if identifier <= 0:
                raise ValueError(f"Mission identifiers must be positive: {config.name}")
            member = BatchMember(identifier, _name(row), len(quests.get(identifier, ())), index)
            listed, registration = bool(row["Enable"]), _registration(category, row)
            if listed and member.quests and registration is None:
                groups.setdefault(_window(row), []).append(member)
            missions.setdefault((category, identifier), _Mission(listed, member.quests, None, registration))
        for original, members in groups.items():
            members.sort(key=lambda member: (member.mission_id, member.row_index))
            key = batch_key(category, members[0].mission_id)
            if key in batches:
                raise ValueError(f"Mission identifiers are duplicated within {config.name}")
            batches[key] = MissionBatch(key, category, config.name, tuple(members), original)
            for member in members:
                missions[(category, member.mission_id)] = _Mission(True, member.quests, key)
    ordered = sorted(batches, key=parse_batch_key)
    return _Inventory(MappingProxyType({configs[category].name: configs[category] for category in ALLOWED_CATEGORIES}),
                      MappingProxyType({key: batches[key] for key in ordered}),
                      MappingProxyType({key: tuple(value) for key, value in locations.items()}),
                      MappingProxyType(missions))


def mission_window_batches(read_resource: Callable[[str], bytes]) -> tuple[MissionBatch, ...]:
    return tuple(_inventory(read_resource).batches.values())


def _unknown(inventory: _Inventory, key: str) -> str:
    category, mission_id = parse_batch_key(key)
    table = CONFIG_TABLES[category]
    holders = sorted(set(inventory.locations.get(mission_id, ())))
    if table not in holders:
        return f"names no mission of {table}" + (f"; it belongs to {', '.join(holders)}" if holders else "")
    mission = inventory.missions[(category, mission_id)]
    if not mission.listed:
        return "names a mission whose Enable is 0"
    if mission.registration is not None:
        return (f"names a cooperative subjugation, whose mission menu sends {mission.registration} on entry; "
                "the server refuses it and the client resends it after every error popup")
    if not mission.quests:
        return "names a mission without member quests"
    return f"names a member that is not the smallest; use {mission.batch}"


def _configured(inventory: _Inventory, key: str) -> MissionBatch:
    batch = inventory.batches.get(key)
    if batch is None:
        raise ValueError(f"Mission window batch {key} {_unknown(inventory, key)}")
    for member in batch.members:
        holders = inventory.locations[member.mission_id]
        if holders != (batch.table,):
            raise ValueError(f"Mission window batch {key} requires member {member.mission_id} once in {batch.table} "
                             f"and in no other mission table, found {', '.join(holders)}")
    return batch


def _confirm(source: _Table, payload: bytes, windows: Mapping[int, Window]) -> str:
    result = _parse(source.name, payload, CONFIG_COLUMNS)
    if result.fields != source.fields or len(result.rows) != len(source.rows):
        raise ValueError(f"Mission window revival changed the table layout: {source.name}")
    for index, (before, after) in enumerate(zip(source.rows, result.rows)):
        expected = dict(before)
        if index in windows:
            if _window(after) != windows[index]:
                raise ValueError(f"Mission window revival did not apply the configured window: {source.name}")
            expected.update({START_FIELD: after[START_FIELD], END_FIELD: after[END_FIELD]})
        if after != expected:
            raise ValueError(f"Mission window revival changed cells outside the configured windows: {source.name}")
    return result.sha256


def _row_window(row: Mapping) -> Window | None:
    try:
        return Window(*(_named(parse_native_date(row[name])) for name in DATE_FIELDS))
    except (ValueError, OverflowError):
        return None


def _client_window(window: Window) -> tuple[datetime | None, datetime | None]:
    return _reading(window.start), _reading(window.end)


def _campaign_missions(table: _Table) -> Mapping[int, CampaignMission]:
    members, mission_id = {}, 0
    for index, row in enumerate(table.rows):
        # 93BB90 keeps the previous row's mission when MissionID is zero.
        mission_id = row["MissionID"] or mission_id
        members.setdefault(mission_id, []).append(index)
    missions = {}
    for identifier, rows in sorted(members.items()):
        if identifier > 0:
            windows = {_row_window(table.rows[index]) for index in rows}
            missions[identifier] = CampaignMission(identifier, tuple(rows),
                                                   tuple(sorted({table.rows[index]["QuestID"] for index in rows})),
                                                   next(iter(windows)) if len(windows) == 1 else None)
    return MappingProxyType(missions)


def growth_campaign_missions(read_resource: Callable[[str], bytes]) -> tuple[CampaignMission, ...]:
    return tuple(_campaign_missions(_read(_reader(read_resource), CAMPAIGN_RESOURCE, CAMPAIGN_COLUMNS)).values())


def _misjudged(judged: CampaignEntry | CampaignFault | None, mission_id: int, window: Window) -> bool:
    # The server drops superseded rows before judging, so only their dates are compared.
    if judged is None:
        return False
    if judged.mission_id != mission_id:
        return True
    if isinstance(judged, CampaignEntry):
        return (judged.opens, judged.closes) != _client_window(window)
    return judged.field in DATE_FIELDS


def _campaign_mismatches(table: _Table, windows: Mapping[int, Window]) -> list[dict]:
    served = GrowthCampaign.from_rows(table.rows, source_sha256=table.sha256)
    if served.unverified is not None:
        return [{"table": table.name, "reason": served.unverified.code}]
    judged = {result.row: result for result in (*served.entries, *served.invalid_entries)}
    missions, mismatches = _campaign_missions(table), []
    for mission_id, window in windows.items():
        rows = missions[mission_id].rows if mission_id in missions else ()
        effective = list(dict.fromkeys(_row_window(table.rows[index]) for index in rows))
        if effective != [window] or any(_misjudged(judged.get(index), mission_id, window) for index in rows):
            mismatches.append({"mission_id": mission_id, "rows": len(rows), "expected": window.to_dict(),
                               "effective": [None if item is None else item.to_dict() for item in effective]})
    return mismatches


class _Patch(NamedTuple):
    revived: tuple
    replacements: Mapping[str, bytes]
    source_sha256: Mapping[str, str]
    effective_sha256: Mapping[str, str]


_UNPATCHED = _Patch((), MappingProxyType({}), MappingProxyType({}), MappingProxyType({}))


def _revive_batches(read_resource, batches: Mapping[str, Window]) -> _Patch:
    inventory = _inventory(read_resource)
    revived = tuple(RevivedBatch(_configured(inventory, key), window) for key, window in batches.items())
    updates, windows = {}, {}
    for batch in revived:
        cells = {START_FIELD: _milliseconds(batch.window.start), END_FIELD: _milliseconds(batch.window.end)}
        for member in batch.batch.members:
            updates.setdefault(batch.batch.table, {})[member.row_index] = cells
            windows.setdefault(batch.batch.table, {})[member.row_index] = batch.window
    replacements, source_sha256, effective_sha256 = {}, {}, {}
    for name, rows in sorted(updates.items()):
        source = inventory.tables[name]
        effective = patch_table_cells(source.payload, rows)
        source_sha256[name] = source.sha256
        effective_sha256[name] = _confirm(source, effective, windows[name])
        if effective != source.payload:
            replacements[name] = effective
    return _Patch(revived, replacements, source_sha256, effective_sha256)


def _campaign_cells(row: Mapping, window: Window) -> dict[str, str]:
    # Dates the client already reads as the configured bound keep their text, so the original window patches nothing.
    bounds = zip(DATE_FIELDS, (window.start, window.end), _client_window(window))
    return {name: _date_text(bound) for name, bound, reading in bounds if not _holds(row[name], reading)}


def _confirm_campaigns(source: _Table, payload: bytes, revived: tuple[RevivedCampaign, ...]) -> str:
    result = _parse(source.name, payload, CAMPAIGN_COLUMNS)
    if result.fields != source.fields or len(result.rows) != len(source.rows):
        raise ValueError(f"Growth campaign windows changed the table layout: {source.name}")
    patched = {index for campaign in revived for index in campaign.mission.rows}
    for index, (before, after) in enumerate(zip(source.rows, result.rows)):
        expected = dict(before)
        if index in patched:
            expected.update({name: after[name] for name in DATE_FIELDS})
        if after != expected:
            raise ValueError(f"Growth campaign windows changed cells outside the configured dates: {source.name}")
    if _campaign_mismatches(result, {campaign.mission.mission_id: campaign.window for campaign in revived}):
        raise ValueError(f"Growth campaign windows did not read back through the client date parser: {source.name}")
    return result.sha256


def _revive_campaigns(read_resource, campaigns: Mapping[str, Window]) -> _Patch:
    source = _read(_reader(read_resource), CAMPAIGN_RESOURCE, CAMPAIGN_COLUMNS)
    unverified = GrowthCampaign.from_rows(source.rows, source_sha256=source.sha256).unverified
    if unverified is not None:
        raise ValueError(f"Growth campaign windows require a verified {CAMPAIGN_RESOURCE}: {unverified.code}")
    missions, revived = _campaign_missions(source), []
    for key, window in campaigns.items():
        mission = missions.get(parse_campaign_key(key))
        if mission is None:
            raise ValueError(f"Growth campaign window {key} names no mission of {CAMPAIGN_RESOURCE}")
        revived.append(RevivedCampaign(mission, window))
    updates = {index: cells for campaign in revived for index in campaign.mission.rows
               if (cells := _campaign_cells(source.rows[index], campaign.window))}
    effective = patch_table_cells(source.payload, updates)
    effective_sha256 = _confirm_campaigns(source, effective, tuple(revived))
    return _Patch(tuple(revived), {CAMPAIGN_RESOURCE: effective} if effective != source.payload else {},
                  {CAMPAIGN_RESOURCE: source.sha256}, {CAMPAIGN_RESOURCE: effective_sha256})


def campaign_state(window: Window | None, clock: datetime) -> str | None:
    if window is None:
        return None
    opens, closes = _client_window(window)
    if opens is None or closes is None:
        return STATE_NAMES[OPEN]
    if clock < opens:
        return STATE_NAMES[UPCOMING]
    return STATE_NAMES[ENDED if clock >= closes else OPEN]


@dataclass(frozen=True)
class MissionWindowRevival:
    replacements: Mapping[str, bytes]
    rules: MissionWindowRules | None = None
    batches: tuple[RevivedBatch, ...] = ()
    source_sha256: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    effective_sha256: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    campaigns: tuple[RevivedCampaign, ...] = ()

    @property
    def targets(self) -> tuple[str, ...]:
        return tuple(sorted({batch.batch.table for batch in self.batches}))

    @property
    def resources(self) -> tuple[str, ...]:
        return tuple(sorted({*self.targets, *((CAMPAIGN_RESOURCE,) if self.campaigns else ())}))

    @property
    def metadata(self) -> dict:
        rules = self.rules
        result = {"schema_version": SCHEMA_VERSION, "enabled": rules is not None and rules.enabled,
                  "rules": rules.to_dict() if rules is not None else None,
                  "rules_hash": rules.content_version if rules is not None else None,
                  "window_policy": WINDOW_POLICY,
                  "tables": {name: {"source_sha256": self.source_sha256[name],
                                    "effective_sha256": self.effective_sha256[name]} for name in self.targets},
                  "batches": [batch.to_dict() for batch in self.batches]}
        if self.campaigns:
            result[CAMPAIGN_FIELD] = {
                "table": CAMPAIGN_RESOURCE, "window_policy": CAMPAIGN_WINDOW_POLICY,
                "source_sha256": self.source_sha256[CAMPAIGN_RESOURCE],
                "effective_sha256": self.effective_sha256[CAMPAIGN_RESOURCE],
                "missions": [campaign.to_dict() for campaign in self.campaigns]}
        return result

    def describe(self, now: datetime) -> dict:
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError("Mission window states require an aware datetime")
        result = self.metadata
        for entry, batch in zip(result["batches"], self.batches):
            entry["state"] = batch.window.state(batch.batch.category, now)
            entry["original_state"] = batch.batch.original.state(batch.batch.category, now)
        if self.campaigns:
            # The server judges campaign windows on the client clock, as GrowthCampaign.free does.
            clock = native_clock(now)
            result[CAMPAIGN_FIELD]["clock"] = clock.isoformat(timespec="milliseconds")
            for entry, campaign in zip(result[CAMPAIGN_FIELD]["missions"], self.campaigns):
                entry["state"] = campaign_state(campaign.window, clock)
                entry["original_state"] = campaign_state(campaign.mission.original, clock)
        return result


def _verified(rules, message: str) -> MissionWindowRules:
    if not isinstance(rules, MissionWindowRules) or MissionWindowRules(**rules.to_dict()) != rules:
        raise ValueError(message)
    return rules


def build_mission_window_revival(read_resource: Callable[[str], bytes],
                                 rules: MissionWindowRules | None) -> MissionWindowRevival:
    if rules is None:
        return MissionWindowRevival(MappingProxyType({}))
    _verified(rules, "Mission window revival requires verified rules")
    if not rules.enabled or not (rules.batches or rules.growth_campaigns):
        return MissionWindowRevival(MappingProxyType({}), rules)
    batches = _revive_batches(read_resource, rules.batches) if rules.batches else _UNPATCHED
    campaigns = _revive_campaigns(read_resource, rules.growth_campaigns) if rules.growth_campaigns else _UNPATCHED
    return MissionWindowRevival(MappingProxyType({**batches.replacements, **campaigns.replacements}), rules,
                                batches.revived, MappingProxyType({**batches.source_sha256, **campaigns.source_sha256}),
                                MappingProxyType({**batches.effective_sha256, **campaigns.effective_sha256}),
                                campaigns.revived)


class MissionWindowMismatch(ValueError):
    code = WINDOW_MISMATCH

    def __init__(self, mismatches):
        super().__init__(WINDOW_MISMATCH)
        self.mismatches = tuple(mismatches)


class GrowthCampaignWindowMismatch(ValueError):
    code = CAMPAIGN_MISMATCH

    def __init__(self, mismatches):
        super().__init__(CAMPAIGN_MISMATCH)
        self.mismatches = tuple(mismatches)


def verify_mission_windows(read_resource: Callable[[str], bytes], revival: MissionWindowRevival | None) -> dict:
    if revival is None:
        return {"enabled": False}
    if not isinstance(revival, MissionWindowRevival):
        raise ValueError("Mission window verification requires a built revival")
    mismatches, served = [], {}
    for name in revival.targets:
        try:
            served[name] = _read(read_resource, name, CONFIG_COLUMNS)
        except ValueError as error:
            mismatches.append({"table": name, "reason": str(error)})
    for batch in revival.batches:
        table = served.get(batch.batch.table)
        if table is None:
            continue
        for member in batch.batch.members:
            rows = [row for row in table.rows if row["MissionID"] == member.mission_id]
            try:
                effective = _window(rows[0]) if len(rows) == 1 else None
            except ValueError:
                effective = None
            if effective != batch.window:
                mismatches.append({"key": batch.batch.key, "mission_id": member.mission_id, "rows": len(rows),
                                   "expected": batch.window.to_dict(),
                                   "effective": effective.to_dict() if effective is not None else None})
    if mismatches:
        raise MissionWindowMismatch(mismatches)
    report = revival.metadata
    report["verification"] = {
        "verified": True, "members": sum(len(batch.batch.members) for batch in revival.batches),
        "served_sha256": {name: table.sha256 for name, table in served.items()},
        "matches_build": all(table.sha256 == revival.effective_sha256[name] for name, table in served.items())}
    return report


def verify_growth_campaign_windows(rules: MissionWindowRules | None, read_resource: Callable[[str], bytes]) -> dict:
    if rules is None:
        return {"enabled": False}
    _verified(rules, "Growth campaign verification requires verified rules")
    configured = rules.growth_campaigns if rules.enabled else {}
    windows = {parse_campaign_key(key): window for key, window in configured.items()}
    report = {"enabled": rules.enabled, "rules_hash": rules.content_version, "table": CAMPAIGN_RESOURCE,
              "window_policy": CAMPAIGN_WINDOW_POLICY,
              "missions": [{"mission_id": mission_id, "window": window.to_dict()}
                           for mission_id, window in windows.items()]}
    if not windows:
        return {**report, "verification": {"verified": True, "missions": 0, "rows": 0, "served_sha256": None}}
    read_resource = _reader(read_resource)
    try:
        table = _read(read_resource, CAMPAIGN_RESOURCE, CAMPAIGN_COLUMNS)
    except ValueError as error:
        raise GrowthCampaignWindowMismatch([{"table": CAMPAIGN_RESOURCE, "reason": str(error)}]) from error
    mismatches = _campaign_mismatches(table, windows)
    if mismatches:
        raise GrowthCampaignWindowMismatch(mismatches)
    missions = _campaign_missions(table)
    return {**report, "verification": {"verified": True, "missions": len(windows),
                                       "rows": sum(len(missions[mission_id].rows) for mission_id in windows),
                                       "served_sha256": table.sha256}}
