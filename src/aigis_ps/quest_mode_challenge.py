from __future__ import annotations

from types import MappingProxyType

from .errors import ProtocolError
from .quest_catalog import GIFT_REWARD_TYPES
from .quest_modes import ModePolicy, require_row_values


CHALLENGE_TYPE = 1
NO_FIXED_TEAM = 0
ROW_VALUES = MappingProxyType({"Type": CHALLENGE_TYPE, "UnitList": NO_FIXED_TEAM})


class ChallengePolicy(ModePolicy):
    name = "challenge"
    quest_types = (CHALLENGE_TYPE,)
    enabled = True
    gate_quest = 1004

    def validate_row(self, row, catalog) -> None:
        require_row_values(row, ROW_VALUES, "Challenge quest")
        bonus = row.get("BonusType")
        if type(bonus) is not int or bonus not in GIFT_REWARD_TYPES:
            raise ProtocolError("quest_definition_invalid", "Challenge quest field BonusType is not a gift reward type", 503)
        if [reward.kind for reward in catalog.star_rewards(row, False)] != ["item"]:
            raise ProtocolError("quest_definition_invalid", "Challenge three-star reward does not resolve to its gift item", 503)


POLICY = ChallengePolicy()
