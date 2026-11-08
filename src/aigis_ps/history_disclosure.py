from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from types import MappingProxyType

from .errors import ProtocolError
from .mission_tags import mission_tables
from .native_clock import NATIVE_EPOCH, parse_native_date
from .native_numbers import INT32_MIN, float32, int32, signed32
from .quest_catalog import QuestCatalog
from .quest_modes import MODE_SPECS, MissionWindow, StartContext


HISTORY_TYPE = 16
HISTORY_CATEGORY = MODE_SPECS[HISTORY_TYPE].mission_category
DISCLOSURE_TABLE = "CfgMissionHistoryTable.atb"
CONFIG_TABLE = mission_tables(HISTORY_CATEGORY)[0]
ENTRY_FIELDS = ("MissionId", "GroupId", "DisclosureConditionDays", "DisclosureConditionMissionId",
                "DisclosureConditionRank")
NO_PREREQUISITE = 0
# 654A20 counts from AACB20(2016, 4, 14) when GameStartDateTime is unset.
UNSET_START_DAY = date(2016, 4, 14).toordinal() - 1
# 8CD4F0 credits three stars per member and one more for a nonzero CHardModeProperty Level.
MEMBER_STARS = 3
PERCENT_SCALE = 100.0
# 94A9B0 adds 0x3F7FFFFE before cvttss2si, so exact integer products round one star higher.
CEILING_BIAS = float32(1 - 2 ** -23)


@dataclass(frozen=True)
class DisclosureEntry:
    mission_id: int
    group: int
    days: int
    prerequisite: int
    percent: int


@dataclass(frozen=True)
class HistoryDisclosure:
    entries: Mapping[int, DisclosureEntry]
    groups: Mapping[int, tuple[DisclosureEntry, ...]]

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping]) -> HistoryDisclosure:
        entries, groups = {}, {}
        try:
            for row in rows:
                values = tuple(row[name] for name in ENTRY_FIELDS)
                entry = DisclosureEntry(*values)
                if not all(int32(value) for value in values) or entry.mission_id in entries:
                    raise _resource_invalid(DISCLOSURE_TABLE)
                entries[entry.mission_id] = entry
                # 948870 appends each row to its group's list in table order.
                groups.setdefault(entry.group, []).append(entry)
        except (KeyError, TypeError) as error:
            raise _resource_invalid(DISCLOSURE_TABLE) from error
        return cls(MappingProxyType(entries), MappingProxyType({key: tuple(value) for key, value in groups.items()}))

    def group_open(self, entry: DisclosureEntry, days: int) -> bool:
        # 9487A0 subtracts the elapsed days from the group's first row in a 32-bit register.
        return signed32(self.groups[entry.group][0].days - days) <= 0

    def prerequisite(self, entry: DisclosureEntry) -> DisclosureEntry | None:
        # 94A9B0 searches only the current group, so a prerequisite elsewhere never holds the mission back.
        target = self.entries.get(entry.prerequisite) if entry.prerequisite != NO_PREREQUISITE else None
        return target if target is not None and target.group == entry.group else None


def _resource_invalid(table: str) -> ProtocolError:
    return ProtocolError("quest_mode_resource_invalid", f"{table} differs from the native History disclosure contract", 503)


def _state_invalid(message: str) -> ProtocolError:
    return ProtocolError("quest_state_invalid", message, 503)


def star_threshold(total: int, percent: int) -> int:
    # 94A9B0 multiplies, divides and adds in single precision; cvttss2si yields INT32_MIN outside int32.
    value = float32(float32(float32(float32(total) * float32(percent)) / PERCENT_SCALE) + CEILING_BIAS)
    return int(value) if INT32_MIN <= value < -INT32_MIN else INT32_MIN


def elapsed_days(state: Mapping | None) -> int:
    try:
        row = state["tables"]["game_param2"][0]
        login, started = row["LastLoginDate"], row["GameStartDateTime"]
    except (KeyError, IndexError, TypeError) as error:
        raise _state_invalid("History disclosure requires the saved login and game start dates") from error
    if not int32(login) or login < 0:
        raise _state_invalid("The saved last login date is not a native day number")
    try:
        moment = parse_native_date(started)
    except ValueError as error:
        raise _state_invalid("The saved game start date is not a native date") from error
    # 63EBA0 truncates the parsed milliseconds to whole days, so no client calendar bound applies.
    start = UNSET_START_DAY if moment is None else (moment - NATIVE_EPOCH) // timedelta(days=1)
    # 654A20 reads two server-sent day numbers, never the clock.
    return signed32(login - start)


def disclosure(catalog) -> HistoryDisclosure:
    return HistoryDisclosure.from_rows(catalog.resource_rows(DISCLOSURE_TABLE))


def _members(catalog, mission_id: int) -> tuple[int, ...]:
    configs = [row for row in catalog.resource_rows(CONFIG_TABLE) if row.get("MissionID") == mission_id]
    if len(configs) != 1:
        raise _resource_invalid(CONFIG_TABLE)
    # 8C9BB0 keeps QuestIdList order and repeats, so a repeated member counts twice.
    return tuple(member["QuestID"] for member in QuestCatalog._quest_id_list_members(configs))


def _hard_level(catalog, quest_id: int) -> int:
    # The client would read a stale QuestTable cursor for a missing member, so the outcome is undetermined.
    if catalog.quest_type(quest_id) is None:
        raise ProtocolError("quest_mode_resource_invalid", "A History disclosure member is absent from QuestTable", 503)
    level = catalog.row(quest_id).get("_HardLevel")
    if not int32(level):
        raise ProtocolError("quest_mode_resource_invalid", "A History disclosure member has no native hard level", 503)
    return level


def _max_rank(records: Mapping, quest_id: int) -> int:
    value = records.get(quest_id, {}).get("MaxRank", 0)
    if not int32(value):
        raise _state_invalid("A History disclosure member record has no native star count")
    return value


def _mission_stars(context: StartContext, mission_id: int) -> tuple[int, int]:
    registered = next((mission for mission in context.catalog.mission_windows(HISTORY_TYPE).ordered()
                       if mission.mission_id == mission_id), None)
    # 948E50 leaves both counts at zero for a mission the platform never registered.
    if registered is None or not registered.enabled(context.platform_code):
        return 0, 0
    total = earned = 0
    for quest_id in _members(context.catalog, mission_id):
        total = signed32(total + MEMBER_STARS + (_hard_level(context.catalog, quest_id) != 0))
        earned = signed32(earned + _max_rank(context.records, quest_id))
    return total, earned


def require_disclosed(context: StartContext, mission: MissionWindow) -> None:
    days = elapsed_days(context.state)
    index = disclosure(context.catalog)
    entry = index.entries.get(mission.mission_id)
    if entry is None:
        raise ProtocolError("quest_closed", "The History mission has no disclosure row", 409)
    if not index.group_open(entry, days):
        raise ProtocolError("quest_closed", "The History group is not disclosed on the last login day", 409)
    prerequisite = index.prerequisite(entry)
    if prerequisite is None:
        return
    if entry.days > days:
        raise ProtocolError("quest_closed", "The History mission is not disclosed on the last login day", 409)
    total, earned = _mission_stars(context, prerequisite.mission_id)
    if star_threshold(total, entry.percent) > earned:
        raise ProtocolError("quest_closed", "The History mission needs more stars on its disclosure mission", 409)
