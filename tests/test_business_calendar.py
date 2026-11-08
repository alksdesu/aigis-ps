import ast
from copy import deepcopy
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
import re
import time

import pytest

import aigis_ps
from aigis_ps import business_calendar
from aigis_ps.business_calendar import (
    BUSINESS_DAY_START_HOUR, CALENDAR_MONTH_TEXT, DAY_TEXT, FIELD_CALENDARS, INVALID, LEGACY, MARKED, MARKER,
    MARKER_KEY, MONTH_TEXT, MONTHS_PER_YEAR, NATIVE_DATETIME, NATIVE_MINUTES_TEXT, NATIVE_SECONDS_TEXT, business_date,
    business_day, business_month, civil_date, civil_day, describe, mktime_day, month_key, new_marker, next_month,
    read_marker, status, wall_clock, wall_text,
)
from aigis_ps.energy_clock import JST, NEW_ORIGIN, SECONDS_PER_DAY, UNIX_EPOCH_DAY, UTC_OFFSET_MINUTES, energy_seconds
from aigis_ps.errors import ProtocolError
from aigis_ps.native_calendar import native_day
from aigis_ps.native_clock import native_clock, parse_native_date


INSTANT = datetime(2026, 10, 31, 15, 5, tzinfo=timezone.utc)
INSTANT_TEXT = "2026/11/01 00:05:00"
OFFSET_MINUTES = (-600, 0, 480, 540, 840)
# UCRT localtime stops early on 3001-01-19 UTC, so that JST date is the last day the client constructor accepts.
FIRST_CLIENT_DAY, LAST_CLIENT_DAY = date(1970, 1, 1), date(3001, 1, 19)
CLOCK_INVALID = ("business_clock_invalid", 503)
CLOCK_READERS = (wall_clock, wall_text, civil_date, business_date, civil_day, business_day, business_month)
MARKER_INVALID = "business_calendar_invalid"
SOURCE = Path(aigis_ps.__file__).resolve().parent
TIME_MODULE = time.__name__
NOW_OWNERS = frozenset(("datetime", "datetime.datetime"))
TODAY_OWNERS = NOW_OWNERS | {"date", "datetime.date"}
# Naive UTC results would be read back as the JST wall clock.
NAIVE_UTC_READERS = frozenset(("utcnow", "utcfromtimestamp"))
HOST_TIME_CALLS = frozenset(("localtime", "mktime", "ctime", "strftime", "tzset", "asctime"))
HOST_TIME_VALUES = frozenset(("timezone", "altzone", "tzname", "daylight"))
HOST_TIME_NAMES = HOST_TIME_CALLS | HOST_TIME_VALUES
CALENDAR_TEXTS = (DAY_TEXT, MONTH_TEXT, CALENDAR_MONTH_TEXT)
NATIVE_TEXTS = (NATIVE_SECONDS_TEXT, NATIVE_MINUTES_TEXT)
SHARED_TEXTS = CALENDAR_TEXTS + NATIVE_TEXTS
CALENDAR_OWNER = Path(business_calendar.__file__).name
# A respelling may add, drop or uncapture groups, so patterns are compared with their parentheses removed.
GROUPING = re.compile(r"\(\?:|[()]")


def day_number(value: date) -> int:
    return value.toordinal() - 1


def offset(minutes: int) -> timezone:
    return timezone(timedelta(minutes=minutes))


def refused(callback, expected=CLOCK_INVALID):
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert (caught.value.code, caught.value.status) == expected


def host_calendar(*arguments):
    raise AssertionError("Business dates must not read the host calendar")


class HostGuardedInstant(datetime):
    # A zone-less astimezone() reads the host zone inside C, out of reach of the time module patches.
    def astimezone(self, tz=None):
        if tz is None:
            host_calendar()
        return super().astimezone(tz)


def guard_host_calendar(monkeypatch):
    for name in ("mktime", "localtime", "gmtime"):
        monkeypatch.setattr(time, name, host_calendar)


def omits_zone(call: ast.Call, position: int) -> bool:
    # A None zone reads the host zone exactly like an omitted one.
    zones = (*call.args[position:position + 1], *(keyword.value for keyword in call.keywords if keyword.arg == "tz"))
    return all(isinstance(zone, ast.Constant) and zone.value is None for zone in zones)


def call_reads_host_zone(call: ast.Call) -> bool:
    if not isinstance(call.func, ast.Attribute):
        return False
    owner, name = ast.unparse(call.func.value), call.func.attr
    return ((name == "astimezone" and omits_zone(call, 0))
            or (name == "now" and owner in NOW_OWNERS and omits_zone(call, 0))
            or (name == "today" and owner in TODAY_OWNERS)
            or name in NAIVE_UTC_READERS
            or (name == "fromtimestamp" and omits_zone(call, 1))
            or (owner == TIME_MODULE and name in HOST_TIME_CALLS))


def reads_host_zone(node: ast.AST, callees: frozenset[ast.AST]) -> bool:
    if isinstance(node, ast.ImportFrom):
        return node.module == TIME_MODULE and any(alias.name in HOST_TIME_NAMES for alias in node.names)
    if isinstance(node, ast.Call):
        return call_reads_host_zone(node)
    if not isinstance(node, ast.Attribute):
        return False
    if ast.unparse(node.value) == TIME_MODULE and node.attr in HOST_TIME_VALUES:
        return True
    # A clock passed or stored uncalled may later run without a zone, so it counts like a call without arguments.
    return node not in callees and call_reads_host_zone(ast.Call(func=node, args=[], keywords=[]))


def host_zone_reads(source: str | bytes, filename: str = "<snippet>") -> list[int]:
    nodes = tuple(ast.walk(ast.parse(source, filename)))
    callees = frozenset(node.func for node in nodes if isinstance(node, ast.Call))
    return sorted(node.lineno for node in nodes if reads_host_zone(node, callees))


def relative_imports(source: str | bytes, filename: str = "<snippet>") -> frozenset[str]:
    # Imports inside functions count too: they load their module on the first call instead of at import.
    return frozenset(alias.name if node.module is None else node.module.partition(".")[0]
                     for node in ast.walk(ast.parse(source, filename))
                     if isinstance(node, ast.ImportFrom) and node.level for alias in node.names)


def import_closure(module: str) -> frozenset[str]:
    loaded, pending = set(), [module]
    while pending:
        path = SOURCE / f"{pending.pop()}.py"
        imported = relative_imports(path.read_bytes(), str(path))
        # `from . import __version__` reads the package initializer, not a sibling module.
        for name in {name for name in imported if (SOURCE / f"{name}.py").is_file()} - loaded:
            loaded.add(name)
            pending.append(name)
    return frozenset(loaded)


def ungrouped(pattern: str) -> str:
    return GROUPING.sub("", pattern)


def calendar_text_spellings(source: str | bytes, filename: str = "<snippet>") -> list[int]:
    shared = {ungrouped(text.pattern) for text in SHARED_TEXTS}
    return sorted(node.lineno for node in ast.walk(ast.parse(source, filename))
                  if isinstance(node, ast.Constant) and type(node.value) is str and ungrouped(node.value) in shared)


@pytest.mark.parametrize("minutes", OFFSET_MINUTES)
def test_one_instant_gives_the_same_jst_business_dates_in_every_offset(minutes):
    moment = INSTANT.astimezone(offset(minutes))
    assert wall_clock(moment) == INSTANT and wall_clock(moment).utcoffset() == JST.utcoffset(None)
    assert wall_text(moment) == INSTANT_TEXT
    assert (civil_date(moment), business_date(moment)) == (date(2026, 11, 1), date(2026, 10, 31))
    assert (civil_day(moment), business_day(moment)) == (day_number(date(2026, 11, 1)), day_number(date(2026, 10, 31)))
    assert business_month(moment) == "2026-10"


def test_naive_clocks_are_read_as_the_jst_wall_clock():
    naive = datetime(2026, 11, 1, 0, 5)
    assert wall_clock(naive) == naive.replace(tzinfo=JST) == INSTANT
    assert (wall_text(naive), civil_day(naive), business_day(naive)) == (
        INSTANT_TEXT, civil_day(INSTANT), business_day(INSTANT))


@pytest.mark.parametrize("clock, civil, business", [
    ((10, 31, 23, 59, 59), (10, 31), (10, 31)),
    ((11, 1, 0, 0, 0), (11, 1), (10, 31)),
    ((11, 1, 0, 59, 59), (11, 1), (10, 31)),
    ((11, 1, 1, 0, 0), (11, 1), (10, 31)),
    ((11, 1, 3, 59, 59), (11, 1), (10, 31)),
    ((11, 1, 4, 0, 0), (11, 1), (11, 1)),
])
def test_the_civil_day_turns_at_midnight_and_the_business_day_at_four(clock, civil, business):
    moment = datetime(2026, *clock, tzinfo=JST)
    assert (civil_date(moment), business_date(moment)) == (date(2026, *civil), date(2026, *business))
    assert (civil_day(moment), business_day(moment)) == (day_number(date(2026, *civil)),
                                                         day_number(date(2026, *business)))


@pytest.mark.parametrize("day, key", [
    (date(2026, 11, 30), "2026-11"), (date(2026, 1, 1), "2026-01"), (date.min, "0001-01"), (date.max, "9999-12"),
])
def test_month_keys_are_four_digit_years_and_two_digit_months(day, key):
    assert month_key(day) == key
    assert MONTH_TEXT.fullmatch(key).groups() == (f"{day.year:04d}", f"{day.month:02d}")
    assert CALENDAR_MONTH_TEXT.fullmatch(key) and DAY_TEXT.fullmatch(day.isoformat())


@pytest.mark.parametrize("text, day, month, calendar_month", [
    ("2026-11-06", True, False, False),
    ("2026-02-30", True, False, False),
    ("2026-11", False, True, True),
    ("2026-13", False, True, False),
    ("2026-00", False, True, False),
    ("2026-11-6", False, False, False),
    ("2026-1", False, False, False),
    ("2026-11-06\n", False, False, False),
    ("2026-11\n", False, False, False),
    ("+2026-11", False, False, False),
    ("2026/11", False, False, False),
    ("\uff12\uff10\uff12\uff16-11", False, False, False),
])
def test_only_the_calendar_month_text_checks_the_month_range(text, day, month, calendar_month):
    assert tuple(pattern.fullmatch(text) is not None for pattern in CALENDAR_TEXTS) == (day, month, calendar_month)


@pytest.mark.parametrize("text, seconds, minutes", [
    ("2026/11/01 04:05:06", True, True),
    ("2026-11-01 04:05:06", True, True),
    ("2026/1/1 04:05:06", True, True),
    ("2026/11/01 24:00:00", True, True),
    ("2026/11/01 4:05:06", False, True),
    ("2026/11/01 04:05", False, True),
    ("2026/11/01 4:05", False, True),
    ("2026/11-01 04:05:06", False, False),
    ("2026/11/01", False, False),
    ("2026/011/01 04:05:06", False, False),
    ("26/11/01 04:05:06", False, False),
    ("2026/11/01 004:05:06", False, False),
    ("2026/11/01 04:5:06", False, False),
    ("2026/11/01 04:05:6", False, False),
    ("2026/11/01  04:05:06", False, False),
    ("2026/11/01T04:05:06", False, False),
    (" 2026/11/01 04:05:06", False, False),
    ("2026/11/01 04:05:06Z", False, False),
    ("2026/11/01 04:05:06\n", False, False),
    ("\uff12\uff10\uff12\uff16/11/01 04:05:06", False, False),
])
def test_each_native_text_keeps_its_own_strictness(text, seconds, minutes):
    assert tuple(pattern.fullmatch(text) is not None for pattern in NATIVE_TEXTS) == (seconds, minutes)


def test_native_texts_capture_the_parts_their_readers_unpack():
    assert NATIVE_SECONDS_TEXT.fullmatch("2026-1-2 03:04:05").groups() == ("2026", "-", "1", "2", "03", "04", "05")
    assert NATIVE_MINUTES_TEXT.fullmatch("2026/1/2 3:04").groups() == ("2026", "/", "1", "2", "3", "04", None)
    assert NATIVE_MINUTES_TEXT.fullmatch("2026/1/2 3:04:05").groups()[-1] == "05"


@pytest.mark.parametrize("source", [
    r'_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")',
    r'_MONTH = re.compile(r"[0-9]{4}-[0-9]{2}\Z")',
    r'_MONTH = re.compile(r"([0-9]{4})-([0-9]{2})\Z")',
    r'_PERIOD = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])\Z")',
    r'_PERIOD = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])\Z")',
    r'_DATE = re.compile(r"([0-9]{4})([/\-])([0-9]{1,2})\2([0-9]{1,2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})\Z")',
    r'_DATE = re.compile(r"([0-9]{4})([/\-])([0-9]{1,2})\2([0-9]{1,2}) ([0-9]{2}):(?:[0-9]{2}):([0-9]{2})\Z")',
    r'_NATIVE_DATE = re.compile(r"([0-9]{4})([/\-])([0-9]{1,2})\2([0-9]{1,2}) ([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?\Z")',
    r'_NATIVE_DATE = re.compile(r"([0-9]{4})([/\-])([0-9]{1,2})\2([0-9]{1,2}) ([0-9]{1,2}):([0-9]{2})(:([0-9]{2}))?\Z")',
])
def test_the_calendar_text_guard_finds_each_respelled_pattern(source):
    assert calendar_text_spellings(source) == [1]


@pytest.mark.parametrize("source", [
    r'MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"',
    r're.fullmatch(r"month:[0-9]{4}-[0-9]{2}|window:[a-f0-9]{64}", period)',
    r'_DATE = re.compile(r"([0-9]{4})([/\-])([0-9]{1,2})\2([0-9]{1,2})\Z")',
    'MONTH_TEXT.fullmatch(value)',
])
def test_the_calendar_text_guard_allows_other_date_shapes(source):
    assert calendar_text_spellings(source) == []


def test_only_the_calendar_spells_the_shared_date_texts():
    spellings = [f"{path.name}:{line}" for path in sorted(SOURCE.glob("*.py")) if path.name != CALENDAR_OWNER
                 for line in calendar_text_spellings(path.read_bytes(), str(path))]
    assert spellings == []
    assert len(calendar_text_spellings(Path(business_calendar.__file__).read_bytes())) == len(SHARED_TEXTS)


@pytest.mark.parametrize("day, following", [
    (date(2026, 11, 30), date(2026, 12, 1)), (date(2026, 12, 1), date(2027, 1, 1)),
    (date(2026, 12, 31), date(2027, 1, 1)), (date(2028, 2, 29), date(2028, 3, 1)),
    (date(1969, 12, 31), date(1970, 1, 1)), (date.min, date(1, 2, 1)),
])
def test_the_next_month_starts_on_its_first_day_and_december_carries_into_january(day, following):
    assert next_month(day) == following


@pytest.mark.parametrize("year", [1969, 2026, 2028, 3001])
def test_every_next_month_begins_the_day_after_its_month_ends(year):
    for month in range(1, MONTHS_PER_YEAR + 1):
        following = next_month(date(year, month, 1))
        last = following - timedelta(days=1)
        assert following.day == 1 and (last.year, last.month) == (year, month) and next_month(last) == following


def test_no_month_follows_the_last_representable_month():
    with pytest.raises(ValueError):
        next_month(date.max)


@pytest.mark.parametrize("clock, month", [
    ((2026, 12, 1, 3, 59, 59, 999000), "2026-11"),
    ((2026, 12, 1, 4, 0, 0), "2026-12"),
    ((2027, 1, 1, 3, 59, 59, 999000), "2026-12"),
    ((2027, 1, 1, 4, 0, 0), "2027-01"),
])
def test_the_business_month_turns_at_four_on_the_first_in_every_representation(clock, month):
    moment = datetime(*clock, tzinfo=JST)
    for representation in (moment, moment.astimezone(timezone.utc), moment.astimezone(offset(600)),
                           moment.replace(tzinfo=None)):
        assert business_month(representation) == month == month_key(business_date(representation))


def test_leap_days_count_like_any_other_day():
    leap = datetime(2028, 2, 29, 12, tzinfo=JST)
    assert civil_day(leap) == civil_day(leap - timedelta(days=1)) + 1 == day_number(date(2028, 2, 29))
    assert business_day(datetime(2028, 3, 1, BUSINESS_DAY_START_HOUR - 1, tzinfo=JST)) == civil_day(leap)


@pytest.mark.parametrize("minutes", OFFSET_MINUTES)
def test_days_agree_with_the_energy_clock_the_native_calendar_and_the_client_constructor(minutes):
    moment = INSTANT.astimezone(offset(minutes))
    local = moment.astimezone(JST)
    assert civil_day(moment) == energy_seconds(moment) // SECONDS_PER_DAY == native_day(moment)
    assert civil_day(moment) == mktime_day(local.year, local.month, local.day)


@pytest.mark.parametrize("parts, day", [
    ((2026, 11, 1), date(2026, 11, 1)), ((2026, 2, 30), date(2026, 3, 2)), ((2026, 13, 1), date(2027, 1, 1)),
    ((2026, 8, 0), date(2026, 7, 31)), ((2026, 0, 1), date(2025, 12, 1)), ((1969, 14, 1), date(1970, 2, 1)),
    ((1970, 1, 1), FIRST_CLIENT_DAY), ((3001, 1, 19), LAST_CLIENT_DAY),
])
def test_mktime_days_normalize_months_and_days_like_the_client_crt(parts, day):
    assert mktime_day(*parts) == day_number(day)


@pytest.mark.parametrize("parts", [
    (1969, 12, 31), (3001, 1, 20), (3003, 1, 1), (1968, 36, 1), (3002, 13, 1), (1, 1, 1), (2026, 8, 2 ** 31 - 1),
    (2026, 8, -2 ** 31),
])
def test_failed_mktime_days_fall_back_to_the_1970_day_number(parts):
    assert mktime_day(*parts) == UNIX_EPOCH_DAY == day_number(FIRST_CLIENT_DAY)


@pytest.mark.parametrize("moment", [INSTANT, datetime(2026, 11, 1, 3, 59, 59), datetime(3001, 1, 19, 23, 59, 59)])
def test_wall_text_parses_back_to_the_client_clock_of_the_same_instant(moment):
    assert parse_native_date(wall_text(moment)) == native_clock(wall_clock(moment))


def test_the_client_calendar_bounds_every_business_date():
    first = datetime.combine(FIRST_CLIENT_DAY, wall_time(), JST)
    last = datetime.combine(LAST_CLIENT_DAY, wall_time(23, 59, 59), JST)
    assert (civil_day(first), civil_day(last)) == (UNIX_EPOCH_DAY, day_number(LAST_CLIENT_DAY))
    assert wall_text(last) == "3001/01/19 23:59:59"
    # 660F70 only subtracts from the civil day number, so the first client morning still has a business day.
    assert business_day(first) == UNIX_EPOCH_DAY - 1 == day_number(business_date(first))
    assert (business_month(first), business_month(last)) == ("1969-12", "3001-01")
    for outside in (first - timedelta(seconds=1), last + timedelta(seconds=1)):
        for function in CLOCK_READERS:
            refused(lambda: function(outside))


@pytest.mark.parametrize("moment", [
    None, 0, INSTANT.isoformat(), date(2026, 11, 1), datetime.max.replace(tzinfo=timezone.utc),
    datetime(1, 1, 1, tzinfo=offset(600)),
])
def test_non_datetime_or_unrepresentable_clocks_are_refused(moment):
    for function in CLOCK_READERS:
        refused(lambda: function(moment))


def test_business_dates_never_read_the_host_calendar(monkeypatch):
    guard_host_calendar(monkeypatch)
    moment = HostGuardedInstant(2026, 10, 31, 23, 5, tzinfo=offset(480))
    assert moment == INSTANT
    with pytest.raises(AssertionError):
        moment.astimezone()
    assert (wall_text(moment), civil_day(moment), business_day(moment), business_month(moment)) == (
        INSTANT_TEXT, day_number(date(2026, 11, 1)), day_number(date(2026, 10, 31)), "2026-10")


@pytest.mark.parametrize("source", [
    "moment.astimezone()", "moment.astimezone(None)", "datetime.now()", "datetime.now(tz=None)",
    "datetime.datetime.now()", "date.today()", "datetime.today()", "datetime.utcnow()", "datetime.utcfromtimestamp(0)",
    "datetime.fromtimestamp(0)", "datetime.fromtimestamp(0, None)", "date.fromtimestamp(0)",
    "time.localtime()", "time.mktime(parts)", "time.ctime()", "time.strftime(NATIVE_DATETIME)", "time.tzset()",
    "time.asctime()", "time.timezone", "time.altzone", "time.tzname[0]", "time.daylight",
    "from time import localtime", "from time import daylight",
])
def test_the_host_zone_guard_catches_each_host_calendar_read(source):
    assert host_zone_reads(source) == [1]


@pytest.mark.parametrize("source", [
    "moment.astimezone(JST)", "moment.astimezone(tz=JST)", "datetime.now(timezone.utc)", "datetime.now(tz=JST)",
    "datetime.fromtimestamp(0, timezone.utc)", "datetime.fromtimestamp(0, tz=JST)", "clock.now()", "time.gmtime()",
    "time.time()", "time.monotonic()", "datetime.timezone.utc", "from time import monotonic",
])
def test_the_host_zone_guard_allows_explicit_zones_and_zone_free_clocks(source):
    assert host_zone_reads(source) == []


@pytest.mark.parametrize("source", [
    "def load(clock=datetime.now): pass", "def load(today=date.today): pass", "Store(path, clock=datetime.now)",
    "schedule(datetime.datetime.now)", "schedule(date.today)", "field(default_factory=datetime.utcnow)",
    "map(datetime.fromtimestamp, stamps)", "converter = time.localtime",
])
def test_the_host_zone_guard_counts_uncalled_host_clocks_as_reads(source):
    assert host_zone_reads(source) == [1]


@pytest.mark.parametrize("source", [
    "def load(clock=utc_now): pass", "Store(path, clock=utc_now)", "schedule(context.now)", "schedule(clock.today)",
    "converter = time.gmtime", "clock = time.time",
])
def test_the_host_zone_guard_allows_uncalled_zone_free_clocks(source):
    assert host_zone_reads(source) == []


def test_no_server_module_reads_the_host_time_zone():
    reads = [f"{path.name}:{line}" for path in sorted(SOURCE.glob("*.py"))
             for line in host_zone_reads(path.read_bytes(), str(path))]
    assert reads == []


def test_the_calendar_loads_only_clock_primitives_at_import():
    # Modules that read business dates import this calendar at load time, so nothing it loads may load them back.
    assert import_closure(Path(business_calendar.__file__).stem) == {"energy_clock", "errors", "native_numbers"}


@pytest.mark.parametrize("source, modules", [
    ("from .errors import ProtocolError", {"errors"}),
    ("from .energy_clock import JST, instant\nfrom .errors import ProtocolError", {"energy_clock", "errors"}),
    ("from . import errors, energy_clock", {"errors", "energy_clock"}),
    ("from .admin_ui.pages import tools", {"admin_ui"}),
    ("def clock():\n    from .growth_campaign import mktime_day", {"growth_campaign"}),
    ("import json\nfrom datetime import date", set()),
])
def test_the_import_guard_reads_every_relative_import_including_deferred_ones(source, modules):
    assert relative_imports(source) == modules


def test_the_import_closure_skips_names_the_package_initializer_defines():
    assert relative_imports("from . import __version__") == {"__version__"}
    assert "__version__" not in import_closure("app")


def test_new_markers_are_fresh_copies_of_the_jst_marker():
    marker = new_marker()
    assert marker == dict(MARKER) == {"schema_version": 1, "utc_offset_minutes": 540, "origin": "new_local"}
    assert (marker["utc_offset_minutes"], marker["origin"]) == (UTC_OFFSET_MINUTES, NEW_ORIGIN)
    marker["origin"] = "changed"
    assert new_marker() == dict(MARKER)


def test_marked_and_unmarked_saves_are_reported_without_being_rewritten():
    marked = {"bootstrap_schema": 1, MARKER_KEY: new_marker()}
    legacy = {"bootstrap_schema": 1, "created_local": INSTANT_TEXT}
    before = deepcopy((marked, legacy))
    assert read_marker(marked) == MARKER and read_marker(legacy) is None
    assert describe(marked) == {"status": MARKED, "marker": dict(MARKER), "error": None}
    assert describe(legacy) == {"status": LEGACY, "marker": None, "error": None}
    read_marker(marked)["origin"] = "changed"
    describe(marked)["marker"]["origin"] = "changed"
    assert (marked, legacy) == before


@pytest.mark.parametrize("state", [
    None, [], {MARKER_KEY: None}, {MARKER_KEY: [1, 540, "new_local"]}, {MARKER_KEY: {}},
    {MARKER_KEY: {**MARKER, "schema_version": True}}, {MARKER_KEY: {**MARKER, "schema_version": 2}},
    {MARKER_KEY: {**MARKER, "utc_offset_minutes": 480}}, {MARKER_KEY: {**MARKER, "utc_offset_minutes": 540.0}},
    {MARKER_KEY: {**MARKER, "origin": "converted"}}, {MARKER_KEY: {**MARKER, "source_utc_offset_minutes": 480}},
    {MARKER_KEY: {name: value for name, value in MARKER.items() if name != "origin"}},
])
def test_invalid_markers_are_reported_and_refused_by_the_reader(state):
    assert describe(state) == {"status": INVALID, "marker": None, "error": MARKER_INVALID}
    refused(lambda: read_marker(state), (MARKER_INVALID, 503))


def test_status_names_each_field_calendar_and_the_marker():
    report = status()
    assert report == {"utc_offset_minutes": 540, "naive_clock_input": "JST server wall clock",
                      "wall_text": NATIVE_DATETIME, "business_day_start_hour": BUSINESS_DAY_START_HOUR,
                      "fields": dict(FIELD_CALENDARS), "marker": dict(MARKER),
                      "legacy_state": "Unmarked saves keep their host-calendar dates and are reported, never rewritten"}
    assert set(report["fields"].values()) == {"wall_text", "civil_day", "business_day"}
    report["marker"]["origin"] = "changed"
    assert status()["marker"] == dict(MARKER)
