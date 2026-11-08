from collections import Counter
from datetime import datetime
from types import MappingProxyType

import pytest

from aigis_ps import quest_modes
from aigis_ps.alxml import loads
from aigis_ps.business_calendar import business_day
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.mission_tags import REGISTRY_CATEGORIES, MissionTagIndex
from aigis_ps.quest_catalog import QuestCatalog, StarReward
from aigis_ps.quest_mode_devil import POLICY
from aigis_ps.quest_modes import (
    CLEAR_FLAG, HARD_STAR_FLAG, MODE_SPECS, PLATFORM_CODES, THREE_STAR_FLAG, MissionWindows, StartContext,
)
from aigis_ps.quest_service import QuestService
from aigis_ps.storage import Store
from test_quest_battle_modes import (
    MISSION_NOW, bare_start, card_ids, charisma, fill_wallet, gold_events, hard_start, new_cards, put_record, record,
    set_rank, three_star_gold, update_game_param,
)
from test_quest_service import (
    NOW, allow_quest, assert_error, assert_refusal, bundle, finish_request, nodes, setup, start_request,
)
from test_quest_settlement import derived, formation, possession


DEVIL_TYPE = 9
DEVIL_CATEGORY = 8
GATE_QUEST = 1004
DEVIL_RANK = 50
CHARISMA = 1000
PLATFORM, OTHER_PLATFORM = 1, 2
# Every devil mission enables platform bits 0–12 (Enable 8191).
UNLISTED_PLATFORM = 13
INVALID_PLATFORMS = (-1, 32, True, "1", None)
TODAY = datetime(2026, 11, 2, 12, tzinfo=JST)
DEVIL_QUESTS = 343
CLEARED = CLEAR_FLAG | THREE_STAR_FLAG
MENU_TAG = 150
NO_MENU_MISSION = "No enabled devil advent mission carries the menu rank tag"
MENU_RANK_SHORT = "Player rank is below the devil advent menu requirement"
# フールフール ended on 2026-03-01 but is still the first mission tagged 150, so it sets the menu rank.
MENU_MISSION = 500090
FURFUR_LV1 = 4497
AMON, AMON_LV1, AMON_LV2, AMON_LV15, AMON_LV16 = 500096, 4542, 4543, 4556, 9504
BIFRONS, BIFRONS_LV1 = 500110, 4654
KIMARIS_LV1 = 6169
FINAL_BATTLE, FINAL_PREVIOUS = 9123, 7967
POSEIOS_EX, MAIN_REQUEST = 8327, 1937
PERSEPHONE_EX, EMPIRE_REQUEST = 8351, 8350
KUROKO_EX, KUROKO_REQUEST = 8204, 8197
WUSHIN, WUSHIN_EASY, WUSHIN_RANK = 500320, 7380, 2
# A rank 1 story mission whose MissionID precedes every devil mission.
STORY_CATEGORY, STORY_MISSION = 0, 100001
PLAYABLE_MISSIONS = frozenset((AMON, BIFRONS, 500155, 500401, 500422, 500461))
HARD_QUESTS = (8633, *range(9501, 9518))
HARD_PLAYABLE = (9504, 9508, 9510)
PLATINUM_ARMOR = 133
UNIT_STAR_QUESTS, ITEM_STAR_QUESTS = 336, 7
# 8414 and 8766 also name a reward item, but 679D70 pays BonusType 3 as the card alone.
CARD_QUESTS_WITH_ITEMS, HAPPY_SPIRIT = (8414, 8766), 194
BLESSING = "battle_god_bless_003069"
# The June Bride challenge text drops を before クリア yet still names Lv.1.
GENUS_CHALLENGE, JUNE_BRIDE_CHALLENGE, TWO_CLEARS_DAILY = 1016, 1169, 1606
# A Lv.N attribute challenge names one level of each standard sixteen-level mission.
STANDARD_MISSIONS = 18
GENUS_CARD = 438
INT32_MIN, INT32_MAX = -(2 ** 31), 2 ** 31 - 1


def refusal(callback):
    with pytest.raises(ProtocolError) as error:
        callback()
    return error.value.code, error.value.status


def refused(code, service, store, starting):
    before = store.profile()
    assert refusal(lambda: service.start(starting, store)) == (code, 409)
    assert store.profile() == before


def cards(response):
    return new_cards(nodes(response))


def pairs(tree, name):
    columns = tree[name].children
    return dict(zip(columns[0].values, columns[1].values))


def cleared(*quest_ids):
    return {quest_id: {"QuestID": quest_id, "MaxRank": 3, "AchieveFlag": CLEARED} for quest_id in quest_ids}


def context(catalog, quest_id, records, *, rank=DEVIL_RANK, hard=False, platform=PLATFORM):
    return StartContext(catalog, catalog.get(quest_id), catalog.mode(quest_id), records, rank, TODAY, hard, platform)


def retagged(catalog, change, category=DEVIL_CATEGORY):
    current = catalog.mission_tags()
    tables = catalog._mission_tables(REGISTRY_CATEGORIES)
    configs, members = tables[category]
    # Reversed table order shows that only MissionID decides which tagged mission comes first.
    tables[category] = (tuple(MappingProxyType({**row, **change(row)}) for row in reversed(configs)), members)
    ranks = {quest_id: row["Rank"] for quest_id, row in catalog._all_rows.items()}
    return MissionTagIndex(MissionWindows(tables, ranks, overrides=current.overrides),
                           quest_rows=MappingProxyType(catalog._all_rows), source_sha256=current.source_sha256,
                           master_snapshot=current.master_snapshot, overrides=current.overrides,
                           categories_without_tag_column=current.categories_without_tag_column)


def retag(catalog, patch, change, category=DEVIL_CATEGORY):
    tags = retagged(catalog, change, category)
    patch.setattr(catalog, "mission_tags", lambda: tags)
    patch.setattr(catalog, "mission_registry", lambda: tags.registry)


@pytest.fixture
def devil(setup, bundle):
    service, store = setup
    allow_quest(store, GATE_QUEST)
    set_rank(bundle, store, DEVIL_RANK)
    update_game_param(store, NowCharisma=CHARISMA)
    return service, store


@pytest.fixture
def replica(devil, bundle):
    _, store = devil
    base = bundle.quests.catalog
    catalog = QuestCatalog(base.snapshot, base._asset_reader, possessions=bundle.possessions,
                           resource_version=base.resource_version, window_overrides={WUSHIN: (None, None)})
    service = QuestService(catalog, bundle.bootstrap, bundle.player_state, bundle.possessions,
                           clock=lambda: NOW, randbelow=lambda _: 0)
    return service, store


def test_devil_policy_is_registered_with_the_four_star_mode_contract():
    assert quest_modes.policy(DEVIL_TYPE) is POLICY
    assert POLICY.status() == {"policy": "devil", "enabled": True, "reason": None, "hard_mode": True,
                               "timed_conditions": False, "gate_quest": GATE_QUEST}
    spec = MODE_SPECS[DEVIL_TYPE]
    assert (spec.mission_category, spec.hard_rank, spec.gold_counted, spec.score_mode, spec.entrust) == (
        DEVIL_CATEGORY, DEVIL_RANK, True, False, False)


def test_every_devil_quest_builds_without_prize_rows_and_rewards_only_first_stars(bundle):
    catalog = bundle.quests.catalog
    quest_ids = catalog.playable_quests(DEVIL_TYPE)
    assert len(quest_ids) == DEVIL_QUESTS and set(quest_ids) <= set(catalog.mode_ids)
    hard, kinds = {}, Counter()
    for quest_id in quest_ids:
        quest, mode = catalog.get(quest_id), catalog.mode(quest_id)
        assert quest.candidates == () and not any(quest.treasures)
        assert mode.mission_category == DEVIL_CATEGORY and mode.gold_counted and not mode.score_mode
        assert mode.map_mission_id is None and mode.map_archive == f"Map{quest.map_no:04d}.aar"
        assert len(mode.star_rewards_normal) == 1
        kinds[mode.star_rewards_normal[0].kind] += 1
        if mode.hard is not None:
            hard[quest_id] = mode.star_rewards_hard
    assert kinds == {"unit": UNIT_STAR_QUESTS, "item": ITEM_STAR_QUESTS}
    assert {catalog.mode(quest_id).star_rewards_normal for quest_id in CARD_QUESTS_WITH_ITEMS} == {
        (StarReward("unit", 3, 1, card_id=HAPPY_SPIRIT),)}
    assert tuple(sorted(hard)) == HARD_QUESTS
    assert set(hard.values()) == {(StarReward("crystal", 0, 3),)}


def test_check_start_admits_the_58_quests_of_the_six_playable_missions_on_2026_11_02(bundle):
    catalog = bundle.quests.catalog
    quest_ids = catalog.playable_quests(DEVIL_TYPE)
    # Every requested quest except the empire ones holds three stars, so only windows and empire locks remain.
    records = cleared(GATE_QUEST, MAIN_REQUEST, *quest_ids)
    outcomes = {}
    for quest_id in quest_ids:
        try:
            outcomes[quest_id] = POLICY.check_start(context(catalog, quest_id, records)).mission_id
        except ProtocolError as error:
            outcomes[quest_id] = error.code
    admitted = sorted(quest_id for quest_id, outcome in outcomes.items() if type(outcome) is int)
    assert len(admitted) == 58 and {outcomes[quest_id] for quest_id in admitted} == PLAYABLE_MISSIONS
    assert Counter(outcome for outcome in outcomes.values() if type(outcome) is str) == {"quest_locked": 18, "quest_closed": 267}
    hard = tuple(quest_id for quest_id in admitted if catalog.mode(quest_id).hard is not None)
    assert hard == HARD_PLAYABLE
    assert all(POLICY.check_start(context(catalog, quest_id, records, hard=True)).mission_id in PLAYABLE_MISSIONS
               for quest_id in hard)


def test_devil_quests_open_after_the_menu_gate_quest_at_rank_fifty(setup, bundle):
    service, store = setup
    update_game_param(store, NowCharisma=CHARISMA)
    set_rank(bundle, store, DEVIL_RANK)
    refused("quest_locked", service, store, bare_start(service, store, AMON_LV1))
    put_record(store, GATE_QUEST, max_rank=0, flags=0)
    refused("quest_locked", service, store, bare_start(service, store, AMON_LV1))
    put_record(store, GATE_QUEST, max_rank=1, flags=CLEAR_FLAG)
    set_rank(bundle, store, DEVIL_RANK - 1)
    refused("quest_rank_required", service, store, bare_start(service, store, AMON_LV1))
    set_rank(bundle, store, DEVIL_RANK)
    service.start(bare_start(service, store, AMON_LV1), store)
    active = store.profile().state["quest_state"]["active"]
    assert (active["quest_id"], active["mission_id"], active["mode"]["quest_type"]) == (AMON_LV1, AMON, DEVIL_TYPE)
    assert charisma(store) == CHARISMA


def test_each_level_needs_three_stars_on_the_previous_level(devil):
    service, store = devil
    refused("quest_locked", service, store, bare_start(service, store, AMON_LV2))
    service.start(bare_start(service, store, AMON_LV1, tick=1001), store)
    assert not cards(service.finish(finish_request(store, AMON_LV1, tick=1002, stars=2), store))
    assert (record(store, AMON_LV1)["MaxRank"], record(store, AMON_LV1)["AchieveFlag"]) == (2, CLEAR_FLAG)
    refused("quest_locked", service, store, bare_start(service, store, AMON_LV2, tick=1003))
    service.start(bare_start(service, store, AMON_LV1, tick=1004), store)
    assert cards(service.finish(finish_request(store, AMON_LV1, tick=1005), store)) == (PLATINUM_ARMOR,)
    service.start(bare_start(service, store, AMON_LV2, tick=1006), store)
    assert store.profile().state["quest_state"]["active"]["quest_id"] == AMON_LV2


def test_ended_missions_stay_closed_and_requested_quests_lock_their_missions(devil):
    service, store = devil
    refused("quest_closed", service, store, bare_start(service, store, FURFUR_LV1))
    tick = 1001
    for quest_id, requested in ((POSEIOS_EX, MAIN_REQUEST), (PERSEPHONE_EX, EMPIRE_REQUEST), (KUROKO_EX, KUROKO_REQUEST)):
        refused("quest_locked", service, store, bare_start(service, store, quest_id, tick))
        put_record(store, requested, max_rank=1, flags=CLEAR_FLAG)
        service.start(bare_start(service, store, quest_id, tick + 1), store)
        assert store.profile().state["quest_state"]["active"]["quest_id"] == quest_id
        tick += 2


@pytest.mark.parametrize("moment,opened", [
    (datetime(2026, 10, 29, 14, 59, 59), False), (datetime(2026, 10, 29, 15), True),
    (datetime(2026, 11, 5, 10, 59, 58), True), (datetime(2026, 11, 5, 10, 59, 59), False),
])
def test_a_limited_devil_mission_opens_at_its_start_and_closes_at_its_raw_end(devil, moment, opened):
    service, store = devil
    service.clock = lambda: moment
    starting = bare_start(service, store, BIFRONS_LV1)
    if opened:
        service.start(starting, store)
        assert store.profile().state["quest_state"]["active"]["mission_id"] == BIFRONS
    else:
        refused("quest_closed", service, store, starting)


def test_a_devil_attempt_started_inside_its_window_settles_after_the_window_ends(devil):
    service, store = devil
    service.clock = lambda: datetime(2026, 11, 5, 10, 59, 58)
    service.start(bare_start(service, store, BIFRONS_LV1), store)
    finished = datetime(2026, 11, 5, 11, 0, 30)
    service.clock = lambda: finished
    assert cards(service.finish(finish_request(store, BIFRONS_LV1), store)) == (PLATINUM_ARMOR,)
    assert record(store, BIFRONS_LV1)["FirstDefeatDate"] == business_day(finished)


def test_first_three_star_devil_clear_grants_its_unit_once_and_counts_gold_events(devil):
    service, store = devil
    quest = service.catalog.get(AMON_LV1)
    owned = card_ids(store).count(PLATINUM_ARMOR)
    response = service.start(bare_start(service, store, AMON_LV1), store)
    assert [child.name for child in loads(response).children] == [
        "SYSTEM", "PGP", "LOTTERY", "REWARD", "TREASURE", "TreasureInfo", "PPU"]
    assert not nodes(response)["LOTTERY"].children[0].values
    active = store.profile().state["quest_state"]["active"]
    assert (active["mission_id"], active["registration"], active["selected_mask"]) == (AMON, 81, 0)
    assert active["mode"] == {"quest_type": DEVIL_TYPE, "difficulty": "normal", "boost_ignored": None, "star_rewards": [
        {"kind": "unit", "reward_type": 3, "count": 1, "card_id": PLATINUM_ARMOR, "item_id": None}]}
    assert active["unit_reservations"] == [{"card_id": PLATINUM_ARMOR, "level": quest.unit_level, "count": 1}]
    finishing = finish_request(store, AMON_LV1, gold=2, extra=gold_events(2))
    response = service.finish(finishing, store)
    completed = store.profile()
    assert service.finish(finishing, store) == response and store.profile() == completed
    tree = nodes(response)
    assert tree["REWARD"].children[0].values == (quest.rank_exp,)
    assert tree["REWARD"].children[1].values == (three_star_gold(quest.gold + 2 * service.rules.gold_per_event),)
    assert new_cards(tree) == (PLATINUM_ARMOR,) and card_ids(store).count(PLATINUM_ARMOR) == owned + 1
    assert not tree["TreasureInfo"].children[0].values and not {"PFC", "POSSESSION", "PPT"} & set(tree)
    row = record(store, AMON_LV1)
    assert (row["MaxRank"], row["AchieveFlag"], row["FirstDefeatDate"]) == (3, CLEARED, business_day(NOW))
    history = completed.state["quest_state"]["history"][-1]
    assert (history["gold_events"], history["mission_id"], history["star_rewards"]) == (2, AMON, active["mode"]["star_rewards"])
    service.start(bare_start(service, store, AMON_LV1, tick=1002), store)
    assert store.profile().state["quest_state"]["active"]["unit_reservations"] == []
    assert not cards(service.finish(finish_request(store, AMON_LV1, tick=1003), store))
    assert card_ids(store).count(PLATINUM_ARMOR) == owned + 1 and record(store, AMON_LV1)["AchieveFlag"] == CLEARED


def test_devil_starts_refuse_candidates_their_maps_never_serialize(devil):
    service, store = devil
    refused("quest_candidates_mismatch", service, store,
            start_request(service, store, AMON_LV1, candidates=[(0, 1, 0)]))
    service.start(start_request(service, store, AMON_LV1, tick=1001), store)
    assert store.profile().state["quest_state"]["active"]["selected_mask"] == 0


@pytest.mark.parametrize("obtained,selected", [(1, 0), (0, 1), (1, 1)])
def test_devil_results_must_report_empty_drop_masks(devil, obtained, selected):
    service, store = devil
    service.start(bare_start(service, store, AMON_LV1), store)
    before = store.profile()
    assert refusal(lambda: service.finish(finish_request(store, AMON_LV1, obtained=obtained, selected=selected),
                                          store)) == ("quest_drop_mismatch", 409)
    assert store.profile() == before


def test_four_star_devil_starts_need_a_challenge_level_with_three_stars(devil):
    service, store = devil
    refused("quest_hard_unavailable", service, store, hard_start(service, store, AMON_LV1))
    put_record(store, AMON_LV15, flags=CLEARED)
    put_record(store, AMON_LV16, max_rank=2, flags=CLEAR_FLAG)
    refused("quest_hard_locked", service, store, hard_start(service, store, AMON_LV16))
    put_record(store, AMON_LV16, flags=CLEARED)
    service.start(hard_start(service, store, AMON_LV16), store)
    assert store.profile().state["quest_state"]["active"]["registration"] == 82


def test_four_star_devil_challenge_grants_three_crystals_once(devil):
    service, store = devil
    put_record(store, AMON_LV15, flags=CLEARED)
    put_record(store, AMON_LV16, flags=CLEARED)
    before, wallet = charisma(store), wallet_totals(store.profile().state)["free"]
    response = service.start(hard_start(service, store, AMON_LV16, boost=2), store)
    assert not nodes(response)["LOTTERY"].children[0].values
    assert charisma(store) == before
    active = store.profile().state["quest_state"]["active"]
    assert (active["registration"], active["unit_reservations"]) == (82, [])
    assert active["mode"] == {"quest_type": DEVIL_TYPE, "difficulty": "hard", "boost_ignored": 2, "star_rewards": [
        {"kind": "crystal", "reward_type": 0, "count": 3, "card_id": None, "item_id": None}]}
    assert_error("quest_result_invalid", lambda: service.finish(finish_request(store, AMON_LV16, stars=3), store))
    assert charisma(store) == before
    tree = nodes(service.finish(finish_request(store, AMON_LV16, stars=4), store))
    assert charisma(store) == before - service.catalog.get(AMON_LV16).charisma
    assert "PFC" in tree and wallet_totals(store.profile().state)["free"] == wallet + 3
    row = record(store, AMON_LV16)
    assert (row["MaxRank"], row["AchieveFlag"]) == (4, CLEARED | HARD_STAR_FLAG)
    assert store.profile().state["quest_state"]["history"][-1]["crystals"] == 3
    service.start(hard_start(service, store, AMON_LV16, tick=1002), store)
    tree = nodes(service.finish(finish_request(store, AMON_LV16, tick=1003, stars=4), store))
    assert "PFC" not in tree and wallet_totals(store.profile().state)["free"] == wallet + 3


def test_four_star_devil_crystals_beyond_the_wallet_become_one_present(devil, bundle):
    service, store = devil
    service.presents = bundle.presents
    put_record(store, AMON_LV15, flags=CLEARED)
    put_record(store, AMON_LV16, flags=CLEARED)
    fill_wallet(store)
    service.start(hard_start(service, store, AMON_LV16), store)
    tree = nodes(service.finish(finish_request(store, AMON_LV16, stars=4), store))
    assert "PFC" not in tree and "PPT" in tree
    saved = store.profile().state
    added = saved["quest_state"]["history"][-1]["crystal_presents"]
    rows = {row["PresentID"]: row for row in bundle.presents.rows(saved)}
    assert len(added) == 1
    assert (rows[added[0]]["Type"], rows[added[0]]["Content"], rows[added[0]]["Description"]) == (0, 3, "★４チャレンジ報酬")


def test_final_battle_grants_its_war_god_blessing_and_discards_a_duplicate(devil, bundle):
    service, store = devil
    for quest_id in (AMON_LV1, KIMARIS_LV1, FINAL_PREVIOUS):
        put_record(store, quest_id, flags=CLEARED)
    assert bundle.possessions.stack(BLESSING) == 1 and possession(store, BLESSING) == 0
    service.start(bare_start(service, store, FINAL_BATTLE), store)
    response = service.finish(finish_request(store, FINAL_BATTLE), store)
    tree = nodes(response)
    assert pairs(tree, "POSSESSION") == {BLESSING: 1} and "IOF" not in tree and not new_cards(tree)
    assert record(store, FINAL_BATTLE)["AchieveFlag"] == CLEARED
    put_record(store, FINAL_BATTLE, max_rank=2, flags=CLEAR_FLAG)
    service.start(bare_start(service, store, FINAL_BATTLE, tick=1002), store)
    tree = nodes(service.finish(finish_request(store, FINAL_BATTLE, tick=1003), store))
    assert pairs(tree, "POSSESSION") == {BLESSING: 1} and pairs(tree, "IOF") == {BLESSING: 1}
    assert record(store, FINAL_BATTLE)["AchieveFlag"] == CLEARED and possession(store, BLESSING) == 1
    assert store.profile().state["quest_state"]["history"][-1]["discarded"] == {BLESSING: 1}


def test_a_reopened_low_rank_mission_still_needs_the_devil_menu_rank(replica, bundle, setup):
    service, store = replica
    ordinary, _ = setup
    registry = service.catalog.mission_registry()
    assert POLICY.menu_mission(service.catalog, PLATFORM).mission_id == MENU_MISSION
    assert (registry.minimum_rank(MENU_MISSION), registry.minimum_rank(WUSHIN)) == (DEVIL_RANK, WUSHIN_RANK)
    refused("quest_closed", ordinary, store, bare_start(ordinary, store, WUSHIN_EASY))
    set_rank(bundle, store, DEVIL_RANK - 1)
    refused("quest_rank_required", service, store, bare_start(service, store, WUSHIN_EASY))
    set_rank(bundle, store, DEVIL_RANK)
    service.start(bare_start(service, store, WUSHIN_EASY), store)
    active = store.profile().state["quest_state"]["active"]
    assert (active["quest_id"], active["mission_id"]) == (WUSHIN_EASY, WUSHIN)


def test_the_devil_menu_resolves_tag_150_through_the_shared_mission_registry(bundle):
    catalog = bundle.quests.catalog
    menu = POLICY.menu_mission(catalog, PLATFORM)
    assert menu is catalog.mission_tags().tag_mission(MENU_TAG, PLATFORM)
    assert (menu.mission_id, menu.category, catalog.mission_registry().minimum_rank(menu.mission_id)) == (
        MENU_MISSION, DEVIL_CATEGORY, DEVIL_RANK)
    assert {platform: POLICY.menu_mission(catalog, platform) for platform in PLATFORM_CODES} == {
        platform: menu if platform < UNLISTED_PLATFORM else None for platform in PLATFORM_CODES}


@pytest.mark.parametrize("change,expected", [
    (lambda row: {}, MENU_MISSION),
    (lambda row: {"TagID": 140, "TagIDRange": 10} if row["MissionID"] == MENU_MISSION else {}, MENU_MISSION),
    (lambda row: {"TagID": 140, "TagIDRange": 9} if row["MissionID"] == MENU_MISSION else {}, AMON),
    (lambda row: {"TagID": 151} if row["MissionID"] == MENU_MISSION else {}, AMON),
    (lambda row: {"TagID": 0} if row["MissionID"] < WUSHIN else {}, WUSHIN),
    (lambda row: {"TagID": 100, "TagIDRange": INT32_MAX - 50} if row["MissionID"] == MENU_MISSION else {}, AMON),
    (lambda row: {"TagID": -1, "TagIDRange": INT32_MIN} if row["MissionID"] == MENU_MISSION else {}, MENU_MISSION),
], ids=("native", "span_reaches_tag", "span_stops_short", "tag_above", "earlier_missions_untagged",
        "span_end_wraps_negative", "span_end_wraps_positive"))
def test_the_menu_rank_comes_from_the_first_mission_whose_tag_span_covers_150(bundle, monkeypatch, change, expected):
    catalog = bundle.quests.catalog
    retag(catalog, monkeypatch, change)
    assert POLICY.menu_mission(catalog, PLATFORM).mission_id == expected


def test_missions_outside_the_request_platform_never_set_the_menu_rank(bundle, monkeypatch):
    catalog = bundle.quests.catalog
    assert POLICY.menu_mission(catalog, UNLISTED_PLATFORM) is None
    retag(catalog, monkeypatch, lambda row: {"Enable": 1 << OTHER_PLATFORM} if row["MissionID"] == MENU_MISSION else {})
    assert POLICY.menu_mission(catalog, PLATFORM).mission_id == AMON
    assert POLICY.menu_mission(catalog, OTHER_PLATFORM).mission_id == MENU_MISSION


def test_devil_starts_resolve_the_menu_tag_on_the_request_platform(bundle, monkeypatch):
    catalog = bundle.quests.catalog
    records = cleared(GATE_QUEST)
    retag(catalog, monkeypatch,
          lambda row: {"Enable": 1 << OTHER_PLATFORM} if row["MissionID"] == MENU_MISSION else {"TagID": 0})
    assert_refusal("quest_rank_required", NO_MENU_MISSION,
                   lambda: POLICY.check_start(context(catalog, AMON_LV1, records)))
    assert POLICY.check_start(context(catalog, AMON_LV1, records, platform=OTHER_PLATFORM)).mission_id == AMON


def test_a_lower_mission_of_any_category_covering_150_sets_the_devil_menu_rank(replica, monkeypatch):
    service, _ = replica
    catalog = service.catalog
    reopened = context(catalog, WUSHIN_EASY, cleared(GATE_QUEST), rank=DEVIL_RANK - 1)
    assert_refusal("quest_rank_required", MENU_RANK_SHORT, lambda: POLICY.check_start(reopened))
    retag(catalog, monkeypatch, lambda row: {"TagID": MENU_TAG} if row["MissionID"] == STORY_MISSION else {},
          STORY_CATEGORY)
    menu = POLICY.menu_mission(catalog, PLATFORM)
    assert (menu.mission_id, menu.category) == (STORY_MISSION, STORY_CATEGORY)
    assert POLICY.check_start(reopened).mission_id == WUSHIN


def test_devil_starts_need_a_mission_carrying_the_menu_tag(bundle, monkeypatch):
    catalog = bundle.quests.catalog
    records = cleared(GATE_QUEST)
    assert POLICY.check_start(context(catalog, AMON_LV1, records)).mission_id == AMON
    retag(catalog, monkeypatch, lambda row: {"TagID": 0})
    assert POLICY.menu_mission(catalog, PLATFORM) is None
    assert_refusal("quest_rank_required", NO_MENU_MISSION,
                   lambda: POLICY.check_start(context(catalog, AMON_LV1, records, rank=999)))
    # The shared resolver checks the platform code before it walks any mission.
    for code in INVALID_PLATFORMS:
        assert refusal(lambda code=code: POLICY.menu_mission(catalog, code)) == ("quest_platform_invalid", 400)


def test_devil_starts_check_the_menu_after_the_base_rules_and_pass_registry_failures_through(bundle, monkeypatch):
    catalog = bundle.quests.catalog
    # A built index never consults the registry again, so a failing registry reaches only the menu rank lookup.
    catalog.mission_tags()
    starting, locked = context(catalog, AMON_LV1, cleared(GATE_QUEST)), context(catalog, AMON_LV1, {})
    failure = ProtocolError("quest_mode_resource_invalid", "Mission windows do not satisfy the native contract", 503)

    def unavailable():
        raise failure

    for name in ("mission_tags", "mission_registry"):
        with monkeypatch.context() as patch:
            patch.setattr(catalog, name, unavailable)
            assert refusal(lambda: POLICY.check_start(locked)) == ("quest_locked", 409)
            with pytest.raises(ProtocolError) as error:
                POLICY.check_start(starting)
            assert error.value is failure


def test_devil_clears_count_attribute_challenges_through_their_derived_specs(derived, bundle, tmp_path):
    specs = derived.missions.progress.clear_specs
    spec = specs.get(GENUS_CHALLENGE)
    assert spec.claimed_by == "devil" and spec.quest_types == (DEVIL_TYPE,) and AMON_LV1 in spec.quest_ids
    assert len(spec.quest_ids) == STANDARD_MISSIONS
    assert (spec.team.field, spec.team.value, spec.team.mode, spec.team.count) == ("Genus", 101, "at_least", 4)
    assert GENUS_CARD in spec.team.card_ids and specs.get(JUNE_BRIDE_CHALLENGE).quest_ids == spec.quest_ids
    initial = derived.missions.initialize(bundle.player_state.initialize(derived.bootstrap.initial_state()))
    initial, units = bundle.player_state.credit_units(initial, [{"card_id": GENUS_CARD, "level": 1, "count": 4}])
    store = Store(tmp_path / "devil.sqlite3", initial)
    allow_quest(store, GATE_QUEST)
    set_rank(bundle, store, DEVIL_RANK)
    update_game_param(store, NowCharisma=CHARISMA)
    team = [(unit["CardID"], unit["GetIndex"], slot, unit["UniqueID"], 0) for slot, unit in enumerate(units, 1)]

    def progress(condition_id):
        rows = derived.missions.progress.snapshot(store.profile().state, MISSION_NOW)
        return next(row["Progress"] for row in rows if row["ID"] == condition_id)

    quests = derived.quests
    for tick, members, expected in ((1000, team[:3], 0), (1002, team, 1)):
        quests.start(bare_start(quests, store, AMON_LV1, tick), store)
        quests.finish(finish_request(store, AMON_LV1, tick=tick + 1, extra=formation(*members)), store)
        assert progress(GENUS_CHALLENGE) == expected
    assert progress(TWO_CLEARS_DAILY) == 2 and progress(JUNE_BRIDE_CHALLENGE) == 0


def test_the_june_bride_challenge_counts_only_lv1_clears_of_its_attribute_team(derived, bundle):
    engine = derived.missions.progress
    bride = engine.clear_specs.get(JUNE_BRIDE_CHALLENGE)
    assert (bride.claimed_by, bride.quest_types, bride.resolved) == ("devil", (DEVIL_TYPE,), True)
    initial = derived.missions.initialize(bundle.player_state.initialize(derived.bootstrap.initial_state()))
    team = [{"card_id": card_id, "get_index": slot, "unique_id": slot}
            for slot, card_id in enumerate(sorted(bride.team.card_ids)[:bride.team.count], 1)]
    for quest_id, expected in ((AMON_LV1, 1), (AMON_LV16, 0), (WUSHIN_EASY, 0)):
        clear = {"quest_id": quest_id, "quest_type": DEVIL_TYPE, "stars": 3, "hard": False, "team": team}
        rows = engine.snapshot(engine.record(initial, "quest_clear", clear, MISSION_NOW), MISSION_NOW)
        assert next(row["Progress"] for row in rows if row["ID"] == JUNE_BRIDE_CHALLENGE) == expected


@pytest.mark.parametrize("field,value", [
    ("Type", 0), ("Type", 11), ("Type", 9.0), ("Type", True), ("Treasure1", 67), ("Treasure3", 1001062),
    ("Treasure5", 15), ("Treasure2", None), ("Treasure4", "0"), ("Treasure1", False), ("Treasure5", -1),
])
def test_rows_outside_the_devil_contract_are_refused(bundle, field, value):
    catalog = bundle.quests.catalog
    with pytest.raises(ProtocolError) as error:
        POLICY.validate_row(MappingProxyType({**catalog.row(AMON_LV1), field: value}), catalog)
    assert (error.value.code, error.value.status) == ("quest_definition_invalid", 503)
    assert field in str(error.value)


def test_rows_without_a_treasure_column_are_refused_and_native_rows_pass(bundle):
    catalog = bundle.quests.catalog
    row = {name: value for name, value in catalog.row(AMON_LV16).items() if name != "Treasure5"}
    assert refusal(lambda: POLICY.validate_row(MappingProxyType(row), catalog)) == ("quest_definition_invalid", 503)
    for quest_id in (AMON_LV1, AMON_LV16, FINAL_BATTLE, WUSHIN_EASY):
        assert POLICY.validate_row(catalog.row(quest_id), catalog) is None
