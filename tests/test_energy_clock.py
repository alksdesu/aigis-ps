import ast
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone, tzinfo
from itertools import product
import math
from pathlib import Path
import struct

import pytest

import aigis_ps
from aigis_ps import energy_clock
from aigis_ps.energy_clock import (CLOCK_SOURCE_REQUIRED, CONVERTED_ORIGIN, ENCODING, JST, MARKER_KEY,
                                   MAX_SOURCE_OFFSET_MINUTES, MILLISECOND, NEW_ORIGIN, PREFIXES, SCHEMA_VERSION,
                                   SECONDS_PER_DAY, UNIX_EPOCH, UNIX_EPOCH_DAY, UTC_OFFSET_MINUTES, anchor_seconds,
                                   convert_state, energy_seconds, instant, mark_new, set_anchor, source_offset_valid,
                                   unix_milliseconds, utc_now, validate_clock)
from aigis_ps.errors import ProtocolError
from aigis_ps.native_numbers import INT32_MAX


NOW = datetime(2026, 11, 1, 0, 23, 45)
SOURCE = Path(aigis_ps.__file__).resolve().parent
CLOCK_OWNER = Path(energy_clock.__file__).name
MICROSECOND = timedelta(microseconds=1)
MILLISECONDS_PER_SECOND = timedelta(seconds=1) // MILLISECOND
MICROSECONDS_PER_MILLISECOND = MILLISECOND // MICROSECOND
DAY_MILLISECONDS = SECONDS_PER_DAY * MILLISECONDS_PER_SECOND
EPOCH_DATE = (UNIX_EPOCH.year, UNIX_EPOCH.month, UNIX_EPOCH.day)
FIRST_INSTANT = datetime.min.replace(tzinfo=timezone.utc)
LAST_INSTANT = datetime.max.replace(tzinfo=timezone.utc)
TIMESTAMP_READERS = frozenset(("fromtimestamp", "utcfromtimestamp"))
EPOCH_NAME = "UNIX_EPOCH"


def float32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def legacy_anchor(target, wall_time, offset, prefix="Charisma"):
    source_zone = timezone(timedelta(minutes=offset))
    morning = wall_time.replace(hour=9, minute=0, second=0, microsecond=0, tzinfo=source_zone)
    delta = morning - UNIX_EPOCH
    elapsed = delta.days * SECONDS_PER_DAY + delta.seconds
    whole_days = abs(elapsed) // SECONDS_PER_DAY * (-1 if elapsed < 0 else 1)
    target[prefix + "ChangeDate"] = whole_days + UNIX_EPOCH_DAY
    target[prefix + "ChangeTime"] = float32((wall_time.hour * 3600 + wall_time.minute * 60 + wall_time.second)
                                            / SECONDS_PER_DAY)
    return wall_time.replace(tzinfo=source_zone)


def state():
    return {"bootstrap_schema": 1, "tables": {"game_param": [{
        "CharismaChangeDate": 739920, "CharismaChangeTime": 0.5,
        "ActionPointChangeDate": 739919, "ActionPointChangeTime": 0.25,
        "Money": 123, "Experience": 456, "NowCharisma": 78, "NowActionPoint": 9,
        "OtherDate": 739000, "metadata": {"values": [1]}}],
        "crystals": [{"FreeFlag": True, "Date": 739800, "Num": 5}],
        "quest_records": [{"FirstDefeatDate": 700000}],
        "presents": [{"Description": "kept"}]},
        "trade_receipts": {"receipts": [{"response": "unchanged", "client_time": -1}]},
        "quest_state": {"active": {"client_time": 123}}, "custom": {"values": [2]}}


def row(saved):
    return saved["tables"]["game_param"][0]


def fails(code, action, status=None):
    with pytest.raises(ProtocolError) as error:
        action()
    assert error.value.code == code
    if status is not None:
        assert error.value.status == status


def spelled_milliseconds(value: datetime) -> int:
    # Saved ledgers and receipts hold this sum, so the shared conversion must reproduce it for either sign.
    delta = value.astimezone(timezone.utc) - UNIX_EPOCH
    return (delta.days * DAY_MILLISECONDS + delta.seconds * MILLISECONDS_PER_SECOND
            + delta.microseconds // MICROSECONDS_PER_MILLISECOND)


def epoch_days(day: date) -> int:
    return day.toordinal() - 1 - UNIX_EPOCH_DAY


def day_start_milliseconds(day: date) -> int:
    return epoch_days(day) * DAY_MILLISECONDS


def callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    return call.func.attr if isinstance(call.func, ast.Attribute) else None


def constants(nodes) -> tuple:
    return tuple(node.value if isinstance(node, ast.Constant) else None for node in nodes)


def names_unix_epoch(node: ast.AST) -> bool:
    return ((isinstance(node, ast.Name) and node.id == EPOCH_NAME)
            or (isinstance(node, ast.Attribute) and node.attr == EPOCH_NAME))


def factors(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        return [*factors(node.left), *factors(node.right)]
    return [node]


def integer_product(node: ast.BinOp) -> int:
    return math.prod(factor.value for factor in factors(node)
                     if isinstance(factor, ast.Constant) and type(factor.value) is int)


def spells_unix_clock(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return type(node.value) is int and node.value == DAY_MILLISECONDS
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Mult):
            return integer_product(node) == DAY_MILLISECONDS
        return isinstance(node.op, ast.Sub) and names_unix_epoch(node.right)
    if not isinstance(node, ast.Call):
        return False
    name = callee(node)
    return ((name == "datetime" and constants(node.args[:len(EPOCH_DATE)]) == EPOCH_DATE)
            or (name in TIMESTAMP_READERS and constants(node.args[:1]) == (0,)))


def unix_clock_spellings(source: str | bytes, filename: str = "<snippet>") -> list[int]:
    return sorted({node.lineno for node in ast.walk(ast.parse(source, filename)) if spells_unix_clock(node)})


def test_explicit_jst_and_utc_clock():
    assert JST.utcoffset(None) == timedelta(minutes=540)
    assert ENCODING == "native_day_fraction_f32_seconds_v1"
    before = datetime.now(timezone.utc)
    current = utc_now()
    after = datetime.now(timezone.utc)
    assert current.utcoffset() == timedelta(0) and before <= current <= after


def test_naive_instants_are_jst_wall_time_independent_of_host(monkeypatch):
    monkeypatch.setenv("TZ", "Pacific/Honolulu")
    expected = NOW.replace(tzinfo=JST)
    assert instant(NOW) == expected and instant(NOW).tzinfo is JST
    assert energy_seconds(NOW) == energy_seconds(expected)
    assert unix_milliseconds(NOW) == unix_milliseconds(expected)


@pytest.mark.parametrize("minutes", [-840, -600, -90, 0, 330, 480, 540, 840])
def test_aware_instants_keep_their_actual_time(minutes):
    original = NOW.replace(tzinfo=JST)
    source = original.astimezone(timezone(timedelta(minutes=minutes)))
    assert instant(source) is source
    assert energy_seconds(source) == energy_seconds(original)


@pytest.mark.parametrize("utc,local", [
    (datetime(2026, 10, 31, 14, 59, 59), datetime(2026, 10, 31, 23, 59, 59)),
    (datetime(2026, 10, 31, 15), datetime(2026, 11, 1)),
    (datetime(2024, 2, 28, 15), datetime(2024, 2, 29)),
    (datetime(2025, 12, 31, 15), datetime(2026, 1, 1)),
])
def test_jst_calendar_uses_native_ordinal_minus_one(utc, local):
    expected = (local.toordinal() - 1) * SECONDS_PER_DAY + local.hour * 3600 + local.minute * 60 + local.second
    assert energy_seconds(utc.replace(tzinfo=timezone.utc)) == expected


def test_microseconds_never_advance_energy_seconds():
    assert energy_seconds(NOW.replace(microsecond=999999)) == energy_seconds(NOW)
    assert energy_seconds(datetime(1, 1, 1)) == 0
    assert energy_seconds(datetime(1970, 1, 1)) == (datetime(1970, 1, 1).toordinal() - 1) * SECONDS_PER_DAY


@pytest.mark.parametrize("value", [None, 0, True, "2026-11-01", object()])
def test_non_datetime_arguments_are_rejected(value):
    fails("energy_clock_argument", lambda: instant(value))
    fails("energy_clock_argument", lambda: energy_seconds(value))
    fails("energy_clock_argument", lambda: unix_milliseconds(value))


class UndefinedOffset(tzinfo):
    def utcoffset(self, value):
        return None


class InvalidOffset(tzinfo):
    def utcoffset(self, value):
        return timedelta(days=1)


def test_undefined_offset_is_naive_and_invalid_offset_is_explicit():
    assert instant(NOW.replace(tzinfo=UndefinedOffset())).tzinfo is JST
    fails("energy_clock_argument", lambda: instant(NOW.replace(tzinfo=InvalidOffset())))


@pytest.mark.parametrize("value", [datetime.max.replace(tzinfo=timezone.utc),
                                   datetime.min.replace(tzinfo=timezone(timedelta(hours=14)))])
def test_jst_calendar_overflow_is_rejected(value):
    fails("energy_clock_argument", lambda: energy_seconds(value))


def test_the_unix_epoch_is_utc_midnight_of_the_native_epoch_day():
    assert UNIX_EPOCH == datetime(1970, 1, 1, tzinfo=timezone.utc) and UNIX_EPOCH.utcoffset() == timedelta(0)
    assert UNIX_EPOCH.toordinal() - 1 == UNIX_EPOCH_DAY and MILLISECOND == timedelta(milliseconds=1)


@pytest.mark.parametrize("moment, expected", [
    (UNIX_EPOCH, 0),
    (UNIX_EPOCH - MILLISECOND, -1),
    (UNIX_EPOCH + MILLISECOND, 1),
    (UNIX_EPOCH - MICROSECOND, -1),
    (UNIX_EPOCH + MILLISECOND - MICROSECOND, 0),
    (UNIX_EPOCH - MILLISECOND - MICROSECOND, -2),
    (UNIX_EPOCH + 5 * MILLISECOND - MICROSECOND, 4),
    (datetime(2026, 11, 1, 8, 59, 59, 999999, tzinfo=JST), day_start_milliseconds(date(2026, 11, 1)) - 1),
    (LAST_INSTANT, day_start_milliseconds(LAST_INSTANT.date()) + DAY_MILLISECONDS - 1),
    (FIRST_INSTANT, day_start_milliseconds(FIRST_INSTANT.date())),
])
def test_unix_milliseconds_floor_toward_the_earlier_millisecond(moment, expected):
    assert unix_milliseconds(moment) == expected == spelled_milliseconds(moment)


@pytest.mark.parametrize("days, seconds, microseconds", product(
    (epoch_days(FIRST_INSTANT.date()), -1, 0, 1, epoch_days(date(2026, 11, 1)), epoch_days(LAST_INSTANT.date())),
    (0, 1, SECONDS_PER_DAY - 1),
    (0, 1, MICROSECONDS_PER_MILLISECOND - 1, MICROSECONDS_PER_MILLISECOND, LAST_INSTANT.microsecond)))
def test_unix_milliseconds_reproduce_the_saved_sum_on_both_sides_of_the_epoch(days, seconds, microseconds):
    moment = UNIX_EPOCH + timedelta(days=days, seconds=seconds, microseconds=microseconds)
    assert unix_milliseconds(moment) == spelled_milliseconds(moment)


@pytest.mark.parametrize("minutes", [-840, -600, 0, 540, 840])
def test_unix_milliseconds_name_the_instant_not_its_offset(minutes):
    moment = NOW.replace(microsecond=678901, tzinfo=JST)
    assert unix_milliseconds(moment.astimezone(timezone(timedelta(minutes=minutes)))) == unix_milliseconds(moment)


@pytest.mark.parametrize("moment", [datetime.min.replace(tzinfo=JST),
                                    datetime.max.replace(tzinfo=timezone(timedelta(hours=-14)))])
def test_instants_outside_the_utc_calendar_have_no_unix_milliseconds(moment):
    with pytest.raises(OverflowError):
        unix_milliseconds(moment)


@pytest.mark.parametrize("source", [
    "stamp = delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000",
    "stamp = delta.days * 86400 * 1000 + delta.seconds * 1000 + delta.microseconds // 1000",
    "DAY = 24 * (60 * 60) * 1000",
    "_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)",
    "EPOCH = datetime.datetime(1970, 1, 1)",
    "start = datetime.fromtimestamp(0, timezone.utc)",
    "stamp = (now - UNIX_EPOCH) // MILLISECOND",
    "stamp = (now - energy_clock.UNIX_EPOCH) // MILLISECOND",
])
def test_the_unix_clock_guard_finds_each_spelling(source):
    assert unix_clock_spellings(source) == [1]


@pytest.mark.parametrize("source", [
    "DAY_MILLISECONDS = 86400000.0",
    "elapsed = delta.days * 86400 + delta.seconds",
    "moment = UNIX_EPOCH + timedelta(milliseconds=value)",
    "stamp = unix_milliseconds(now)",
    "NATIVE_EPOCH = datetime(1, 1, 1, tzinfo=JST)",
    "seconds = ((utc.toordinal() - 1) * 86400 + utc.second) * 1000 + utc.microsecond // 1000",
])
def test_the_unix_clock_guard_allows_other_epochs_and_the_shared_conversion(source):
    assert unix_clock_spellings(source) == []


def test_only_the_energy_clock_spells_the_unix_epoch_or_its_millisecond_sum():
    spellings = [f"{path.name}:{line}" for path in sorted(SOURCE.glob("*.py")) if path.name != CLOCK_OWNER
                 for line in unix_clock_spellings(path.read_bytes(), str(path))]
    assert spellings == []
    assert unix_clock_spellings(Path(energy_clock.__file__).read_bytes())


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("seconds", [0, 1, 179, 180, 43200, 86398, 86399, 86400, 86401,
                                     INT32_MAX * SECONDS_PER_DAY + 86399])
def test_anchor_encoding_round_trips_integer_seconds(prefix, seconds):
    target = {"retained": [1]}
    assert set_anchor(target, seconds, prefix) is None
    day, second = divmod(seconds, SECONDS_PER_DAY)
    assert target[prefix + "ChangeDate"] == day
    assert target[prefix + "ChangeTime"] == float32(second / SECONDS_PER_DAY)
    assert type(target[prefix + "ChangeTime"]) is float
    assert anchor_seconds(target, prefix) == seconds
    assert target["retained"] == [1]


def test_all_whole_seconds_in_one_day_survive_float32_storage():
    target = {}
    for second in range(SECONDS_PER_DAY):
        set_anchor(target, second)
        assert anchor_seconds(target) == second


def test_anchor_reader_quantizes_to_float32_before_nearest_second():
    target = {"CharismaChangeDate": 100, "CharismaChangeTime": 0.123456789}
    before = deepcopy(target)
    assert anchor_seconds(target) == 100 * SECONDS_PER_DAY + round(float32(0.123456789) * SECONDS_PER_DAY)
    assert target == before
    target["CharismaChangeTime"] = float32(0.99999994)
    assert anchor_seconds(target) == 101 * SECONDS_PER_DAY


@pytest.mark.parametrize("day", [-1, True, 1.0, None, INT32_MAX + 1])
def test_invalid_anchor_day(day):
    target = {"CharismaChangeDate": day, "CharismaChangeTime": 0.5}
    fails("energy_clock_anchor_invalid", lambda: anchor_seconds(target), 503)


@pytest.mark.parametrize("fraction", [-0.1, True, None, "0.5", math.inf, -math.inf, math.nan,
                                      1, 0.999999999, 10 ** 1000])
def test_invalid_anchor_fraction(fraction):
    target = {"CharismaChangeDate": 1, "CharismaChangeTime": fraction}
    fails("energy_clock_anchor_invalid", lambda: anchor_seconds(target), 503)


@pytest.mark.parametrize("seconds", [-1, True, 1.0, None, (INT32_MAX + 1) * SECONDS_PER_DAY])
def test_invalid_set_does_not_partially_mutate_the_row(seconds):
    target = {"CharismaChangeDate": 20, "CharismaChangeTime": 0.5}
    before = deepcopy(target)
    fails("energy_clock_anchor_invalid", lambda: set_anchor(target, seconds), 503)
    assert target == before


@pytest.mark.parametrize("prefix", ["", "Money", None, True, "CharismaChangeDate"])
def test_unknown_prefix_never_writes_arbitrary_fields(prefix):
    target = {}
    fails("energy_clock_argument", lambda: set_anchor(target, 1, prefix))
    fails("energy_clock_argument", lambda: anchor_seconds(target, prefix))
    assert target == {}


def test_mark_new_sets_both_clocks_and_preserves_all_other_data():
    original = state()
    before = deepcopy(original)
    updated = mark_new(original, NOW)
    assert original == before
    assert updated["energy_clock"] == {"schema_version": 1, "encoding": ENCODING,
                                      "utc_offset_minutes": 540, "origin": "new_local"}
    for prefix in PREFIXES:
        assert anchor_seconds(row(updated), prefix) == energy_seconds(NOW)
    assert validate_clock(updated) is None
    retained = deepcopy(updated)
    retained.pop("energy_clock")
    for prefix in PREFIXES:
        for suffix in ("ChangeDate", "ChangeTime"):
            row(retained)[prefix + suffix] = row(before)[prefix + suffix]
    assert retained == before
    updated["custom"]["values"].append(3)
    row(updated)["metadata"]["values"].append(4)
    assert original == before


@pytest.mark.parametrize("offset", [-840, -330, 0, 330, 480, 540, 840])
def test_conversion_preserves_instants_using_only_explicit_fixed_offset(offset):
    original = state()
    moments = {}
    for prefix, wall_time in zip(PREFIXES, (NOW, NOW - timedelta(days=2, hours=3))):
        moments[prefix] = legacy_anchor(row(original), wall_time, offset, prefix)
    before = deepcopy(original)
    converted = convert_state(original, offset)
    assert original == before
    for prefix in PREFIXES:
        assert anchor_seconds(row(converted), prefix) == energy_seconds(moments[prefix])
    assert converted["energy_clock"] == {"schema_version": 1, "encoding": ENCODING,
        "utc_offset_minutes": 540, "origin": "converted", "source_utc_offset_minutes": offset}
    assert validate_clock(converted) is None
    copied = deepcopy(converted)
    copied.pop("energy_clock")
    for prefix in PREFIXES:
        for suffix in ("ChangeDate", "ChangeTime"):
            row(copied)[prefix + suffix] = row(before)[prefix + suffix]
    assert copied == before
    converted["trade_receipts"]["receipts"][0]["response"] = "changed copy"
    assert original == before


@pytest.mark.parametrize("offset", [-840, -330, 0, 330, 480, 540, 600, 840])
@pytest.mark.parametrize("wall_time", [
    datetime(1900, 3, 1, 12, 34, 56), datetime(1969, 12, 29, 23, 59, 59),
    datetime(1969, 12, 30, 12), datetime(1969, 12, 31, 12), datetime(1970, 1, 1, 12),
    datetime(1970, 1, 2, 12), datetime(1970, 1, 3, 12), datetime(2026, 11, 1, 12),
], ids=["1900", "before_epoch_3", "before_epoch_2", "before_epoch_1",
        "epoch", "after_epoch_1", "after_epoch_2", "modern"])
def test_actual_legacy_producer_dates_convert_or_explicitly_reject_epoch_ambiguity(offset, wall_time):
    original = state()
    moment = legacy_anchor(row(original), wall_time, offset)
    legacy_anchor(row(original), wall_time, offset, "ActionPoint")
    before = deepcopy(original)
    if row(original)["CharismaChangeDate"] == 719162 and offset != 540:
        fails("energy_clock_source_ambiguous", lambda: convert_state(original, offset), 503)
    else:
        converted = convert_state(original, offset)
        local = moment.astimezone(JST)
        expected = (local.toordinal() - 1) * 86400 + local.hour * 3600 + local.minute * 60 + local.second
        assert anchor_seconds(row(converted)) == expected
        assert anchor_seconds(row(converted), "ActionPoint") == expected
    assert original == before


def test_legacy_utc14_counterexample_requires_restoring_the_lost_civil_day():
    original = state()
    legacy_anchor(row(original), datetime(2026, 11, 1, 12), 840)
    assert row(original)["CharismaChangeDate"] == datetime(2026, 10, 31).toordinal() - 1
    converted = convert_state(original, 840)
    assert anchor_seconds(row(converted)) == energy_seconds(datetime(2026, 11, 1, 7))


@pytest.mark.parametrize("offset", [-840, 0, 480, 600, 840])
def test_legacy_epoch_day_is_ambiguous_for_two_distinct_source_dates(offset):
    original = state()
    first = datetime(1970, 1, 1, 12)
    second = first + timedelta(days=1 if offset > 540 else -1)
    legacy_anchor(row(original), first, offset)
    legacy_anchor(row(original), second, offset, "ActionPoint")
    assert row(original)["CharismaChangeDate"] == row(original)["ActionPointChangeDate"] == 719162
    assert row(original)["CharismaChangeTime"] == row(original)["ActionPointChangeTime"]
    fails("energy_clock_source_ambiguous", lambda: convert_state(original, offset), 503)


def test_legacy_utc9_is_identical_to_civil_ordinals_including_the_epoch():
    original = state()
    legacy_anchor(row(original), datetime(1970, 1, 1, 12), 540)
    legacy_anchor(row(original), datetime(1900, 1, 1, 3), 540, "ActionPoint")
    converted = convert_state(original, 540)
    for prefix in PREFIXES:
        assert anchor_seconds(row(converted), prefix) == anchor_seconds(row(original), prefix)


def test_conversion_crosses_jst_midnight_and_preserves_elapsed_seconds():
    original = state()
    day = NOW.toordinal() - 1
    old_clock = day * SECONDS_PER_DAY + 23 * 3600 + 59 * 60
    set_anchor(row(original), old_clock)
    converted = convert_state(original, 480)
    assert row(converted)["CharismaChangeDate"] == day + 1
    assert anchor_seconds(row(converted)) == old_clock + 3600
    source_now = datetime(2026, 11, 2, 0, 2, tzinfo=timezone(timedelta(hours=8)))
    assert energy_seconds(source_now) - anchor_seconds(row(converted)) == 180


@pytest.mark.parametrize("offset", [None, True, 1.0, "480", -841, 841])
def test_unmarked_conversion_requires_an_explicit_bounded_integer_offset(offset):
    original = state()
    before = deepcopy(original)
    fails("energy_clock_source_invalid", lambda: convert_state(original, offset))
    assert original == before
    with pytest.raises(ProtocolError) as caught:
        convert_state(original, offset)
    assert str(caught.value) == "Source UTC offset must be an explicit integer from -840 to 840 minutes"


@pytest.mark.parametrize("offset, valid", [
    (-841, False), (-840, True), (-1, True), (0, True), (540, True), (840, True), (841, False),
    (True, False), (540.0, False), ("540", False), (None, False),
])
def test_source_offsets_are_integers_within_fourteen_hours_of_utc(offset, valid):
    assert source_offset_valid(offset) is valid


def test_marker_spellings_stay_readable_by_old_saves():
    assert (MARKER_KEY, SCHEMA_VERSION, NEW_ORIGIN, CONVERTED_ORIGIN) == ("energy_clock", 1, "new_local", "converted")
    assert (UTC_OFFSET_MINUTES, MAX_SOURCE_OFFSET_MINUTES) == (540, 840)
    assert JST.utcoffset(None) == timedelta(minutes=UTC_OFFSET_MINUTES)


@pytest.mark.parametrize("prefix", PREFIXES)
def test_zero_unmarked_anchor_is_not_assumed_to_mean_current_time(prefix):
    original = state()
    set_anchor(row(original), 0, prefix)
    before = deepcopy(original)
    fails(CLOCK_SOURCE_REQUIRED, lambda: convert_state(original, 480), 503)
    assert original == before


@pytest.mark.parametrize("seconds,offset", [(1, 840), (INT32_MAX * SECONDS_PER_DAY + 86399, -840)])
def test_conversion_underflow_or_overflow_leaves_both_anchors_unchanged(seconds, offset):
    original = state()
    set_anchor(row(original), seconds, "ActionPoint")
    before = deepcopy(original)
    fails("energy_clock_anchor_invalid", lambda: convert_state(original, offset), 503)
    assert original == before


def test_missing_marker_is_distinct_from_invalid_marker():
    original = state()
    # Migration status and both restore paths match this published code.
    assert CLOCK_SOURCE_REQUIRED == "energy_clock_source_required"
    fails(CLOCK_SOURCE_REQUIRED, lambda: validate_clock(original), 503)
    original["energy_clock"] = None
    fails("energy_clock_invalid", lambda: validate_clock(original), 503)
    fails("energy_clock_invalid", lambda: convert_state(original, 480), 503)


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"schema_version": 2}, {"encoding": "unknown"},
    {"utc_offset_minutes": 480}, {"utc_offset_minutes": 540.0}, {"origin": "legacy"},
    {"extra": 1}, {"source_utc_offset_minutes": 480},
])
def test_invalid_new_marker_is_never_silently_repaired(change):
    marked = mark_new(state(), NOW)
    marked["energy_clock"].update(change)
    before = deepcopy(marked)
    fails("energy_clock_invalid", lambda: validate_clock(marked), 503)
    fails("energy_clock_invalid", lambda: convert_state(marked, 480), 503)
    assert marked == before


@pytest.mark.parametrize("source", [None, True, "480", 480.0, -841, 841])
def test_converted_marker_requires_valid_source_metadata(source):
    marked = convert_state(state(), 480)
    marked["energy_clock"]["source_utc_offset_minutes"] = source
    fails("energy_clock_invalid", lambda: validate_clock(marked), 503)


@pytest.mark.parametrize("converted", [False, True])
def test_valid_marked_conversion_is_an_exact_deep_copy_noop(converted):
    marked = convert_state(state(), 480) if converted else mark_new(state(), NOW)
    before = deepcopy(marked)
    for offset in (None, True, -900, 0, 480, 540):
        result = convert_state(marked, offset)
        assert result == marked and result is not marked
        result["custom"]["values"].append(7)
        assert marked == before


@pytest.mark.parametrize("prefix", PREFIXES)
def test_valid_marker_does_not_bypass_anchor_validation(prefix):
    marked = mark_new(state(), NOW)
    row(marked)[prefix + "ChangeTime"] = math.inf
    fails("energy_clock_anchor_invalid", lambda: validate_clock(marked), 503)
    fails("energy_clock_anchor_invalid", lambda: convert_state(marked, 480), 503)


@pytest.mark.parametrize("bad", [None, {}, {"bootstrap_schema": True, "tables": {}},
    {"bootstrap_schema": 1, "tables": None}, {"bootstrap_schema": 1, "tables": {"game_param": []}},
    {"bootstrap_schema": 1, "tables": {"game_param": [{}, {}]}},
    {"bootstrap_schema": 1, "tables": {"game_param": [None]}}])
def test_invalid_state_structure_is_not_created_or_repaired(bad):
    before = deepcopy(bad)
    for action in (lambda: mark_new(bad, NOW), lambda: validate_clock(bad), lambda: convert_state(bad, 480)):
        fails("energy_clock_state_invalid", action, 503)
    assert bad == before


def test_mark_new_can_initialize_missing_anchor_fields():
    original = {"bootstrap_schema": 1, "tables": {"game_param": [{"Money": 5}]}}
    marked = mark_new(original, NOW)
    assert validate_clock(marked) is None
    assert original == {"bootstrap_schema": 1, "tables": {"game_param": [{"Money": 5}]}}
