from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps import quest_modes
from aigis_ps.alxml import loads
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.errors import ProtocolError
from aigis_ps.mission_tags import TAG_LOOKUP, MissionTagIndex
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.quest_catalog import TREASURE_SLOTS, QuestCatalog, StarReward
from aigis_ps.quest_mode_empire import EMPIRE_CATEGORY, EMPIRE_TYPE, MISSION_CONFIG, POLICY
from aigis_ps.quest_modes import (
    CLEAR_FLAG, HARD_STAR_FLAG, OPEN, PLATFORM_CODES, SLOT_FLAGS, MissionSelector, StartContext, THREE_STAR_FLAG, validate_entrust_parity,
)
from aigis_ps.quest_service import QuestService
from aigis_ps.storage import Store
from test_quest_battle_modes import (
    MISSION_NOW, bare_start, gold_events, hard_start, mutate, new_cards, put_record, record, set_rank,
    three_star_gold, update_game_param,
)
from test_quest_service import NOW, allow_quest, assert_refusal, bundle, finish_request, nodes, setup, start_request
from test_quest_settlement import derived, drops, explicit, possession


GATE_QUEST = 1004
STORY_QUEST = 1001
EMPIRE_RANK = 5
EMPIRE_QUESTS = 84
EMPIRE_MISSIONS = 12
SHARED_WITH_EMERGENCY = 42
FOUR_STAR_QUESTS = 54
FIRST_QUEST, FIRST_MISSION = 7317, 940007
NEXT_QUEST = 7318
SLOT_FOUR_QUEST = 7319
KEYED_QUEST, KEYED_MISSION, KEY_QUEST = 8853, 940011, 8724
HARD_QUEST, HARD_MISSION = 60001, 940012
SLOT_FOUR_HARD_QUEST = 60002
EMERGENCY_MISSION = 200384
DARK_CRYSTAL = "trade_item_004000_11"
DARK_CRYSTAL_TYPE, DARK_CRYSTAL_COUNT = 100022, 100
FIRST_DROPS = {22003: 3, 1001: 1, 1005: 1}
FIRST_ITEMS = {DARK_CRYSTAL: 3, "gift_0000_bouquet": 1, "gift_1000_beer": 1}
COLLECTION_UNIT = 418
OTHER_ITEM = "gift_0002_ruby"
UNIT_CARD = 67
CRYSTAL_REWARD_TYPE, UNIT_REWARD_TYPE = 0, 3
SLOT_FOUR_FLAG, COLLECTION_FLAG = SLOT_FLAGS[3], SLOT_FLAGS[4]
PT3_MISSIONS = (642, 643, 644, 645)
NO_PT3_PROGRESS = ((0, 0),) * len(PT3_MISSIONS)
PT3_AFTER_ONE_CLEAR = ((1, 1), (1, 0), (1, 0), (1, 0))
PT3_AFTER_TWO_CLEARS = ((1, 1), (2, 0), (2, 0), (2, 0))
CHARISMA = 1000
GOLD_EVENTS = 2
OVERFLOW = 60
UNKNOWN_DROP = 9999
DECODABLE_TREASURES = MappingProxyType({1: 418, 2: 1000000 + UNIT_CARD, 3: 1003, 4: 2003, 5: 3001, 6: 4001, 7: 5001,
                                        8: 10014})
UNUSED_DROP_TYPES = (2, 5, 6, 7)
TREASURE_FIELDS = tuple(f"Treasure{slot}" for slot in range(1, TREASURE_SLOTS + 1))
MODE_CACHES = ("_mode_definitions", "_modes", "_mode_failures")
FIRST_CRYSTAL = {"kind": "crystal", "reward_type": 0, "count": 0, "card_id": None, "item_id": None}
DARK_CRYSTALS = {"kind": "item", "reward_type": DARK_CRYSTAL_TYPE, "count": DARK_CRYSTAL_COUNT, "card_id": None,
                 "item_id": DARK_CRYSTAL}
START_CHILDREN = ["SYSTEM", "PGP", "LOTTERY", "REWARD", "TREASURE", "TreasureInfo", "PPU"]
FINISH_CHILDREN = ["SYSTEM", "PGP", "PBP", "PPU", "QR", "REWARD", "TREASURE", "TreasureInfo"]
PLATFORM = 1
ENTRY_TAG, ENTRY_MISSION = 801, 940012
INVALID_PLATFORMS = (PLATFORM_CODES.start - 1, PLATFORM_CODES.stop, True, "1", None)
ENTRY_QUESTS = (60001, 60002, 60003, 60004, 60005, 60006)
OTHER_TAG, OTHER_TAG_MISSION = 902, 940001
MENU_CLOSED = "The empire menu needs an open Tag 801 mission showing a quest today"
GATE_LOCKED = "The mode entry requires its gate quest to be cleared"
ENDED_WINDOW = (MISSION_NOW - timedelta(days=2), MISSION_NOW - timedelta(days=1))
FOUR_STARS = 4
CLEARED = MappingProxyType({"MaxRank": 1, "AchieveFlag": CLEAR_FLAG})
BEST_RECORD = MappingProxyType({"MaxRank": FOUR_STARS,
                                "AchieveFlag": CLEAR_FLAG | THREE_STAR_FLAG | HARD_STAR_FLAG | sum(SLOT_FLAGS)})
GATE_RECORDS = MappingProxyType({GATE_QUEST: CLEARED})
# Entrust admits without a request, choosing missions through a selector for its own menu gate.
ENTRUST_SELECTOR = MissionSelector(lambda mission: True, "entrust_menu_closed",
                                   "The entrust menu is closed for every playable mission")
# 679B10 numbers weekdays from Sunday, and MISSION_NOW is a Sunday.
SUNDAY, MONDAY = 1, 2
EVERY_WEEKDAY = 0x7F
ENDED_OPEN_DAYS = MappingProxyType({"StartOpenDay": 1, "EndOpenDay": 2})
CLOSED_TODAY = MappingProxyType({
    "holding_mask": {"_HoldingParam": EVERY_WEEKDAY & ~(1 << (SUNDAY - 1))},
    "open_week": {"OpenWeek": MONDAY},
    "open_days": ENDED_OPEN_DAYS,
})


@pytest.fixture
def empire(setup, bundle):
    service, store = setup
    allow_quest(store, GATE_QUEST)
    set_rank(bundle, store, EMPIRE_RANK)
    update_game_param(store, NowCharisma=CHARISMA)
    return service, store


def refused(code, status, callback):
    with pytest.raises(ProtocolError) as error:
        callback()
    assert (error.value.code, error.value.status) == (code, status)


def children(xml):
    return [child.name for child in loads(xml).children]


def possessed(tree):
    columns = tree["POSSESSION"].children
    return dict(zip(columns[0].values, columns[1].values))


def slot_bits(quest):
    return sum(bit for code, bit in zip(quest.treasures, SLOT_FLAGS) if code)


def candidate_bits(quest):
    return sum({SLOT_FLAGS[candidate.slot - 1] for candidate in quest.candidates})


def selected_mask(store):
    return store.profile().state["quest_state"]["active"]["selected_mask"]


def clear(service, store, quest_id, tick=1000, **result):
    service.start(start_request(service, store, quest_id, tick=tick), store)
    finishing = finish_request(store, quest_id, tick=tick + 1, selected=selected_mask(store), **result)
    return nodes(service.finish(finishing, store))


def unlock_four_star(service, store, quest_id=HARD_QUEST):
    put_record(store, quest_id, flags=CLEAR_FLAG | THREE_STAR_FLAG | slot_bits(service.catalog.get(quest_id)))


def fresh_catalog(bundle, **options):
    shared = bundle.quests.catalog
    return QuestCatalog(shared.snapshot, lambda name: bundle.native_cdn.assets.read(name)[0],
                        possessions=bundle.possessions, resource_version=shared.resource_version, **options)


def start_context(catalog, quest_id, records, *, rank=EMPIRE_RANK, hard=False, platform=PLATFORM, selector=None):
    return StartContext(catalog, catalog.get(quest_id), catalog.mode(quest_id), records, rank, MISSION_NOW, hard,
                        platform, selector=selector)


def admission(catalog, quest_id, records, **options):
    return quest_modes.admit(POLICY, start_context(catalog, quest_id, records, **options))


def best_records(catalog):
    return dict.fromkeys((*catalog.all_ids, *catalog.mode_ids), BEST_RECORD)


def saved_records(store):
    return {row["QuestID"]: row for row in store.profile().state["tables"]["quest_records"]}


def with_quest_rows(tags, rows):
    return MissionTagIndex(tags.registry, quest_rows=MappingProxyType(rows), source_sha256=tags.source_sha256,
                           master_snapshot=tags.master_snapshot, overrides=tags.overrides)


def replace_row(monkeypatch, catalog, quest_id, change):
    monkeypatch.setitem(catalog._all_rows, quest_id, {**catalog._all_rows[quest_id], **change})
    for cache in MODE_CACHES:
        monkeypatch.setattr(catalog, cache, {})


class Memberships:
    def __init__(self, windows, quest_id, missions):
        self.windows, self.quest_id, self.replaced = windows, quest_id, missions

    def missions(self, quest_id):
        return self.replaced if quest_id == self.quest_id else self.windows.missions(quest_id)


def test_empire_policy_opens_type_18_with_four_star_challenges_behind_the_1004_gate():
    assert quest_modes.policy(18) is POLICY
    assert POLICY.status() == {"policy": "empire", "enabled": True, "reason": None, "hard_mode": True,
                               "timed_conditions": False, "gate_quest": GATE_QUEST}
    support = quest_modes.support_status()[18]
    assert (support["mission_category"], support["mission_resources"], support["gold_counted"], support["score_mode"],
            support["hard_rank"], support["entrust"]) == (EMPIRE_CATEGORY, ["Empire", "Emergency"], True, False,
                                                          EMPIRE_RANK, True)


def test_every_empire_quest_builds_from_its_single_enabled_empire_mission(bundle):
    catalog = bundle.quests.catalog
    quests = catalog.playable_quests(18)
    configs = {row["MissionID"]: row for row in catalog.resource_rows(MISSION_CONFIG)}
    assert len(quests) == EMPIRE_QUESTS and len(configs) == EMPIRE_MISSIONS and set(quests) <= set(catalog.mode_ids)
    shared, four_star = [], []
    for quest_id in quests:
        quest, mode = catalog.get(quest_id), catalog.mode(quest_id)
        assert mode.map_mission_id in configs and quest.rank == EMPIRE_RANK
        assert mode.map_archive == f"Map{mode.map_mission_id:04d}_{quest.map_no:04d}.aar"
        assert mode.gold_counted and not mode.score_mode and mode.star_rewards_normal == (StarReward("crystal", 0, 0),)
        if EMERGENCY_MISSION in mode.mission_ids:
            shared.append(quest_id)
        if mode.hard is not None:
            four_star.append(quest_id)
            assert mode.hard.rank == EMPIRE_RANK
            assert mode.star_rewards_hard == (StarReward("item", DARK_CRYSTAL_TYPE, DARK_CRYSTAL_COUNT,
                                                         item_id=configs[mode.map_mission_id]["DarkCrystalItemId"]),)
    assert (len(shared), len(four_star)) == (SHARED_WITH_EMERGENCY, FOUR_STAR_QUESTS)
    assert {row["DarkCrystalItemId"] for row in configs.values()} == {DARK_CRYSTAL}


@pytest.mark.parametrize("gate_record", [None, (3, THREE_STAR_FLAG), (3, INT32_MAX & ~CLEAR_FLAG)])
def test_empire_quests_require_a_cleared_1004_record(setup, bundle, gate_record):
    service, store = setup
    set_rank(bundle, store, EMPIRE_RANK)
    if gate_record is not None:
        put_record(store, GATE_QUEST, max_rank=gate_record[0], flags=gate_record[1])
    before = store.profile()
    refused("quest_locked", 409, lambda: service.start(start_request(service, store, FIRST_QUEST), store))
    # check_hard would answer quest_hard_locked here, so quest_locked proves the gate runs first.
    refused("quest_locked", 409, lambda: service.start(hard_start(service, store, HARD_QUEST, tick=1001), store))
    assert store.profile() == before
    put_record(store, GATE_QUEST, max_rank=1, flags=CLEAR_FLAG)
    service.start(start_request(service, store, FIRST_QUEST, tick=1002), store)
    assert store.profile().state["quest_state"]["active"]["mission_id"] == FIRST_MISSION


def test_the_tag_801_menu_gate_reaches_entrust_through_its_declared_check_start_parity():
    assert POLICY.entrust_parity == frozenset({"check_start"}) and POLICY.entrust_blocker() is None
    validate_entrust_parity(POLICY)


def test_tag_801_names_an_open_empire_mission_that_admits_every_empire_quest(bundle):
    catalog = bundle.quests.catalog
    tags = catalog.mission_tags()
    menu = tags.tag_mission(ENTRY_TAG, PLATFORM)
    assert (menu.mission_id, menu.category, tags.registry.members(ENTRY_MISSION)) == (
        ENTRY_MISSION, EMPIRE_CATEGORY, ENTRY_QUESTS)
    assert {tags.tag_mission(ENTRY_TAG, platform) for platform in PLATFORM_CODES} == {menu}
    assert all(POLICY.entry_open(catalog, now=MISSION_NOW, platform_code=platform) for platform in PLATFORM_CODES)
    records = best_records(catalog)
    starts = [(quest_id, hard) for quest_id in catalog.playable_quests(EMPIRE_TYPE) for hard in (False, True)
              if not hard or catalog.mode(quest_id).hard is not None]
    owners = {admission(catalog, quest_id, records, rank=INT32_MAX, hard=hard).category for quest_id, hard in starts}
    assert (len(starts), owners) == (EMPIRE_QUESTS + FOUR_STAR_QUESTS, {EMPIRE_CATEGORY})


def test_an_ended_tag_801_mission_closes_empire_battles_and_entrust_admission(empire, bundle):
    _, store = empire
    catalog = fresh_catalog(bundle, window_overrides={ENTRY_MISSION: ENDED_WINDOW})
    tags = catalog.mission_tags()
    assert tags.tag_mission(ENTRY_TAG, PLATFORM).mission_id == ENTRY_MISSION
    assert not tags.is_open(TAG_LOOKUP, ENTRY_TAG, now=MISSION_NOW, platform_code=PLATFORM)
    preview = bundle.quests.catalog.preview_mission_tags({ENTRY_MISSION: ENDED_WINDOW})
    previewed = SimpleNamespace(mission_tags=lambda: preview)
    assert not any(POLICY.entry_open(source, now=MISSION_NOW, platform_code=PLATFORM) for source in (catalog, previewed))
    assert tags.tag_mission(OTHER_TAG, PLATFORM).mission_id == OTHER_TAG_MISSION
    assert tags.is_open(TAG_LOOKUP, OTHER_TAG, now=MISSION_NOW, platform_code=PLATFORM)
    assert catalog.mission_windows(EMPIRE_TYPE).mission(FIRST_MISSION).state(MISSION_NOW) == OPEN
    service = QuestService(catalog, bundle.bootstrap, bundle.player_state, bundle.possessions,
                           clock=lambda: NOW, randbelow=lambda _: 0)
    before = store.profile()
    assert_refusal("quest_closed", MENU_CLOSED,
                   lambda: service.start(start_request(service, store, FIRST_QUEST), store))
    assert store.profile() == before
    records = saved_records(store)
    assert_refusal("quest_closed", MENU_CLOSED,
                   lambda: admission(catalog, FIRST_QUEST, records, selector=ENTRUST_SELECTOR))
    assert admission(bundle.quests.catalog, FIRST_QUEST, records, selector=ENTRUST_SELECTOR).mission_id == FIRST_MISSION
    challenged = best_records(catalog)
    assert admission(bundle.quests.catalog, KEYED_QUEST, challenged, hard=True).mission_id == KEYED_MISSION
    assert_refusal("quest_closed", MENU_CLOSED, lambda: admission(catalog, KEYED_QUEST, challenged, hard=True))
    assert_refusal("quest_locked", GATE_LOCKED, lambda: POLICY.check_start(start_context(catalog, FIRST_QUEST, {})))
    assert_refusal("quest_closed", MENU_CLOSED,
                   lambda: POLICY.check_start(start_context(catalog, FIRST_QUEST, GATE_RECORDS)))


def test_check_start_asks_the_policy_entry_gate_after_the_inherited_rules(bundle, monkeypatch):
    catalog, asked = bundle.quests.catalog, []

    def closed(source, *, now, platform_code):
        asked.append((source, now, platform_code))
        return False

    monkeypatch.setattr(POLICY, "entry_open", closed)
    assert_refusal("quest_locked", GATE_LOCKED, lambda: POLICY.check_start(start_context(catalog, FIRST_QUEST, {})))
    assert asked == []
    assert_refusal("quest_closed", MENU_CLOSED, lambda: admission(catalog, FIRST_QUEST, GATE_RECORDS))
    assert asked == [(catalog, MISSION_NOW, PLATFORM)]
    monkeypatch.setattr(POLICY, "entry_open", lambda source, *, now, platform_code: True)
    assert admission(catalog, FIRST_QUEST, GATE_RECORDS).mission_id == FIRST_MISSION


@pytest.mark.parametrize("closure", CLOSED_TODAY.values(), ids=list(CLOSED_TODAY))
def test_the_menu_entry_needs_a_tag_801_member_quest_open_on_the_jst_day(bundle, monkeypatch, closure):
    catalog = bundle.quests.catalog
    tags = catalog.mission_tags()
    closed = {**catalog._all_rows, **{quest_id: {**catalog.row(quest_id), **closure} for quest_id in ENTRY_QUESTS}}
    closed_tags = with_quest_rows(tags, closed)
    assert closed_tags.is_open(TAG_LOOKUP, ENTRY_TAG, now=MISSION_NOW, platform_code=PLATFORM)
    assert not closed_tags.displayable(TAG_LOOKUP, ENTRY_TAG, now=MISSION_NOW, platform_code=PLATFORM)
    monkeypatch.setattr(catalog, "mission_tags", lambda: closed_tags)
    assert_refusal("quest_closed", MENU_CLOSED, lambda: admission(catalog, FIRST_QUEST, GATE_RECORDS))
    reopened = with_quest_rows(tags, {**closed, ENTRY_QUESTS[-1]: catalog.row(ENTRY_QUESTS[-1])})
    monkeypatch.setattr(catalog, "mission_tags", lambda: reopened)
    assert admission(catalog, FIRST_QUEST, GATE_RECORDS).mission_id == FIRST_MISSION


# The native && evaluates HasAnyDisplayableQuest first and skips IsOpenMission once it is false.
@pytest.mark.parametrize("displayable,opened,asked", [(False, True, ("displayable",)),
                                                      (True, False, ("displayable", "is_open"))])
def test_the_menu_entry_needs_both_native_predicates_of_tag_801(bundle, monkeypatch, displayable, opened, asked):
    catalog = bundle.quests.catalog
    calls = []

    def answer(name, result):
        def predicate(kind, value, *, now, platform_code):
            calls.append((name, kind, value, now, platform_code))
            return result
        return predicate

    answers = SimpleNamespace(displayable=answer("displayable", displayable), is_open=answer("is_open", opened))
    monkeypatch.setattr(catalog, "mission_tags", lambda: answers)
    assert_refusal("quest_closed", MENU_CLOSED, lambda: admission(catalog, FIRST_QUEST, GATE_RECORDS))
    assert calls == [(name, TAG_LOOKUP, ENTRY_TAG, MISSION_NOW, PLATFORM) for name in asked]


@pytest.mark.parametrize("platform", INVALID_PLATFORMS)
def test_the_menu_entry_refuses_platform_codes_outside_the_native_range(bundle, platform):
    catalog = bundle.quests.catalog
    records = {GATE_QUEST: CLEARED, KEY_QUEST: CLEARED}
    assert all(type(mission.enable) is bool for mission in catalog.mission_windows(EMPIRE_TYPE).missions(KEYED_QUEST))
    assert admission(catalog, KEYED_QUEST, records).mission_id == KEYED_MISSION
    refused("quest_platform_invalid", 400, lambda: admission(catalog, KEYED_QUEST, records, platform=platform))


def test_an_unavailable_tag_registry_refuses_empire_entry_with_its_own_error(bundle, monkeypatch):
    catalog = bundle.quests.catalog

    def unavailable():
        raise ProtocolError("quest_mode_resource_invalid", "Mission windows do not satisfy the native contract", 503)

    monkeypatch.setattr(catalog, "mission_tags", unavailable)
    refused("quest_mode_resource_invalid", 503, lambda: admission(catalog, FIRST_QUEST, GATE_RECORDS))


def test_empire_quests_open_at_their_native_rank_five(setup, bundle):
    service, store = setup
    allow_quest(store, GATE_QUEST)
    set_rank(bundle, store, EMPIRE_RANK - 1)
    before = store.profile()
    refused("quest_rank_required", 409, lambda: service.start(start_request(service, store, FIRST_QUEST), store))
    assert store.profile() == before
    set_rank(bundle, store, EMPIRE_RANK)
    service.start(start_request(service, store, FIRST_QUEST, tick=1001), store)
    assert store.profile().state["quest_state"]["active"]["quest_id"] == FIRST_QUEST


def test_key_quests_lock_an_empire_mission_until_the_previous_chapter_is_cleared(empire):
    service, store = empire
    assert service.catalog.get(KEYED_QUEST).prerequisite == 0
    assert service.catalog.mission_windows(18).mission(KEYED_MISSION).key_quest == KEY_QUEST
    before = store.profile()
    refused("quest_locked", 409, lambda: service.start(start_request(service, store, KEYED_QUEST), store))
    assert store.profile() == before
    put_record(store, KEY_QUEST, flags=THREE_STAR_FLAG)
    unlock_four_star(service, store, KEYED_QUEST)
    before = store.profile()
    refused("quest_locked", 409, lambda: service.start(start_request(service, store, KEYED_QUEST, tick=1001), store))
    refused("quest_locked", 409, lambda: service.start(hard_start(service, store, KEYED_QUEST, tick=1002), store))
    assert store.profile() == before
    put_record(store, KEY_QUEST, max_rank=1, flags=CLEAR_FLAG)
    service.start(start_request(service, store, KEYED_QUEST, tick=1003), store)
    assert store.profile().state["quest_state"]["active"]["mission_id"] == KEYED_MISSION
    service.start(hard_start(service, store, KEYED_QUEST, tick=1004), store)
    active = store.profile().state["quest_state"]["active"]
    assert (active["mission_id"], active["mode"]["difficulty"]) == (KEYED_MISSION, "hard")


def test_a_chapter_opening_clear_grants_drops_the_collection_unit_counted_gold_and_the_first_crystal(empire):
    service, store = empire
    quest = service.catalog.get(FIRST_QUEST)
    assert sorted(candidate.slot for candidate in quest.candidates) == [1, 2, 3]
    assert (quest.treasures[3], quest.treasures[4]) == (0, COLLECTION_UNIT)
    response = service.start(start_request(service, store, FIRST_QUEST), store)
    assert children(response) == START_CHILDREN
    active = store.profile().state["quest_state"]["active"]
    assert active["mission_id"] == FIRST_MISSION
    assert active["unit_reservations"] == [
        {"card_id": COLLECTION_UNIT, "level": service.rules.collection_unit_level, "count": 1}]
    assert active["mode"] == {"quest_type": 18, "difficulty": "normal", "star_rewards": [FIRST_CRYSTAL],
                              "boost_ignored": None}
    selected, wallet = active["selected_mask"], wallet_totals(store.profile().state)["free"]
    owned = {item: possession(store, item) for item in FIRST_ITEMS}
    finishing = finish_request(store, FIRST_QUEST, obtained=selected, selected=selected, gold=GOLD_EVENTS,
                               extra=gold_events(GOLD_EVENTS))
    response = service.finish(finishing, store)
    completed = store.profile()
    assert service.finish(finishing, store) == response and store.profile() == completed
    tree = nodes(response)
    assert children(response) == FINISH_CHILDREN + ["POSSESSION", "PFC"]
    assert drops(tree) == {**FIRST_DROPS, COLLECTION_UNIT: 1} and new_cards(tree) == (COLLECTION_UNIT,)
    assert possessed(tree) == {item: owned[item] + count for item, count in FIRST_ITEMS.items()}
    assert tree["REWARD"].children[0].values == (quest.rank_exp,)
    settled = three_star_gold(quest.gold + GOLD_EVENTS * service.rules.gold_per_event)
    assert tree["REWARD"].children[1].values == (settled,)
    assert wallet_totals(completed.state)["free"] == wallet + service.rules.first_three_star_crystals
    row = record(store, FIRST_QUEST)
    assert (row["MaxRank"], row["AchieveFlag"]) == (
        3, CLEAR_FLAG | THREE_STAR_FLAG | candidate_bits(quest) | COLLECTION_FLAG)
    history = completed.state["quest_state"]["history"][-1]
    assert (history["mission_id"], history["gold_events"], history["crystals"], history["mode_presents"]) == (
        FIRST_MISSION, GOLD_EVENTS, service.rules.first_three_star_crystals, [])


def test_the_next_empire_quest_opens_once_its_appear_condition_is_cleared(empire):
    service, store = empire
    assert service.catalog.get(NEXT_QUEST).prerequisite == FIRST_QUEST
    before = store.profile()
    refused("quest_locked", 409, lambda: service.start(start_request(service, store, NEXT_QUEST), store))
    assert store.profile() == before
    clear(service, store, FIRST_QUEST, tick=1001, stars=1)
    service.start(start_request(service, store, NEXT_QUEST, tick=1003), store)
    assert store.profile().state["quest_state"]["active"]["quest_id"] == NEXT_QUEST


def test_slot_four_prize_rows_grant_the_fourth_treasure_and_set_its_record_bit(empire):
    service, store = empire
    allow_quest(store, NEXT_QUEST)
    quest = service.catalog.get(SLOT_FOUR_QUEST)
    fourth = [index for index, candidate in enumerate(quest.candidates) if candidate.slot == 4]
    assert fourth and quest.treasures[3]
    unseen = explicit(quest, (0,) * len(quest.candidates))
    service.start(start_request(service, store, SLOT_FOUR_QUEST, candidates=unseen), store)
    obtained = sum(1 << index for index in fourth)
    finishing = finish_request(store, SLOT_FOUR_QUEST, obtained=obtained, selected=selected_mask(store), stars=1)
    tree = nodes(service.finish(finishing, store))
    assert drops(tree) == {quest.treasures[3]: service.catalog.decode_reward(quest.treasures[3]).count * len(fourth)}
    assert record(store, SLOT_FOUR_QUEST)["AchieveFlag"] == CLEAR_FLAG | SLOT_FOUR_FLAG
    seen = explicit(quest, tuple(int(candidate.slot == 4) for candidate in quest.candidates))
    service.start(start_request(service, store, SLOT_FOUR_QUEST, tick=1002, candidates=seen), store)
    assert store.profile().state["quest_state"]["active"]["quest_id"] == SLOT_FOUR_QUEST


def test_a_first_four_star_empire_result_grants_the_mission_dark_crystals_once(empire):
    service, store = empire
    quest = service.catalog.get(HARD_QUEST)
    unlock_four_star(service, store)
    response = service.start(hard_start(service, store, HARD_QUEST), store)
    assert not nodes(response)["LOTTERY"].children[0].values
    active = store.profile().state["quest_state"]["active"]
    assert (active["registration"], active["mission_id"], active["selected_mask"]) == (82, HARD_MISSION, 0)
    assert active["mode"] == {"quest_type": 18, "difficulty": "hard", "star_rewards": [DARK_CRYSTALS],
                              "boost_ignored": None}
    granted = possession(store, DARK_CRYSTAL) + DARK_CRYSTAL_COUNT
    response = service.finish(finish_request(store, HARD_QUEST, stars=4), store)
    tree = nodes(response)
    assert children(response) == FINISH_CHILDREN + ["POSSESSION"]
    assert possessed(tree) == {DARK_CRYSTAL: granted} and not drops(tree) and not new_cards(tree)
    row = record(store, HARD_QUEST)
    assert (row["MaxRank"], row["AchieveFlag"]) == (4, CLEAR_FLAG | THREE_STAR_FLAG | HARD_STAR_FLAG | slot_bits(quest))
    assert store.profile().state["quest_state"]["history"][-1]["star_rewards"] == [DARK_CRYSTALS]
    service.start(hard_start(service, store, HARD_QUEST, tick=1002), store)
    response = service.finish(finish_request(store, HARD_QUEST, tick=1003, stars=4), store)
    assert "POSSESSION" not in nodes(response) and possession(store, DARK_CRYSTAL) == granted


def test_four_star_empire_challenges_need_three_stars_and_every_nonempty_treasure_bit(empire):
    service, store = empire
    opener = slot_bits(service.catalog.get(HARD_QUEST))
    follower = slot_bits(service.catalog.get(SLOT_FOUR_HARD_QUEST))
    assert opener & COLLECTION_FLAG and not opener & SLOT_FOUR_FLAG and follower & SLOT_FOUR_FLAG
    locked = ((HARD_QUEST, 2, opener), (HARD_QUEST, 3, opener & ~COLLECTION_FLAG),
              (SLOT_FOUR_HARD_QUEST, 3, follower & ~SLOT_FOUR_FLAG))
    for tick, (quest_id, max_rank, slots) in enumerate(locked, 1000):
        put_record(store, quest_id, max_rank=max_rank, flags=CLEAR_FLAG | slots)
        before = store.profile()
        refused("quest_hard_locked", 409, lambda: service.start(hard_start(service, store, quest_id, tick=tick), store))
        assert store.profile() == before
    put_record(store, SLOT_FOUR_HARD_QUEST, flags=CLEAR_FLAG | follower)
    service.start(hard_start(service, store, SLOT_FOUR_HARD_QUEST, tick=1010), store)
    assert store.profile().state["quest_state"]["active"]["mode"]["difficulty"] == "hard"
    put_record(store, FIRST_QUEST)
    before = store.profile()
    refused("quest_hard_unavailable", 409,
            lambda: service.start(hard_start(service, store, FIRST_QUEST, tick=1011), store))
    assert store.profile() == before


def test_four_star_dark_crystals_beyond_the_item_stack_are_discarded(empire, bundle):
    service, store = empire
    stack = bundle.possessions.stack(DARK_CRYSTAL)
    mutate(store, lambda state: bundle.possessions.credit(state, {DARK_CRYSTAL: stack - DARK_CRYSTAL_COUNT + OVERFLOW}))
    unlock_four_star(service, store)
    service.start(hard_start(service, store, HARD_QUEST), store)
    tree = nodes(service.finish(finish_request(store, HARD_QUEST, stars=4), store))
    assert possessed(tree) == {DARK_CRYSTAL: stack} and possession(store, DARK_CRYSTAL) == stack
    assert dict(zip(tree["IOF"].children[0].values, tree["IOF"].children[1].values)) == {DARK_CRYSTAL: OVERFLOW}
    assert store.profile().state["quest_state"]["history"][-1]["discarded"] == {DARK_CRYSTAL: OVERFLOW}


@pytest.mark.parametrize("quest_type", [0, 16, True, 18.0, "18", None])
def test_rows_of_other_quest_types_are_refused(bundle, quest_type):
    catalog = bundle.quests.catalog
    row = catalog.row(FIRST_QUEST)
    assert POLICY.validate_row(row, catalog) is None
    refused("quest_definition_invalid", 503, lambda: POLICY.validate_row({**row, "Type": quest_type}, catalog))


@pytest.mark.parametrize("memberships,code,status", [
    (lambda emergency, empire: (), "quest_closed", 409),
    (lambda emergency, empire: (emergency,), "quest_closed", 409),
    (lambda emergency, empire: (replace(emergency, enable=True),), "quest_mode_resource_invalid", 503),
    (lambda emergency, empire: (replace(emergency, enable=True), empire), "quest_mode_resource_invalid", 503),
    (lambda emergency, empire: (empire, replace(empire, mission_id=HARD_MISSION)), "quest_mode_resource_invalid", 503),
])
def test_empire_rows_need_exactly_one_enabled_empire_mission(bundle, monkeypatch, memberships, code, status):
    catalog = bundle.quests.catalog
    windows = catalog.mission_windows(18)
    listed = windows.missions(FIRST_QUEST)
    emergency = next(mission for mission in listed if mission.category != EMPIRE_CATEGORY)
    empire = next(mission for mission in listed if mission.category == EMPIRE_CATEGORY)
    assert (emergency.mission_id, emergency.listed, empire.mission_id, empire.listed) == (
        EMERGENCY_MISSION, False, FIRST_MISSION, True)
    assert POLICY.validate_row(catalog.row(FIRST_QUEST), catalog) is None
    replaced = Memberships(windows, FIRST_QUEST, memberships(emergency, empire))
    monkeypatch.setattr(catalog, "mission_windows", lambda quest_type: replaced)
    refused(code, status, lambda: POLICY.validate_row(catalog.row(FIRST_QUEST), catalog))


@pytest.mark.parametrize("change", [
    {"HardMode_RewardItemID": OTHER_ITEM},
    {"HardMode_RewardItemID": "null"},
    {"HardMode_RewardType": CRYSTAL_REWARD_TYPE, "HardMode_RewardCount": 0, "HardMode_RewardItemID": "null"},
    {"HardMode_RewardType": UNIT_REWARD_TYPE, "HardMode_RewardCount": 1, "HardMode_RewardCardID": UNIT_CARD},
])
def test_four_star_rewards_must_be_the_owning_mission_dark_crystal(bundle, change):
    catalog = bundle.quests.catalog
    row = catalog.row(HARD_QUEST)
    assert POLICY.validate_row(row, catalog) is None
    refused("quest_mode_resource_invalid", 503, lambda: POLICY.validate_row({**row, **change}, catalog))


def test_the_dark_crystal_is_read_from_the_owning_empire_mission(bundle, monkeypatch):
    catalog = bundle.quests.catalog
    original = catalog.resource_rows

    def renamed(name):
        rows = original(name)
        if name != MISSION_CONFIG:
            return rows
        return tuple({**row, "DarkCrystalItemId": OTHER_ITEM} if row["MissionID"] == HARD_MISSION else row
                     for row in rows)

    monkeypatch.setattr(catalog, "resource_rows", renamed)
    refused("quest_mode_resource_invalid", 503, lambda: POLICY.validate_row(catalog.row(HARD_QUEST), catalog))
    assert catalog.row(KEYED_QUEST)["_HardLevel"] and POLICY.validate_row(catalog.row(KEYED_QUEST), catalog) is None
    monkeypatch.setattr(catalog, "resource_rows", lambda name: tuple(
        row for row in original(name) if name != MISSION_CONFIG or row["MissionID"] != HARD_MISSION))
    refused("quest_mode_resource_invalid", 503, lambda: POLICY.validate_row(catalog.row(HARD_QUEST), catalog))


def test_four_star_fields_of_quests_without_the_challenge_are_not_checked(bundle):
    catalog = bundle.quests.catalog
    row = catalog.row(FIRST_QUEST)
    assert row["_HardLevel"] == 0
    assert POLICY.validate_row({**row, "HardMode_RewardType": DARK_CRYSTAL_TYPE, "HardMode_RewardCount": 1,
                                "HardMode_RewardItemID": OTHER_ITEM}, catalog) is None


@pytest.mark.parametrize("field", TREASURE_FIELDS)
@pytest.mark.parametrize("drop_type,code", DECODABLE_TREASURES.items(), ids=[f"type{key}" for key in DECODABLE_TREASURES])
def test_treasures_of_every_decodable_drop_type_are_accepted_in_every_slot(bundle, drop_type, code, field):
    catalog = bundle.quests.catalog
    assert catalog.decode_reward(code).drop_type == drop_type
    assert POLICY.validate_row({**catalog.row(FIRST_QUEST), field: code}, catalog) is None


def test_an_empire_quest_builds_with_drop_types_the_resources_do_not_use_yet(bundle, monkeypatch):
    catalog = bundle.quests.catalog
    treasures = (*(DECODABLE_TREASURES[drop_type] for drop_type in UNUSED_DROP_TYPES), COLLECTION_UNIT)
    replace_row(monkeypatch, catalog, FIRST_QUEST, dict(zip(TREASURE_FIELDS, treasures)))
    assert catalog.get(FIRST_QUEST).treasures == treasures
    assert catalog.status()["mode_failed_quests"] == {}


def test_undecodable_treasures_are_refused_with_the_catalog_reward_error(bundle, monkeypatch):
    catalog = bundle.quests.catalog
    replace_row(monkeypatch, catalog, FIRST_QUEST, {"Treasure2": UNKNOWN_DROP})
    refused("quest_reward_unknown", 503, lambda: catalog.get(FIRST_QUEST))
    assert catalog.status()["mode_failed_quests"][FIRST_QUEST]["code"] == "quest_reward_unknown"


def test_an_empire_row_off_the_contract_is_refused_before_charging(empire, bundle):
    _, store = empire
    catalog = fresh_catalog(bundle)
    catalog._all_rows[HARD_QUEST] = {**catalog._all_rows[HARD_QUEST], "HardMode_RewardItemID": OTHER_ITEM}
    service = QuestService(catalog, bundle.bootstrap, bundle.player_state, bundle.possessions,
                           clock=lambda: NOW, randbelow=lambda _: 0)
    before = store.profile()
    refused("quest_mode_resource_invalid", 503, lambda: service.start(bare_start(service, store, HARD_QUEST), store))
    assert store.profile() == before
    assert catalog.status()["mode_failed_quests"][HARD_QUEST]["code"] == "quest_mode_resource_invalid"
    service.start(start_request(service, store, FIRST_QUEST, tick=1001), store)
    assert store.profile().state["quest_state"]["active"]["quest_id"] == FIRST_QUEST


def test_any_empire_clear_counts_for_the_permanent_pt3_missions(derived, bundle, tmp_path):
    for condition_id in PT3_MISSIONS:
        spec = derived.missions.progress.clear_specs.get(condition_id)
        assert (spec.claimed_by, spec.quest_types, spec.quest_ids, spec.kind_conflict, spec.resolved) == (
            "empire", (18,), None, True, True)
    initial = derived.missions.initialize(bundle.player_state.initialize(derived.bootstrap.initial_state()))
    store = Store(tmp_path / "empire.sqlite3", initial)
    allow_quest(store, GATE_QUEST)
    set_rank(bundle, store, EMPIRE_RANK)
    update_game_param(store, NowCharisma=CHARISMA)

    def progress():
        rows = {row["ID"]: row for row in derived.missions.progress.snapshot(store.profile().state, MISSION_NOW)}
        return tuple((rows[key]["Progress"], rows[key]["Status"]) for key in PT3_MISSIONS)

    assert progress() == NO_PT3_PROGRESS
    clear(derived.quests, store, STORY_QUEST)
    assert progress() == NO_PT3_PROGRESS
    clear(derived.quests, store, FIRST_QUEST, tick=1002, stars=1)
    assert progress() == PT3_AFTER_ONE_CLEAR
    update_game_param(store, NowCharisma=CHARISMA)
    unlock_four_star(derived.quests, store)
    derived.quests.start(hard_start(derived.quests, store, HARD_QUEST, tick=1004), store)
    derived.quests.finish(finish_request(store, HARD_QUEST, tick=1005, stars=4), store)
    assert progress() == PT3_AFTER_TWO_CLEARS
