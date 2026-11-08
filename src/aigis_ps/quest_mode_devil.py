from __future__ import annotations

from types import MappingProxyType

from .errors import ProtocolError
from .quest_catalog import TREASURE_SLOTS
from .quest_modes import MissionWindow, ModePolicy, StartContext, require_row_values


DEVIL_TYPE = 9
# OnCommandMissionTypeDevil greys the menu out unless IsReachedRequiredRank("Tag", 150) holds.
MENU_TAG = 150
# Devil quests pay only first star rewards, so a Treasure code marks an unaudited resource.
NO_TREASURE = 0
ROW_VALUES = MappingProxyType({"Type": DEVIL_TYPE,
                               **{f"Treasure{slot}": NO_TREASURE for slot in range(1, TREASURE_SLOTS + 1)}})


class DevilPolicy(ModePolicy):
    name = "devil"
    quest_types = (DEVIL_TYPE,)
    enabled = True
    hard_mode = True
    gate_quest = 1004

    def validate_row(self, row, catalog) -> None:
        require_row_values(row, ROW_VALUES, "Devil advent")

    def check_start(self, context: StartContext) -> MissionWindow:
        mission = super().check_start(context)
        menu = self.menu_mission(context.catalog, context.platform_code)
        # IsReachedRequiredRank is false when no mission carries the tag, so the menu stays greyed out.
        if menu is None:
            raise ProtocolError("quest_rank_required", "No enabled devil advent mission carries the menu rank tag", 409)
        if context.rank < context.catalog.mission_registry().minimum_rank(menu.mission_id):
            raise ProtocolError("quest_rank_required", "Player rank is below the devil advent menu requirement", 409)
        return mission

    @staticmethod
    def menu_mission(catalog, platform_code: int) -> MissionWindow | None:
        # 991E30 resolves the tag over the whole mission manager, so a mission of any category can set the menu rank.
        return catalog.mission_tags().tag_mission(MENU_TAG, platform_code)


POLICY = DevilPolicy()
