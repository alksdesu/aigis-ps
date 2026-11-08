from __future__ import annotations

from types import MappingProxyType

from .errors import ProtocolError
from .history_disclosure import HISTORY_CATEGORY, require_disclosed
from .native_numbers import int32
from .quest_catalog import StarReward
from .quest_modes import MAX_OBTAINED, MissionWindow, ModePolicy, StartContext, require_row_values
from .quest_protocol import MAX_CANDIDATES


REPRODUCE_TYPE = 8
# OnCommandMenuQuestCommon has no IsHardMode_3 or IsHardMode_14 command, so a four-star level marks an unaudited resource.
NO_HARD_LEVEL = 0
# 679D70 pays BonusType 0 as BonusNum free crystals; Reproduce rows name none, so the base pays its local count.
CRYSTAL_STAR_REWARD = 0
NO_STAR_CRYSTALS = 0
ROW_VALUES = MappingProxyType({"Type": REPRODUCE_TYPE, "_HardLevel": NO_HARD_LEVEL,
                               "BonusType": CRYSTAL_STAR_REWARD, "BonusNum": NO_STAR_CRYSTALS})
FREE_CRYSTAL = (StarReward("crystal", CRYSTAL_STAR_REWARD, NO_STAR_CRYSTALS),)
LOCAL_RULES = MappingProxyType({
    "reproduce_windows": "closed_unless_mission_window_rules_reopen_the_batch",
    "history_windows": "native_history_mission_window",
    "candidate_selection": "start_record_raw_max_rank_saved_as_candidate_stars",
    "candidate_limit": MAX_CANDIDATES,
    "special_prizes": "isappear_quest_star_comparisons_drawn_like_ordinary_candidates",
    "override_prizes": "drop_percent_code_without_slot_bit_or_collection",
    "stage_variant_prizes": "listed_only_below_three_stars",
    "dated_prizes": "refused",
    "skipped_prizes": "excluded_and_counted",
    "obtained_limit": MAX_OBTAINED,
    "treasure_slots": "slots_4_and_5_pay_and_mark_like_slots_1_to_3",
    "first_three_stars": "quest_rules_first_three_star_crystals",
    "stack_zero_items": "possession_bounded_by_int32_without_iof",
    "gold_get": "counted",
    "drop_bonus": "attempt_start_or_entrust_time_jst_weekday_value_floored_once_per_request",
})


class ReproducePolicy(ModePolicy):
    name = "reproduce"
    quest_types = (REPRODUCE_TYPE,)
    enabled = True
    gate_quest = 1004
    # The History disclosure check reads no request field, so entrust admission can run the same check_start.
    entrust_parity = frozenset({"check_start"})

    def validate_row(self, row, catalog) -> None:
        require_row_values(row, ROW_VALUES, "Reproduce quest")
        unit_list = row.get("UnitList")
        if not int32(unit_list) or unit_list < 0 or (unit_list and not catalog.fixed_units(unit_list)):
            raise ProtocolError("quest_definition_invalid", "Reproduce UnitList must name a fixed native team", 503)
        if catalog.star_rewards(row, False) != FREE_CRYSTAL:
            raise ProtocolError("quest_definition_invalid", "Reproduce first three-star reward is not the free crystal", 503)

    def check_start(self, context: StartContext) -> MissionWindow:
        mission = super().check_start(context)
        # A reproduce quest listed by a History mission starts from that mission's menu, which opens once disclosed.
        if mission.category == HISTORY_CATEGORY:
            require_disclosed(context, mission)
        return mission

    def local_rules(self) -> dict:
        return dict(LOCAL_RULES)


POLICY = ReproducePolicy()
