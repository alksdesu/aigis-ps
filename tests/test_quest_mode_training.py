from datetime import timedelta

import pytest

from aigis_ps import quest_modes
from aigis_ps.alxml import loads
from aigis_ps.business_calendar import business_day
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.errors import ProtocolError
from aigis_ps.quest_catalog import QuestCatalog, StarReward
from aigis_ps.quest_mode_training import POLICY
from aigis_ps.quest_modes import CLEAR_FLAG, ENDED, OPEN, SLOT_FLAGS, THREE_STAR_FLAG
from aigis_ps.quest_service import QuestService
from aigis_ps.storage import Store
from test_quest_battle_modes import (
    MISSION_NOW, TREASURE_SLOTS, bare_start, charisma, gold_events, hard_start, mutate, new_cards, put_record, record,
    update_game_param,
)
from test_quest_service import (
    NOW, allow_quest, assert_error, bundle, exit_request, finish_request, nodes, setup, start_request,
)
from test_quest_settlement import derived


PRACTICE = 9901
PRACTICE_MISSION = 910001
PRACTICE_QUESTS = tuple(range(9901, 9908))
ENEMY_TEST_MISSION = 910002
ENEMY_TESTS = tuple(range(9911, 9920))
MISSION_QUESTS = {PRACTICE_MISSION: PRACTICE_QUESTS, ENEMY_TEST_MISSION: ENEMY_TESTS}
PRIZE_SLOT_QUEST = 9918
PRIZE_SLOTS = (1, 2, 2, 3, 3, 4, 5)
BEGINNER_PRACTICE_STEP = 10008
DAILY_TWO_CLEARS = 1606
BOOST_CLOSE = "2026/11/01 12:00:01"
NO_REWARD = {"kind": "none", "reward_type": 4, "count": 0, "card_id": None, "item_id": None}
START_CHILDREN = ["SYSTEM", "PGP", "LOTTERY", "REWARD", "TREASURE", "TreasureInfo", "PPU"]
FINISH_CHILDREN = ["SYSTEM", "PGP", "PBP", "PPU", "QR", "REWARD", "TREASURE", "TreasureInfo"]
ECONOMY_FIELDS = ("Money", "Experience", "Rank", "NowCharisma", "NowActionPoint")
CLEARED = (3, CLEAR_FLAG | THREE_STAR_FLAG)


@pytest.fixture
def practice(setup):
    allow_quest(setup[1], 1004)
    return setup


def refusal(callback):
    with pytest.raises(ProtocolError) as error:
        callback()
    return error.value.code, error.value.status, str(error.value)


def refused(code, status, callback):
    assert refusal(callback)[:2] == (code, status)


def attempt(store):
    return store.profile().state["quest_state"]["active"]


def slot_bits(slots):
    return sum(SLOT_FLAGS[slot - 1] for slot in set(slots))


def practice_start(service, store, quest_id=PRACTICE, tick=1000):
    return start_request(service, store, quest_id, tick)


def clear(service, store, quest_id=PRACTICE, tick=1000, **result):
    service.start(practice_start(service, store, quest_id, tick), store)
    result.setdefault("selected", attempt(store)["selected_mask"])
    return nodes(service.finish(finish_request(store, quest_id, tick=tick + 1, **result), store))


def child_names(response):
    return [child.name for child in loads(response).children]


def economy(store):
    row = store.profile().state["tables"]["game_param"][0]
    return {name: row[name] for name in ECONOMY_FIELDS}


def results(store, quest_ids):
    saved = {row["QuestID"]: (row["MaxRank"], row["AchieveFlag"]) for row in store.profile().state["tables"]["quest_records"]}
    return {quest_id: saved.get(quest_id) for quest_id in quest_ids}


def zero_reward(tree):
    return tree["REWARD"].children[0].values == tree["REWARD"].children[1].values == (0,)


def boost_experience(store):
    mutate(store, lambda state: state["tables"]["config"][0].update(ExperienceUpCloseDateTime=BOOST_CLOSE) or state)


def fresh_catalog(bundle, overrides=None):
    shared = bundle.quests.catalog
    return QuestCatalog(shared.snapshot, lambda name: bundle.native_cdn.assets.read(name)[0],
                        possessions=bundle.possessions, resource_version=shared.resource_version,
                        window_overrides=overrides)


def reopened_enemy_tests(bundle):
    return fresh_catalog(bundle, {ENEMY_TEST_MISSION: (MISSION_NOW - timedelta(days=1), MISSION_NOW + timedelta(days=1))})


def service_for(bundle, catalog):
    return QuestService(catalog, bundle.bootstrap, bundle.player_state, bundle.possessions,
                        clock=lambda: NOW, randbelow=lambda _: 0)


def test_training_policy_opens_type_15_behind_the_1004_gate_without_four_star_or_daily_rules():
    assert quest_modes.policy(15) is POLICY
    assert POLICY.status() == {"policy": "training", "enabled": True, "reason": None, "hard_mode": False,
                               "timed_conditions": False, "gate_quest": 1004}
    support = quest_modes.support_status()[15]
    assert (support["mission_category"], support["mission_resources"], support["gold_counted"],
            support["score_mode"], support["hard_rank"], support["entrust"]) == (13, ["Training"], False, False, None, False)


def test_every_listed_practice_quest_builds_a_free_reward_less_definition_from_its_mission_map(bundle):
    catalog = bundle.quests.catalog
    listed = catalog.playable_quests(15)
    assert listed == PRACTICE_QUESTS + ENEMY_TESTS
    for mission_id, quest_ids in MISSION_QUESTS.items():
        for quest_id in quest_ids:
            quest, mode = catalog.get(quest_id), catalog.mode(quest_id)
            assert (quest.charisma, quest.action_points, quest.rank, quest.rank_exp, quest.gold) == (0, 0, 1, 0, 0)
            assert quest.treasures == (0,) * 5 and quest.unit_list == 0
            assert (mode.mission_category, mode.mission_ids, mode.hard, mode.gold_counted, mode.score_mode) == (
                13, (mission_id,), None, False, False)
            assert mode.star_rewards_normal == (StarReward("none", 4, 0),) and mode.star_rewards_hard == ()
            assert (mode.map_archive, mode.map_mission_id) == (f"Map{mission_id}_{quest.map_no:04d}.aar", mission_id)
    slots = {quest_id: tuple(row.slot for row in catalog.get(quest_id).candidates) for quest_id in listed}
    assert slots == {**dict.fromkeys(listed, ()), PRIZE_SLOT_QUEST: PRIZE_SLOTS}
    assert not set(listed) & set(catalog.status()["mode_failed_quests"])


def test_practice_requires_a_cleared_1004_record(setup):
    service, store = setup
    before = store.profile()
    assert_error("quest_locked", lambda: service.start(start_request(service, store, PRACTICE), store))
    assert store.profile() == before
    put_record(store, 1004, flags=THREE_STAR_FLAG | TREASURE_SLOTS)
    before = store.profile()
    assert_error("quest_locked", lambda: service.start(start_request(service, store, PRACTICE, tick=1001), store))
    assert store.profile() == before
    put_record(store, 1004, max_rank=1, flags=CLEAR_FLAG)
    service.start(start_request(service, store, PRACTICE, tick=1002), store)
    assert attempt(store)["quest_id"] == PRACTICE


def test_practice_starts_without_charisma_and_saves_the_practice_mission(practice):
    service, store = practice
    update_game_param(store, NowCharisma=0, NowActionPoint=0)
    before = store.profile()
    assert_error("quest_energy_insufficient", lambda: service.start(start_request(service, store), store))
    assert store.profile() == before
    response = service.start(start_request(service, store, PRACTICE, tick=1001), store)
    tree = nodes(response)
    assert child_names(response) == START_CHILDREN
    assert not tree["LOTTERY"].children[0].values and not tree["PPU"].children and zero_reward(tree)
    assert charisma(store) == 0
    active = attempt(store)
    assert (active["mission_id"], active["selected_mask"], active["unit_reservations"]) == (PRACTICE_MISSION, 0, [])
    assert active["mode"] == {"quest_type": 15, "difficulty": "normal", "star_rewards": [NO_REWARD], "boost_ignored": None}


def test_practice_clear_grants_no_experience_gold_or_star_reward_and_marks_the_record(practice):
    service, store = practice
    boost_experience(store)
    service.start(start_request(service, store, PRACTICE), store)
    started, wallet = economy(store), wallet_totals(store.profile().state)
    finishing = finish_request(store, PRACTICE, gold=5, extra=gold_events(5))
    response = service.finish(finishing, store)
    completed = store.profile()
    assert service.finish(finishing, store) == response and store.profile() == completed
    tree = nodes(response)
    assert child_names(response) == FINISH_CHILDREN
    assert zero_reward(tree) and not new_cards(tree) and not tree["TreasureInfo"].children[0].values
    assert economy(store) == started and wallet_totals(completed.state) == wallet
    row = record(store, PRACTICE)
    assert (row["MaxRank"], row["AchieveFlag"], row["FirstDefeatDate"]) == (3, CLEAR_FLAG | THREE_STAR_FLAG, business_day(NOW))
    history = completed.state["quest_state"]["history"][-1]
    assert (history["gold_events"], history["rewards"], history["crystals"], history["mission_id"]) == (0, {}, 0, PRACTICE_MISSION)
    assert history["star_rewards"] == [NO_REWARD] and history["mode_presents"] == []
    assert zero_reward(clear(service, store, tick=1002))
    assert store.profile().state["quest_state"]["history"][-1]["star_rewards"] == []
    assert record(store, PRACTICE)["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG


def test_every_listed_practice_quest_starts_and_settles_without_rewards(practice):
    service, store = practice
    before = economy(store)
    for index, quest_id in enumerate(PRACTICE_QUESTS):
        assert zero_reward(clear(service, store, quest_id, tick=1000 + 2 * index))
    assert economy(store) == before
    assert results(store, PRACTICE_QUESTS) == dict.fromkeys(PRACTICE_QUESTS, CLEARED)


@pytest.mark.parametrize("result,kind", [(-1, "retreat"), (0, "failure")])
def test_practice_settles_ordinary_low_star_results_and_closes_exits_without_clearing(practice, result, kind):
    service, store = practice
    service.start(start_request(service, store, PRACTICE), store)
    before = store.profile()
    assert_error("quest_result_invalid", lambda: service.finish(finish_request(store, PRACTICE, stars=0), store))
    assert store.profile() == before
    service.exit(exit_request(store, PRACTICE, result), store)
    saved = store.profile().state
    assert saved["quest_state"]["active"] is None and saved["quest_state"]["history"][-1]["kind"] == kind
    assert all(row["QuestID"] != PRACTICE for row in saved["tables"]["quest_records"])
    for tick, stars in ((1001, 2), (1003, 1)):
        assert zero_reward(clear(service, store, tick=tick, stars=stars))
    assert (record(store, PRACTICE)["MaxRank"], record(store, PRACTICE)["AchieveFlag"]) == (2, CLEAR_FLAG)


def test_practice_refuses_four_star_starts_and_pairs_77_with_80(practice):
    service, store = practice
    before = store.profile()
    for number in (78, 82):
        assert_error("quest_hard_unavailable", lambda: service.start(hard_start(service, store, PRACTICE, number=number), store))
    assert store.profile() == before
    service.start(start_request(service, store, PRACTICE, tick=1001, number=77), store)
    assert_error("quest_result_profile_mismatch", lambda: service.finish(finish_request(store, PRACTICE, tick=1002), store))
    service.finish(finish_request(store, PRACTICE, tick=1002, number=80), store)
    assert record(store, PRACTICE)["MaxRank"] == 3


def test_enemy_test_quests_play_behind_the_gate_inside_their_native_window(setup):
    service, store = setup
    mission = service.catalog.mission_windows(15).mission(ENEMY_TEST_MISSION)
    inside = mission.end - timedelta(seconds=1)
    assert mission.state(inside) == OPEN
    service.clock = lambda: inside
    update_game_param(store, NowCharisma=0, NowActionPoint=0)
    before = store.profile()
    for quest_id in ENEMY_TESTS:
        refused("quest_locked", 409, lambda: service.start(practice_start(service, store, quest_id), store))
    assert store.profile() == before
    allow_quest(store, 1004)
    started = economy(store)
    for index, quest_id in enumerate(ENEMY_TESTS):
        assert zero_reward(clear(service, store, quest_id, tick=1000 + 2 * index))
        assert store.profile().state["quest_state"]["history"][-1]["mission_id"] == ENEMY_TEST_MISSION
    assert economy(store) == started
    assert results(store, ENEMY_TESTS) == dict.fromkeys(ENEMY_TESTS, CLEARED)


@pytest.mark.parametrize("at_end", [True, False])
def test_enemy_test_quests_after_their_native_window_get_the_closed_mission_refusal(practice, bundle, at_end):
    service, store = practice
    mission = service.catalog.mission_windows(15).mission(ENEMY_TEST_MISSION)
    after = mission.end if at_end else MISSION_NOW
    assert mission.state(after) == ENDED
    service.clock = lambda: after
    past = (MISSION_NOW - timedelta(days=2), MISSION_NOW - timedelta(days=1))
    closed = service_for(bundle, fresh_catalog(bundle, {PRACTICE_MISSION: past}))
    expected = refusal(lambda: closed.start(practice_start(closed, store), store))
    assert expected[:2] == ("quest_closed", 409)
    before = store.profile()
    for quest_id in ENEMY_TESTS:
        assert refusal(lambda: service.start(practice_start(service, store, quest_id), store)) == expected
    assert store.profile() == before
    assert not set(ENEMY_TESTS) & set(service.catalog.status()["mode_failed_quests"])


def test_window_overrides_reopen_enemy_test_quests_as_ordinary_practice_quests(practice, bundle):
    _, store = practice
    reopened = reopened_enemy_tests(bundle)
    service = service_for(bundle, reopened)
    assert reopened.mission_windows(15).mission(ENEMY_TEST_MISSION).state(MISSION_NOW) == OPEN
    update_game_param(store, NowCharisma=0, NowActionPoint=0)
    boost_experience(store)
    started, wallet = economy(store), wallet_totals(store.profile().state)
    for index, quest_id in enumerate(ENEMY_TESTS):
        tick = 1000 + 2 * index
        response = service.start(practice_start(service, store, quest_id, tick), store)
        assert child_names(response) == START_CHILDREN and zero_reward(nodes(response))
        active = attempt(store)
        assert (active["mission_id"], active["unit_reservations"]) == (ENEMY_TEST_MISSION, [])
        assert active["mode"] == {"quest_type": 15, "difficulty": "normal", "star_rewards": [NO_REWARD], "boost_ignored": None}
        response = service.finish(finish_request(store, quest_id, tick=tick + 1, selected=active["selected_mask"],
                                                 gold=5, extra=gold_events(5)), store)
        tree = nodes(response)
        assert child_names(response) == FINISH_CHILDREN
        assert zero_reward(tree) and not new_cards(tree) and not tree["TreasureInfo"].children[0].values
        history = store.profile().state["quest_state"]["history"][-1]
        assert (history["gold_events"], history["rewards"], history["crystals"], history["mission_id"]) == (
            0, {}, 0, ENEMY_TEST_MISSION)
        assert history["star_rewards"] == [NO_REWARD] and history["mode_presents"] == []
    assert economy(store) == started and wallet_totals(store.profile().state) == wallet
    assert results(store, ENEMY_TESTS) == dict.fromkeys(ENEMY_TESTS, CLEARED)
    assert not set(ENEMY_TESTS) & set(reopened.status()["mode_failed_quests"])


def test_enemy_test_prize_slots_over_empty_treasures_only_mark_the_record(practice, bundle):
    _, store = practice
    service = service_for(bundle, reopened_enemy_tests(bundle))
    quest = service.catalog.get(PRIZE_SLOT_QUEST)
    assert quest.treasures == (0,) * 5 and tuple(row.slot for row in quest.candidates) == PRIZE_SLOTS
    started = economy(store)
    first_slots, every_candidate = (2, 4), (1 << len(PRIZE_SLOTS)) - 1
    unmarked = [(index, slot, 0) for index, slot in enumerate(PRIZE_SLOTS)]
    marked = [(index, slot, int(slot in first_slots)) for index, slot in enumerate(PRIZE_SLOTS)]

    def settle(sent, obtained, tick):
        lottery = nodes(service.start(start_request(service, store, PRIZE_SLOT_QUEST, tick=tick,
                                                    candidates=sent), store))["LOTTERY"]
        assert lottery.children[1].values == (1,) * len(PRIZE_SLOTS) and attempt(store)["selected_mask"] == every_candidate
        response = service.finish(finish_request(store, PRIZE_SLOT_QUEST, tick=tick + 1, obtained=obtained,
                                                 selected=every_candidate), store)
        tree = nodes(response)
        assert child_names(response) == FINISH_CHILDREN
        assert zero_reward(tree) and not new_cards(tree) and not tree["TreasureInfo"].children[0].values
        assert store.profile().state["quest_state"]["history"][-1]["rewards"] == {}
        return record(store, PRIZE_SLOT_QUEST)["AchieveFlag"]

    first_obtained = sum(1 << PRIZE_SLOTS.index(slot) for slot in first_slots)
    assert settle(unmarked, first_obtained, 1000) == CLEAR_FLAG | THREE_STAR_FLAG | slot_bits(first_slots)
    before = store.profile()
    refused("quest_candidates_mismatch", 409, lambda: service.start(
        start_request(service, store, PRIZE_SLOT_QUEST, tick=1002, candidates=unmarked), store))
    assert store.profile() == before
    assert settle(marked, every_candidate, 1003) == CLEAR_FLAG | THREE_STAR_FLAG | slot_bits(PRIZE_SLOTS)
    assert economy(store) == started


@pytest.mark.parametrize("change", [{"Type": 4}, {"Type": True}, {"UnitList": 1}, {"UnitList": False},
                                    {"BonusType": 0}, {"BonusType": 102}, {"BonusType": None}])
def test_practice_rows_outside_the_reward_free_contract_are_refused(bundle, change):
    catalog = bundle.quests.catalog
    row = catalog.row(PRACTICE)
    assert POLICY.validate_row(row, catalog) is None
    refused("quest_definition_invalid", 503, lambda: POLICY.validate_row({**row, **change}, catalog))


def test_every_listed_practice_row_satisfies_the_reward_free_contract(bundle):
    catalog = bundle.quests.catalog
    for quest_id in catalog.playable_quests(15):
        assert POLICY.validate_row(catalog.row(quest_id), catalog) is None


def test_a_practice_row_with_a_star_reward_is_refused_before_charging(practice, bundle):
    _, store = practice
    catalog, quest_id = fresh_catalog(bundle), PRACTICE_QUESTS[1]
    catalog._all_rows[quest_id] = {**catalog._all_rows[quest_id], "BonusType": 0}
    service = service_for(bundle, catalog)
    before = store.profile()
    refused("quest_definition_invalid", 503, lambda: service.start(bare_start(service, store, quest_id), store))
    assert store.profile() == before
    assert catalog.status()["mode_failed_quests"][quest_id]["code"] == "quest_definition_invalid"


def claim_beginner_steps_before(progress, state, target):
    result, _, timestamp, shifted, active, indexed = progress._context(state, MISSION_NOW)
    sequence = progress.catalog.sequence
    for identifier in sequence[:sequence.index(target)]:
        entry = progress._entry(indexed, active[identifier], shifted)
        entry.update(progress=entry["goal"], status=2)
        indexed[(identifier, entry["period"])] = entry
    result["mission_state"]["entries"] = [indexed[key] for key in sorted(indexed)]
    result["mission_state"]["last_time"] = timestamp
    return result


def test_practice_clears_count_for_the_beginner_practice_step_through_the_training_spec(derived, bundle, tmp_path):
    spec = derived.missions.progress.clear_specs.get(BEGINNER_PRACTICE_STEP)
    assert (spec.claimed_by, spec.quest_types, spec.quest_ids, spec.resolved) == ("training", (15,), None, True)
    initial = derived.missions.initialize(bundle.player_state.initialize(derived.bootstrap.initial_state()))
    store = Store(tmp_path / "practice.sqlite3",
                  claim_beginner_steps_before(derived.missions.progress, initial, BEGINNER_PRACTICE_STEP))
    allow_quest(store, 1004)
    quests = derived.quests

    def progress(identifier):
        rows = derived.missions.progress.snapshot(store.profile().state, MISSION_NOW)
        return next((row["Status"], row["Progress"]) for row in rows if row["ID"] == identifier)

    quests.start(start_request(quests, store, 1001), store)
    quests.finish(finish_request(store, 1001, selected=attempt(store)["selected_mask"]), store)
    assert progress(BEGINNER_PRACTICE_STEP) == (0, 0) and progress(DAILY_TWO_CLEARS) == (0, 1)
    clear(quests, store, tick=1002, stars=1)
    assert progress(BEGINNER_PRACTICE_STEP) == (1, 1) and progress(DAILY_TWO_CLEARS) == (1, 2)
