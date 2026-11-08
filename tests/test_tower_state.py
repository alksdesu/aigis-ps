from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import json

import pytest

from aigis_ps.business_calendar import BUSINESS_DAY_START_HOUR, business_day
from aigis_ps.config_registry import REGISTRY, ConfigError
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.local_io import MAX_JSON_DEPTH
from aigis_ps.native_calendar import native_day
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.player_state import UNIT_BOOLEANS, UNIT_FIELDS
from aigis_ps.quest_protocol import TeamMember
from aigis_ps.tower_catalog import (
    DESCRIPTIONS, GATE_QUEST, MISSING_TEMPLATE, SCORE_SOURCE, STAR_SOURCE, UNKNOWN_REWARD, TowerCatalog,
)
from aigis_ps.tower_state import (
    CHALLENGE_COST, DOMAIN, LEDGER, MAX_TEAM_ROWS, SCHEMA_VERSION, TEAM_FIELDS, TEAM_SCHEMA_VERSION, TowerRules, claim,
    day_key, describe, load_tower_rules, read_state, record_floor, release_charge, reserve_charge, rewards_due,
    saved_records, settle_clock, team_rows, write_state,
)
from test_business_calendar import FIRST_CLIENT_DAY, LAST_CLIENT_DAY, OFFSET_MINUTES, day_number, offset
from test_quest_service import bundle
from test_storage import NONFINITE
from test_tower_catalog import (
    CONFIG_TABLE, CONSUME, CONSUME_STACK, CRYSTAL_STACK, GENERAL, GENERAL_CRYSTAL, GENERAL_FLOORS, GENERATION, HERO,
    HERO_CRYSTAL, HERO_DAILY, HERO_FLOORS, OTHER_PRINCE, OUTSIDE_FLOOR, PRINCE, TICKET, UX, build, cleared, config,
    native_date, world,
)


NOW = datetime(2026, 11, 2, 12, tzinfo=JST)
DAY = timedelta(days=1)
RULES = TowerRules()
GATE = cleared(GATE_QUEST)
OPENED = cleared(GATE_QUEST, HERO_FLOORS[0])
FIRST_STOCK, FIRST_LIMIT = GENERATION[0][1], GENERATION[0][2]
POOL_CLOSED = "summon_ticket_pool_disabled"
LEDGER_DIGEST = "a" * 64
SAVED_DAY = 739000
LEGACY_SCHEMA = 1
CRYSTAL_PRESENT = {"kind": "free_crystal", "amount": 1, "description": DESCRIPTIONS[SCORE_SOURCE]}
ONE_SECOND = timedelta(seconds=1)
ZONES = tuple(offset(minutes) for minutes in OFFSET_MINUTES)
DAY_HOURS = tuple(dict.fromkeys((0, RULES.generation_hour, RULES.replenish_hour)))


def unit(card_id, get_index, unique_id, **values):
    row = {name: False if name in UNIT_BOOLEANS else 0 for name, _ in UNIT_FIELDS}
    return {**row, "CardID": card_id, "GetIndex": get_index, "UniqueID": unique_id, **values}


def snapshot(saved):
    return {name: saved[name] for name, _ in TEAM_FIELDS}


def member(saved, slot, get_index=None):
    return TeamMember(saved["CardID"], saved["GetIndex"] if get_index is None else get_index, slot, saved["UniqueID"], 0)


PRINCE_UNIT = unit(PRINCE, 0, 1, ClassID=1, Exp=500)
FIRST = unit(56, 2, 7, ClassID=3, Exp=1200, OrganizationIndex=1, SkillEvolved=1, SkillExperience=9, CanFame=True)
SECOND = unit(60, 3, 8, ClassID=4, OrganizationIndex=-1, ClassChangeStatusMod=-1, Famed=True)


def profile(counts=None, records=None):
    rows = [{"QuestID": quest, "MaxRank": row["MaxRank"], "AchieveFlag": row["AchieveFlag"]}
            for quest, row in (records or {}).items()]
    return {"bootstrap_schema": 1, "tables": {
        "possessions": [{"ItemID": item_id, "Count": count} for item_id, count in (counts or {}).items()],
        "quest_records": rows}}


def held(state, item_id):
    return next((row["Count"] for row in state["tables"]["possessions"] if row["ItemID"] == item_id), 0)


def record(state, mission=HERO):
    return state[DOMAIN]["towers"][str(mission)]


def accept(reward):
    return None


def refuse_tickets(reward):
    return POOL_CLOSED if reward.get("item_id") == TICKET else None


def refused(callback, code, status=503):
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert (caught.value.code, caught.value.status) == (code, status)


def settled(catalog, *, records=GATE, at=NOW, rules=RULES, counts=None):
    return settle_clock(profile(counts), catalog, rules, records, at)[0]


def with_domain(state, catalog, change):
    domain = read_state(state, catalog)
    change(domain)
    return write_state(state, catalog, domain)


def legacy_domain():
    # Schema 1 predates high score teams, and its RewardType 3 tiers waited as unknown.
    return {"schema_version": LEGACY_SCHEMA, "consume": {CONSUME: {"day": SAVED_DAY}},
            "towers": {str(HERO): {"floors": {"10001": 90000, "10002": 5000}, "total": 95000, "stack": FIRST_LIMIT,
                                   "generation_day": SAVED_DAY, "score_rewards": {"10001": [1, 2], "10002": [1]},
                                   "star_rewards": [1], "deferred": [
                                       {"source": SCORE_SOURCE, "key": [10001, 3], "reason": UNKNOWN_REWARD}]}},
            "receipts": {"schema_version": 2, "receipts": [], "processed_keys": []}}


def team_domain():
    domain = legacy_domain()
    domain["schema_version"] = TEAM_SCHEMA_VERSION
    domain["towers"][str(HERO)]["teams"] = {"10001": [snapshot(FIRST), snapshot(SECOND)]}
    return domain


def saved_domain():
    domain = team_domain()
    domain["schema_version"] = SCHEMA_VERSION
    domain["charge"] = None
    return domain


def test_rules_default_to_the_official_tower_schedule():
    assert RULES.to_dict() == {"generation_hour": 4, "replenish_hour": 4, "first_access_settles_day": True,
                               "high_score": "self", "member_count_prince": False}
    assert len(RULES.content_version) == 64
    assert TowerRules(high_score="omit").content_version != RULES.content_version
    # Saves and receipts written under the earlier local defaults name a different rule version.
    assert TowerRules(generation_hour=0, member_count_prince=True).content_version != RULES.content_version


@pytest.mark.parametrize("changes", [
    {"generation_hour": 24}, {"replenish_hour": -1}, {"replenish_hour": True}, {"generation_hour": 4.0},
    {"first_access_settles_day": 1}, {"member_count_prince": None}, {"high_score": "team"}, {"high_score": 1},
])
def test_rules_reject_values_outside_their_domain(changes):
    with pytest.raises(ValueError):
        TowerRules(**changes)


def test_rules_file_is_strict(tmp_path):
    path = tmp_path / "tower-rules.json"
    assert load_tower_rules(None) == RULES
    path.write_bytes(b'{"replenish_hour": 5, "high_score": "omit", "first_access_settles_day": false}')
    assert load_tower_rules(path) == TowerRules(replenish_hour=5, high_score="omit", first_access_settles_day=False)
    for text in (b'{"replenish_hour": 5, "replenish_hour": 6}', b'{"towers": 1}', b"[]", b'{"generation_hour": NaN}',
                 b'{"generation_hour": 25}', b'{"high_score": "' + b"x" * 20000 + b'"}'):
        path.write_bytes(text)
        with pytest.raises(ValueError):
            load_tower_rules(path)


@pytest.mark.parametrize("number", NONFINITE)
def test_rules_file_refuses_nonfinite_numbers_while_parsing(tmp_path, number):
    path = tmp_path / "tower-rules.json"
    path.write_bytes(b'{"generation_hour": ' + number.encode("ascii") + b"}")
    with pytest.raises(ValueError, match="finite"):
        load_tower_rules(path)


def test_rules_file_nesting_is_reported_by_the_registry_and_stays_a_value_error(tmp_path):
    path = tmp_path / "tower-rules.json"
    path.write_bytes(b'{"generation_hour": ' + b"[" * MAX_JSON_DEPTH + b"]" * MAX_JSON_DEPTH + b"}")
    with pytest.raises(ValueError, match="nesting"):
        load_tower_rules(path)
    with pytest.raises(ConfigError) as error:
        REGISTRY["tower-rules"].load(path)
    assert (error.value.code, error.value.key) == ("config_nesting_too_deep", "tower-rules")


def test_day_keys_turn_at_their_jst_hours():
    midnight = datetime(2026, 11, 2, tzinfo=JST)
    refill = midnight + timedelta(hours=RULES.replenish_hour)
    assert day_key(midnight, 0) == date(2026, 11, 2).toordinal() - 1
    assert day_key(midnight, 0) == day_key(midnight - ONE_SECOND, 0) + 1
    assert day_key(refill, RULES.replenish_hour) == day_key(refill - ONE_SECOND, RULES.replenish_hour) + 1
    assert day_key(datetime(2026, 11, 1, 19, tzinfo=timezone.utc), RULES.replenish_hour) == day_key(
        refill, RULES.replenish_hour)
    refused(lambda: day_key(midnight.replace(tzinfo=None), 0), "tower_clock_invalid")
    refused(lambda: day_key(datetime.max.replace(tzinfo=timezone.utc), 0), "tower_clock_invalid")
    refused(lambda: day_key(datetime.min.replace(tzinfo=JST), RULES.replenish_hour), "tower_clock_invalid")


@pytest.mark.parametrize("hour", DAY_HOURS)
@pytest.mark.parametrize("day", [date(2026, 11, 2), date(2027, 1, 1), date(2028, 2, 29), date(2028, 3, 1)])
def test_day_keys_stay_the_native_day_of_the_moment_shifted_back_by_their_hour(hour, day):
    turn = datetime.combine(day, time(hour), JST)
    for moment, expected in ((turn - ONE_SECOND, day - DAY), (turn, day)):
        for shown in (moment.astimezone(zone) for zone in ZONES):
            assert day_key(shown, hour) == native_day(shown - timedelta(hours=hour)) == day_number(expected)


@pytest.mark.parametrize("moment", [
    datetime(2026, 11, 2, 3, 59, 59, 999000, tzinfo=JST), datetime(2026, 11, 2, 4, tzinfo=JST),
    datetime(2026, 12, 1, tzinfo=JST), datetime(2027, 1, 1, 3, 59, 59, tzinfo=JST), datetime(2027, 1, 1, 4, tzinfo=JST),
])
def test_a_day_turning_at_the_business_day_start_is_the_business_day(moment):
    for shown in (moment.astimezone(zone) for zone in ZONES):
        assert day_key(shown, BUSINESS_DAY_START_HOUR) == business_day(shown)


@pytest.mark.parametrize("hour", DAY_HOURS)
def test_day_keys_refuse_days_outside_the_client_calendar(hour):
    first = datetime.combine(FIRST_CLIENT_DAY, time(hour), JST)
    last = datetime.combine(LAST_CLIENT_DAY + DAY, time(hour), JST) - ONE_SECOND
    assert (day_key(first, hour), day_key(last, hour)) == (day_number(FIRST_CLIENT_DAY), day_number(LAST_CLIENT_DAY))
    for outside in (first - ONE_SECOND, last + ONE_SECOND):
        for shown in (outside.astimezone(zone) for zone in ZONES):
            refused(lambda: day_key(shown, hour), "tower_clock_invalid")
    refused(lambda: day_key(datetime(1, 1, 1, hour, tzinfo=JST), hour), "tower_clock_invalid")


def test_settlement_leaves_saves_without_an_open_tower_untouched():
    state = profile()
    assert settle_clock(state, build(), RULES, {}, NOW) == (state, ())
    assert settle_clock(state, build(), RULES, {}, NOW)[0] is state


def test_first_access_settles_one_day():
    catalog, state = build(), profile()
    updated, changed = settle_clock(state, catalog, RULES, GATE, NOW)
    assert changed == (CONSUME,) and held(updated, CONSUME) == HERO_DAILY and DOMAIN not in state
    assert updated[DOMAIN]["towers"] == {str(HERO): {
        "floors": {}, "total": 0, "stack": FIRST_STOCK, "generation_day": day_key(NOW, RULES.generation_hour),
        "score_rewards": {}, "star_rewards": [], "deferred": [], "teams": {}}}
    assert updated[DOMAIN]["consume"] == {CONSUME: {"day": day_key(NOW, RULES.replenish_hour)}}
    again, changed = settle_clock(updated, catalog, RULES, GATE, NOW + timedelta(hours=1))
    assert again is updated and changed == ()


def test_first_access_can_wait_for_the_next_day():
    catalog, rules = build(), TowerRules(first_access_settles_day=False)
    state, changed = settle_clock(profile(), catalog, rules, GATE, NOW)
    assert (changed, held(state, CONSUME), record(state)["stack"]) == ((), 0, 0)
    state, changed = settle_clock(state, catalog, rules, GATE, NOW + DAY)
    assert (changed, held(state, CONSUME), record(state)["stack"]) == ((CONSUME,), HERO_DAILY, FIRST_STOCK)


def test_lazy_settlement_accrues_whole_days_up_to_each_cap():
    catalog = build()
    state = settled(catalog)
    state, _ = settle_clock(state, catalog, RULES, GATE, NOW + DAY)
    assert (record(state)["stack"], held(state, CONSUME)) == (2 * FIRST_STOCK, 2 * HERO_DAILY)
    state, _ = settle_clock(state, catalog, RULES, GATE, NOW + 4 * DAY)
    assert (record(state)["stack"], held(state, CONSUME)) == (5 * FIRST_STOCK, 5 * HERO_DAILY)
    state, changed = settle_clock(state, catalog, RULES, GATE, NOW + 14 * DAY)
    assert (record(state)["stack"], held(state, CONSUME), changed) == (FIRST_LIMIT, CONSUME_STACK, (CONSUME,))
    full, changed = settle_clock(state, catalog, RULES, GATE, NOW + 15 * DAY)
    assert (record(full)["stack"], held(full, CONSUME), changed) == (FIRST_LIMIT, CONSUME_STACK, ())
    assert full[DOMAIN]["consume"][CONSUME]["day"] == day_key(NOW + 15 * DAY, RULES.replenish_hour)
    assert record(full)["generation_day"] == day_key(NOW + 15 * DAY, RULES.generation_hour)


def test_refill_stops_at_the_item_stack():
    state, changed = settle_clock(profile({CONSUME: CONSUME_STACK - 1}), build(), RULES, GATE, NOW)
    assert (held(state, CONSUME), changed) == (CONSUME_STACK, (CONSUME,))


def test_generation_and_refill_turn_together_at_the_official_hour():
    catalog = build()
    state = settled(catalog)
    state, changed = settle_clock(state, catalog, RULES, GATE, datetime(2026, 11, 3, 3, 59, 59, tzinfo=JST))
    assert (record(state)["stack"], held(state, CONSUME), changed) == (FIRST_STOCK, HERO_DAILY, ())
    state, changed = settle_clock(state, catalog, RULES, GATE, datetime(2026, 11, 3, 4, tzinfo=JST))
    assert (record(state)["stack"], held(state, CONSUME), changed) == (2 * FIRST_STOCK, 2 * HERO_DAILY, (CONSUME,))


def test_each_day_key_waits_for_its_own_configured_hour():
    catalog, rules = build(), TowerRules(generation_hour=0)
    state = settled(catalog, rules=rules)
    state, changed = settle_clock(state, catalog, rules, GATE, datetime(2026, 11, 3, 3, 59, 59, tzinfo=JST))
    assert (record(state)["stack"], held(state, CONSUME), changed) == (2 * FIRST_STOCK, HERO_DAILY, ())
    state, changed = settle_clock(state, catalog, rules, GATE, datetime(2026, 11, 3, 4, tzinfo=JST))
    assert (record(state)["stack"], held(state, CONSUME), changed) == (2 * FIRST_STOCK, 2 * HERO_DAILY, (CONSUME,))


def test_refills_never_grant_more_than_one_stack_whatever_the_daily_sum():
    tables = world()
    tables[CONFIG_TABLE][0] = config(HERO, HERO_CRYSTAL, INT32_MAX, 0)
    catalog, rules = build(tables), TowerRules(first_access_settles_day=False)
    state, changed = settle_clock(settled(catalog, rules=rules), catalog, rules, GATE, NOW + 2 * DAY)
    assert (changed, held(state, CONSUME)) == ((CONSUME,), CONSUME_STACK)


def test_clock_rollback_never_rewinds_the_day_keys():
    catalog = build()
    state = settled(catalog, at=NOW + 2 * DAY)
    rolled, changed = settle_clock(state, catalog, RULES, GATE, NOW)
    assert rolled is state and changed == ()
    later, _ = settle_clock(rolled, catalog, RULES, GATE, NOW + 3 * DAY)
    assert (record(later)["stack"], held(later, CONSUME)) == (2 * FIRST_STOCK, 2 * HERO_DAILY)


def test_disclosure_opens_the_next_tower_and_refills_add_up():
    tables = world()
    tables[CONFIG_TABLE][1] = config(GENERAL, GENERAL_CRYSTAL, 2, HERO_FLOORS[0])
    catalog = build(tables)
    state = settled(catalog)
    assert set(state[DOMAIN]["towers"]) == {str(HERO)}
    state, changed = settle_clock(state, catalog, RULES, OPENED, NOW + timedelta(hours=1))
    assert set(state[DOMAIN]["towers"]) == {str(HERO), str(GENERAL)} and changed == ()
    assert (record(state, GENERAL)["stack"], held(state, CONSUME)) == (FIRST_STOCK, HERO_DAILY)
    state, _ = settle_clock(state, catalog, RULES, OPENED, NOW + DAY)
    assert held(state, CONSUME) == HERO_DAILY + HERO_DAILY + 2


def test_closed_windows_are_not_settled():
    tables = world()
    tables[CONFIG_TABLE][0] = config(HERO, HERO_CRYSTAL, HERO_DAILY, 0, start=native_date(NOW + DAY),
                                     end=native_date(NOW + 3 * DAY))
    catalog = build(tables)
    state = profile()
    assert settle_clock(state, catalog, RULES, GATE, NOW)[0] is state
    state, _ = settle_clock(state, catalog, RULES, GATE, NOW + DAY)
    assert record(state)["stack"] == FIRST_STOCK
    ended, changed = settle_clock(state, catalog, RULES, GATE, NOW + 5 * DAY)
    assert ended is state and changed == ()


def test_stock_left_by_a_higher_tier_is_kept():
    catalog = build()
    state = with_domain(settled(catalog), catalog, lambda domain: domain["towers"][str(HERO)].update(stack=1600))
    state, _ = settle_clock(state, catalog, RULES, GATE, NOW + 3 * DAY)
    assert record(state)["stack"] == 1600


def test_settlement_inputs_are_validated():
    catalog, state = build(), profile()
    refused(lambda: settle_clock(state, catalog, RULES.to_dict(), GATE, NOW), "tower_rules_invalid")
    refused(lambda: settle_clock(state, catalog, RULES, list(GATE), NOW), "tower_records_invalid")
    refused(lambda: settle_clock(state, catalog, RULES, GATE, NOW.replace(tzinfo=None)), "tower_clock_invalid")
    before_calendar = datetime.combine(FIRST_CLIENT_DAY, time(), JST) - ONE_SECOND
    refused(lambda: settle_clock(state, catalog, RULES, GATE, before_calendar), "tower_clock_invalid")
    refused(lambda: describe(state, catalog, RULES, before_calendar), "tower_clock_invalid")


def test_floor_results_keep_the_best_and_the_tower_total():
    catalog = build()
    state = settled(catalog)
    state, *values = record_floor(state, catalog, HERO, HERO_FLOORS[0], 12000)
    assert values == [0, 12000, 0, 12000]
    same, *values = record_floor(state, catalog, HERO, HERO_FLOORS[0], 9000)
    assert same is state and values == [12000, 12000, 12000, 12000]
    state, *values = record_floor(state, catalog, HERO, HERO_FLOORS[1], 8000)
    assert values == [0, 8000, 12000, 20000]
    assert (record(state)["floors"], record(state)["total"]) == ({"10001": 12000, "10002": 8000}, 20000)
    assert record_floor(state, catalog, HERO, HERO_FLOORS[2], 0)[0] is state


def test_charges_reserved_at_a_start_settle_once_on_their_own_floor():
    catalog = build()
    state = settled(catalog)
    assert read_state(state, catalog)["charge"] is None
    reserved = reserve_charge(state, catalog, HERO_FLOORS[0])
    assert read_state(reserved, catalog)["charge"] == {"quest_id": HERO_FLOORS[0]}
    assert read_state(state, catalog)["charge"] is None
    settled_state, due = release_charge(reserved, catalog, HERO_FLOORS[0])
    assert due == CHALLENGE_COST and read_state(settled_state, catalog)["charge"] is None
    again, due = release_charge(settled_state, catalog, HERO_FLOORS[0])
    assert again is settled_state and due == 0


def test_a_restart_replaces_the_reservation_and_a_foreign_floor_leaves_it():
    catalog = build()
    reserved = reserve_charge(settled(catalog), catalog, HERO_FLOORS[0])
    moved = reserve_charge(reserved, catalog, HERO_FLOORS[1])
    assert read_state(moved, catalog)["charge"] == {"quest_id": HERO_FLOORS[1]}
    kept, due = release_charge(moved, catalog, HERO_FLOORS[0])
    assert kept is moved and due == 0
    refused(lambda: reserve_charge(settled(catalog), catalog, GATE_QUEST), "tower_request_invalid", 400)
    refused(lambda: release_charge(settled(catalog), catalog, GATE_QUEST), "tower_request_invalid", 400)


def test_floor_results_require_a_settled_floor_of_the_tower():
    catalog = build()
    state = settled(catalog)
    refused(lambda: record_floor(profile(), catalog, HERO, HERO_FLOORS[0], 1), "tower_state_invalid")
    refused(lambda: record_floor(state, catalog, GENERAL, HERO_FLOORS[0], 1), "tower_request_invalid", 400)
    refused(lambda: record_floor(state, catalog, HERO, GENERAL_FLOORS[0], 1), "tower_request_invalid", 400)
    for score in (-1, True, 0x80000000):
        refused(lambda: record_floor(state, catalog, HERO, HERO_FLOORS[0], score), "tower_score_invalid")


def test_only_a_strictly_higher_best_replaces_the_saved_team():
    catalog = build()
    first, second = [snapshot(FIRST)], [snapshot(SECOND), snapshot(FIRST)]
    state, *_ = record_floor(settled(catalog), catalog, HERO, HERO_FLOORS[0], 12000, team=first)
    assert record(state)["teams"] == {"10001": first}
    for score in (9000, 12000):
        kept, *values = record_floor(state, catalog, HERO, HERO_FLOORS[0], score, team=second)
        assert kept is state and values == [12000, 12000, 12000, 12000]
    state, *_ = record_floor(state, catalog, HERO, HERO_FLOORS[0], 15000, team=second)
    assert record(state)["teams"] == {"10001": second}
    unsaved, *_ = record_floor(state, catalog, HERO, HERO_FLOORS[0], 16000)
    assert (record(unsaved)["floors"], record(unsaved)["teams"]) == ({"10001": 16000}, {})
    alone, *_ = record_floor(state, catalog, HERO, HERO_FLOORS[1], 8000, team=[])
    assert record(alone)["teams"] == {"10001": second, "10002": []}
    refused(lambda: record_floor(state, catalog, HERO, HERO_FLOORS[1], 8000, team=[snapshot(PRINCE_UNIT)]),
            "tower_state_invalid")


def test_team_rows_keep_real_non_prince_formation_units_in_slot_order():
    reserve, other_prince = unit(57, 4, 9), unit(OTHER_PRINCE, 5, 10)
    units = [PRINCE_UNIT, FIRST, SECOND, reserve, other_prince]
    # The reserve sits outside the formation, so it joins only if one identity column stops being checked.
    members = [member(SECOND, 2), member(PRINCE_UNIT, 0), member(FIRST, 1),
               member(PRINCE_UNIT, 3, get_index=UX + 1001), member(reserve, -1), member(other_prince, 4),
               member(FIRST, 5), member(reserve, 6, get_index=UX + 1002),
               TeamMember(reserve["CardID"] + 1, reserve["GetIndex"], 7, reserve["UniqueID"], 0),
               TeamMember(reserve["CardID"], reserve["GetIndex"], 8, SECOND["UniqueID"], 0),
               TeamMember(reserve["CardID"], FIRST["GetIndex"], 9, reserve["UniqueID"], 0)]
    rows = team_rows(units, members)
    assert rows == [snapshot(FIRST), snapshot(SECOND)] and all(len(row) == len(TEAM_FIELDS) for row in rows)
    assert {"SkillExperience", "CanFame", "Famed"}.isdisjoint(rows[0]) and rows[1]["OrganizationIndex"] == -1
    crowd = [unit(56, index, index) for index in range(2, 2 + MAX_TEAM_ROWS + 3)]
    assert team_rows(crowd, [member(saved, slot) for slot, saved in reversed(list(enumerate(crowd)))]) == [
        snapshot(saved) for saved in crowd[:MAX_TEAM_ROWS]]
    assert team_rows(units, ()) == [] == team_rows(units, [member(PRINCE_UNIT, 0)])


def test_reached_tiers_are_mailed_once():
    catalog = build()
    state, *_ = record_floor(settled(catalog), catalog, HERO, HERO_FLOORS[0], 10000)
    state, presents, deferred = rewards_due(state, catalog, HERO, GATE, accept)
    description = DESCRIPTIONS[SCORE_SOURCE]
    assert presents == ({"kind": "item", "item_id": HERO_CRYSTAL, "amount": 300, "description": description},
                        {"kind": "unit", "template_id": 2, "count": 1, "description": description})
    assert deferred == () and record(state)["score_rewards"] == {"10001": [1, 2]}
    again = rewards_due(state, catalog, HERO, GATE, accept)
    assert again[0] is state and again[1:] == ((), ())


def test_undeliverable_tiers_wait_and_retry_first():
    catalog = build()
    stars = cleared(GATE_QUEST, *HERO_FLOORS)
    state = settled(catalog)
    for floor, score in ((HERO_FLOORS[0], 90000), (HERO_FLOORS[2], 5000)):
        state, *_ = record_floor(state, catalog, HERO, floor, score)
    state, presents, deferred = rewards_due(state, catalog, HERO, stars, refuse_tickets)
    assert [present["description"] for present in presents] == [DESCRIPTIONS[SCORE_SOURCE]] * 3 + [DESCRIPTIONS[STAR_SOURCE]] * 2
    assert presents[2] == CRYSTAL_PRESENT
    assert presents[-1] == {"kind": "unit", "template_id": 3, "count": 1, "description": DESCRIPTIONS[STAR_SOURCE]}
    waiting = ({"source": SCORE_SOURCE, "key": [10003, 1], "reason": MISSING_TEMPLATE},
               {"source": STAR_SOURCE, "key": [2], "reason": POOL_CLOSED})
    assert deferred == waiting and record(state)["star_rewards"] == [1, 3]
    state, *_ = record_floor(state, catalog, HERO, HERO_FLOORS[1], 5000)
    state, presents, deferred = rewards_due(state, catalog, HERO, stars, accept)
    assert [present["item_id"] for present in presents] == [TICKET, HERO_CRYSTAL]
    assert deferred == waiting[:1] and record(state)["star_rewards"] == [1, 2, 3]
    assert record(state)["score_rewards"] == {"10001": [1, 2, 3], "10002": [1]}


def test_crystal_tiers_older_saves_deferred_as_unknown_are_mailed_on_the_next_retry():
    catalog = build()
    state, presents, deferred = rewards_due({**profile(), DOMAIN: legacy_domain()}, catalog, HERO, GATE, accept)
    assert (presents, deferred) == ((CRYSTAL_PRESENT,), ())
    assert record(state)["score_rewards"] == {"10001": [1, 2, 3], "10002": [1]}
    assert (state[DOMAIN]["schema_version"], record(state)["teams"]) == (SCHEMA_VERSION, {})


def test_delivery_checks_must_answer_none_or_a_code():
    catalog = build()
    state, *_ = record_floor(settled(catalog), catalog, HERO, HERO_FLOORS[0], 5000)
    for answer in (7, "", "x" * 200, b"code", True):
        refused(lambda: rewards_due(state, catalog, HERO, GATE, lambda reward: answer), "tower_deliverable_invalid")
    refused(lambda: rewards_due(state, catalog, HERO, GATE, None), "tower_deliverable_invalid")
    refused(lambda: rewards_due(profile(), catalog, HERO, GATE, accept), "tower_state_invalid")
    refused(lambda: rewards_due(state, catalog, 999, GATE, accept), "tower_request_invalid", 400)


def test_claims_move_stock_into_holdings_up_to_the_item_stack():
    catalog = build()
    state = with_domain(settled(catalog), catalog, lambda domain: domain["towers"][str(HERO)].update(stack=FIRST_LIMIT))
    updated, grants = claim(state, catalog, [HERO], {})
    assert (grants, record(updated)["stack"]) == ({HERO_CRYSTAL: FIRST_LIMIT}, 0)
    partial, grants = claim(state, catalog, (HERO, GENERAL), {HERO_CRYSTAL: CRYSTAL_STACK - 999})
    assert (grants, record(partial)["stack"]) == ({HERO_CRYSTAL: 999}, FIRST_LIMIT - 999)
    full, grants = claim(state, catalog, [HERO], {HERO_CRYSTAL: CRYSTAL_STACK})
    assert full is state and grants == {}
    empty, grants = claim(settled(catalog, records=OPENED, at=NOW, rules=TowerRules(first_access_settles_day=False)),
                          catalog, [GENERAL, HERO], {})
    assert grants == {}


def test_claims_share_one_stack_between_towers_paying_the_same_crystal():
    tables = world()
    tables[CONFIG_TABLE][1] = config(GENERAL, HERO_CRYSTAL, 0, HERO_FLOORS[0])
    catalog = build(tables)
    room = FIRST_STOCK + FIRST_STOCK // 5
    state, grants = claim(settled(catalog, records=OPENED), catalog, [HERO, GENERAL], {HERO_CRYSTAL: CRYSTAL_STACK - room})
    assert grants == {HERO_CRYSTAL: room}
    assert (record(state)["stack"], record(state, GENERAL)["stack"]) == (0, 2 * FIRST_STOCK - room)


@pytest.mark.parametrize("missions", [[], [HERO, HERO], [999999], ["110001"], [True], HERO, [HERO, GENERAL, HERO + 2]])
def test_claims_name_distinct_configured_towers(missions):
    catalog = build()
    refused(lambda: claim(settled(catalog), catalog, missions, {}), "tower_request_invalid", 400)


@pytest.mark.parametrize("holdings", [{HERO_CRYSTAL: -1}, {1: 2}, {HERO_CRYSTAL: True}, [(HERO_CRYSTAL, 1)]])
def test_claims_need_nonnegative_holdings(holdings):
    catalog = build()
    refused(lambda: claim(settled(catalog), catalog, [HERO], holdings), "tower_holdings_invalid")


def test_saves_without_the_domain_read_as_not_started():
    catalog, state = build(), profile()
    assert read_state(state, catalog) == {"schema_version": SCHEMA_VERSION, "consume": {}, "towers": {},
                                          "charge": None,
                                          "receipts": {"schema_version": 2, "receipts": [], "processed_keys": []}}
    assert DOMAIN not in state
    saved = {**profile(), DOMAIN: saved_domain()}
    assert read_state(saved, catalog)["towers"] == saved_domain()["towers"]


def test_schema_1_saves_read_as_holding_no_teams_until_their_next_write():
    catalog = build()
    state = {**profile(), DOMAIN: legacy_domain()}
    domain = read_state(state, catalog)
    assert domain["schema_version"] == SCHEMA_VERSION and _hero(domain) == {**_hero(legacy_domain()), "teams": {}}
    assert domain["charge"] is None and state[DOMAIN] == legacy_domain()
    written = write_state(state, catalog, domain)
    assert written[DOMAIN] == domain == read_state(written, catalog) and state[DOMAIN] == legacy_domain()


def test_schema_2_saves_read_as_owing_no_charge_until_their_next_write():
    catalog = build()
    state = {**profile(), DOMAIN: team_domain()}
    domain = read_state(state, catalog)
    assert (domain["schema_version"], domain["charge"]) == (SCHEMA_VERSION, None)
    assert _hero(domain) == _hero(team_domain()) and state[DOMAIN] == team_domain()
    written = write_state(state, catalog, domain)
    assert written[DOMAIN] == domain == read_state(written, catalog) and state[DOMAIN] == team_domain()


@pytest.mark.parametrize("mutate", [
    lambda domain: _hero(domain).update(teams={}),
    lambda domain: _hero(domain).update(extra=0),
    lambda domain: domain.update(towers=[]),
    lambda domain: domain.update(charge=None),
], ids=["teams-before-schema-2", "extra-field", "towers-not-keyed", "charge-before-schema-3"])
def test_schema_1_saves_keep_their_own_shape(mutate):
    domain = legacy_domain()
    mutate(domain)
    refused(lambda: read_state({**profile(), DOMAIN: domain}, build()), "tower_state_invalid")


def test_schema_2_saves_keep_their_own_shape():
    domain = team_domain()
    domain["charge"] = None
    refused(lambda: read_state({**profile(), DOMAIN: domain}, build()), "tower_state_invalid")


def _hero(domain):
    return domain["towers"][str(HERO)]


def _team(domain):
    return _hero(domain)["teams"]["10001"]


@pytest.mark.parametrize("mutate", [
    lambda domain: domain.update(schema_version=4),
    lambda domain: domain.update(schema_version=True),
    lambda domain: domain.update(extra=1),
    lambda domain: domain.pop("receipts"),
    lambda domain: domain.pop("charge"),
    lambda domain: domain.update(charge={}),
    lambda domain: domain.update(charge={"quest_id": OUTSIDE_FLOOR}),
    lambda domain: domain.update(charge={"quest_id": "10001"}),
    lambda domain: domain.update(charge={"quest_id": 10001, "extra": 0}),
    lambda domain: domain.update(charge=[10001]),
    lambda domain: domain["consume"].update({"gift_1000_beer": {"day": 1}}),
    lambda domain: domain["consume"][CONSUME].update(day=-1),
    lambda domain: domain["consume"][CONSUME].update(extra=0),
    lambda domain: domain.update(towers=[]),
    lambda domain: domain["towers"].update({"110009": deepcopy(_hero(domain))}),
    lambda domain: domain["towers"].update({"0110001": domain["towers"].pop(str(HERO))}),
    lambda domain: _hero(domain).update(extra=0),
    lambda domain: _hero(domain)["floors"].update({"11001": 5}) or _hero(domain).update(total=95005),
    lambda domain: _hero(domain)["floors"].update({"10003": 0}),
    lambda domain: _hero(domain)["floors"].update({"10002": True}) or _hero(domain).update(total=90001),
    lambda domain: _hero(domain).update(total=95001),
    lambda domain: _hero(domain).update(stack=-1),
    lambda domain: _hero(domain).update(stack=1621),
    lambda domain: _hero(domain).update(generation_day="1"),
    lambda domain: _hero(domain)["score_rewards"].update({"10001": [9]}),
    lambda domain: _hero(domain)["score_rewards"].update({"10001": [1, 1]}),
    lambda domain: _hero(domain)["score_rewards"].update({"10003": []}),
    lambda domain: _hero(domain)["score_rewards"].update({"11001": [1]}),
    lambda domain: _hero(domain)["score_rewards"].update({"10002": ["1"]}),
    lambda domain: _hero(domain).update(star_rewards=[7]),
    lambda domain: _hero(domain).update(star_rewards=[[1]]),
    lambda domain: _hero(domain).update(deferred=[{"source": "bonus", "key": [1], "reason": "x"}]),
    lambda domain: _hero(domain).update(deferred=[{"source": [STAR_SOURCE], "key": [2], "reason": "x"}]),
    lambda domain: _hero(domain).update(deferred=[{"source": STAR_SOURCE, "key": [2, 1], "reason": "x"}]),
    lambda domain: _hero(domain).update(deferred=[{"source": STAR_SOURCE, "key": [1], "reason": "x"}]),
    lambda domain: _hero(domain)["deferred"].append(dict(_hero(domain)["deferred"][0])),
    lambda domain: _hero(domain)["deferred"].append({"source": SCORE_SOURCE, "key": [OUTSIDE_FLOOR, 1], "reason": "x"}),
    lambda domain: _hero(domain)["deferred"][0].update(reason=""),
    lambda domain: _hero(domain)["deferred"][0].update(extra=0),
    lambda domain: domain.update(receipts={"schema_version": 2, "receipts": [{}], "processed_keys": []}),
    lambda domain: _hero(domain).pop("teams"),
    lambda domain: _hero(domain).update(teams=[]),
    lambda domain: _hero(domain)["teams"].update({"10003": []}),
    lambda domain: _hero(domain)["teams"].update({"11001": []}),
    lambda domain: _hero(domain)["teams"].update({"10002": {}}),
    lambda domain: _team(domain).append(snapshot(PRINCE_UNIT)),
    lambda domain: _team(domain).append(snapshot(unit(OTHER_PRINCE, 5, 10))),
    lambda domain: _team(domain).append(dict(_team(domain)[0])),
    lambda domain: _team(domain).extend(snapshot(unit(57, index, index)) for index in range(20, 20 + MAX_TEAM_ROWS)),
    lambda domain: _team(domain).append([56]),
    lambda domain: _team(domain)[0].pop("SkillEvolved"),
    lambda domain: _team(domain)[0].update(SkillExperience=0),
    lambda domain: _team(domain)[0].update(Exp=True),
    lambda domain: _team(domain)[0].update(Exp=INT32_MAX + 1),
    lambda domain: _team(domain)[0].update(Love="1"),
    lambda domain: _team(domain)[0].update(CardID=0),
    lambda domain: _team(domain)[0].update(UniqueID=0),
])
def test_invalid_saved_domains_are_refused(mutate):
    domain = saved_domain()
    mutate(domain)
    refused(lambda: read_state({**profile(), DOMAIN: domain}, build()), "tower_state_invalid")


def test_receipts_follow_the_shared_request_ledger():
    catalog, domain = build(), saved_domain()
    LEDGER.record(domain, domain["receipts"], {"request": LEDGER_DIGEST, "content": LEDGER_DIGEST, "revision": 5,
                                               "response": "<DA />"})
    state = {**profile(), DOMAIN: domain}
    assert read_state(state, catalog, revision=5)["receipts"]["processed_keys"] == [LEDGER_DIGEST]
    refused(lambda: read_state(state, catalog, revision=4), "tower_state_invalid")


def test_writes_are_validated_and_canonical():
    catalog, domain = build(), saved_domain()
    hero = _hero(domain)
    hero["floors"] = {"10002": 5000, "10001": 90000}
    hero["score_rewards"] = {"10002": [1], "10001": [2, 1]}
    hero["teams"] = {"10002": [snapshot(FIRST)], "10001": [snapshot(SECOND), snapshot(FIRST)]}
    state = profile()
    written = write_state(state, catalog, domain)
    assert list(_hero(written[DOMAIN])["floors"]) == ["10001", "10002"]
    assert _hero(written[DOMAIN])["score_rewards"] == {"10001": [1, 2], "10002": [1]} and DOMAIN not in state
    assert list(_hero(written[DOMAIN])["teams"]) == ["10001", "10002"]
    assert _team(written[DOMAIN]) == [snapshot(SECOND), snapshot(FIRST)]
    hero["total"] = 1
    refused(lambda: write_state(state, catalog, domain), "tower_state_invalid")
    assert DOMAIN not in state


def test_descriptions_report_each_tower_for_the_console():
    catalog = build()
    state = profile(records=OPENED)
    state, _ = settle_clock(state, catalog, RULES, OPENED, NOW)
    state, *_ = record_floor(state, catalog, HERO, HERO_FLOORS[0], 90000, team=[snapshot(SECOND), snapshot(FIRST)])
    state, _, _ = rewards_due(state, catalog, HERO, OPENED, refuse_tickets)
    view = describe(state, catalog, RULES, NOW + DAY, accept)
    hero, general = view["towers"]
    assert (view["started"], view["receipts"], view["charge"]) == (True, 0, None)
    assert view["generation_day"] == day_key(NOW + DAY, RULES.generation_hour)
    assert (view["next_generation_at"], view["next_replenish_at"]) == ("2026-11-04T04:00:00+09:00",
                                                                      "2026-11-04T04:00:00+09:00")
    assert (hero["accessible"], hero["stack"], hero["pending_generation_days"], hero["total_score"]) == (
        True, FIRST_STOCK, 1, 90000)
    assert hero["generation"] == {"score": -1, "count": FIRST_STOCK, "limit": FIRST_LIMIT, "stack_bound": 1620}
    assert hero["deferred"] == [{"source": STAR_SOURCE, "key": [2], "reason": POOL_CLOSED, "current_reason": None}]
    assert hero["due"] == [] and hero["total_stars"] == 3
    assert hero["teams"] == {"10001": [SECOND["CardID"], FIRST["CardID"]]} and general["teams"] == {}
    assert (general["started"], general["disclosed"], general["accessible"]) == (True, True, True)
    assert view["consume"] == [{"item_id": CONSUME, "held": HERO_DAILY, "stack": CONSUME_STACK, "daily_add": HERO_DAILY,
                                "day": day_key(NOW, RULES.replenish_hour), "pending_days": 1}]
    quiet = describe(profile(), catalog, RULES, NOW)
    assert quiet["started"] is False and not any(tower["started"] for tower in quiet["towers"])
    assert json.loads(json.dumps(view)) == view


def test_saved_records_index_each_quest_once():
    state = profile(records=OPENED)
    assert set(saved_records(state)) == {GATE_QUEST, HERO_FLOORS[0]}
    assert settle_clock(profile(), build(), RULES, saved_records(state), NOW)[0][DOMAIN]["towers"].keys() == {
        str(HERO), str(GENERAL)}
    state["tables"]["quest_records"].append(dict(state["tables"]["quest_records"][0]))
    refused(lambda: saved_records(state), "tower_records_invalid")
    refused(lambda: saved_records({"tables": {"quest_records": [{"QuestID": "1004"}]}}), "tower_records_invalid")
    refused(lambda: saved_records({"tables": []}), "tower_records_invalid")
    for row in ({"QuestID": GATE_QUEST, "MaxRank": "3", "AchieveFlag": 0}, {"QuestID": GATE_QUEST, "MaxRank": 3},
                {"QuestID": 0, "MaxRank": 0, "AchieveFlag": 0}, {"QuestID": GATE_QUEST, "MaxRank": 0, "AchieveFlag": None}):
        refused(lambda: saved_records({"tables": {"quest_records": [row]}}), "tower_records_invalid")


def test_descriptions_list_tiers_reached_but_not_yet_settled():
    catalog = build()
    state = with_domain(settled(catalog), catalog, lambda domain: _hero(domain).update(
        floors={"10001": 10000}, total=10000))
    hero = describe(state, catalog, RULES, NOW)["towers"][0]
    assert hero["due"] == [{"source": SCORE_SOURCE, "key": [10001, 1], "threshold": 5000},
                           {"source": SCORE_SOURCE, "key": [10001, 2], "threshold": 10000}]


@pytest.fixture(scope="module")
def native_towers(bundle):
    return TowerCatalog(bundle.quests.catalog, bundle.possessions)


def preview_check(presents):
    def deliverable(reward):
        try:
            presents.preview([reward], 0)
        except ProtocolError as error:
            return error.code
        return None
    return deliverable


def test_native_first_access_opens_the_hero_tower_only(native_towers, bundle):
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    before = held(initial, CONSUME)
    state, changed = settle_clock(initial, native_towers, RULES, GATE, NOW)
    assert changed == (CONSUME,) and held(state, CONSUME) == before + HERO_DAILY
    assert list(state[DOMAIN]["towers"]) == ["110001"] and record(state, 110001)["stack"] == FIRST_STOCK
    state, _ = settle_clock(state, native_towers, RULES, cleared(GATE_QUEST, 10001), NOW + timedelta(hours=1))
    assert list(state[DOMAIN]["towers"]) == ["110001", "110002", "110003"]
    assert held(state, CONSUME) == before + HERO_DAILY


def test_native_ticket_tiers_wait_for_their_pools(native_towers, bundle):
    every_floor = cleared(GATE_QUEST, *native_towers.by_floor)
    state = settled(native_towers, records=every_floor)

    def peak(domain):
        for tower in native_towers.towers:
            domain["towers"][str(tower.mission_id)].update(floors={str(floor): 90000 for floor in tower.floors},
                                                           total=90000 * len(tower.floors))

    state = with_domain(state, native_towers, peak)
    deliverable = preview_check(bundle.presents)
    mailed, crystals, waiting = {}, {}, {}
    for tower in native_towers.towers:
        state, presents, deferred = rewards_due(state, native_towers, tower.mission_id, every_floor, deliverable)
        mailed[tower.mission_id] = len(presents)
        crystals[tower.mission_id] = sum(present == CRYSTAL_PRESENT for present in presents)
        waiting[tower.mission_id] = deferred
    tickets = {mission: [entry["key"][0] for entry in entries if entry["source"] == STAR_SOURCE]
               for mission, entries in waiting.items()}
    assert tickets == {110001: [5, 10, 50, 100], 110002: [50, 100], 110003: [50]}
    assert {entry["reason"] for entries in waiting.values() for entry in entries} == {POOL_CLOSED}
    # Each playable floor off the fifth floors closes its score tiers with one divine crystal.
    assert crystals == {110001: 36, 110002: 36, 110003: 27}
    assert mailed == {110001: 581, 110002: 583, 110003: 428}
    again = rewards_due(state, native_towers, 110001, every_floor, deliverable)
    assert again[0] is state and again[1] == ()
