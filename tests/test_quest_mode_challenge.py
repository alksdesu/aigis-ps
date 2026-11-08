from collections import Counter
from dataclasses import replace
from functools import reduce
from operator import or_
from types import MappingProxyType

import pytest

from aigis_ps import quest_modes
from aigis_ps.alxml import loads
from aigis_ps.business_calendar import business_day
from aigis_ps.errors import ProtocolError
from aigis_ps.mission_state import SEQUENTIAL_PERIOD
from aigis_ps.quest_catalog import StarReward
from aigis_ps.quest_clear_spec import QUEST_CLEAR_CONDITION
from aigis_ps.quest_mode_challenge import POLICY
from aigis_ps.quest_modes import CLEAR_FLAG, SLOT_FLAGS, THREE_STAR_FLAG
from aigis_ps.storage import Store
from test_quest_battle_modes import (
    MISSION_NOW, bare_start, card_ids, charisma, gold_events, hard_start, new_cards, put_record, record, set_rank,
    three_star_gold, update_game_param,
)
from test_quest_service import (
    NOW, allow_quest, bundle, client_candidates, finish_request, nodes, setup, start_request,
)
from test_quest_settlement import derived, drops, possession


CHALLENGE_TYPE = 1
STAR_GIFT_TYPE = 102
GATE_QUEST = 1004
CHALLENGE_MISSION = 800001
CHALLENGE_QUESTS = tuple(range(2001, 2062))
SLOT_FOUR_FIVE_QUESTS = tuple(range(2021, 2059))
TOP_RANK = 50
CHARISMA = 5000
RUBY = "gift_0002_ruby"
GIFT_ITEMS = MappingProxyType({
    100: "gift_0000_bouquet", 101: "gift_0001_quartz", 102: RUBY, 103: "gift_0003_diamond", 104: "gift_1000_beer",
    105: "gift_1001_wine", 106: "gift_1002_fineLiquor", 107: "gift_1003_millenniumWine",
})
DIAMOND_TYPE = 103
# Empire dark crystals use this item star type; it names its item through the row's RewardItemID column.
ITEM_STAR_TYPE = 100022
MODE_CACHES = ("_mode_definitions", "_modes", "_mode_failures")
BEGINNER_WEEKDAY_MISSION = 10002
INT32_MAX = 2 ** 31 - 1
# OnCommand IsChallengeClearCount(5,15,25,35,45,55) marks the clears that add two barracks slots; 55 is ClearQuestCountLimit.
CAPACITY_CLEARS, CAPACITY_STEP, CLEAR_LIMIT = (5, 15, 25, 35, 45, 55), 2, 55
# 2041 sets PrizeEnemyDropPercent on ordinary enemies, which 5D33E0 reads only for enemies 7000-7999.
DROP_PERCENT_QUEST = 2041
DROP_PERCENTS = MappingProxyType({1: 60, 2: 50, 3: 30, 4: 30, 5: 20})


def selected(store):
    return store.profile().state["quest_state"]["active"]["selected_mask"]


def item_counts(tree):
    columns = tree["POSSESSION"].children
    return dict(zip(columns[0].values, columns[1].values))


def slot_bits(quest):
    return reduce(or_, (SLOT_FLAGS[candidate.slot - 1] for candidate in quest.candidates), 0)


def attributes(service, store, quest_id):
    return [attribute for _, _, attribute in client_candidates(service, store, quest_id)]


def entry_prizes(catalog, quest_id):
    quest = catalog.get(quest_id)
    rows = catalog.archive_rows(catalog.mode(quest_id).map_archive, f"Entry{quest.entry_no:02d}.atb")
    return [row for row in rows if row["PrizeCardID"]]


def replace_row(monkeypatch, catalog, quest_id, change):
    monkeypatch.setitem(catalog._all_rows, quest_id, {**catalog._all_rows[quest_id], **change})
    for cache in MODE_CACHES:
        monkeypatch.setattr(catalog, cache, {})


def challenge_capacity(capacity, state):
    others = [row for row in state["tables"]["quest_records"] if row["QuestID"] not in CHALLENGE_QUESTS]
    without = {**state, "tables": {**state["tables"], "quest_records": others}}
    return capacity.usage(state).capacities[0] - capacity.usage(without).capacities[0]


def assert_refused(code, status, callback):
    with pytest.raises(ProtocolError) as error:
        callback()
    assert (error.value.code, error.value.status) == (code, status)
    return error.value


@pytest.fixture
def challenger(setup, bundle):
    service, store = setup
    allow_quest(store, GATE_QUEST)
    set_rank(bundle, store, TOP_RANK)
    update_game_param(store, NowCharisma=CHARISMA)
    return service, store


def test_challenge_policy_is_registered_with_the_permanent_mode_contract():
    assert quest_modes.policy(CHALLENGE_TYPE) is POLICY and POLICY.quest_types == (CHALLENGE_TYPE,)
    assert POLICY.status() == {"policy": "challenge", "enabled": True, "reason": None, "hard_mode": False,
                               "timed_conditions": False, "gate_quest": GATE_QUEST}


def test_every_challenge_quest_builds_with_prize_slots_one_to_five_and_the_ruby(bundle):
    catalog = bundle.quests.catalog
    assert catalog.playable_quests(CHALLENGE_TYPE) == CHALLENGE_QUESTS
    assert set(CHALLENGE_QUESTS) <= set(catalog.mode_ids)
    slot_four_five, outside_prerequisites = [], {}
    for position, quest_id in enumerate(CHALLENGE_QUESTS, 1):
        assert POLICY.validate_row(catalog.row(quest_id), catalog) is None
        quest, mode = catalog.get(quest_id), catalog.mode(quest_id)
        if quest.prerequisite not in CHALLENGE_QUESTS:
            outside_prerequisites[quest_id] = quest.prerequisite
        slots = {candidate.slot for candidate in quest.candidates}
        assert slots and slots <= set(range(1, len(SLOT_FLAGS) + 1))
        assert all(quest.treasures[slot - 1] for slot in slots)
        assert quest.rank == min(position, TOP_RANK) and quest.unit_list == 0
        assert (mode.mission_ids, mode.hard, mode.gold_counted, mode.score_mode) == ((CHALLENGE_MISSION,), None, True, False)
        assert (mode.map_archive, mode.map_mission_id) == (f"Map{quest.map_no:04d}.aar", None)
        assert mode.star_rewards_normal == (StarReward("item", STAR_GIFT_TYPE, 1, item_id=RUBY),)
        if slots & {4, 5}:
            slot_four_five.append(quest_id)
    assert tuple(slot_four_five) == SLOT_FOUR_FIVE_QUESTS
    assert outside_prerequisites == {2001: GATE_QUEST}


@pytest.mark.parametrize("gate_record", [None, (0, 0), (3, INT32_MAX & ~CLEAR_FLAG)])
def test_challenges_require_the_cleared_menu_gate_quest(setup, bundle, gate_record):
    service, store = setup
    put_record(store, 2001)
    set_rank(bundle, store, 2)
    if gate_record is not None:
        max_rank, flags = gate_record
        put_record(store, GATE_QUEST, max_rank=max_rank, flags=flags)
    before = store.profile()
    error = assert_refused("quest_locked", 409, lambda: service.start(start_request(service, store, 2002), store))
    assert "gate quest" in str(error) and store.profile() == before
    put_record(store, GATE_QUEST, max_rank=1, flags=CLEAR_FLAG)
    service.start(start_request(service, store, 2002), store)
    assert store.profile().state["quest_state"]["active"]["quest_id"] == 2002


def test_the_first_challenge_follows_1004_through_its_cross_type_prerequisite(setup):
    service, store = setup
    quest = service.catalog.get(2001)
    assert quest.prerequisite == GATE_QUEST
    before = store.profile()
    with pytest.raises(ProtocolError, match="prerequisite") as error:
        service.start(start_request(service, store, 2001), store)
    assert error.value.code == "quest_locked" and store.profile() == before
    allow_quest(store, GATE_QUEST)
    remaining = charisma(store)
    service.start(start_request(service, store, 2001), store)
    assert store.profile().state["quest_state"]["active"]["quest_id"] == 2001
    assert charisma(store) == remaining


def test_rank_fifty_challenges_refuse_rank_forty_nine_players(setup, bundle):
    service, store = setup
    allow_quest(store, GATE_QUEST)
    put_record(store, 2048)
    set_rank(bundle, store, TOP_RANK - 1)
    before = store.profile()
    assert_refused("quest_rank_required", 409, lambda: service.start(start_request(service, store, 2050), store))
    assert store.profile() == before
    set_rank(bundle, store, TOP_RANK)
    service.start(start_request(service, store, 2050), store)
    assert store.profile().state["quest_state"]["active"]["quest_id"] == 2050


def test_four_star_registrations_are_refused_without_charging(challenger):
    service, store = challenger
    put_record(store, 2001)
    before = store.profile()
    for tick, number in ((1000, 78), (1001, 82)):
        assert_refused("quest_hard_unavailable", 409,
                       lambda: service.start(hard_start(service, store, 2001, tick, number), store))
    assert store.profile() == before


def test_first_three_star_clear_credits_slot_four_drops_gold_events_and_the_ruby_once(challenger):
    service, store = challenger
    put_record(store, 2020)
    quest = service.catalog.get(2021)
    assert [candidate.slot for candidate in quest.candidates] == [1, 2, 3, 4]
    response = service.start(start_request(service, store, 2021), store)
    assert nodes(response)["LOTTERY"].children[1].values == (1, 1, 1, 1)
    assert charisma(store) == CHARISMA
    active = store.profile().state["quest_state"]["active"]
    assert active["mission_id"] == CHALLENGE_MISSION and active["mode"] == {
        "quest_type": CHALLENGE_TYPE, "difficulty": "normal", "boost_ignored": None,
        "star_rewards": [{"kind": "item", "reward_type": STAR_GIFT_TYPE, "count": 1, "card_id": None, "item_id": RUBY}]}
    assert quest.unit_level == 0 and active["unit_reservations"] == [{"card_id": 57, "level": quest.unit_level, "count": 1}]
    finishing = finish_request(store, 2021, obtained=0b1111, selected=0b1111, gold=2, extra=gold_events(2))
    response = service.finish(finishing, store)
    completed = store.profile()
    assert service.finish(finishing, store) == response and store.profile() == completed
    assert charisma(store) == CHARISMA - quest.charisma
    tree = nodes(response)
    assert drops(tree) == {1001: 1, 1002: 1, 1003: 1, 57: 1} and new_cards(tree) == (57,)
    dropped = completed.state["player_state"]["units"][-1]
    assert (dropped["CardID"], dropped["Exp"], dropped["SkillLevel"]) == (57, 0, 1)
    assert item_counts(tree) == {"gift_0000_bouquet": 1, "gift_0001_quartz": 1, RUBY: 2}
    assert tree["REWARD"].children[0].values == (quest.rank_exp,)
    assert tree["REWARD"].children[1].values == (three_star_gold(quest.gold + 2 * service.rules.gold_per_event),)
    assert "PFC" not in tree and "PPT" not in tree
    assert [child.name for child in loads(response).children][-1] == "POSSESSION"
    row = record(store, 2021)
    assert row["MaxRank"] == 3 and row["FirstDefeatDate"] == business_day(NOW)
    assert row["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG | slot_bits(quest)
    history = completed.state["quest_state"]["history"][-1]
    assert (history["star_rewards"], history["gold_events"], history["mission_id"]) == (
        active["mode"]["star_rewards"], 2, CHALLENGE_MISSION)
    service.start(start_request(service, store, 2021, tick=1002), store)
    assert attributes(service, store, 2021) == [1, 1, 1, 1]
    tree = nodes(service.finish(finish_request(store, 2021, tick=1003, selected=0b1111), store))
    assert "POSSESSION" not in tree and possession(store, RUBY) == 2


def test_slot_five_drops_take_bit_zero_and_mark_their_candidates_on_the_next_start(challenger):
    service, store = challenger
    put_record(store, 2027)
    quest = service.catalog.get(2028)
    assert [candidate.slot for candidate in quest.candidates] == [1, 2, 3, 4, 5]
    service.start(start_request(service, store, 2028), store)
    tree = nodes(service.finish(finish_request(store, 2028, obtained=0b11000, selected=0b11111, stars=2), store))
    assert drops(tree) == {57: 1, 58: 1} and new_cards(tree) == (57, 58) and "POSSESSION" not in tree
    row = record(store, 2028)
    assert row["MaxRank"] == 2 and row["AchieveFlag"] == CLEAR_FLAG | SLOT_FLAGS[3] | SLOT_FLAGS[4]
    stale = [(index, slot, 0) for index, slot, _ in client_candidates(service, store, 2028)]
    before = store.profile()
    assert_refused("quest_candidates_mismatch", 409,
                   lambda: service.start(start_request(service, store, 2028, 1002, candidates=stale), store))
    assert store.profile() == before
    assert attributes(service, store, 2028) == [0, 0, 0, 1, 1]
    service.start(start_request(service, store, 2028, tick=1002), store)
    tree = nodes(service.finish(finish_request(store, 2028, tick=1003, selected=0b11111), store))
    assert item_counts(tree) == {RUBY: 1} and not new_cards(tree)
    row = record(store, 2028)
    assert row["MaxRank"] == 3 and row["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG | SLOT_FLAGS[3] | SLOT_FLAGS[4]


@pytest.mark.parametrize("basis_points,roll,picked", [(10000, 9999, 1), (2000, 1999, 1), (2000, 2000, 0)])
def test_drop_percent_on_ordinary_enemies_leaves_the_lottery_to_the_local_rate(challenger, basis_points, roll, picked):
    service, store = challenger
    prizes = entry_prizes(service.catalog, DROP_PERCENT_QUEST)
    assert {row["PrizeCardID"]: row["PrizeEnemyDropPercent"] for row in prizes} == DROP_PERCENTS
    quest = service.catalog.get(DROP_PERCENT_QUEST)
    assert sorted(candidate.slot for candidate in quest.candidates) == sorted(DROP_PERCENTS)
    put_record(store, quest.prerequisite)
    service.rules = replace(service.rules, drop_basis_points=basis_points)
    service.randbelow = lambda _: roll
    response = service.start(start_request(service, store, DROP_PERCENT_QUEST), store)
    assert nodes(response)["LOTTERY"].children[1].values == (picked,) * len(quest.candidates)
    assert selected(store) == picked * ((1 << len(quest.candidates)) - 1)


@pytest.mark.parametrize("field,value", [
    ("Type", 0), ("Type", 2), ("Type", True), ("UnitList", 1), ("UnitList", None), ("BonusType", 0),
    ("BonusType", 3), ("BonusType", 99), ("BonusType", 108), ("BonusType", 102.0), ("BonusType", True),
    ("BonusType", None),
])
def test_rows_outside_the_challenge_contract_are_refused(bundle, field, value):
    catalog = bundle.quests.catalog
    row = MappingProxyType({**catalog.row(2021), field: value})
    error = assert_refused("quest_definition_invalid", 503, lambda: POLICY.validate_row(row, catalog))
    assert field in str(error)


@pytest.mark.parametrize("bonus_type,item", GIFT_ITEMS.items())
def test_every_gift_bonus_type_pays_its_own_gift_item(bundle, bonus_type, item):
    catalog = bundle.quests.catalog
    row = MappingProxyType({**catalog.row(2021), "BonusType": bonus_type})
    assert POLICY.validate_row(row, catalog) is None
    assert catalog.star_rewards(row, False) == (StarReward("item", bonus_type, 1, item_id=item),)


def test_item_star_rewards_outside_the_gift_types_are_refused(bundle):
    catalog = bundle.quests.catalog
    row = MappingProxyType({**catalog.row(2021), "BonusType": ITEM_STAR_TYPE, "NormalMode_RewardItemID": RUBY})
    assert catalog.star_rewards(row, False) == (StarReward("item", ITEM_STAR_TYPE, 1, item_id=RUBY),)
    error = assert_refused("quest_definition_invalid", 503, lambda: POLICY.validate_row(row, catalog))
    assert "BonusType" in str(error)


def test_a_challenge_paying_another_gift_grants_that_gift_on_its_first_three_star_clear(challenger, monkeypatch):
    service, store = challenger
    replace_row(monkeypatch, service.catalog, 2001, {"BonusType": DIAMOND_TYPE})
    diamond = GIFT_ITEMS[DIAMOND_TYPE]
    owned = possession(store, diamond)
    service.start(start_request(service, store, 2001), store)
    assert store.profile().state["quest_state"]["active"]["mode"]["star_rewards"] == [
        {"kind": "item", "reward_type": DIAMOND_TYPE, "count": 1, "card_id": None, "item_id": diamond}]
    tree = nodes(service.finish(finish_request(store, 2001, selected=selected(store)), store))
    assert item_counts(tree) == {diamond: owned + 1} and possession(store, RUBY) == 0
    service.start(start_request(service, store, 2001, tick=1002), store)
    tree = nodes(service.finish(finish_request(store, 2001, tick=1003, selected=selected(store)), store))
    assert "POSSESSION" not in tree and possession(store, diamond) == owned + 1


@pytest.mark.parametrize("count", [0, -1])
def test_a_gift_star_reward_without_a_positive_count_is_refused(bundle, count):
    catalog = bundle.quests.catalog
    row = MappingProxyType({**catalog.row(2021), "BonusNum": count})
    error = assert_refused("quest_definition_invalid", 503, lambda: POLICY.validate_row(row, catalog))
    assert "gift item" in str(error)


@pytest.mark.parametrize("change,code,status", [
    ({"Treasure1": -1}, "quest_definition_invalid", 503), ({"Treasure4": "57"}, "quest_definition_invalid", 503),
    ({"Treasure5": INT32_MAX + 1}, "quest_definition_invalid", 503), ({"Treasure5": 5000}, "quest_reward_unknown", 503),
    ({"Treasure5": 1100054}, "quest_reward_unsupported", 501),
])
def test_challenge_treasures_outside_the_shared_reward_contract_are_refused(bundle, monkeypatch, change, code, status):
    catalog = bundle.quests.catalog
    replace_row(monkeypatch, catalog, 2028, change)
    assert_refused(code, status, lambda: catalog.get(2028))
    assert catalog.status()["mode_failed_quests"][2028]["code"] == code


@pytest.mark.parametrize("changes", [{"BonusNum": 5}, {"Treasure4": 0, "Treasure5": 0}, {"Treasure1": 2004}])
def test_boundary_rows_satisfy_the_challenge_contract(bundle, monkeypatch, changes):
    catalog = bundle.quests.catalog
    replace_row(monkeypatch, catalog, 2028, changes)
    quest, mode = catalog.get(2028), catalog.mode(2028)
    row = catalog.row(2028)
    assert quest.treasures == tuple(row[f"Treasure{slot}"] for slot in range(1, len(quest.treasures) + 1))
    assert mode.star_rewards_normal == (StarReward("item", STAR_GIFT_TYPE, row["BonusNum"], item_id=RUBY),)


def test_a_challenge_row_outside_the_contract_refuses_to_start_without_charging(challenger, monkeypatch):
    service, store = challenger
    catalog = service.catalog
    replace_row(monkeypatch, catalog, 2061, {"UnitList": 1})
    put_record(store, 2060)
    before = store.profile()
    assert_refused("quest_definition_invalid", 503, lambda: service.start(bare_start(service, store, 2061), store))
    assert store.profile() == before
    assert catalog.status()["mode_failed_quests"][2061]["code"] == "quest_definition_invalid"


def test_every_challenge_starts_and_settles_along_the_prerequisite_chain(challenger, bundle):
    service, store = challenger
    catalog, capacity = service.catalog, bundle.unit_capacity
    pending, cleared, tick, bonuses = list(CHALLENGE_QUESTS), {GATE_QUEST}, 1000, []
    while pending:
        quest = catalog.get(next(quest_id for quest_id in pending if catalog.get(quest_id).prerequisite in cleared))
        rewards = Counter()
        for candidate in quest.candidates:
            code = quest.treasures[candidate.slot - 1]
            rewards[code] += catalog.decode_reward(code).count
        decoded = {code: catalog.decode_reward(code) for code in rewards}
        rubies = possession(store, RUBY) + 1 + sum(count for code, count in rewards.items() if decoded[code].item_id == RUBY)
        units = len(card_ids(store)) + sum(count for code, count in rewards.items() if decoded[code].kind == "unit")
        service.start(start_request(service, store, quest.quest_id, tick), store)
        everything = (1 << len(quest.candidates)) - 1
        tree = nodes(service.finish(finish_request(store, quest.quest_id, tick + 1, obtained=everything,
                                                   selected=everything), store))
        assert drops(tree) == dict(rewards)
        assert (possession(store, RUBY), len(card_ids(store))) == (rubies, units)
        assert record(store, quest.quest_id)["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG | slot_bits(quest)
        bonuses.append(challenge_capacity(capacity, store.profile().state))
        pending.remove(quest.quest_id)
        cleared.add(quest.quest_id)
        tick += 2
    assert capacity.clear_limit == CLEAR_LIMIT
    assert bonuses == [CAPACITY_STEP * sum(clears >= threshold for threshold in CAPACITY_CLEARS)
                       for clears in range(1, len(CHALLENGE_QUESTS) + 1)]


def test_challenge_clears_count_toward_any_quest_missions_only(derived, bundle, tmp_path):
    assert "challenge" not in derived.missions.progress.clear_specs.status()["claimed_by"]
    initial = derived.missions.initialize(bundle.player_state.initialize(derived.bootstrap.initial_state()))
    store = Store(tmp_path / "missions.sqlite3", initial)
    allow_quest(store, GATE_QUEST)
    any_clear = [row.condition_id for row in derived.catalog.active(MISSION_NOW)
                 if row.condition_type == QUEST_CLEAR_CONDITION and row.quest_type == -2 and row.quest_id == 0
                 and row.period_type != SEQUENTIAL_PERIOD]
    quests = derived.quests
    quests.start(start_request(quests, store, 2001), store)
    quests.finish(finish_request(store, 2001, selected=selected(store)), store)
    snapshot = {row["ID"]: row for row in derived.missions.progress.snapshot(store.profile().state, MISSION_NOW)}
    assert any_clear and all(snapshot[key]["Progress"] == 1 for key in any_clear)
    assert snapshot[BEGINNER_WEEKDAY_MISSION]["Progress"] == 0
