from __future__ import annotations

from datetime import datetime
from types import MappingProxyType

from .errors import ProtocolError
from .mission_tags import STATE_NAMES, TAG_LOOKUP
from .quest_catalog import TREASURE_SLOTS
from .quest_modes import OPEN, MissionWindow, ModePolicy, StartContext, require_row_values


RAID_TYPE = 11
# OnCommandMissionTypeRaid greys the menu out unless IsReachedRequiredRank("Tag", 120) holds.
MENU_TAG = 120
# Raid quests pay only their first three-star unit, so a Treasure code marks an unaudited resource.
NO_TREASURE = 0
# Raid has no IsHardMode_10 command, so a four-star level marks an unaudited resource.
NO_HARD_LEVEL = 0
# 679D70 pays BonusType 3 as the BonusNum card alone.
UNIT_STAR_REWARD = 3
NO_FIXED_TEAM = 0
ROW_VALUES = MappingProxyType({
    "Type": RAID_TYPE, **{f"Treasure{slot}": NO_TREASURE for slot in range(1, TREASURE_SLOTS + 1)},
    "_HardLevel": NO_HARD_LEVEL, "BonusType": UNIT_STAR_REWARD, "UnitList": NO_FIXED_TEAM,
})
LOCAL_RULES = MappingProxyType({
    "start_path": "77AB70 DoQuestPlayable::Raid -> Func_RaidSallyView -> DoQuestStart_Raid -> 88AA20",
    "menu_tag": MENU_TAG,
    "menu_rank": "lowest_member_rank_of_the_first_registered_mission_covering_the_tag",
    "boost": "accepted_and_ignored",
    "formation": "bl_rows_of_every_raid_group",
})


class RaidPolicy(ModePolicy):
    name = "raid"
    quest_types = (RAID_TYPE,)
    enabled = True
    gate_quest = 1004

    def validate_row(self, row, catalog) -> None:
        require_row_values(row, ROW_VALUES, "Raid quest")

    def check_start(self, context: StartContext) -> MissionWindow:
        mission = super().check_start(context)
        if not self.menu_reached(context.catalog, rank=context.rank, platform_code=context.platform_code):
            raise ProtocolError("quest_rank_required", f"Player rank has not reached the raid menu rank of Tag {MENU_TAG}", 409)
        return mission

    def local_rules(self) -> dict:
        return dict(LOCAL_RULES)

    @staticmethod
    def menu_reached(catalog, *, rank: int, platform_code: int) -> bool:
        # 991E30 resolves the tag over the whole mission manager, so a mission of any category can set the menu rank.
        return catalog.mission_tags().rank_reached(TAG_LOOKUP, MENU_TAG, rank=rank, platform_code=platform_code)

    @staticmethod
    def describe(catalog, *, now: datetime, platform_code: int) -> dict:
        menu = catalog.mission_tags().explain(TAG_LOOKUP, MENU_TAG, now=now, platform_code=platform_code)
        # explain checks the platform code and a given clock but reads a missing clock as a static lookup.
        if menu["clock"] is None:
            raise ProtocolError("quest_clock_invalid", "Raid mission states require an aware datetime", 503)
        windows = catalog.mission_windows(RAID_TYPE)
        missions = {name: [] for name in STATE_NAMES.values()}
        disabled = []
        for mission in windows.ordered():
            if mission.enabled(platform_code):
                missions[STATE_NAMES[mission.state(now)]].append(mission.mission_id)
            else:
                disabled.append(mission.mission_id)
        open_missions = frozenset(missions[STATE_NAMES[OPEN]])
        quests = catalog.playable_quests(RAID_TYPE)
        open_quests = sum(1 for quest_id in quests
                          if any(mission.mission_id in open_missions for mission in windows.missions(quest_id)))
        return {"menu": menu, "missions": {**missions, "disabled": disabled},
                "quests": {"open": open_quests, "closed": len(quests) - open_quests}}


POLICY = RaidPolicy()
