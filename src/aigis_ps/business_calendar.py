from __future__ import annotations

from datetime import date, datetime, timedelta
import re
from types import MappingProxyType

from .energy_clock import JST, NEW_ORIGIN, SECONDS_PER_DAY, UNIX_EPOCH_DAY, UTC_OFFSET_MINUTES, instant
from .errors import ProtocolError


MARKER_KEY = "business_calendar"
SCHEMA_VERSION = 1
NATIVE_DATETIME = "%Y/%m/%d %H:%M:%S"
DAY_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
# MONTH_TEXT checks the shape only: its readers build the date, which refuses a month outside 01-12.
MONTH_TEXT = re.compile(r"([0-9]{4})-([0-9]{2})\Z")
CALENDAR_MONTH_TEXT = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])\Z")
NATIVE_SECONDS_TEXT = re.compile(r"([0-9]{4})([/\-])([0-9]{1,2})\2([0-9]{1,2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})\Z")
NATIVE_MINUTES_TEXT = re.compile(
    r"([0-9]{4})([/\-])([0-9]{1,2})\2([0-9]{1,2}) ([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?\Z")
# 660F70 subtracts one day from 63EBA0's civil day number before JST 04:00.
BUSINESS_DAY_START_HOUR = 4
BUSINESS_DAY_START = timedelta(hours=BUSINESS_DAY_START_HOUR)
MONTHS_PER_YEAR = 12
TM_YEAR_BASE = 1900
# UCRT common_mktime and common_localtime_s bounds in the client, wider than the documented 1970..3000.
MKTIME_TM_YEARS = range(69, 1103)
LOCALTIME_MAX_SECONDS = 0x793582AFF
MARKER = MappingProxyType({"schema_version": SCHEMA_VERSION, "utc_offset_minutes": UTC_OFFSET_MINUTES,
                           "origin": NEW_ORIGIN})
MARKED, LEGACY, INVALID = "marked", "legacy_host_calendar", "invalid"
FIELD_CALENDARS = MappingProxyType({
    "created_local": "wall_text", "game_param2.GameStartDateTime": "wall_text",
    "crystals.Date": "civil_day", "quest_records.FirstDefeatDate": "business_day",
})


def _clock_invalid() -> ProtocolError:
    return ProtocolError("business_clock_invalid", "Business dates need a server instant inside the client calendar", 503)


def _marker_invalid() -> ProtocolError:
    return ProtocolError("business_calendar_invalid", "Saved business calendar marker is invalid or unsupported", 503)


def mktime_day(year: int, month: int, day: int) -> int:
    # The client calls _mktime64 at 09:00 JST, which is midnight UTC, so a valid result is a whole day.
    tm_year = year - TM_YEAR_BASE
    carry, tm_mon = divmod(month - 1, MONTHS_PER_YEAR)
    if tm_year in MKTIME_TM_YEARS and tm_year + carry in MKTIME_TM_YEARS:
        number = date(TM_YEAR_BASE + tm_year + carry, tm_mon + 1, 1).toordinal() - 1 + day - 1
        if 0 <= (number - UNIX_EPOCH_DAY) * SECONDS_PER_DAY <= LOCALTIME_MAX_SECONDS:
            return number
    # A failed _mktime64 returns -1, which the day division truncates to the 1970-01-01 day number.
    return UNIX_EPOCH_DAY


def _client_clock(now: datetime) -> tuple[datetime, int]:
    try:
        local = instant(now).astimezone(JST)
    except (OverflowError, ValueError, TypeError) as error:
        raise _clock_invalid() from error
    day = mktime_day(local.year, local.month, local.day)
    # A failed _mktime64 collapses to the 1970 day number, which no business date may take silently.
    if day != local.toordinal() - 1:
        raise _clock_invalid()
    return local, day


def wall_clock(now: datetime) -> datetime:
    return _client_clock(now)[0]


def wall_text(now: datetime) -> str:
    return wall_clock(now).strftime(NATIVE_DATETIME)


def civil_date(now: datetime) -> date:
    return wall_clock(now).date()


def business_date(now: datetime) -> date:
    return (wall_clock(now) - BUSINESS_DAY_START).date()


def civil_day(now: datetime) -> int:
    return _client_clock(now)[1]


def business_day(now: datetime) -> int:
    local, civil = _client_clock(now)
    return civil - 1 if local.hour < BUSINESS_DAY_START_HOUR else civil


def month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def next_month(day: date) -> date:
    carry, tm_mon = divmod(day.month, MONTHS_PER_YEAR)
    return date(day.year + carry, tm_mon + 1, 1)


def month_start(day: date) -> datetime:
    return datetime(day.year, day.month, 1, BUSINESS_DAY_START_HOUR, tzinfo=JST)


def business_month(now: datetime) -> str:
    return month_key(business_date(now))


def new_marker() -> dict:
    return dict(MARKER)


def read_marker(state: dict) -> dict | None:
    if not isinstance(state, dict):
        raise _marker_invalid()
    if MARKER_KEY not in state:
        return None
    marker = state[MARKER_KEY]
    if (not isinstance(marker, dict) or marker.keys() != MARKER.keys()
            or any(type(marker[name]) is not type(value) or marker[name] != value for name, value in MARKER.items())):
        raise _marker_invalid()
    return dict(marker)


def describe(state: dict) -> dict:
    try:
        marker = read_marker(state)
    except ProtocolError as error:
        return {"status": INVALID, "marker": None, "error": error.code}
    return {"status": LEGACY if marker is None else MARKED, "marker": marker, "error": None}


def status() -> dict:
    return {"utc_offset_minutes": UTC_OFFSET_MINUTES, "naive_clock_input": "JST server wall clock",
            "wall_text": NATIVE_DATETIME, "business_day_start_hour": BUSINESS_DAY_START_HOUR,
            "fields": dict(FIELD_CALENDARS), "marker": new_marker(),
            "legacy_state": "Unmarked saves keep their host-calendar dates and are reported, never rewritten"}
