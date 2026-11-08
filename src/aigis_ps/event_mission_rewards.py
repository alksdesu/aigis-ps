from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from enum import StrEnum
import hashlib
import json
import threading
from types import MappingProxyType

from . import quest_modes
from .errors import ProtocolError
from .native_numbers import int32
from .quest_modes import MISSION_CATEGORIES, SCORE_TYPES, MissionWindow


SCHEMA_VERSION = 1
EMC_TABLE, SAVE_TABLE, REWARD_TABLE = "EmcMissionConfig.atb", "SpecialMissionRewardConfig.atb", "RewardConfig.atb"
SOURCE_TABLES = (EMC_TABLE, SAVE_TABLE, REWARD_TABLE)
RESOURCE_INVALID = "quest_mode_resource_invalid"
UNSUPPORTED = "quest_event_reward_unsupported"
ENTRY_ID, SAVE_KEY, LADDER_KEY = "ID_Mission", "ID_Save", "ID_ResultReward"
HEAD, INCENTIVE, CONDITION, ITEM = "ID_Reward", "ID_Incentive", "Type_Condition", "ID_Item"
ENTRY_COLUMNS = MappingProxyType({"emc_id": ENTRY_ID, "first_quest": "ID_QuestFirst", "last_quest": "ID_QuestLast",
                                  "kind": "Type_Quest", "state": "Type_State"})
LADDER_COLUMNS = MappingProxyType({
    "threshold": "Param_Condition", "valid_condition": "Param_ValidCondition", "valid_low": "Param_Valid1",
    "valid_high": "Param_Valid2", "main_incentive": "Type_MainIncentive", "sub_incentive": "Type_SubIncentive",
    "amount": "Param_Incentive", "item_id": ITEM,
})
# 60FDB0 stores both tables in one record and leaves the valid fields of SpecialMissionRewardConfig rows zero.
SAVE_COLUMNS = MappingProxyType({"threshold": "Param_Condition", "main_incentive": "Type_Incentive",
                                 "sub_incentive": "Type_IncentiveItem", "amount": "Param_Incentive", "item_id": ITEM})
SAVE_HEAD_COLUMNS = MappingProxyType({"days": "Days", "time": "Time"})
NO_HEAD_COLUMNS: Mapping[str, str] = MappingProxyType({})
# 461440 returns unless the started mission is an emergency mission (category 1).
EMERGENCY_CATEGORY = 1
# 460B20 shows condition 6 and 460FD0 conditions 4 and 7, and 45B7C0 fills their scores only for score quests.
SCORE_CONDITIONS = frozenset((4, 6, 7))
# 461440 compares key 5 (EventParam) when the smallest member's entry is a collection, and key 9 otherwise.
COLLECTION_KIND = 2
COLLECTION_KEY, TRIAL_KEY = 5, 9
# 611FE0 reads condition 10 groups through cfgRnkbns, so unnamed ones are rank rewards rather than orphans.
RANK_CONDITION = 10
LADDER_HIDDEN = "result_pages_show_ladders_only_for_score_quests_and_emergency_missions"
SAVES_UNREAD = "no_client_reader_shows_special_mission_rewards"
NOT_PROMISED = MappingProxyType({
    "History": "history_reward_texts_promise_exchange_items_or_drops_not_an_after_event_distribution",
    "Reproduce": "reproduced_missions_keep_the_original_event_distribution_days",
})
RULES = MappingProxyType({
    "entries": "nonzero_new_id_mission_opens_an_entry_and_other_rows_add_their_save_and_reward_keys_to_it",
    "groups": "groups_attach_to_the_first_entry_by_id_mission_naming_them_and_unnamed_groups_are_dropped",
    "heads": "a_nonzero_new_id_reward_opens_a_group_and_repeated_heads_merge_keeping_the_last_days_and_time",
    "incentives": "zero_or_unchanged_id_incentive_keeps_the_current_incentive_and_incentives_list_in_id_order",
    "conditions": "zero_type_condition_keeps_the_last_nonzero_value_across_group_heads",
    "lookup": "first_entry_by_id_mission_covering_the_quest_with_its_first_reward_key_as_the_ladder",
    "score": "score_quests_whose_ladder_holds_conditions_4_6_or_7",
    "emergency": "emergency_missions_whose_quest_ladder_holds_the_key_of_the_smallest_member_entry",
    "after_event": "emergency_missions_with_a_member_entry_holding_special_mission_rewards",
    "gate": "starts_and_entrust_refuse_unsettled_families_with_501_before_charging",
    "not_promised": "history_and_reproduce_special_mission_rewards_and_hidden_ladders_are_reported_never_paid",
    "orphans": "unnamed_groups_never_reach_candidates_previews_or_presents",
})


class Family(StrEnum):
    SCORE = "score"
    EMERGENCY = "emergency"
    AFTER_EVENT = "after_event"


NO_FAMILIES: frozenset[Family] = frozenset()
LADDER_FAMILIES = frozenset((Family.SCORE, Family.EMERGENCY))


def settled_families(quest_type) -> frozenset[Family]:
    if quest_type is None:
        return NO_FAMILIES
    return frozenset(map(Family, quest_modes.policy(quest_type).event_reward_families))


@dataclass(frozen=True)
class IncentiveRow:
    incentive: int
    condition: int
    threshold: int
    main_incentive: int
    sub_incentive: int
    amount: int
    item_id: str
    valid_condition: int = 0
    valid_low: int = 0
    valid_high: int = 0


@dataclass(frozen=True)
class RewardGroup:
    group: int
    emc_id: int
    rows: tuple[IncentiveRow, ...]

    def summary(self) -> dict:
        return {"group": self.group, "emc_id": self.emc_id, "rows": len(self.rows),
                "incentives": len({row.incentive for row in self.rows}),
                "conditions": _counts(row.condition for row in self.rows),
                "main_incentives": _counts(row.main_incentive for row in self.rows)}


@dataclass(frozen=True)
class SaveGroup(RewardGroup):
    # 00610477 writes Days and Time at every head row, so a repeated head keeps the last ones.
    days: int = 0
    time: int = 0

    def summary(self) -> dict:
        return {**super().summary(), "days": self.days, "time": self.time}


@dataclass(frozen=True)
class MissionEntry:
    emc_id: int
    first_quest: int
    last_quest: int
    kind: int
    state: int
    save_keys: tuple[int, ...]
    ladder_keys: tuple[int, ...]

    def covers(self, quest_id: int) -> bool:
        # 60FD30 skips an entry while QuestID < ID_QuestFirst or ID_QuestLast < QuestID.
        return self.first_quest <= quest_id <= self.last_quest

    def to_dict(self) -> dict:
        return {**asdict(self), "save_keys": list(self.save_keys), "ladder_keys": list(self.ladder_keys)}


@dataclass
class _EntryDraft:
    row: Mapping
    saves: set[int] = field(default_factory=set)
    ladders: set[int] = field(default_factory=set)


@dataclass
class _GroupDraft:
    emc_id: int
    incentives: dict[int, list[IncentiveRow]] = field(default_factory=dict)
    head: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _Tables:
    entries: tuple[MissionEntry, ...]
    ladders: Mapping[int, RewardGroup]
    saves: Mapping[int, SaveGroup]
    counts: Mapping[str, Mapping[str, int]]
    unattached: Mapping[str, Mapping[int, frozenset[int]]]
    sources: Mapping[str, str]


@dataclass
class _Reach:
    quests: set[int] = field(default_factory=set)
    categories: dict[str, set[int]] = field(default_factory=dict)
    families: set[Family] = field(default_factory=set)
    refused: list[dict] = field(default_factory=list)

    def merge(self, other: _Reach) -> None:
        self.quests.update(other.quests)
        for category, quests in other.categories.items():
            self.categories.setdefault(category, set()).update(quests)
        self.families.update(other.families)
        self.refused.extend(other.refused)


@dataclass
class _Unpromised:
    reason: str
    entries: list[int] = field(default_factory=list)
    groups: list[SaveGroup] = field(default_factory=list)
    quests: set[int] = field(default_factory=set)

    def add(self, emc_id: int, groups: tuple[SaveGroup, ...], quests: set[int]) -> None:
        self.entries.append(emc_id)
        self.groups.extend(groups)
        self.quests.update(quests)

    def to_dict(self) -> dict:
        days = [group.days for group in self.groups]
        return {"reason": self.reason, "entries": list(self.entries), "groups": len(self.groups),
                "quests": len(self.quests), "days": [min(days), max(days)]}


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _counts(values: Iterable) -> dict[str, int]:
    return {str(key): count for key, count in sorted(Counter(values).items())}


def _names(families: Iterable[Family]) -> list[str]:
    return sorted(family.value for family in families)


def _failure(error: ProtocolError) -> dict:
    return {"code": error.code, "message": str(error)}


def _settled_view() -> dict[str, list[str]]:
    return {str(quest_type): _names(map(Family, entry.event_reward_families))
            for quest_type, entry in sorted(quest_modes.registry().items()) if entry.event_reward_families}


def _cell(row: Mapping, column: str):
    value = row[column]
    if column == ITEM:
        if type(value) is not str:
            raise ValueError(f"{column} must be a native text cell")
    elif not int32(value):
        raise ValueError(f"{column} must be a native integer cell")
    return value


def _load_entries(rows: Iterable[Mapping]) -> tuple[tuple[MissionEntry, ...], dict[str, int]]:
    drafts: dict[int, _EntryDraft] = {}
    current, last = None, 0
    counts = Counter(rows=0, continuations=0, skipped=0)
    for row in rows:
        counts["rows"] += 1
        identifier = _cell(row, ENTRY_ID)
        if identifier and identifier != last:
            last = identifier
            # 612E60 finds or inserts the entry, so a later row naming a known ID_Mission rewrites its columns.
            current = drafts.setdefault(identifier, _EntryDraft(row))
            current.row = row
        elif current is None:
            # 006101F2 skips rows that precede the first entry.
            counts["skipped"] += 1
            continue
        else:
            counts["continuations"] += 1
        for keys, column in ((current.saves, SAVE_KEY), (current.ladders, LADDER_KEY)):
            if value := _cell(row, column):
                keys.add(value)
    entries = tuple(MissionEntry(**{name: _cell(draft.row, column) for name, column in ENTRY_COLUMNS.items()},
                                 save_keys=tuple(sorted(draft.saves)), ladder_keys=tuple(sorted(draft.ladders)))
                    for _, draft in sorted(drafts.items()))
    return entries, dict(counts)


def _owners(entries: tuple[MissionEntry, ...], keys: Callable[[MissionEntry], tuple[int, ...]]) -> dict[int, int]:
    owners = {}
    # 006103EB and 0061073E walk the entries in ID_Mission order and attach a group to the first one naming it.
    for entry in entries:
        for key in keys(entry):
            owners.setdefault(key, entry.emc_id)
    return owners


def _load_groups(rows: Iterable[Mapping], owners: Mapping[int, int], columns: Mapping[str, str],
                 head_columns: Mapping[str, str]) -> tuple[dict[int, _GroupDraft], dict[str, int]]:
    groups: dict[int, _GroupDraft] = {}
    head = incentive_id = condition = 0
    group = incentive = None
    counts = Counter(rows=0, skipped=0)
    for row in rows:
        counts["rows"] += 1
        identifier = _cell(row, HEAD)
        if identifier and identifier != head:
            head, incentive_id, group, incentive = identifier, 0, None, None
            if identifier not in owners:
                counts["skipped"] += 1
                continue
            group = groups.setdefault(identifier, _GroupDraft(owners[identifier]))
            group.head = {name: _cell(row, column) for name, column in head_columns.items()}
        if group is not None:
            value = _cell(row, INCENTIVE)
            # 613050 finds or inserts the incentive, so ID_Incentive 0 or an unchanged one keeps the current incentive.
            if value and value != incentive_id:
                incentive_id = value
                incentive = group.incentives.setdefault(value, [])
        if incentive is None:
            counts["skipped"] += 1
            continue
        # 006104F1 keeps the last nonzero Type_Condition without resetting it at a group head.
        condition = _cell(row, CONDITION) or condition
        incentive.append(IncentiveRow(incentive_id, condition,
                                      **{name: _cell(row, column) for name, column in columns.items()}))
    return groups, dict(counts)


def _finished[T: RewardGroup](drafts: Mapping[int, _GroupDraft], kind: type[T]) -> Mapping[int, T]:
    # Incentives sit in a std::map, so readers meet them in ID_Incentive order.
    return MappingProxyType({identifier: kind(identifier, draft.emc_id, tuple(
        row for key in sorted(draft.incentives) for row in draft.incentives[key]), **draft.head)
        for identifier, draft in sorted(drafts.items())})


def _unattached(rows: Iterable[Mapping], attached: Mapping[int, RewardGroup]) -> Mapping[int, frozenset[int]]:
    conditions, current = {}, None
    for row in rows:
        current = _cell(row, HEAD) or current
        if current is not None and current not in attached:
            conditions.setdefault(current, set()).add(_cell(row, CONDITION))
    return MappingProxyType({group: frozenset(values) for group, values in sorted(conditions.items())})


class EventMissionCatalog:
    def __init__(self, quests):
        self.quests = quests
        self._lock = threading.RLock()
        self._tables: _Tables | None = None
        self._entry_cache: dict[int, MissionEntry | None] = {}
        self._family_cache: dict[tuple[int, int, int], frozenset[Family]] = {}
        self._summary: dict | None = None
        self._content_version: str | None = None

    def _loaded(self) -> _Tables:
        with self._lock:
            if self._tables is None:
                self._tables = self._load()
            return self._tables

    def _load(self) -> _Tables:
        rows = {name: self.quests.resource_rows(name) for name in SOURCE_TABLES}
        try:
            entries, entry_counts = _load_entries(rows[EMC_TABLE])
            ladder_drafts, ladder_counts = _load_groups(
                rows[REWARD_TABLE], _owners(entries, lambda entry: entry.ladder_keys), LADDER_COLUMNS, NO_HEAD_COLUMNS)
            save_drafts, save_counts = _load_groups(
                rows[SAVE_TABLE], _owners(entries, lambda entry: entry.save_keys), SAVE_COLUMNS, SAVE_HEAD_COLUMNS)
            ladders, saves = _finished(ladder_drafts, RewardGroup), _finished(save_drafts, SaveGroup)
            unattached = MappingProxyType({REWARD_TABLE: _unattached(rows[REWARD_TABLE], ladders),
                                           SAVE_TABLE: _unattached(rows[SAVE_TABLE], saves)})
        except (KeyError, TypeError, ValueError) as error:
            raise ProtocolError(RESOURCE_INVALID, "Event mission rewards differ from the native contract", 503) from error
        counts = MappingProxyType({EMC_TABLE: entry_counts, REWARD_TABLE: ladder_counts, SAVE_TABLE: save_counts})
        sources = MappingProxyType({name: self.quests.resource_sha256(name) for name in SOURCE_TABLES})
        return _Tables(entries, ladders, saves, counts, unattached, sources)

    @property
    def entries(self) -> tuple[MissionEntry, ...]:
        return self._loaded().entries

    def entry(self, quest_id: int) -> MissionEntry | None:
        with self._lock:
            if quest_id not in self._entry_cache:
                self._entry_cache[quest_id] = next(
                    (entry for entry in self._loaded().entries if entry.covers(quest_id)), None)
            return self._entry_cache[quest_id]

    def reward_group(self, group: int) -> RewardGroup | None:
        return self._loaded().ladders.get(group)

    def save_group(self, group: int) -> SaveGroup | None:
        return self._loaded().saves.get(group)

    def ladder_group(self, entry: MissionEntry | None) -> RewardGroup | None:
        if entry is None or not entry.ladder_keys:
            return None
        # Readers take the first node of the entry's reward map, which stays empty when another entry claimed it.
        group = self.reward_group(entry.ladder_keys[0])
        return group if group is not None and group.emc_id == entry.emc_id else None

    def ladder(self, entry: MissionEntry | None) -> tuple[IncentiveRow, ...]:
        group = self.ladder_group(entry)
        return () if group is None else group.rows

    def save_groups(self, entry: MissionEntry | None) -> tuple[SaveGroup, ...]:
        if entry is None:
            return ()
        groups = (self.save_group(key) for key in entry.save_keys)
        return tuple(group for group in groups if group is not None and group.emc_id == entry.emc_id and group.rows)

    def decision_key(self, members: Iterable[int]) -> int:
        members = tuple(members)
        # 461440 reads the entry of the smallest member QuestID and compares key 9 without one.
        entry = self.entry(min(members)) if members else None
        return COLLECTION_KEY if entry is not None and entry.kind == COLLECTION_KIND else TRIAL_KEY

    def _members(self, quest_type: int, mission: MissionWindow) -> tuple[int, ...]:
        return self.quests.mission_windows(quest_type).members(mission.mission_id)

    def families(self, quest_id: int, mission: MissionWindow) -> frozenset[Family]:
        if not isinstance(mission, MissionWindow):
            raise ValueError("Event mission families are decided for an admitted mission window")
        quest_type = self.quests.quest_type(quest_id)
        score, emergency = quest_type in SCORE_TYPES, mission.category == EMERGENCY_CATEGORY
        # Only score result pages and emergency missions show these rewards, so other starts never read the tables.
        if not score and not emergency:
            return NO_FAMILIES
        key = (quest_id, mission.mission_id, mission.category)
        with self._lock:
            if key not in self._family_cache:
                self._family_cache[key] = self._families(quest_id, quest_type, mission, score=score,
                                                         emergency=emergency)
            return self._family_cache[key]

    def _families(self, quest_id: int, quest_type: int, mission: MissionWindow, *, score: bool,
                  emergency: bool) -> frozenset[Family]:
        ladder, found = self.ladder(self.entry(quest_id)), set()
        if score and any(row.condition in SCORE_CONDITIONS for row in ladder):
            found.add(Family.SCORE)
        if emergency:
            members = self._members(quest_type, mission)
            decision = self.decision_key(members)
            if any(row.condition == decision for row in ladder):
                found.add(Family.EMERGENCY)
            if any(self.save_groups(self.entry(member)) for member in members):
                found.add(Family.AFTER_EVENT)
        return frozenset(found)

    def unsettled(self, quest_id: int, mission: MissionWindow) -> frozenset[Family]:
        return self.families(quest_id, mission) - settled_families(self.quests.quest_type(quest_id))

    def require_settled(self, quest_id: int, mission: MissionWindow) -> None:
        unsettled = self.unsettled(quest_id, mission)
        if unsettled:
            raise ProtocolError(UNSUPPORTED, f"Quest {quest_id} shows {', '.join(_names(unsettled))} event rewards "
                                             f"in mission {mission.mission_id} that no mode settles", 501)

    @property
    def content_version(self) -> str:
        with self._lock:
            if self._content_version is None:
                self._content_version = _digest({"schema": SCHEMA_VERSION, "sources": dict(self._loaded().sources),
                                                 "rules": dict(RULES), "settled": _settled_view()})
            return self._content_version

    def _reach(self, quest_id: int, quest_type: int, missions: Iterable[MissionWindow]) -> _Reach:
        reach, settled = _Reach({quest_id}), settled_families(quest_type)
        for mission in missions:
            families = self.families(quest_id, mission)
            reach.categories.setdefault(MISSION_CATEGORIES[mission.category], set()).add(quest_id)
            reach.families.update(families)
            if families - settled:
                reach.refused.append({"quest_id": quest_id, "mission_id": mission.mission_id, "listed": mission.listed,
                                      "families": _names(families), "unsettled": _names(families - settled)})
        return reach

    @staticmethod
    def _reasons(ladder: RewardGroup | None, saves: tuple[SaveGroup, ...], reach: _Reach) -> dict:
        reasons = {}
        if ladder is not None and ladder.rows and not reach.families & LADDER_FAMILIES:
            reasons["ladder"] = LADDER_HIDDEN
        if saves and reach.categories and Family.AFTER_EVENT not in reach.families:
            reasons["saves"] = {name: NOT_PROMISED.get(name, SAVES_UNREAD) for name in sorted(reach.categories)}
        return reasons

    def _not_promised(self, reached: Mapping[int, _Reach]) -> dict:
        entries, ladders, saves = {entry.emc_id: entry for entry in self.entries}, [], {}
        for emc_id, reach in sorted(reached.items()):
            entry = entries[emc_id]
            ladder, groups = self.ladder_group(entry), self.save_groups(entry)
            reasons = self._reasons(ladder, groups, reach)
            if "ladder" in reasons:
                ladders.append({"emc_id": emc_id, "group": ladder.group, "first_quest": min(reach.quests),
                                "last_quest": max(reach.quests), "reason": reasons["ladder"]})
            for category, reason in reasons.get("saves", {}).items():
                saves.setdefault(category, _Unpromised(reason)).add(emc_id, groups, reach.categories[category])
        return {"ladders": ladders, "saves": {category: bucket.to_dict() for category, bucket in sorted(saves.items())}}

    def _summarize(self) -> dict:
        tables, quest_ids = self._loaded(), tuple(sorted({*self.quests.all_ids, *self.quests.mode_ids}))
        counts = Counter(dict.fromkeys(_names(Family), 0))
        reached: dict[int, _Reach] = {}
        refused, failures = [], {}
        for quest_id in quest_ids:
            quest_type = self.quests.quest_type(quest_id)
            try:
                reach = self._reach(quest_id, quest_type, self.quests.mission_windows(quest_type).missions(quest_id))
                entry = self.entry(quest_id)
            except ProtocolError as error:
                failures[str(quest_id)] = _failure(error)
                continue
            counts.update(_names(reach.families))
            refused.extend(reach.refused)
            if entry is not None:
                reached.setdefault(entry.emc_id, _Reach()).merge(reach)
        rank = [group for group, conditions in tables.unattached[REWARD_TABLE].items() if RANK_CONDITION in conditions]
        return {"entries": len(tables.entries), "rows": {name: dict(value) for name, value in tables.counts.items()},
                "ladder_groups": {"attached": len(tables.ladders), "rank": len(rank),
                                  "orphaned": len(tables.unattached[REWARD_TABLE]) - len(rank)},
                "save_groups": {"attached": len(tables.saves), "unattached": len(tables.unattached[SAVE_TABLE])},
                "quests": len(quest_ids), "families": dict(counts), "refused": refused,
                "not_promised": self._not_promised(reached), "failures": failures}

    def status(self) -> dict:
        result = {"schema_version": SCHEMA_VERSION, "rules": dict(RULES), "settled": _settled_view(),
                  "official_server_rules": False}
        with self._lock:
            try:
                tables, version = self._loaded(), self.content_version
                if self._summary is None:
                    self._summary = self._summarize()
            except ProtocolError as error:
                return {**result, "ready": False, "error": _failure(error)}
            return {**result, "ready": not self._summary["failures"], "content_version": version,
                    "resource_version": self.quests.resource_version, "source_sha256": dict(tables.sources),
                    **deepcopy(self._summary)}

    def _mission_view(self, quest_id: int, quest_type: int, mission: MissionWindow) -> dict:
        families = self.families(quest_id, mission)
        view = {"mission_id": mission.mission_id, "category": MISSION_CATEGORIES[mission.category],
                "listed": mission.listed, "families": _names(families),
                "unsettled": _names(families - settled_families(quest_type)), "decision_key": None,
                "member_entry": None}
        if mission.category == EMERGENCY_CATEGORY:
            members = self._members(quest_type, mission)
            # 610C50 hands the collection menu the ladder of the smallest member's entry.
            member = self.entry(min(members)) if members else None
            view.update(decision_key=self.decision_key(members), member_entry=None if member is None else member.emc_id)
        return view

    def describe(self, quest_id: int) -> dict:
        quest_type = self.quests.quest_type(quest_id)
        if quest_type is None:
            raise ProtocolError("quest_not_found", "Quest is absent from the fixed master snapshot", 404)
        with self._lock:
            missions = self.quests.mission_windows(quest_type).missions(quest_id)
            entry, reach = self.entry(quest_id), self._reach(quest_id, quest_type, missions)
            ladder, saves = self.ladder_group(entry), self.save_groups(entry)
            return {"quest_id": quest_id, "quest_type": quest_type, "score_mode": quest_type in SCORE_TYPES,
                    "settled": _names(settled_families(quest_type)), "content_version": self.content_version,
                    "entry": None if entry is None else entry.to_dict(),
                    "ladder": None if ladder is None else ladder.summary(),
                    "saves": [group.summary() for group in saves],
                    "missions": [self._mission_view(quest_id, quest_type, mission) for mission in missions],
                    "not_promised": self._reasons(ladder, saves, reach)}
