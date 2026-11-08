from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aigis_ps import quest_modes
from aigis_ps.alxml import Column, Container, loads
from aigis_ps.battle_events import ENTRUST, RESULT
from aigis_ps.crystal_wallet import FREE_CRYSTAL_LIMIT, wallet_totals
from aigis_ps.drop_bonus import RESOURCE as DROP_BONUS_RESOURCE, WEEKDAYS, apply as apply_drop_bonus
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.mission_service import MissionService
from aigis_ps.mission_state import MissionProgress
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.player_state import PRINCE_CARD_IDS
from aigis_ps.present_backlog import STORED_PRESENT_TEXT_BYTES
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.quest_catalog import HARMONY_TYPE, DropCandidate, QuestCatalog, QuestDefinition
from aigis_ps.quest_modes import CLEAR_FLAG, SLOT_FLAGS, SettleOutcome, THREE_STAR_FLAG
from aigis_ps.quest_protocol import AVATAR_INDEX_OFFSETS, FIXED_INDEX_OFFSET, parse_finish
from aigis_ps.quest_service import QuestRules, QuestService
from aigis_ps.storage import Store
from aigis_ps.vote_rules import VoteRules
from aigis_ps.wallet_money import GOLD_LIMIT
from test_drop_bonus import CRYSTAL, CRYSTAL_TYPE, row as bonus_row, table_bytes as bonus_table
from test_history_disclosure import disclose
from test_quest_battle_modes import (
    MISSION_NOW, Recorder, edit_active, fill_wallet, game_params, gold_events, hard_start, mutate, new_cards, put_record,
    record, set_rank, three_star_gold, unlocked, update_game_param,
)
from test_quest_service import (
    PRIZE_ENEMY_ATTR, allow_quest, assert_error, bundle, finish_request, nodes, setup, start_request,
)
from test_vote_catalog import build as vote_catalog, vote_sources
from test_vote_quest_tickets import (
    BEFORE_REVIVAL, FIRST as TICKET, REVIVAL, SECOND_WINDOW, TODAY, isolated, quest_tickets,
)


ITEM = "gift_0002_ruby"
TEAM_COLUMNS = ("A1", "A7", "A8", "AB", "AA")
JST_HALF_PAST = datetime(2026, 11, 1, 12, 30, tzinfo=JST)
HOST_ZONES = (timezone(timedelta(hours=-10)), timezone(timedelta(hours=14)))
STAR_CRYSTAL = {"kind": "free_crystal", "amount": 1, "description": "★３報酬"}
ITEM_PRESENT = 22
CRYSTAL_PRESENT = 0
MENU_GATE_QUEST = 1004
CHARISMA_STOCK = 1000
HISTORY_TYPE, HISTORY_RANK = 16, 9
# Both History quests drop one 5002 (three carved crystals) from slot 5, and only the first gets a synthetic bonus row.
BONUS_QUEST, PLAIN_QUEST, CRYSTAL_MISSION = 11, 12, 920002
HARD_QUEST = 1002
CRYSTAL_DROP, CRYSTAL_SLOT, CRYSTAL_DROPS = 5002, 5, 3
BONUS_VALUE, BONUS_CRYSTALS = 1.5, 4
NO_BONUS_DAY = {"value": 0.0, "increments": {}}
BONUS_DAY = {"value": BONUS_VALUE, "increments": {CRYSTAL: BONUS_CRYSTALS - CRYSTAL_DROPS}}
# Nine crystals of one request floor once to 13, not three times to 4.
BATCH_CRYSTALS, BATCH_BONUS_CRYSTALS = 3 * CRYSTAL_DROPS, 13
BATCH_BONUS_DAY = {"value": BONUS_VALUE, "increments": {CRYSTAL: BATCH_BONUS_CRYSTALS - BATCH_CRYSTALS}}
# float32 holds 2**31 exactly, so this bonus row scales even one crystal past INT32_MAX.
SATURATING_BONUS_VALUE = float(INT32_MAX + 1)
SATURATING_WEEK = (SATURATING_BONUS_VALUE,) * len(WEEKDAYS)
# 15 charisma per ticket floors this quest's 25 charisma to one ticket.
TICKET_CHARISMA, TICKETS_PER_RUN = 25, 1
SECOND, MINUTE = timedelta(seconds=1), timedelta(minutes=1)
SUNDAY_NOON, SUNDAY_LAST_MINUTE = datetime(2026, 11, 1, 12, tzinfo=JST), datetime(2026, 11, 1, 23, 59, tzinfo=JST)
MONDAY_NOON, MONDAY_LAST_MINUTE = datetime(2026, 11, 2, 12, tzinfo=JST), datetime(2026, 11, 2, 23, 59, tzinfo=JST)
REPRODUCE_TYPE, GOLD_RUSH_QUEST, GOLD_RUSH_MISSION, GOLD_RUSH_RANK, GOLD_RUSH_OVERRIDE = 8, 4807, 300130, 9, 437
GOLD_RUSH_SEEN_FLAGS = CLEAR_FLAG | THREE_STAR_FLAG | SLOT_FLAGS[1] | SLOT_FLAGS[2]
# 4807 inserts its 7005 prize enemy at RT index 3 only once the record holds three stars.
GOLD_RUSH_BELOW_THREE = ((0, 2, 0), (1, 3, 0), (2, 1, 0), (3, 2, 0))
GOLD_RUSH_UNSEEN_THREE = ((0, 2, 0), (1, 3, 0), (2, 1, 0), (3, 1, PRIZE_ENEMY_ATTR), (4, 2, 0))
GOLD_RUSH_SEEN_BELOW_THREE = ((0, 2, 1), (1, 3, 1), (2, 1, 0), (3, 2, 1))
GOLD_RUSH_SEEN_THREE = ((0, 2, 1), (1, 3, 1), (2, 1, 0), (3, 1, PRIZE_ENEMY_ATTR), (4, 2, 1))
GOLD_RUSH_RESERVED = [77, 56, 133, GOLD_RUSH_OVERRIDE, 77, 249]
COLLECTION_BIT = SLOT_FLAGS[4]
LEGACY_CANDIDATE_FIELDS = ("min_stars", "max_stars", "override_code")
# Quest 1001 credits 17 experience and 675 gold at three stars; campaigns raise them to (int)(17 * 1.2f) or by 1.5.
BASE_REWARD = (17, 675)
CAMPAIGN_REWARDS = {"ExperienceUpCloseDateTime": (20, 675), "MoneyUpCloseDateTime": (17, 1012)}
TACTICAL_GUIDE = 3001
# The client calendar ends with 3001-01-19 JST; this is the first POSIX second of the day past it.
CALENDAR_OVERFLOW_SECONDS = 32536825200
STAND_IN_OFFSET = AVATAR_INDEX_OFFSETS.start + 1
# QuestRules accepts at most this gold per event and this many counted events.
GOLD_EVENT_RULE_LIMIT = 100000


def custom_quest(service, monkeypatch, quest_id, *, treasures, slots):
    original = service.catalog.get
    quest = replace(original(quest_id), treasures=treasures,
                    candidates=tuple(DropCandidate(slot, index, 0, 2, 0) for index, slot in enumerate(slots)))
    monkeypatch.setattr(service.catalog, "get", lambda value: quest if value == quest_id else original(value))
    return quest


def explicit(quest, attributes):
    return [(index, candidate.slot, attribute) for index, (candidate, attribute) in enumerate(zip(quest.candidates, attributes))]


def drops(tree):
    columns = tree["TreasureInfo"].children
    return dict(zip(columns[1].values, columns[2].values))


def possession(store, item):
    return {row["ItemID"]: row["Count"] for row in store.profile().state["tables"]["possessions"]}.get(item, 0)


def item_counts(tree, name="POSSESSION"):
    columns = tree[name].children
    return dict(zip(columns[0].values, columns[1].values))


def bonus_values(tree):
    return tuple(column.values[0] for column in tree["BONUS"].children)


def play(service, store, quest_id=1001, tick=1000):
    service.start(start_request(service, store, quest_id, tick=tick), store)
    selected = store.profile().state["quest_state"]["active"]["selected_mask"]
    return nodes(service.finish(finish_request(store, quest_id, tick=tick + 1, selected=selected), store))


def last_settlement(store):
    return store.profile().state["quest_state"]["history"][-1]


def waiting(store):
    return store.profile().state["quest_state"].get("pending_presents")


def queued(reward, copies=1):
    return {"reward": reward, "copies": copies}


def mail(bundle, store, key):
    rows = {row["PresentID"]: row for row in bundle.presents.rows(store.profile().state)}
    return [(rows[present_id]["Type"], rows[present_id]["Content"], rows[present_id].get("ItemID"),
             rows[present_id]["Description"]) for present_id in last_settlement(store)[key]]


def as_mail(reward):
    if reward["kind"] == "free_crystal":
        return CRYSTAL_PRESENT, reward["amount"], None, reward["description"]
    return ITEM_PRESENT, reward["amount"], reward["item_id"], reward["description"]


def test_prize_slots_beyond_five_grant_nothing_but_count_as_obtained_once_the_record_exists(setup, monkeypatch):
    service, store = setup
    allow_quest(store, 1001)
    quest = custom_quest(service, monkeypatch, 1002, treasures=(67, 0, 68, 4, 15), slots=(1, 2, 4, 5, 8, 41))
    service.start(start_request(service, store, 1002, candidates=explicit(quest, (0,) * 6)), store)
    tree = nodes(service.finish(finish_request(store, 1002, obtained=0b111111, selected=0b111111), store))
    assert drops(tree) == {67: 1, 4: 1, 15: 1} and new_cards(tree) == (67, 4, 15)
    assert record(store, 1002)["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG | 0x10000 | 0x1000 | 0x10 | 0x1
    before = store.profile()
    assert_error("quest_candidates_mismatch", lambda: service.start(
        start_request(service, store, 1002, tick=1002, candidates=explicit(quest, (1, 1, 1, 1, 0, 0))), store))
    assert store.profile() == before
    service.start(start_request(service, store, 1002, tick=1002, candidates=explicit(quest, (1,) * 6)), store)


def test_a_slot_five_drop_sets_the_collection_bit_before_the_collection_reward_is_judged(setup, monkeypatch):
    service, store = setup
    allow_quest(store, 1001)
    quest = custom_quest(service, monkeypatch, 1002, treasures=(67, 68, 4, 0, 15), slots=(1, 2, 3, 5))
    service.start(start_request(service, store, 1002, candidates=explicit(quest, (0,) * 4)), store)
    tree = nodes(service.finish(finish_request(store, 1002, obtained=0b1111, selected=0b1111), store))
    assert drops(tree) == {67: 1, 68: 1, 4: 1, 15: 1}
    assert record(store, 1002)["AchieveFlag"] & 0x11111 == 0x11101


def test_gold_events_stop_at_the_frozen_local_cap(setup):
    service, store = setup
    service.rules = replace(service.rules, max_gold_events=2)
    service.start(start_request(service, store), store)
    service.rules = replace(service.rules, max_gold_events=1000)
    tree = nodes(service.finish(finish_request(store, gold=5, extra=gold_events(5)), store))
    settled = three_star_gold(service.catalog.get(1001).gold + 2 * service.rules.gold_per_event)
    assert tree["REWARD"].children[1].values == (settled,)
    assert store.profile().state["quest_state"]["history"][-1]["gold_events"] == 2


def rewarded(tree):
    return tuple(column.values[0] for column in tree["REWARD"].children)


def open_campaign(store, field, closes):
    mutate(store, lambda state: state["tables"]["config"][0].update({field: closes}) or state)


@pytest.mark.parametrize("field", CAMPAIGN_REWARDS)
@pytest.mark.parametrize("closes,boosted", [("2026/11/01 12:00:01", True), ("2026/11/01 12:00:00", False), ("", False)])
def test_an_unexpired_campaign_credits_the_native_float_product(setup, field, closes, boosted):
    service, store = setup
    open_campaign(store, field, closes)
    service.start(start_request(service, store), store)
    tree = nodes(service.finish(finish_request(store), store))
    assert rewarded(tree) == (CAMPAIGN_REWARDS[field] if boosted else BASE_REWARD)


@pytest.mark.parametrize("host_zone", HOST_ZONES, ids=str)
@pytest.mark.parametrize("field", CAMPAIGN_REWARDS)
@pytest.mark.parametrize("closes,boosted", [("2026/11/01 12:29:59", False), ("2026/11/01 12:30:01", True)])
def test_campaigns_close_on_jst_whatever_the_host_calendar(setup, host_zone, field, closes, boosted):
    service, store = setup
    service.clock = lambda: JST_HALF_PAST.astimezone(host_zone)
    open_campaign(store, field, closes)
    service.start(start_request(service, store), store)
    tree = nodes(service.finish(finish_request(store), store))
    assert rewarded(tree) == (CAMPAIGN_REWARDS[field] if boosted else BASE_REWARD)


def test_a_boosted_run_repeats_the_single_run_gold_floored_before_the_boost(setup, monkeypatch):
    service, store = setup
    update_game_param(store, NowCharisma=CHARISMA_STOCK, NowActionPoint=CHARISMA_STOCK)
    open_campaign(store, "MoneyUpCloseDateTime", "2026/11/01 12:00:01")
    monkeypatch.setattr(quest_modes.policy(0), "start_boost", lambda context: context.request.boost)
    # One run floors (450 * 3% + 200 event gold) * 1.5 * 1.5 = 480.375 once, so three runs credit 1440, not 1441.
    service.rules = replace(service.rules, gold_percent=3)
    service.start(start_request(service, store, boost=3), store)
    tree = nodes(service.finish(finish_request(store, gold=2, extra=gold_events(2)), store))
    assert rewarded(tree) == (3 * BASE_REWARD[0], 3 * 480)
    assert last_settlement(store)["gold_events"] == 2


def test_gold_event_sums_past_int32_settle_at_the_gold_limit(setup):
    service, store = setup
    service.rules = QuestRules(gold_per_event=GOLD_EVENT_RULE_LIMIT, max_gold_events=GOLD_EVENT_RULE_LIMIT)
    service.start(start_request(service, store), store)
    before = store.profile().state["tables"]["game_param"][0]["Money"]
    events = 0x7FFFFFFF // GOLD_EVENT_RULE_LIMIT + 1
    tree = nodes(service.finish(finish_request(store, gold=events, extra=gold_events(events)), store))
    assert store.profile().state["tables"]["game_param"][0]["Money"] == GOLD_LIMIT
    assert rewarded(tree)[1] == GOLD_LIMIT - before and last_settlement(store)["gold_events"] == events


def test_boosted_rewards_past_int32_settle_at_the_last_rank_and_the_gold_limit(setup, bundle, monkeypatch):
    service, store = setup
    update_game_param(store, NowCharisma=CHARISMA_STOCK, NowActionPoint=CHARISMA_STOCK)
    original = service.catalog.get
    quest = replace(original(1001), rank_exp=0x7FFFFFFF, gold=0x7FFFFFFF)
    monkeypatch.setattr(service.catalog, "get", lambda value: quest if value == 1001 else original(value))
    monkeypatch.setattr(quest_modes.policy(0), "start_boost", lambda context: context.request.boost)
    service.start(start_request(service, store, boost=3), store)
    service.finish(finish_request(store), store)
    row = store.profile().state["tables"]["game_param"][0]
    last = sum(rank["NextExp"] for rank in bundle.bootstrap.rank_rows) - 1
    assert (row["Money"], row["Experience"]) == (GOLD_LIMIT, min(last, 0x7FFFFFFF))


def test_event_param_is_clamped_on_every_settlement_even_without_event_drops(setup):
    service, store = setup
    update_game_param(store, EventParam=1900)
    service.start(start_request(service, store), store)
    tree = nodes(service.finish(finish_request(store), store))
    assert game_params(tree)["AF"] == "1800"
    assert store.profile().state["tables"]["game_param"][0]["EventParam"] == 1800


class Mailbox:
    def __init__(self, presents):
        self.presents, self.room, self.refused = presents, 0, set()

    def preview(self, rewards, existing_count):
        return self.presents.preview(rewards, existing_count)

    def enqueue(self, state, rewards):
        if any(reward.get("item_id") in self.refused for reward in rewards):
            raise ProtocolError("present_item_unsupported", "The item is no longer mailable", 501)
        if len(rewards) > self.room:
            raise ProtocolError("present_capacity", "The present box is full", 409)
        state, added = self.presents.enqueue(state, rewards)
        self.room -= len(rewards)
        return state, added

    def response(self, state, present_ids):
        return self.presents.response(state, present_ids)


def test_identical_overflow_crystals_wait_as_copies_and_arrive_as_separate_presents(setup, bundle):
    service, store = setup
    mailbox = Mailbox(bundle.presents)
    service.presents = mailbox
    fill_wallet(store)
    for tick, quest_id in zip((1000, 1002, 1004), (1001, 1002, 1003)):
        tree = play(service, store, quest_id, tick)
        assert "PFC" not in tree and "PPT" not in tree and last_settlement(store)["crystals_pending"] == 1
    assert waiting(store) == [queued(STAR_CRYSTAL, 3)]
    mailbox.room = 2
    assert "PPT" in play(service, store, tick=1006)
    assert mail(bundle, store, "delivered_presents") == [as_mail(STAR_CRYSTAL)] * 2
    assert waiting(store) == [queued(STAR_CRYSTAL)]
    mailbox.room = MAX_PRESENTS
    play(service, store, tick=1008)
    assert mail(bundle, store, "delivered_presents") == [as_mail(STAR_CRYSTAL)] and waiting(store) is None
    assert wallet_totals(store.profile().state)["free"] == FREE_CRYSTAL_LIMIT


def test_mode_hooks_commit_with_the_attempt_and_merge_into_single_children(setup, bundle, monkeypatch):
    service, store = setup
    story = quest_modes.policy(0)

    def prepare_start(context, state):
        state = bundle.possessions.credit(state, {ITEM: 1})
        return state, (bundle.possessions.response(state, item_ids=(ITEM,)),)

    def settle(context):
        assert context.first_clear and context.stars == 3 and context.mission_id is not None
        return SettleOutcome(bundle.possessions.credit(context.state, {ITEM: 2}), (Container("ModeResult"),), (ITEM,))

    monkeypatch.setattr(story, "prepare_start", prepare_start)
    monkeypatch.setattr(story, "settle", settle)
    assert "POSSESSION" in nodes(service.start(start_request(service, store), store)) and possession(store, ITEM) == 1
    names = [child.name for child in loads(service.finish(finish_request(store), store)).children]
    assert names[-1] == "ModeResult" and names.count("POSSESSION") == 1 and possession(store, ITEM) == 3


@pytest.mark.parametrize("hook,result", [
    ("prepare_start", lambda context, state: [state, ()]),
    ("prepare_start", lambda context, state: (state, [Container("ModeResult")])),
    ("settle", lambda context: {"state": context.state}),
    ("settle", lambda context: SettleOutcome(context.state, (Container("ModeResult"),), (1,))),
])
def test_invalid_mode_hook_results_leave_the_profile_unchanged(setup, monkeypatch, hook, result):
    service, store = setup
    monkeypatch.setattr(quest_modes.policy(0), hook, result)
    if hook == "settle":
        service.start(start_request(service, store), store)
    before = store.profile()
    action = ((lambda: service.start(start_request(service, store), store)) if hook == "prepare_start"
              else (lambda: service.finish(finish_request(store), store)))
    assert_error("quest_mode_hook_invalid", action)
    assert store.profile() == before


def team(name, rows):
    return Container(name, tuple(Column(column, "I", tuple(row[index] for row in rows))
                                 for index, column in enumerate(TEAM_COLUMNS)))


def formation(*rows):
    return (team("BL", rows),)


def stand_in(player, unit, offset=STAND_IN_OFFSET):
    return unit["CardID"], player["battle"]["UX"] + offset, 0, unit["UniqueID"], 0


def test_mission_events_carry_saved_formation_members_without_prince_or_avatars(setup, bundle, caplog):
    service, store = setup
    mutate(store, lambda state: bundle.player_state.credit_units(state, [{"card_id": 67, "level": 1, "count": 2}])[0])
    player = store.profile().state["player_state"]
    prince, first, second = player["units"]
    assert prince["CardID"] in (1, 309)
    recorder = Recorder()
    service.missions = recorder
    service.start(start_request(service, store), store)
    mission_id = store.profile().state["quest_state"]["active"]["mission_id"]
    rows = ((prince["CardID"], prince["GetIndex"], 0, prince["UniqueID"], 0),
            (67, first["GetIndex"], 1, first["UniqueID"], 0), (68, second["GetIndex"], 2, second["UniqueID"], 0),
            stand_in(player, prince), (67, first["GetIndex"], 4, first["UniqueID"], 0),
            (67, 999999, 5, 999999, 0))
    with caplog.at_level("WARNING", logger="aigis_ps"):
        service.finish(finish_request(store, extra=formation(*rows)), store)
    assert [record.getMessage() for record in caplog.records] == [
        "Quest 1001 result lists 2 formation rows absent from the saved roster"]
    assert recorder.events == [("quest_clear", {
        "quest_id": 1001, "quest_type": 0, "stars": 3, "hard": False, "mission_id": mission_id, "score_mode": False,
        "kills": 20, "team": [{"card_id": 67, "get_index": first["GetIndex"], "unique_id": first["UniqueID"]}]})]
    assert type(mission_id) is int and mission_id > 0


def settled_with_warnings(service, store, quest_id, caplog, *teams):
    with caplog.at_level("WARNING", logger="aigis_ps"):
        service.finish(finish_request(store, quest_id, extra=teams), store)
    assert record(store, quest_id)["AchieveFlag"] & CLEAR_FLAG
    return [entry.getMessage() for entry in caplog.records]


def test_stand_ins_that_do_not_copy_the_saved_prince_are_logged_without_refusing(setup, bundle, caplog):
    service, store = setup
    mutate(store, lambda state: bundle.player_state.credit_units(state, [{"card_id": 67, "level": 1, "count": 1}])[0])
    player = store.profile().state["player_state"]
    prince, first = player["units"]
    service.start(start_request(service, store), store)
    rows = (stand_in(player, prince), stand_in(player, first, STAND_IN_OFFSET + 1),
            stand_in(player, {**prince, "UniqueID": first["UniqueID"]}, AVATAR_INDEX_OFFSETS.stop - 1),
            stand_in(player, first, AVATAR_INDEX_OFFSETS.stop))
    assert settled_with_warnings(service, store, 1001, caplog, team("BL", rows)) == [
        "Quest 1001 result lists 2 stand-in rows that do not copy the saved prince"]


def test_hl_rows_of_a_quest_without_a_fixed_team_are_logged_without_refusing(setup, caplog):
    service, store = setup
    assert not service.catalog.get(1001).unit_list
    service.start(start_request(service, store), store)
    ux = store.profile().state["player_state"]["battle"]["UX"]
    assert settled_with_warnings(service, store, 1001, caplog, team("HL", [(67, ux + FIXED_INDEX_OFFSET, 1, 0, 0)])) == [
        "Quest 1001 result HL differs from its fixed team: 1 rows reported, 0 members expected"]


@pytest.fixture
def guide(setup):
    service, store = setup
    allow_quest(store, MENU_GATE_QUEST)
    service.start(start_request(service, store, TACTICAL_GUIDE), store)
    player = store.profile().state["player_state"]
    members = service.catalog.fixed_units(service.catalog.get(TACTICAL_GUIDE).unit_list)
    rows = [(unit.card_id, player["battle"]["UX"] + FIXED_INDEX_OFFSET + index, index + 1, 0, unit.cost_mod)
            for index, unit in enumerate(members)]
    assert len(rows) > 1
    return SimpleNamespace(service=service, store=store, player=player, rows=rows)


@pytest.mark.parametrize("shift", [0, 1], ids=["table_order", "rotated"])
def test_a_fixed_team_reported_in_any_row_order_settles_without_warnings(guide, caplog, shift):
    rows = guide.rows[shift:] + guide.rows[:shift]
    assert not settled_with_warnings(guide.service, guide.store, TACTICAL_GUIDE, caplog, team("HL", rows))


@pytest.mark.parametrize("change", [
    lambda rows: rows[:-1],
    lambda rows: [*rows, (rows[0][0], rows[-1][1] + 1, len(rows) + 1, 0, rows[0][4])],
    lambda rows: [(row[0] + 1, *row[1:]) if index == 0 else row for index, row in enumerate(rows)],
    lambda rows: [(*row[:4], row[4] - 1) if index == 0 else row for index, row in enumerate(rows)],
    lambda rows: [(row[0], row[1] + 1, *row[2:]) for row in rows],
    lambda rows: [],
], ids=["missing_member", "extra_member", "card", "cost_mod", "shifted_index", "no_hl"])
def test_hl_rows_that_differ_from_quest_unit_table_are_logged_without_refusing(guide, caplog, change):
    rows = change(guide.rows)
    assert settled_with_warnings(guide.service, guide.store, TACTICAL_GUIDE, caplog, team("HL", rows)) == [
        f"Quest {TACTICAL_GUIDE} result HL differs from its fixed team: {len(rows)} rows reported, "
        f"{len(guide.rows)} members expected"]


def prince_copies(player, count):
    prince = next(unit for unit in player["units"] if unit["CardID"] in PRINCE_CARD_IDS)
    return tuple(stand_in(player, prince, STAND_IN_OFFSET + index) for index in range(count))


@pytest.mark.parametrize("count", [1, 2])
def test_stand_ins_beside_a_fixed_team_outside_type_10_are_logged_without_refusing(guide, caplog, count):
    assert settled_with_warnings(guide.service, guide.store, TACTICAL_GUIDE, caplog,
                                 team("BL", prince_copies(guide.player, count)), team("HL", guide.rows)) == [
        f"Quest {TACTICAL_GUIDE} result lists {count} stand-in rows beside its fixed team"]


@pytest.mark.parametrize("count,logged", [(1, False), (2, True)])
def test_a_type_10_fixed_team_keeps_one_stand_in(guide, caplog, count, logged):
    teams = (team("BL", prince_copies(guide.player, count)), team("HL", guide.rows))
    request = parse_finish(finish_request(guide.store, TACTICAL_GUIDE, extra=teams))
    with caplog.at_level("WARNING", logger="aigis_ps"):
        guide.service._log_team_differences(guide.player, guide.service.catalog.get(TACTICAL_GUIDE), HARMONY_TYPE,
                                            request)
    assert [entry.getMessage() for entry in caplog.records] == (
        [f"Quest {TACTICAL_GUIDE} result lists {count} stand-in rows beside its fixed team"] if logged else [])


def test_an_unreadable_fixed_team_is_logged_and_the_result_still_settles(guide, caplog, monkeypatch):
    def unavailable(list_id):
        raise ProtocolError("quest_asset_unavailable", "Verified quest resource is unavailable", 503)

    monkeypatch.setattr(guide.service.catalog, "fixed_units", unavailable)
    unit_list = guide.service.catalog.get(TACTICAL_GUIDE).unit_list
    assert settled_with_warnings(guide.service, guide.store, TACTICAL_GUIDE, caplog, team("HL", guide.rows)) == [
        f"Quest {TACTICAL_GUIDE} fixed team {unit_list} cannot be compared: quest_asset_unavailable"]


@pytest.fixture
def derived(bundle):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: MISSION_NOW
    catalog = bundle.missions.catalog
    progress = MissionProgress(catalog, clear_specs=bundle.missions.progress.clear_specs)
    missions = MissionService(bootstrap, bundle.player_state, bundle.presents, catalog, progress=progress)
    quests = copy(bundle.quests)
    quests.bootstrap, quests.missions, quests.clock = bootstrap, missions, lambda: MISSION_NOW
    return SimpleNamespace(bootstrap=bootstrap, missions=missions, quests=quests, catalog=catalog)


def test_derived_specs_keep_story_missions_counting_through_the_story_policy(derived, bundle, tmp_path):
    initial = derived.missions.initialize(bundle.player_state.initialize(derived.bootstrap.initial_state()))
    store = Store(tmp_path / "derived.sqlite3", initial)
    allow_quest(store, 1003)
    targets = [row.condition_id for row in derived.catalog.active(MISSION_NOW)
               if row.condition_type == 2 and row.quest_type == 0 and row.quest_id == 1004]
    assert targets and all(derived.missions.progress.clear_specs.get(key).claimed_by == "story" for key in targets)
    play(derived.quests, store, 1004)

    def progress():
        return {row["ID"]: row["Progress"] for row in derived.missions.progress.snapshot(store.profile().state, MISSION_NOW)}

    assert all(progress()[key] == 1 for key in targets) and progress()[642] == 0
    allow_quest(store, 3006)
    play(derived.quests, store, 3007, tick=1002)
    mutate(store, lambda state: derived.missions.progress.claim(state, [10001], MISSION_NOW))
    play(derived.quests, store, 1005, tick=1004)
    assert progress()[10002] == 0 and progress()[642] == 0


GODDESS_GIFT = {"kind": "item", "amount": 2, "item_id": ITEM, "description": "テスト報酬"}


def settle_with(monkeypatch, *rewards):
    monkeypatch.setattr(quest_modes.policy(0), "settle", lambda context: SettleOutcome(context.state, presents=rewards))


def test_mode_presents_share_the_single_present_child(setup, bundle, monkeypatch):
    service, store = setup
    service.presents = bundle.presents
    settle_with(monkeypatch, GODDESS_GIFT)
    service.start(start_request(service, store), store)
    tree = nodes(service.finish(finish_request(store), store))
    saved = store.profile().state
    added = saved["quest_state"]["history"][-1]["mode_presents"]
    rows = {row["PresentID"]: row for row in bundle.presents.rows(saved)}
    assert "PPT" in tree and len(added) == 1 and saved["quest_state"]["history"][-1]["mode_presents_pending"] == 0
    assert (rows[added[0]]["Type"], rows[added[0]]["Content"], rows[added[0]]["ItemID"]) == (22, 2, ITEM)


def test_waiting_item_presents_keep_the_amount_of_each_settlement(setup, bundle, monkeypatch):
    service, store = setup
    mailbox = Mailbox(bundle.presents)
    service.presents = mailbox
    larger = {**GODDESS_GIFT, "amount": GODDESS_GIFT["amount"] + 1}
    settle_with(monkeypatch, GODDESS_GIFT, GODDESS_GIFT)
    play(service, store)
    assert last_settlement(store)["mode_presents_pending"] == 2
    settle_with(monkeypatch, larger)
    play(service, store, tick=1002)
    assert waiting(store) == [queued(GODDESS_GIFT, 2), queued(larger)]
    mailbox.room = MAX_PRESENTS
    settle_with(monkeypatch)
    play(service, store, tick=1004)
    assert mail(bundle, store, "delivered_presents") == [as_mail(GODDESS_GIFT)] * 2 + [as_mail(larger)]
    assert waiting(store) is None


@pytest.mark.parametrize("reward", [
    {**GODDESS_GIFT, "amount": 0}, {**GODDESS_GIFT, "item_id": ""}, {"kind": "summon_ticket", "pool_id": 1, "count": 1,
    "description": "x"}, {"kind": "free_crystal", "amount": 1}, {**GODDESS_GIFT, "description": "x" * 300},
    {**GODDESS_GIFT, "description": "x" * STORED_PRESENT_TEXT_BYTES}])
def test_invalid_mode_presents_are_refused_without_changes(setup, monkeypatch, reward):
    service, store = setup
    settle_with(monkeypatch, reward)
    service.start(start_request(service, store), store)
    before = store.profile()
    assert_error("quest_mode_hook_invalid", lambda: service.finish(finish_request(store), store))
    assert store.profile() == before


def test_mode_presents_without_a_present_service_refuse_the_settlement(setup, monkeypatch):
    service, store = setup
    settle_with(monkeypatch, GODDESS_GIFT)
    service.start(start_request(service, store), store)
    before = store.profile()
    assert_error("quest_present_unavailable", lambda: service.finish(finish_request(store), store))
    assert store.profile() == before


@pytest.mark.parametrize("queue", [
    [queued(STAR_CRYSTAL, 0)], [queued(STAR_CRYSTAL), queued(dict(STAR_CRYSTAL), 2)], [STAR_CRYSTAL]])
def test_a_stored_queue_outside_the_journal_form_refuses_the_settlement(setup, queue):
    service, store = setup
    service.start(start_request(service, store), store)
    mutate(store, lambda state: state["quest_state"].update(pending_presents=queue) or state)
    before = store.profile()
    assert_error("quest_state_invalid", lambda: service.finish(finish_request(store), store))
    assert store.profile() == before


def test_a_stored_queue_with_256_byte_text_still_loads_and_is_mailed(setup, bundle):
    service, store = setup
    service.presents = bundle.presents
    saved = {**STAR_CRYSTAL, "description": "x" * STORED_PRESENT_TEXT_BYTES}
    service.start(start_request(service, store), store)
    mutate(store, lambda state: state["quest_state"].update(pending_presents=[queued(saved)]) or state)
    service.finish(finish_request(store), store)
    assert mail(bundle, store, "delivered_presents") == [as_mail(saved)] and waiting(store) is None


def test_unmailable_mode_presents_are_refused_instead_of_queued(setup, bundle, monkeypatch):
    service, store = setup
    service.presents = Mailbox(bundle.presents)
    settle_with(monkeypatch, {**GODDESS_GIFT, "item_id": "item_absent_from_config"})
    service.start(start_request(service, store), store)
    before = store.profile()
    assert_error("quest_present_unsupported", lambda: service.finish(finish_request(store), store))
    assert store.profile() == before and "pending_presents" not in before.state["quest_state"]


DIAMOND_GIFT = {**GODDESS_GIFT, "item_id": "gift_0003_diamond", "amount": 1}
BOUQUET_GIFT = {**GODDESS_GIFT, "item_id": "gift_0000_bouquet"}


def test_a_queued_present_the_mailbox_stops_accepting_never_delays_new_presents(setup, bundle, monkeypatch, caplog):
    service, store = setup
    mailbox = Mailbox(bundle.presents)
    service.presents = mailbox
    settle_with(monkeypatch, GODDESS_GIFT, DIAMOND_GIFT)
    play(service, store)
    mailbox.room, mailbox.refused = MAX_PRESENTS, {ITEM}
    settle_with(monkeypatch, BOUQUET_GIFT)
    with caplog.at_level("WARNING", logger="aigis_ps"):
        tree = play(service, store, tick=1002)
    assert "PPT" in tree and last_settlement(store)["mode_presents_pending"] == 0
    assert mail(bundle, store, "delivered_presents") == [as_mail(DIAMOND_GIFT)]
    assert mail(bundle, store, "mode_presents") == [as_mail(BOUQUET_GIFT)]
    assert waiting(store) == [queued(GODDESS_GIFT)]
    assert any("present_item_unsupported" in record.getMessage() for record in caplog.records)


def test_a_capacity_stop_queues_later_presents_behind_the_backlog_in_order(setup, bundle, monkeypatch):
    service, store = setup
    mailbox = Mailbox(bundle.presents)
    service.presents = mailbox
    settle_with(monkeypatch, GODDESS_GIFT, GODDESS_GIFT, DIAMOND_GIFT)
    play(service, store)
    mailbox.room = 1
    fill_wallet(store)
    settle_with(monkeypatch, BOUQUET_GIFT)
    play(service, store, 1002, tick=1002)
    settlement = last_settlement(store)
    assert mail(bundle, store, "delivered_presents") == [as_mail(GODDESS_GIFT)]
    assert (settlement["crystal_presents"], settlement["crystals_pending"]) == ([], 1)
    assert (settlement["mode_presents"], settlement["mode_presents_pending"]) == ([], 1)
    order = [GODDESS_GIFT, DIAMOND_GIFT, STAR_CRYSTAL, BOUQUET_GIFT]
    assert waiting(store) == [queued(reward) for reward in order]
    mailbox.room = MAX_PRESENTS
    settle_with(monkeypatch)
    play(service, store, tick=1004)
    assert mail(bundle, store, "delivered_presents") == [as_mail(reward) for reward in order] and waiting(store) is None


@pytest.fixture(scope="module")
def gold_rush_catalog(bundle):
    shared = bundle.quests.catalog
    return QuestCatalog(shared.snapshot, lambda name: bundle.native_cdn.assets.read(name)[0],
                        possessions=bundle.possessions, resource_version=shared.resource_version,
                        window_overrides={GOLD_RUSH_MISSION: (None, None)})


@pytest.fixture
def gold_rush(setup, bundle, gold_rush_catalog, monkeypatch):
    monkeypatch.setattr(type(quest_modes.policy(REPRODUCE_TYPE)), "enabled", True)
    _, store = setup
    # Reproduce opens behind the same 1004 menu gate as History.
    allow_quest(store, MENU_GATE_QUEST)
    set_rank(bundle, store, GOLD_RUSH_RANK)
    update_game_param(store, NowCharisma=CHARISMA_STOCK)
    return QuestService(gold_rush_catalog, bundle.bootstrap, bundle.player_state, bundle.possessions,
                        clock=lambda: SUNDAY_NOON, randbelow=lambda _: 0), store


def refuse_recomputing(*_args, **_kwargs):
    raise AssertionError("Exact replays must not select prize candidates again")


def reserved_cards(active):
    return [grant["card_id"] for grant in active["unit_reservations"]]


def test_gold_rush_starts_expect_the_prize_enemy_only_from_three_saved_stars(gold_rush, monkeypatch):
    service, store = gold_rush
    before = store.profile()
    assert_error("quest_candidates_mismatch", lambda: service.start(
        start_request(service, store, GOLD_RUSH_QUEST, candidates=GOLD_RUSH_UNSEEN_THREE), store))
    assert store.profile() == before
    response = service.start(start_request(service, store, GOLD_RUSH_QUEST, candidates=GOLD_RUSH_BELOW_THREE), store)
    assert len(nodes(response)["LOTTERY"].children[1].values) == len(GOLD_RUSH_BELOW_THREE)
    active = store.profile().state["quest_state"]["active"]
    assert active["candidate_stars"] == 0
    assert reserved_cards(active) == [card for card in GOLD_RUSH_RESERVED if card != GOLD_RUSH_OVERRIDE]
    put_record(store, GOLD_RUSH_QUEST, flags=GOLD_RUSH_SEEN_FLAGS)
    before = store.profile()
    assert_error("quest_candidates_mismatch", lambda: service.start(
        start_request(service, store, GOLD_RUSH_QUEST, tick=1002, candidates=GOLD_RUSH_SEEN_BELOW_THREE), store))
    assert store.profile() == before
    starting = start_request(service, store, GOLD_RUSH_QUEST, tick=1002, candidates=GOLD_RUSH_SEEN_THREE)
    response = service.start(starting, store)
    assert len(nodes(response)["LOTTERY"].children[1].values) == len(GOLD_RUSH_SEEN_THREE)
    assert store.profile().state["quest_state"]["active"]["candidate_stars"] == 3
    started = store.profile()
    with monkeypatch.context() as patch:
        patch.setattr(QuestDefinition, "candidates_for", refuse_recomputing)
        assert service.start(starting, store) == response and store.profile() == started


def test_a_gold_rush_prize_enemy_grants_its_override_without_slot_one_or_the_collection(gold_rush, monkeypatch):
    service, store = gold_rush
    put_record(store, GOLD_RUSH_QUEST, flags=GOLD_RUSH_SEEN_FLAGS)
    service.start(start_request(service, store, GOLD_RUSH_QUEST, candidates=GOLD_RUSH_SEEN_THREE), store)
    assert reserved_cards(store.profile().state["quest_state"]["active"]) == GOLD_RUSH_RESERVED
    everything = (1 << len(GOLD_RUSH_SEEN_THREE)) - 1
    finishing = finish_request(store, GOLD_RUSH_QUEST, obtained=0b01011, selected=everything)
    response = service.finish(finishing, store)
    tree = nodes(response)
    assert new_cards(tree) == (77, 56, GOLD_RUSH_OVERRIDE) and drops(tree) == {77: 1, 56: 1, GOLD_RUSH_OVERRIDE: 1}
    assert record(store, GOLD_RUSH_QUEST)["AchieveFlag"] == GOLD_RUSH_SEEN_FLAGS
    assert last_settlement(store)["candidate_stars"] == 3
    completed = store.profile()
    with monkeypatch.context() as patch:
        patch.setattr(QuestDefinition, "candidates_for", refuse_recomputing)
        assert service.finish(finishing, store) == response and store.profile() == completed
    service.start(start_request(service, store, GOLD_RUSH_QUEST, tick=1002, candidates=GOLD_RUSH_SEEN_THREE), store)
    tree = nodes(service.finish(finish_request(store, GOLD_RUSH_QUEST, tick=1003, obtained=0b00100,
                                               selected=everything), store))
    assert new_cards(tree) == (133, 249)
    assert record(store, GOLD_RUSH_QUEST)["AchieveFlag"] == GOLD_RUSH_SEEN_FLAGS | SLOT_FLAGS[0] | COLLECTION_BIT


def legacy_attempt(*, candidate_fields):
    def change(active):
        del active["candidate_stars"]
        if candidate_fields:
            for candidate in active["definition"]["candidates"]:
                for name in LEGACY_CANDIDATE_FIELDS:
                    del candidate[name]
    return change


def test_attempts_saved_without_candidate_stars_settle_unconditional_quests_from_every_candidate(setup):
    service, store = setup
    allow_quest(store, 1001)
    service.start(start_request(service, store, 1002), store)
    edit_active(store, legacy_attempt(candidate_fields=True))
    tree = nodes(service.finish(finish_request(store, 1002, obtained=0b111, selected=0b111), store))
    assert drops(tree) == {4: 1, 67: 1, 68: 1, 15: 1} and new_cards(tree) == (4, 67, 68, 15)
    assert last_settlement(store)["candidate_stars"] is None


@pytest.mark.parametrize("candidate_fields,message", [
    (False, "Conditional prize candidates require the MaxRank saved at the attempt start"),
    (True, "Stored quest definition does not match its fixed catalog"),
])
def test_conditional_attempts_saved_without_candidate_stars_are_refused(gold_rush, candidate_fields, message):
    service, store = gold_rush
    service.start(start_request(service, store, GOLD_RUSH_QUEST, candidates=GOLD_RUSH_BELOW_THREE), store)
    selected = store.profile().state["quest_state"]["active"]["selected_mask"]
    edit_active(store, legacy_attempt(candidate_fields=candidate_fields))
    before = store.profile()
    with pytest.raises(ProtocolError, match=f"^{message}$") as error:
        service.finish(finish_request(store, GOLD_RUSH_QUEST, selected=selected), store)
    assert (error.value.code, error.value.status) == ("quest_state_invalid", 503)
    assert store.profile() == before


def bonus_catalog(bundle, bonus_resource):
    shared = bundle.quests.catalog

    def read(name):
        if name != DROP_BONUS_RESOURCE:
            return bundle.native_cdn.assets.read(name)[0]
        if bonus_resource is None:
            raise KeyError(name)
        return bonus_resource
    return QuestCatalog(shared.snapshot, read, possessions=bundle.possessions, resource_version=shared.resource_version)


@pytest.fixture(scope="module")
def bonus_catalogs(bundle):
    return SimpleNamespace(rows=bonus_catalog(bundle, bonus_table([bonus_row(BONUS_QUEST), bonus_row(HARD_QUEST)])),
                           missing=bonus_catalog(bundle, None))


@pytest.fixture(scope="module")
def ticket_provider(bundle, vote_sources):
    return quest_tickets(isolated(bundle), vote_catalog(vote_sources, VoteRules(revival_start=REVIVAL)), vote_sources)


@pytest.fixture
def crystal_store(setup, bundle):
    _, store = setup
    allow_quest(store, MENU_GATE_QUEST)
    set_rank(bundle, store, HISTORY_RANK)
    update_game_param(store, NowCharisma=CHARISMA_STOCK)
    disclose(store, bundle.quests.catalog, CRYSTAL_MISSION)
    return store


def crystal_service(bundle, catalog, tickets=None):
    service = QuestService(catalog, bundle.bootstrap, bundle.player_state, bundle.possessions, randbelow=lambda _: 0)
    if tickets is not None:
        service.event_rewards.attach_vote_tickets(tickets)
    return service


def start_unseen(service, store, quest_id, started, tick=1000):
    quest = service.catalog.get(quest_id)
    service.clock = lambda: started
    unseen = explicit(quest, (0,) * len(quest.candidates))
    service.start(start_request(service, store, quest_id, tick=tick, candidates=unseen), store)
    return quest, store.profile().state["quest_state"]["active"]["selected_mask"]


def play_crystals(service, store, quest_id, started, finished, tick=1000):
    quest, selected = start_unseen(service, store, quest_id, started, tick)
    obtained = sum(1 << index for index, candidate in enumerate(quest.candidates) if candidate.slot == CRYSTAL_SLOT)
    finishing = finish_request(store, quest_id, tick=tick + 1, obtained=obtained, selected=selected)
    service.clock = lambda: finished
    return finishing, service.finish(finishing, store)


def test_a_bonus_row_scales_the_obtained_crystals_of_its_own_quest_on_a_bonus_day(crystal_store, bundle, bonus_catalogs):
    store, service = crystal_store, crystal_service(bundle, bonus_catalogs.rows)
    held = possession(store, CRYSTAL)
    tree = nodes(play_crystals(service, store, BONUS_QUEST, MONDAY_NOON, MONDAY_NOON + MINUTE)[1])
    assert drops(tree) == {CRYSTAL_DROP: CRYSTAL_DROPS} and item_counts(tree) == {CRYSTAL: held + BONUS_CRYSTALS}
    assert bonus_values(tree) == (CRYSTAL_TYPE, BONUS_VALUE) and last_settlement(store)["drop_bonus"] == BONUS_DAY
    tree = nodes(play_crystals(service, store, PLAIN_QUEST, MONDAY_NOON + MINUTE, MONDAY_NOON + 2 * MINUTE,
                               tick=1002)[1])
    assert drops(tree) == {CRYSTAL_DROP: CRYSTAL_DROPS}
    assert item_counts(tree) == {CRYSTAL: held + BONUS_CRYSTALS + CRYSTAL_DROPS}
    assert "BONUS" not in tree and last_settlement(store)["drop_bonus"] is None


@pytest.mark.parametrize("started,finished,receipt", [
    (SUNDAY_NOON, SUNDAY_NOON + MINUTE, NO_BONUS_DAY),
    (SUNDAY_LAST_MINUTE, SUNDAY_LAST_MINUTE + 2 * MINUTE, NO_BONUS_DAY),
    (MONDAY_LAST_MINUTE, MONDAY_LAST_MINUTE + 2 * MINUTE, BONUS_DAY),
], ids=["sunday", "sunday_start_monday_result", "monday_start_tuesday_result"])
def test_the_attempt_start_day_decides_the_bonus_value(crystal_store, bundle, bonus_catalogs, started, finished, receipt):
    store, service = crystal_store, crystal_service(bundle, bonus_catalogs.rows)
    held = possession(store, CRYSTAL)
    tree = nodes(play_crystals(service, store, BONUS_QUEST, started, finished)[1])
    credited = CRYSTAL_DROPS + sum(receipt["increments"].values())
    assert drops(tree) == {CRYSTAL_DROP: CRYSTAL_DROPS} and item_counts(tree) == {CRYSTAL: held + credited}
    assert ("BONUS" in tree) == bool(receipt["value"]) and last_settlement(store)["drop_bonus"] == receipt


def test_a_bonus_day_reports_bonus_even_when_the_result_obtains_none_of_its_item(crystal_store, bundle, bonus_catalogs):
    store, service = crystal_store, crystal_service(bundle, bonus_catalogs.rows)
    _, selected = start_unseen(service, store, BONUS_QUEST, MONDAY_NOON)
    held = possession(store, CRYSTAL)
    tree = nodes(service.finish(finish_request(store, BONUS_QUEST, selected=selected), store))
    assert drops(tree) == {} and possession(store, CRYSTAL) == held
    assert bonus_values(tree) == (CRYSTAL_TYPE, BONUS_VALUE)
    assert last_settlement(store)["drop_bonus"] == {"value": BONUS_VALUE, "increments": {}}


@pytest.mark.parametrize("quest_id", [BONUS_QUEST, PLAIN_QUEST], ids=["bonus_row", "no_bonus_row"])
@pytest.mark.parametrize("field,value,message", [
    ("candidate_stars", -1, "Prize candidates require the record's raw nonnegative MaxRank"),
    ("candidate_stars", True, "Prize candidates require the record's raw nonnegative MaxRank"),
    ("started_at", True, "Saved attempt start time is invalid"),
    ("started_at", "2026-11-02", "Saved attempt start time is invalid"),
    ("started_at", CALENDAR_OVERFLOW_SECONDS, "Saved attempt start time is invalid"),
], ids=["negative_stars", "boolean_stars", "boolean_start", "text_start", "calendar_overflow_start"])
def test_results_refuse_a_corrupted_attempt_snapshot_without_changing_the_state(crystal_store, bundle, bonus_catalogs,
                                                                               quest_id, field, value, message):
    store, service = crystal_store, crystal_service(bundle, bonus_catalogs.rows)
    assert (service.catalog.mode(quest_id).drop_bonus is None) == (quest_id == PLAIN_QUEST)
    prerequisite = service.catalog.get(quest_id).prerequisite
    if prerequisite:
        allow_quest(store, prerequisite)
    _, selected = start_unseen(service, store, quest_id, MONDAY_NOON)
    edit_active(store, lambda active: active.update({field: value}))
    before = store.profile()
    with pytest.raises(ProtocolError, match=f"^{message}$") as error:
        service.finish(finish_request(store, quest_id, selected=selected), store)
    assert (error.value.code, error.value.status) == ("quest_state_invalid", 503)
    assert store.profile() == before


def test_bonus_crystals_beyond_the_stack_are_discarded_through_iof(crystal_store, bundle, bonus_catalogs):
    store, service = crystal_store, crystal_service(bundle, bonus_catalogs.rows)
    stack, held = bundle.possessions.stack(CRYSTAL), possession(crystal_store, CRYSTAL)
    mutate(store, lambda state: bundle.possessions.credit(state, {CRYSTAL: stack - 1 - held}))
    tree = nodes(play_crystals(service, store, BONUS_QUEST, MONDAY_NOON, MONDAY_NOON + MINUTE)[1])
    overflow = BONUS_CRYSTALS - 1
    assert item_counts(tree) == {CRYSTAL: stack} and item_counts(tree, "IOF") == {CRYSTAL: overflow}
    assert drops(tree) == {CRYSTAL_DROP: CRYSTAL_DROPS} and bonus_values(tree) == (CRYSTAL_TYPE, BONUS_VALUE)
    settlement = last_settlement(store)
    assert (settlement["discarded"], settlement["drop_bonus"]) == ({CRYSTAL: overflow}, BONUS_DAY)


def test_bonus_crystals_past_int32_are_capped_before_the_stack_discards_them(crystal_store, bundle):
    catalog = bonus_catalog(bundle, bonus_table([bonus_row(BONUS_QUEST, values=SATURATING_WEEK)]))
    store, service = crystal_store, crystal_service(bundle, catalog)
    stack, held = bundle.possessions.stack(CRYSTAL), possession(store, CRYSTAL)
    credited = apply_drop_bonus(SATURATING_BONUS_VALUE, CRYSTAL_DROPS)
    assert SATURATING_BONUS_VALUE * CRYSTAL_DROPS > credited == INT32_MAX
    finishing, response = play_crystals(service, store, BONUS_QUEST, MONDAY_NOON, MONDAY_NOON + MINUTE)
    tree, overflow = nodes(response), held + credited - stack
    assert item_counts(tree) == {CRYSTAL: stack} and item_counts(tree, "IOF") == {CRYSTAL: overflow}
    assert drops(tree) == {CRYSTAL_DROP: CRYSTAL_DROPS} and bonus_values(tree) == (CRYSTAL_TYPE, SATURATING_BONUS_VALUE)
    settlement = last_settlement(store)
    assert settlement["discarded"] == {CRYSTAL: overflow}
    assert settlement["drop_bonus"] == {"value": SATURATING_BONUS_VALUE, "increments": {CRYSTAL: credited - CRYSTAL_DROPS}}
    completed = store.profile()
    service.clock = lambda: SUNDAY_NOON
    assert service.finish(finishing, store) == response and store.profile() == completed


def test_drop_bonus_never_scales_the_collection_reward(crystal_store, bundle, bonus_catalogs, monkeypatch):
    store, service = crystal_store, crystal_service(bundle, bonus_catalogs.rows)
    slots = (1, 2, 3)
    quest = custom_quest(service, monkeypatch, BONUS_QUEST, treasures=(CRYSTAL_DROP,) * 3 + (0, CRYSTAL_DROP),
                         slots=slots)
    held = possession(store, CRYSTAL)
    service.clock = lambda: MONDAY_NOON
    service.start(start_request(service, store, BONUS_QUEST, candidates=explicit(quest, (0,) * len(slots))), store)
    everything = (1 << len(slots)) - 1
    tree = nodes(service.finish(finish_request(store, BONUS_QUEST, obtained=everything, selected=everything), store))
    assert len(slots) * CRYSTAL_DROPS == BATCH_CRYSTALS
    assert drops(tree) == {CRYSTAL_DROP: BATCH_CRYSTALS + CRYSTAL_DROPS}
    assert item_counts(tree) == {CRYSTAL: held + BATCH_BONUS_CRYSTALS + CRYSTAL_DROPS}
    assert last_settlement(store)["drop_bonus"] == BATCH_BONUS_DAY
    assert record(store, BONUS_QUEST)["AchieveFlag"] & COLLECTION_BIT


def test_four_star_results_never_report_drop_bonus(unlocked, bundle, bonus_catalogs):
    _, store = unlocked
    service = crystal_service(bundle, bonus_catalogs.rows)
    assert bonus_catalogs.rows.mode(HARD_QUEST).drop_bonus is not None
    service.clock = lambda: MONDAY_NOON
    service.start(hard_start(service, store, HARD_QUEST), store)
    tree = nodes(service.finish(finish_request(store, HARD_QUEST, stars=4), store))
    assert "BONUS" not in tree and last_settlement(store)["drop_bonus"] is None


@pytest.mark.parametrize("value", [True, "2026-11-02", CALENDAR_OVERFLOW_SECONDS],
                         ids=["boolean_start", "text_start", "calendar_overflow_start"])
def test_four_star_results_refuse_a_corrupted_attempt_start_without_changing_the_state(unlocked, bundle, bonus_catalogs,
                                                                                     value):
    _, store = unlocked
    service = crystal_service(bundle, bonus_catalogs.rows)
    service.clock = lambda: MONDAY_NOON
    service.start(hard_start(service, store, HARD_QUEST), store)
    edit_active(store, lambda active: active.update(started_at=value))
    before = store.profile()
    with pytest.raises(ProtocolError, match="^Saved attempt start time is invalid$") as error:
        service.finish(finish_request(store, HARD_QUEST, stars=4), store)
    assert (error.value.code, error.value.status) == ("quest_state_invalid", 503)
    assert store.profile() == before


def test_a_missing_bonus_table_settles_without_bonus_and_reports_its_fault(crystal_store, bundle, bonus_catalogs):
    store, service = crystal_store, crystal_service(bundle, bonus_catalogs.missing)
    held = possession(store, CRYSTAL)
    tree = nodes(play_crystals(service, store, BONUS_QUEST, MONDAY_NOON, MONDAY_NOON + MINUTE)[1])
    assert item_counts(tree) == {CRYSTAL: held + CRYSTAL_DROPS} and "BONUS" not in tree
    assert last_settlement(store)["drop_bonus"] is None
    status = bonus_catalogs.missing.drop_bonus_status()
    assert (status["available"], status["error"]["code"]) == (False, "quest_asset_unavailable")


@pytest.mark.parametrize("started,finished,count,reason", [
    (BEFORE_REVIVAL, BEFORE_REVIVAL + SECOND, 0, "no_window"),
    (BEFORE_REVIVAL + SECOND, BEFORE_REVIVAL + 2 * SECOND, TICKETS_PER_RUN, "issued"),
    (SECOND_WINDOW - SECOND, SECOND_WINDOW, TICKETS_PER_RUN, "issued"),
], ids=["start_before_the_window", "start_inside_the_window", "result_in_the_next_group"])
def test_results_credit_quest_tickets_by_the_ticket_window_of_the_attempt_start(crystal_store, bundle, bonus_catalogs,
                                                                                ticket_provider, started, finished,
                                                                                count, reason):
    store, service = crystal_store, crystal_service(bundle, bonus_catalogs.missing, ticket_provider)
    assert service.catalog.get(BONUS_QUEST).charisma == TICKET_CHARISMA
    tree = nodes(play_crystals(service, store, BONUS_QUEST, started, finished)[1])
    receipt = last_settlement(store)["event_rewards"]
    assert receipt["vote_tickets"] == {
        "error": None, "item_id": TICKET if count else None, "count": count, "reason": reason}
    assert receipt["context"]["now"] == started.isoformat(timespec="seconds")
    assert possession(store, TICKET) == count and item_counts(tree).get(TICKET) == (count or None)


def test_tickets_beyond_the_cap_are_discarded_through_iof(crystal_store, bundle, bonus_catalogs, ticket_provider):
    store, service = crystal_store, crystal_service(bundle, bonus_catalogs.missing, ticket_provider)
    mutate(store, lambda state: bundle.possessions.credit(state, {TICKET: ticket_provider.cap}))
    tree = nodes(play_crystals(service, store, BONUS_QUEST, TODAY, TODAY + MINUTE)[1])
    assert item_counts(tree)[TICKET] == ticket_provider.cap and item_counts(tree, "IOF") == {TICKET: TICKETS_PER_RUN}
    assert last_settlement(store)["event_rewards"]["discarded"] == {"vote_tickets": {TICKET: TICKETS_PER_RUN}}


def test_one_result_merges_drops_bonus_and_tickets_into_single_containers_and_replays_unchanged(
        crystal_store, bundle, bonus_catalogs, ticket_provider, monkeypatch):
    store, service = crystal_store, crystal_service(bundle, bonus_catalogs.rows, ticket_provider)
    service.presents = bundle.presents
    monkeypatch.setattr(quest_modes.policy(HISTORY_TYPE), "settle", lambda context: SettleOutcome(
        context.state, (Container("ModeResult"),), presents=(GODDESS_GIFT,)))
    stack, held = bundle.possessions.stack(CRYSTAL), possession(store, CRYSTAL)
    mutate(store, lambda state: bundle.possessions.credit(state, {CRYSTAL: stack - 1 - held,
                                                                  TICKET: ticket_provider.cap}))
    finishing, response = play_crystals(service, store, BONUS_QUEST, TODAY, TODAY + MINUTE)
    names = [child.name for child in loads(response).children]
    assert [names.count(name) for name in ("POSSESSION", "IOF", "BONUS")] == [1, 1, 1]
    assert names[-3:] == ["PPT", "BONUS", "ModeResult"]
    tree = nodes(response)
    overflow = {CRYSTAL: BONUS_CRYSTALS - 1, TICKET: TICKETS_PER_RUN}
    assert item_counts(tree) == {CRYSTAL: stack, TICKET: ticket_provider.cap} and item_counts(tree, "IOF") == overflow
    settlement = last_settlement(store)
    assert (settlement["discarded"], settlement["drop_bonus"]) == (overflow, BONUS_DAY)
    assert settlement["event_rewards"]["discarded"] == {"vote_tickets": {TICKET: TICKETS_PER_RUN}}
    completed = store.profile()
    service.clock = lambda: SUNDAY_NOON
    service.event_rewards.attach_vote_tickets(None, "fixture_detached")
    assert service.finish(finishing, store) == response and store.profile() == completed


def test_previews_report_the_bonus_tickets_and_event_param_without_changing_the_state(
        crystal_store, bundle, bonus_catalogs, ticket_provider):
    store, service = crystal_store, crystal_service(bundle, bonus_catalogs.rows, ticket_provider)
    state = store.profile().state
    saved = deepcopy(state)
    batch = {"source": ENTRUST, "runs": 3, "now": TODAY, "bonus_instant": MONDAY_NOON,
             "drop_items": {CRYSTAL: BATCH_CRYSTALS}, "state": state}
    preview = service.preview_event_rewards(BONUS_QUEST, **batch)
    assert preview["drop_bonus"] == BATCH_BONUS_DAY
    assert preview["event_rewards"]["vote_tickets"] == {"error": None, "item_id": TICKET,
                                                        "count": 3 * TICKETS_PER_RUN, "reason": "issued"}
    assert preview["event_rewards"]["event_param"]["rewritten"] is False
    started_sunday = service.preview_event_rewards(BONUS_QUEST, **{**batch, "bonus_instant": SUNDAY_NOON})
    assert started_sunday["drop_bonus"] == NO_BONUS_DAY
    assert started_sunday["event_rewards"]["vote_tickets"] == preview["event_rewards"]["vote_tickets"]
    result = service.preview_event_rewards(PLAIN_QUEST, source=RESULT, runs=1, now=TODAY, bonus_instant=MONDAY_NOON,
                                           drop_items={CRYSTAL: CRYSTAL_DROPS}, state=state, event_points=5)
    event_param = result["event_rewards"]["event_param"]
    assert result["drop_bonus"] is None and (event_param["rewritten"], event_param["points"]) == (True, 5)
    assert state == saved
    for change in ({"source": "battle"}, {"runs": 0}, {"drop_items": {CRYSTAL: -1}}, {"event_points": -1},
                   {"bonus_instant": "monday"}, {"state": None}):
        with pytest.raises(ProtocolError) as error:
            service.preview_event_rewards(BONUS_QUEST, **{**batch, **change})
        assert (error.value.code, error.value.status) == ("quest_event_preview_invalid", 400)
