from __future__ import annotations

from types import MappingProxyType

from .errors import ProtocolError
from .quest_catalog import TREASURE_SLOTS
from .quest_modes import MissionWindow, ModePolicy, SettleContext, SettleOutcome, StartContext, require_row_values
from .tower_catalog import GATE_QUEST, SCORE_REWARD_TABLE, SCORING_TABLE, TOWER_TYPE, TowerCatalog
from .tower_service import LOCAL_RULES
from .tower_state import CHALLENGE_COST, TowerRules, record_floor, reserve_charge, team_rows


NO_FIXED_TEAM = 0
# Floors pay only through the tower score and total-star tables, so a Treasure code marks an unaudited resource.
NO_TREASURE = 0
ROW_VALUES = MappingProxyType({"Type": TOWER_TYPE, "UnitList": NO_FIXED_TEAM,
                               **{f"Treasure{slot}": NO_TREASURE for slot in range(1, TREASURE_SLOTS + 1)}})


def _invalid(message: str) -> ProtocolError:
    return ProtocolError("quest_definition_invalid", message, 503)


class TowerPolicy(ModePolicy):
    name = "tower"
    quest_types = (TOWER_TYPE,)
    enabled = True
    gate_quest = GATE_QUEST

    def validate_row(self, row, catalog) -> None:
        require_row_values(row, ROW_VALUES, "Tower")
        quest_id = row.get("QuestID")
        listed = [mission for mission in catalog.mission_windows(TOWER_TYPE).missions(quest_id) if mission.listed]
        if len(listed) != 1:
            raise _invalid("Tower floors must belong to exactly one enabled tower")
        if not any(entry.get("QuestID") == quest_id for entry in catalog.resource_rows(SCORING_TABLE)):
            raise _invalid("Tower floors require their row in the shared scoring table")
        rewards = catalog.resource_rows(SCORE_REWARD_TABLE.format(listed[0].mission_id))
        if not any(entry.get("QuestID") == quest_id for entry in rewards):
            raise _invalid("Tower floors require their score reward group")

    @staticmethod
    def _tower(context):
        towers = context.services.tower
        if towers is None or context.services.presents is None:
            raise ProtocolError("quest_tower_unavailable", "Tower battles require the tower and present services", 409)
        if towers.catalog.quests is not context.catalog:
            raise ProtocolError("quest_tower_catalog_mismatch", "The tower service reads another quest catalog", 503)
        return towers, towers.catalog.floor_tower(context.quest.quest_id)

    def check_start(self, context: StartContext) -> MissionWindow:
        mission = super().check_start(context)
        towers, tower = self._tower(context)
        if not TowerCatalog.disclosed(tower, context.records):
            raise ProtocolError("quest_locked", "The tower opens once its disclosure quest is cleared", 409)
        if towers.challenges(context.state, tower, context.now) < CHALLENGE_COST:
            raise ProtocolError("quest_tower_consume_shortage", "The tower floor needs a challenge item", 409)
        return mission

    def prepare_start(self, context: StartContext, state: dict) -> tuple[dict, tuple]:
        towers, tower = self._tower(context)
        state, changed = towers.settle(state, context.now)
        # Official notice 9370: only the clear consumes the challenge item, so the start answers the unspent count.
        state = reserve_charge(state, towers.catalog, context.quest.quest_id)
        items = tuple(dict.fromkeys((tower.consume_item_id, *changed)))
        return state, (towers.possessions.response(state, items, include_enabled=True),)

    def settle(self, context: SettleContext) -> SettleOutcome:
        towers, tower = self._tower(context)
        quest_id, request = context.quest.quest_id, context.request
        # A result settles the elapsed generation days before its score can raise TotalScore.
        state, changed = towers.settle(context.state, context.now)
        state, _ = towers.charge_challenge(state, tower, quest_id)
        player = towers.player(state)
        score = towers.catalog.score_floor(quest_id, context.stars, sallies=request.sallies, members=request.members,
                                           visitors=request.visitors, ux=player["battle"]["UX"], rules=towers.rules)
        state, _, _, _, total = record_floor(state, towers.catalog, tower.mission_id, quest_id, score.score,
                                             team=team_rows(player["units"], request.members))
        state, presents = towers.rewards(state)
        items = tuple(dict.fromkeys((tower.consume_item_id, *changed)))
        return SettleOutcome(state, (score.result(total),), item_ids=items, presents=presents)

    def local_rules(self) -> dict:
        return {"gate_quest": GATE_QUEST, "default_rules": TowerRules().to_dict(),
                "configured_rules": "TowerService.status reports the tower rules file in force", **LOCAL_RULES}


POLICY = TowerPolicy()
