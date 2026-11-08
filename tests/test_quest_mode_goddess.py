from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, Lock
from types import MappingProxyType

import pytest

from aigis_ps import quest_mode_goddess, quest_modes
from aigis_ps.alxml import loads
from aigis_ps.errors import ProtocolError
from aigis_ps.item_summon_tickets import ItemSummonTicketService
from aigis_ps.present_backlog import MAX_PRESENT_TEXT_BYTES
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.quest_mode_goddess import (
    BEST_RESULT_STARS, CONFIG_TABLE, DESCRIPTION, GRANTED_FIELD, POLICY, REWARD_TABLE, TOTAL_STAR_FIELD,
    TOTAL_STAR_KEY, reward_tiers,
)
from aigis_ps.quest_modes import CLEAR_FLAG, MODE_PROGRESS, THREE_STAR_FLAG
from aigis_ps.summon_tickets import SummonTicketRules
from test_quest_battle_modes import hard_start, mutate, put_record, record, set_rank
from test_quest_service import bundle, exit_request, finish_request, nodes, request, setup, start_request
from test_quest_settlement import Mailbox


GODDESS_TYPE, GODDESS_CATEGORY = 19, 17
GODDESS_RANK = 50
GODDESS_QUESTS, FIXED_TEAM_QUESTS = 21, 5
MAIN_QUEST = 1001
FIRST_MISSION, OTHER_MISSION = 950001, 950002
FIRST_MISSION_QUESTS = (16001, 16002, 16003, 16004, 16005)
FIRST_QUEST, SECOND_QUEST, THIRD_QUEST, FOURTH_QUEST = FIRST_MISSION_QUESTS[:4]
NEXT_MISSION_QUEST = 16101
# Mission 950000 only feeds the top banner, yet its quest still counts toward the saved total.
BANNER_QUEST, BANNER_MISSION = 16000, 950000
FIXED_TEAM_QUEST, FIXED_TEAM_MISSION = 16201, 950003
LAST_QUEST = 16305
MISSING_TEAM = 999999
NEEDED_STARS = 5
TIER_COUNT, ITEM_TIERS = 60, 54
UNIT_TIERS = {15: 173, 25: 512, 35: 512, 40: 122, 45: 1930, 60: 173}
TEMPLATE_CARDS = {173: 250, 512: 570, 122: 194, 1930: 2749}
# GoddessRevivalExclanation promises this black unit at 45 stars, which only the template reading delivers.
AEGIS_TIER, AEGIS_NAME = 45, "アイギスの神影イージス"
ITEM_PRESENT, UNIT_PRESENT = 22, 1
FIRST_TIERS = [(ITEM_PRESENT, 1, "trade_item_001000", DESCRIPTION), (ITEM_PRESENT, 10, "gift_0000_bouquet", DESCRIPTION),
               (ITEM_PRESENT, 5, "gift_1000_beer", DESCRIPTION)]
THIRD_TIER, THIRD_TIER_ITEM = 3, "gift_1000_beer"
# Tiers 4, 9 and 14 share this item, so the preflight test opens a window that holds tier 9 alone.
NINTH_TIER_ITEM = "crystal_item_001000"
# Present Type 2 is gold, a form the goddess reward table never uses.
UNMAPPED_REWARD_TYPE = 2
# Summon pool 418 backs these tiers and is enabled by the default item ticket policy.
SUMMON_TICKET_TIERS = {5: "item_smn_ticket_85125", 10: "item_smn_ticket_85125", 30: "item_smn_ticket_19134",
                       50: "item_smn_ticket_85125", 55: "item_smn_ticket_85125"}
# Item tickets arrive one per present, whatever the tier amount.
TICKETS_PER_PRESENT = 1
UNDELIVERABLE_FIELDS = frozenset(("threshold", "reward_type", "param", "item_id", "reason"))
GAME_PARAM2_REGISTRATION = 35
STORY_TYPE = 0
RACERS, RACE_SECONDS = 2, 10


class RefusingPresents:
    def __init__(self, presents, refused_item):
        self.presents, self.refused_item = presents, refused_item

    def preview(self, rewards, existing_count):
        if any(reward.get("item_id") == self.refused_item for reward in rewards):
            raise ProtocolError("present_item_unsupported", "The item is no longer mailable", 501)
        return self.presents.preview(rewards, existing_count)

    def __getattr__(self, name):
        return getattr(self.presents, name)


@pytest.fixture
def goddess(setup, bundle):
    service, store = setup
    set_rank(bundle, store, GODDESS_RANK)
    service.presents = bundle.presents
    return service, store


@pytest.fixture(scope="module")
def closed_ticket_pools(bundle):
    tickets = bundle.item_summon_tickets
    backing = {tickets.catalog.definition(ticket).pool_id for ticket in SUMMON_TICKET_TIERS.values()}
    return ItemSummonTicketService(bundle.bootstrap, bundle.player_state, bundle.presents, bundle.summon_pools,
                                   tickets.catalog, rules=SummonTicketRules(tuple(sorted(
                                       set(tickets.rules.enabled_pools) - backing))))


def refused(code, status, callback):
    with pytest.raises(ProtocolError) as error:
        callback()
    assert (error.value.code, error.value.status) == (code, status)


def save_total(store, total):
    def change(state):
        state["tables"]["game_param2"][0][TOTAL_STAR_FIELD] = total
        return state
    mutate(store, change)


def saved_total(store):
    return store.profile().state["tables"]["game_param2"][0][TOTAL_STAR_FIELD]


def rewarded(store):
    return store.profile().state["quest_state"].get(MODE_PROGRESS, {}).get(POLICY.name)


def save_progress(store, progress):
    def change(state):
        state["quest_state"][MODE_PROGRESS] = progress
        return state
    mutate(store, change)


def seed_stars(store, stars):
    for quest_id, max_rank in stars.items():
        put_record(store, quest_id, max_rank=max_rank, flags=CLEAR_FLAG | (THREE_STAR_FLAG if max_rank == 3 else 0))
    save_total(store, sum(stars.values()))


def spread(total, quest_ids):
    stars, left = {}, total
    for quest_id in quest_ids:
        if not left:
            break
        stars[quest_id] = min(BEST_RESULT_STARS, left)
        left -= stars[quest_id]
    assert not left
    return stars


def race_commits(store, monkeypatch):
    original, barrier, lock, calls = store.update_state, Barrier(RACERS), Lock(), []

    def update(state, revision):
        with lock:
            calls.append(revision)
            racing = len(calls) <= RACERS
        if racing:
            barrier.wait(timeout=RACE_SECONDS)
        return original(state, revision)

    monkeypatch.setattr(store, "update_state", update)


def drop_record(store, quest_id):
    def change(state):
        state["tables"]["quest_records"] = [row for row in state["tables"]["quest_records"] if row["QuestID"] != quest_id]
        return state
    mutate(store, change)


def show_record(store, quest_id):
    def change(state):
        for row in state["tables"]["quest_records"]:
            if row["QuestID"] == quest_id:
                row["Shown"] = True
        return state
    mutate(store, change)


def clear(service, store, quest_id, tick=1000, stars=3):
    service.start(start_request(service, store, quest_id, tick=tick), store)
    selected = store.profile().state["quest_state"]["active"]["selected_mask"]
    return service.finish(finish_request(store, quest_id, tick=tick + 1, stars=stars, selected=selected), store)


def child_names(xml):
    return [child.name for child in loads(xml).children]


def total_update(tree):
    columns = tree["PGP2"].children
    return dict(zip(columns[0].values, columns[1].values))


def settled_presents(bundle, store, key):
    saved = store.profile().state
    rows = {row["PresentID"]: row for row in bundle.presents.rows(saved)}
    return [(rows[present_id]["Type"], rows[present_id]["Content"], rows[present_id].get("ItemID"),
             rows[present_id]["Description"]) for present_id in saved["quest_state"]["history"][-1][key]]


def mailed(bundle, store):
    return settled_presents(bundle, store, "mode_presents")


def delivered(bundle, store):
    return settled_presents(bundle, store, "delivered_presents")


def expected_mail(reward):
    if reward["kind"] == "unit":
        return UNIT_PRESENT, reward["template_id"], None, reward["description"]
    return ITEM_PRESENT, reward["amount"], reward["item_id"], reward["description"]


def tier_rewards(catalog):
    return {tier.threshold: tier.present() for tier in reward_tiers(catalog)}


def patch_rows(monkeypatch, catalog, table, change):
    original = catalog.resource_rows
    rows = tuple(change([dict(row) for row in original(table)]))
    monkeypatch.setattr(catalog, "resource_rows", lambda name: rows if name == table else original(name))


def drop_first_quest(rows):
    return [row for row in rows if row["QuestId"] != FIRST_QUEST]


def move_first_quest(rows):
    return [{**row, "MissionGroupId": OTHER_MISSION} if row["QuestId"] == FIRST_QUEST else row for row in rows]


def text_star_need(rows):
    return [{**row, "NeedStarNum": str(NEEDED_STARS)} if row["QuestId"] == FIRST_QUEST else row for row in rows]


def drop_need_column(rows):
    return [{key: value for key, value in row.items() if key != "NeedStarNum"} for row in rows]


def repeat_threshold(rows):
    return [*rows, rows[0]]


def drop_item_column(rows):
    return [{key: value for key, value in row.items() if key != "GeneralItemID"} for row in rows]


def zero_threshold(rows):
    return [{**rows[0], "RequiredStarCount": 0}, *rows[1:]]


def text_reward_param(rows):
    return [{**rows[0], "RewardParam": str(rows[0]["RewardParam"])}, *rows[1:]]


def unmapped_second_tier(rows):
    return [{**row, "RewardType": UNMAPPED_REWARD_TYPE} if row["RequiredStarCount"] == 2 else row for row in rows]


def star_need_row(mission_id, stars):
    return {"MissionGroupId": mission_id, "QuestId": FIRST_QUEST, "NeedQuestTitle": None, "NeedStarNum": stars}


def extra_star_needs(rows):
    # The original FIRST_QUEST row still needs 0 stars, so only the any-row reading of 784C60 locks the quest.
    return [star_need_row(FIRST_MISSION, NEEDED_STARS), *rows, star_need_row(OTHER_MISSION, NEEDED_STARS + 1)]


def test_goddess_policy_opens_type_19_without_a_gate_or_four_star_challenge():
    assert quest_modes.policy(GODDESS_TYPE) is POLICY
    assert POLICY.status() == {"policy": "goddess", "enabled": True, "reason": None, "hard_mode": False,
                               "timed_conditions": False, "gate_quest": None}
    support = quest_modes.support_status()[GODDESS_TYPE]
    assert (support["mission_category"], support["mission_resources"], support["score_mode"], support["hard_rank"],
            support["entrust"]) == (GODDESS_CATEGORY, ["GoddessRevival"], False, None, False)


def test_every_goddess_quest_builds_from_its_quest_id_list_mission(bundle):
    catalog = bundle.quests.catalog
    quests = catalog.playable_quests(GODDESS_TYPE)
    assert len(quests) == GODDESS_QUESTS and set(quests) <= set(catalog.mode_ids)
    fixed = []
    for quest_id in quests:
        POLICY.validate_row(catalog.row(quest_id), catalog)
        quest, mode = catalog.get(quest_id), catalog.mode(quest_id)
        assert quest.rank == GODDESS_RANK and mode.hard is None and not mode.score_mode
        assert mode.map_archive == f"Map{mode.map_mission_id:04d}_{quest.map_no:04d}.aar"
        if quest.unit_list:
            fixed.append(quest_id)
            assert catalog.fixed_units(quest.unit_list)
    assert len(fixed) == FIXED_TEAM_QUESTS and FIXED_TEAM_QUEST in fixed


def test_total_star_tiers_mail_items_or_present_unit_templates(bundle):
    catalog = bundle.quests.catalog
    rewards = tier_rewards(catalog)
    assert list(rewards) == list(range(1, TIER_COUNT + 1))
    assert sum(reward["kind"] == "item" for reward in rewards.values()) == ITEM_TIERS
    units = {threshold: reward["template_id"] for threshold, reward in rewards.items() if reward["kind"] == "unit"}
    assert units == UNIT_TIERS and all(reward["description"] == DESCRIPTION for reward in rewards.values())
    templates = {row["PresentID"]: row["CardID"] for row in catalog.resource_rows("PresentUnitData.atb")}
    assert {template: templates[template] for template in UNIT_TIERS.values()} == TEMPLATE_CARDS
    assert catalog.resource_rows("NameText.atb")[templates[UNIT_TIERS[AEGIS_TIER]] - 1]["Message"] == AEGIS_NAME
    assert all(bundle.presents.preview([rewards[threshold]], 0) for threshold in UNIT_TIERS)


def test_goddess_quests_need_rank_fifty_but_no_menu_gate(setup, bundle):
    service, store = setup
    service.presents = bundle.presents
    set_rank(bundle, store, GODDESS_RANK - 1)
    before = store.profile()
    refused("quest_rank_required", 409, lambda: service.start(start_request(service, store, FIXED_TEAM_QUEST), store))
    assert store.profile() == before
    set_rank(bundle, store, GODDESS_RANK)
    service.start(start_request(service, store, FIXED_TEAM_QUEST, tick=1001), store)
    active = store.profile().state["quest_state"]["active"]
    assert active["mission_id"] == FIXED_TEAM_MISSION and active["mode"]["quest_type"] == GODDESS_TYPE


def test_goddess_starts_require_the_present_service(setup, bundle):
    service, store = setup
    set_rank(bundle, store, GODDESS_RANK)
    before = store.profile()
    refused("quest_present_unavailable", 409, lambda: service.start(start_request(service, store, LAST_QUEST), store))
    assert store.profile() == before


def test_goddess_quests_have_no_four_star_challenge(goddess):
    service, store = goddess
    put_record(store, FIRST_QUEST, max_rank=3, flags=CLEAR_FLAG | THREE_STAR_FLAG)
    before = store.profile()
    refused("quest_hard_unavailable", 409, lambda: service.start(hard_start(service, store, FIRST_QUEST), store))
    assert store.profile() == before


def test_a_first_clear_saves_the_total_and_mails_each_crossed_tier(goddess, bundle):
    service, store = goddess
    service.start(start_request(service, store, FIRST_QUEST), store)
    finishing = finish_request(store, FIRST_QUEST, selected=store.profile().state["quest_state"]["active"]["selected_mask"])
    response = service.finish(finishing, store)
    completed = store.profile()
    assert service.finish(finishing, store) == response and store.profile() == completed
    tree = nodes(response)
    assert child_names(response)[-1] == "PGP2" and total_update(tree) == {TOTAL_STAR_FIELD: "3"}
    assert saved_total(store) == 3 and "PFC" in tree and "PPT" in tree
    row = record(store, FIRST_QUEST)
    assert (row["MaxRank"], row["AchieveFlag"], row["Shown"]) == (3, CLEAR_FLAG | THREE_STAR_FLAG, False)
    assert mailed(bundle, store) == FIRST_TIERS and rewarded(store) == {GRANTED_FIELD: 3}
    assert completed.state["quest_state"]["history"][-1]["mode_presents_pending"] == 0
    tree = nodes(clear(service, store, FIRST_QUEST, tick=1002))
    assert "PGP2" not in tree and "PPT" not in tree and saved_total(store) == 3 and not mailed(bundle, store)


def test_identical_concurrent_results_mail_each_tier_once(goddess, bundle, monkeypatch):
    service, store = goddess
    service.start(start_request(service, store, FIRST_QUEST), store)
    finishing = finish_request(store, FIRST_QUEST, selected=store.profile().state["quest_state"]["active"]["selected_mask"])
    race_commits(store, monkeypatch)
    with ThreadPoolExecutor(max_workers=RACERS) as pool:
        futures = [pool.submit(service.finish, finishing, store) for _ in range(RACERS)]
        responses = {future.result(timeout=RACERS * RACE_SECONDS) for future in futures}
    assert len(responses) == 1 and mailed(bundle, store) == FIRST_TIERS and rewarded(store) == {GRANTED_FIELD: 3}
    assert len(bundle.presents.rows(store.profile().state)) == len(FIRST_TIERS)


def test_low_star_results_raise_the_total_by_the_best_result_only(goddess, bundle):
    service, store = goddess
    rewards = tier_rewards(service.catalog)
    for tick, stars in ((1000, 1), (1002, 2)):
        tree = nodes(clear(service, store, SECOND_QUEST, tick=tick, stars=stars))
        assert total_update(tree) == {TOTAL_STAR_FIELD: str(stars)} and record(store, SECOND_QUEST)["MaxRank"] == stars
        assert mailed(bundle, store) == [expected_mail(rewards[stars])] and "PFC" not in tree
    tree = nodes(clear(service, store, SECOND_QUEST, tick=1004, stars=1))
    assert "PGP2" not in tree and saved_total(store) == 2 and record(store, SECOND_QUEST)["MaxRank"] == 2


def test_only_tiers_above_the_rewarded_total_are_crossed(goddess, bundle):
    service, store = goddess
    seed_stars(store, dict(zip(FIRST_MISSION_QUESTS, (3, 3, 3, 1))))
    rewards = tier_rewards(service.catalog)
    tree = nodes(clear(service, store, NEXT_MISSION_QUEST, stars=2))
    assert total_update(tree) == {TOTAL_STAR_FIELD: "12"} and saved_total(store) == 12
    assert mailed(bundle, store) == [expected_mail(rewards[11]), expected_mail(rewards[12])]


def test_tiers_above_the_rewarded_total_are_mailed_even_when_the_saved_total_holds(goddess, bundle):
    service, store = goddess
    rewards = tier_rewards(service.catalog)
    clear(service, store, SECOND_QUEST, stars=1)
    put_record(store, FIRST_QUEST, flags=CLEAR_FLAG | THREE_STAR_FLAG)
    save_total(store, 4)
    tree = nodes(clear(service, store, SECOND_QUEST, tick=1002, stars=1))
    assert "PGP2" not in tree and saved_total(store) == 4 and rewarded(store) == {GRANTED_FIELD: 4}
    assert mailed(bundle, store) == [expected_mail(rewards[threshold]) for threshold in range(2, 5)]


def test_crossing_a_unit_tier_mails_its_present_unit_template(goddess, bundle):
    service, store = goddess
    seed_stars(store, dict(zip(FIRST_MISSION_QUESTS, (3, 3, 3, 3, 2))))
    tree = nodes(clear(service, store, NEXT_MISSION_QUEST, stars=1))
    assert total_update(tree) == {TOTAL_STAR_FIELD: "15"}
    assert mailed(bundle, store) == [(UNIT_PRESENT, UNIT_TIERS[15], None, DESCRIPTION)]


def test_a_save_without_the_record_starts_it_at_the_saved_total(goddess, bundle):
    service, store = goddess
    save_total(store, 10)
    tree = nodes(clear(service, store, FIRST_QUEST))
    assert total_update(tree) == {TOTAL_STAR_FIELD: "3"} and saved_total(store) == 3
    assert "PPT" not in tree and not mailed(bundle, store) and rewarded(store) == {GRANTED_FIELD: 10}
    for quest_id in (SECOND_QUEST, THIRD_QUEST):
        put_record(store, quest_id, flags=CLEAR_FLAG | THREE_STAR_FLAG)
    rewards = tier_rewards(service.catalog)
    tree = nodes(clear(service, store, FOURTH_QUEST, tick=1002))
    assert total_update(tree) == {TOTAL_STAR_FIELD: "12"} and rewarded(store) == {GRANTED_FIELD: 12}
    assert mailed(bundle, store) == [expected_mail(rewards[11]), expected_mail(rewards[12])]


def test_a_drop_followed_by_recovery_mails_no_tier_twice(goddess, bundle):
    service, store = goddess
    rewards = tier_rewards(service.catalog)
    clear(service, store, FIRST_QUEST)
    assert mailed(bundle, store) == [expected_mail(rewards[threshold]) for threshold in range(1, 4)]
    drop_record(store, FIRST_QUEST)
    for tick, stars in ((1002, 1), (1004, 3)):
        tree = nodes(clear(service, store, SECOND_QUEST, tick=tick, stars=stars))
        assert total_update(tree) == {TOTAL_STAR_FIELD: str(stars)} and saved_total(store) == stars
        assert not mailed(bundle, store) and rewarded(store) == {GRANTED_FIELD: 3}
    tree = nodes(clear(service, store, THIRD_QUEST, tick=1006, stars=1))
    assert total_update(tree) == {TOTAL_STAR_FIELD: "4"} and rewarded(store) == {GRANTED_FIELD: 4}
    assert mailed(bundle, store) == [expected_mail(rewards[4])]


def test_a_saved_total_below_the_records_mails_every_tier_up_to_their_sum(goddess, bundle):
    service, store = goddess
    for quest_id in (FIRST_QUEST, SECOND_QUEST):
        put_record(store, quest_id, max_rank=1, flags=CLEAR_FLAG)
    rewards = tier_rewards(service.catalog)
    tree = nodes(clear(service, store, FIRST_QUEST))
    assert total_update(tree) == {TOTAL_STAR_FIELD: "4"} and saved_total(store) == 4
    assert mailed(bundle, store) == [expected_mail(rewards[threshold]) for threshold in range(1, 5)]


def test_records_of_other_quest_types_stay_out_of_the_total(goddess):
    service, store = goddess
    put_record(store, MAIN_QUEST, max_rank=3, flags=CLEAR_FLAG | THREE_STAR_FLAG)
    tree = nodes(clear(service, store, FIRST_QUEST, stars=1))
    assert total_update(tree) == {TOTAL_STAR_FIELD: "1"} and saved_total(store) == 1


def test_the_shown_flag_keeps_its_saved_value(goddess):
    service, store = goddess
    put_record(store, FIRST_QUEST, max_rank=1, flags=CLEAR_FLAG)
    show_record(store, FIRST_QUEST)
    save_total(store, 1)
    tree = nodes(clear(service, store, FIRST_QUEST))
    assert {column.name: column.values for column in tree["QR"].children}["Shown"] == (True,)
    assert record(store, FIRST_QUEST)["Shown"] is True and total_update(tree) == {TOTAL_STAR_FIELD: "3"}


def test_a_full_mailbox_queues_the_crossed_tiers_once_and_delivers_them_later(goddess, bundle):
    service, store = goddess
    mailbox = Mailbox(bundle.presents)
    service.presents = mailbox
    tree = nodes(clear(service, store, FIRST_QUEST))
    journal = store.profile().state["quest_state"]
    queued = [tier.present() for tier in reward_tiers(service.catalog)[:3]]
    assert "PPT" not in tree and total_update(tree) == {TOTAL_STAR_FIELD: "3"} and saved_total(store) == 3
    assert journal["pending_presents"] == [{"reward": reward, "copies": 1} for reward in queued]
    assert journal["history"][-1]["mode_presents_pending"] == 3 and rewarded(store) == {GRANTED_FIELD: 3}
    mailbox.room = MAX_PRESENTS
    tree = nodes(clear(service, store, FIRST_QUEST, tick=1002))
    assert delivered(bundle, store) == [expected_mail(reward) for reward in queued] and not mailed(bundle, store)
    assert "PGP2" not in tree and "pending_presents" not in store.profile().state["quest_state"]


def test_the_banner_mission_quest_counts_toward_the_total(goddess, bundle):
    service, store = goddess
    missions = service.catalog.mission_windows(GODDESS_TYPE).missions(BANNER_QUEST)
    assert [mission.mission_id for mission in missions] == [BANNER_MISSION]
    seed_stars(store, {BANNER_QUEST: 3})
    rewards = tier_rewards(service.catalog)
    tree = nodes(clear(service, store, FIRST_QUEST))
    assert total_update(tree) == {TOTAL_STAR_FIELD: "6"} and saved_total(store) == 6
    assert mailed(bundle, store) == [expected_mail(rewards[threshold]) for threshold in range(4, 7)]


@pytest.mark.parametrize("threshold,ticket", SUMMON_TICKET_TIERS.items())
def test_default_ticket_pools_mail_every_summon_ticket_tier(goddess, bundle, threshold, ticket):
    service, store = goddess
    others = [quest_id for quest_id in service.catalog.playable_quests(GODDESS_TYPE) if quest_id != FIRST_QUEST]
    seed_stars(store, spread(threshold - 1, others))
    tree = nodes(clear(service, store, FIRST_QUEST, stars=1))
    assert total_update(tree) == {TOTAL_STAR_FIELD: str(threshold)}
    assert mailed(bundle, store) == [(ITEM_PRESENT, TICKETS_PER_PRESENT, ticket, DESCRIPTION)]


def test_status_lists_the_ticket_tiers_item_ticket_rules_without_their_pool_leave_unmailable(goddess, bundle,
                                                                                             closed_ticket_pools,
                                                                                             monkeypatch):
    service, store = goddess
    assert service.status()["mode_reward_delivery"][GODDESS_TYPE] == {"ready": True, "undeliverable": []}
    monkeypatch.setattr(bundle.presents, "item_tickets", closed_ticket_pools)
    report = service.status()["mode_reward_delivery"][GODDESS_TYPE]
    assert report["ready"] is False and all(set(entry) == UNDELIVERABLE_FIELDS for entry in report["undeliverable"])
    assert {entry["threshold"]: (entry["item_id"], entry["reason"]) for entry in report["undeliverable"]} == {
        threshold: (ticket, "summon_ticket_pool_disabled") for threshold, ticket in SUMMON_TICKET_TIERS.items()}
    others = [quest_id for quest_id in service.catalog.playable_quests(GODDESS_TYPE) if quest_id != FIRST_QUEST]
    seed_stars(store, spread(min(SUMMON_TICKET_TIERS) - 1, others))
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.start(start_request(service, store, FIRST_QUEST), store)
    assert (error.value.code, error.value.status, error.value.__cause__.code) == (
        "quest_goddess_reward_unsupported", 501, "summon_ticket_pool_disabled")
    assert store.profile() == before


def test_reward_texts_past_the_native_cell_refuse_the_start_before_charging(goddess, monkeypatch):
    service, store = goddess
    monkeypatch.setattr(quest_mode_goddess, "DESCRIPTION", "x" * (MAX_PRESENT_TEXT_BYTES + 1))
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.start(start_request(service, store, FIRST_QUEST), store)
    # An empty-mailbox preview takes the label, but the settlement could not queue a present carrying it.
    assert (error.value.code, error.value.status, error.value.__cause__.code) == (
        "quest_goddess_reward_unsupported", 501, "quest_present_unsupported")
    assert store.profile() == before
    undeliverable = service.status()["mode_reward_delivery"][GODDESS_TYPE]["undeliverable"]
    assert len(undeliverable) == TIER_COUNT
    assert {entry["reason"] for entry in undeliverable} == {"quest_present_unsupported"}
    monkeypatch.setattr(quest_mode_goddess, "DESCRIPTION", "x" * MAX_PRESENT_TEXT_BYTES)
    service.start(start_request(service, store, FIRST_QUEST, tick=1001), store)
    assert store.profile().state["quest_state"]["active"]["quest_id"] == FIRST_QUEST


def test_crossable_tiers_that_cannot_be_mailed_refuse_the_start_before_charging(goddess, bundle):
    service, store = goddess
    service.presents = RefusingPresents(bundle.presents, THIRD_TIER_ITEM)
    before = store.profile()
    refused("quest_goddess_reward_unsupported", 501,
            lambda: service.start(start_request(service, store, FIRST_QUEST), store))
    assert store.profile() == before
    seed_stars(store, {FIRST_QUEST: 3})
    service.start(start_request(service, store, FIRST_QUEST, tick=1001), store)
    assert store.profile().state["quest_state"]["active"]["quest_id"] == FIRST_QUEST


def test_the_preflight_skips_tiers_at_or_below_the_rewarded_total(goddess, bundle):
    service, store = goddess
    clear(service, store, SECOND_QUEST, stars=1)
    service.presents = RefusingPresents(bundle.presents, THIRD_TIER_ITEM)
    save_progress(store, {POLICY.name: {GRANTED_FIELD: THIRD_TIER - 1}})
    before = store.profile()
    refused("quest_goddess_reward_unsupported", 501,
            lambda: service.start(start_request(service, store, FIRST_QUEST, tick=1002), store))
    assert store.profile() == before
    save_progress(store, {POLICY.name: {GRANTED_FIELD: THIRD_TIER}})
    service.start(start_request(service, store, FIRST_QUEST, tick=1003), store)
    assert saved_total(store) == 1 and store.profile().state["quest_state"]["active"]["quest_id"] == FIRST_QUEST


def test_the_preflight_stops_at_the_best_total_the_started_quest_can_reach(goddess, bundle):
    service, store = goddess
    service.presents = RefusingPresents(bundle.presents, NINTH_TIER_ITEM)
    seed_stars(store, dict(zip(FIRST_MISSION_QUESTS, (3, 3, 2))))
    service.start(start_request(service, store, FIRST_QUEST), store)
    before = store.profile()
    refused("quest_goddess_reward_unsupported", 501,
            lambda: service.start(start_request(service, store, THIRD_QUEST, tick=1001), store))
    assert store.profile() == before and before.state["quest_state"]["active"]["quest_id"] == FIRST_QUEST


def test_reward_types_without_a_present_form_refuse_the_start(goddess, monkeypatch):
    service, store = goddess
    patch_rows(monkeypatch, service.catalog, REWARD_TABLE, unmapped_second_tier)
    before = store.profile()
    refused("quest_goddess_reward_unsupported", 501,
            lambda: service.start(start_request(service, store, FIRST_QUEST), store))
    assert store.profile() == before


def test_a_reward_table_changed_after_the_start_refuses_the_settlement(goddess, monkeypatch):
    service, store = goddess
    service.start(start_request(service, store, FIRST_QUEST), store)
    patch_rows(monkeypatch, service.catalog, REWARD_TABLE, unmapped_second_tier)
    before = store.profile()
    refused("quest_goddess_reward_unsupported", 501, lambda: service.finish(finish_request(store, FIRST_QUEST), store))
    assert store.profile() == before


def test_need_star_num_locks_the_quest_until_the_saved_total_reaches_it(goddess, monkeypatch):
    service, store = goddess
    patch_rows(monkeypatch, service.catalog, CONFIG_TABLE, extra_star_needs)
    save_total(store, NEEDED_STARS - 1)
    before = store.profile()
    refused("quest_locked", 409, lambda: service.start(start_request(service, store, FIRST_QUEST), store))
    assert store.profile() == before
    service.start(start_request(service, store, SECOND_QUEST, tick=1001), store)
    save_total(store, NEEDED_STARS)
    service.start(start_request(service, store, FIRST_QUEST, tick=1002), store)
    active = store.profile().state["quest_state"]["active"]
    assert (active["quest_id"], active["mission_id"]) == (FIRST_QUEST, FIRST_MISSION)


def test_a_saved_total_outside_native_counts_refuses_the_start(goddess):
    service, store = goddess
    save_total(store, -1)
    before = store.profile()
    refused("quest_state_invalid", 503, lambda: service.start(start_request(service, store, FIRST_QUEST), store))
    assert store.profile() == before


def test_the_rewarded_total_never_reaches_a_response(goddess):
    service, store = goddess
    save_total(store, 10)
    started = service.start(start_request(service, store, FIRST_QUEST), store)
    selected = store.profile().state["quest_state"]["active"]["selected_mask"]
    finished = service.finish(finish_request(store, FIRST_QUEST, selected=selected), store)
    loaded = service.bootstrap.game_param2(request(store, GAME_PARAM2_REGISTRATION, ()), store)
    assert rewarded(store) == {GRANTED_FIELD: 10} and total_update(nodes(finished)) == {TOTAL_STAR_FIELD: "3"}
    assert {column.name: column.values for column in loads(loaded).children}[TOTAL_STAR_KEY] == (3,)
    for response in (started, finished, loaded):
        assert MODE_PROGRESS.encode() not in response and GRANTED_FIELD.encode() not in response


def test_the_rewarded_total_survives_save_loading(goddess, bundle):
    service, store = goddess
    clear(service, store, FIRST_QUEST)
    saved = store.profile().state
    assert bundle.initialize(deepcopy(saved))["quest_state"] == saved["quest_state"]


def test_other_battles_keep_the_rewarded_total(goddess):
    service, store = goddess
    clear(service, store, FIRST_QUEST)
    clear(service, store, MAIN_QUEST, tick=1002)
    assert rewarded(store) == {GRANTED_FIELD: 3}
    service.start(start_request(service, store, SECOND_QUEST, tick=1004), store)
    service.exit(exit_request(store, SECOND_QUEST), store)
    assert rewarded(store) == {GRANTED_FIELD: 3}


@pytest.mark.parametrize("progress", [
    [], {"unregistered": {}}, {POLICY.name: [3]}, {POLICY.name: {}}, {POLICY.name: {GRANTED_FIELD: -1}},
    {POLICY.name: {GRANTED_FIELD: "3"}}, {POLICY.name: {GRANTED_FIELD: True}},
    {POLICY.name: {GRANTED_FIELD: 3, "spare": 0}},
])
def test_a_malformed_rewarded_total_refuses_the_start_before_charging(goddess, progress):
    service, store = goddess
    clear(service, store, FIRST_QUEST)
    save_progress(store, progress)
    before = store.profile()
    refused("quest_state_invalid", 503,
            lambda: service.start(start_request(service, store, SECOND_QUEST, tick=1002), store))
    assert store.profile() == before


def test_a_settle_hook_keeps_only_its_own_progress(goddess, monkeypatch):
    service, store = goddess
    other = quest_modes.policy(STORY_TYPE).name
    settle = POLICY.settle

    def reach_other(context):
        outcome = settle(context)
        outcome.state["quest_state"][MODE_PROGRESS][other] = {GRANTED_FIELD: 1}
        return outcome

    monkeypatch.setattr(POLICY, "settle", reach_other)
    clear(service, store, FIRST_QUEST)
    assert store.profile().state["quest_state"][MODE_PROGRESS] == {POLICY.name: {GRANTED_FIELD: 3}}


def test_settle_hook_progress_outside_the_journal_shape_refuses_the_settlement(goddess, monkeypatch):
    service, store = goddess
    settle = POLICY.settle

    def wrap_progress(context):
        outcome = settle(context)
        journal = outcome.state["quest_state"]
        journal[MODE_PROGRESS] = [journal[MODE_PROGRESS]]
        return outcome

    monkeypatch.setattr(POLICY, "settle", wrap_progress)
    service.start(start_request(service, store, FIRST_QUEST), store)
    selected = store.profile().state["quest_state"]["active"]["selected_mask"]
    before = store.profile()
    refused("quest_mode_hook_invalid", 503,
            lambda: service.finish(finish_request(store, FIRST_QUEST, selected=selected), store))
    assert store.profile() == before


@pytest.mark.parametrize("changes,table,change,code", [
    ({"Type": 0}, None, None, "quest_definition_invalid"),
    ({"UnitList": MISSING_TEAM}, None, None, "quest_definition_invalid"),
    ({"UnitList": -1}, None, None, "quest_definition_invalid"),
    ({}, CONFIG_TABLE, drop_first_quest, "quest_mode_resource_invalid"),
    ({}, CONFIG_TABLE, move_first_quest, "quest_mode_resource_invalid"),
    ({}, CONFIG_TABLE, text_star_need, "quest_mode_resource_invalid"),
    ({}, CONFIG_TABLE, drop_need_column, "quest_mode_resource_invalid"),
    ({}, REWARD_TABLE, repeat_threshold, "quest_mode_resource_invalid"),
    ({}, REWARD_TABLE, drop_item_column, "quest_mode_resource_invalid"),
    ({}, REWARD_TABLE, zero_threshold, "quest_mode_resource_invalid"),
    ({}, REWARD_TABLE, text_reward_param, "quest_mode_resource_invalid"),
])
def test_validate_row_refuses_rows_outside_the_goddess_contract(bundle, monkeypatch, changes, table, change, code):
    catalog = bundle.quests.catalog
    if table is not None:
        patch_rows(monkeypatch, catalog, table, change)
    row = MappingProxyType({**catalog.row(FIRST_QUEST), **changes})
    refused(code, 503, lambda: POLICY.validate_row(row, catalog))
