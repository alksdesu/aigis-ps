from copy import deepcopy
from datetime import datetime
import json
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps import quest_modes, tower_catalog
from aigis_ps.alxml import Column, Container
from aigis_ps.energy_clock import energy_seconds, set_anchor
from aigis_ps.errors import ProtocolError
from aigis_ps.native_numbers import float32
from aigis_ps.present_backlog import MAX_PRESENT_TEXT_BYTES, UNBOUNDED_PRESENT
from aigis_ps.quest_mode_tower import POLICY, ROW_VALUES, TowerPolicy
from aigis_ps.quest_modes import ENTRUST_TYPES, MissionWindow
from aigis_ps.quest_service import QuestService
from aigis_ps.storage import Store
from aigis_ps.tower_catalog import (
    CRYSTAL_REWARD, DESCRIPTIONS, FLOAT_RESULTS, GATE_QUEST, INTEGER_RESULTS, SCORE_REWARD_TABLE, SCORE_SOURCE,
    SCORING_TABLE, STAR_SOURCE, TOWER_TYPE, UNKNOWN_REWARD,
)
from aigis_ps.tower_service import LOCAL_RULES, TowerService
from aigis_ps.tower_state import CHALLENGE_COST, DOMAIN, TowerRules
from test_quest_battle_modes import MISSION_NOW, bare_start, hard_start, set_rank
from test_quest_protocol import sally_container
from test_quest_service import bundle, exit_request, finish_request, nodes
from test_quest_settlement import TEAM_COLUMNS, derived, formation
from test_tower_service import (
    CONSUME, CONSUME_STACK, DAILY_REFILL, ENABLED, FIRST_GENERATION, GENERAL, GENERAL_FLOOR, HERO, HERO_FLOOR,
    HERO_SECOND, HERO_THIRD, LEGACY_COLUMNS, POOL_CLOSED, SPIRIT, SPIRIT_FLOOR, TICKET, TICKET_TIER, TOWER_RANK, Clock,
    clear, column_names, floor, held, hold, mutate, possession, team_of, tower_record, towers, wire_rows,
    without_enabled,
)
from test_tower_state import snapshot


NOW = datetime(2026, 11, 2, 12)
TOWER_FLOORS = 123
FORMATION_CARD = 67
# Condition 1180 asks for a Genus 105 only team on 悪霊の塔 floor 11, one of the 22 tower team challenges.
SPIRIT_CHALLENGE, SPIRIT_CHALLENGE_FLOOR, SPIRIT_GENUS, GENUS_CARD = 1180, 12011, 105, 437
TOWER_CHALLENGES = 22
GUEST_INDEX = 900000
SCORE_TEXT, STAR_TEXT = DESCRIPTIONS[SCORE_SOURCE], DESCRIPTIONS[STAR_SOURCE]
HARD_REFUSAL = ("quest_hard_unavailable", 409)
# Four sorties: two plain, one token and one guest, costing 10, 12, 5 and 8.
SALLIES = sally_container(((0, 10, 10, 0), (False, False, False, False)),
                          ((1, 12, 12, 0), (False, False, False, False)),
                          ((-1, 5, 5, 0), (True, False, False, False)),
                          ((2, 8, 8, 0), (False, True, False, False)))
CONSUMED_COST = 35
# Two formation units and one support unit at 0.25 make 2.25 members; the prince pays his cost but never counts.
MEMBERS = 2.25
# 英傑: StarCountPoint3 × (7.5 − 0.02 × 35) × (2.5 − 0.1 × 2.25) with sorties unscored and guests counted whole.
HERO_RESULT = {"ConsumeCost": CONSUMED_COST, "StarScore": 10000, "Score": 154700, "MemberCount": MEMBERS,
               "ConsumeCostScore": 6.8, "MemberCountScore": 2.275, "SallyCount": 3.0, "SallyCountScore": 1.0}
# 統帥: StarCountPoint3 × (2.5 − 0.1 × 2.25) × (7.5 − 0.45 × 2.25) with the guest sortie at 0.25.
GENERAL_RESULT = {"ConsumeCost": CONSUMED_COST, "StarScore": 10000, "Score": 147590, "MemberCount": MEMBERS,
                  "ConsumeCostScore": 1.0, "MemberCountScore": 2.275, "SallyCount": 2.25, "SallyCountScore": 6.4875}
# 悪霊: StarCountPoint2 × (2.5 − 0.05 × 2.25) × (5 − 0.085 × 2.25).
SPIRIT_RESULT = {"ConsumeCost": CONSUMED_COST, "StarScore": 5000, "Score": 57404, "MemberCount": MEMBERS,
                 "ConsumeCostScore": 1.0, "MemberCountScore": 2.3875, "SallyCount": 2.25, "SallyCountScore": 4.80875}
# The same 悪霊 run at one star scores StarCountPoint1 = 2500 instead.
SPIRIT_ONE_STAR = 28702
# 英傑 floor 1 closes its score tiers with RewardID 10, one divine crystal at 90000.
DIVINE_TIER = 10
FREE_CRYSTAL_PRESENT = 0


@pytest.fixture
def battle(bundle, towers, tmp_path):
    clock = Clock(NOW)
    quests = QuestService(bundle.quests.catalog, bundle.bootstrap, bundle.player_state, bundle.possessions,
                          clock=clock, randbelow=lambda _: 0)
    quests.presents = bundle.presents
    quests.tower = TowerService(bundle.bootstrap, bundle.player_state, bundle.possessions, bundle.presents, towers,
                                rules=TowerRules(), clock=clock)
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    set_anchor(initial["tables"]["game_param"][0], energy_seconds(NOW))
    initial, _ = bundle.player_state.credit_units(initial, [{"card_id": FORMATION_CARD, "level": 1, "count": 2}])
    store = Store(tmp_path / "tower-battles.sqlite3", initial)
    set_rank(bundle, store, TOWER_RANK)
    clear(store, GATE_QUEST)
    return SimpleNamespace(quests=quests, towers=quests.tower, store=store, clock=clock)


def refusal(callback):
    with pytest.raises(ProtocolError) as caught:
        callback()
    return caught.value.code, caught.value.status, str(caught.value)


def support(*rows):
    return Container("AL", tuple(Column(name, "I", tuple(row[index] for row in rows))
                                 for index, name in enumerate(TEAM_COLUMNS)))


def team(store, *picks):
    player = store.profile().state["player_state"]
    prince = player["units"][0]
    avatar = player["battle"]["UX"] + 1001
    members = [(unit["CardID"], unit["GetIndex"], slot, unit["UniqueID"], 0)
               for slot, unit in enumerate((prince, *(player["units"][pick] for pick in picks or (1, 2))))]
    members.append((prince["CardID"], avatar, len(members), prince["UniqueID"], 0))
    return (*formation(*members), support((FORMATION_CARD, GUEST_INDEX, 0, GUEST_INDEX, 0)), SALLIES)


def play(battle, quest_id, stars, tick, extra=None):
    battle.quests.start(bare_start(battle.quests, battle.store, quest_id, tick), battle.store)
    finish = finish_request(battle.store, quest_id, tick=tick + 1, stars=stars,
                            extra=team(battle.store) if extra is None else extra)
    return nodes(battle.quests.finish(finish, battle.store))


def tower_result(tree):
    container = tree["TowerResult"]
    assert [(column.name, column.type.value) for column in container.children] == [
        *((name, "I") for name in INTEGER_RESULTS), *((name, "F") for name in FLOAT_RESULTS)]
    return {column.name: column.values[0] for column in container.children}


def expected(result, total):
    return {**{name: result[name] for name in INTEGER_RESULTS if name != "TotalScore"}, "TotalScore": total,
            **{name: float32(result[name]) for name in FLOAT_RESULTS}}


def mailbox(store):
    return store.profile().state["tables"]["presents"]


def unchanged(battle, callback):
    before = battle.store.profile()
    outcome = refusal(callback)
    assert battle.store.profile() == before
    return outcome


def test_tower_policy_registers_the_enabled_mode_contract():
    assert quest_modes.policy(TOWER_TYPE) is POLICY and isinstance(POLICY, TowerPolicy)
    assert (POLICY.enabled, POLICY.gate_quest, POLICY.hard_mode, POLICY.timed_conditions, POLICY.reason) == (
        True, GATE_QUEST, False, False, None)
    assert TOWER_TYPE not in ENTRUST_TYPES and POLICY.entrust_parity == frozenset()
    assert POLICY.entrust_blocker() == ("Mode tower overrides check_start, prepare_start, settle without a declared "
                                        "entrust counterpart")
    rules = quest_modes.support_status()[TOWER_TYPE]["local_rules"]
    assert rules == {"gate_quest": GATE_QUEST, "default_rules": TowerRules().to_dict(),
                     "configured_rules": "TowerService.status reports the tower rules file in force", **LOCAL_RULES}
    assert json.loads(json.dumps(rules)) == rules


def test_every_tower_floor_builds_without_prize_candidates(bundle, towers):
    catalog = bundle.quests.catalog
    floors = [quest_id for quest_id in catalog.mode_ids if catalog.quest_type(quest_id) == TOWER_TYPE]
    assert len(floors) == TOWER_FLOORS and set(floors) == set(towers.by_floor)
    for quest_id in floors:
        quest, mode = catalog.get(quest_id), catalog.mode(quest_id)
        assert quest.candidates == () and quest.treasures == (0, 0, 0, 0, 0)
        assert mode.mission_ids == (towers.floor_tower(quest_id).mission_id,) and not mode.score_mode
        assert mode.hard is None and not mode.gold_counted
    assert not set(floors) & set(catalog.status()["mode_failed_quests"])


@pytest.mark.parametrize("field,value", [
    ("Type", 0), ("Type", 14.0), ("Type", True), ("UnitList", 1), ("Treasure1", 67), ("Treasure5", 1),
    ("Treasure3", None), ("Treasure2", False),
])
def test_rows_outside_the_tower_field_contract_are_refused(bundle, field, value):
    row = dict(bundle.quests.catalog.row(HERO_FLOOR))
    assert {name: row[name] for name in ROW_VALUES} == dict(ROW_VALUES)
    row[field] = value
    code, status, message = refusal(lambda: POLICY.validate_row(MappingProxyType(row), bundle.quests.catalog))
    assert (code, status, message) == ("quest_definition_invalid", 503,
                                       f"Tower field {field} differs from the native contract")


class Resources:
    def __init__(self, catalog, *, missions=None, tables=None):
        self.catalog, self.missions, self.tables = catalog, missions, tables or {}

    def mission_windows(self, quest_type):
        if self.missions is None:
            return self.catalog.mission_windows(quest_type)
        return SimpleNamespace(missions=lambda quest_id: self.missions)

    def resource_rows(self, name):
        return self.tables.get(name, self.catalog.resource_rows(name))


def test_rows_outside_one_enabled_scored_tower_are_refused(bundle):
    catalog = bundle.quests.catalog
    row = catalog.row(HERO_FLOOR)
    tower = catalog.mission_windows(TOWER_TYPE).mission(HERO)
    unlisted = MissionWindow(HERO, tower.category, 0, None, None)
    POLICY.validate_row(row, Resources(catalog))
    POLICY.validate_row(row, Resources(catalog, missions=(tower, unlisted)))
    for resources, message in (
            (Resources(catalog, missions=()), "Tower floors must belong to exactly one enabled tower"),
            (Resources(catalog, missions=(unlisted,)), "Tower floors must belong to exactly one enabled tower"),
            (Resources(catalog, missions=(tower, tower)), "Tower floors must belong to exactly one enabled tower"),
            (Resources(catalog, tables={SCORING_TABLE: ()}), "Tower floors require their row in the shared scoring table"),
            (Resources(catalog, tables={SCORE_REWARD_TABLE.format(HERO): ()}),
             "Tower floors require their score reward group")):
        assert refusal(lambda: POLICY.validate_row(row, resources)) == ("quest_definition_invalid", 503, message)


def test_floors_open_after_the_gate_quest_and_their_tower_disclosure(battle):
    mutate(battle.store, lambda state: {**state, "tables": {**state["tables"], "quest_records": []}})
    gate = unchanged(battle, lambda: battle.quests.start(bare_start(battle.quests, battle.store, HERO_FLOOR), battle.store))
    assert gate == ("quest_locked", 409, "The mode entry requires its gate quest to be cleared")
    clear(battle.store, GATE_QUEST)
    for tick, quest_id in enumerate((GENERAL_FLOOR, SPIRIT_FLOOR), 1000):
        locked = unchanged(battle, lambda: battle.quests.start(bare_start(battle.quests, battle.store, quest_id, tick),
                                                               battle.store))
        assert locked == ("quest_locked", 409, "The tower opens once its disclosure quest is cleared")
    play(battle, HERO_FLOOR, 1, 1010)
    play(battle, GENERAL_FLOOR, 1, 1020)
    assert tower_record(battle.store.profile().state, GENERAL)["floors"]


def test_starts_without_held_or_pending_challenge_items_refuse(battle):
    battle.quests.start(bare_start(battle.quests, battle.store, HERO_FLOOR, 1000), battle.store)
    hold(battle.store, CONSUME, 0)
    short = unchanged(battle, lambda: battle.quests.start(bare_start(battle.quests, battle.store, HERO_FLOOR, 1001),
                                                          battle.store))
    assert short == ("quest_tower_consume_shortage", 409, "The tower floor needs a challenge item")
    battle.clock.moment = NOW.replace(day=NOW.day + 1)
    started = nodes(battle.quests.start(bare_start(battle.quests, battle.store, HERO_FLOOR, 1002), battle.store))
    assert possession(started["POSSESSION"]) == {CONSUME: DAILY_REFILL}


def test_starts_charge_nothing_reserve_the_settlement_and_replay_exactly(battle):
    envelope = bare_start(battle.quests, battle.store, HERO_FLOOR)
    first = battle.quests.start(envelope, battle.store)
    assert possession(nodes(first)["POSSESSION"]) == {CONSUME: DAILY_REFILL}
    reserved = battle.store.profile()
    assert held(reserved.state, CONSUME) == DAILY_REFILL
    assert reserved.state[DOMAIN]["charge"] == {"quest_id": HERO_FLOOR}
    assert tower_record(reserved.state)["stack"] == FIRST_GENERATION
    assert battle.quests.start(envelope, battle.store) == first and battle.store.profile() == reserved


def test_only_the_clear_that_settles_a_floor_charges_its_challenge_item(battle):
    tree = play(battle, HERO_FLOOR, 3, 1000)
    state = battle.store.profile().state
    assert possession(tree["POSSESSION"]) == {CONSUME: DAILY_REFILL - CHALLENGE_COST}
    assert held(state, CONSUME) == DAILY_REFILL - CHALLENGE_COST and state[DOMAIN]["charge"] is None


def test_a_restart_charges_once_on_the_clear_that_settles_it(battle):
    for tick in (1000, 1002, 1004):
        battle.quests.start(bare_start(battle.quests, battle.store, HERO_FLOOR, tick), battle.store)
        assert held(battle.store.profile().state, CONSUME) == DAILY_REFILL
    finish = finish_request(battle.store, HERO_FLOOR, tick=1006, stars=1, extra=team(battle.store))
    tree = nodes(battle.quests.finish(finish, battle.store))
    assert possession(tree["POSSESSION"]) == {CONSUME: DAILY_REFILL - CHALLENGE_COST}
    assert held(battle.store.profile().state, CONSUME) == DAILY_REFILL - CHALLENGE_COST


def test_attempts_saved_before_settlement_charges_never_charge_again(battle):
    battle.quests.start(bare_start(battle.quests, battle.store, HERO_FLOOR, 1000), battle.store)

    def charged_at_start(state):
        # An attempt saved by the earlier build paid at its start and carries no charge key or reservation.
        state["quest_state"]["active"].pop("charge")
        state[DOMAIN]["charge"] = None
        return battle.towers.possessions.debit(state, {CONSUME: CHALLENGE_COST})

    mutate(battle.store, charged_at_start)
    finish = finish_request(battle.store, HERO_FLOOR, tick=1001, stars=1, extra=team(battle.store))
    tree = nodes(battle.quests.finish(finish, battle.store))
    assert possession(tree["POSSESSION"]) == {CONSUME: DAILY_REFILL - CHALLENGE_COST}
    assert held(battle.store.profile().state, CONSUME) == DAILY_REFILL - CHALLENGE_COST


def test_a_start_keeps_the_saved_enabled_flag_of_its_challenge_item(battle):
    battle.quests.start(bare_start(battle.quests, battle.store, HERO_FLOOR, 1000), battle.store)
    mutate(battle.store, lambda state: battle.towers.possessions.set_enabled(state, {CONSUME: 1}))
    started = nodes(battle.quests.start(bare_start(battle.quests, battle.store, HERO_FLOOR, 1001), battle.store))
    assert possession(started["POSSESSION"], ENABLED) == {CONSUME: 1}
    assert possession(started["POSSESSION"]) == {CONSUME: DAILY_REFILL}


def test_a_start_cached_without_the_enabled_column_replays_its_stored_bytes(battle):
    envelope = bare_start(battle.quests, battle.store, HERO_FLOOR)
    battle.quests.start(envelope, battle.store)
    profile = battle.store.profile()
    state = deepcopy(profile.state)
    active = state["quest_state"]["active"]
    legacy = without_enabled(active["response"].encode("utf-8"))
    # Rewriting the cached start commits a revision, which the cache must name to stay replayable.
    active.update(response=legacy.decode("utf-8"), response_revision=profile.revision + 1)
    battle.store.update_state(state, profile.revision)
    cached = battle.store.profile()
    assert column_names(nodes(legacy)["POSSESSION"]) == LEGACY_COLUMNS
    assert battle.quests.start(envelope, battle.store) == legacy and battle.store.profile() == cached


def test_three_towers_score_a_floor_each_and_report_the_nine_result_columns(battle):
    hold(battle.store, CONSUME, CONSUME_STACK)
    totals = {}
    for tick, (quest_id, mission, stars, result) in enumerate((
            (HERO_FLOOR, HERO, 3, HERO_RESULT), (GENERAL_FLOOR, GENERAL, 3, GENERAL_RESULT),
            (SPIRIT_FLOOR, SPIRIT, 2, SPIRIT_RESULT)), 1000):
        tree = play(battle, quest_id, stars, 10 * tick)
        totals[mission] = result["Score"]
        assert tower_result(tree) == expected(result, totals[mission])
        record = tower_record(battle.store.profile().state, mission)
        assert record["floors"] == {str(quest_id): result["Score"]} and record["total"] == result["Score"]
    replay = play(battle, SPIRIT_FLOOR, 1, 20000)
    assert tower_result(replay)["Score"] == SPIRIT_ONE_STAR and tower_result(replay)["TotalScore"] == totals[SPIRIT]
    assert tower_record(battle.store.profile().state, SPIRIT)["floors"] == {str(SPIRIT_FLOOR): SPIRIT_RESULT["Score"]}
    second = play(battle, HERO_SECOND, 1, 20010)
    hero = tower_record(battle.store.profile().state, HERO)
    assert hero["total"] == HERO_RESULT["Score"] + tower_result(second)["Score"] == tower_result(second)["TotalScore"]


def test_only_a_result_raising_the_floor_best_saves_its_team_for_the_high_score_panel(battle):
    hold(battle.store, CONSUME, CONSUME_STACK)
    _, first, second = battle.store.profile().state["player_state"]["units"][:3]

    def saved_team():
        return tower_record(battle.store.profile().state)["teams"][str(HERO_FLOOR)]

    play(battle, HERO_FLOOR, 1, 1000)
    assert saved_team() == [snapshot(first), snapshot(second)]
    raised = play(battle, HERO_FLOOR, 3, 1010, extra=team(battle.store, 2, 1))
    assert tower_result(raised)["Score"] == HERO_RESULT["Score"] and saved_team() == [snapshot(second), snapshot(first)]
    for tick, stars in ((1020, 3), (1030, 1)):
        play(battle, HERO_FLOOR, stars, tick)
        assert saved_team() == [snapshot(second), snapshot(first)]
    response = nodes(battle.towers.floor_info(floor(battle.store, HERO_FLOOR, tick=1040), battle.store))
    assert team_of(response["HighScoreTeam"]) == wire_rows(second, first)
    assert list(response).index("HighScoreTeam") < list(response).index("HighScorePlayer")


def test_tower_starts_and_results_keep_the_explicit_empty_treasure(battle):
    started = nodes(battle.quests.start(bare_start(battle.quests, battle.store, HERO_FLOOR, 1000), battle.store))
    finishing = finish_request(battle.store, HERO_FLOOR, tick=1001, stars=3, extra=team(battle.store))
    finished = nodes(battle.quests.finish(finishing, battle.store))
    # TREASURE has no business reader in the client, so both answers keep it as an explicit empty table.
    assert "TREASURE" in started and not started["TREASURE"].children
    assert "TREASURE" in finished and not finished["TREASURE"].children


def test_score_tiers_pay_once_and_unpayable_tiers_wait_until_their_pool_opens(battle, bundle, towers, monkeypatch):
    hold(battle.store, CONSUME, CONSUME_STACK)
    hero = towers.tower(HERO)
    play(battle, HERO_FLOOR, 3, 1000)
    record = tower_record(battle.store.profile().state)
    paid = [tier.key[1] for tier in hero.score_rewards[HERO_FLOOR]]
    assert paid[-1] == DIVINE_TIER and hero.reward(SCORE_SOURCE, (HERO_FLOOR, DIVINE_TIER)).reward_type == CRYSTAL_REWARD
    assert record["score_rewards"] == {str(HERO_FLOOR): paid} and record["star_rewards"] == [1, 2, 3]
    assert record["deferred"] == []
    rows = mailbox(battle.store)
    assert [row["Description"] for row in rows] == [SCORE_TEXT] * len(paid) + [STAR_TEXT] * 3
    assert (rows[len(paid) - 1]["Type"], rows[len(paid) - 1]["Content"]) == (FREE_CRYSTAL_PRESENT, 1)
    play(battle, HERO_SECOND, 2, 1010)
    record = tower_record(battle.store.profile().state)
    assert record["star_rewards"] == [1, 2, 3, 4]
    assert record["deferred"] == [{"source": STAR_SOURCE, "key": list(TICKET_TIER), "reason": POOL_CLOSED}]
    mailed = len(mailbox(battle.store))
    play(battle, HERO_FLOOR, 3, 1020)
    assert len(mailbox(battle.store)) == mailed and tower_record(battle.store.profile().state) == record
    monkeypatch.setattr(bundle.presents.item_tickets, "validate_item", lambda item_id: None)
    tree = play(battle, HERO_THIRD, 1, 1030)
    record = tower_record(battle.store.profile().state)
    tickets = [row for row in mailbox(battle.store)[mailed:] if row.get("ItemID") == TICKET]
    assert len(tickets) == 1 and tickets[0]["Description"] == STAR_TEXT and "PPT" in tree
    # 3 + 2 + 1 stars reach total-star tier 6 in the same result that mails the waiting tier 5.
    assert record["star_rewards"] == [1, 2, 3, 4, 5, 6] and record["deferred"] == []


def test_divine_crystal_tiers_older_saves_deferred_as_unknown_mail_with_the_next_result(battle):
    hold(battle.store, CONSUME, CONSUME_STACK)
    play(battle, HERO_FLOOR, 3, 1000)
    waiting = {"source": SCORE_SOURCE, "key": [HERO_FLOOR, DIVINE_TIER], "reason": UNKNOWN_REWARD}

    def unpaid(state):
        record = state[DOMAIN]["towers"][str(HERO)]
        record["score_rewards"][str(HERO_FLOOR)].remove(DIVINE_TIER)
        record["deferred"].append(waiting)
        return state

    mutate(battle.store, unpaid)
    mailed = len(mailbox(battle.store))
    play(battle, HERO_FLOOR, 1, 1010)
    record = tower_record(battle.store.profile().state)
    assert [(row["Type"], row["Content"], row["Description"]) for row in mailbox(battle.store)[mailed:]] == [
        (FREE_CRYSTAL_PRESENT, 1, SCORE_TEXT)]
    assert DIVINE_TIER in record["score_rewards"][str(HERO_FLOOR)] and record["deferred"] == []


def test_tiers_whose_texts_the_battle_journal_cannot_hold_wait_instead_of_failing_the_result(battle, towers,
                                                                                             monkeypatch):
    hold(battle.store, CONSUME, CONSUME_STACK)
    tiers = len(towers.tower(HERO).score_rewards[HERO_FLOOR])
    oversized = MappingProxyType({source: "x" * (MAX_PRESENT_TEXT_BYTES + 1) for source in DESCRIPTIONS})
    monkeypatch.setattr(tower_catalog, "DESCRIPTIONS", oversized)
    tree = play(battle, HERO_FLOOR, 3, 1000)
    record = tower_record(battle.store.profile().state)
    assert tower_result(tree)["Score"] == HERO_RESULT["Score"] and mailbox(battle.store) == []
    assert len(record["deferred"]) == tiers + 3
    assert {entry["reason"] for entry in record["deferred"]} == {UNBOUNDED_PRESENT}
    monkeypatch.undo()
    play(battle, HERO_FLOOR, 1, 1010)
    record = tower_record(battle.store.profile().state)
    assert record["deferred"] == [] and record["star_rewards"] == [1, 2, 3]
    assert [row["Description"] for row in mailbox(battle.store)] == [SCORE_TEXT] * tiers + [STAR_TEXT] * 3


def test_any_tower_result_retries_the_deferred_tiers_of_every_started_tower(battle, bundle, monkeypatch):
    hold(battle.store, CONSUME, CONSUME_STACK)
    play(battle, HERO_FLOOR, 3, 1000)
    play(battle, HERO_SECOND, 2, 1010)
    waiting = {"source": STAR_SOURCE, "key": list(TICKET_TIER), "reason": POOL_CLOSED}
    assert waiting in tower_record(battle.store.profile().state)["deferred"]
    monkeypatch.setattr(bundle.presents.item_tickets, "validate_item", lambda item_id: None)
    mailed = len(mailbox(battle.store))
    play(battle, GENERAL_FLOOR, 1, 1020)
    hero = tower_record(battle.store.profile().state)
    tickets = [row for row in mailbox(battle.store)[mailed:] if row.get("ItemID") == TICKET]
    assert len(tickets) == 1 and tickets[0]["Description"] == STAR_TEXT
    assert TICKET_TIER[0] in hero["star_rewards"] and waiting not in hero["deferred"]


def test_a_result_settles_the_day_it_ends_on_and_reports_the_refilled_items(battle):
    battle.quests.start(bare_start(battle.quests, battle.store, HERO_FLOOR, 1000), battle.store)
    battle.clock.moment = NOW.replace(day=NOW.day + 1)
    tree = nodes(battle.quests.finish(finish_request(battle.store, HERO_FLOOR, tick=1001, stars=1,
                                                     extra=team(battle.store)), battle.store))
    state = battle.store.profile().state
    refilled = {CONSUME: 2 * DAILY_REFILL - CHALLENGE_COST}
    assert possession(tree["POSSESSION"]) == refilled == {CONSUME: held(state, CONSUME)}
    assert tower_record(state)["stack"] == 2 * FIRST_GENERATION
    assert (tower_record(state, GENERAL)["stack"], tower_record(state, SPIRIT)["stack"]) == (FIRST_GENERATION,
                                                                                            FIRST_GENERATION)


def test_a_result_cached_without_the_enabled_column_replays_its_stored_bytes(battle):
    battle.quests.start(bare_start(battle.quests, battle.store, HERO_FLOOR, 1000), battle.store)
    battle.clock.moment = NOW.replace(day=NOW.day + 1)
    finishing = finish_request(battle.store, HERO_FLOOR, tick=1001, stars=1, extra=team(battle.store))
    finished = battle.quests.finish(finishing, battle.store)
    profile = battle.store.profile()
    state = deepcopy(profile.state)
    entry = state["quest_state"]["history"][-1]
    legacy = without_enabled(entry["response"].encode("utf-8"))
    # Rewriting the cached result commits a revision, which the cache must name to stay replayable.
    entry.update(response=legacy.decode("utf-8"), response_revision=profile.revision + 1)
    battle.store.update_state(state, profile.revision)
    cached = battle.store.profile()
    assert column_names(nodes(finished)["POSSESSION"]) == (*LEGACY_COLUMNS, ENABLED)
    assert column_names(nodes(legacy)["POSSESSION"]) == LEGACY_COLUMNS
    assert battle.quests.finish(finishing, battle.store) == legacy and battle.store.profile() == cached


def test_spirit_tower_team_challenges_count_through_their_derived_specs(derived, bundle, towers, tmp_path):
    specs = derived.missions.progress.clear_specs
    claimed = [specs.get(key) for key in sorted(specs.ids) if specs.get(key).claimed_by == POLICY.name]
    assert len(claimed) == TOWER_CHALLENGES and all(
        spec.quest_types == (TOWER_TYPE,) and len(spec.quest_ids) == 1 and spec.team.mode == "only"
        and towers.floor_tower(next(iter(spec.quest_ids))).mission_id == SPIRIT for spec in claimed)
    spec = specs.get(SPIRIT_CHALLENGE)
    assert spec.quest_ids == {SPIRIT_CHALLENGE_FLOOR} and (spec.team.field, spec.team.value) == ("Genus", SPIRIT_GENUS)
    assert GENUS_CARD in spec.team.card_ids and FORMATION_CARD not in spec.team.card_ids
    initial = derived.missions.initialize(bundle.player_state.initialize(derived.bootstrap.initial_state()))
    initial, units = bundle.player_state.credit_units(initial, [{"card_id": GENUS_CARD, "level": 1, "count": 2},
                                                                {"card_id": FORMATION_CARD, "level": 1, "count": 1}])
    store = Store(tmp_path / "tower-challenges.sqlite3", initial)
    set_rank(bundle, store, TOWER_RANK)
    clear(store, GATE_QUEST, HERO_FLOOR, SPIRIT_FLOOR)
    hold(store, CONSUME, CONSUME_STACK)
    quests = derived.quests
    quests.tower = TowerService(bundle.bootstrap, bundle.player_state, bundle.possessions, bundle.presents, towers,
                                rules=TowerRules(), clock=lambda: MISSION_NOW)
    rows = [(unit["CardID"], unit["GetIndex"], slot, unit["UniqueID"], 0) for slot, unit in enumerate(units, 1)]

    def progress():
        snapshot = derived.missions.progress.snapshot(store.profile().state, MISSION_NOW)
        return next(row["Progress"] for row in snapshot if row["ID"] == SPIRIT_CHALLENGE)

    for tick, members, counted in ((1000, rows, 0), (1002, rows[:2], 1)):
        quests.start(bare_start(quests, store, SPIRIT_CHALLENGE_FLOOR, tick), store)
        quests.finish(finish_request(store, SPIRIT_CHALLENGE_FLOOR, tick=tick + 1, extra=formation(*members)), store)
        assert progress() == counted


@pytest.mark.parametrize("result", [0, -1], ids=["defeat", "retreat"])
def test_failed_or_abandoned_tower_battles_charge_nothing(battle, result):
    battle.quests.start(bare_start(battle.quests, battle.store, HERO_FLOOR), battle.store)
    battle.quests.exit(exit_request(battle.store, HERO_FLOOR, result), battle.store)
    state = battle.store.profile().state
    assert held(state, CONSUME) == DAILY_REFILL and state["quest_state"]["active"] is None
    assert tower_record(state)["floors"] == {}
    retried = play(battle, HERO_FLOOR, 1, 2000)
    assert possession(retried["POSSESSION"]) == {CONSUME: DAILY_REFILL - CHALLENGE_COST}
    assert held(battle.store.profile().state, CONSUME) == DAILY_REFILL - CHALLENGE_COST


def test_four_star_tower_starts_are_refused_without_charging(battle):
    code, status, _ = unchanged(battle, lambda: battle.quests.start(hard_start(battle.quests, battle.store, HERO_FLOOR),
                                                                    battle.store))
    assert (code, status) == HARD_REFUSAL


@pytest.mark.parametrize("missing", ["tower", "presents"])
def test_tower_battles_need_the_tower_and_present_services(battle, missing):
    setattr(battle.quests, missing, None)
    starting = bare_start(battle.quests, battle.store, HERO_FLOOR)
    assert unchanged(battle, lambda: battle.quests.start(starting, battle.store)) == (
        "quest_tower_unavailable", 409, "Tower battles require the tower and present services")


def test_tower_battles_refuse_a_tower_service_over_another_quest_catalog(battle):
    battle.quests.tower = SimpleNamespace(catalog=SimpleNamespace(quests=object()))
    starting = bare_start(battle.quests, battle.store, HERO_FLOOR)
    assert unchanged(battle, lambda: battle.quests.start(starting, battle.store)) == (
        "quest_tower_catalog_mismatch", 503, "The tower service reads another quest catalog")


def test_a_tower_result_keeps_its_attempt_when_the_sorties_cannot_be_scored(battle):
    battle.quests.start(bare_start(battle.quests, battle.store, HERO_FLOOR, 1000), battle.store)
    before = battle.store.profile()
    negative = sally_container(((0, 10, -1, 0), (False, False, False, False)))
    finishing = finish_request(battle.store, HERO_FLOOR, tick=1001, stars=1, extra=(negative,))
    code, status, _ = refusal(lambda: battle.quests.finish(finishing, battle.store))
    assert (code, status) == ("tower_score_invalid", 400) and battle.store.profile() == before
    assert before.state["quest_state"]["active"]["quest_id"] == HERO_FLOOR
