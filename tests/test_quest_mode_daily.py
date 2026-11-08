from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from aigis_ps import quest_modes
from aigis_ps.alxml import loads
from aigis_ps.business_calendar import business_day
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.errors import ProtocolError
from aigis_ps.quest_catalog import QuestCatalog, StarReward
from aigis_ps.quest_mode_daily import POLICY
from aigis_ps.quest_modes import CLEAR_FLAG, SLOT_FLAGS, THREE_STAR_FLAG
from aigis_ps.quest_service import QuestService
from aigis_ps.storage import Store
from test_quest_battle_modes import (
    MISSION_NOW, bare_start, charisma, gold_events, hard_start, mutate, new_cards, put_record, record, set_rank,
    three_star_gold, update_game_param,
)
from test_quest_service import (
    NOW, allow_quest, bundle, client_candidates, finish_request, nodes, setup, start_request,
)
from test_quest_settlement import derived, drops, possession


DAILY_TYPE = 2
SCRIPTED_GATE_QUESTS = (1003, 1004)
DAILY_RANK = 5
CHARISMA = 1000
SPIRIT_MISSION = 700102
BEGINNER_MISSION = 10002
BEGINNER_QUESTS = frozenset((101, 111, 121, 135, 138))
MAGIC_CRYSTAL = "trade_item_001000"
CONSIGNMENT_TICKET = "item_consignment_ticket"
DAILY_CATEGORY = quest_modes.MODE_SPECS[DAILY_TYPE].mission_category
EVERY_DAY = 0x7F
INT32_MAX = 2 ** 31 - 1
SCHEDULE_FIELDS = ("_HoldingParam", "OpenWeek", "StartOpenDay", "EndOpenDay")
SUN, MON, TUE, WED, THU, FRI, SAT = range(1, 8)
WEEK = MappingProxyType({weekday: NOW + timedelta(days=weekday - SUN) for weekday in range(SUN, SAT + 1)})
LIMITED = MappingProxyType({304: (SUN, MON, FRI), 314: (SUN, THU), 324: (SUN, TUE), 334: (SUN, THU), 344: (SUN, WED, SAT)})
NATIVE_DAY = NOW.date().toordinal() - 1


def assert_refused(code, status, callback):
    with pytest.raises(ProtocolError) as error:
        callback()
    assert (error.value.code, error.value.status) == (code, status)
    return error.value


def assert_start_refused(code, service, store, starting):
    before = store.profile()
    error = assert_refused(code, 409, lambda: service.start(starting, store))
    assert store.profile() == before
    return error


def active(store):
    return store.profile().state["quest_state"]["active"]


def item_counts(tree):
    columns = tree["POSSESSION"].children
    return dict(zip(columns[0].values, columns[1].values))


def unlock_prerequisite(service, store, quest_id):
    prerequisite = service.catalog.get(quest_id).prerequisite
    if prerequisite:
        allow_quest(store, prerequisite)


def fresh_service(bundle, window_overrides=None):
    shared = bundle.quests.catalog
    catalog = QuestCatalog(shared.snapshot, shared._asset_reader, possessions=bundle.possessions,
                           resource_version=shared.resource_version, window_overrides=window_overrides)
    return QuestService(catalog, bundle.bootstrap, bundle.player_state, bundle.possessions,
                        clock=lambda: NOW, randbelow=lambda _: 0)


@pytest.fixture
def daily(setup, bundle):
    service, store = setup
    set_rank(bundle, store, DAILY_RANK)
    update_game_param(store, NowCharisma=CHARISMA)
    return service, store


def test_daily_policy_is_registered_with_the_permanent_mode_contract():
    assert quest_modes.policy(DAILY_TYPE) is POLICY
    assert POLICY.status() == {"policy": "daily", "enabled": True, "reason": None, "hard_mode": False,
                               "timed_conditions": True, "gate_quest": None}


def test_every_weekday_quest_builds_from_the_current_resources(bundle):
    catalog = bundle.quests.catalog
    quest_ids = catalog.playable_quests(DAILY_TYPE)
    assert len(quest_ids) == 28 and set(quest_ids) <= set(catalog.mode_ids)
    for quest_id in quest_ids:
        quest, mode = catalog.get(quest_id), catalog.mode(quest_id)
        assert mode.hard is None and mode.gold_counted and mode.mission_category == 5 and len(mode.mission_ids) == 1
        assert mode.star_rewards_normal == (StarReward("crystal", 0, 0),)
        assert all(1 <= candidate.slot <= len(SLOT_FLAGS) for candidate in quest.candidates)
    assert {quest_id for quest_id in quest_ids if catalog.row(quest_id)["_HoldingParam"] != EVERY_DAY} == set(LIMITED)


def test_weekday_quests_open_at_rank_five_without_any_scripted_gate_quest(setup, bundle):
    service, store = setup
    update_game_param(store, NowCharisma=CHARISMA)
    for quest_id in SCRIPTED_GATE_QUESTS:
        put_record(store, quest_id, max_rank=0, flags=0)
    set_rank(bundle, store, DAILY_RANK - 1)
    assert_start_refused("quest_rank_required", service, store, start_request(service, store, 101))
    set_rank(bundle, store, DAILY_RANK)
    error = assert_start_refused("quest_locked", service, store, start_request(service, store, 103))
    assert str(error) == "The prerequisite quest has not been cleared"
    service.start(start_request(service, store, 101), store)
    attempt = active(store)
    assert (attempt["quest_id"], attempt["mission_id"], attempt["mode"]["quest_type"]) == (101, 700101, DAILY_TYPE)
    assert charisma(store) == CHARISMA
    service.finish(finish_request(store, 101, selected=attempt["selected_mask"], stars=1), store)
    assert charisma(store) == CHARISMA - service.catalog.get(101).charisma


@pytest.mark.parametrize("quest_id,weekdays", [(101, tuple(WEEK)), *LIMITED.items()])
def test_weekday_quests_open_only_on_their_jst_weekdays(daily, quest_id, weekdays):
    service, store = daily
    unlock_prerequisite(service, store, quest_id)
    for tick, (weekday, moment) in enumerate(WEEK.items(), 1000):
        service.clock = lambda moment=moment: moment
        starting = start_request(service, store, quest_id, tick)
        if weekday in weekdays:
            service.start(starting, store)
            assert active(store)["client_time"] == tick
        else:
            assert_start_refused("quest_closed", service, store, starting)


@pytest.mark.parametrize("moment,opened", [
    (datetime(2026, 11, 2, 23, 59, 59), False), (datetime(2026, 11, 3), True),
    (datetime(2026, 11, 3, 23, 59, 59), True), (datetime(2026, 11, 4, 0, 4, 59), True),
    (datetime(2026, 11, 4, 0, 5), False), (datetime(2026, 11, 2, 0, 4, 59), True), (datetime(2026, 11, 2, 0, 5), False),
    (datetime(2026, 11, 2, 14, 59, 59, tzinfo=timezone.utc), False), (datetime(2026, 11, 2, 15, tzinfo=timezone.utc), True),
    (datetime(2026, 11, 3, 15, 4, 59, tzinfo=timezone.utc), True), (datetime(2026, 11, 3, 15, 5, tzinfo=timezone.utc), False),
    (datetime(2026, 11, 3, 20, tzinfo=timezone.utc), False),
])
def test_limited_quests_follow_the_jst_day_with_five_minutes_of_grace(daily, moment, opened):
    service, store = daily
    unlock_prerequisite(service, store, 324)
    service.clock = lambda: moment
    starting = start_request(service, store, 324)
    if opened:
        service.start(starting, store)
        assert active(store)["quest_id"] == 324
    else:
        assert_start_refused("quest_closed", service, store, starting)


@pytest.mark.parametrize("window", [
    (MISSION_NOW - timedelta(days=1), MISSION_NOW), (MISSION_NOW + timedelta(seconds=1), MISSION_NOW + timedelta(days=1)),
])
def test_a_closed_weekday_mission_refuses_its_quests_without_charging(daily, bundle, window):
    _, store = daily
    service = fresh_service(bundle, {SPIRIT_MISSION: window})
    assert service.catalog.mode(121).mission_ids == (SPIRIT_MISSION,)
    assert_start_refused("quest_closed", service, store, start_request(service, store, 121))
    service.start(start_request(service, store, 101), store)
    assert active(store)["quest_id"] == 101


def test_a_started_limited_quest_settles_after_the_jst_day_changes(daily):
    service, store = daily
    unlock_prerequisite(service, store, 324)
    service.clock = lambda: datetime(2026, 11, 3, 23, 59)
    service.start(start_request(service, store, 324), store)
    service.clock = lambda: datetime(2026, 11, 4, 1)
    service.finish(finish_request(store, 324, selected=active(store)["selected_mask"], stars=2), store)
    row = record(store, 324)
    assert row["AchieveFlag"] == CLEAR_FLAG and row["MaxRank"] == 2
    assert row["FirstDefeatDate"] == business_day(datetime(2026, 11, 4, 1)) == datetime(2026, 11, 3).toordinal() - 1


@pytest.mark.parametrize("crystals", [1, 3])
def test_first_three_star_clear_credits_drops_gold_events_and_local_crystals_once(daily, crystals):
    service, store = daily
    service.rules = replace(service.rules, first_three_star_crystals=crystals)
    quest = service.catalog.get(138)
    items, wallet = possession(store, MAGIC_CRYSTAL), wallet_totals(store.profile().state)["free"]
    response = service.start(start_request(service, store, 138), store)
    assert len(nodes(response)["LOTTERY"].children[0].values) == len(quest.candidates)
    attempt = active(store)
    assert attempt["mission_id"] == 700103 and attempt["mode"] == {"quest_type": DAILY_TYPE, "difficulty": "normal",
        "boost_ignored": None, "star_rewards": [{"kind": "crystal", "reward_type": 0, "count": 0, "card_id": None, "item_id": None}]}
    mask = attempt["selected_mask"]
    finishing = finish_request(store, 138, obtained=mask, selected=mask, gold=2, extra=gold_events(2))
    response = service.finish(finishing, store)
    completed = store.profile()
    assert service.finish(finishing, store) == response and store.profile() == completed
    tree = nodes(response)
    assert tree["REWARD"].children[0].values == (quest.rank_exp,)
    assert tree["REWARD"].children[1].values == (three_star_gold(quest.gold + 2 * service.rules.gold_per_event),)
    assert drops(tree) == {2001: 4, 2002: 4} and item_counts(tree) == {MAGIC_CRYSTAL: items + 8}
    assert wallet_totals(completed.state)["free"] == sum(tree["PFC"].children[2].values) == wallet + crystals
    names = [child.name for child in loads(response).children]
    assert names[-1] == "PFC" and names.count("POSSESSION") == 1
    row = record(store, 138)
    assert row["MaxRank"] == 3 and row["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG | SLOT_FLAGS[0] | SLOT_FLAGS[1]
    assert row["FirstDefeatDate"] == business_day(NOW)
    history = completed.state["quest_state"]["history"][-1]
    assert (history["crystals"], history["gold_events"], history["mission_id"]) == (crystals, 2, 700103)
    assert history["star_rewards"] == attempt["mode"]["star_rewards"]
    assert all(attribute == 1 for _, _, attribute in client_candidates(service, store, 138))
    service.start(start_request(service, store, 138, tick=1002), store)
    mask = active(store)["selected_mask"]
    tree = nodes(service.finish(finish_request(store, 138, tick=1003, obtained=mask, selected=mask), store))
    assert "PFC" not in tree and wallet_totals(store.profile().state)["free"] == wallet + crystals
    assert item_counts(tree) == {MAGIC_CRYSTAL: items + 16}


def test_slot_four_and_five_drops_grant_units_and_mark_every_slot_bit(daily):
    service, store = daily
    assert sorted({candidate.slot for candidate in service.catalog.get(121).candidates}) == [1, 2, 3, 4, 5]
    service.start(start_request(service, store, 121), store)
    units = [56, 56, 57, 57, 58, 59, 60]
    assert sorted(grant["card_id"] for grant in active(store)["unit_reservations"]) == units
    mask = active(store)["selected_mask"]
    tree = nodes(service.finish(finish_request(store, 121, obtained=mask, selected=mask, stars=1), store))
    assert sorted(new_cards(tree)) == units and drops(tree) == {56: 2, 57: 2, 58: 1, 59: 1, 60: 1} and "PFC" not in tree
    row = record(store, 121)
    assert row["MaxRank"] == 1 and row["AchieveFlag"] == CLEAR_FLAG | sum(SLOT_FLAGS)
    stale = [(index, slot, 0) for index, slot, _ in client_candidates(service, store, 121)]
    assert_start_refused("quest_candidates_mismatch", service, store, start_request(service, store, 121, 1002, candidates=stale))
    assert all(attribute == 1 for _, _, attribute in client_candidates(service, store, 121))
    service.start(start_request(service, store, 121, tick=1002), store)
    assert active(store)["client_time"] == 1002


def test_orb_drops_report_absolute_possession_counts(daily, bundle):
    service, store = daily
    mutate(store, lambda state: bundle.possessions.credit(state, {"evolve_orb_100003": 1}))
    gained = {"evolve_orb_100003": 2, "evolve_orb_100007": 1, "evolve_orb_100002": 1}
    before = {item: possession(store, item) for item in gained}
    service.start(start_request(service, store, 148), store)
    mask = active(store)["selected_mask"]
    tree = nodes(service.finish(finish_request(store, 148, obtained=mask, selected=mask), store))
    assert item_counts(tree) == {item: before[item] + amount for item, amount in gained.items()}
    assert {item: possession(store, item) for item in gained} == item_counts(tree)
    assert record(store, 148)["AchieveFlag"] & sum(SLOT_FLAGS[:4]) == sum(SLOT_FLAGS[:4])


def test_four_star_starts_are_refused_without_charging(daily):
    service, store = daily
    put_record(store, 101)
    assert_start_refused("quest_hard_unavailable", service, store, hard_start(service, store, 101))


def test_weekday_quests_are_entrust_products_of_their_one_daily_mission(bundle):
    catalog, windows = bundle.entrust.catalog, bundle.quests.catalog.mission_windows(DAILY_TYPE)
    quest_ids = bundle.quests.catalog.playable_quests(DAILY_TYPE)
    assert len(quest_ids) == 28
    for quest_id in quest_ids:
        product = catalog.product(quest_id)
        (mission,) = windows.missions(quest_id)
        assert (product.quest_type, product.ticket_item_id, product.ticket_count) == (DAILY_TYPE, CONSIGNMENT_TICKET, 1)
        assert product.mode.mission_ids == (mission.mission_id,) and mission.category == DAILY_CATEGORY


def test_beginner_mission_counts_only_beginner_weekday_clears(derived, bundle, tmp_path):
    spec = derived.missions.progress.clear_specs.get(BEGINNER_MISSION)
    assert spec.claimed_by == "daily" and spec.quest_types == (DAILY_TYPE,) and spec.quest_ids == BEGINNER_QUESTS
    initial = derived.missions.initialize(bundle.player_state.initialize(derived.bootstrap.initial_state()))
    store = Store(tmp_path / "beginner.sqlite3", initial)
    allow_quest(store, 1004)
    allow_quest(store, 3006)
    quests = derived.quests
    quests.start(start_request(quests, store, 3007), store)
    quests.finish(finish_request(store, 3007), store)
    mutate(store, lambda state: derived.missions.progress.claim(state, [10001], MISSION_NOW))
    set_rank(bundle, store, DAILY_RANK)
    update_game_param(store, NowCharisma=CHARISMA)

    def beginner():
        rows = derived.missions.progress.snapshot(store.profile().state, MISSION_NOW)
        row = next(row for row in rows if row["ID"] == BEGINNER_MISSION)
        return row["Progress"], row["Status"]

    assert beginner() == (0, 0)
    for tick, quest_id, expected in ((1002, 251, (0, 0)), (1004, 101, (1, 1))):
        quests.start(start_request(quests, store, quest_id, tick=tick), store)
        quests.finish(finish_request(store, quest_id, tick=tick + 1, selected=active(store)["selected_mask"], stars=1), store)
        assert beginner() == expected


@pytest.mark.parametrize("field,value", [
    ("Type", 0), ("Type", 4), ("Type", 2.0), ("_HoldingParam", -1), ("_HoldingParam", EVERY_DAY + 1),
    ("_HoldingParam", True), ("OpenWeek", -1), ("OpenWeek", 11), ("OpenWeek", "1"), ("StartOpenDay", -1),
    ("EndOpenDay", -1), ("EndOpenDay", INT32_MAX + 1), ("UnitList", 1), ("UnitList", None),
])
def test_rows_outside_the_weekday_contract_are_refused(bundle, field, value):
    catalog = bundle.quests.catalog
    error = assert_refused("quest_definition_invalid", 503,
                           lambda: POLICY.validate_row(MappingProxyType({**catalog.row(101), field: value}), catalog))
    assert field in str(error)


@pytest.mark.parametrize("changes", [
    {"_HoldingParam": 0, "OpenWeek": 0}, {"_HoldingParam": EVERY_DAY}, {"_HoldingParam": 0, "OpenWeek": 10},
    {"StartOpenDay": NATIVE_DAY, "EndOpenDay": NATIVE_DAY + 1}, {"StartOpenDay": INT32_MAX},
])
def test_boundary_rows_satisfy_the_weekday_contract(bundle, changes):
    catalog = bundle.quests.catalog
    assert POLICY.validate_row(MappingProxyType({**catalog.row(101), **changes}), catalog) is None


def test_a_weekday_row_outside_the_contract_refuses_to_start_without_charging(daily, bundle):
    _, store = daily
    service = fresh_service(bundle)
    rows = service.catalog._all_rows
    rows[121] = {**rows[121], "_HoldingParam": EVERY_DAY + 1}
    before = store.profile()
    assert_refused("quest_definition_invalid", 503, lambda: service.start(bare_start(service, store, 121), store))
    assert store.profile() == before
    assert service.catalog.status()["mode_failed_quests"][121]["code"] == "quest_definition_invalid"


def test_only_absent_schedule_columns_read_as_zero_like_the_client(bundle):
    catalog = bundle.quests.catalog
    row = catalog.row(101)
    assert POLICY.validate_row(MappingProxyType({name: value for name, value in row.items()
                                                 if name not in SCHEDULE_FIELDS}), catalog) is None
    for field in ("Type", "UnitList"):
        error = assert_refused("quest_definition_invalid", 503, lambda: POLICY.validate_row(
            MappingProxyType({name: value for name, value in row.items() if name != field}), catalog))
        assert field in str(error)
