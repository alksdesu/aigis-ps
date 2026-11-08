from collections import Counter
from datetime import timedelta
from types import MappingProxyType

import pytest

from aigis_ps import quest_modes
from aigis_ps.alxml import loads
from aigis_ps.battle_events import EVENT_PARAM_LIMIT
from aigis_ps.business_calendar import business_day
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.drop_bonus import WEEKDAYS
from aigis_ps.errors import ProtocolError
from aigis_ps.history_disclosure import DISCLOSURE_TABLE
from aigis_ps.quest_catalog import QuestCatalog, StarReward
from aigis_ps.quest_mode_history import POLICY
from aigis_ps.quest_modes import CLEAR_FLAG, SLOT_FLAGS, THREE_STAR_FLAG
from aigis_ps.quest_service import QuestService
from aigis_ps.storage import Store
from test_history_disclosure import GROUP_CLOSED, STARS_SHORT, disclose, start_on
from test_quest_battle_modes import (
    MISSION_NOW, bare_start, charisma, gold_events, hard_start, mutate, new_cards, put_record, record, set_rank,
    three_star_gold, update_game_param,
)
from test_quest_service import (
    NOW, allow_quest, assert_error, bundle, client_candidates, finish_request, nodes, setup, start_request,
)
from test_quest_settlement import bonus_values, derived, drops, item_counts, possession


GATE_QUEST = 1004
CHARISMA = 1000
INT32_MAX = 0x7FFFFFFF
COLLECTION_BIT = 1
HISTORY_QUESTS = 2016
CHAINED_QUESTS = 1775
QUEST_LIST = "HistoryMissionQuestList.atb"
FIRST_QUEST = 1
FIRST_MISSION = 920001
DISCLOSURE_RANK = 70
DROP_QUEST = 45
DROP_MISSION = 920004
LIST_ONLY_QUEST = 4125
LIST_ONLY_MISSION = 920025
LIST_ONLY_RANK = 9
TICKET_QUEST, TICKET_MISSION = 7606, 920176
TICKET_RANK = 5
TICKET = "item_smn_ticket_40017"
PROLOGUE = (9998, 9999)
PROLOGUE_MISSION = 920000
BEGINNER_MISSION = 10009
BOUQUET = "gift_0000_bouquet"
CARVED_CRYSTAL = "trade_item_002000"
EVENT_POINTS = 3001
EVOLVE_ORB = 4001
UNKNOWN_CODE = 999999
DECODABLE_CODES = MappingProxyType({1: 8, 2: 1001654, 3: 1001, 4: 2001, 5: EVENT_POINTS, 6: EVOLVE_ORB, 7: 5003, 8: 10021})
EVENT_PARAM_DROP_TYPE, EVOLVE_ORB_DROP_TYPE = 5, 6
EVENT_SLOT, ORB_SLOT = 3, 4
MODE_CACHES = ("_mode_definitions", "_modes", "_mode_failures")
CRYSTAL_REWARD = {"kind": "crystal", "reward_type": 0, "count": 0, "card_id": None, "item_id": None}
NO_REWARD = {"kind": "none", "reward_type": 4, "count": 0, "card_id": None, "item_id": None}
FINISH_CHILDREN = ["SYSTEM", "PGP", "PBP", "PPU", "QR", "REWARD", "TREASURE", "TreasureInfo", "POSSESSION", "PFC"]
# Quest 13 drops 5002 (three carved crystals) from three slot 5 rows; its DropBonusConfig row scales them by 1.5.
CRYSTAL_QUEST, CRYSTAL_PREREQUISITE, CRYSTAL_RANK, CRYSTAL_MISSION = 13, 12, 9, 920002
CRYSTAL_DROP, CRYSTAL_DROPS, BONUS_CRYSTALS = 5002, 9, 13
CARVED_TYPE, BONUS_VALUE = 1, 1.5
BONUS_DAYS = frozenset(("Mon", "Wed", "Sat"))


@pytest.fixture
def history(setup):
    service, store = setup
    allow_quest(store, GATE_QUEST)
    update_game_param(store, NowCharisma=CHARISMA)
    return service, store


def selected(store):
    return store.profile().state["quest_state"]["active"]["selected_mask"]


def clear(service, store, quest_id, tick=1000, **result):
    service.start(start_request(service, store, quest_id, tick), store)
    mask = selected(store)
    return nodes(service.finish(finish_request(store, quest_id, tick + 1, obtained=mask, selected=mask, **result), store))


def refused(code, status, callback):
    with pytest.raises(ProtocolError) as error:
        callback()
    assert (error.value.code, error.value.status) == (code, status)


def undisclosed(service, store, quest_id, message, tick=1000):
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.start(start_request(service, store, quest_id, tick), store)
    assert (error.value.code, error.value.status, str(error.value)) == ("quest_closed", 409, message)
    assert store.profile() == before


def fresh_service(bundle):
    shared = bundle.quests.catalog
    catalog = QuestCatalog(shared.snapshot, lambda name: bundle.native_cdn.assets.read(name)[0],
                           possessions=bundle.possessions, resource_version=shared.resource_version)
    return QuestService(catalog, bundle.bootstrap, bundle.player_state, bundle.possessions,
                        clock=lambda: NOW, randbelow=lambda _: 0)


def replace_row(monkeypatch, catalog, quest_id, change):
    monkeypatch.setitem(catalog._all_rows, quest_id, {**catalog._all_rows[quest_id], **change})
    for cache in MODE_CACHES:
        monkeypatch.setattr(catalog, cache, {})


def expected_drops(catalog, quest):
    counts = Counter()
    for candidate in quest.candidates:
        if code := quest.reward_code(candidate):
            counts[code] += catalog.decode_reward(code).count
    return dict(counts)


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


def test_history_policy_opens_type_16_behind_the_1004_gate_without_four_star_or_daily_rules():
    assert quest_modes.policy(16) is POLICY
    assert POLICY.status() == {"policy": "history", "enabled": True, "reason": None, "hard_mode": False,
                               "timed_conditions": False, "gate_quest": GATE_QUEST}
    support = quest_modes.support_status()[16]
    assert (support["mission_category"], support["mission_resources"], support["gold_counted"],
            support["score_mode"], support["hard_rank"], support["entrust"]) == (14, ["History"], True, False, None, True)


def test_every_listed_history_quest_builds_from_its_own_mission_map(bundle):
    catalog = bundle.quests.catalog
    quest_ids = catalog.playable_quests(16)
    assert len(quest_ids) == HISTORY_QUESTS and set(quest_ids) <= set(catalog.mode_ids)
    for quest_id in quest_ids:
        quest, mode = catalog.get(quest_id), catalog.mode(quest_id)
        assert len(mode.mission_ids) == 1 and mode.map_mission_id == mode.mission_ids[0]
        assert mode.map_archive == f"Map{mode.map_mission_id:04d}_{quest.map_no:04d}.aar"
        assert (mode.mission_category, mode.hard, mode.gold_counted, mode.score_mode) == (14, None, True, False)
        assert mode.star_rewards_normal in ((StarReward("crystal", 0, 0),), (StarReward("none", 4, 0),))
        assert all(1 <= candidate.slot <= len(SLOT_FLAGS) for candidate in quest.candidates)
    assert {quest_id for quest_id in quest_ids if catalog.mode(quest_id).star_rewards_normal[0].kind == "none"} == set(PROLOGUE)
    chained = list(filter(None, (catalog.get(quest_id).prerequisite for quest_id in quest_ids)))
    assert len(chained) == CHAINED_QUESTS and set(chained) <= set(quest_ids)


def test_history_requires_a_cleared_1004_record_and_the_client_disclosure_conditions(setup):
    service, store = setup
    disclosure = next(row for row in service.catalog.resource_rows(DISCLOSURE_TABLE) if row["MissionId"] == FIRST_MISSION)
    assert (disclosure["DisclosureConditionMissionId"], disclosure["DisclosureConditionRank"]) == (PROLOGUE_MISSION, DISCLOSURE_RANK)
    before = store.profile()
    assert_error("quest_locked", lambda: service.start(start_request(service, store, FIRST_QUEST), store))
    assert store.profile() == before
    put_record(store, GATE_QUEST, flags=THREE_STAR_FLAG | sum(SLOT_FLAGS))
    before = store.profile()
    assert_error("quest_locked", lambda: service.start(start_request(service, store, FIRST_QUEST, tick=1001), store))
    assert store.profile() == before
    put_record(store, GATE_QUEST, max_rank=1, flags=CLEAR_FLAG)
    # A new save has claimed no login day yet, and one login day later its prerequisite 920000 has no stars.
    undisclosed(service, store, FIRST_QUEST, GROUP_CLOSED, tick=1002)
    mutate(store, lambda state: start_on(state, 1))
    undisclosed(service, store, FIRST_QUEST, STARS_SHORT, tick=1002)
    disclose(store, service.catalog, FIRST_MISSION, days=1)
    available = charisma(store)
    service.start(start_request(service, store, FIRST_QUEST, tick=1002), store)
    active = store.profile().state["quest_state"]["active"]
    assert (active["quest_id"], active["mission_id"], active["mode"]["quest_type"]) == (FIRST_QUEST, FIRST_MISSION, 16)
    assert store.profile().state["tables"]["game_param"][0]["Rank"] == 1
    assert charisma(store) == available


def test_first_three_star_history_clear_credits_drops_gold_events_and_one_crystal(history):
    service, store = history
    disclose(store, service.catalog, DROP_MISSION)
    quest, crystals = service.catalog.get(DROP_QUEST), service.rules.first_three_star_crystals
    bouquets, wallet = possession(store, BOUQUET), wallet_totals(store.profile().state)["free"]
    response = service.start(start_request(service, store, DROP_QUEST), store)
    assert nodes(response)["LOTTERY"].children[1].values == (1,) * len(quest.candidates)
    active = store.profile().state["quest_state"]["active"]
    assert active["mission_id"] == DROP_MISSION and active["mode"] == {
        "quest_type": 16, "difficulty": "normal", "boost_ignored": None, "star_rewards": [CRYSTAL_REWARD]}
    assert sorted(grant["card_id"] for grant in active["unit_reservations"]) == [8, 67, 68]
    assert charisma(store) == CHARISMA
    mask = selected(store)
    finishing = finish_request(store, DROP_QUEST, obtained=mask, selected=mask, gold=2, extra=gold_events(2))
    response = service.finish(finishing, store)
    completed = store.profile()
    assert service.finish(finishing, store) == response and store.profile() == completed
    assert charisma(store) == CHARISMA - quest.charisma
    tree = nodes(response)
    assert [child.name for child in loads(response).children] == FINISH_CHILDREN
    assert tree["REWARD"].children[0].values == (quest.rank_exp,)
    assert tree["REWARD"].children[1].values == (three_star_gold(quest.gold + 2 * service.rules.gold_per_event),)
    assert drops(tree) == {8: 1, 68: 1, 67: 1, 1001: 1} and sorted(new_cards(tree)) == [8, 67, 68]
    assert item_counts(tree) == {BOUQUET: bouquets + 1}
    assert wallet_totals(completed.state)["free"] == wallet + crystals
    row = record(store, DROP_QUEST)
    assert row["MaxRank"] == 3 and row["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG | sum(SLOT_FLAGS[:4])
    assert row["FirstDefeatDate"] == business_day(NOW)
    entry = completed.state["quest_state"]["history"][-1]
    assert (entry["crystals"], entry["gold_events"], entry["mission_id"]) == (crystals, 2, DROP_MISSION)
    assert entry["star_rewards"] == [CRYSTAL_REWARD] and entry["mode_presents"] == []
    service.start(start_request(service, store, DROP_QUEST, tick=1002), store)
    assert all(attribute == 1 for _, _, attribute in client_candidates(service, store, DROP_QUEST))
    tree = nodes(service.finish(finish_request(store, DROP_QUEST, tick=1003, obtained=mask, selected=mask), store))
    assert "PFC" not in tree and wallet_totals(store.profile().state)["free"] == wallet + crystals
    assert item_counts(tree) == {BOUQUET: bouquets + 2} and sorted(new_cards(tree)) == [8, 67, 68]


@pytest.mark.parametrize("offset,day", enumerate(WEEKDAYS), ids=WEEKDAYS)
def test_real_drop_bonus_scales_carved_crystals_only_on_monday_wednesday_and_saturday(history, bundle, offset, day):
    service, store = history
    set_rank(bundle, store, CRYSTAL_RANK)
    allow_quest(store, CRYSTAL_PREREQUISITE)
    disclose(store, service.catalog, CRYSTAL_MISSION)
    service.clock = lambda: NOW + timedelta(days=offset)
    carved = possession(store, CARVED_CRYSTAL)
    tree = clear(service, store, CRYSTAL_QUEST)
    bonus_day = day in BONUS_DAYS
    credited = BONUS_CRYSTALS if bonus_day else CRYSTAL_DROPS
    assert drops(tree)[CRYSTAL_DROP] == CRYSTAL_DROPS and item_counts(tree)[CARVED_CRYSTAL] == carved + credited
    assert list(tree) == FINISH_CHILDREN + (["BONUS"] if bonus_day else [])
    receipt = store.profile().state["quest_state"]["history"][-1]["drop_bonus"]
    if bonus_day:
        assert bonus_values(tree) == (CARVED_TYPE, BONUS_VALUE)
        assert receipt == {"value": BONUS_VALUE, "increments": {CARVED_CRYSTAL: BONUS_CRYSTALS - CRYSTAL_DROPS}}
    else:
        assert receipt == {"value": 0.0, "increments": {}}


def test_a_quest_listed_only_in_quest_id_list_follows_its_prerequisite_and_rank(history, bundle):
    service, store = history
    catalog = service.catalog
    assert all(row["QuestID"] != LIST_ONLY_QUEST for row in catalog.resource_rows(QUEST_LIST))
    assert [mission.mission_id for mission in catalog.mission_windows(16).missions(LIST_ONLY_QUEST)] == [LIST_ONLY_MISSION]
    quest, mode = catalog.get(LIST_ONLY_QUEST), catalog.mode(LIST_ONLY_QUEST)
    assert (mode.map_archive, mode.map_mission_id) == ("Map920025_3524.aar", LIST_ONLY_MISSION)
    set_rank(bundle, store, LIST_ONLY_RANK)
    disclose(store, catalog, LIST_ONLY_MISSION)
    before = store.profile()
    assert_error("quest_locked", lambda: service.start(start_request(service, store, LIST_ONLY_QUEST), store))
    assert store.profile() == before
    allow_quest(store, quest.prerequisite)
    set_rank(bundle, store, LIST_ONLY_RANK - 1)
    before = store.profile()
    assert_error("quest_rank_required", lambda: service.start(start_request(service, store, LIST_ONLY_QUEST, 1001), store))
    assert store.profile() == before
    set_rank(bundle, store, LIST_ONLY_RANK)
    carved = possession(store, CARVED_CRYSTAL)
    tree = clear(service, store, LIST_ONLY_QUEST, tick=1002)
    assert drops(tree) == {85: 1, 126: 1, 58: 2, 5003: 10, 1001654: 1}
    assert sorted(new_cards(tree)) == [58, 58, 85, 126, 1654]
    assert item_counts(tree) == {CARVED_CRYSTAL: carved + 10}
    row = record(store, LIST_ONLY_QUEST)
    assert row["MaxRank"] == 3 and row["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG | sum(SLOT_FLAGS)
    assert store.profile().state["quest_state"]["history"][-1]["mission_id"] == LIST_ONLY_MISSION


@pytest.mark.parametrize("quest_id,listed,native", [
    (6714, {920125}, 920124), (6727, {920124}, 920125), (4156, {920025, 920029}, 920029),
])
def test_quest_id_list_membership_wins_where_the_mission_quest_list_differs(history, bundle, quest_id, listed, native):
    service, store = history
    catalog = service.catalog
    assert {row["MissionID"] for row in catalog.resource_rows(QUEST_LIST) if row["QuestID"] == quest_id} == listed
    quest, mode = catalog.get(quest_id), catalog.mode(quest_id)
    assert (mode.mission_ids, mode.map_mission_id) == ((native,), native)
    assert mode.map_archive == f"Map{native}_{quest.map_no:04d}.aar"
    set_rank(bundle, store, quest.rank)
    if quest.prerequisite:
        allow_quest(store, quest.prerequisite)
    disclose(store, catalog, native)
    service.start(start_request(service, store, quest_id), store)
    assert store.profile().state["quest_state"]["active"]["mission_id"] == native


@pytest.mark.parametrize("held,discarded", [(INT32_MAX - 1, {}), (INT32_MAX, {TICKET: 1})])
def test_a_collected_stack_zero_ticket_is_bounded_only_by_int32(history, bundle, held, discarded):
    service, store = history
    set_rank(bundle, store, TICKET_RANK)
    disclose(store, service.catalog, TICKET_MISSION)
    quest = service.catalog.get(TICKET_QUEST)
    assert sorted({candidate.slot for candidate in quest.candidates}) == [1, 2, 3]
    assert quest.treasures[3] == 0 and service.catalog.decode_reward(quest.treasures[4]).item_id == TICKET
    assert bundle.possessions.stack(TICKET) == 0
    mutate(store, lambda state: bundle.possessions.credit_capped(state, {TICKET: held})[0])
    tree = clear(service, store, TICKET_QUEST)
    assert drops(tree) == {64: 2, 55: 1, 1005: 1, 10021: 1}
    assert (item_counts(tree, "IOF") if "IOF" in tree else {}) == discarded
    assert item_counts(tree)[TICKET] == INT32_MAX and possession(store, TICKET) == INT32_MAX
    assert store.profile().state["quest_state"]["history"][-1]["discarded"] == discarded
    assert record(store, TICKET_QUEST)["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG | sum(SLOT_FLAGS[:3]) | COLLECTION_BIT
    tree = clear(service, store, TICKET_QUEST, tick=1002)
    assert 10021 not in drops(tree) and "IOF" not in tree and possession(store, TICKET) == INT32_MAX


def test_prologue_quests_clear_without_a_star_reward_and_unlock_their_successor(history, bundle):
    service, store = history
    set_rank(bundle, store, LIST_ONLY_RANK)
    disclose(store, service.catalog, PROLOGUE_MISSION, days=1)
    first, second = PROLOGUE
    before = store.profile()
    assert_error("quest_locked", lambda: service.start(start_request(service, store, second), store))
    assert store.profile() == before
    wallet, carved = wallet_totals(store.profile().state)["free"], possession(store, CARVED_CRYSTAL)
    service.start(start_request(service, store, first, tick=1001), store)
    active = store.profile().state["quest_state"]["active"]
    assert (active["mission_id"], active["mode"]["star_rewards"]) == (PROLOGUE_MISSION, [NO_REWARD])
    mask = selected(store)
    tree = nodes(service.finish(finish_request(store, first, tick=1002, obtained=mask, selected=mask), store))
    assert "PFC" not in tree and wallet_totals(store.profile().state)["free"] == wallet
    assert drops(tree) == {1001654: 1, 5002: 9} and new_cards(tree) == (1654,)
    assert item_counts(tree) == {CARVED_CRYSTAL: carved + 9}
    assert record(store, first)["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG | sum(SLOT_FLAGS[:2])
    assert store.profile().state["quest_state"]["history"][-1]["crystals"] == 0
    service.start(start_request(service, store, second, tick=1003), store)
    assert store.profile().state["quest_state"]["active"]["quest_id"] == second


def test_history_refuses_four_star_starts_without_charging(history):
    service, store = history
    put_record(store, DROP_QUEST, flags=CLEAR_FLAG | THREE_STAR_FLAG | sum(SLOT_FLAGS))
    before = store.profile()
    for number in (78, 82):
        assert_error("quest_hard_unavailable", lambda: service.start(hard_start(service, store, DROP_QUEST, number=number), store))
    assert store.profile() == before


def test_history_clears_count_for_beginner_mission_10009_through_the_history_spec(derived, bundle, tmp_path):
    spec = derived.missions.progress.clear_specs.get(BEGINNER_MISSION)
    assert (spec.claimed_by, spec.quest_types, spec.quest_ids, spec.resolved) == ("history", (16,), None, True)
    initial = derived.missions.initialize(bundle.player_state.initialize(derived.bootstrap.initial_state()))
    store = Store(tmp_path / "history.sqlite3", claim_beginner_steps_before(derived.missions.progress, initial, BEGINNER_MISSION))
    allow_quest(store, GATE_QUEST)
    quests = derived.quests
    disclose(store, quests.catalog, FIRST_MISSION, days=1)

    def beginner():
        rows = derived.missions.progress.snapshot(store.profile().state, MISSION_NOW)
        row = next(row for row in rows if row["ID"] == BEGINNER_MISSION)
        return row["Status"], row["Progress"]

    assert beginner() == (0, 0)
    for tick, quest_id, expected in ((1000, 1001, (0, 0)), (1002, FIRST_QUEST, (1, 1))):
        quests.start(start_request(quests, store, quest_id, tick), store)
        quests.finish(finish_request(store, quest_id, tick + 1, selected=selected(store), stars=1), store)
        assert beginner() == expected


@pytest.mark.parametrize("drop_type,code", DECODABLE_CODES.items())
def test_history_rows_accept_treasures_of_every_decodable_drop_type(bundle, drop_type, code):
    catalog = bundle.quests.catalog
    assert catalog.decode_reward(code).drop_type == drop_type
    assert POLICY.validate_row(MappingProxyType({**catalog.row(DROP_QUEST), "Treasure4": code}), catalog) is None


def test_history_treasures_settle_drop_types_the_resources_do_not_use_yet(history, bundle):
    _, store = history
    service = fresh_service(bundle)
    catalog = service.catalog
    catalog._all_rows[DROP_QUEST] = {**catalog._all_rows[DROP_QUEST],
                                     f"Treasure{EVENT_SLOT}": EVENT_POINTS, f"Treasure{ORB_SLOT}": EVOLVE_ORB}
    disclose(store, catalog, DROP_MISSION)
    points, orb = catalog.decode_reward(EVENT_POINTS), catalog.decode_reward(EVOLVE_ORB)
    assert (points.kind, points.drop_type, orb.kind, orb.drop_type) == (
        "event_param", EVENT_PARAM_DROP_TYPE, "item", EVOLVE_ORB_DROP_TYPE)
    quest = catalog.get(DROP_QUEST)
    slots = Counter(candidate.slot for candidate in quest.candidates)
    assert slots[EVENT_SLOT] and slots[ORB_SLOT]
    orbs, event_param = possession(store, orb.item_id), store.profile().state["tables"]["game_param"][0]["EventParam"]
    tree = clear(service, store, DROP_QUEST)
    assert drops(tree) == expected_drops(catalog, quest)
    assert item_counts(tree)[orb.item_id] == orbs + slots[ORB_SLOT] * orb.count
    assert store.profile().state["tables"]["game_param"][0]["EventParam"] == min(
        event_param + slots[EVENT_SLOT] * points.count, EVENT_PARAM_LIMIT)


@pytest.mark.parametrize("value", [15, 16.0, True, None])
def test_history_rows_of_another_type_are_refused(bundle, value):
    catalog = bundle.quests.catalog
    row = catalog.row(DROP_QUEST)
    assert POLICY.validate_row(row, catalog) is None
    refused("quest_definition_invalid", 503, lambda: POLICY.validate_row(MappingProxyType({**row, "Type": value}), catalog))


@pytest.mark.parametrize("change,code", [
    ({"Treasure1": -1}, "quest_definition_invalid"), ({"Treasure2": "36"}, "quest_definition_invalid"),
    ({"Treasure3": None}, "quest_definition_invalid"), ({"Treasure5": 2 ** 31}, "quest_definition_invalid"),
    ({"Treasure4": UNKNOWN_CODE}, "quest_reward_unknown"),
])
def test_history_treasures_outside_the_shared_reward_contract_are_refused(bundle, monkeypatch, change, code):
    catalog = bundle.quests.catalog
    replace_row(monkeypatch, catalog, DROP_QUEST, change)
    refused(code, 503, lambda: catalog.get(DROP_QUEST))
    assert catalog.status()["mode_failed_quests"][DROP_QUEST]["code"] == code


@pytest.mark.parametrize("change,code,status", [
    ({"Treasure4": UNKNOWN_CODE}, "quest_reward_unknown", 503),
    ({"OpenWeek": 1}, "quest_condition_unsupported", 501),
])
def test_a_history_row_outside_the_contract_is_refused_before_charging(history, bundle, change, code, status):
    _, store = history
    service = fresh_service(bundle)
    service.catalog._all_rows[DROP_QUEST] = {**service.catalog._all_rows[DROP_QUEST], **change}
    disclose(store, service.catalog, FIRST_MISSION)
    before = store.profile()
    refused(code, status, lambda: service.start(bare_start(service, store, DROP_QUEST), store))
    assert store.profile() == before
    assert service.catalog.status()["mode_failed_quests"][DROP_QUEST]["code"] == code
    service.start(start_request(service, store, FIRST_QUEST, tick=1001), store)
    assert store.profile().state["quest_state"]["active"]["mission_id"] == FIRST_MISSION
