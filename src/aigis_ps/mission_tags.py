from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
import hashlib
import json
import re
import threading
from types import MappingProxyType
from typing import Literal, get_args

from .energy_clock import JST
from .errors import ProtocolError
from .native_numbers import int32
from .quest_modes import (
    ENDED, MISSION_CATEGORIES, OPEN, QUEST_ID_LIST_CATEGORIES, UPCOMING, MissionWindow, MissionWindows, quest_open_on,
    require_platform, validate_window_overrides,
)


# A77B90 builds DailyReproduce missions from server data and never writes their +0x48 tag.
SERVER_PROVIDED_CATEGORIES = frozenset((4,))
REGISTRY_CATEGORIES = tuple(sorted(set(MISSION_CATEGORIES) - SERVER_PROVIDED_CATEGORIES))
# 8D4DF0, 8D5140, 8D5600 and 8D5A90 create a mission per config row; the other loaders only per MissionQuestList row.
CONFIG_CREATED_CATEGORIES = frozenset((14, 15, 16, 17))
# 8DB230 never reads TagID or TagIDRange, so tower missions keep the zero tag 8CA7A0 gives every mission.
TAGLESS_CATEGORIES = frozenset((12,))
MISSION_CONFIG = "{}MissionConfig.atb"
MISSION_QUEST_LIST = "{}MissionQuestList.atb"
TAG_COLUMN = "TagID"
LookupKind = Literal["ID", "Tag"]
ID_LOOKUP, TAG_LOOKUP = get_args(LookupKind)
MEMO_LIMIT = 4096
LIST_LIMIT = 64
CONTENT_SCHEMA = 1
STATE_NAMES = MappingProxyType({UPCOMING: "upcoming", OPEN: "open", ENDED: "ended"})
CREATION_NAMES = MappingProxyType({True: "config", False: "quest_list"})
RULES = MappingProxyType({
    "registry": "config_categories_without_server_provided",
    "creation": "config_rows_of_config_created_categories_else_quest_list_rows",
    "registration": "created_with_enable_flag_or_platform_bit",
    "tag": "first_registered_mission_by_id_covering_positive_tag", "tag_columns": "unread_by_tagless_categories",
    "span": "int32_wrapping_tag_range", "open": "window_state_without_grace",
    "displayable": "open_with_a_member_open_today_absent_rows_closed", "rank": "minimum_member_rank_open_or_not",
})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def mission_tables(category: int) -> tuple[str, ...]:
    kind = MISSION_CATEGORIES[category]
    config = MISSION_CONFIG.format(kind)
    return (config,) if category in QUEST_ID_LIST_CATEGORIES else (config, MISSION_QUEST_LIST.format(kind))


REGISTRY_TABLES = tuple(name for category in REGISTRY_CATEGORIES for name in mission_tables(category))


def _kind(kind) -> str:
    if not isinstance(kind, str) or kind not in (ID_LOOKUP, TAG_LOOKUP):
        raise ProtocolError("quest_tag_invalid", "Mission lookups use the ID or Tag kind")
    return ID_LOOKUP if kind == ID_LOOKUP else TAG_LOOKUP


def _value(value) -> int:
    if not int32(value):
        raise ProtocolError("quest_tag_invalid", "Mission lookups require a native integer")
    return value


def _clock(now) -> datetime:
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ProtocolError("quest_clock_invalid", "Mission availability requires an aware datetime", 503)
    return now


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(JST).isoformat(timespec="microseconds")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _sources(value) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(REGISTRY_TABLES):
        raise ValueError("Tag resolution requires the digest of every registry table")
    if any(type(digest) is not str or not _SHA256.match(digest) for digest in value.values()):
        raise ValueError("Registry table digests must be lowercase sha256 text")
    return MappingProxyType({name: value[name] for name in REGISTRY_TABLES})


def _labels(labels) -> tuple[str, ...]:
    names = None if isinstance(labels, str) or not isinstance(labels, Iterable) else tuple(labels)
    if names is None or not all(isinstance(name, str) and name for name in names):
        raise ValueError("Tag consumers must name their dependents with text labels")
    return names


def _consumers(consumers) -> tuple[tuple[tuple[str, int], tuple[str, ...]], ...]:
    if not isinstance(consumers, Mapping):
        raise ValueError("Tag consumers must map lookups to their dependents")
    result = []
    for key, labels in consumers.items():
        if type(key) is not tuple or len(key) != 2:
            raise ValueError("Tag consumers are keyed by a lookup kind and value")
        result.append(((_kind(key[0]), _value(key[1])), _labels(labels)))
    return tuple(result)


def _window_values(overrides: Mapping) -> list[list]:
    return [[mission_id, _iso(start), _iso(end)] for mission_id, (start, end) in sorted(overrides.items())]


def _tag_read(mission: MissionWindow) -> bool:
    return mission.category not in TAGLESS_CATEGORIES


def _tagged(mission: MissionWindow) -> bool:
    return _tag_read(mission) and bool(mission.tag or mission.tag_span)


def _covers(mission: MissionWindow, tag: int) -> bool:
    return _tag_read(mission) and mission.covers(tag)


def _client_created(mission: MissionWindow, registry: MissionWindows) -> bool:
    return mission.category in CONFIG_CREATED_CATEGORIES or bool(registry.members(mission.mission_id))


def _mission_record(mission: MissionWindow) -> dict:
    return {"mission_id": mission.mission_id, "category": mission.category, "enable": mission.enable,
            "start": _iso(mission.start), "end": _iso(mission.end), "tag": mission.tag, "tag_span": mission.tag_span}


class MissionTagIndex:
    def __init__(self, registry: MissionWindows, *, quest_rows: Mapping[int, Mapping], source_sha256: Mapping[str, str],
                 master_snapshot: str, overrides=None, categories_without_tag_column: Iterable[int] = ()):
        if not isinstance(registry, MissionWindows) or registry.categories != REGISTRY_CATEGORIES:
            raise ValueError("Tag resolution requires one registry of every client-loaded mission category")
        if not isinstance(quest_rows, Mapping):
            raise ValueError("Tag resolution requires the quest table rows")
        if type(master_snapshot) is not str or not master_snapshot:
            raise ValueError("Tag resolution requires a master snapshot identifier")
        untagged = frozenset(categories_without_tag_column)
        if any(type(category) is not int for category in untagged) or not untagged <= set(REGISTRY_CATEGORIES):
            raise ValueError("Tag columns can only be absent from registry categories")
        self.registry = registry
        self.source_sha256 = _sources(source_sha256)
        self.master_snapshot = master_snapshot
        self.overrides = validate_window_overrides(overrides)
        self.categories_without_tag_column = untagged
        self._quest_rows = quest_rows
        self._ordered = registry.ordered()
        self._by_id = MappingProxyType({mission.mission_id: mission for mission in self._ordered})
        self._by_category = MappingProxyType({
            category: tuple(mission for mission in self._ordered if mission.category == category)
            for category in REGISTRY_CATEGORIES})
        self._created = frozenset(mission.mission_id for mission in self._ordered if _client_created(mission, registry))
        self._tag_walk = tuple(mission for mission in self._ordered
                               if mission.mission_id in self._created and _tag_read(mission))
        self._memo: dict[tuple[int, int], MissionWindow | None] = {}
        self._memo_lock = threading.Lock()
        self.content_version = _digest({
            "schema": CONTENT_SCHEMA, "rules": dict(RULES), "categories": list(REGISTRY_CATEGORIES),
            "config_created": sorted(CONFIG_CREATED_CATEGORIES), "tagless": sorted(TAGLESS_CATEGORIES),
            "sources": dict(self.source_sha256), "master": master_snapshot, "overrides": _window_values(self.overrides)})

    def lookup(self, kind: LookupKind, value: int, platform_code: int) -> MissionWindow | None:
        platform = require_platform(platform_code)
        kind, value = _kind(kind), _value(value)
        if kind == TAG_LOOKUP:
            return self.tag_mission(value, platform)
        mission = self._by_id.get(value)
        return mission if mission is not None and self._registered(mission, platform) else None

    def tag_mission(self, tag: int, platform_code: int) -> MissionWindow | None:
        platform = require_platform(platform_code)
        tag = _value(tag)
        # 8D75A0 refuses nonpositive tags, then walks the manager in MissionID order whether a mission is open or not.
        if tag <= 0:
            return None
        key = (tag, platform)
        with self._memo_lock:
            if key not in self._memo:
                if len(self._memo) >= MEMO_LIMIT:
                    self._memo.clear()
                self._memo[key] = next((mission for mission in self._tag_walk
                                        if mission.covers(tag) and mission.enabled(platform)), None)
            return self._memo[key]

    def is_open(self, kind: LookupKind, value: int, *, now: datetime, platform_code: int) -> bool:
        mission = self.lookup(kind, value, platform_code)
        clock = _clock(now)
        return mission is not None and mission.state(clock) == OPEN

    def displayable(self, kind: LookupKind, value: int, *, now: datetime, platform_code: int) -> bool:
        mission = self.lookup(kind, value, platform_code)
        clock = _clock(now)
        if mission is None or mission.state(clock) != OPEN:
            return False
        # 8CB810 would test the previous cursor row for a member absent from QuestTable; such a member stays closed.
        return any(quest_open_on(self._quest_rows[quest_id], clock)
                   for quest_id in self.registry.members(mission.mission_id) if quest_id in self._quest_rows)

    def rank_reached(self, kind: LookupKind, value: int, *, rank: int, platform_code: int) -> bool:
        mission = self.lookup(kind, value, platform_code)
        if not int32(rank):
            raise ValueError("Rank checks require a native player rank")
        # 991E30 compares the lowest member rank without refreshing the mission window.
        return mission is not None and rank >= self.registry.minimum_rank(mission.mission_id)

    def explain(self, kind: LookupKind, value: int, *, now: datetime | None = None, platform_code: int) -> dict:
        mission = self.lookup(kind, value, platform_code)
        kind = _kind(kind)
        clock = None if now is None else _clock(now)
        candidates = self._candidates(kind, value)
        return {
            "kind": kind, "value": value, "platform_code": platform_code, "clock": _iso(clock),
            "mission": None if mission is None else mission.mission_id,
            "reason": None if mission is not None else self._unresolved(kind, value, candidates),
            "open": None if clock is None else (mission is not None and mission.state(clock) == OPEN),
            "minimum_rank": None if mission is None else self.registry.minimum_rank(mission.mission_id),
            "candidate_count": len(candidates),
            "registered_count": sum(1 for candidate in candidates if self._registered(candidate, platform_code)),
            "candidates": [self._explained(candidate, mission, platform_code, clock)
                           for candidate in candidates[:LIST_LIMIT]],
        }

    def status(self, now: datetime | None = None, platform_code: int | None = None,
               consumers: Mapping = MappingProxyType({})) -> dict:
        platform = None if platform_code is None else require_platform(platform_code)
        clock = None if now is None else _clock(now)
        if (platform is None) != (clock is None):
            raise ValueError("Live tag status requires both a clock and a platform code")
        dependents = _consumers(consumers)
        with self._memo_lock:
            memo = {"entries": len(self._memo), "limit": MEMO_LIMIT}
        config_only = [mission.mission_id for mission in self._ordered if mission.mission_id not in self._created]
        result = {
            "schema": CONTENT_SCHEMA, "content_version": self.content_version, "master_snapshot": self.master_snapshot,
            "missions": len(self._ordered), "created_missions": len(self._created),
            "tagged_missions": sum(map(_tagged, self._ordered)),
            "config_only_count": len(config_only), "config_only": config_only[:LIST_LIMIT],
            "categories": {category: self._category_status(category) for category in REGISTRY_CATEGORIES},
            "server_provided": sorted(SERVER_PROVIDED_CATEGORIES),
            "categories_without_tag_column": sorted(self.categories_without_tag_column),
            "override_count": len(self.overrides), "overrides": self._override_records()[:LIST_LIMIT],
            "consumer_count": len(dependents),
            "consumers": [{"kind": kind, "value": value, "labels": list(labels[:LIST_LIMIT])}
                          for (kind, value), labels in dependents[:LIST_LIMIT]],
            "memo": memo, "rules": dict(RULES),
        }
        if clock is not None:
            lookups = self._live_lookups(dependents)
            result["live"] = {"clock": _iso(clock), "platform_code": platform, "lookup_count": len(lookups),
                              "lookups": [self._live(kind, value, labels, clock, platform)
                                          for (kind, value), labels in lookups[:LIST_LIMIT]]}
        return result

    def _registered(self, mission: MissionWindow, platform: int) -> bool:
        return mission.mission_id in self._created and mission.enabled(platform)

    def _category_status(self, category: int) -> dict:
        missions = self._by_category[category]
        return {"name": MISSION_CATEGORIES[category], "tables": list(mission_tables(category)),
                "creation": CREATION_NAMES[category in CONFIG_CREATED_CATEGORIES], "missions": len(missions),
                "created": sum(1 for mission in missions if mission.mission_id in self._created),
                "listed": sum(mission.listed for mission in missions), "tagged": sum(map(_tagged, missions)),
                "tag_column": category not in self.categories_without_tag_column,
                "tag_read": category not in TAGLESS_CATEGORIES}

    def _override_records(self) -> list[dict]:
        # An override on a mission the client never creates or no platform enables cannot change what a gate resolves.
        return [{"mission_id": mission_id, "start": start, "end": end, "known": mission_id in self._by_id,
                 "effective": mission_id in self._created and self._by_id[mission_id].listed}
                for mission_id, start, end in _window_values(self.overrides)]

    def _candidates(self, kind: str, value: int) -> tuple[MissionWindow, ...]:
        if kind == ID_LOOKUP:
            return tuple(mission for mission in (self._by_id.get(value),) if mission is not None)
        return () if value <= 0 else tuple(mission for mission in self._ordered if _covers(mission, value))

    @staticmethod
    def _unresolved(kind: str, value: int, candidates: tuple[MissionWindow, ...]) -> str:
        if kind == TAG_LOOKUP and value <= 0:
            return "tag_not_positive"
        return "not_registered" if candidates else "no_mission"

    def _explained(self, candidate: MissionWindow, winner: MissionWindow | None, platform: int,
                   clock: datetime | None) -> dict:
        registered = self._registered(candidate, platform)
        return {**_mission_record(candidate), "created": candidate.mission_id in self._created,
                "registered": registered, "state": None if clock is None else STATE_NAMES[candidate.state(clock)],
                "shadowed_by": (winner.mission_id if registered and winner is not None and candidate is not winner
                                else None)}

    def _live_lookups(self, dependents) -> tuple[tuple[tuple[str, int], tuple[str, ...]], ...]:
        keyed = dict(dependents)
        for tag in sorted({mission.tag for mission in self._ordered if _tag_read(mission) and mission.tag > 0}):
            keyed.setdefault((TAG_LOOKUP, tag), ())
        return tuple(keyed.items())

    def _live(self, kind: str, value: int, labels: tuple[str, ...], clock: datetime, platform: int) -> dict:
        mission = self.lookup(kind, value, platform)
        return {"kind": kind, "value": value, "consumers": list(labels[:LIST_LIMIT]),
                "mission": None if mission is None else mission.mission_id,
                "category": None if mission is None else mission.category,
                "state": None if mission is None else STATE_NAMES[mission.state(clock)],
                "open": self.is_open(kind, value, now=clock, platform_code=platform),
                "displayable": self.displayable(kind, value, now=clock, platform_code=platform),
                "minimum_rank": None if mission is None else self.registry.minimum_rank(mission.mission_id)}
