from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from types import MappingProxyType

from .alxml import Column, Container
from .bootstrap import PGP2_FIELDS
from .errors import ProtocolError
from .native_numbers import int32
from .present_backlog import require_deliverable
from .quest_modes import (
    MODE_PROGRESS, MissionWindow, ModePolicy, ModeServices, SettleContext, SettleOutcome, StartContext,
    require_row_values,
)


GODDESS_TYPE = 19
ROW_VALUES = MappingProxyType({"Type": GODDESS_TYPE})
REWARD_TABLE = "GoddessRevivalRewardConfig.atb"
CONFIG_TABLE = "CfgMissionGoddessRevivalTable.atb"
TOTAL_STAR_FIELD = "GoddessRevivalTotalStarCount"
TOTAL_STAR_KEY = next(wire for wire, field in PGP2_FIELDS if field == TOTAL_STAR_FIELD)
GRANTED_FIELD = "granted"
# Goddess quests have no four-star challenge, so one result lifts a quest to three stars at most.
BEST_RESULT_STARS = 3
UNIT_REWARD_TYPE = 1
ITEM_REWARD_TYPE = 22
UNIT_REWARD_COUNT = 1
# SystemText 6267 is the resource's only goddess reward label.
DESCRIPTION = "女神の復活クエスト報酬"


@dataclass(frozen=True)
class RewardTier:
    threshold: int
    reward_type: int
    param: int
    item_id: str

    def present(self) -> dict:
        if self.reward_type == ITEM_REWARD_TYPE and self.item_id and self.param > 0:
            return {"kind": "item", "amount": self.param, "item_id": self.item_id, "description": DESCRIPTION}
        # 9173E0 hands RewardParam to 630BA0, which reads Type 1 through PresentUnitData like a present Content.
        if self.reward_type == UNIT_REWARD_TYPE and self.param > 0:
            return {"kind": "unit", "template_id": self.param, "count": UNIT_REWARD_COUNT, "description": DESCRIPTION}
        raise ProtocolError("quest_goddess_reward_unsupported",
                            f"Goddess total star reward {self.threshold} has no supported present form", 501)


def _resource_invalid(table: str) -> ProtocolError:
    return ProtocolError("quest_mode_resource_invalid", f"{table} differs from the native goddess revival contract", 503)


def reward_tiers(catalog) -> tuple[RewardTier, ...]:
    tiers = {}
    try:
        for row in catalog.resource_rows(REWARD_TABLE):
            tier = RewardTier(row["RequiredStarCount"], row["RewardType"], row["RewardParam"], row["GeneralItemID"])
            if (not all(int32(value) for value in (tier.threshold, tier.reward_type, tier.param))
                    or tier.threshold <= 0 or tier.threshold in tiers or not isinstance(tier.item_id, str)):
                raise _resource_invalid(REWARD_TABLE)
            tiers[tier.threshold] = tier
    except KeyError as error:
        raise _resource_invalid(REWARD_TABLE) from error
    return tuple(tiers[threshold] for threshold in sorted(tiers))


def _star_requirements(catalog) -> Mapping[tuple[int, int], int]:
    required = {}
    try:
        for row in catalog.resource_rows(CONFIG_TABLE):
            mission_id, quest_id, need = row["MissionGroupId"], row["QuestId"], row["NeedStarNum"]
            if not (int32(mission_id) and int32(quest_id) and int32(need)) or mission_id <= 0 or quest_id <= 0:
                raise _resource_invalid(CONFIG_TABLE)
            # 784C60 hides the quest while any row for it in the mission needs more stars than the saved total.
            required[mission_id, quest_id] = max(required.get((mission_id, quest_id), 0), need)
    except KeyError as error:
        raise _resource_invalid(CONFIG_TABLE) from error
    return MappingProxyType(required)


def _saved_total(state) -> int:
    try:
        value = state["tables"]["game_param2"][0][TOTAL_STAR_FIELD]
    except (KeyError, IndexError, TypeError) as error:
        raise ProtocolError("quest_state_invalid", "Goddess revival quests require the saved total star count", 503) from error
    if not int32(value) or value < 0:
        raise ProtocolError("quest_state_invalid", "The saved goddess total star count is not a native count", 503)
    return value


def _reward_record(state, name: str) -> Mapping | None:
    journal = state.get("quest_state") or {}
    record = journal.get(MODE_PROGRESS, {}).get(name)
    if record is not None and (not isinstance(record, dict) or set(record) != {GRANTED_FIELD}
                               or not int32(record[GRANTED_FIELD]) or record[GRANTED_FIELD] < 0):
        raise ProtocolError("quest_state_invalid", "The goddess rewarded total is not a native count", 503)
    return record


def _granted(record: Mapping | None, saved: int) -> int:
    # Saves without the record mailed every tier up to the total they had reached.
    return saved if record is None else record[GRANTED_FIELD]


def _total_stars(catalog, records: Iterable[Mapping]) -> int:
    return sum(row["MaxRank"] for row in records if catalog.quest_type(row["QuestID"]) == GODDESS_TYPE)


def _reachable_total(context: StartContext) -> int:
    best = context.records.get(context.quest.quest_id, {}).get("MaxRank", 0)
    return _total_stars(context.catalog, context.records.values()) - best + max(best, BEST_RESULT_STARS)


def _crossed_tiers(catalog, granted: int, total: int) -> tuple[RewardTier, ...]:
    if total <= granted:
        return ()
    return tuple(tier for tier in reward_tiers(catalog) if granted < tier.threshold <= total)


def _presents(services: ModeServices):
    if services.presents is None:
        raise ProtocolError("quest_present_unavailable",
                            "Goddess revival total star rewards require the present service", 409)
    return services.presents


def _total_star_update(total: int) -> Container:
    return Container("PGP2", (Column("A1", "S", (TOTAL_STAR_KEY,)), Column("A2", "S", (str(total),))))


class GoddessPolicy(ModePolicy):
    name = "goddess"
    quest_types = (GODDESS_TYPE,)
    enabled = True

    def validate_row(self, row, catalog) -> None:
        require_row_values(row, ROW_VALUES, "Goddess revival quest")
        quest_id, unit_list = row.get("QuestID"), row.get("UnitList")
        if not int32(unit_list) or unit_list < 0 or (unit_list and not catalog.fixed_units(unit_list)):
            raise ProtocolError("quest_definition_invalid", "Goddess revival UnitList must name a fixed native team", 503)
        required = _star_requirements(catalog)
        if not any((mission.mission_id, quest_id) in required
                   for mission in catalog.mission_windows(GODDESS_TYPE).missions(quest_id)):
            raise ProtocolError("quest_mode_resource_invalid",
                                "CfgMissionGoddessRevivalTable has no row for the quest under its mission", 503)
        reward_tiers(catalog)

    def check_start(self, context: StartContext) -> MissionWindow:
        mission = super().check_start(context)
        saved = _saved_total(context.state)
        granted = _granted(_reward_record(context.state, self.name), saved)
        if _star_requirements(context.catalog).get((mission.mission_id, context.quest.quest_id), 0) > saved:
            raise ProtocolError("quest_locked", "The goddess revival quest requires more total stars", 409)
        presents = _presents(context.services)
        for tier in _crossed_tiers(context.catalog, granted, _reachable_total(context)):
            reward = tier.present()
            try:
                require_deliverable(presents, reward)
            except ProtocolError as error:
                raise ProtocolError("quest_goddess_reward_unsupported",
                                    f"Goddess total star reward {tier.threshold} cannot be mailed", 501) from error
        return mission

    def settle(self, context: SettleContext) -> SettleOutcome:
        saved = _saved_total(context.state)
        record = _reward_record(context.state, self.name)
        granted = _granted(record, saved)
        total = _total_stars(context.catalog, context.state["tables"]["quest_records"])
        settled = {GRANTED_FIELD: max(granted, total)}
        if total == saved and record == settled:
            return SettleOutcome(context.state)
        state = deepcopy(context.state)
        state["tables"]["game_param2"][0][TOTAL_STAR_FIELD] = total
        state["quest_state"].setdefault(MODE_PROGRESS, {})[self.name] = settled
        children = (_total_star_update(total),) if total != saved else ()
        rewards = tuple(tier.present() for tier in _crossed_tiers(context.catalog, granted, total))
        return SettleOutcome(state, children, presents=rewards)

    def undeliverable_rewards(self, catalog, services: ModeServices) -> tuple[dict, ...]:
        presents, undeliverable = _presents(services), []
        for tier in reward_tiers(catalog):
            try:
                require_deliverable(presents, tier.present())
            except ProtocolError as error:
                undeliverable.append({**asdict(tier), "reason": error.code})
        return tuple(undeliverable)


POLICY = GoddessPolicy()
