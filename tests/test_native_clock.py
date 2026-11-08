from datetime import datetime, timedelta, timezone

import pytest

from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.native_clock import (MAX_DAY_ONE_MILLISECONDS, UTC_DAY_ONE, NativeDateUndetermined, day_one_milliseconds,
                                   native_clock, parse_native_date)
from test_business_calendar import import_closure


CLOSES = datetime(2026, 8, 27, 11, tzinfo=JST)
UNIX_DAY = datetime(1970, 1, 1, tzinfo=JST)
FIRST_UTC_INSTANT = datetime.min.replace(tzinfo=timezone.utc)
LAST_UTC_INSTANT = datetime.max.replace(tzinfo=timezone.utc)
# Mission windows in quest_modes need this clock, and growth_campaign loads quest_modes.
CLOCK_READERS = frozenset(("quest_modes", "growth_campaign"))


@pytest.mark.parametrize(("text", "expected"), [
    ("2026/8/20 11:00:00", datetime(2026, 8, 20, 11, tzinfo=JST)),
    ("2026/8/20 11:00", datetime(2026, 8, 20, 11, tzinfo=JST)),
    ("2026-8-20 11:00:00", datetime(2026, 8, 20, 11, tzinfo=JST)),
    ("2026/8/20", datetime(2026, 8, 20, tzinfo=JST)),
    ("2026-8-20", datetime(2026, 8, 20, tzinfo=JST)),
    ("2026/8/20 0:0:0", datetime(2026, 8, 20, tzinfo=JST)),
    ("2026/8/20 11:00:00 JST", datetime(2026, 8, 20, 11, tzinfo=JST)),
    ("2026/8/20+11:00", datetime(2026, 8, 20, 11, tzinfo=JST)),
    ("2026/8/20abc", datetime(2026, 8, 20, tzinfo=JST)),
    ("2026/8/20\0 11:00:00", datetime(2026, 8, 20, tzinfo=JST)),
    ("46254.5", datetime(127, 8, 23, 12, tzinfo=JST)),
    (" 46254", datetime(127, 8, 23, tzinfo=JST)),
    ("2026", datetime(6, 7, 20, tzinfo=JST)),
    ("2147483648e-5", datetime(59, 10, 18, 20, 4, 31, 872000, tzinfo=JST)),
    ("99999999999e-10", datetime(1, 1, 11, tzinfo=JST)),
    ("2026/8/20 129:534443557:30", datetime(3042, 10, 19, 17, 27, 55, 648000, tzinfo=JST)),
])
def test_native_dates_follow_the_numeric_date_and_clock_branches(text, expected):
    assert parse_native_date(text) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("2026/2/30", datetime(2026, 3, 2, tzinfo=JST)),
    ("2026/13/1", datetime(2027, 1, 1, tzinfo=JST)),
    ("2026/8/0", datetime(2026, 7, 31, tzinfo=JST)),
    ("1969/14/1", datetime(1970, 2, 1, tzinfo=JST)),
    ("2026/2/30 11:00:00", datetime(2026, 3, 2, 11, tzinfo=JST)),
])
def test_out_of_range_months_and_days_normalize_like_mktime(text, expected):
    assert parse_native_date(text) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("1969/12/31", UNIX_DAY),
    ("1969/12/31 11:00:00", UNIX_DAY.replace(hour=11)),
    ("3001/1/20", UNIX_DAY),
    ("3003/1/1", UNIX_DAY),
    ("1968/36/1", UNIX_DAY),
    ("3001/1/1", datetime(3001, 1, 1, tzinfo=JST)),
    ("3001/1/19 23:59:59", datetime(3001, 1, 19, 23, 59, 59, tzinfo=JST)),
])
def test_mktime_failures_fall_back_to_1970_within_the_client_crt_bounds(text, expected):
    assert parse_native_date(text) == expected


@pytest.mark.parametrize("text", [
    "$B4AE", "2026/8", "abc", " ", "2026/8-20", "0x1F", " -0x1", "2026/8/20 -1:00:00", "2026/8/20 11:-1:00",
    "2026/8/20 11:00:-1", "2147483648/1/1", "2026/8/99999999999", "1e999",
])
def test_undetermined_native_dates_are_rejected(text):
    with pytest.raises(NativeDateUndetermined):
        parse_native_date(text)


@pytest.mark.parametrize("text", ["", "0", "-0", "0.000000001", "\0abc"])
def test_empty_and_zero_dates_are_the_unset_sentinel(text):
    assert parse_native_date(text) is None


@pytest.mark.parametrize("text", ["-1", "1e10", "2147483648.5"])
def test_dates_outside_the_supported_calendar_are_not_undetermined(text):
    with pytest.raises(ValueError) as caught:
        parse_native_date(text)
    assert not isinstance(caught.value, NativeDateUndetermined)


def test_float32_clock_boundaries_match_the_client_clock():
    assert parse_native_date("2026/8/27 10:59:59") == datetime(2026, 8, 27, 10, 59, 59, tzinfo=JST)
    assert parse_native_date("2026/8/27 11:00:00") == CLOSES
    rounded_up = parse_native_date("2026/8/20 06:00:07")
    rounded_down = parse_native_date("2026/8/27 12:00:08")
    assert rounded_up == datetime(2026, 8, 20, 6, 0, 7, 4000, tzinfo=JST)
    assert rounded_down == datetime(2026, 8, 27, 12, 0, 7, 996000, tzinfo=JST)
    assert native_clock(datetime(2026, 8, 20, 6, 0, 7, tzinfo=JST)) == rounded_up
    assert native_clock(datetime(2026, 8, 27, 12, 0, 8, 999000, tzinfo=JST)) == rounded_down
    assert native_clock(CLOSES.astimezone(timezone.utc)) == CLOSES


def test_the_clock_reads_whole_seconds_and_falls_back_to_1970_outside_the_client_calendar():
    assert native_clock(CLOSES + timedelta(microseconds=999999)) == CLOSES
    assert native_clock(datetime(3001, 1, 20, 11, tzinfo=JST)) == UNIX_DAY.replace(hour=11)
    assert native_clock(datetime(1969, 12, 31, 11, tzinfo=JST)) == parse_native_date("1969/12/31 11:00:00")


@pytest.mark.parametrize("moment", [
    None, "2026-08-21", datetime(2026, 8, 21, 12), datetime.max.replace(tzinfo=timezone.utc),
    datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=10))),
])
def test_the_clock_requires_an_aware_datetime_it_can_place_in_jst(moment):
    with pytest.raises(ProtocolError) as caught:
        native_clock(moment)
    assert (caught.value.code, caught.value.status) == ("quest_clock_invalid", 503)


def test_native_dates_require_text():
    with pytest.raises(ValueError) as caught:
        parse_native_date(20260820)
    assert not isinstance(caught.value, NativeDateUndetermined)


def test_the_client_clock_never_loads_the_modules_that_read_it():
    assert not import_closure("native_clock") & CLOCK_READERS


def saved_sum(moment: datetime) -> int:
    # Mission watermarks and trade history already hold this sum, so the shared conversion must reproduce it.
    utc = moment.astimezone(timezone.utc)
    seconds = (utc.toordinal() - 1) * 86400 + utc.hour * 3600 + utc.minute * 60 + utc.second
    return seconds * 1000 + utc.microsecond // 1000


@pytest.mark.parametrize("moment", [
    UTC_DAY_ONE, UTC_DAY_ONE + timedelta(microseconds=999), UTC_DAY_ONE + timedelta(microseconds=1000),
    datetime(1, 1, 1, 9, tzinfo=JST), datetime(1970, 1, 1, tzinfo=timezone.utc) - timedelta(microseconds=1),
    datetime(2026, 11, 1, 4, tzinfo=JST) - timedelta(microseconds=1), datetime(2026, 11, 1, 4, tzinfo=JST),
    datetime(2026, 11, 6, 12, 34, 56, 789999, tzinfo=timezone(timedelta(hours=-14))),
    datetime.max.replace(tzinfo=timezone(timedelta(hours=14))), LAST_UTC_INSTANT,
])
def test_day_one_milliseconds_reproduce_the_saved_sum(moment):
    assert day_one_milliseconds(moment) == saved_sum(moment)


def test_naive_day_one_clocks_are_the_jst_wall_clock():
    naive = datetime(2026, 11, 1, 0, 5, 0, 999)
    assert day_one_milliseconds(naive) == day_one_milliseconds(naive.replace(tzinfo=JST)) == saved_sum(
        naive.replace(tzinfo=JST))


def test_the_day_one_range_ends_at_the_last_utc_millisecond():
    assert UTC_DAY_ONE == FIRST_UTC_INSTANT and day_one_milliseconds(UTC_DAY_ONE) == 0
    assert MAX_DAY_ONE_MILLISECONDS == day_one_milliseconds(LAST_UTC_INSTANT) == 315537897599999
    assert UTC_DAY_ONE + timedelta(milliseconds=MAX_DAY_ONE_MILLISECONDS) == LAST_UTC_INSTANT.replace(microsecond=999000)


@pytest.mark.parametrize("moment", [
    datetime.min.replace(tzinfo=JST), datetime(1, 1, 1, 8, 59, 59, 999999, tzinfo=JST),
    datetime.max.replace(tzinfo=timezone(timedelta(hours=-1))),
])
def test_instants_outside_the_utc_calendar_have_no_day_one_milliseconds(moment):
    with pytest.raises(OverflowError):
        day_one_milliseconds(moment)


@pytest.mark.parametrize("value", [None, 0, "2026-11-01", datetime(2026, 11, 1).date()])
def test_day_one_milliseconds_require_a_datetime(value):
    with pytest.raises(ProtocolError) as caught:
        day_one_milliseconds(value)
    assert caught.value.code == "energy_clock_argument"
