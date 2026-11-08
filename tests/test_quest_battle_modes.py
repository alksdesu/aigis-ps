from copy import deepcopy
from dataclasses import replace
from datetime import datetime

import pytest

from aigis_ps.alxml import Column, Container
from aigis_ps.battle_events import EVENT_PARAM_LIMIT
from aigis_ps.business_calendar import civil_day
from aigis_ps.crystal_wallet import FREE_CRYSTAL_LIMIT, credit_free_crystals, wallet_totals
from aigis_ps.energy_clock import JST
from aigis_ps.quest_catalog import Reward, StarReward
from aigis_ps.quest_economy import STAR_GOLD_RATES
from aigis_ps.quest_modes import CLEAR_FLAG, HARD_STAR_FLAG, THREE_STAR_FLAG
from aigis_ps.quest_service import QuestRules
from aigis_ps.storage import Store
from test_mission_hooks import local
from test_quest_modes import disable_policy
from test_quest_service import (
    NOW, allow_quest, assert_error, bundle, finish_request, nodes, request, setup, start_request,
)


TREASURE_SLOTS = 0x10000 | 0x1000 | 0x100 | 0x1
MISSION_NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


def mutate(store, change):
    profile = store.profile()
    store.update_state(change(deepcopy(profile.state)), profile.revision)


def update_game_param(store, **values):
    def change(state):
        state["tables"]["game_param"][0].update(values)
        return state
    mutate(store, change)


def set_rank(bundle, store, rank):
    update_game_param(store, Rank=rank, Experience=sum(row["NextExp"] for row in bundle.bootstrap.rank_rows[:rank - 1]))


def put_record(store, quest_id, *, max_rank=3, flags=CLEAR_FLAG | THREE_STAR_FLAG | TREASURE_SLOTS):
    def change(state):
        rows = [row for row in state["tables"]["quest_records"] if row["QuestID"] != quest_id]
        rows.append({"QuestID": quest_id, "MaxRank": max_rank, "AchieveFlag": flags, "RemainAssault": 0,
                     "FirstDefeatDate": 0, "Shown": False})
        state["tables"]["quest_records"] = rows
        return state
    mutate(store, change)


def edit_active(store, change):
    def apply(state):
        change(state["quest_state"]["active"])
        return state
    mutate(store, apply)


def fill_wallet(store):
    mutate(store, lambda state: credit_free_crystals(state, FREE_CRYSTAL_LIMIT - wallet_totals(state)["free"], civil_day(NOW)))


def record(store, quest_id):
    return next(row for row in store.profile().state["tables"]["quest_records"] if row["QuestID"] == quest_id)


def card_ids(store):
    return [unit["CardID"] for unit in store.profile().state["player_state"]["units"]]


def charisma(store):
    return store.profile().state["tables"]["game_param"][0]["NowCharisma"]


def hard_start(service, store, quest_id=1002, tick=1000, number=82, *, boost=None):
    columns = (Column("QI", "I", (quest_id,)), Column("Rev", "S", (service.catalog.resource_version,)))
    if boost is not None:
        columns += (Container("Boost", (Column("V", "I", (boost,)),)),)
    return request(store, number, columns, tick=tick)


def bare_start(service, store, quest_id, tick=1000):
    return request(store, 81, (Column("QI", "I", (quest_id,)), Column("Rev", "S", (service.catalog.resource_version,))), tick=tick)


def gold_events(count):
    return (Container("GG", (Column("I", "I", (count,)),)),)


def three_star_gold(gold):
    return int(gold * STAR_GOLD_RATES[3])


def game_params(tree):
    columns = tree["PGP"].children
    return dict(zip(columns[0].values, columns[1].values))


def new_cards(tree):
    return tree["PPU"].children[0].values


class Recorder:
    def __init__(self):
        self.events = []

    def record(self, state, kind, payload, now):
        self.events.append((kind, deepcopy(payload)))
        return state


@pytest.fixture
def guides(setup):
    allow_quest(setup[1], 1004)
    return setup


@pytest.fixture
def unlocked(setup, bundle):
    service, store = setup
    allow_quest(store, 1001)
    put_record(store, 1002)
    set_rank(bundle, store, 50)
    return service, store


def test_tactical_guide_requires_the_native_menu_gate_quest(setup):
    service, store = setup
    before = store.profile()
    assert_error("quest_locked", lambda: service.start(start_request(service, store, 3001), store))
    assert store.profile() == before
    allow_quest(store, 1004)
    service.start(start_request(service, store, 3001, tick=1001), store)
    assert store.profile().state["quest_state"]["active"]["quest_id"] == 3001


def test_tactical_guide_grants_its_unit_once_on_the_first_three_star_result(guides):
    service, store = guides
    response = service.start(start_request(service, store, 3001), store)
    assert not nodes(response)["LOTTERY"].children[0].values
    active = store.profile().state["quest_state"]["active"]
    assert active["mode"]["quest_type"] == 4 and active["mode"]["difficulty"] == "normal"
    assert [grant["card_id"] for grant in active["unit_reservations"]] == [395]
    finishing = finish_request(store, 3001)
    response = service.finish(finishing, store)
    completed = store.profile()
    assert service.finish(finishing, store) == response and store.profile() == completed
    tree = nodes(response)
    assert new_cards(tree) == (395,) and card_ids(store).count(395) == 1
    assert tree["PBP"].children[1].values == (completed.state["player_state"]["battle"]["UX"],)
    assert record(store, 3001)["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG and record(store, 3001)["MaxRank"] == 3
    assert "PFC" not in tree and completed.state["quest_state"]["history"][-1]["star_rewards"] == active["mode"]["star_rewards"]
    service.start(start_request(service, store, 3001, tick=1002), store)
    assert not store.profile().state["quest_state"]["active"]["unit_reservations"]
    response = service.finish(finish_request(store, 3001, tick=1003), store)
    assert not new_cards(nodes(response)) and card_ids(store).count(395) == 1


def test_tactical_guide_low_stars_clear_without_the_unit_and_open_the_next_guide(guides):
    service, store = guides
    before = store.profile()
    assert_error("quest_locked", lambda: service.start(start_request(service, store, 3002), store))
    assert store.profile() == before
    service.start(start_request(service, store, 3001, tick=1001), store)
    assert not new_cards(nodes(service.finish(finish_request(store, 3001, tick=1002, stars=1), store)))
    assert record(store, 3001)["AchieveFlag"] == CLEAR_FLAG and record(store, 3001)["MaxRank"] == 1
    service.start(start_request(service, store, 3002, tick=1003), store)
    assert not new_cards(nodes(service.finish(finish_request(store, 3002, tick=1004, stars=2), store)))
    service.start(start_request(service, store, 3001, tick=1005), store)
    assert new_cards(nodes(service.finish(finish_request(store, 3001, tick=1006), store))) == (395,)
    assert record(store, 3001)["MaxRank"] == 3


def test_tactical_guide_pairs_77_with_80_and_refuses_four_star_starts(guides):
    service, store = guides
    assert_error("quest_hard_unavailable", lambda: service.start(hard_start(service, store, 3001), store))
    service.start(start_request(service, store, 3001, tick=1001, number=77), store)
    assert_error("quest_result_profile_mismatch", lambda: service.finish(finish_request(store, 3001, tick=1002), store))
    assert new_cards(nodes(service.finish(finish_request(store, 3001, tick=1002, number=80), store))) == (395,)


def test_tactical_guide_attempt_survives_reopening_and_replays_exactly(guides, tmp_path):
    service, store = guides
    service.start(start_request(service, store, 3001), store)
    reopened = Store(store.path)
    finishing = finish_request(reopened, 3001)
    response = service.finish(finishing, reopened)
    again = Store(store.path)
    assert service.finish(finishing, again) == response and again.profile() == reopened.profile()


def test_clearing_the_seventh_guide_completes_the_first_beginner_step_only(local, tmp_path):
    data = local.bundle
    store = Store(tmp_path / "beginner.sqlite3", local.missions.initialize(data.player_state.initialize(local.bootstrap.initial_state())))
    allow_quest(store, 1004)
    allow_quest(store, 3006)
    quests = local.quests
    quests.start(start_request(quests, store, 3007), store)
    quests.finish(finish_request(store, 3007), store)

    def rows():
        return {row["ID"]: row for row in local.missions.progress.snapshot(store.profile().state, MISSION_NOW)}

    assert rows()[10001]["Status"] == 1 and rows()[1605]["Status"] == 1
    mutate(store, lambda state: local.missions.progress.claim(state, [10001], MISSION_NOW))
    quests.start(start_request(quests, store, 1001, tick=1002), store)
    quests.finish(finish_request(store, 1001, tick=1003), store)
    assert rows()[10002]["Status"] == 0 and rows()[10002]["Progress"] == 0 and rows()[1606]["Status"] == 1


def test_four_star_start_requires_three_stars_every_treasure_and_rank(setup, bundle):
    service, store = setup
    allow_quest(store, 1001)
    for tick, (max_rank, slots, rank) in enumerate(((2, TREASURE_SLOTS, 50), (3, 0x11100, 50), (3, TREASURE_SLOTS, 49)), 1000):
        put_record(store, 1002, max_rank=max_rank, flags=CLEAR_FLAG | slots)
        set_rank(bundle, store, rank)
        before = store.profile()
        assert_error("quest_hard_locked", lambda: service.start(hard_start(service, store, tick=tick), store))
        assert store.profile() == before
    set_rank(bundle, store, 50)
    assert_error("quest_hard_unavailable", lambda: service.start(hard_start(service, store, 1001, tick=1010), store))


def test_four_star_result_grants_one_crystal_once_and_marks_the_record(unlocked):
    service, store = unlocked
    before = charisma(store)
    response = service.start(hard_start(service, store), store)
    assert not nodes(response)["LOTTERY"].children[0].values
    assert charisma(store) == before
    active = store.profile().state["quest_state"]["active"]
    assert active["registration"] == 82 and active["selected_mask"] == 0 and active["unit_reservations"] == []
    assert active["mode"] == {"quest_type": 0, "difficulty": "hard", "boost_ignored": None,
        "star_rewards": [{"kind": "crystal", "reward_type": 0, "count": 1, "card_id": None, "item_id": None}]}
    wallet = wallet_totals(store.profile().state)["free"]
    finishing = finish_request(store, 1002, stars=4)
    response = service.finish(finishing, store)
    completed = store.profile()
    assert service.finish(finishing, store) == response and store.profile() == completed
    tree = nodes(response)
    assert charisma(store) == before - service.catalog.get(1002).charisma
    assert "PFC" in tree and wallet_totals(completed.state)["free"] == wallet + 1
    assert not new_cards(tree) and not tree["TreasureInfo"].children[0].values
    row = record(store, 1002)
    assert row["MaxRank"] == 4 and row["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG | HARD_STAR_FLAG | TREASURE_SLOTS
    history = completed.state["quest_state"]["history"][-1]
    assert (history["difficulty"], history["crystals"], history["crystal_presents"]) == ("hard", 1, [])
    service.start(hard_start(service, store, tick=1002), store)
    response = service.finish(finish_request(store, 1002, tick=1003, stars=4), store)
    assert "PFC" not in nodes(response) and wallet_totals(store.profile().state)["free"] == wallet + 1
    assert charisma(store) == before - 2 * service.catalog.get(1002).charisma


def test_low_four_star_results_neither_reward_nor_mark_and_three_stars_are_invalid(unlocked):
    service, store = unlocked
    for tick, stars in ((1000, 1), (1002, 2)):
        service.start(hard_start(service, store, tick=tick), store)
        assert "PFC" not in nodes(service.finish(finish_request(store, 1002, tick=tick + 1, stars=stars), store))
    row = record(store, 1002)
    assert row["MaxRank"] == 3 and not row["AchieveFlag"] & HARD_STAR_FLAG
    service.start(hard_start(service, store, tick=1004), store)
    assert_error("quest_result_invalid", lambda: service.finish(finish_request(store, 1002, tick=1005, stars=3), store))
    assert_error("quest_result_profile_mismatch",
                 lambda: service.finish(finish_request(store, 1002, tick=1005, number=80, stars=4), store))


def test_four_star_crystal_beyond_the_wallet_becomes_one_present(unlocked, bundle):
    service, store = unlocked
    service.presents = bundle.presents
    fill_wallet(store)
    service.start(hard_start(service, store), store)
    response = service.finish(finish_request(store, 1002, stars=4), store)
    tree = nodes(response)
    assert "PFC" not in tree and "PPT" in tree
    saved = store.profile().state
    assert wallet_totals(saved)["free"] == FREE_CRYSTAL_LIMIT
    added = saved["quest_state"]["history"][-1]["crystal_presents"]
    rows = {row["PresentID"]: row for row in bundle.presents.rows(saved)}
    assert len(added) == 1 and (rows[added[0]]["Type"], rows[added[0]]["Content"]) == (0, 1)
    assert rows[added[0]]["Description"] == "★４チャレンジ報酬"


def test_wallet_overflow_without_a_present_service_refuses_before_charging(unlocked):
    service, store = unlocked
    fill_wallet(store)
    before = store.profile()
    assert_error("quest_present_unavailable", lambda: service.start(hard_start(service, store), store))
    assert store.profile() == before


def test_residual_boost_is_ignored_and_the_entry_cost_is_charged_once(unlocked):
    service, store = unlocked
    before = charisma(store)
    starting = hard_start(service, store, number=78, boost=3)
    response = service.start(starting, store)
    assert service.start(starting, store) == response
    assert charisma(store) == before
    assert store.profile().state["quest_state"]["active"]["mode"]["boost_ignored"] == 3
    assert_error("quest_result_profile_mismatch", lambda: service.finish(finish_request(store, 1002, stars=4), store))
    assert charisma(store) == before and record(store, 1002)["MaxRank"] == 3
    service.finish(finish_request(store, 1002, number=80, stars=4), store)
    assert charisma(store) == before - service.catalog.get(1002).charisma
    assert record(store, 1002)["MaxRank"] == 4


def test_attempt_saved_before_modes_settles_as_an_ordinary_main_quest(setup):
    service, store = setup
    recorder = Recorder()
    service.missions = recorder
    service.start(start_request(service, store), store)

    def legacy(active):
        del active["mode"]
        del active["mission_id"]
        del active["charge"]
        del active["rules"]["gold_per_event"]
        del active["rules"]["max_gold_events"]

    edit_active(store, legacy)
    default_cap = QuestRules().max_gold_events
    reported = default_cap + 1
    tree = nodes(service.finish(finish_request(store, gold=reported, extra=gold_events(reported)), store))
    assert tree["REWARD"].children[1].values == (three_star_gold(service.catalog.get(1001).gold),)
    assert "PFC" in tree and record(store, 1001)["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG
    history = store.profile().state["quest_state"]["history"][-1]
    assert history["gold_events"] == default_cap and history["mission_id"] is None
    assert [(kind, payload["mission_id"]) for kind, payload in recorder.events] == [("quest_clear", None)]


def test_attempt_without_a_saved_mode_must_be_an_ordinary_main_quest(unlocked):
    service, store = unlocked
    service.start(hard_start(service, store), store)
    edit_active(store, lambda active: active.pop("mode"))
    assert_error("quest_state_invalid", lambda: service.finish(finish_request(store, 1002, stars=4), store))


@pytest.mark.parametrize("key,value", [
    ("difficulty", "hard"), ("difficulty", "easy"), ("quest_type", 4), ("boost_ignored", 0), ("boost_ignored", True),
    ("star_rewards", [{"kind": "crystal", "reward_type": 0, "count": 99, "card_id": None, "item_id": None}]),
    ("extra", 1)])
def test_tampered_saved_mode_is_refused(setup, key, value):
    service, store = setup
    service.start(start_request(service, store), store)
    edit_active(store, lambda active: active["mode"].update({key: value}))
    assert_error("quest_state_invalid", lambda: service.finish(finish_request(store), store))


@pytest.mark.parametrize("gold,events,credited", [(3, 5, 3), (4, 1, 1), (2, None, 0), (0, 4, 0)])
def test_gold_events_credit_the_smaller_of_goldget_and_gg_records(setup, gold, events, credited):
    service, store = setup
    service.start(start_request(service, store), store)
    tree = nodes(service.finish(finish_request(store, gold=gold, extra=() if events is None else gold_events(events)), store))
    settled = three_star_gold(service.catalog.get(1001).gold + credited * service.rules.gold_per_event)
    assert tree["REWARD"].children[1].values == (settled,)
    assert store.profile().state["quest_state"]["history"][-1]["gold_events"] == credited
    assert "AF" not in game_params(tree)


def test_gold_exempt_modes_ignore_gold_events(setup, monkeypatch):
    service, store = setup
    original = service.catalog.mode
    monkeypatch.setattr(service.catalog, "mode", lambda quest_id: replace(original(quest_id), gold_counted=False))
    service.start(start_request(service, store), store)
    tree = nodes(service.finish(finish_request(store, gold=3, extra=gold_events(3)), store))
    assert tree["REWARD"].children[1].values == (three_star_gold(service.catalog.get(1001).gold),)


def test_item_star_rewards_stop_at_the_stack_and_report_the_discarded_part(setup, bundle, monkeypatch):
    service, store = setup
    item = "gift_0002_ruby"
    stack = bundle.possessions.stack(item)
    mutate(store, lambda state: bundle.possessions.credit(state, {item: stack - 2}))
    original = service.catalog.mode

    def mode(quest_id):
        described = original(quest_id)
        return replace(described, star_rewards_normal=described.star_rewards_normal + (StarReward("item", 102, 5, item_id=item),))

    monkeypatch.setattr(service.catalog, "mode", mode)
    service.start(start_request(service, store), store)
    tree = nodes(service.finish(finish_request(store), store))
    assert dict(zip(tree["IOF"].children[0].values, tree["IOF"].children[1].values)) == {item: 3}
    assert dict(zip(tree["POSSESSION"].children[0].values, tree["POSSESSION"].children[1].values)) == {item: stack}
    saved = store.profile().state
    assert {row["ItemID"]: row["Count"] for row in saved["tables"]["possessions"]}[item] == stack
    assert saved["quest_state"]["history"][-1]["discarded"] == {item: 3}


@pytest.mark.parametrize("saved", [1500, EVENT_PARAM_LIMIT])
def test_event_drops_report_the_capped_event_param_even_when_it_stays_at_the_cap(setup, monkeypatch, saved):
    service, store = setup
    allow_quest(store, 1001)
    original = service.catalog.decode_reward
    monkeypatch.setattr(service.catalog, "decode_reward",
                        lambda code: Reward("event_param", code, 700) if code == 67 else original(code))
    update_game_param(store, EventParam=saved)
    service.start(start_request(service, store, 1002), store)
    tree = nodes(service.finish(finish_request(store, 1002, obtained=2, selected=7, stars=1), store))
    assert game_params(tree)["AF"] == str(EVENT_PARAM_LIMIT)
    assert store.profile().state["tables"]["game_param"][0]["EventParam"] == EVENT_PARAM_LIMIT
    assert not new_cards(tree) and tree["TreasureInfo"].children[1].values == (67,)


@pytest.mark.parametrize("quest_id", [4453, 6401, 6314, 10001])
def test_reserved_modes_refuse_to_start_without_charging(setup, monkeypatch, quest_id):
    service, store = setup
    disable_policy(monkeypatch, service.catalog.quest_type(quest_id))
    before = store.profile()
    assert_error("quest_mode_unsupported", lambda: service.start(bare_start(service, store, quest_id), store))
    assert store.profile() == before
