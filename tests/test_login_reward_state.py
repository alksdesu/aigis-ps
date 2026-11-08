from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import date, datetime, time, timedelta, timezone

import pytest

from aigis_ps.energy_clock import JST, UNIX_EPOCH, instant
from aigis_ps.errors import ProtocolError
from aigis_ps.login_reward_state import (
    LEDGER_KEY, LEGACY, NEW_LOCAL, RESPONSE_CAPACITY, RULES_CHANGED, advance_login_rewards, initialize_login_rewards,
    read_current_login_rewards, read_login_rewards,
)
from test_business_calendar import FIRST_CLIENT_DAY, LAST_CLIENT_DAY, offset


NOW = datetime(2026, 11, 10, 12, 34, 56, 789123, tzinfo=JST)
BUSINESS_EDGES = (datetime(2026, 12, 1, 3, 59, 59, 999000, tzinfo=JST), datetime(2026, 12, 1, 4, tzinfo=JST),
                  datetime(2027, 1, 1, 3, 59, 59, 999000, tzinfo=JST), datetime(2027, 1, 1, 4, tzinfo=JST))
CLIENT_CALENDAR = (datetime.combine(FIRST_CLIENT_DAY, time(), JST),
                   datetime.combine(LAST_CLIENT_DAY, time(23, 59, 59, 999000), JST))
OUTSIDE_CALENDAR = (CLIENT_CALENDAR[0] - timedelta(milliseconds=1), CLIENT_CALENDAR[1] + timedelta(milliseconds=1))


def representations(moment):
    return (moment, moment.astimezone(timezone.utc), moment.astimezone(offset(480)), moment.astimezone(offset(-480)),
            moment.replace(tzinfo=None))


def shifted_day(now):
    return (instant(now).astimezone(JST) - timedelta(hours=4)).date()


def month_text(day):
    return f"{day.year:04d}-{day.month:02d}"


def epoch_milliseconds(now):
    return (instant(now) - UNIX_EPOCH) // timedelta(milliseconds=1)


def state(day=None, count=0):
    return {"bootstrap_schema": 1, "tables": {"game_param2": [{
        "LastLoginDate": 0 if day is None else day.toordinal() - 1,
        "LogInStampCount": count, "StampCardGrade": 123456, "Other": [1, 2]}],
        "presents": [{"old": "mail"}], "crystals": [{"old": "wallet"}]},
        "receipt": {"old": [3]}}


def seeded(*, new=True, now=NOW, total=7):
    return initialize_login_rewards(state(), now, new_account=new, beginner_count=total)


def assert_unchanged_on_error(source, callback, code=None):
    before = deepcopy(source)
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert source == before
    if code is not None:
        assert caught.value.code == code
    return caught.value


def unrelated(source):
    source = deepcopy(source)
    source.pop("login_rewards", None)
    source["tables"]["game_param2"][0].pop("LastLoginDate")
    source["tables"]["game_param2"][0].pop("LogInStampCount")
    return source


def test_explicit_new_seed_is_separate_from_zero_legacy_baseline():
    original = state()
    new = initialize_login_rewards(original, NOW, new_account=True)
    legacy = initialize_login_rewards(original, NOW)
    assert read_login_rewards(new) == {"schema_version": 1, "last_claim_day": None, "stamp_month": None,
        "stamp_count": 0, "beginner_eligible": True, "beginner_claimed": 0, "beginner_total": 7,
        "last_time": 1794281696789, "baseline_source": "new_local"}
    assert read_login_rewards(legacy)["baseline_source"] == "legacy"
    assert not read_login_rewards(legacy)["beginner_eligible"]
    assert "login_rewards" not in original
    assert unrelated(new) == unrelated(legacy) == unrelated(original)


def test_the_ledger_stays_under_the_key_existing_saves_use():
    source = seeded()
    assert LEDGER_KEY == "login_rewards" and source[LEDGER_KEY] == read_login_rewards(source)


def test_response_capacity_keeps_the_spelling_of_saved_pending_reasons():
    assert RESPONSE_CAPACITY == "login_reward_response_capacity"


def test_baseline_sources_keep_the_spelling_existing_saves_use():
    assert (NEW_LOCAL, LEGACY) == ("new_local", "legacy")


def test_first_claim_at_seed_instant_updates_only_two_native_fields_and_ledger():
    source = seeded()
    before = deepcopy(source)
    updated, result = advance_login_rewards(source, NOW)
    assert (result.day, result.year, result.month, result.ordinal, result.new_day,
            result.beginner_ordinal, result.beginner_remaining) == ("2026-11-10", 2026, 11, 1, True, 1, 6)
    row = updated["tables"]["game_param2"][0]
    assert row["LastLoginDate"] == date(2026, 11, 10).toordinal() - 1
    assert row["LogInStampCount"] == 1
    assert source == before and unrelated(updated) == unrelated(source)
    with pytest.raises(FrozenInstanceError):
        result.ordinal = 99


def test_same_day_updates_only_watermark_and_never_grants_a_second_bonus():
    claimed, _ = advance_login_rewards(seeded(), NOW)
    repeated, result = advance_login_rewards(claimed, NOW + timedelta(hours=2))
    assert not result.new_day and result.ordinal == 1 and result.beginner_ordinal == 0
    assert result.beginner_remaining == 6
    assert repeated["tables"] == claimed["tables"]
    a, b = read_login_rewards(claimed), read_login_rewards(repeated)
    assert b.pop("last_time") - a.pop("last_time") == 7200000
    assert a == b


def test_exact_same_instant_is_an_equal_independent_candidate():
    claimed, _ = advance_login_rewards(seeded(), NOW)
    repeated, result = advance_login_rewards(claimed, NOW)
    assert repeated == claimed and repeated is not claimed and not result.new_day
    repeated["receipt"]["old"].append(4)
    assert claimed["receipt"]["old"] == [3]


def test_skip_days_is_one_cumulative_claim_and_cross_month_resets_one():
    first, _ = advance_login_rewards(seeded(), NOW)
    second, result = advance_login_rewards(first, NOW + timedelta(days=5))
    assert result.ordinal == 2 and result.beginner_ordinal == 2
    next_month, result = advance_login_rewards(second, datetime(2027, 2, 20, 4, tzinfo=JST))
    assert (result.day, result.ordinal, result.beginner_ordinal) == ("2027-02-20", 1, 3)
    assert next_month["login_rewards"]["stamp_month"] == "2027-02"


def test_four_am_boundary_controls_both_claim_day_and_month():
    before = datetime(2026, 11, 1, 3, 59, 59, 999999, tzinfo=JST)
    original = seeded(now=before)
    october, result = advance_login_rewards(original, before)
    assert result.day == "2026-10-31" and result.month == 10
    november, result = advance_login_rewards(october, datetime(2026, 11, 1, 4, tzinfo=JST))
    assert result.day == "2026-11-01" and result.ordinal == 1 and result.beginner_ordinal == 2
    assert november["tables"]["game_param2"][0]["LastLoginDate"] == date(2026, 11, 1).toordinal() - 1


@pytest.mark.parametrize("moment", BUSINESS_EDGES + CLIENT_CALENDAR)
def test_claim_day_stamp_month_and_watermark_follow_the_shifted_jst_clock(moment):
    for now in representations(moment):
        day = shifted_day(now)
        updated, result = advance_login_rewards(seeded(now=now), now)
        assert (result.day, result.year, result.month) == (day.isoformat(), day.year, day.month)
        assert updated[LEDGER_KEY]["stamp_month"] == month_text(day)
        assert updated[LEDGER_KEY]["last_time"] == epoch_milliseconds(now)
        assert updated["tables"]["game_param2"][0]["LastLoginDate"] == day.toordinal() - 1


@pytest.mark.parametrize("total", [1, 7, 366])
def test_beginner_progress_stops_at_its_saved_total(total):
    current = seeded(total=total)
    for index in range(total + 2):
        current, result = advance_login_rewards(current, NOW + timedelta(days=index), beginner_count=total)
        assert result.beginner_ordinal == index + 1 if index < total else result.beginner_ordinal == 0
        assert result.beginner_remaining == max(0, total - index - 1)
    assert current["login_rewards"]["beginner_claimed"] == total


def test_legacy_zero_account_receives_only_future_ordinary_stamps():
    updated, result = advance_login_rewards(seeded(new=False), NOW)
    assert result.new_day and result.ordinal == 1
    assert result.beginner_ordinal == result.beginner_remaining == 0
    assert not updated["login_rewards"]["beginner_eligible"]


@pytest.mark.parametrize("old_day,count,expected,new_day", [
    (date(2026, 11, 10), 5, 5, False), (date(2026, 11, 2), 2, 3, True),
    (date(2026, 10, 31), 31, 1, True), (date(2025, 12, 31), 31, 1, True),
])
def test_legacy_nonzero_fields_are_preserved_as_the_baseline(old_day, count, expected, new_day):
    original = state(old_day, count)
    initialized = initialize_login_rewards(original, NOW)
    assert initialized["tables"] == original["tables"]
    assert initialized["login_rewards"]["last_claim_day"] == old_day.isoformat()
    updated, result = advance_login_rewards(initialized, NOW)
    assert result.ordinal == expected and result.new_day is new_day
    assert result.beginner_ordinal == 0 and updated["login_rewards"]["baseline_source"] == "legacy"


def test_existing_initialization_is_read_only_even_at_another_time_or_origin_flag():
    original, _ = advance_login_rewards(seeded(new=False), NOW)
    initialized = initialize_login_rewards(original, None, new_account=True)
    assert initialized == original and initialized is not original
    assert not initialized["login_rewards"]["beginner_eligible"]
    assert initialize_login_rewards(original, NOW + timedelta(days=100)) == original


def test_reader_returns_an_independent_ledger():
    original = seeded()
    ledger = read_login_rewards(original)
    ledger["beginner_claimed"] = 99
    assert original["login_rewards"]["beginner_claimed"] == 0


@pytest.mark.parametrize("new", [True, False])
def test_timezone_views_of_one_instant_are_identical(new):
    expected = initialize_login_rewards(state(), NOW, new_account=new)
    for view in (NOW.astimezone(timezone.utc), NOW.astimezone(timezone(timedelta(hours=-8))), NOW.replace(tzinfo=None)):
        assert initialize_login_rewards(state(), view, new_account=new) == expected
        assert advance_login_rewards(expected, view) == advance_login_rewards(expected, NOW)


def test_negative_unix_time_and_millisecond_precision_are_exact():
    now = datetime(1969, 12, 31, 23, 59, 59, 999900, tzinfo=timezone.utc)
    initialized = seeded(now=now)
    assert initialized["login_rewards"]["last_time"] == -1
    updated, result = advance_login_rewards(initialized, now)
    assert result.day == "1970-01-01"
    assert updated["tables"]["game_param2"][0]["LastLoginDate"] == date(1970, 1, 1).toordinal() - 1


def test_clock_backwards_by_a_millisecond_is_atomic():
    source, _ = advance_login_rewards(seeded(), NOW)
    error = assert_unchanged_on_error(source, lambda: advance_login_rewards(source, NOW - timedelta(milliseconds=1)),
                                     "login_rewards_clock_backwards")
    assert error.status == 409


def test_submillisecond_clock_variation_does_not_invent_precision():
    source, _ = advance_login_rewards(seeded(), NOW)
    updated, result = advance_login_rewards(source, NOW - timedelta(microseconds=100))
    assert updated == source and not result.new_day


def test_leap_day_and_year_boundary_use_calendar_months():
    initialized = initialize_login_rewards(state(date(2024, 2, 28), 28), datetime(2024, 2, 29, 4, tzinfo=JST))
    leap, result = advance_login_rewards(initialized, datetime(2024, 2, 29, 4, tzinfo=JST))
    assert result.ordinal == 29
    march, result = advance_login_rewards(leap, datetime(2024, 3, 1, 4, tzinfo=JST))
    assert result.ordinal == 1 and march["login_rewards"]["stamp_month"] == "2024-03"


@pytest.mark.parametrize("value", [True, False, 0, -1, 367, 7.0, "7", None])
def test_beginner_total_argument_is_strict_and_bounded(value):
    source = state()
    assert_unchanged_on_error(source, lambda: initialize_login_rewards(source, NOW, beginner_count=value),
                              "login_rewards_count_invalid")
    source = seeded()
    assert_unchanged_on_error(source, lambda: advance_login_rewards(source, NOW, beginner_count=value),
                              "login_rewards_count_invalid")
    assert_unchanged_on_error(source, lambda: read_current_login_rewards(source, beginner_count=value),
                              "login_rewards_count_invalid")


@pytest.mark.parametrize("new", [None, 0, 1, "true"])
def test_new_account_requires_explicit_boolean(new):
    source = state()
    assert_unchanged_on_error(source, lambda: initialize_login_rewards(source, NOW, new_account=new),
                              "login_rewards_origin_invalid")


def test_saved_beginner_total_still_loads_but_cannot_advance_implicitly():
    source = seeded(total=5)
    loaded = initialize_login_rewards(source, NOW)
    assert loaded == source and loaded is not source
    assert read_current_login_rewards(source, beginner_count=5) == read_login_rewards(source)
    assert_unchanged_on_error(source, lambda: read_current_login_rewards(source, beginner_count=7), RULES_CHANGED)
    failure = assert_unchanged_on_error(source, lambda: advance_login_rewards(source, NOW), RULES_CHANGED)
    assert failure.status == 503


@pytest.mark.parametrize("day,count", [(None, 1), (date(2026, 11, 1), 0), (date(2026, 11, 1), 2),
    (date(2026, 10, 31), 32), (date(2026, 11, 11), 1)])
def test_inconsistent_or_future_legacy_baseline_is_not_reset(day, count):
    source = state(day, count)
    assert_unchanged_on_error(source, lambda: initialize_login_rewards(source, NOW), "login_rewards_invalid")


def test_future_legacy_day_is_compared_to_four_am_day_not_wall_date():
    source = state(date(2026, 11, 1), 1)
    assert_unchanged_on_error(source,
        lambda: initialize_login_rewards(source, datetime(2026, 11, 1, 3, 59, tzinfo=JST)), "login_rewards_invalid")


def test_new_account_cannot_overwrite_old_native_baseline():
    source = state(date(2026, 10, 31), 1)
    assert_unchanged_on_error(source, lambda: initialize_login_rewards(source, NOW, new_account=True),
                              "login_rewards_baseline_invalid")


@pytest.mark.parametrize("field,value", [("LastLoginDate", True), ("LastLoginDate", -1),
    ("LastLoginDate", 2**31), ("LastLoginDate", 3652059), ("LogInStampCount", True),
    ("LogInStampCount", -1), ("LogInStampCount", "0")])
def test_invalid_native_field_values_are_rejected(field, value):
    source = state()
    source["tables"]["game_param2"][0][field] = value
    assert_unchanged_on_error(source, lambda: initialize_login_rewards(source, NOW), "login_rewards_invalid")


@pytest.mark.parametrize("source", [None, {}, {"bootstrap_schema": True},
    {"bootstrap_schema": 1, "tables": {}}, {"bootstrap_schema": 1, "tables": {"game_param2": []}},
    {"bootstrap_schema": 1, "tables": {"game_param2": [{}]}}])
def test_invalid_state_shape_is_rejected(source):
    assert_unchanged_on_error(source, lambda: initialize_login_rewards(source, NOW), "login_rewards_invalid")


def test_read_and_advance_require_an_explicitly_initialized_baseline():
    source = state()
    assert_unchanged_on_error(source, lambda: read_login_rewards(source), "login_rewards_required")
    assert_unchanged_on_error(source, lambda: advance_login_rewards(source, NOW), "login_rewards_required")


@pytest.mark.parametrize("field,value", [("schema_version", True), ("schema_version", 2),
    ("last_claim_day", "2026-11-31"), ("last_claim_day", "2026-1-1"), ("stamp_month", "2026-10"),
    ("stamp_count", True), ("stamp_count", 0), ("beginner_eligible", 1), ("beginner_eligible", False),
    ("beginner_claimed", True), ("beginner_claimed", -1), ("beginner_claimed", 8),
    ("beginner_total", 0), ("beginner_total", 367), ("baseline_source", "legacy"),
    ("baseline_source", "unknown"), ("last_time", True), ("last_time", 2**63), ("last_time", -2**63)])
def test_corrupt_ledgers_are_not_overwritten(field, value):
    source, _ = advance_login_rewards(seeded(), NOW)
    source["login_rewards"][field] = value
    assert_unchanged_on_error(source, lambda: read_login_rewards(source), "login_rewards_invalid")
    assert_unchanged_on_error(source, lambda: initialize_login_rewards(source, NOW), "login_rewards_invalid")


def test_native_projection_mismatch_and_preclaim_bonus_are_invalid():
    source, _ = advance_login_rewards(seeded(), NOW)
    source["tables"]["game_param2"][0]["LogInStampCount"] += 1
    assert_unchanged_on_error(source, lambda: read_login_rewards(source), "login_rewards_invalid")
    source = seeded()
    source["login_rewards"]["beginner_claimed"] = 1
    assert_unchanged_on_error(source, lambda: read_login_rewards(source), "login_rewards_invalid")


def test_claim_day_cannot_be_later_than_saved_watermark():
    source, _ = advance_login_rewards(seeded(), NOW)
    source["login_rewards"]["last_time"] -= 86400000
    assert_unchanged_on_error(source, lambda: read_login_rewards(source), "login_rewards_invalid")


@pytest.mark.parametrize("value", [None, 123, [], {"unknown": 1}])
def test_present_invalid_ledger_is_not_treated_as_legacy(value):
    source = state()
    source["login_rewards"] = value
    assert_unchanged_on_error(source, lambda: initialize_login_rewards(source, NOW), "login_rewards_invalid")


@pytest.mark.parametrize("value", [None, True, 0, "now", datetime.max.replace(tzinfo=timezone.utc),
                                   datetime(1, 1, 1, 0, tzinfo=JST)])
def test_bad_clock_is_rejected_without_mutation(value):
    source = state()
    assert_unchanged_on_error(source, lambda: initialize_login_rewards(source, value), "login_rewards_clock_invalid")


@pytest.mark.parametrize("moment", OUTSIDE_CALENDAR)
def test_instants_outside_the_client_calendar_are_refused(moment):
    for now in representations(moment):
        source = state()
        assert_unchanged_on_error(source, lambda: initialize_login_rewards(source, now), "login_rewards_clock_invalid")
        source = seeded()
        assert_unchanged_on_error(source, lambda: advance_login_rewards(source, now), "login_rewards_clock_invalid")
    source = seeded()
    source[LEDGER_KEY]["last_time"] = epoch_milliseconds(moment)
    assert_unchanged_on_error(source, lambda: read_login_rewards(source), "login_rewards_invalid")
