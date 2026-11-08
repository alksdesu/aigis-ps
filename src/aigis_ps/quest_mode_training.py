from __future__ import annotations

from types import MappingProxyType

from .quest_modes import ModePolicy, require_row_values


PRACTICE_TYPE = 15
NO_FIXED_TEAM = 0
# BonusType 4 has no SystemText_QuestRewardName (67AD70), so the client shows no three-star reward.
NO_STAR_REWARD = 4
ROW_VALUES = MappingProxyType({"Type": PRACTICE_TYPE, "UnitList": NO_FIXED_TEAM, "BonusType": NO_STAR_REWARD})


class TrainingPolicy(ModePolicy):
    name = "training"
    quest_types = (PRACTICE_TYPE,)
    enabled = True
    gate_quest = 1004

    def validate_row(self, row, catalog) -> None:
        require_row_values(row, ROW_VALUES, "Practice quest")


POLICY = TrainingPolicy()
