import ast
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import aigis_ps
from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.app import create_app
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.present_backlog import EMPTY_MAILBOX, MAX_PRESENT_TEXT_BYTES, UNBOUNDED_PRESENT
from aigis_ps.quest_modes import CLEAR_FLAG
from aigis_ps.quest_protocol import TeamMember
from aigis_ps.request_ledger import HISTORY_LIMIT
from aigis_ps.storage import Store
from aigis_ps.tower_catalog import GATE_QUEST, SCORE_SOURCE, STAR_SOURCE, TowerCatalog
from aigis_ps.tower_service import (
    CRYSTAL_CLAIM, FLOOR_INFO, LOCAL_RULES, OPERATIONS, PORTAL_QUEST, TOWER_INFO, TowerService,
)
from aigis_ps.tower_state import (
    CHALLENGE_COST, DOMAIN, LEGACY_SCHEMA_VERSION, SCHEMA_VERSION, TEAM_FIELDS, TowerRules, read_state, record_floor,
    reserve_charge, rewards_due, saved_records, team_rows,
)
from test_evolution_integration import post
from test_quest_battle_modes import set_rank
from test_quest_service import bundle, request


HERO, GENERAL, SPIRIT = 110001, 110002, 110003
HERO_FLOOR, HERO_SECOND, HERO_THIRD, GENERAL_FLOOR, SPIRIT_FLOOR = 10001, 10002, 10003, 11001, 12001
CONSUME = "tower_consume_item_110001"
CRYSTALS = {HERO: "tower_crystal_110001", GENERAL: "tower_crystal_110002", SPIRIT: "tower_crystal_110003"}
HERO_CRYSTAL = CRYSTALS[HERO]
CONSUME_STACK, CRYSTAL_STACK = 30, 999999
DAILY_REFILL, FIRST_GENERATION, FIRST_LIMIT = 3, 250, 1500
TOWER_RANK = 70
NOW = datetime(2026, 11, 2, 12, tzinfo=JST)
DAY = timedelta(days=1)
POOL_CLOSED, DETACHED = "summon_ticket_pool_disabled", "present_item_unsupported"
# The 英傑 total-star tier 5 mails item_smn_ticket_18003, whose pool is not enabled.
TICKET_TIER, TICKET = (5,), "item_smn_ticket_18003"
GIFT_TIER = (2,)
LEGACY_COLUMNS, ENABLED = ("ItemID", "Count"), "Enabled"
POSSESSION_COLUMNS = (*LEGACY_COLUMNS, ENABLED)
TEAM_COLUMNS = tuple(alias for _, alias in TEAM_FIELDS)
TEAM_CARD = 67
# The 英傑 floor 1 tier 6 mails 300 tower crystals.
TOWER_CRYSTAL_TIER = (HERO_FLOOR, 6)
SOURCE = Path(aigis_ps.__file__).resolve().parent


class Clock:
    def __init__(self, moment):
        self.moment = moment

    def __call__(self):
        return self.moment


class Conflicts:
    def __init__(self, store, failures):
        self.store, self.failures = store, failures

    def profile(self):
        return self.store.profile()

    def update_state(self, state, revision):
        if self.failures:
            self.failures -= 1
            raise ValueError("Profile revision changed; reload before updating")
        return self.store.update_state(state, revision)


@pytest.fixture(scope="module")
def towers(bundle):
    return TowerCatalog(bundle.quests.catalog, bundle.possessions)


def tower_service(bundle, towers, clock, **rules):
    return TowerService(bundle.bootstrap, bundle.player_state, bundle.possessions, bundle.presents, towers,
                        rules=TowerRules(**rules), clock=clock)


@pytest.fixture
def world(bundle, towers, tmp_path):
    clock = Clock(NOW)
    store = Store(tmp_path / "towers.sqlite3", bundle.player_state.initialize(bundle.bootstrap.initial_state()))
    return SimpleNamespace(towers=tower_service(bundle, towers, clock), store=store, clock=clock)


def floor(store, quest_id, tick=1000):
    return request(store, FLOOR_INFO, (Column("QUEST", "I", (quest_id,)),), tick=tick)


def listing(store, tick=1000):
    return request(store, TOWER_INFO, (), tick=tick)


def claiming(store, *missions, tick=1000):
    return request(store, CRYSTAL_CLAIM, (Column("MISSION", "I", missions),), tick=tick)


def tree(xml):
    return {child.name: child for child in loads(xml).children}


def rows(container):
    return dict(zip(container.children[0].values, container.children[1].values))


def columns(container):
    return {column.name: column.values[0] for column in container.children}


def column_names(container):
    return tuple(column.name for column in container.children)


def possession(container, field="Count"):
    assert column_names(container) == POSSESSION_COLUMNS
    return dict(zip(container.children[0].values, container.children[POSSESSION_COLUMNS.index(field)].values))


def team_of(container):
    assert column_names(container) == TEAM_COLUMNS
    return [dict(zip(TEAM_COLUMNS, row)) for row in zip(*(column.values for column in container.children))]


def wire_rows(*units):
    return [{alias: unit[name] for name, alias in TEAM_FIELDS} for unit in units]


def without_enabled(response):
    return dumps(Container(children=tuple(
        Container(node.name, tuple(column for column in node.children if column.name != ENABLED))
        if node.name == "POSSESSION" else node for node in loads(response).children)))


def mutate(store, change):
    profile = store.profile()
    store.update_state(change(deepcopy(profile.state)), profile.revision)


def clear(store, *quest_ids, stars=3):
    def change(state):
        kept = [row for row in state["tables"]["quest_records"] if row["QuestID"] not in quest_ids]
        state["tables"]["quest_records"] = kept + [
            {"QuestID": quest_id, "MaxRank": stars, "AchieveFlag": CLEAR_FLAG, "RemainAssault": 0,
             "FirstDefeatDate": 0, "Shown": False} for quest_id in quest_ids]
        return state
    mutate(store, change)


def hold(store, item_id, count):
    def change(state):
        kept = [row for row in state["tables"]["possessions"] if row["ItemID"] != item_id]
        state["tables"]["possessions"] = kept + [{"ItemID": item_id, "Count": count}]
        return state
    mutate(store, change)


def held(state, item_id):
    return next((row["Count"] for row in state["tables"]["possessions"] if row["ItemID"] == item_id), 0)


def tower_record(state, mission=HERO):
    return state[DOMAIN]["towers"][str(mission)]


def refused(callback, code, status):
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert (caught.value.code, caught.value.status) == (code, status)


def accept(reward):
    return None


def opened(world):
    clear(world.store, GATE_QUEST)
    world.towers.towers_info(listing(world.store), world.store)
    return world.store.profile()


def portal(world, tick=1000):
    response = tree(world.towers.floor_info(floor(world.store, PORTAL_QUEST, tick), world.store))
    return rows(response["StackCrystal"])[HERO], possession(response["POSSESSION"])[CONSUME]


def test_handlers_serve_the_three_registered_tower_operations(world):
    handlers = world.towers.handlers()
    assert tuple(handlers) == OPERATIONS == (FLOOR_INFO, TOWER_INFO, CRYSTAL_CLAIM) == (66, 67, 68)
    assert handlers[FLOOR_INFO] == world.towers.floor_info and handlers[CRYSTAL_CLAIM] == world.towers.claim


def test_the_portal_query_before_the_gate_answers_empty_towers_without_writing(world):
    before = world.store.profile()
    response = tree(world.towers.floor_info(floor(world.store, PORTAL_QUEST), world.store))
    assert list(response) == ["SYSTEM", "PersonalRecord", "StackCrystal", "POSSESSION"]
    assert columns(response["PersonalRecord"]) == {"Score": 0, "TotalScore": 0}
    assert rows(response["StackCrystal"]) == {HERO: 0, GENERAL: 0, SPIRIT: 0}
    assert possession(response["POSSESSION"]) == {CONSUME: 0, **{crystal: 0 for crystal in CRYSTALS.values()}}
    assert world.store.profile() == before and DOMAIN not in before.state


def test_the_first_portal_visit_after_the_gate_settles_one_day_once(world):
    clear(world.store, GATE_QUEST)
    before, envelope = world.store.profile(), floor(world.store, PORTAL_QUEST)
    first = world.towers.floor_info(envelope, world.store)
    response, after = tree(first), world.store.profile()
    assert rows(response["StackCrystal"]) == {HERO: FIRST_GENERATION, GENERAL: 0, SPIRIT: 0}
    assert possession(response["POSSESSION"])[CONSUME] == DAILY_REFILL == held(after.state, CONSUME)
    assert after.revision == before.revision + 1 and list(after.state[DOMAIN]["towers"]) == [str(HERO)]
    assert world.towers.floor_info(envelope, world.store) == first and world.store.profile() == after


def test_floor_queries_report_the_floor_best_the_tower_total_and_the_own_high_score(world, bundle, towers):
    set_rank(bundle, world.store, TOWER_RANK)
    opened(world)
    best, second = 40000, 5000
    mutate(world.store, lambda state: world.towers.set_floor_best(
        world.towers.set_floor_best(state, HERO_FLOOR, best), HERO_SECOND, second))
    experience = world.store.profile().state["tables"]["game_param"][0]["Experience"]
    response = tree(world.towers.floor_info(floor(world.store, HERO_FLOOR), world.store))
    assert list(response) == ["SYSTEM", "PersonalRecord", "HighScorePlayer", "StackCrystal", "POSSESSION"]
    assert columns(response["PersonalRecord"]) == {"Score": best, "TotalScore": best + second}
    assert experience > 0 and columns(response["HighScorePlayer"]) == {
        "Score": best, "Exp": experience, "Title": 0, "Blessing": 0}
    assert rows(response["StackCrystal"]) == {HERO: FIRST_GENERATION}
    assert possession(response["POSSESSION"]) == {CONSUME: DAILY_REFILL, HERO_CRYSTAL: 0}
    unplayed = tree(world.towers.floor_info(floor(world.store, HERO_THIRD), world.store))
    assert "HighScorePlayer" not in unplayed
    assert columns(unplayed["PersonalRecord"]) == {"Score": 0, "TotalScore": best + second}
    omitted = tree(tower_service(bundle, towers, world.clock, high_score="omit").floor_info(
        floor(world.store, HERO_FLOOR), world.store))
    assert list(omitted) == ["SYSTEM", "PersonalRecord", "StackCrystal", "POSSESSION"]


def test_floor_queries_answer_the_team_saved_with_the_floor_best(world, bundle, towers):
    opened(world)
    best = 40000

    def raise_bests(state):
        state, units = bundle.player_state.credit_units(state, [{"card_id": TEAM_CARD, "level": 1, "count": 2}])
        members = [TeamMember(unit["CardID"], unit["GetIndex"], slot, unit["UniqueID"], 0)
                   for slot, unit in enumerate(reversed(units), 1)]
        team = team_rows(state["player_state"]["units"], members)
        state = record_floor(state, towers, HERO, HERO_FLOOR, best, team=team)[0]
        return record_floor(state, towers, HERO, HERO_SECOND, best, team=[])[0]

    mutate(world.store, raise_bests)
    state = world.store.profile().state
    first, second = state["player_state"]["units"][-2:]
    response = tree(world.towers.floor_info(floor(world.store, HERO_FLOOR), world.store))
    assert list(response) == ["SYSTEM", "PersonalRecord", "HighScoreTeam", "HighScorePlayer", "StackCrystal",
                              "POSSESSION"]
    assert team_of(response["HighScoreTeam"]) == wire_rows(second, first)
    assert columns(response["HighScorePlayer"])["Score"] == best
    alone = tree(world.towers.floor_info(floor(world.store, HERO_SECOND), world.store))
    assert team_of(alone["HighScoreTeam"]) == [] and columns(alone["HighScorePlayer"])["Score"] == best
    omitted = tree(tower_service(bundle, towers, world.clock, high_score="omit").floor_info(
        floor(world.store, HERO_FLOOR), world.store))
    assert list(omitted) == ["SYSTEM", "PersonalRecord", "StackCrystal", "POSSESSION"]
    saved = tower_record(state)["teams"]
    assert list(saved) == [str(HERO_FLOOR), str(HERO_SECOND)] and saved[str(HERO_SECOND)] == []
    assert tower_record(world.towers.set_floor_best(state, HERO_FLOOR, best))["teams"] == saved
    for score in (best + 1, best - 1, 0):
        assert tower_record(world.towers.set_floor_best(state, HERO_FLOOR, score))["teams"] == {str(HERO_SECOND): []}


@pytest.mark.parametrize("quest_id", [GATE_QUEST, -1, 99999])
def test_floor_queries_refuse_quests_that_are_neither_the_portal_nor_a_floor(world, quest_id):
    before = world.store.profile()
    refused(lambda: world.towers.floor_info(floor(world.store, quest_id), world.store), "tower_request_invalid", 400)
    assert world.store.profile() == before


def test_undisclosed_towers_read_and_claim_as_empty(world):
    clear(world.store, GATE_QUEST)
    response = tree(world.towers.floor_info(floor(world.store, GENERAL_FLOOR), world.store))
    assert columns(response["PersonalRecord"]) == {"Score": 0, "TotalScore": 0}
    assert rows(response["StackCrystal"]) == {GENERAL: 0}
    assert possession(response["POSSESSION"]) == {CONSUME: DAILY_REFILL, CRYSTALS[GENERAL]: 0}
    saved = world.store.profile()
    assert list(saved.state[DOMAIN]["towers"]) == [str(HERO)]
    claimed = tree(world.towers.claim(claiming(world.store, GENERAL, SPIRIT), world.store))
    assert possession(claimed["POSSESSION"]) == {CRYSTALS[GENERAL]: 0, CRYSTALS[SPIRIT]: 0}
    assert rows(claimed["StackCrystal"]) == {GENERAL: 0, SPIRIT: 0}
    assert world.store.profile() == saved


def test_claims_credit_up_to_the_stack_keep_the_rest_and_replay_exactly(world, bundle, towers):
    opened(world)
    room = 100
    hold(world.store, HERO_CRYSTAL, CRYSTAL_STACK - room)
    before, envelope = world.store.profile(), claiming(world.store, HERO, GENERAL)
    wire = world.towers.claim(envelope, world.store)
    response, after = tree(wire), world.store.profile()
    assert list(response) == ["SYSTEM", "POSSESSION", "StackCrystal"]
    assert possession(response["POSSESSION"]) == {HERO_CRYSTAL: CRYSTAL_STACK, CRYSTALS[GENERAL]: 0}
    assert rows(response["StackCrystal"]) == {HERO: FIRST_GENERATION - room, GENERAL: 0}
    assert after.revision == before.revision + 1 and held(after.state, HERO_CRYSTAL) == CRYSTAL_STACK
    receipts = after.state[DOMAIN]["receipts"]["receipts"]
    assert len(receipts) == 1 and receipts[0]["revision"] == after.revision
    assert world.towers.claim(envelope, world.store) == wire and world.store.profile() == after
    reconfigured = tower_service(bundle, towers, world.clock, high_score="omit")
    refused(lambda: reconfigured.claim(envelope, world.store), "tower_retry_stale", 409)
    full = tree(world.towers.claim(claiming(world.store, HERO, tick=1001), world.store))
    assert rows(full["StackCrystal"]) == {HERO: FIRST_GENERATION - room} and world.store.profile() == after
    clear(world.store, HERO_FLOOR)
    refused(lambda: world.towers.claim(envelope, world.store), "tower_retry_stale", 409)


def receipted(store, **changes):
    profile = store.profile()
    state = deepcopy(profile.state)
    # Rewriting the receipt commits a revision, which the receipt must name to stay replayable.
    state[DOMAIN]["receipts"]["receipts"][0].update(revision=profile.revision + 1, **changes)
    store.update_state(state, profile.revision)
    return store.profile()


def test_claim_receipts_saved_without_the_enabled_column_replay_their_stored_bytes(world):
    opened(world)
    envelope = claiming(world.store, HERO)
    legacy = without_enabled(world.towers.claim(envelope, world.store))
    saved = receipted(world.store, response=legacy.decode("utf-8"))
    assert column_names(tree(legacy)["POSSESSION"]) == LEGACY_COLUMNS
    assert world.towers.initialize(saved.state) is saved.state
    assert world.towers.claim(envelope, world.store) == legacy and world.store.profile() == saved


def test_claim_receipts_saved_before_crystal_tiers_paid_replay_their_stored_bytes(world, towers):
    opened(world)
    envelope = claiming(world.store, HERO)
    wire = world.towers.claim(envelope, world.store)
    legacy = world.towers.legacy_content_version
    content = {"schema": 1, "catalog": towers.legacy_content_version, "rules": TowerRules().content_version}
    assert legacy == hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert legacy != world.towers.content_version
    saved = receipted(world.store, content=legacy)
    assert world.towers.initialize(saved.state) is saved.state
    assert world.towers.claim(envelope, world.store) == wire and world.store.profile() == saved
    receipted(world.store, content="0" * 64)
    refused(lambda: world.towers.claim(envelope, world.store), "tower_retry_stale", 409)


def test_schema_1_tower_saves_serve_as_holding_no_teams_and_upgrade_on_their_next_write(world):
    opened(world)
    best = 40000
    profile = world.store.profile()
    state = world.towers.set_floor_best(profile.state, HERO_FLOOR, best)
    state[DOMAIN]["schema_version"] = LEGACY_SCHEMA_VERSION
    del state[DOMAIN]["charge"]
    for record in state[DOMAIN]["towers"].values():
        record.pop("teams")
    world.store.update_state(state, profile.revision)
    legacy = world.store.profile()
    assert world.towers.initialize(legacy.state) is legacy.state
    response = tree(world.towers.floor_info(floor(world.store, HERO_FLOOR), world.store))
    assert list(response) == ["SYSTEM", "PersonalRecord", "HighScorePlayer", "StackCrystal", "POSSESSION"]
    assert columns(response["HighScorePlayer"])["Score"] == best and world.store.profile() == legacy
    world.towers.claim(claiming(world.store, HERO), world.store)
    upgraded = world.store.profile().state
    assert (upgraded[DOMAIN]["schema_version"], tower_record(upgraded)["teams"]) == (SCHEMA_VERSION, {})


def test_claim_receipts_saved_under_the_earlier_rule_defaults_replay_their_stored_bytes(world, bundle, towers):
    opened(world)
    envelope = claiming(world.store, HERO)
    old_defaults = tower_service(bundle, towers, world.clock, generation_hour=0, member_count_prince=True)
    wire = old_defaults.claim(envelope, world.store)
    saved = world.store.profile()
    receipt = saved.state[DOMAIN]["receipts"]["receipts"][0]
    assert receipt["content"] == old_defaults.content_version != world.towers.content_version
    assert world.towers.claim(envelope, world.store) == wire and world.store.profile() == saved
    # A file that spelled out only one of the earlier defaults versions its receipts by that mixture.
    for changes in ({"generation_hour": 0}, {"member_count_prince": True}):
        mixed = tower_service(bundle, towers, world.clock, **changes)
        assert mixed.content_version in world.towers.replayable_versions
    assert tower_service(bundle, towers, world.clock, replenish_hour=5).content_version not in \
        world.towers.replayable_versions


def test_settlement_charges_follow_the_start_reservation(world, towers):
    state = opened(world).state
    hero = towers.tower(HERO)
    reserved = reserve_charge(state, towers, HERO_FLOOR)
    charged, count = world.towers.charge_challenge(reserved, hero, HERO_FLOOR)
    assert count == CHALLENGE_COST and held(charged, CONSUME) == DAILY_REFILL - CHALLENGE_COST
    assert read_state(charged, towers)["charge"] is None
    again, count = world.towers.charge_challenge(charged, hero, HERO_FLOOR)
    assert again is charged and count == 0
    unreserved, count = world.towers.charge_challenge(state, hero, HERO_FLOOR)
    assert unreserved is state and count == 0 and held(state, CONSUME) == DAILY_REFILL


def test_a_short_settlement_charge_stops_at_zero_and_warns(world, towers, caplog):
    state = opened(world).state
    reserved = reserve_charge(state, towers, HERO_FLOOR)
    reserved["tables"]["possessions"] = [row for row in reserved["tables"]["possessions"] if row["ItemID"] != CONSUME]
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        charged, count = world.towers.charge_challenge(reserved, towers.tower(HERO), HERO_FLOOR)
    assert count == 0 and held(charged, CONSUME) == 0
    assert read_state(charged, towers)["charge"] is None
    assert any("challenge items short" in record.getMessage() for record in caplog.records)


def test_claims_whose_receipts_were_evicted_stay_processed(world):
    opened(world)
    first = claiming(world.store, HERO)
    world.towers.claim(first, world.store)
    for day in range(1, HISTORY_LIMIT + 1):
        world.clock.moment = NOW + day * DAY
        world.towers.claim(claiming(world.store, HERO, tick=1000 + day), world.store)
    before = world.store.profile()
    ledger = before.state[DOMAIN]["receipts"]
    assert (len(ledger["receipts"]), len(ledger["processed_keys"])) == (HISTORY_LIMIT, HISTORY_LIMIT + 1)
    refused(lambda: world.towers.claim(first, world.store), "tower_request_stale", 409)
    assert world.store.profile() == before


def test_a_claim_after_the_day_turns_also_reports_the_refilled_challenge_item(world):
    opened(world)
    world.clock.moment = NOW + DAY
    response = tree(world.towers.claim(claiming(world.store, GENERAL, HERO), world.store))
    assert possession(response["POSSESSION"]) == {CRYSTALS[GENERAL]: 0, HERO_CRYSTAL: 2 * FIRST_GENERATION,
                                                  CONSUME: 2 * DAILY_REFILL}
    assert rows(response["StackCrystal"]) == {GENERAL: 0, HERO: 0}
    saved = world.store.profile().state
    assert held(saved, CONSUME) == 2 * DAILY_REFILL and tower_record(saved)["stack"] == 0


def test_tower_answers_keep_the_saved_enabled_flag_of_a_refilled_challenge_item(world):
    opened(world)
    mutate(world.store, lambda state: world.towers.possessions.set_enabled(state, {CONSUME: 1}))
    world.clock.moment = NOW + DAY
    entered = tree(world.towers.floor_info(floor(world.store, PORTAL_QUEST), world.store))
    assert possession(entered["POSSESSION"], ENABLED) == {CONSUME: 1, **dict.fromkeys(CRYSTALS.values(), 0)}
    assert possession(entered["POSSESSION"])[CONSUME] == 2 * DAILY_REFILL
    world.clock.moment = NOW + 2 * DAY
    claimed = tree(world.towers.claim(claiming(world.store, HERO), world.store))
    assert possession(claimed["POSSESSION"], ENABLED) == {HERO_CRYSTAL: 0, CONSUME: 1}
    assert possession(claimed["POSSESSION"])[CONSUME] == 3 * DAILY_REFILL


@pytest.mark.parametrize("missions,code", [
    ((HERO, HERO), "tower_request_invalid"), ((999999,), "tower_request_invalid"),
    ((HERO, GENERAL, SPIRIT, HERO), "invalid_request_fields"), ((), "invalid_request_fields"),
], ids=["repeated", "unknown", "more-than-every-tower", "empty"])
def test_claims_refuse_repeated_unknown_or_surplus_missions(world, missions, code):
    opened(world)
    before = world.store.profile()
    refused(lambda: world.towers.claim(claiming(world.store, *missions), world.store), code, 400)
    assert world.store.profile() == before


def test_days_settle_lazily_at_the_official_four_oclock(world):
    opened(world)
    midnight = datetime(2026, 11, 3, tzinfo=JST)
    for moment, expected in ((midnight - timedelta(seconds=1), (FIRST_GENERATION, DAILY_REFILL)),
                             (midnight + timedelta(minutes=30), (FIRST_GENERATION, DAILY_REFILL)),
                             (midnight + timedelta(hours=4), (2 * FIRST_GENERATION, 2 * DAILY_REFILL)),
                             (midnight + 10 * DAY, (FIRST_LIMIT, CONSUME_STACK))):
        world.clock.moment = moment
        assert portal(world) == expected


def test_a_configured_generation_hour_settles_on_its_own_clock(world, bundle, towers):
    clear(world.store, GATE_QUEST)
    split = tower_service(bundle, towers, world.clock, generation_hour=0)
    split.floor_info(floor(world.store, PORTAL_QUEST), world.store)
    midnight = datetime(2026, 11, 3, tzinfo=JST)
    for tick, (moment, expected) in enumerate((
            (midnight + timedelta(minutes=30), (2 * FIRST_GENERATION, DAILY_REFILL)),
            (midnight + timedelta(hours=4), (2 * FIRST_GENERATION, 2 * DAILY_REFILL))), 1001):
        world.clock.moment = moment
        response = tree(split.floor_info(floor(world.store, PORTAL_QUEST, tick=tick), world.store))
        assert (rows(response["StackCrystal"])[HERO], possession(response["POSSESSION"])[CONSUME]) == expected


def test_first_access_can_wait_for_the_first_day(world, bundle, towers):
    lazy = tower_service(bundle, towers, world.clock, first_access_settles_day=False)
    clear(world.store, GATE_QUEST)
    before = world.store.profile()
    response = tree(lazy.floor_info(floor(world.store, PORTAL_QUEST), world.store))
    assert (rows(response["StackCrystal"])[HERO], possession(response["POSSESSION"])[CONSUME]) == (0, 0)
    after = world.store.profile()
    assert after.revision == before.revision + 1 and tower_record(after.state)["stack"] == 0
    world.clock.moment = NOW + DAY
    response = tree(lazy.floor_info(floor(world.store, PORTAL_QUEST), world.store))
    assert (rows(response["StackCrystal"])[HERO], possession(response["POSSESSION"])[CONSUME]) == (FIRST_GENERATION,
                                                                                                   DAILY_REFILL)


def test_towers_info_answers_the_system_alone_and_settles(world):
    clear(world.store, GATE_QUEST)
    assert list(tree(world.towers.towers_info(listing(world.store), world.store))) == ["SYSTEM"]
    saved = world.store.profile().state
    assert tower_record(saved)["stack"] == FIRST_GENERATION and held(saved, CONSUME) == DAILY_REFILL


@pytest.mark.parametrize("operation", ["floor_info", "claim"])
def test_conflicting_commits_retry_then_report_the_changed_profile(world, operation):
    clear(world.store, GATE_QUEST)
    envelope = floor(world.store, PORTAL_QUEST) if operation == "floor_info" else claiming(world.store, HERO)
    before = world.store.profile()
    refused(lambda: getattr(world.towers, operation)(envelope, Conflicts(world.store, 3)), "profile_changed", 409)
    assert world.store.profile() == before
    response = tree(getattr(world.towers, operation)(envelope, Conflicts(world.store, 2)))
    assert rows(response["StackCrystal"])[HERO] == (FIRST_GENERATION if operation == "floor_info" else 0)
    assert world.store.profile().revision == before.revision + 1


def test_delivery_is_judged_live_against_the_attached_ticket_service(world, bundle, towers, monkeypatch):
    hero = towers.tower(HERO)
    ticket, gift = hero.reward(STAR_SOURCE, TICKET_TIER).present(), hero.reward(STAR_SOURCE, GIFT_TIER).present()
    assert ticket["item_id"] == TICKET and bundle.presents.item_tickets is not None
    assert (world.towers.deliverable(ticket), world.towers.deliverable(gift)) == (POOL_CLOSED, None)
    monkeypatch.setattr(bundle.presents, "item_tickets", None)
    assert (world.towers.deliverable(ticket), world.towers.deliverable(gift)) == (DETACHED, None)
    assert world.towers.status()["item_tickets_attached"] is False


def test_delivery_refuses_texts_the_battle_journal_cannot_hold_before_the_mailbox_preview(world, bundle, towers):
    reward = towers.tower(HERO).reward(SCORE_SOURCE, TOWER_CRYSTAL_TIER).present()
    oversized = {**reward, "description": "x" * (MAX_PRESENT_TEXT_BYTES + 1)}
    assert len(bundle.presents.preview([oversized], EMPTY_MAILBOX)) == 1
    assert (world.towers.deliverable(reward), world.towers.deliverable(oversized)) == (None, UNBOUNDED_PRESENT)
    assert world.towers.deliverable({**reward, "kind": "free_crystal"}) == UNBOUNDED_PRESENT


def test_initialize_checks_the_saved_tower_state(world):
    initial = world.store.profile().state
    assert world.towers.initialize(initial) is initial
    opened(world)
    world.towers.claim(claiming(world.store, HERO), world.store)
    saved = world.store.profile().state
    assert world.towers.initialize(saved) is saved
    broken = deepcopy(saved)
    tower_record(broken)["stack"] = -1
    refused(lambda: world.towers.initialize(broken), "tower_state_invalid", 503)
    forged = deepcopy(saved)
    forged[DOMAIN]["receipts"]["receipts"][0]["response"] = "<DA><SYSTEM>"
    refused(lambda: world.towers.initialize(forged), "tower_state_invalid", 503)


def test_no_server_module_names_the_116_tower_high_score_notice():
    # 116 keeps omitting TowerHighScore, so 70A990 goes straight to OnNotifiedHighScorePrize.
    named = [path.name for path in SOURCE.glob("*.py") if any(
        isinstance(node, ast.Constant) and node.value == "TowerHighScore"
        for node in ast.walk(ast.parse(path.read_bytes(), str(path))))]
    assert named == []


def test_status_and_describe_report_the_live_tower_view(world, bundle, towers):
    status = world.towers.status()
    assert status["ready"] and status["implemented_operations"] == [66, 67, 68]
    assert status["rules"] == TowerRules().to_dict() and status["local_rules"] == dict(LOCAL_RULES)
    assert status["catalog"]["content_version"] == towers.content_version and json.dumps(status)
    assert status["legacy_content_version"] == world.towers.legacy_content_version != status["content_version"]
    assert set(status["replayable_content_versions"]) == world.towers.replayable_versions
    assert {status["content_version"], status["legacy_content_version"]} <= world.towers.replayable_versions
    assert tower_service(bundle, towers, world.clock, high_score="omit").content_version != status["content_version"]
    opened(world)
    view = world.towers.describe(world.store.profile().state, NOW)
    assert view["receipt_content_version"] == status["content_version"] and view["started"]
    assert view["charge"] is None
    assert [(entry["mission_id"], entry["started"]) for entry in view["towers"]] == [
        (HERO, True), (GENERAL, False), (SPIRIT, False)]
    assert view["towers"][0]["stack"] == FIRST_GENERATION and json.dumps(view)


def test_service_construction_requires_shared_dependencies(world, bundle, towers):
    def build(**changes):
        arguments = {"bootstrap": bundle.bootstrap, "players": bundle.player_state, "possessions": bundle.possessions,
                     "presents": bundle.presents, "catalog": towers, "rules": TowerRules(), "clock": world.clock}
        arguments.update(changes)
        return TowerService(**arguments)

    refused(lambda: build(possessions=SimpleNamespace()), "tower_dependencies_invalid", 503)
    refused(lambda: build(presents=None), "tower_dependencies_invalid", 503)
    refused(lambda: build(catalog=SimpleNamespace(possessions=bundle.possessions)), "tower_dependencies_invalid", 503)
    refused(lambda: build(rules={}), "tower_rules_invalid", 503)
    with pytest.raises(ValueError):
        build(clock=NOW)
    assert build(clock=None).clock is bundle.bootstrap.clock


def test_floor_bests_set_by_hand_recompute_the_total_and_never_pay_their_tiers(world, towers):
    state = opened(world).state
    best = 25000
    changed = world.towers.set_floor_best(state, HERO_FLOOR, best)
    record = tower_record(changed)
    reached = [tier.key[1] for tier in towers.tower(HERO).score_rewards[HERO_FLOOR] if tier.threshold <= best]
    assert record["floors"] == {str(HERO_FLOOR): best} and record["total"] == best
    assert reached and record["score_rewards"] == {str(HERO_FLOOR): reached}
    assert rewards_due(changed, towers, HERO, saved_records(changed), accept)[1] == ()
    lowered = world.towers.set_floor_best(changed, HERO_FLOOR, 0)
    assert tower_record(lowered)["floors"] == {} and tower_record(lowered)["total"] == 0
    assert tower_record(lowered)["score_rewards"] == {str(HERO_FLOOR): reached}
    ceiling = towers.scoring(HERO_FLOOR).ceiling
    refused(lambda: world.towers.set_floor_best(state, HERO_FLOOR, ceiling + 1), "tower_admin_invalid", 400)
    refused(lambda: world.towers.set_floor_best(state, HERO_FLOOR, -1), "tower_admin_invalid", 400)
    refused(lambda: world.towers.set_floor_best(state, GATE_QUEST, best), "tower_request_invalid", 400)
    refused(lambda: world.towers.set_floor_best(state, GENERAL_FLOOR, best), "tower_not_started", 409)


def test_crystal_stock_adjustments_stay_within_the_generation_limit(world):
    state = opened(world).state
    assert tower_record(world.towers.adjust_stack(state, HERO, FIRST_LIMIT))["stack"] == FIRST_LIMIT
    assert tower_record(world.towers.adjust_stack(state, HERO, 0))["stack"] == 0
    for stack in (FIRST_LIMIT + 1, -1, True):
        refused(lambda: world.towers.adjust_stack(state, HERO, stack), "tower_admin_invalid", 400)
    refused(lambda: world.towers.adjust_stack(state, GENERAL, 0), "tower_not_started", 409)
    refused(lambda: world.towers.adjust_stack(state, 999999, 0), "tower_request_invalid", 400)


def test_settling_now_adds_exactly_one_day_after_catching_up(world):
    state = opened(world).state
    state, changed, forced = world.towers.settle_now(state, NOW)
    assert changed == (CONSUME,) and forced is True
    assert (tower_record(state)["stack"], held(state, CONSUME)) == (2 * FIRST_GENERATION, 2 * DAILY_REFILL)
    state, changed, forced = world.towers.settle_now(state, NOW + 2 * DAY)
    assert changed == (CONSUME,) and forced is True
    assert (tower_record(state)["stack"], held(state, CONSUME)) == (5 * FIRST_GENERATION, 5 * DAILY_REFILL)
    closed = deepcopy(state)
    closed["tables"]["quest_records"] = []
    assert world.towers.settle_now(closed, NOW + 2 * DAY) == (closed, (), False)


def test_deferred_tiers_are_judged_live_and_mailed_once_their_pool_opens(world, bundle, towers, monkeypatch):
    initial = world.store.profile().state
    refused(lambda: world.towers.redeliver_deferred(initial, HERO), "tower_not_started", 409)
    refused(lambda: world.towers.redeliver_deferred(initial, 999999), "tower_request_invalid", 400)
    clear(world.store, GATE_QUEST)
    clear(world.store, HERO_FLOOR)
    clear(world.store, HERO_SECOND, stars=2)
    state = world.towers.settle(world.store.profile().state, NOW)[0]
    state, presents, deferred = rewards_due(state, towers, HERO, saved_records(state), world.towers.deliverable)
    assert [present["description"] for present in presents] == ["塔合計★数報酬"] * (TICKET_TIER[0] - 1)
    assert deferred == ({"source": STAR_SOURCE, "key": list(TICKET_TIER), "reason": POOL_CLOSED},)
    live = {**deferred[0], "current_reason": POOL_CLOSED}
    assert world.towers.describe(state, NOW)["towers"][0]["deferred"] == [live]
    waiting, mailed, kept = world.towers.redeliver_deferred(state, HERO)
    assert (mailed, kept) == ((), deferred) and tower_record(waiting) == tower_record(state)
    monkeypatch.setattr(bundle.presents.item_tickets, "validate_item", lambda item_id: None)
    assert world.towers.describe(state, NOW)["towers"][0]["deferred"] == [{**live, "current_reason": None}]
    delivered, mailed, kept = world.towers.redeliver_deferred(state, HERO)
    assert len(mailed) == 1 and kept == () and tower_record(delivered)["star_rewards"] == [1, 2, 3, 4, 5]
    mail = {row["PresentID"]: row for row in delivered["tables"]["presents"]}
    assert (mail[mailed[0]]["ItemID"], mail[mailed[0]]["Description"]) == (TICKET, "塔合計★数報酬")


def test_reward_resets_clear_the_claimed_tiers_and_report_them(world, towers):
    clear(world.store, GATE_QUEST)
    clear(world.store, HERO_FLOOR, stars=2)
    state = world.towers.settle(world.store.profile().state, NOW)[0]
    state = world.towers.set_floor_best(state, HERO_FLOOR, 10000)
    state = rewards_due(state, towers, HERO, saved_records(state), accept)[0]
    stars_only, reset = world.towers.reset_rewards(state, HERO, (STAR_SOURCE,))
    assert reset == ({"source": STAR_SOURCE, "key": [1]}, {"source": STAR_SOURCE, "key": [2]})
    assert tower_record(stars_only)["star_rewards"] == [] and tower_record(stars_only)["score_rewards"]
    everything, reset = world.towers.reset_rewards(state, HERO)
    assert reset[:2] == ({"source": SCORE_SOURCE, "key": [HERO_FLOOR, 1]}, {"source": SCORE_SOURCE, "key": [HERO_FLOOR, 2]})
    assert (tower_record(everything)["score_rewards"], tower_record(everything)["star_rewards"]) == ({}, [])
    for sources in ((), ("score", "score"), ("bronze",), "score", {SCORE_SOURCE}):
        refused(lambda: world.towers.reset_rewards(state, HERO, sources), "tower_admin_invalid", 400)


def test_native_wire_serves_the_tower_operations_and_replays_claims_after_restart(world, bundle, tmp_path):
    initial = deepcopy(world.store.profile().state)
    initial["tables"]["quest_records"] = [{"QuestID": GATE_QUEST, "MaxRank": 3, "AchieveFlag": CLEAR_FLAG,
                                           "RemainAssault": 0, "FirstDefeatDate": 0, "Shown": False}]
    handlers = world.towers.handlers() | bundle.player_state.handlers() | bundle.bootstrap.handlers()

    def application():
        return create_app(tmp_path, initial_state=initial, state_initializer=world.towers.initialize,
                          handlers=handlers)

    app = application()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, entered = post(client, floor(app.state.store, PORTAL_QUEST))
        assert rows(entered["StackCrystal"]) == {HERO: FIRST_GENERATION, GENERAL: 0, SPIRIT: 0}
        assert possession(entered["POSSESSION"]) == {CONSUME: DAILY_REFILL, **dict.fromkeys(CRYSTALS.values(), 0)}
        assert list(post(client, listing(app.state.store))[1]) == ["SYSTEM"]
        envelope = claiming(app.state.store, HERO)
        wire, claimed = post(client, envelope)
        assert possession(claimed["POSSESSION"]) == {HERO_CRYSTAL: FIRST_GENERATION}
        assert rows(claimed["StackCrystal"]) == {HERO: 0}
        saved = app.state.store.profile()
    reopened = application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire and reopened.state.store.profile() == saved
