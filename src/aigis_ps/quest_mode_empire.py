from __future__ import annotations

from datetime import datetime
from types import MappingProxyType

from .errors import ProtocolError
from .mission_tags import TAG_LOOKUP
from .quest_modes import MODE_SPECS, MissionWindow, ModePolicy, StartContext, require_row_values


EMPIRE_TYPE = 18
ROW_VALUES = MappingProxyType({"Type": EMPIRE_TYPE})
EMPIRE_CATEGORY = MODE_SPECS[EMPIRE_TYPE].mission_category
MISSION_CONFIG = "EmpireMissionConfig.atb"
# The Empire portal banner and PageEmpire tab need HasAnyDisplayableQuest and IsOpenMission on Tag 801.
MENU_TAG = 801


class EmpirePolicy(ModePolicy):
    name = "empire"
    quest_types = (EMPIRE_TYPE,)
    enabled = True
    hard_mode = True
    gate_quest = 1004
    # The entry gate reads no request field, so entrust admission can run the same check_start.
    entrust_parity = frozenset({"check_start"})

    def validate_row(self, row, catalog) -> None:
        require_row_values(row, ROW_VALUES, "Empire quest")
        mission = self._owner(row.get("QuestID"), catalog)
        if row.get("_HardLevel"):
            self._check_dark_crystal(row, catalog, mission.mission_id)

    @staticmethod
    def _owner(quest_id, catalog):
        listed = [mission for mission in catalog.mission_windows(EMPIRE_TYPE).missions(quest_id) if mission.listed]
        if not listed:
            raise ProtocolError("quest_closed", "Empire quest has no enabled mission", 409)
        if len(listed) != 1 or listed[0].category != EMPIRE_CATEGORY:
            raise ProtocolError("quest_mode_resource_invalid",
                                "Empire quests must be listed by exactly one enabled Empire mission", 503)
        return listed[0]

    @staticmethod
    def _check_dark_crystal(row, catalog, mission_id):
        item_id = next((config.get("DarkCrystalItemId") for config in catalog.resource_rows(MISSION_CONFIG)
                        if config.get("MissionID") == mission_id), None)
        if any(reward.kind != "item" or reward.item_id != item_id for reward in catalog.star_rewards(row, True)):
            raise ProtocolError("quest_mode_resource_invalid",
                                "Empire four-star rewards must grant the mission's dark crystal item", 503)

    def check_start(self, context: StartContext) -> MissionWindow:
        mission = super().check_start(context)
        if not self.entry_open(context.catalog, now=context.now, platform_code=context.platform_code):
            raise ProtocolError("quest_closed",
                                f"The empire menu needs an open Tag {MENU_TAG} mission showing a quest today", 409)
        return mission

    def entry_open(self, catalog, *, now: datetime, platform_code: int) -> bool:
        tags = catalog.mission_tags()
        return (tags.displayable(TAG_LOOKUP, MENU_TAG, now=now, platform_code=platform_code)
                and tags.is_open(TAG_LOOKUP, MENU_TAG, now=now, platform_code=platform_code))


POLICY = EmpirePolicy()
