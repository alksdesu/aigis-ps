from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime
import json
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps import quest_modes
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.mission_window_revival import MissionWindowRules, build_mission_window_revival
from aigis_ps.quest_catalog import QuestCatalog, StarReward
from aigis_ps.quest_mode_reproduce import POLICY, ReproducePolicy
from aigis_ps.quest_modes import (
    CLEAR_FLAG, ENTRUST_PARITY_HOOKS, MAX_OBTAINED, MODE_SPECS, SLOT_FLAGS, THREE_STAR_FLAG, ModePolicy, StartContext,
    candidate_attributes, validate_entrust_parity,
)
from aigis_ps.quest_protocol import MAX_CANDIDATES
from aigis_ps.quest_service import QuestService
from test_history_disclosure import (
    GROUP_CLOSED, OPEN_DAYS, STARS_SHORT, disclose, login_state, prerequisite_members, rate, start_on,
)
from test_quest_battle_modes import charisma, hard_start, mutate, new_cards, put_record, record, set_rank, update_game_param
from test_quest_service import allow_quest, bundle, finish_request, nodes, setup, start_request
from test_quest_settlement import bonus_values, drops, item_counts, possession


REPRODUCE_TYPE = 8
GATE_QUEST = 1004
REPRODUCE_RANK = 9
CHARISMA = 1000
PLATFORM = 1
INT32_MAX = 0x7FFFFFFF
REPRODUCE_QUESTS = 207
CONDITIONAL_QUESTS = list(range(4807, 4814))
FREE_CRYSTAL = (StarReward("crystal", 0, 0),)
# Not the default count, so the payout shows the crystals come from the local rule rather than the row.
FIRST_STAR_CRYSTALS = 2
CLEARED = CLEAR_FLAG | THREE_STAR_FLAG
COLLECTION_BIT = 1
MONDAY = datetime(2026, 11, 2, 12, tzinfo=JST)
SUNDAY = datetime(2026, 11, 8, 12, tzinfo=JST)
OPENED = {"start_at": "2026-11-01T15:00:00+09:00", "end_at": "2026-11-16T10:59:59+09:00"}
OPENING = (datetime(2026, 11, 1, 15, tzinfo=JST), datetime(2026, 11, 16, 10, 59, 59, tzinfo=JST))
GOLD_RUSH_BATCH, GOLD_RUSH_22_BATCH, RANCE_BATCH = "Reproduce:300084", "Reproduce:300217", "Reproduce:320180"
RANCE_MISSIONS = (320180, 320390, 320424)
RANCE_ORIGINAL_END = datetime(2026, 10, 15, 10, 59, 59, tzinfo=JST)
HISTORY_QUEST, HISTORY_PREREQUISITE, HISTORY_MISSION = 9032, 4030, 920014
HISTORY_MAP = "Map920014_3809.aar"
# 9032 drops one 1654 unit, three 5002 (three carved crystals each) and three 5003 (five each).
HISTORY_DROPS = MappingProxyType({1001654: 1, 5002: 9, 5003: 15})
GATE_LOCKED = "The mode entry requires its gate quest to be cleared"
# 4807 belongs to Gold Rush 13 (300130); from three stars its 7005 prize enemy sits at RT index 3.
GOLD_RUSH_QUEST, GOLD_RUSH_MISSION, GOLD_RUSH_SPECIAL, GOLD_RUSH_OVERRIDE = 4807, 300130, 3, 437
PRIZE_ENEMY_ATTR = 2
FIXED_TEAMS = MappingProxyType({6747: 258, 7894: 391, 8254: 421})
RANCE_QUEST, RANCE_MISSION = 8254, 320424
# 7208 (Gold Rush 27, 300293) pays its Treasure5 summon ticket once slots 1-3 are all obtained.
TICKET_QUEST, TICKET, GIFT_CODE, TICKET_CODE = 7208, "item_smn_ticket_19007", 1002, 10011
GIFT_INDEX = 1
COLLECTION_SLOT = 4
# 7894 drops 5002 (three carved crystals) from RT index 2; its DropBonusConfig row scales them by 1.5, floored.
BONUS_QUEST, CRYSTAL_INDEX, CRYSTAL_CODE, CRYSTAL_DROPS, BONUS_CRYSTALS = 7894, 2, 5002, 3, 4
CARVED_CRYSTAL, CARVED_TYPE, BONUS_VALUE = "trade_item_002000", 1, 1.5
LOCAL_RULE_KEYS = frozenset((
    "reproduce_windows", "history_windows", "candidate_selection", "candidate_limit", "special_prizes", "override_prizes",
    "stage_variant_prizes", "dated_prizes", "skipped_prizes", "obtained_limit", "treasure_slots", "first_three_stars",
    "stack_zero_items", "gold_get", "drop_bonus"))


def refused_start(code, service, store, starting):
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.start(starting, store)
    assert (error.value.code, error.value.status) == (code, 409)
    assert store.profile() == before
    return error.value


def saved(store, quest_id):
    return next((row for row in store.profile().state["tables"]["quest_records"] if row["QuestID"] == quest_id), None)


def reproduce_start(service, store, quest_id, tick=1000):
    current = saved(store, quest_id)
    candidates = service.catalog.get(quest_id).candidates_for(0 if current is None else current["MaxRank"])
    return start_request(service, store, quest_id, tick, candidates=list(candidate_attributes(current, candidates)))


def lottery(store):
    return store.profile().state["quest_state"]["active"]["selected_mask"]


def hold(store, item_id, count):
    def change(state):
        rows = [row for row in state["tables"]["possessions"] if row["ItemID"] != item_id]
        state["tables"]["possessions"] = [*rows, {"ItemID": item_id, "Count": count}]
        return state
    mutate(store, change)


def admissions(catalog):
    quest_ids = catalog.playable_quests(REPRODUCE_TYPE)
    disclosing = prerequisite_members(catalog, HISTORY_MISSION)
    records = {quest_id: {"QuestID": quest_id, "MaxRank": 3, "AchieveFlag": CLEARED}
               for quest_id in (GATE_QUEST, HISTORY_PREREQUISITE, *disclosing, *quest_ids)}
    outcomes = {}
    for quest_id in quest_ids:
        context = StartContext(catalog, catalog.get(quest_id), catalog.mode(quest_id), records, REPRODUCE_RANK, MONDAY,
                               False, PLATFORM, login_state(OPEN_DAYS))
        try:
            outcomes[quest_id] = quest_modes.admit(POLICY, context).mission_id
        except ProtocolError as error:
            outcomes[quest_id] = error.code
    return (Counter(outcome for outcome in outcomes.values() if type(outcome) is int),
            Counter(outcome for outcome in outcomes.values() if type(outcome) is str))


@pytest.fixture(scope="module")
def reopen(bundle):
    shared = bundle.quests.catalog
    base = lambda name: bundle.native_cdn.assets.read(name)[0]
    built = {}

    def catalog(*keys):
        if keys not in built:
            revival = build_mission_window_revival(base, MissionWindowRules(True, {key: OPENED for key in keys}))
            reader = lambda name: revival.replacements[name] if name in revival.replacements else base(name)
            built[keys] = QuestCatalog(shared.snapshot, reader, possessions=bundle.possessions,
                                       resource_version=shared.resource_version)
        return built[keys]
    return catalog


@pytest.fixture
def reproduce(setup, bundle, reopen):
    _, store = setup
    allow_quest(store, GATE_QUEST)
    set_rank(bundle, store, REPRODUCE_RANK)
    update_game_param(store, NowCharisma=CHARISMA)

    def service(*keys, now=MONDAY):
        catalog = reopen(*keys) if keys else bundle.quests.catalog
        return QuestService(catalog, bundle.bootstrap, bundle.player_state, bundle.possessions,
                            clock=lambda: now, randbelow=lambda _: 0)
    return service, store


def test_reproduce_policy_is_enabled_behind_the_menu_gate_and_overrides_only_the_start_check():
    assert quest_modes.policy(REPRODUCE_TYPE) is POLICY
    assert POLICY.status() == {"policy": "reproduce", "enabled": True, "reason": None, "hard_mode": False,
                               "timed_conditions": False, "gate_quest": GATE_QUEST}
    spec = MODE_SPECS[REPRODUCE_TYPE]
    assert (spec.resources, spec.gold_counted, spec.hard_rank, spec.score_mode, spec.entrust) == (
        ("Reproduce", "History"), True, None, False, True)
    # The start check adds the History disclosure, which entrust admission runs as well.
    hooks = (*ENTRUST_PARITY_HOOKS, "entrust_rewards", "entrust_settle")
    overridden = {name for name in hooks if getattr(ReproducePolicy, name) is not getattr(ModePolicy, name)}
    assert overridden == POLICY.entrust_parity == frozenset({"check_start"})
    assert POLICY.entrust_blocker() is None and validate_entrust_parity(POLICY) is None


def test_reproduce_local_rules_describe_windows_candidates_stars_tickets_and_bonus():
    rules = quest_modes.support_status()[REPRODUCE_TYPE]["local_rules"]
    assert rules == POLICY.local_rules() and json.loads(json.dumps(rules)) == rules
    assert set(rules) == LOCAL_RULE_KEYS
    assert (rules["candidate_limit"], rules["obtained_limit"], rules["gold_get"]) == (MAX_CANDIDATES, MAX_OBTAINED, "counted")
    rules["obtained_limit"] = 0
    assert POLICY.local_rules()["obtained_limit"] == MAX_OBTAINED


def test_every_reproduce_quest_builds_with_the_free_first_star_crystal(bundle):
    catalog = bundle.quests.catalog
    quest_ids = catalog.playable_quests(REPRODUCE_TYPE)
    assert len(quest_ids) == REPRODUCE_QUESTS and set(quest_ids) <= set(catalog.mode_ids)
    archives = {}
    for quest_id in quest_ids:
        mode = catalog.mode(quest_id)
        assert (mode.quest_type, mode.hard, mode.gold_counted, mode.score_mode) == (REPRODUCE_TYPE, None, True, False)
        assert mode.star_rewards_normal == FREE_CRYSTAL and mode.star_rewards_hard == ()
        assert POLICY.validate_row(catalog.row(quest_id), catalog) is None
        archives[quest_id] = (mode.map_archive, mode.map_mission_id)
    assert archives.pop(HISTORY_QUEST) == (HISTORY_MAP, HISTORY_MISSION)
    assert all(owner is None and archive == f"Map{catalog.get(quest_id).map_no:04d}.aar"
               for quest_id, (archive, owner) in archives.items())
    assert [quest_id for quest_id in quest_ids if catalog.get(quest_id).conditional] == CONDITIONAL_QUESTS
    status = catalog.status()
    assert not set(status["mode_failed_quests"]) & set(quest_ids) and status["excluded_prize_rows"] == {}


def test_reproduce_fixed_teams_name_their_native_unit_lists(bundle):
    catalog = bundle.quests.catalog
    teams = {quest_id: catalog.get(quest_id).unit_list for quest_id in catalog.playable_quests(REPRODUCE_TYPE)
             if catalog.get(quest_id).unit_list}
    assert teams == FIXED_TEAMS
    assert all(catalog.fixed_units(unit_list) for unit_list in teams.values())


def test_default_windows_keep_every_reproduce_batch_closed_without_charging(reproduce, bundle):
    make, store = reproduce
    service = make()
    for quest_id in (GOLD_RUSH_QUEST, TICKET_QUEST, BONUS_QUEST):
        refused_start("quest_closed", service, store, reproduce_start(service, store, quest_id))
    admitted, refused = admissions(bundle.quests.catalog)
    assert admitted == {HISTORY_MISSION: 1} and refused == {"quest_closed": REPRODUCE_QUESTS - 1}


def test_history_member_9032_starts_through_mission_920014_behind_the_menu_gate(setup, bundle):
    service, store = setup
    set_rank(bundle, store, REPRODUCE_RANK)
    update_game_param(store, NowCharisma=CHARISMA)
    allow_quest(store, HISTORY_PREREQUISITE)
    error = refused_start("quest_locked", service, store, reproduce_start(service, store, HISTORY_QUEST))
    assert str(error) == GATE_LOCKED
    allow_quest(store, GATE_QUEST)
    error = refused_start("quest_closed", service, store, reproduce_start(service, store, HISTORY_QUEST))
    assert str(error) == GROUP_CLOSED
    disclose(store, service.catalog, HISTORY_MISSION)
    quest, mode = service.catalog.get(HISTORY_QUEST), service.catalog.mode(HISTORY_QUEST)
    assert (mode.map_archive, mode.map_mission_id, mode.mission_ids) == (HISTORY_MAP, HISTORY_MISSION, (HISTORY_MISSION,))
    service.start(reproduce_start(service, store, HISTORY_QUEST, tick=1001), store)
    active = store.profile().state["quest_state"]["active"]
    assert (active["mission_id"], active["mode"]["quest_type"], active["candidate_stars"]) == (
        HISTORY_MISSION, REPRODUCE_TYPE, 0)
    assert charisma(store) == CHARISMA
    carved = possession(store, CARVED_CRYSTAL)
    mask = lottery(store)
    tree = nodes(service.finish(finish_request(store, HISTORY_QUEST, tick=1002, obtained=mask, selected=mask), store))
    assert charisma(store) == CHARISMA - quest.charisma
    # The shared test clock is a Sunday, when the 9032 DropBonusConfig row is 0.
    assert drops(tree) == HISTORY_DROPS and "BONUS" not in tree
    assert item_counts(tree)[CARVED_CRYSTAL] == carved + HISTORY_DROPS[5002] + HISTORY_DROPS[5003]
    assert record(store, HISTORY_QUEST)["AchieveFlag"] == CLEARED | sum(SLOT_FLAGS[:3])
    assert store.profile().state["quest_state"]["history"][-1]["mission_id"] == HISTORY_MISSION


@pytest.mark.parametrize("days,ranks,message", [
    (0, (3, 3, 3, 3, 3), GROUP_CLOSED), (1, (3, 3, 3, 1, 0), STARS_SHORT), (1, (3, 3, 3, 2, 0), None),
], ids=["account_start_day", "ten_stars", "eleven_stars"])
def test_9032_waits_for_the_disclosure_of_its_history_mission(setup, bundle, days, ranks, message):
    service, store = setup
    set_rank(bundle, store, REPRODUCE_RANK)
    update_game_param(store, NowCharisma=CHARISMA)
    allow_quest(store, GATE_QUEST)
    allow_quest(store, HISTORY_PREREQUISITE)
    members = prerequisite_members(service.catalog, HISTORY_MISSION)
    mutate(store, lambda state: rate(start_on(state, days), dict(zip(members, ranks, strict=True))))
    starting = reproduce_start(service, store, HISTORY_QUEST)
    if message is not None:
        assert str(refused_start("quest_closed", service, store, starting)) == message
        return
    service.start(starting, store)
    assert store.profile().state["quest_state"]["active"]["mission_id"] == HISTORY_MISSION


def test_window_rules_reopen_every_member_of_the_rance_batch_through_patched_resources(reproduce, bundle, reopen):
    catalog = reopen(RANCE_BATCH)
    windows = catalog.mission_windows(REPRODUCE_TYPE)
    assert {(windows.mission(mission_id).start, windows.mission(mission_id).end) for mission_id in RANCE_MISSIONS} == {
        OPENING}
    assert bundle.quests.catalog.mission_windows(REPRODUCE_TYPE).mission(RANCE_MISSION).end == RANCE_ORIGINAL_END
    admitted, refused = admissions(catalog)
    assert admitted == {320180: 3, 320390: 9, 320424: 9, HISTORY_MISSION: 1} and refused == {"quest_closed": 185}
    make, store = reproduce
    closed = make()
    refused_start("quest_closed", closed, store, reproduce_start(closed, store, RANCE_QUEST))
    service = make(RANCE_BATCH)
    service.start(reproduce_start(service, store, RANCE_QUEST), store)
    active = store.profile().state["quest_state"]["active"]
    assert (active["quest_id"], active["mission_id"]) == (RANCE_QUEST, RANCE_MISSION)
    assert service.catalog.get(RANCE_QUEST).unit_list == FIXED_TEAMS[RANCE_QUEST]


def test_gold_rush_4807_at_three_stars_pays_its_437_override_without_slot_one(reproduce):
    make, store = reproduce
    service = make(GOLD_RUSH_BATCH)
    put_record(store, GOLD_RUSH_QUEST, flags=CLEARED)
    quest = service.catalog.get(GOLD_RUSH_QUEST)
    special = quest.candidates_for(3)[GOLD_RUSH_SPECIAL]
    assert (special.slot, special.special_attr, quest.reward_code(special), quest.marks_slot(special)) == (
        1, PRIZE_ENEMY_ATTR, GOLD_RUSH_OVERRIDE, False)
    response = service.start(reproduce_start(service, store, GOLD_RUSH_QUEST), store)
    active = store.profile().state["quest_state"]["active"]
    assert (active["mission_id"], active["candidate_stars"]) == (GOLD_RUSH_MISSION, 3)
    assert len(nodes(response)["LOTTERY"].children[1].values) == len(quest.candidates_for(3))
    finishing = finish_request(store, GOLD_RUSH_QUEST, obtained=1 << GOLD_RUSH_SPECIAL, selected=lottery(store))
    tree = nodes(service.finish(finishing, store))
    assert new_cards(tree) == (GOLD_RUSH_OVERRIDE,) and drops(tree) == {GOLD_RUSH_OVERRIDE: 1}
    assert record(store, GOLD_RUSH_QUEST)["AchieveFlag"] == CLEARED
    assert store.profile().state["quest_state"]["history"][-1]["candidate_stars"] == 3


def test_stack_zero_summon_tickets_from_a_gold_rush_collection_enter_possessions_without_iof(reproduce):
    make, store = reproduce
    service = make(GOLD_RUSH_22_BATCH)
    quest = service.catalog.get(TICKET_QUEST)
    ticket = service.catalog.decode_reward(quest.treasures[COLLECTION_SLOT])
    assert (quest.treasures[COLLECTION_SLOT], ticket.kind, ticket.item_id, ticket.count) == (TICKET_CODE, "item", TICKET, 1)
    assert service.possessions.stack(TICKET) == 0
    put_record(store, TICKET_QUEST, flags=CLEARED | SLOT_FLAGS[0] | SLOT_FLAGS[2])
    hold(store, TICKET, INT32_MAX - 1)
    service.start(reproduce_start(service, store, TICKET_QUEST), store)
    finishing = finish_request(store, TICKET_QUEST, obtained=1 << GIFT_INDEX, selected=lottery(store))
    tree = nodes(service.finish(finishing, store))
    assert drops(tree) == {GIFT_CODE: 1, TICKET_CODE: 1} and "IOF" not in tree
    assert item_counts(tree)[TICKET] == possession(store, TICKET) == INT32_MAX
    assert record(store, TICKET_QUEST)["AchieveFlag"] == CLEARED | sum(SLOT_FLAGS[:3]) | COLLECTION_BIT


@pytest.mark.parametrize("moment,bonus_day", [(MONDAY, True), (SUNDAY, False)], ids=["monday", "sunday"])
def test_rance_drop_bonus_scales_carved_crystals_on_monday_only(reproduce, moment, bonus_day):
    make, store = reproduce
    service = make(RANCE_BATCH, now=moment)
    assert service.catalog.mode(BONUS_QUEST).drop_bonus.value_on(moment) == (BONUS_VALUE if bonus_day else 0.0)
    carved = possession(store, CARVED_CRYSTAL)
    service.start(reproduce_start(service, store, BONUS_QUEST), store)
    finishing = finish_request(store, BONUS_QUEST, obtained=1 << CRYSTAL_INDEX, selected=lottery(store))
    tree = nodes(service.finish(finishing, store))
    credited = BONUS_CRYSTALS if bonus_day else CRYSTAL_DROPS
    assert drops(tree) == {CRYSTAL_CODE: CRYSTAL_DROPS} and item_counts(tree)[CARVED_CRYSTAL] == carved + credited
    assert ("BONUS" in tree) is bonus_day
    receipt = store.profile().state["quest_state"]["history"][-1]["drop_bonus"]
    if bonus_day:
        assert bonus_values(tree) == (CARVED_TYPE, BONUS_VALUE)
        assert receipt == {"value": BONUS_VALUE, "increments": {CARVED_CRYSTAL: BONUS_CRYSTALS - CRYSTAL_DROPS}}
    else:
        assert receipt == {"value": 0.0, "increments": {}}


def test_a_first_reproduce_three_star_clear_pays_the_local_free_crystal_count_once(reproduce):
    make, store = reproduce
    service = make(RANCE_BATCH)
    service.rules = replace(service.rules, first_three_star_crystals=FIRST_STAR_CRYSTALS)
    service.start(reproduce_start(service, store, RANCE_QUEST), store)
    assert store.profile().state["quest_state"]["active"]["mode"]["star_rewards"] == [asdict(reward) for reward in FREE_CRYSTAL]
    wallet = wallet_totals(store.profile().state)["free"]
    first = nodes(service.finish(finish_request(store, RANCE_QUEST, selected=lottery(store)), store))
    assert "PFC" in first and wallet_totals(store.profile().state)["free"] == wallet + FIRST_STAR_CRYSTALS
    assert store.profile().state["quest_state"]["history"][-1]["crystals"] == FIRST_STAR_CRYSTALS
    service.start(reproduce_start(service, store, RANCE_QUEST, tick=1002), store)
    repeat = nodes(service.finish(finish_request(store, RANCE_QUEST, tick=1003, selected=lottery(store)), store))
    assert "PFC" not in repeat and wallet_totals(store.profile().state)["free"] == wallet + FIRST_STAR_CRYSTALS


@pytest.mark.parametrize("number", [78, 82])
def test_reproduce_quests_have_no_four_star_challenge(reproduce, number):
    make, store = reproduce
    service = make(GOLD_RUSH_BATCH)
    put_record(store, GOLD_RUSH_QUEST, flags=CLEARED | sum(SLOT_FLAGS))
    refused_start("quest_hard_unavailable", service, store, hard_start(service, store, GOLD_RUSH_QUEST, number=number))


@pytest.mark.parametrize("field,value", [
    ("Type", 16), ("Type", 8.0), ("Type", True), ("_HardLevel", 200), ("_HardLevel", None), ("BonusType", 3),
    ("BonusType", False), ("BonusNum", 1), ("BonusNum", -1), ("UnitList", -1), ("UnitList", 2 ** 31),
    ("UnitList", True), ("UnitList", 391.0), ("UnitList", INT32_MAX),
])
def test_rows_outside_the_reproduce_contract_are_refused(bundle, field, value):
    catalog = bundle.quests.catalog
    with pytest.raises(ProtocolError) as error:
        POLICY.validate_row(MappingProxyType({**catalog.row(BONUS_QUEST), field: value}), catalog)
    assert (error.value.code, error.value.status) == ("quest_definition_invalid", 503)
    assert field in str(error.value)


@pytest.mark.parametrize("field", ["Type", "_HardLevel", "BonusType", "BonusNum", "UnitList"])
def test_rows_missing_a_contract_column_are_refused(bundle, field):
    catalog = bundle.quests.catalog
    row = MappingProxyType({name: value for name, value in catalog.row(BONUS_QUEST).items() if name != field})
    with pytest.raises(ProtocolError) as error:
        POLICY.validate_row(row, catalog)
    assert (error.value.code, error.value.status) == ("quest_definition_invalid", 503) and field in str(error.value)


@pytest.mark.parametrize("rewards", [
    (StarReward("crystal", 0, 1),), (StarReward("unit", 3, 1, card_id=GOLD_RUSH_OVERRIDE),),
    (*FREE_CRYSTAL, StarReward("crystal", 0, 3)), (),
])
def test_rows_whose_first_star_reward_is_not_the_free_crystal_are_refused(bundle, rewards):
    catalog = bundle.quests.catalog
    resolved = SimpleNamespace(fixed_units=catalog.fixed_units, star_rewards=lambda row, hard: rewards)
    with pytest.raises(ProtocolError, match="^Reproduce first three-star reward is not the free crystal$") as error:
        POLICY.validate_row(catalog.row(BONUS_QUEST), resolved)
    assert (error.value.code, error.value.status) == ("quest_definition_invalid", 503)
