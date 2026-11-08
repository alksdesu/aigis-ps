from collections import Counter
from datetime import date, datetime, timedelta
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps.energy_clock import JST, UNIX_EPOCH
from aigis_ps.errors import ProtocolError
from aigis_ps.history_disclosure import (
    CONFIG_TABLE, DISCLOSURE_TABLE, ENTRY_FIELDS, HISTORY_CATEGORY, HISTORY_TYPE, UNSET_START_DAY, disclosure,
    elapsed_days, require_disclosed, star_threshold,
)
from aigis_ps.login_reward_state import advance_login_rewards
from aigis_ps.native_calendar import native_day
from aigis_ps.native_clock import parse_native_date
from aigis_ps.native_numbers import INT32_MAX, INT32_MIN
from aigis_ps.quest_catalog import QuestCatalog
from aigis_ps.quest_mode_history import POLICY
from aigis_ps.quest_modes import (
    CLEAR_FLAG, THREE_STAR_FLAG, MissionWindow, MissionWindows, StartContext, validate_entrust_parity,
)
from aigis_ps.quest_service import QuestService
from test_quest_battle_modes import MISSION_NOW, hard_start, mutate, set_rank, update_game_param
from test_quest_service import NOW, allow_quest, bundle, setup, start_request


START_FORMAT = "%Y/%m/%d %H:%M:%S"
PLATFORM, OTHER_PLATFORM = 1, 5
FULL_STARS = 3
CLEARED = CLEAR_FLAG | THREE_STAR_FLAG
# Group 12 opens last, on day 330 after the account start.
OPEN_DAYS = 330
LOGIN_DAY = date(2026, 11, 2).toordinal() - 1
ACCOUNT_START = "2026/11/02 16:49:50"
# 2026-11-02 counted from the 2016-04-14 fallback start.
UNSET_ELAPSED = 3854
# Ten million hours past the login day land 416,666 whole days later, far outside the client calendar.
FAR_CLOCK, FAR_DAYS = "2026/11/02 10000000:00:00", 416666
# Server datetimes end on 9999-12-31; the client would still count later start days, the server refuses them.
LAST_START_DAY = date.max.toordinal() - 1
GATE_QUEST, CHARISMA, HISTORY_RANK = 1004, 1000, 9
DISCLOSURE_ROWS, HISTORY_MEMBERS = 241, 2017
GROUP_DAYS = (1, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300, 330)
GROUP_HEADS = (920000, 920030, 920056, 920081, 920100, 920119, 920136, 920153, 920169, 920188, 920206, 920223)
SAME_GROUP_EDGES = 229
# 94A9B0 never checks these prerequisites; 120186 names no mission at all.
CROSS_GROUP_EDGES = MappingProxyType({
    920030: 920029, 920056: 920055, 920081: 920080, 920100: 920185, 920119: 920118, 920136: 920186,
    920153: 120186, 920169: 920168, 920188: 920187, 920206: 920205, 920223: 920222,
})
# (prerequisite total stars, percent, required stars) of every same-group prerequisite, with its row count.
THRESHOLDS = MappingProxyType({
    (6, 70, 5): 1, (15, 70, 11): 21, (18, 29, 6): 8, (18, 70, 13): 53, (21, 70, 15): 1, (24, 70, 17): 3,
    (27, 70, 19): 73, (30, 69, 21): 61, (33, 70, 24): 5, (39, 69, 27): 1, (93, 70, 66): 2,
})
PROLOGUE, PROLOGUE_MISSION = (9998, 9999), 920000
FIRST_QUEST, FIRST_MISSION = 1, 920001
DROP_QUEST, DROP_MISSION = 45, 920004
SECOND_GROUP_QUEST, SECOND_GROUP_MISSION, CROSS_GROUP_PREREQUISITE = 4158, 920030, 920029
UNLISTED_MISSION = 929999
PAST_WINDOW = (datetime(2026, 1, 1, tzinfo=JST), datetime(2026, 1, 2, tzinfo=JST))
NO_ROW = "The History mission has no disclosure row"
GROUP_CLOSED = "The History group is not disclosed on the last login day"
MISSION_CLOSED = "The History mission is not disclosed on the last login day"
STARS_SHORT = "The History mission needs more stars on its disclosure mission"
WINDOW_CLOSED = "The quest mission is disabled or outside its window"
GATE_LOCKED = "The mode entry requires its gate quest to be cleared"
PREREQUISITE_LOCKED = "The prerequisite quest has not been cleared"
GROUP, ROOT, TARGET = 1, 10, 11
TARGET_QUESTS = "9"
PLAIN_LEVELS = MappingProxyType({1: 0, 2: 0, 3: 0})
HARD_LEVEL = 200


def state_of(login, started):
    return {"tables": {"game_param2": [{"LastLoginDate": login, "GameStartDateTime": started}]}}


def login_state(days, *, login=LOGIN_DAY):
    return state_of(login, date.fromordinal(login + 1 - days).strftime(START_FORMAT))


def start_on(state, days):
    # Claiming at the saved login instant keeps the login ledger consistent and never runs its clock backwards.
    ledger = state["login_rewards"]
    claimed, _ = advance_login_rewards(state, UNIX_EPOCH + timedelta(milliseconds=ledger["last_time"]),
                                       beginner_count=ledger["beginner_total"])
    row = state["tables"]["game_param2"][0]
    row.update(claimed["tables"]["game_param2"][0])
    state["login_rewards"] = claimed["login_rewards"]
    row["GameStartDateTime"] = date.fromordinal(row["LastLoginDate"] + 1 - days).strftime(START_FORMAT)
    return state


def rate(state, ranks, *, flags=CLEARED):
    records = {row["QuestID"]: row for row in state["tables"]["quest_records"]}
    for quest_id, stars in ranks.items():
        records[quest_id] = {"RemainAssault": 0, "FirstDefeatDate": 0, "Shown": False, **records.get(quest_id, {}),
                             "QuestID": quest_id, "MaxRank": stars, "AchieveFlag": flags}
    state["tables"]["quest_records"] = list(records.values())
    return state


def prerequisite_members(catalog, mission_id):
    index = disclosure(catalog)
    target = index.prerequisite(index.entries[mission_id])
    return () if target is None else catalog.mission_windows(HISTORY_TYPE).members(target.mission_id)


def disclose_state(state, catalog, mission_id, *, days=OPEN_DAYS):
    return rate(start_on(state, days), dict.fromkeys(prerequisite_members(catalog, mission_id), FULL_STARS))


def disclose(store, catalog, mission_id, *, days=OPEN_DAYS):
    mutate(store, lambda state: disclose_state(state, catalog, mission_id, days=days))


def records_of(ranks, *, flags=CLEARED):
    return {quest_id: {"QuestID": quest_id, "MaxRank": stars, "AchieveFlag": flags} for quest_id, stars in ranks.items()}


def context(catalog, records=MappingProxyType({}), state=None, *, days=OPEN_DAYS, platform=PLATFORM):
    return StartContext(catalog, None, None, records, 0, MISSION_NOW, False, platform,
                        login_state(days) if state is None else state)


def entry_row(mission_id, group, days, prerequisite=0, percent=0):
    return dict(zip(ENTRY_FIELDS, (mission_id, group, days, prerequisite, percent)))


def config_row(mission_id, members, enable=True):
    return {"MissionID": mission_id, "QuestIdList": members, "Enable": enable,
            "StartDateTime": [0, 0], "EndDateTime": [0, 0]}


def synthetic(entries, configs, levels):
    windows = MissionWindows({HISTORY_CATEGORY: (configs, QuestCatalog._quest_id_list_members(configs))}, {})
    tables = {DISCLOSURE_TABLE: tuple(map(MappingProxyType, entries)), CONFIG_TABLE: tuple(map(MappingProxyType, configs))}
    return SimpleNamespace(resource_rows=tables.__getitem__, mission_windows=lambda quest_type: windows, windows=windows,
                           quest_type=lambda quest_id: HISTORY_TYPE if quest_id in levels else None,
                           row=lambda quest_id: MappingProxyType({"QuestID": quest_id, "_HardLevel": levels[quest_id]}))


def chain(members="1,2", *, enable=True, listed=True, levels=PLAIN_LEVELS, percent=70):
    prerequisite = [config_row(ROOT, members, enable)] if listed else []
    return synthetic([entry_row(ROOT, GROUP, 1), entry_row(TARGET, GROUP, 1, ROOT, percent)],
                     [*prerequisite, config_row(TARGET, TARGET_QUESTS)], levels)


def damaged(rows, change):
    return SimpleNamespace(resource_rows=lambda name: tuple(map(MappingProxyType, change([dict(row) for row in rows]))))


def without(field):
    return lambda rows: [{name: value for name, value in row.items() if name != field} for row in rows]


def replaced(field, value):
    return lambda rows: [{**rows[0], field: value}, *rows[1:]]


def outcome(callback):
    try:
        callback()
    except ProtocolError as error:
        return error.code, error.status, str(error)
    return None


def refused(service, store, quest_id, message, *, tick=1000, code="quest_closed"):
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.start(start_request(service, store, quest_id, tick), store)
    assert (error.value.code, error.value.status, str(error.value)) == (code, 409, message)
    assert store.profile() == before


def started(service, store, quest_id, *, tick=1000):
    service.start(start_request(service, store, quest_id, tick), store)
    return store.profile().state["quest_state"]["active"]["mission_id"]


def overridden_service(bundle, overrides):
    shared = bundle.quests.catalog
    catalog = QuestCatalog(shared.snapshot, lambda name: bundle.native_cdn.assets.read(name)[0],
                           possessions=bundle.possessions, resource_version=shared.resource_version,
                           window_overrides=overrides)
    return QuestService(catalog, bundle.bootstrap, bundle.player_state, bundle.possessions,
                        clock=lambda: NOW, randbelow=lambda _: 0)


def census(catalog, state, records):
    windows = catalog.mission_windows(HISTORY_TYPE)
    disclosed = []
    for mission in windows.ordered():
        result = outcome(lambda: require_disclosed(context(catalog, records, state), mission))
        assert result is None or result[:2] == ("quest_closed", 409)
        if result is None:
            disclosed.append(mission.mission_id)
    return len(disclosed), Counter(catalog.quest_type(quest_id) for mission_id in disclosed
                                   for quest_id in windows.members(mission_id))


@pytest.fixture
def history(setup, bundle):
    service, store = setup
    allow_quest(store, GATE_QUEST)
    update_game_param(store, NowCharisma=CHARISMA)
    set_rank(bundle, store, HISTORY_RANK)
    return service, store


def test_the_real_disclosure_table_indexes_twelve_groups_in_row_order(bundle):
    catalog = bundle.quests.catalog
    rows, index = catalog.resource_rows(DISCLOSURE_TABLE), disclosure(catalog)
    assert len(rows) == len(index.entries) == DISCLOSURE_ROWS
    assert [row["RecordIndex"] for row in rows] == list(range(1, DISCLOSURE_ROWS + 1))
    assert set(index.entries) == {mission.mission_id for mission in catalog.mission_windows(HISTORY_TYPE).ordered()}
    assert tuple(index.groups) == tuple(range(1, len(GROUP_DAYS) + 1))
    assert tuple(group[0].days for group in index.groups.values()) == GROUP_DAYS
    assert tuple(group[0].mission_id for group in index.groups.values()) == GROUP_HEADS
    assert all({entry.days for entry in group} == {group[0].days} for group in index.groups.values())
    chained = [entry for entry in index.entries.values() if entry.prerequisite]
    assert [entry.mission_id for entry in index.entries.values() if not entry.prerequisite] == [PROLOGUE_MISSION]
    assert sum(index.prerequisite(entry) is not None for entry in chained) == SAME_GROUP_EDGES
    assert {entry.mission_id: entry.prerequisite for entry in chained
            if index.prerequisite(entry) is None} == CROSS_GROUP_EDGES


def test_real_same_group_prerequisites_need_the_single_precision_star_counts(bundle):
    catalog = bundle.quests.catalog
    index, windows = disclosure(catalog), catalog.mission_windows(HISTORY_TYPE)
    listed = [member["QuestID"] for member in QuestCatalog._quest_id_list_members(catalog.resource_rows(CONFIG_TABLE))]
    assert len(listed) == len(set(listed)) == HISTORY_MEMBERS
    assert not any(catalog.row(quest_id)["_HardLevel"] for quest_id in listed)
    thresholds = Counter()
    for entry in index.entries.values():
        if (target := index.prerequisite(entry)) is not None:
            total = FULL_STARS * len(windows.members(target.mission_id))
            thresholds[total, entry.percent, star_threshold(total, entry.percent)] += 1
    assert thresholds == THRESHOLDS


@pytest.mark.parametrize("total,percent,required", [
    *THRESHOLDS,
    # Exact integer products of 2 or more round one star above the ceiling.
    (30, 70, 22), (10, 70, 8), (4, 50, 3), (2, 50, 1),
    (0, 70, 0), (0, 0, 0), (0, INT32_MAX, 0), (0, INT32_MIN, 0),
    (INT32_MAX, 100, INT32_MIN), (INT32_MAX, INT32_MAX, INT32_MIN), (INT32_MIN, INT32_MAX, INT32_MIN),
])
def test_star_thresholds_round_like_the_single_precision_client(total, percent, required):
    assert star_threshold(total, percent) == required


@pytest.mark.parametrize("login,started,days", [
    (LOGIN_DAY, ACCOUNT_START, 0), (LOGIN_DAY, "2026/11/01 23:59:59", 1), (LOGIN_DAY, "", UNSET_ELAPSED),
    (LOGIN_DAY, "0", UNSET_ELAPSED), (0, ACCOUNT_START, -LOGIN_DAY), (INT32_MAX, "", INT32_MAX - UNSET_START_DAY),
])
def test_elapsed_days_count_from_the_start_day_to_the_last_login_day(login, started, days):
    assert elapsed_days(state_of(login, started)) == days


@pytest.mark.parametrize("started,start_day", [
    ("1", 1), ("0.5", 0), ("46254.5", 46254), ("1100000", 1100000), (FAR_CLOCK, LOGIN_DAY + FAR_DAYS),
    (str(LAST_START_DAY), LAST_START_DAY),
])
def test_start_days_outside_the_client_calendar_count_whole_days_from_the_native_epoch(started, start_day):
    assert elapsed_days(state_of(LOGIN_DAY, started)) == LOGIN_DAY - start_day


@pytest.mark.parametrize("started", [
    ACCOUNT_START, "2026/11/01 23:59:59", "1970/1/1", "1969/12/31 11:00:00", "3001/1/20", "2026/2/30",
    "3001/1/19 23:59:59",
])
def test_start_days_inside_the_client_calendar_are_its_native_days(started):
    assert elapsed_days(state_of(LOGIN_DAY, started)) == LOGIN_DAY - native_day(parse_native_date(started))


@pytest.mark.parametrize("state", [
    None, {}, {"tables": {"game_param2": []}}, {"tables": {"game_param2": [{"LastLoginDate": LOGIN_DAY}]}},
    {"tables": {"game_param2": [{"GameStartDateTime": ACCOUNT_START}]}},
    *(state_of(login, ACCOUNT_START) for login in (-1, 2 ** 31, True, float(LOGIN_DAY), str(LOGIN_DAY), None)),
    *(state_of(LOGIN_DAY, started) for started in ("2026/11", "garbage", "2026/11/02 -1:00:00", "99999999", 0, None)),
    *(state_of(LOGIN_DAY, started) for started in ("-1", "1e999", "0x1F", "2026/11/02 1000000000:00:00",
                                                   str(LAST_START_DAY + 1))),
])
def test_unusable_saved_dates_are_an_invalid_state(state):
    assert outcome(lambda: elapsed_days(state))[:2] == ("quest_state_invalid", 503)


def test_the_group_gate_opens_on_the_login_day_of_its_first_row_without_charging(history):
    service, store = history
    first, _ = PROLOGUE
    mutate(store, lambda state: start_on(state, 0))
    refused(service, store, first, GROUP_CLOSED)
    mutate(store, lambda state: start_on(state, 1))
    assert started(service, store, first) == PROLOGUE_MISSION
    mutate(store, lambda state: start_on(state, GROUP_DAYS[1] - 1))
    refused(service, store, SECOND_GROUP_QUEST, GROUP_CLOSED, tick=1001)
    mutate(store, lambda state: start_on(state, GROUP_DAYS[1]))
    cross_group = service.catalog.mission_windows(HISTORY_TYPE).members(CROSS_GROUP_PREREQUISITE)
    assert not {row["QuestID"] for row in store.profile().state["tables"]["quest_records"]} & set(cross_group)
    assert started(service, store, SECOND_GROUP_QUEST, tick=1002) == SECOND_GROUP_MISSION


@pytest.mark.parametrize("ranks,allowed", [((2, 2), False), ((3, 2), True), ((2, 3), True), ((0, 3), False)])
def test_the_mission_gate_counts_max_rank_on_its_prerequisite_without_clear_flags(history, ranks, allowed):
    service, store = history
    mutate(store, lambda state: rate(start_on(state, 1), dict(zip(PROLOGUE, ranks)), flags=0))
    if allowed:
        assert started(service, store, FIRST_QUEST) == FIRST_MISSION
    else:
        refused(service, store, FIRST_QUEST, STARS_SHORT)


def test_quest_45_waits_for_thirteen_stars_on_its_prerequisite_mission(history):
    service, store = history
    members = prerequisite_members(service.catalog, DROP_MISSION)
    mutate(store, lambda state: rate(start_on(state, 1), dict(zip(members, (3, 3, 3, 3)))))
    refused(service, store, DROP_QUEST, STARS_SHORT)
    mutate(store, lambda state: rate(state, {members[4]: 1}))
    assert started(service, store, DROP_QUEST) == DROP_MISSION


def test_native_entry_checks_refuse_before_the_disclosure_check(setup, bundle):
    service, store = setup
    update_game_param(store, NowCharisma=CHARISMA)
    set_rank(bundle, store, HISTORY_RANK)
    refused(service, store, FIRST_QUEST, GATE_LOCKED, code="quest_locked")
    allow_quest(store, GATE_QUEST)
    refused(service, store, PROLOGUE[1], PREREQUISITE_LOCKED, code="quest_locked")
    before = store.profile()
    for number in (78, 82):
        error = outcome(lambda: service.start(hard_start(service, store, FIRST_QUEST, number=number), store))
        assert error[:2] == ("quest_hard_unavailable", 409)
    assert store.profile() == before
    refused(service, store, FIRST_QUEST, GROUP_CLOSED)


def test_window_overrides_close_history_missions_without_changing_their_disclosure(history, bundle):
    _, store = history
    closed = overridden_service(bundle, {FIRST_MISSION: PAST_WINDOW})
    refused(closed, store, FIRST_QUEST, WINDOW_CLOSED)
    disclose(store, closed.catalog, FIRST_MISSION)
    refused(closed, store, FIRST_QUEST, WINDOW_CLOSED)
    prerequisite_closed = overridden_service(bundle, {PROLOGUE_MISSION: PAST_WINDOW})
    assert started(prerequisite_closed, store, FIRST_QUEST) == FIRST_MISSION


@pytest.mark.parametrize("state,rated,missions,types", [
    (state_of(0, ACCOUNT_START), False, 0, {}), (login_state(0), False, 0, {}),
    (login_state(1), False, 1, {HISTORY_TYPE: 2}), (login_state(GROUP_DAYS[1] - 1), False, 1, {HISTORY_TYPE: 2}),
    (login_state(GROUP_DAYS[1]), False, 2, {HISTORY_TYPE: 11}), (login_state(OPEN_DAYS), False, 12, {HISTORY_TYPE: 87}),
    (state_of(LOGIN_DAY, ""), False, 12, {HISTORY_TYPE: 87}), (login_state(OPEN_DAYS), True, 241, {HISTORY_TYPE: 2016, 8: 1}),
    (login_state(GROUP_DAYS[1] - 1), True, 30, {HISTORY_TYPE: 217, 8: 1}),
], ids=["never_claimed", "account_start_day", "day_1", "day_29", "day_30", "day_330", "unset_start", "all_three_stars",
        "first_group_all_three_stars"])
def test_real_disclosure_census_matches_the_client(bundle, state, rated, missions, types):
    catalog = bundle.quests.catalog
    windows = catalog.mission_windows(HISTORY_TYPE)
    everyone = (quest_id for mission in windows.ordered() for quest_id in windows.members(mission.mission_id))
    records = records_of(dict.fromkeys(everyone, FULL_STARS)) if rated else {}
    assert census(catalog, state, records) == (missions, Counter(types))


def test_a_history_mission_without_a_disclosure_row_stays_closed(bundle):
    mission = MissionWindow(UNLISTED_MISSION, HISTORY_CATEGORY, True, None, None)
    assert outcome(lambda: require_disclosed(context(bundle.quests.catalog), mission)) == ("quest_closed", 409, NO_ROW)


@pytest.mark.parametrize("head,member", [(ROOT, TARGET), (TARGET, ROOT)], ids=["lower_mission_first", "higher_mission_first"])
@pytest.mark.parametrize("first_days,days,disclosed", [
    (INT32_MAX, -1, True), (INT32_MAX, 0, False), (INT32_MIN, 1, False), (INT32_MIN, 0, True), (5, 4, False), (5, 5, True),
])
def test_the_group_gate_reads_its_first_table_row_with_32_bit_subtraction(head, member, first_days, days, disclosed):
    catalog = synthetic([entry_row(head, GROUP, first_days), entry_row(member, GROUP, 0)],
                        [config_row(ROOT, "1"), config_row(TARGET, "2")], {1: 0, 2: 0})
    result = outcome(lambda: require_disclosed(context(catalog, days=days), catalog.windows.mission(member)))
    assert result == (None if disclosed else ("quest_closed", 409, GROUP_CLOSED))


@pytest.mark.parametrize("own_days,prerequisite,days,result", [
    (5, ROOT, 4, ("quest_closed", 409, MISSION_CLOSED)), (5, ROOT, 5, None), (5, 0, 1, None),
])
def test_only_a_chained_mission_also_waits_for_its_own_days(own_days, prerequisite, days, result):
    catalog = synthetic([entry_row(ROOT, GROUP, 1), entry_row(TARGET, GROUP, own_days, prerequisite)],
                        [config_row(ROOT, "1"), config_row(TARGET, "2")], {1: 0, 2: 0})
    assert outcome(lambda: require_disclosed(context(catalog, days=days), catalog.windows.mission(TARGET))) == result


# The History loader registers any nonzero Enable on every platform, so platform bits never hide a prerequisite.
@pytest.mark.parametrize("options,disclosed", [
    ({}, False), ({"enable": False}, True), ({"enable": 1 << OTHER_PLATFORM}, False), ({"enable": 1 << PLATFORM}, False),
    ({"listed": False}, True),
], ids=["registered", "disabled", "other_platform_bit", "this_platform_bit", "unlisted"])
def test_a_prerequisite_the_platform_never_registered_counts_no_stars(options, disclosed):
    catalog = chain(**options)
    result = outcome(lambda: require_disclosed(context(catalog, days=1), catalog.windows.mission(TARGET)))
    assert result == (None if disclosed else ("quest_closed", 409, STARS_SHORT))


@pytest.mark.parametrize("members,levels,ranks,disclosed", [
    ("1,1,2", PLAIN_LEVELS, {1: 3, 2: 1}, True), ("1,1,2", PLAIN_LEVELS, {1: 3}, False),
    ("1,2,3", PLAIN_LEVELS, {1: 3, 2: 3, 3: 1}, True), ("1,2,3", {**PLAIN_LEVELS, 1: HARD_LEVEL}, {1: 3, 2: 3, 3: 1}, False),
], ids=["repeated_member_counts_twice", "repeated_member_short", "plain_total", "hard_level_adds_a_star"])
def test_prerequisite_totals_follow_the_quest_id_list_and_hard_levels(members, levels, ranks, disclosed):
    catalog = chain(members, levels=MappingProxyType(levels))
    result = outcome(lambda: require_disclosed(context(catalog, records_of(ranks), days=1), catalog.windows.mission(TARGET)))
    assert result == (None if disclosed else ("quest_closed", 409, STARS_SHORT))


@pytest.mark.parametrize("members,levels", [
    ("1,99", PLAIN_LEVELS), ("1,2", {**PLAIN_LEVELS, 2: None}), ("1,2", {**PLAIN_LEVELS, 2: 1.0}),
], ids=["absent_member", "missing_hard_level", "float_hard_level"])
def test_prerequisite_members_outside_the_native_quest_table_are_a_resource_fault(members, levels):
    catalog = chain(members, levels=MappingProxyType(levels))
    result = outcome(lambda: require_disclosed(context(catalog, days=1), catalog.windows.mission(TARGET)))
    assert result[:2] == ("quest_mode_resource_invalid", 503)


@pytest.mark.parametrize("stars", ["3", 3.0, True, None, 2 ** 31])
def test_prerequisite_records_without_a_native_star_count_are_an_invalid_state(stars):
    catalog = chain()
    records = {1: {"QuestID": 1, "MaxRank": stars, "AchieveFlag": CLEARED}}
    result = outcome(lambda: require_disclosed(context(catalog, records, days=1), catalog.windows.mission(TARGET)))
    assert result[:2] == ("quest_state_invalid", 503)


@pytest.mark.parametrize("change", [
    *(without(field) for field in ENTRY_FIELDS),
    *(replaced("DisclosureConditionDays", value) for value in ("1", 1.0, True, None, 2 ** 31)),
    replaced("MissionId", INT32_MIN - 1), lambda rows: [*rows, rows[-1]],
])
def test_disclosure_tables_outside_the_native_contract_fail_history_definitions(bundle, change):
    catalog = bundle.quests.catalog
    broken = damaged(catalog.resource_rows(DISCLOSURE_TABLE), change)
    assert outcome(lambda: disclosure(broken))[:2] == ("quest_mode_resource_invalid", 503)
    assert outcome(lambda: POLICY.validate_row(catalog.row(FIRST_QUEST), broken))[:2] == ("quest_mode_resource_invalid", 503)


@pytest.mark.parametrize("field", ["RecordIndex", "EventIndex", "BannerTextureId", "Title"])
def test_columns_the_client_never_reads_do_not_affect_the_index(bundle, field):
    catalog = bundle.quests.catalog
    assert disclosure(damaged(catalog.resource_rows(DISCLOSURE_TABLE), without(field))) == disclosure(catalog)


def test_history_entrust_admission_runs_the_same_disclosure_check():
    assert POLICY.entrust_parity == frozenset({"check_start"}) and POLICY.entrust_blocker() is None
    assert validate_entrust_parity(POLICY) is None
