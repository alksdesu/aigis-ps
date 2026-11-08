from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum, auto
import functools
import importlib
import re
from types import MappingProxyType

from .al_format import parse_table
from .drop_bonus import bonus_grant
from .errors import ProtocolError
from .mission_catalog import _date
from .native_calendar import native_day, native_weekday
from .native_clock import native_clock
from .native_numbers import UINT32_MASK, int32, signed32


CLEAR_FLAG = 0x01000000
THREE_STAR_FLAG = 0x00100000
HARD_STAR_FLAG = 0x00200000
SLOT_FLAGS = (0x10000, 0x1000, 0x100, 0x10, 1)
# The client skips a quest's event once its record holds this bit, and sets it itself before playing one (65D6D0).
EPISODE_FLAG = 0x40000000
# 4A1F40 stops recording obtained drops once it holds seven.
MAX_OBTAINED = 7
# Mission Enable masks hold one bit per native platform code (8D8140 +80).
PLATFORM_CODES = range(32)
MISSION_CATEGORIES = MappingProxyType({
    0: "Story", 1: "Emergency", 2: "Subjugation", 3: "Reproduce", 4: "DailyReproduce", 5: "Daily",
    6: "Challenge", 7: "Tutorial", 8: "DevilAdvent", 9: "Harlem", 10: "Raid", 11: "Assault", 12: "Tower",
    13: "Training", 14: "History", 15: "Colosseum", 16: "Empire", 17: "GoddessRevival",
})
# These loaders test Enable as a platform bit mask (8CEEA0); the rest register any nonzero cell through B09FE0.
PLATFORM_MASK_CATEGORIES = frozenset((1, 2, 3, 8, 10, 11, 12))
# Native 9935E0 skips GoldGet for 討伐/大総力/強襲/英傑の塔/トレーニング through its fixed name table.
GOLD_EXEMPT_CATEGORIES = frozenset((2, 10, 11, 12, 13))
# Native 8C5360 names these categories' maps Map%04d_%04d.aar after the owning mission; the rest use Map%04d.aar.
PREFIXED_MAP_CATEGORIES = frozenset((2, 12, 13, 14, 15, 16, 17))
# History (8C9BB0) and goddess revival (8C96D0) take members from Config.QuestIdList, not a quest list table.
QUEST_ID_LIST_CATEGORIES = frozenset((14, 17))
# Only the story (8D83A0) and empire (8C8020) loaders keep KeyQuestID for the 8CE3C0 playability check.
KEY_QUEST_CATEGORIES = frozenset((0, 16))
SCORE_TYPES = frozenset((6, 7, 13))
UNBOOSTED = 1
# Menu scripts set QuestBoost to at most 3, and 4316C0 sends Boost.V only above 1.
MAX_BOOST = 3
BOOSTS = range(UNBOOSTED, MAX_BOOST + 1)
HARD_RANKS = MappingProxyType({0: 50, 3: 5, 9: 50, 18: 5})
NO_MISSION_RANK = 300
UPCOMING, OPEN, ENDED = 0, 1, 2
# The client re-checks 679B10 at departure, so a start may reach the server just after the JST day changes.
DAY_OPEN_GRACE = timedelta(seconds=300)
OPEN_WEEK_DAYS = MappingProxyType({8: (1, 7), 9: (3, 6), 10: (5, 6)})
CONDITION_FIELDS = ("ID_Quest", "Type_Condition", "Param_Condition", "ID_QuestBegin", "ID_QuestEnd")
MODE_MODULES = ("quest_mode_story", "quest_mode_tutorial", "quest_mode_challenge", "quest_mode_daily",
                "quest_mode_subjugation", "quest_mode_reproduce", "quest_mode_devil", "quest_mode_harlem",
                "quest_mode_raid", "quest_mode_tower", "quest_mode_training", "quest_mode_history",
                "quest_mode_empire", "quest_mode_goddess")
UNAVAILABLE = MappingProxyType({
    3: "QuestTable has no Type 3 quests",
    7: "QuestTable has no Type 7 quests",
    13: "Assault mission quests are absent from QuestTable",
})
# User-audited entrust scope; requests still pass the native gates.
ENTRUST_TYPES = frozenset((0, 2, 8, 16, 18))
# Entrust skips the battle, so a mode overriding any of these battle hooks must declare its entrust counterpart.
ENTRUST_PARITY_HOOKS = ("check_start", "start_boost", "prepare_start", "score_result", "settle")
# Mirrors event_mission_rewards.Family, which imports this module.
EVENT_REWARD_FAMILIES = frozenset(("score", "emergency", "after_event"))
# The only journal part a settle hook may change: its own server-private progress, keyed by policy name.
MODE_PROGRESS = "mode_progress"
_REQUEST_QUESTS = re.compile(r"[0-9]{1,10}(?:,[0-9]{1,10})*\Z")
QUEST_ID_LIST_PATTERN = re.compile(r"[0-9, ]*\Z")


@dataclass(frozen=True)
class ModeSpec:
    quest_type: int
    categories: tuple[int, ...]

    @property
    def mission_category(self) -> int | None:
        return self.categories[0] if self.categories else None

    @property
    def resources(self) -> tuple[str, ...]:
        return tuple(MISSION_CATEGORIES[category] for category in self.categories)

    @property
    def score_mode(self) -> bool:
        return self.quest_type in SCORE_TYPES

    @property
    def gold_counted(self) -> bool:
        return self.mission_category is not None and self.mission_category not in GOLD_EXEMPT_CATEGORIES

    @property
    def hard_rank(self) -> int | None:
        return HARD_RANKS.get(self.quest_type)

    @property
    def entrust(self) -> bool:
        return self.quest_type in ENTRUST_TYPES


MODE_SPECS = MappingProxyType({spec.quest_type: spec for spec in (
    ModeSpec(0, (0,)), ModeSpec(1, (6,)), ModeSpec(2, (5,)), ModeSpec(3, ()), ModeSpec(4, (7,)),
    ModeSpec(6, (2,)), ModeSpec(7, ()), ModeSpec(8, (3, 14)), ModeSpec(9, (8,)), ModeSpec(10, (9,)),
    ModeSpec(11, (10,)), ModeSpec(13, (11,)), ModeSpec(14, (12,)), ModeSpec(15, (13,)), ModeSpec(16, (14,)),
    ModeSpec(18, (16, 1)), ModeSpec(19, (17,)),
)})


@dataclass(frozen=True)
class ModeServices:
    presents: object = None
    card_status: object = None
    possessions: object = None
    tower: object = None
    subjugation: object = None


@dataclass(frozen=True)
class StartContext:
    catalog: object
    quest: object
    mode: object
    records: Mapping
    rank: int
    now: datetime
    hard: bool
    platform_code: int
    state: Mapping | None = None
    request: object = None
    services: ModeServices = ModeServices()
    selector: MissionSelector | None = None


@dataclass(frozen=True)
class SettleContext:
    catalog: object
    quest: object
    mode: object
    request: object
    state: dict
    hard: bool
    stars: int
    now: datetime
    old_flags: int
    flags: int
    first_clear: bool
    mission_id: int | None
    services: ModeServices = ModeServices()
    boost: int = UNBOOSTED
    score: int | None = None
    previous_record: Mapping | None = None


@dataclass(frozen=True)
class ScoreContext:
    catalog: object
    quest: object
    mode: object
    request: object
    record: Mapping
    boost: int
    now: datetime
    mission_id: int | None
    services: ModeServices = ModeServices()


@dataclass(frozen=True)
class ScoreResult:
    # A score mode's MaxRank is the packed value 8CEAF0/8CEC40 decode, not a star count.
    max_rank: int
    score: int
    details: Mapping


@dataclass(frozen=True)
class SettleOutcome:
    state: dict
    children: tuple = ()
    item_ids: tuple[str, ...] = ()
    # Present rewards in presents.enqueue form; the settlement mails them or queues them while the mailbox is full.
    presents: tuple[dict, ...] = ()


@dataclass(frozen=True)
class EntrustContext:
    catalog: object
    quest: object
    mode: object
    state: dict
    mission_id: int
    quantity: int
    now: datetime
    platform_code: int
    services: ModeServices = ModeServices()


@dataclass(frozen=True)
class EntrustDraw:
    # TreasureInfo reports each DropID once with its total over the whole batch.
    counts: Mapping[int, int]
    units: tuple[Mapping, ...]
    items: Mapping[str, int]


@dataclass(frozen=True)
class EntrustRewards:
    draw: EntrustDraw
    children: tuple = ()


@dataclass(frozen=True)
class SpecialCondition:
    kind: int
    threshold: int
    first_quest_id: int
    last_quest_id: int


def map_archive(map_no: int, mission_id: int | None = None) -> str:
    return f"Map{map_no:04d}.aar" if mission_id is None else f"Map{mission_id:04d}_{map_no:04d}.aar"


def require_platform(code) -> int:
    if type(code) is not int or code not in PLATFORM_CODES:
        raise ProtocolError("quest_platform_invalid", "Mission availability requires a native platform code")
    return code


def require_row_values(row: Mapping, values: Mapping[str, int], label: str) -> None:
    for name, expected in values.items():
        value = row.get(name)
        if type(value) is not int or value != expected:
            raise ProtocolError("quest_definition_invalid", f"{label} field {name} differs from the native contract", 503)


def tag_span_end(tag: int, span: int) -> int:
    # 8D75A0 adds TagIDRange in a 32-bit register before its signed compare, so the end wraps.
    return signed32(tag + span)


@dataclass(frozen=True)
class MissionWindow:
    mission_id: int
    category: int
    enable: bool | int
    start: datetime | None
    end: datetime | None
    request_quests: tuple[int, ...] = ()
    request_rank: int = 0
    key_quest: int = 0
    tag: int = 0
    tag_span: int = 0

    @property
    def listed(self) -> bool:
        # Nonzero Enable on some platform; per-platform registration (8D8140 +80) is enabled().
        return bool(self.enable)

    def archive(self, map_no: int) -> str:
        return map_archive(map_no, self.mission_id if self.category in PREFIXED_MAP_CATEGORIES else None)

    def enabled(self, platform_code: int) -> bool:
        if self.category not in PLATFORM_MASK_CATEGORIES:
            return bool(self.enable)
        return bool((1 << require_platform(platform_code)) & self.enable)

    def covers(self, tag: int) -> bool:
        return self.tag <= tag <= tag_span_end(self.tag, self.tag_span)

    def state(self, now: datetime) -> int:
        # 8CB420 compares the raw End, so a dated mission with End 0 ends as soon as it starts.
        if self.start is None and self.end is None:
            return OPEN
        # 8CB420 reads the client clock, which keeps whole seconds on its 4 ms grid.
        clock = native_clock(now)
        if self.start is not None and clock < self.start:
            return UPCOMING
        if self.end is None or clock >= self.end:
            return ENDED
        return OPEN


@dataclass(frozen=True)
class MissionSelector:
    accepts: Callable[[MissionWindow], bool]
    code: str
    message: str
    choose: Callable[[tuple[MissionWindow, ...]], MissionWindow] | None = None

    def pick(self, playable: tuple[MissionWindow, ...]) -> MissionWindow:
        if self.choose is None:
            return playable[0]
        chosen = self.choose(playable)
        if not any(chosen is mission for mission in playable):
            raise ProtocolError("quest_mission_selector_invalid",
                                "Mission selectors must choose one of the playable missions", 503)
        return chosen


# Ordered by how far a mission got, so a refusal reports the furthest stage any listed mission reached.
class _MissionStage(IntEnum):
    CLOSED = auto()
    UNSELECTED = auto()
    RANK_SHORT = auto()
    LOCKED = auto()
    PLAYABLE = auto()


def _record(records, quest_id):
    return records.get(quest_id) or {}


def _stars(row):
    return row.get("MaxRank", 0)


def _cleared(row):
    return bool(row.get("AchieveFlag", 0) & CLEAR_FLAG)


def quest_cleared(records: Mapping, quest_id: int) -> bool:
    return _cleared(_record(records, quest_id))


def slot_obtained(record: Mapping | None, slot: int) -> bool:
    # 65D900 reads the slot bit for slots 1-5; any other slot counts once the record has a nonzero flag word.
    if record is None:
        return False
    if 1 <= slot <= len(SLOT_FLAGS):
        return bool(record["AchieveFlag"] & SLOT_FLAGS[slot - 1])
    return record["AchieveFlag"] != 0


def candidate_attributes(record: Mapping | None, candidates) -> tuple[tuple[int, int, int], ...]:
    return tuple((index, candidate.slot, int(slot_obtained(record, candidate.slot)) | candidate.special_attr)
                 for index, candidate in enumerate(candidates))


def condition_allows(condition: SpecialCondition, records: Mapping) -> bool:
    matching = [row for identifier, row in records.items()
                if condition.first_quest_id <= identifier <= condition.last_quest_id]
    if condition.kind == 1:
        return sum(_stars(row) for row in matching) >= condition.threshold
    if condition.kind == 2:
        return sum(_cleared(row) for row in matching) >= condition.threshold
    if condition.kind == 3:
        return sum(_stars(row) for row in matching) >= 3 * (condition.last_quest_id - condition.first_quest_id + 1)
    # 632A90 moves on to the next row for any other kind, so it never locks the quest.
    return True


def special_condition_satisfied(records: Mapping, quest_id: int, conditions: Mapping) -> bool:
    return all(condition_allows(condition, records) for condition in conditions.get(quest_id, ()))


def parse_special_conditions(raw: bytes) -> Mapping[int, tuple[SpecialCondition, ...]]:
    table = parse_table(raw)
    if (tuple(field["name"] for field in table["fields"]) != CONDITION_FIELDS
            or any(field["type_name"] != "int32" for field in table["fields"])):
        raise ValueError("Special condition schema differs from native physical columns")
    conditions = {}
    for row in table["rows"]:
        values = tuple(row[name] for name in CONDITION_FIELDS)
        if any(not int32(value) for value in values):
            raise ValueError("Special condition fields must be native integers")
        identifier, kind, threshold, first, last = values
        conditions.setdefault(identifier, []).append(SpecialCondition(kind, threshold, first, last))
    return MappingProxyType({key: tuple(value) for key, value in conditions.items()})


def quest_open_on(row: Mapping, moment: datetime) -> bool:
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise ProtocolError("quest_clock_invalid", "Daily availability requires an aware datetime", 503)
    day, weekday = native_day(moment), native_weekday(moment)
    holding, week = row.get("_HoldingParam", 0), row.get("OpenWeek", 0)
    first, last = row.get("StartOpenDay", 0), row.get("EndOpenDay", 0)
    if any(not int32(value) for value in (holding, week, first, last)):
        raise ProtocolError("quest_definition_invalid", "Daily availability fields must be native integers", 503)
    # 679B10: a nonzero _HoldingParam replaces OpenWeek with a Sunday-first weekday bit mask.
    if holding:
        if not holding & (1 << (weekday - 1)):
            return False
    elif week and weekday not in OPEN_WEEK_DAYS.get(week, (week,)):
        return False
    return not (first and last and (first > day or day >= last))


def _window_override(value):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError("Window overrides require a start and end")
    start, end = value
    for moment in (start, end):
        if moment is not None and (not isinstance(moment, datetime) or moment.utcoffset() is None):
            raise ValueError("Window overrides require aware datetimes")
    if start is not None and end is None:
        raise ValueError("A dated window override requires an end")
    if start is not None and end <= start:
        raise ValueError("Window override end must follow its start")
    return start, end


def validate_window_overrides(overrides) -> Mapping[int, tuple[datetime | None, datetime | None]]:
    if overrides is None:
        return MappingProxyType({})
    if not isinstance(overrides, Mapping):
        raise ValueError("Window overrides must be a mission mapping")
    result = {}
    for mission_id, value in overrides.items():
        if not int32(mission_id) or mission_id <= 0:
            raise ValueError("Window overrides require positive mission identifiers")
        result[mission_id] = _window_override(value)
    return MappingProxyType(result)


class MissionWindows:
    def __init__(self, tables: Mapping[int, tuple[list, list]], quest_ranks: Mapping[int, int], *, overrides=None):
        overrides = validate_window_overrides(overrides)
        missions, members, quests = {}, {}, {}
        for category, (config_rows, list_rows) in tables.items():
            for row in config_rows:
                identifier = row.get("MissionID")
                if not int32(identifier) or identifier <= 0 or identifier in missions:
                    raise ValueError("Mission identifiers are invalid or duplicated")
                enable = row.get("Enable")
                if type(enable) is not bool and not int32(enable):
                    raise ValueError("Mission enable flags must be native booleans or platform masks")
                start, end = _date(row.get("StartDateTime")), _date(row.get("EndDateTime"))
                if identifier in overrides:
                    start, end = overrides[identifier]
                text = row.get("RequestQuestIds", "")
                rank = row.get("RequestClearRank", 0)
                if not isinstance(text, str) or (text and not _REQUEST_QUESTS.fullmatch(text)) or not int32(rank) or rank < 0:
                    raise ValueError("Mission request quests are invalid")
                requested = tuple(int(value) for value in text.split(",")) if text else ()
                if any(not int32(value) or value <= 0 for value in requested):
                    raise ValueError("Mission request quests must be positive native integers")
                key_quest = row.get("KeyQuestID", 0) if category in KEY_QUEST_CATEGORIES else 0
                if not int32(key_quest) or key_quest < 0:
                    raise ValueError("Mission key quests must be nonnegative native integers")
                tag, tag_span = row.get("TagID", 0), row.get("TagIDRange", 0)
                if not int32(tag) or not int32(tag_span):
                    raise ValueError("Mission tags must be native integers")
                missions[identifier] = MissionWindow(identifier, category, enable, start, end, requested, rank,
                                                     key_quest, tag, tag_span)
            for row in list_rows:
                mission_id, quest_id = row.get("MissionID"), row.get("QuestID")
                if not int32(mission_id) or mission_id <= 0 or not int32(quest_id) or quest_id <= 0:
                    raise ValueError("Mission quest list rows are invalid")
                if mission_id not in missions or missions[mission_id].category != category:
                    continue
                members.setdefault(mission_id, set()).add(quest_id)
                quests.setdefault(quest_id, set()).add(mission_id)
        self._categories = tuple(tables)
        self._missions = MappingProxyType(missions)
        self._ordered = tuple(missions[identifier] for identifier in sorted(missions))
        self._members = MappingProxyType({key: tuple(sorted(value)) for key, value in members.items()})
        self._quests = MappingProxyType({key: tuple(sorted(value)) for key, value in quests.items()})
        self._minimum = MappingProxyType({identifier: min((quest_ranks[quest] for quest in members.get(identifier, ())
                                                           if quest in quest_ranks), default=NO_MISSION_RANK)
                                          for identifier in missions})

    @property
    def categories(self) -> tuple[int, ...]:
        return self._categories

    def mission(self, mission_id: int) -> MissionWindow:
        if mission_id not in self._missions:
            raise ProtocolError("quest_mission_unknown", "Mission is absent from the verified configuration", 409)
        return self._missions[mission_id]

    def missions(self, quest_id: int) -> tuple[MissionWindow, ...]:
        return tuple(self._missions[identifier] for identifier in self._quests.get(quest_id, ()))

    def members(self, mission_id: int) -> tuple[int, ...]:
        return self._members.get(self.mission(mission_id).mission_id, ())

    def ordered(self) -> tuple[MissionWindow, ...]:
        return self._ordered

    def archive(self, quest_id: int, map_no: int) -> tuple[str, int | None]:
        listed = [mission for mission in self.missions(quest_id) if mission.listed]
        archives = {mission.archive(map_no) for mission in listed}
        if not archives:
            raise ProtocolError("quest_closed", "Quest has no enabled mission that names its map", 409)
        if len(archives) != 1:
            raise ProtocolError("quest_mission_ambiguous", "Enabled missions name different map archives for one quest", 503)
        owners = sorted({mission.mission_id for mission in listed if mission.category in PREFIXED_MAP_CATEGORIES})
        return archives.pop(), owners[0] if len(owners) == 1 else None

    def minimum_rank(self, mission_id: int) -> int:
        return self._minimum[self.mission(mission_id).mission_id]

    @staticmethod
    def request_satisfied(mission: MissionWindow, records: Mapping) -> bool:
        # 8CE430 only registers a positive RequestClearRank; every other requested quest needs three stars.
        required = mission.request_rank if mission.request_rank > 0 else 3
        return all(_stars(_record(records, quest_id)) >= required for quest_id in mission.request_quests)

    @staticmethod
    def key_satisfied(mission: MissionWindow, records: Mapping) -> bool:
        return not mission.key_quest or quest_cleared(records, mission.key_quest)

    def require_playable(self, quest_id: int, *, now: datetime, rank: int, records: Mapping, platform_code: int,
                         selector: MissionSelector | None = None) -> MissionWindow:
        missions = self.missions(quest_id)
        if not missions:
            raise ProtocolError("quest_closed", "Quest does not belong to a verified mission", 409)
        collect = selector is not None and selector.choose is not None
        reached, playable = _MissionStage.CLOSED, []
        for mission in missions:
            stage = self._stage(mission, now=now, rank=rank, records=records, platform_code=platform_code,
                                selector=selector)
            if stage is not _MissionStage.PLAYABLE:
                reached = max(reached, stage)
            elif not collect:
                return mission
            else:
                playable.append(mission)
        if playable:
            return selector.pick(tuple(playable))
        raise self._refusal(reached, selector)

    def _stage(self, mission: MissionWindow, *, now: datetime, rank: int, records: Mapping, platform_code: int,
               selector: MissionSelector | None) -> _MissionStage:
        if not mission.enabled(platform_code) or mission.state(now) != OPEN:
            return _MissionStage.CLOSED
        if selector is not None and not selector.accepts(mission):
            return _MissionStage.UNSELECTED
        if rank < self.minimum_rank(mission.mission_id):
            return _MissionStage.RANK_SHORT
        if not self.request_satisfied(mission, records) or not self.key_satisfied(mission, records):
            return _MissionStage.LOCKED
        return _MissionStage.PLAYABLE

    @staticmethod
    def _refusal(reached: _MissionStage, selector: MissionSelector | None) -> ProtocolError:
        if reached is _MissionStage.LOCKED:
            return ProtocolError("quest_locked", "The mission requires its requested or key quest results", 409)
        if reached is _MissionStage.RANK_SHORT:
            return ProtocolError("quest_rank_required", "Player rank is below the mission requirement", 409)
        if reached is _MissionStage.UNSELECTED:
            return ProtocolError(selector.code, selector.message, 409)
        return ProtocolError("quest_closed", "The quest mission is disabled or outside its window", 409)


class ModePolicy:
    name = "mode"
    quest_types: tuple[int, ...] = ()
    enabled = False
    reason: str | None = None
    hard_mode = False
    timed_conditions = False
    # Menu entry scripts gate the mode on this quest's clear flag; 77A690/8A5620 do not check it themselves.
    gate_quest: int | None = None
    entrust_parity: frozenset[str] = frozenset()
    # Event mission reward families this mode's settlement pays; starts and entrust refuse the others with 501.
    event_reward_families: frozenset[str] = frozenset()

    def require_enabled(self) -> None:
        if not self.enabled:
            raise ProtocolError("quest_mode_unsupported", f"Quest mode is not implemented: {self.reason}", 501)

    def validate_row(self, row: Mapping, catalog) -> None:
        return None

    def check_start(self, context: StartContext) -> MissionWindow:
        self.require_enabled()
        if self.gate_quest is not None and not quest_cleared(context.records, self.gate_quest):
            raise ProtocolError("quest_locked", "The mode entry requires its gate quest to be cleared", 409)
        windows = context.catalog.mission_windows(context.mode.quest_type)
        mission = windows.require_playable(context.quest.quest_id, now=context.now, rank=context.rank,
                                           records=context.records, platform_code=context.platform_code,
                                           selector=context.selector)
        if not self.day_open(context.catalog, context.quest.quest_id, context.now):
            raise ProtocolError("quest_closed", "The quest is not open on the current JST day", 409)
        if not special_condition_satisfied(context.records, context.quest.quest_id, context.catalog.special_conditions()):
            raise ProtocolError("quest_locked", "The quest requires its related quest results", 409)
        if context.hard:
            self.check_hard(context)
        return mission

    def day_open(self, catalog, quest_id: int, now: datetime) -> bool:
        if not self.timed_conditions:
            return True
        row = catalog.row(quest_id)
        return quest_open_on(row, now) or quest_open_on(row, now - DAY_OPEN_GRACE)

    def entry_open(self, catalog, *, now: datetime, platform_code: int) -> bool:
        # Entrust availability reads this menu gate on its own, so an override must also refuse in check_start.
        return True

    def check_hard(self, context: StartContext) -> None:
        hard = context.mode.hard
        if not self.hard_mode or hard is None:
            raise ProtocolError("quest_hard_unavailable", "This quest has no playable four-star challenge", 409)
        record = _record(context.records, context.quest.quest_id)
        if _stars(record) < 3:
            raise ProtocolError("quest_hard_locked", "The four-star challenge requires three stars", 409)
        required = sum(bit for code, bit in zip(context.quest.treasures, SLOT_FLAGS) if code)
        if record.get("AchieveFlag", 0) & UINT32_MASK & required != required:
            raise ProtocolError("quest_hard_locked", "The four-star challenge requires every nonempty treasure", 409)
        if context.rank < hard.rank:
            raise ProtocolError("quest_hard_locked", "Player rank is below the four-star challenge requirement", 409)

    def prepare_start(self, context: StartContext, state: dict) -> tuple[dict, tuple]:
        return state, ()

    def start_boost(self, context: StartContext) -> int:
        return UNBOOSTED

    def score_result(self, context: ScoreContext) -> ScoreResult:
        raise ProtocolError("quest_mode_hook_invalid", "Score modes require a score hook", 503)

    def settle(self, context: SettleContext) -> SettleOutcome:
        return SettleOutcome(context.state)

    def entrust_rewards(self, context: EntrustContext, draw: EntrustDraw) -> EntrustRewards:
        # Entrust reaches DropBonus only through this hook, so an override must call super() first.
        grant = bonus_grant(context.mode.drop_bonus, context.now, draw.items)
        if not grant.increments:
            return EntrustRewards(draw, grant.children)
        items = {item_id: count + grant.increments.get(item_id, 0) for item_id, count in draw.items.items()}
        return EntrustRewards(EntrustDraw(draw.counts, draw.units, items), grant.children)

    def entrust_settle(self, context: EntrustContext) -> SettleOutcome:
        return SettleOutcome(context.state)

    def entrust_blocker(self, hooks: tuple[str, ...] = ENTRUST_PARITY_HOOKS) -> str | None:
        undeclared = [name for name in _overridden_battle_hooks(self, hooks) if name not in self.entrust_parity]
        if not undeclared:
            return None
        return f"Mode {self.name} overrides {', '.join(undeclared)} without a declared entrust counterpart"

    def claims(self, spec) -> bool:
        return spec is not None and spec.quest_types is not None and set(spec.quest_types) <= set(self.quest_types)

    def mission_matcher(self, definition, payload, spec) -> bool | None:
        # A claimed spec carries the derived quest set and team rule; None leaves the decision to MissionProgress.
        return spec.evaluate(payload) if self.claims(spec) else None

    def local_rules(self) -> dict:
        return {}

    def undeliverable_rewards(self, catalog, services: ModeServices) -> tuple[dict, ...] | None:
        # None marks a mode whose starts preflight no rewards; an empty tuple means every preflighted one can be mailed.
        return None

    def status(self) -> dict:
        return {"policy": self.name, "enabled": self.enabled, "reason": self.reason, "hard_mode": self.hard_mode,
                "timed_conditions": self.timed_conditions, "gate_quest": self.gate_quest}


class UnavailablePolicy(ModePolicy):
    name = "unavailable"

    def __init__(self, quest_type: int, reason: str):
        self.quest_types = (quest_type,)
        self.reason = reason


def _overridden_battle_hooks(policy: ModePolicy, hooks: tuple[str, ...] = ENTRUST_PARITY_HOOKS) -> tuple[str, ...]:
    # entrust_parity is a class declaration, so only class-level overrides count; instance attributes do not.
    return tuple(name for name in hooks if getattr(type(policy), name) is not getattr(ModePolicy, name))


def validate_entrust_parity(policy: ModePolicy) -> None:
    declared = policy.entrust_parity
    if type(declared) is not frozenset or not declared <= set(ENTRUST_PARITY_HOOKS):
        raise ProtocolError("quest_mode_registry_invalid",
                            f"Mode {policy.name} must declare entrust parity as a frozenset of battle hook names", 503)
    if not declared <= set(_overridden_battle_hooks(policy)):
        raise ProtocolError("quest_mode_registry_invalid",
                            f"Mode {policy.name} declares entrust parity for battle hooks it does not override", 503)


def validate_event_reward_families(policy: ModePolicy) -> None:
    declared = policy.event_reward_families
    if type(declared) is not frozenset or not declared <= EVENT_REWARD_FAMILIES:
        raise ProtocolError("quest_mode_registry_invalid",
                            f"Mode {policy.name} must declare event reward families as a frozenset of family names", 503)


def admit(policy: ModePolicy, context: StartContext) -> MissionWindow:
    quest = context.quest
    if quest.prerequisite and not quest_cleared(context.records, quest.prerequisite):
        raise ProtocolError("quest_locked", "The prerequisite quest has not been cleared", 409)
    if context.rank < quest.rank:
        raise ProtocolError("quest_rank_required", "Player rank is below the quest requirement", 409)
    return policy.check_start(context)


@functools.cache
def registry() -> Mapping[int, ModePolicy]:
    policies = {}
    for name in MODE_MODULES:
        policy = getattr(importlib.import_module(f".{name}", __package__), "POLICY", None)
        if not isinstance(policy, ModePolicy) or not policy.quest_types:
            raise ProtocolError("quest_mode_registry_invalid", f"Mode module {name} must declare a policy", 503)
        validate_entrust_parity(policy)
        validate_event_reward_families(policy)
        for quest_type in policy.quest_types:
            if quest_type in policies or quest_type not in MODE_SPECS or quest_type in UNAVAILABLE:
                raise ProtocolError("quest_mode_registry_invalid", f"Quest type {quest_type} is registered inconsistently", 503)
            if not policy.enabled and not policy.reason:
                raise ProtocolError("quest_mode_registry_invalid", f"Disabled mode {name} requires a reason", 503)
            policies[quest_type] = policy
    for quest_type, reason in UNAVAILABLE.items():
        policies[quest_type] = UnavailablePolicy(quest_type, reason)
    if set(policies) != set(MODE_SPECS):
        raise ProtocolError("quest_mode_registry_invalid", "Every known quest type requires a policy", 503)
    return MappingProxyType(dict(sorted(policies.items())))


def policy(quest_type: int) -> ModePolicy:
    if not int32(quest_type) or quest_type < 0:
        raise ProtocolError("quest_type_invalid", "Quest type must be a nonnegative native integer", 503)
    return registry().get(quest_type) or UnavailablePolicy(quest_type, "Unregistered quest type")


def support_status(counts: Mapping[int, int] | None = None) -> dict:
    result = {}
    for quest_type, entry in registry().items():
        spec = MODE_SPECS[quest_type]
        result[quest_type] = {**entry.status(), "mission_category": spec.mission_category,
                              "mission_resources": list(spec.resources), "score_mode": spec.score_mode,
                              "gold_counted": spec.gold_counted, "hard_rank": spec.hard_rank, "entrust": spec.entrust,
                              "local_rules": entry.local_rules()}
        if counts is not None:
            result[quest_type]["quests"] = counts.get(quest_type, 0)
    return result
