from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from enum import StrEnum
import hashlib
import json
import math
from pathlib import Path
import threading
from types import MappingProxyType
from typing import NamedTuple

from .errors import ProtocolError
from .event_mission_rewards import SCORE_CONDITIONS, EventMissionCatalog, IncentiveRow
from .local_io import deserialize, read_bounded
from .mission_catalog import _date
from .mission_tags import STATE_NAMES, mission_tables
from .mission_window_revival import COOPERATIVE_REGISTRATIONS, Window
from .native_numbers import INT32_MAX, INT32_MIN, float32, int32
from .quest_catalog import integer
from .quest_entries import UNCONDITIONAL, UnsupportedAppearance, expected_enemies, row_interval
from .quest_modes import ENDED, MAX_BOOST, MODE_PROGRESS, MODE_SPECS, UNBOOSTED, MissionWindow
from .quest_protocol import MAX_KILLS


SCHEMA_VERSION = 1
SUBJUGATION_TYPE = 6
CONFIG_TABLE, QUEST_LIST_TABLE = mission_tables(MODE_SPECS[SUBJUGATION_TYPE].mission_category)
EMC_TABLE, REWARD_TABLE, TEXT_TABLE = "EmcMissionConfig.atb", "RewardConfig.atb", "SystemText.atb"
SOURCE_TABLES = (CONFIG_TABLE, QUEST_LIST_TABLE, EMC_TABLE, REWARD_TABLE, TEXT_TABLE)
ENTRY_TABLE, ENEMY_TABLE = "Entry{:02d}.atb", "Enemy.atb"
MAX_RULE_BYTES = 16 * 1024
# QuestService keeps each mode's mode_progress entry under the policy name.
PROGRESS_NAME = "subjugation"
PROGRESS_FIELDS = frozenset(("best", "total"))
# 8CEC40 and 8CECC0 read the best run from the low 12 bits of MaxRank; 8CEAF0 and 8CEBC0 read the total above them.
BEST_BITS = 12
BEST_LIMIT = (1 << BEST_BITS) - 1
MAX_TOTAL = INT32_MAX >> BEST_BITS
# 8D8AE0 caps SubjugationType 1 at 99999 and every other type at TotalScoreLimit.
COOPERATIVE_TYPE, COOPERATIVE_LIMIT = 1, 99999
STANDARD_TYPE = 0
# 8D94D0 raises bonus factors at or below 1.0 to 1.0.
MINIMUM_FACTOR = 1.0
# 460B20 compares type 6 with the mission best; 460FD0 compares types 4 and 7 with the mission total.
BEST_REACHED, TOTAL_REACHED, TOTAL_EVERY = 6, 4, 7
TIER_SOURCES = MappingProxyType({BEST_REACHED: "best", TOTAL_REACHED: "total", TOTAL_EVERY: "total"})
if frozenset(TIER_SOURCES) != SCORE_CONDITIONS:
    raise AssertionError("Subjugation tier sources must be the score conditions of the shared event mission catalog")
# 6334F0 admits ValidCondition 1 once either score reaches Param_Valid1 and 3 while the earlier one is in [Valid1, Valid2).
ANY_SCORE, ABOVE_LOW, BELOW_HIGH = 0, 1, 3
VALID_CONDITIONS = frozenset((ANY_SCORE, ABOVE_LOW, BELOW_HIGH))
# 6359A0 offers only tiers whose Type_MainIncentive is 5, the present incentive.
PRESENT_INCENTIVE = 5
# 633810 keeps Param_Incentive as the present content, except that a named item counts at least one.
UNIT_PRESENT, GOLD_PRESENT, NAMED_ITEM_PRESENT = 1, 2, 22
LEGACY_PRESENTS = frozenset((7, 14))
SUB_INCENTIVES = frozenset((UNIT_PRESENT, GOLD_PRESENT, NAMED_ITEM_PRESENT, *LEGACY_PRESENTS))
MINIMUM_NAMED_ITEMS = 1
# 633810 shows a repeating tier's crossing count unless it names an item, and 630BA0 reads that count as a unit's card.
COUNTED_REPEATS, COUNTED_REPEAT_AMOUNT = frozenset((GOLD_PRESENT, *LEGACY_PRESENTS)), 1
# SystemText 154 is the resource's score quest reward label.
DESCRIPTION_TEXT = 154
TIER_COLUMNS = MappingProxyType({
    "incentive": "ID_Incentive", "condition": "Type_Condition", "threshold": "Param_Condition",
    "valid_condition": "Param_ValidCondition", "valid_low": "Param_Valid1", "valid_high": "Param_Valid2",
    "main_incentive": "Type_MainIncentive", "sub_incentive": "Type_SubIncentive", "amount": "Param_Incentive",
    "item_id": "ID_Item",
})
RESOURCE_SOURCE, OVERRIDE_SOURCE = "resource", "override"
RECORDS_ABOVE, RECORDS_BELOW = "records_above_watermark", "records_below_watermark"
REACHED, UNREACHED = "reached", "unreached"
NEXT_FIELDS = ("incentive", "condition", "next_at", "remaining")
RESOURCE_INVALID = "quest_mode_resource_invalid"
REWARD_INVALID = "quest_subjugation_reward_invalid"
STATE_INVALID = "quest_state_invalid"
COOPERATIVE_REASON = "Cooperative subjugation scores depend on the server-wide 143 defeat count"
SCORE_POLICY = ("ceil(float32(float32(k) x factor)) where the factor is ClearBonusFactor below the planned enemy count "
                "and CompleteBonusFactor from it, each at least 1.0; k is the reported kills, clamped to the planned "
                "count under the clamp policy; MaxRank packs min(total, limit) << 12 | min(best, 4095)")
REWARD_POLICY = ("tiers come from the shared event mission catalog: the ladder group that the first EmcMissionConfig "
                 "entry by ID_Mission covering the quest claims; types 4 and 6 pay once when crossed and type 7 pays "
                 "per crossed multiple after its valid condition, as named items or single gold and legacy presents "
                 "only; settlements cross from the higher of the watermark and the records before the run, so no tier "
                 "below either is mailed, and a watermark that disagrees with the records is reported without blocking")


class KillsPolicy(StrEnum):
    CLAMP = "clamp"
    RAW = "raw"


class BoostPolicy(StrEnum):
    TOTAL_ONLY = "total_only"
    TOTAL_AND_BEST = "total_and_best"
    IGNORE = "ignore"


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _choice[T: StrEnum](kind: type[T], value, label: str) -> T:
    if not isinstance(value, str) or value not in {member.value for member in kind}:
        raise ValueError(f"{label} must be one of {', '.join(kind)}")
    return kind(value)


@dataclass(frozen=True)
class SubjugationRules:
    kills_policy: KillsPolicy = KillsPolicy.CLAMP
    boost_policy: BoostPolicy = BoostPolicy.TOTAL_ONLY

    def __post_init__(self):
        object.__setattr__(self, "kills_policy", _choice(KillsPolicy, self.kills_policy, "Subjugation kills_policy"))
        object.__setattr__(self, "boost_policy", _choice(BoostPolicy, self.boost_policy, "Subjugation boost_policy"))

    def to_dict(self) -> dict:
        return {"kills_policy": self.kills_policy.value, "boost_policy": self.boost_policy.value}

    @property
    def content_version(self) -> str:
        return _digest({"schema": SCHEMA_VERSION, **self.to_dict()})


DEFAULT_RULES = SubjugationRules()
RULE_FIELDS = frozenset(field.name for field in fields(SubjugationRules))


def load_subjugation_rules(path) -> SubjugationRules:
    if path is None:
        return DEFAULT_RULES
    values = deserialize(read_bounded(Path(path), MAX_RULE_BYTES).decode("utf-8-sig"))
    if not isinstance(values, dict) or set(values) - RULE_FIELDS:
        raise ValueError("Subjugation rules accept only kills_policy and boost_policy")
    return SubjugationRules(**values)


class ScorePair(NamedTuple):
    best: int
    total: int


ZERO_SCORE = ScorePair(0, 0)


def _bounded(value, limit: int) -> bool:
    return type(value) is int and 0 <= value <= limit


def _pair(value) -> ScorePair:
    if not isinstance(value, tuple) or len(value) != len(ScorePair._fields):
        raise ValueError("Subjugation scores pair a best run with a mission total")
    best, total = value
    if not _bounded(best, BEST_LIMIT) or not _bounded(total, MAX_TOTAL):
        raise ValueError("Subjugation scores must fit the packed MaxRank fields")
    return ScorePair(best, total)


def pack(best: int, total: int) -> int:
    scores = _pair((best, total))
    return (scores.total << BEST_BITS) | scores.best


def unpack(max_rank: int) -> ScorePair:
    if not int32(max_rank) or max_rank < 0:
        raise ValueError("Packed scores require a nonnegative native int32 MaxRank")
    return ScorePair(max_rank & BEST_LIMIT, max_rank >> BEST_BITS)


def _product(kills: int, factor: float) -> float:
    try:
        return float32(float32(kills) * factor)
    except OverflowError as error:
        raise ValueError("Subjugation scores must fit a native float32") from error


def _effective_factor(value) -> float:
    if type(value) is not float or not math.isfinite(value) or value < MINIMUM_FACTOR or float32(value) != value:
        raise ValueError("Subjugation bonus factors must be finite float32 values of at least 1.0")
    if math.ceil(_product(MAX_KILLS, value)) > INT32_MAX:
        raise ValueError("Subjugation bonus factors must keep every reported score within int32")
    return value


def _resource_factor(value) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise ValueError("Subjugation bonus factors must be finite float32 cells")
    return _effective_factor(max(value, MINIMUM_FACTOR))


@dataclass(frozen=True)
class SubjugationQuest:
    quest_id: int
    mission_id: int
    subjugation_type: int
    limit: int
    factor_clear: float
    factor_complete: float
    expected: int
    start_button: str
    per_run_cap: int
    first_quest: int
    entry: str

    def __post_init__(self):
        if not int32(self.expected) or self.expected < 0 or not _bounded(self.limit, MAX_TOTAL):
            raise ValueError("Subjugation quests require a native planned enemy count and score limit")
        _effective_factor(self.factor_clear)
        _effective_factor(self.factor_complete)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SubjugationScore:
    score: int
    kills_scored: int
    factor: float
    kills_over_expected: int


@dataclass(frozen=True)
class SubjugationMission:
    mission_id: int
    subjugation_type: int
    total_score_limit: int
    limit: int
    members: tuple[int, ...]
    quests: tuple[int, ...]
    first_quest: int
    window: MissionWindow
    window_source: str

    @property
    def supported(self) -> bool:
        return self.subjugation_type == STANDARD_TYPE


class RewardRange(NamedTuple):
    emc_id: int
    first_quest: int
    last_quest: int
    group: int


@dataclass(frozen=True)
class RewardTier:
    group: int
    incentive: int
    condition: int
    threshold: int
    valid_condition: int
    valid_low: int
    valid_high: int
    main_incentive: int
    sub_incentive: int
    amount: int
    item_id: str

    @classmethod
    def from_row(cls, group: int, row: Mapping) -> RewardTier:
        try:
            values = {name: row[column] for name, column in TIER_COLUMNS.items()}
        except (KeyError, TypeError) as error:
            raise ProtocolError(REWARD_INVALID, f"{REWARD_TABLE} group {group} lacks a native reward column", 503) from error
        return cls(group, **values)._verified()

    @classmethod
    def from_incentive(cls, group: int, row: IncentiveRow) -> RewardTier:
        if not isinstance(row, IncentiveRow):
            raise ProtocolError(REWARD_INVALID, f"{REWARD_TABLE} group {group} holds a foreign incentive row", 503)
        return cls(group, **asdict(row))._verified()

    def _verified(self) -> RewardTier:
        values = asdict(self)
        if any(not int32(value) for name, value in values.items() if name != "item_id") or type(self.item_id) is not str:
            raise ProtocolError(REWARD_INVALID, f"{REWARD_TABLE} group {self.group} holds a non-native reward cell", 503)
        label = f"{REWARD_TABLE} group {self.group} incentive {self.incentive}"
        for field, value, accepted in (("Type_Condition", self.condition, TIER_SOURCES),
                                       ("Param_ValidCondition", self.valid_condition, VALID_CONDITIONS),
                                       ("Type_MainIncentive", self.main_incentive, (PRESENT_INCENTIVE,)),
                                       ("Type_SubIncentive", self.sub_incentive, SUB_INCENTIVES)):
            if value not in accepted:
                raise ProtocolError(REWARD_INVALID, f"{label} has unsupported {field} {value}", 503)
        # 633990 divides both type 7 scores by Param_Condition.
        if self.condition == TOTAL_EVERY and self.threshold < 1:
            raise ProtocolError(REWARD_INVALID, f"{label} repeats every {self.threshold} points", 503)
        counted = self.sub_incentive in COUNTED_REPEATS and self.amount == COUNTED_REPEAT_AMOUNT
        if self.condition == TOTAL_EVERY and self.sub_incentive != NAMED_ITEM_PRESENT and not counted:
            raise ProtocolError(REWARD_INVALID, f"{label} repeats Type_SubIncentive {self.sub_incentive} with "
                                f"Param_Incentive {self.amount}, which the result page counts differently", 503)
        return self

    @property
    def source(self) -> str:
        return TIER_SOURCES[self.condition]

    def to_dict(self) -> dict:
        return {**asdict(self), "source": self.source}


def crossings(tier: RewardTier, before, after) -> int:
    if not isinstance(tier, RewardTier):
        raise ValueError("Crossings require a verified reward tier")
    old, new = (getattr(_pair(value), tier.source) for value in (before, after))
    if tier.condition != TOTAL_EVERY:
        return int(old < tier.threshold <= new)
    if tier.valid_condition == ABOVE_LOW:
        if tier.valid_low > old and tier.valid_low > new:
            return 0
        old, new = max(0, old - tier.valid_low), max(0, new - tier.valid_low)
    elif tier.valid_condition == BELOW_HIGH:
        if not tier.valid_low <= old < tier.valid_high:
            return 0
        new = min(new, tier.valid_high)
    return max(0, new // tier.threshold - old // tier.threshold)


def next_crossing(tier: RewardTier, value: int, cap: int) -> int | None:
    if not isinstance(tier, RewardTier) or not _bounded(value, MAX_TOTAL) or not _bounded(cap, MAX_TOTAL):
        raise ValueError("Next crossings require a verified tier and native scores")
    threshold = tier.threshold
    if tier.condition != TOTAL_EVERY:
        target = threshold if value < threshold else None
    elif tier.valid_condition == ABOVE_LOW:
        target = tier.valid_low + (max(0, value - tier.valid_low) // threshold + 1) * threshold
    elif tier.valid_condition == BELOW_HIGH and not tier.valid_low <= value < tier.valid_high:
        target = None
    else:
        target = (value // threshold + 1) * threshold
        if tier.valid_condition == BELOW_HIGH and target > tier.valid_high:
            target = None
    return target if target is not None and target <= cap else None


def present(tier: RewardTier, times: int, description: str) -> dict:
    if not isinstance(tier, RewardTier) or type(times) is not int or times < 1:
        raise ValueError("Subjugation presents require a reward tier crossed at least once")
    if type(description) is not str or not description:
        raise ValueError("Subjugation presents require their reward label")
    if tier.sub_incentive == UNIT_PRESENT:
        return {"kind": "unit", "template_id": tier.amount, "count": times, "description": description}
    if tier.sub_incentive == GOLD_PRESENT:
        return {"kind": "gold", "amount": tier.amount * times, "description": description}
    if tier.sub_incentive == NAMED_ITEM_PRESENT:
        return {"kind": "item", "amount": max(MINIMUM_NAMED_ITEMS, tier.amount) * times, "item_id": tier.item_id,
                "description": description}
    return {"kind": "legacy_item", "amount": tier.amount * times, "present_type": tier.sub_incentive,
            "description": description}


def _state_invalid(message: str) -> ProtocolError:
    return ProtocolError(STATE_INVALID, message, 503)


def _progress(state: Mapping) -> Mapping | None:
    if not isinstance(state, Mapping):
        raise _state_invalid("Subjugation watermarks live in a player state")
    journal = state.get("quest_state")
    if journal is None:
        return None
    progress = journal.get(MODE_PROGRESS, {}) if isinstance(journal, Mapping) else None
    entries = progress.get(PROGRESS_NAME) if isinstance(progress, Mapping) else None
    if not isinstance(progress, Mapping) or not isinstance(entries, (Mapping, type(None))):
        raise _state_invalid("The battle journal holds no readable subjugation progress")
    return entries


def watermark(state: Mapping, mission_id: int) -> ScorePair | None:
    entries = _progress(state)
    entry = None if entries is None else entries.get(str(mission_id))
    if entry is None:
        return None
    try:
        if not isinstance(entry, Mapping) or set(entry) != PROGRESS_FIELDS:
            raise ValueError("Subjugation watermarks hold only best and total")
        return _pair((entry["best"], entry["total"]))
    except ValueError as error:
        raise _state_invalid(f"The subjugation watermark of mission {mission_id} is not a packed score pair") from error


def scores_view(scores: ScorePair | None) -> dict | None:
    return None if scores is None else scores._asdict()


def with_watermark(state: dict, mission_id: int, scores) -> dict:
    _progress(state)
    if not isinstance(state, dict) or not isinstance(state.get("quest_state"), dict):
        raise _state_invalid("Subjugation watermarks are written into a battle journal")
    if not int32(mission_id) or mission_id <= 0:
        raise ValueError("Subjugation watermarks require a positive native mission identifier")
    entry = _pair(scores)._asdict()
    updated = deepcopy(state)
    progress = updated["quest_state"].setdefault(MODE_PROGRESS, {})
    progress[PROGRESS_NAME] = {**(progress.get(PROGRESS_NAME) or {}), str(mission_id): entry}
    return updated


def higher(first, second) -> ScorePair:
    first, second = _pair(first), _pair(second)
    return ScorePair(max(first.best, second.best), max(first.total, second.total))


def settlement_basis(saved, recorded) -> ScorePair:
    # The records are the client's BeforeSummary and the watermark what was mailed, so neither may mail a tier again.
    return _pair(recorded) if saved is None else higher(saved, recorded)


def watermark_mismatch(saved, recorded) -> dict[str, str]:
    recorded = _pair(recorded)
    if saved is None:
        return {}
    return {name: RECORDS_ABOVE if held < shown else RECORDS_BELOW
            for name, held, shown in zip(ScorePair._fields, _pair(saved), recorded) if held != shown}


def recorded_scores(record: Mapping) -> ScorePair:
    try:
        return unpack(record["MaxRank"])
    except (KeyError, TypeError, ValueError) as error:
        raise _state_invalid("Subjugation quest records require a packed native MaxRank") from error


def _records(records) -> Mapping:
    if not isinstance(records, Mapping):
        raise _state_invalid("Subjugation scores read quest records keyed by QuestID")
    return records


def _state_records(state: Mapping) -> Mapping:
    tables = state.get("tables") if isinstance(state, Mapping) else None
    rows = tables.get("quest_records", ()) if isinstance(tables, Mapping) else None
    if not isinstance(rows, (list, tuple)) or any(not isinstance(row, Mapping) for row in rows):
        raise _state_invalid("Player state holds no readable quest records")
    records = {row.get("QuestID"): row for row in rows}
    if len(records) != len(rows):
        raise _state_invalid("Quest records contain duplicate identifiers")
    return records


def _failure(error: ProtocolError) -> dict:
    return {"code": error.code, "message": str(error)}


def _conditional(label: str) -> ProtocolError:
    return ProtocolError("quest_definition_invalid", f"Score quests need enemies that appear at every MaxRank: {label}", 503)


def _entry_invalid(label: str) -> ProtocolError:
    return ProtocolError("quest_entry_invalid", f"Map entry does not satisfy the verified contract: {label}", 503)


def _text(value) -> str:
    if type(value) is not str:
        raise ValueError("Subjugation quest lists require native text cells")
    return value


def _aware(now) -> datetime:
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ProtocolError("quest_clock_invalid", "Subjugation states require an aware datetime", 503)
    return now


@dataclass(frozen=True)
class _Tables:
    configs: Mapping[int, Mapping]
    entries: Mapping[tuple[int, int], tuple[Mapping, ...]]
    scope: tuple[MissionWindow, ...]
    sources: Mapping[str, str]


def _config_invalid(mission_id: int) -> ProtocolError:
    return ProtocolError(RESOURCE_INVALID, f"{CONFIG_TABLE} mission {mission_id} differs from the native contract", 503)


def _config(tables: _Tables, window: MissionWindow) -> Mapping:
    row = tables.configs.get(window.mission_id)
    if row is None:
        raise ValueError(f"{CONFIG_TABLE} lacks the mission")
    return row


def _kind(row: Mapping) -> int:
    return integer(row["SubjugationType"], minimum=INT32_MIN)


def _window_source(row: Mapping, window: MissionWindow) -> str:
    original = (_date(row["StartDateTime"]), _date(row["EndDateTime"]))
    return RESOURCE_SOURCE if original == (window.start, window.end) else OVERRIDE_SOURCE


class SubjugationCatalog:
    def __init__(self, quests, rules: SubjugationRules = DEFAULT_RULES, *, events: EventMissionCatalog | None = None):
        if not isinstance(rules, SubjugationRules):
            raise ValueError("Subjugation catalogs require verified subjugation rules")
        if events is None:
            events = EventMissionCatalog(quests)
        elif not isinstance(events, EventMissionCatalog) or events.quests is not quests:
            raise ValueError("Subjugation catalogs share the event mission catalog of their own quest catalog")
        self.quests = quests
        self.rules = rules
        self.events = events
        self._lock = threading.RLock()
        self._tables: _Tables | None = None
        self._mission_cache: dict[int, SubjugationMission] = {}
        self._quest_cache: dict[int, SubjugationQuest] = {}
        self._ladder_cache: dict[int, tuple[RewardTier, ...]] = {}
        self._content_version: str | None = None

    def _loaded(self) -> _Tables:
        with self._lock:
            if self._tables is None:
                self._tables = self._load()
            return self._tables

    def _load(self) -> _Tables:
        windows = self.quests.mission_windows(SUBJUGATION_TYPE)
        try:
            configs = {}
            for row in self.quests.resource_rows(CONFIG_TABLE):
                mission_id = integer(row["MissionID"], minimum=1)
                if mission_id in configs:
                    raise ValueError(f"{CONFIG_TABLE} repeats mission {mission_id}")
                configs[mission_id] = row
            entries = {}
            for row in self.quests.resource_rows(QUEST_LIST_TABLE):
                key = (integer(row["MissionID"], minimum=1), integer(row["QuestID"], minimum=1))
                entries.setdefault(key, []).append(row)
        except ProtocolError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise ProtocolError(RESOURCE_INVALID, "Subjugation resources differ from the native contract", 503) from error
        scope = tuple(window for window in windows.ordered() if window.listed and any(
            self.quests.quest_type(quest_id) == SUBJUGATION_TYPE for quest_id in windows.members(window.mission_id)))
        sources = MappingProxyType({name: self.quests.resource_sha256(name) for name in SOURCE_TABLES})
        return _Tables(MappingProxyType(configs), MappingProxyType({key: tuple(rows) for key, rows in entries.items()}),
                       scope, sources)

    @property
    def mission_ids(self) -> tuple[int, ...]:
        return tuple(window.mission_id for window in self._loaded().scope)

    def mission(self, mission_id: int) -> SubjugationMission:
        with self._lock:
            if type(mission_id) is int and mission_id in self._mission_cache:
                return self._mission_cache[mission_id]
            tables = self._loaded()
            window = next((window for window in tables.scope
                           if type(mission_id) is int and window.mission_id == mission_id), None)
            if window is None:
                raise ProtocolError("quest_mission_unknown", "Mission is not an enabled subjugation mission with quests", 409)
            mission = self._mission(tables, window)
            self._mission_cache[mission_id] = mission
            return mission

    def _mission(self, tables: _Tables, window: MissionWindow) -> SubjugationMission:
        try:
            row = _config(tables, window)
            kind = _kind(row)
            declared = integer(row["TotalScoreLimit"], minimum=INT32_MIN)
            limit = COOPERATIVE_LIMIT if kind == COOPERATIVE_TYPE else declared
            if not 0 <= limit <= MAX_TOTAL:
                raise ValueError("TotalScoreLimit must fit the packed MaxRank total")
            source = _window_source(row, window)
        except (KeyError, ValueError) as error:
            raise _config_invalid(window.mission_id) from error
        members = self.quests.mission_windows(SUBJUGATION_TYPE).members(window.mission_id)
        quests = tuple(quest_id for quest_id in members if self.quests.quest_type(quest_id) == SUBJUGATION_TYPE)
        # 8CEAF0 and 8CEC40 read the smallest member QuestID when called with 0.
        return SubjugationMission(window.mission_id, kind, declared, limit, members, quests, min(members), window, source)

    def quest(self, quest_id: int) -> SubjugationQuest:
        with self._lock:
            if type(quest_id) is int and quest_id in self._quest_cache:
                return self._quest_cache[quest_id]
            if type(quest_id) is not int or self.quests.quest_type(quest_id) != SUBJUGATION_TYPE:
                raise ProtocolError("quest_not_found", "Quest is not a subjugation quest", 404)
            quest = self._quest(quest_id)
            self._quest_cache[quest_id] = quest
            return quest

    def _quest(self, quest_id: int) -> SubjugationQuest:
        row = self.quests.row(quest_id)
        try:
            map_no, entry_no = integer(row.get("MapNo"), minimum=1), integer(row.get("EntryNo"))
        except ValueError as error:
            raise ProtocolError("quest_definition_invalid", "Subjugation quests require a native map and entry", 503) from error
        archive, owner = self.quests.mission_windows(SUBJUGATION_TYPE).archive(quest_id, map_no)
        if owner is None:
            raise ProtocolError("quest_mission_ambiguous", "Subjugation quests must belong to one enabled mission", 503)
        mission = self.mission(owner)
        if not mission.supported:
            raise ProtocolError("quest_mode_unsupported", f"{COOPERATIVE_REASON} (SubjugationType "
                                                          f"{mission.subjugation_type})", 501)
        listed = self._loaded().entries.get((owner, quest_id), ())
        if len(listed) != 1:
            raise ProtocolError(RESOURCE_INVALID, f"{QUEST_LIST_TABLE} must list quest {quest_id} once under mission "
                                                  f"{owner}", 503)
        try:
            clear = _resource_factor(listed[0]["ClearBonusFactor"])
            complete = _resource_factor(listed[0]["CompleteBonusFactor"])
            button = _text(listed[0]["StartButtonID"])
        except (KeyError, ValueError) as error:
            raise ProtocolError(RESOURCE_INVALID, f"{QUEST_LIST_TABLE} row of quest {quest_id} differs from the native "
                                                  "contract", 503) from error
        entry = ENTRY_TABLE.format(entry_no)
        expected = self._expected(archive, entry)
        return SubjugationQuest(quest_id, owner, mission.subjugation_type, mission.limit, clear, complete, expected,
                                button, self._cap(expected, clear, complete), mission.first_quest, f"{archive}/{entry}")

    def _expected(self, archive: str, entry: str) -> int:
        label = f"{archive}/{entry}"
        entries = self.quests.archive_rows(archive, entry)
        enemies = self.quests.archive_rows(archive, ENEMY_TABLE)
        try:
            intervals = tuple(row_interval(row) for row in entries)
        except UnsupportedAppearance as error:
            raise _conditional(label) from error
        except ValueError as error:
            raise _entry_invalid(label) from error
        # Planned counts then stay the same for every MaxRank, packed or not.
        if any(interval not in (None, UNCONDITIONAL) for interval in intervals):
            raise _conditional(label)
        try:
            return expected_enemies(entries, enemies, 0)
        except ValueError as error:
            raise _entry_invalid(label) from error

    def _scored(self, expected: int, clear: float, complete: float, kills: int) -> SubjugationScore:
        scored = min(kills, expected) if self.rules.kills_policy is KillsPolicy.CLAMP else kills
        # 45A7D0 picks the factor from the reported kills, whatever the kills policy scores.
        factor = clear if kills < expected else complete
        return SubjugationScore(math.ceil(_product(scored, factor)), scored, factor, max(0, kills - expected))

    def _cap(self, expected: int, clear: float, complete: float) -> int:
        # Scores never fall as kills rise under one factor, so the last kills under each factor bound every run.
        highest = expected if self.rules.kills_policy is KillsPolicy.CLAMP else MAX_KILLS
        candidates = {min(highest, MAX_KILLS), *((min(expected - 1, MAX_KILLS),) if expected else ())}
        return max(self._scored(expected, clear, complete, kills).score for kills in candidates)

    @staticmethod
    def _require_quest(quest) -> SubjugationQuest:
        if not isinstance(quest, SubjugationQuest):
            raise ValueError("Subjugation scores require a subjugation quest")
        return quest

    def score(self, quest: SubjugationQuest, kills: int) -> SubjugationScore:
        quest = self._require_quest(quest)
        if type(kills) is not int or not 0 <= kills <= MAX_KILLS:
            raise ValueError("Subjugation kills must be a reported native kill count")
        return self._scored(quest.expected, quest.factor_clear, quest.factor_complete, kills)

    def per_run_cap(self, quest: SubjugationQuest) -> int:
        quest = self._require_quest(quest)
        return self._cap(quest.expected, quest.factor_clear, quest.factor_complete)

    def accumulate(self, best: int, total: int, score: int, boost: int, limit: int) -> ScorePair:
        current = _pair((best, total))
        if type(score) is not int or score < 0:
            raise ValueError("Subjugation run scores must be nonnegative integers")
        if type(boost) is not int or not UNBOOSTED <= boost <= MAX_BOOST:
            raise ValueError(f"Subjugation boosts range from {UNBOOSTED} to {MAX_BOOST}")
        if not _bounded(limit, MAX_TOTAL):
            raise ValueError("Subjugation limits must fit the packed MaxRank total")
        policy = self.rules.boost_policy
        gained = score * (UNBOOSTED if policy is BoostPolicy.IGNORE else boost)
        best_gain = gained if policy is BoostPolicy.TOTAL_AND_BEST else score
        return ScorePair(min(max(current.best, best_gain), BEST_LIMIT), min(current.total + gained, limit))

    def _member_scores(self, records, mission_id: int) -> tuple[ScorePair, ...]:
        records = _records(records)
        return tuple(recorded_scores(records[quest_id]) for quest_id in self.mission(mission_id).members
                     if quest_id in records)

    def mission_best(self, records: Mapping, mission_id: int) -> int:
        # 8CECC0 keeps the highest low 12 bits among the member records.
        return max((scores.best for scores in self._member_scores(records, mission_id)), default=0)

    def mission_total(self, records: Mapping, mission_id: int) -> int:
        # 8CEBC0 adds the member totals and stops at the 8D8AE0 limit.
        total = sum(scores.total for scores in self._member_scores(records, mission_id))
        return min(total, self.mission(mission_id).limit)

    def mission_score(self, records: Mapping, mission_id: int) -> ScorePair:
        return ScorePair(self.mission_best(records, mission_id), self.mission_total(records, mission_id))

    def first_quest_record(self, records: Mapping, mission_id: int) -> dict:
        first = self.mission(mission_id).first_quest
        record = _records(records).get(first)
        # 8CEAF0 and 8CEC40 answer 0 while the first quest has no record.
        scores = ZERO_SCORE if record is None else recorded_scores(record)
        return {"quest_id": first, "recorded": record is not None, "best": scores.best, "total": scores.total}

    def reward_range(self, quest_id: int) -> RewardRange | None:
        entry = self.events.entry(quest_id)
        if entry is None:
            return None
        group = self.events.ladder_group(entry)
        return RewardRange(entry.emc_id, entry.first_quest, entry.last_quest, 0 if group is None else group.group)

    def reward_group(self, mission_id: int) -> int | None:
        groups = {entry.group if entry is not None and entry.group else None
                  for entry in (self.reward_range(quest_id) for quest_id in self.mission(mission_id).quests)}
        if len(groups) != 1:
            raise ProtocolError(REWARD_INVALID, f"Mission {mission_id} members name different reward groups", 503)
        return groups.pop()

    def ladder(self, mission_id: int) -> tuple[RewardTier, ...]:
        with self._lock:
            if type(mission_id) is int and mission_id in self._ladder_cache:
                return self._ladder_cache[mission_id]
            group = self.reward_group(mission_id)
            rows = () if group is None else self.events.ladder(self.events.entry(self.mission(mission_id).quests[0]))
            tiers = tuple(RewardTier.from_incentive(group, row) for row in rows)
            self._ladder_cache[mission_id] = tiers
            return tiers

    def description(self) -> str:
        text = self.quests.system_text(DESCRIPTION_TEXT)
        if not text:
            raise ProtocolError(REWARD_INVALID, f"SystemText {DESCRIPTION_TEXT} must label subjugation reward presents", 503)
        return text

    @property
    def content_version(self) -> str:
        with self._lock:
            if self._content_version is None:
                tables = self._loaded()
                self._content_version = _digest({
                    "schema": SCHEMA_VERSION, "resource_version": self.quests.resource_version,
                    "master": self.quests.snapshot.identifier, "sources": dict(tables.sources),
                    "events": self.events.content_version,
                    "windows": {str(window.mission_id): {"enable": window.enable, **_window(window)}
                                for window in tables.scope},
                    "rules": self.rules.to_dict(), "score": SCORE_POLICY, "rewards": REWARD_POLICY})
            return self._content_version

    def _ladder_summary(self, mission: SubjugationMission) -> dict:
        tiers = self.ladder(mission.mission_id)
        ranges = sorted({entry for entry in map(self.reward_range, mission.quests) if entry is not None})
        return {"group": self.reward_group(mission.mission_id), "ranges": [entry._asdict() for entry in ranges],
                "tiers": len(tiers), "conditions": _counts(tier.condition for tier in tiers),
                "sub_incentives": _counts(tier.sub_incentive for tier in tiers)}

    def _mission_status(self, mission_id: int) -> dict:
        try:
            mission = self.mission(mission_id)
        except ProtocolError as error:
            return {"mission_id": mission_id, "ready": False, "error": _failure(error)}
        result = {"mission_id": mission_id, "subjugation_type": mission.subjugation_type, "supported": mission.supported,
                  "total_score_limit": mission.total_score_limit, "limit": mission.limit,
                  "window": _window_view(mission.window, mission.window_source),
                  "members": list(mission.members), "first_quest": mission.first_quest}
        if not mission.supported:
            return {**result, "ready": True, "reason": COOPERATIVE_REASON}
        quests, ready = [], True
        for quest_id in mission.quests:
            try:
                quests.append(self.quest(quest_id).to_dict())
            except ProtocolError as error:
                quests.append({"quest_id": quest_id, "error": _failure(error)})
                ready = False
        try:
            rewards = self._ladder_summary(mission)
        except ProtocolError as error:
            rewards, ready = {"error": _failure(error)}, False
        return {**result, "ready": ready, "quests": quests, "rewards": rewards}

    def _cooperative(self, tables: _Tables, now: datetime | None) -> list[dict]:
        entries = []
        for window in self.quests.mission_windows(SUBJUGATION_TYPE).ordered():
            if not window.listed:
                continue
            try:
                row = _config(tables, window)
                kind = _kind(row)
                if kind not in COOPERATIVE_REGISTRATIONS:
                    continue
                source = _window_source(row, window)
            except (KeyError, ValueError):
                entries.append({"mission_id": window.mission_id, "error": _failure(_config_invalid(window.mission_id))})
                continue
            state = None if now is None else window.state(now)
            # Without a clock no window can be ruled out; an ended window never reopens for this catalog.
            if state != ENDED:
                entries.append({"mission_id": window.mission_id, "subjugation_type": kind,
                                "registration": COOPERATIVE_REGISTRATIONS[kind], "window": _window_view(window, source),
                                "state": None if state is None else STATE_NAMES[state]})
        return entries

    def status(self, now: datetime | None = None) -> dict:
        if now is not None:
            _aware(now)
        result = {"schema_version": SCHEMA_VERSION, "rules": self.rules.to_dict(),
                  "rules_version": self.rules.content_version, "score_policy": SCORE_POLICY,
                  "reward_policy": REWARD_POLICY}
        with self._lock:
            try:
                tables, version = self._loaded(), self.content_version
            except ProtocolError as error:
                return {**result, "ready": False, "error": _failure(error)}
            missions = [self._mission_status(window.mission_id) for window in tables.scope]
            try:
                description = {"text": self.description()}
            except ProtocolError as error:
                description = {"error": _failure(error)}
            cooperative = self._cooperative(tables, now)
            ready = (all(entry["ready"] for entry in missions) and "error" not in description
                     and not any("error" in entry for entry in cooperative))
            return {**result, "ready": ready, "content_version": version,
                    "resource_version": self.quests.resource_version, "source_sha256": dict(tables.sources),
                    "description": description, "missions": missions, "cooperative": cooperative}

    def _record_view(self, quest_id: int, record: Mapping | None) -> dict:
        if record is None:
            return {"quest_id": quest_id, "recorded": False, "max_rank": None, "best": 0, "total": 0}
        scores = recorded_scores(record)
        return {"quest_id": quest_id, "recorded": True, "max_rank": record["MaxRank"], **scores._asdict()}

    def _tiers(self, mission: SubjugationMission, basis: ScorePair, recorded: ScorePair) -> tuple[list, dict]:
        views, nearest = [], dict.fromkeys(ScorePair._fields)
        for tier in self.ladder(mission.mission_id):
            times = crossings(tier, ZERO_SCORE, basis)
            cap = BEST_LIMIT if tier.source == "best" else mission.limit
            target = next_crossing(tier, getattr(basis, tier.source), cap)
            # Settlements mail a tier once the records reach it, so a watermark above them leaves a longer way to go.
            remaining = None if target is None else target - getattr(recorded, tier.source)
            view = {"incentive": tier.incentive, "condition": tier.condition, "threshold": tier.threshold,
                    "source": tier.source, "times": times, "state": REACHED if times else UNREACHED,
                    "next_at": target, "remaining": remaining}
            views.append(view)
            held = nearest[tier.source]
            # 611130 asks 6359A0 once per score, and 6359A0 lets a later tier replace an equally close one.
            if target is not None and (held is None or view["remaining"] <= held["remaining"]):
                nearest[tier.source] = view
        return views, {source: None if view is None else {key: view[key] for key in NEXT_FIELDS}
                       for source, view in nearest.items()}

    def _describe(self, mission_id: int, state: Mapping, records: Mapping, now: datetime) -> dict:
        try:
            mission = self.mission(mission_id)
        except ProtocolError as error:
            return {"mission_id": mission_id, "error": _failure(error)}
        scores, saved = self.mission_score(records, mission_id), watermark(state, mission_id)
        basis = settlement_basis(saved, scores)
        result = {"mission_id": mission_id, "supported": mission.supported,
                  "state": STATE_NAMES[mission.window.state(now)], "limit": mission.limit,
                  "records": [self._record_view(quest_id, records.get(quest_id)) for quest_id in mission.members],
                  **scores._asdict(), "watermark": scores_view(saved),
                  "basis": basis._asdict(), "mismatch": watermark_mismatch(saved, scores),
                  "first_quest": self.first_quest_record(records, mission_id)}
        if not mission.supported:
            return {**result, "reason": COOPERATIVE_REASON}
        try:
            tiers, nearest = self._tiers(mission, basis, scores)
        except ProtocolError as error:
            return {**result, "rewards_error": _failure(error)}
        return {**result, "tiers": tiers, "next": nearest}

    def describe(self, state: Mapping, now: datetime) -> dict:
        _aware(now)
        records = _state_records(state)
        with self._lock:
            return {"rules": self.rules.to_dict(), "content_version": self.content_version,
                    "missions": [self._describe(mission_id, state, records, now) for mission_id in self.mission_ids]}


def _window(window: MissionWindow) -> dict:
    return Window(window.start, window.end).to_dict()


def _window_view(window: MissionWindow, source: str) -> dict:
    return {"enable": window.enable, **_window(window), "source": source}


def _counts(values) -> dict[str, int]:
    return {str(key): count for key, count in sorted(Counter(values).items())}


def crossed_rewards(catalog: SubjugationCatalog, mission_id: int, before, after) -> tuple[tuple[RewardTier, dict], ...]:
    crossed = tuple((tier, times) for tier in catalog.ladder(mission_id) if (times := crossings(tier, before, after)))
    if not crossed:
        return ()
    description = catalog.description()
    return tuple((tier, present(tier, times, description)) for tier, times in crossed)


def resync_watermark(catalog: SubjugationCatalog, state: Mapping, mission_id: int) -> tuple[dict, ScorePair]:
    recorded = catalog.mission_score(_state_records(state), mission_id)
    return with_watermark(state, mission_id, recorded), recorded


def redeliver(catalog: SubjugationCatalog, state: Mapping, mission_id: int, presents) -> tuple[dict, list[dict], ScorePair]:
    recorded = catalog.mission_score(_state_records(state), mission_id)
    saved = watermark(state, mission_id)
    # A missing watermark reads as the records, like settlements that never mail the tiers below them.
    basis = recorded if saved is None else saved
    rewards = [reward for _, reward in crossed_rewards(catalog, mission_id, basis, recorded)]
    mailed: list[dict] = []
    if rewards:
        state, mailed = presents.enqueue(state, rewards)
    reached = higher(basis, recorded)
    return with_watermark(state, mission_id, reached), mailed, reached


def reset_progress(catalog: SubjugationCatalog, state: Mapping, mission_id: int) -> tuple[dict, list[int]]:
    members = frozenset(catalog.mission(mission_id).members)
    _state_records(state)
    _progress(state)
    updated = deepcopy(state)
    cleared = []
    for row in updated["tables"]["quest_records"]:
        if row.get("QuestID") in members and row.get("MaxRank"):
            row["MaxRank"] = 0
            cleared.append(row["QuestID"])
    journal = updated.get("quest_state")
    progress = journal.get(MODE_PROGRESS) if isinstance(journal, dict) else None
    entries = progress.get(PROGRESS_NAME) if isinstance(progress, dict) else None
    if isinstance(entries, dict):
        entries.pop(str(mission_id), None)
    return updated, sorted(cleared)
