from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

import aigis_ps.business_calendar as business_calendar
import aigis_ps.monthly_capacity as module
from aigis_ps.business_calendar import BUSINESS_DAY_START_HOUR, MONTHS_PER_YEAR, month_key
from aigis_ps.crystal_spending import initialize_spending, read_spending, settle_spending
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.monthly_capacity import (
    BASELINE_UNNEEDED,
    INT32_MAX,
    PERIOD_AHEAD,
    PERIOD_INVALID,
    STAMINA_FIELDS,
    establish_baseline,
    grant_next_capacity,
    initialize_capacity,
    legacy_extensions,
    read_capacity,
    settle_capacity,
)
from test_crystal_spending_ledger import MONTH_TURNS, OUTSIDE_CLIENT_CALENDAR, representations


NOW = datetime(2026, 11, 15, 12, 34, 56, 123000, tzinfo=JST)
CURRENT = "ExtendedMaxCharisma"
NEXT = "ExtendedMaxCharismaNextMonth"
STAMINA = "ExtendedMaxStamina"
NEXT_STAMINA = "ExtendedMaxStaminaNextMonth"


def state(*, initialized=False, now=NOW):
    source = {"bootstrap_schema": 1, "tables": {
        "game_param2": [{"StampCardGrade": 0, "LogInStampCount": 7}],
        "game_param": [{CURRENT: 0, NEXT: 0, STAMINA: 0, NEXT_STAMINA: 0, "NowCharisma": 1000,
                        "Rank": 1, "CharismaChangeDate": 739000, "CharismaChangeTime": 0.5}],
        "crystals": [{"FreeFlag": True, "Num": 17, "Date": 739000}]}, "other": {"keep": [1]}}
    source = initialize_spending(source, now, origin="new_local")
    return initialize_capacity(source) if initialized else source


def configured(current=10, next_value=20, *, now=NOW):
    source = state(initialized=True, now=now)
    source["monthly_capacity"].update(current=current, next=next_value)
    source["tables"]["game_param"][0].update({CURRENT: current, NEXT: next_value})
    return source


def recorder(calls, *, modify=False):
    def callback(candidate, now):
        ledger = candidate.get("monthly_capacity")
        row = candidate["tables"]["game_param"][0]
        calls.append((ledger["period"] if ledger else None, row[CURRENT], row[NEXT], now))
        if modify:
            candidate["other"]["keep"].append(row[CURRENT])
        return candidate
    return callback


def fail(source, callback, code):
    before = deepcopy(source)
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert caught.value.code == code
    assert source == before
    return caught.value


def test_initialize_uses_spending_period_and_exact_instant_without_touching_other_fields():
    source = state()
    before = deepcopy(source)
    updated = initialize_capacity(source)
    assert read_capacity(updated) == {"schema_version": 1, "period": "2026-11", "current": 0, "next": 0,
                                      "last_time": source["crystal_spending"]["last_time"]}
    assert {key: value for key, value in updated.items() if key != "monthly_capacity"} == before
    updated["other"]["keep"].append(2)
    assert source == before


def test_legacy_absent_zero_extension_fields_can_be_initialized():
    source = state()
    for key in (CURRENT, NEXT, STAMINA, NEXT_STAMINA):
        source["tables"]["game_param"][0].pop(key)
    updated = initialize_capacity(source)
    assert [updated["tables"]["game_param"][0][name] for name in (CURRENT, NEXT, STAMINA, NEXT_STAMINA)] == [0] * 4
    assert CURRENT not in source["tables"]["game_param"][0]


@pytest.mark.parametrize("field", [CURRENT, NEXT, STAMINA, NEXT_STAMINA])
def test_nonzero_legacy_extensions_require_explicit_baseline_instead_of_guessing(field):
    source = state()
    source["tables"]["game_param"][0][field] = 1
    fail(source, lambda: initialize_capacity(source), "capacity_baseline_required")


def test_existing_initialize_only_validates_and_is_independent_of_spending_clock(monkeypatch):
    source = configured()
    source.pop("crystal_spending")
    def forbidden(_):
        raise AssertionError("Existing monthly capacity must not infer a new baseline")
    monkeypatch.setattr(module, "read_spending", forbidden)
    updated = initialize_capacity(source)
    assert updated == source and updated is not source
    updated["monthly_capacity"]["current"] = 99
    assert read_capacity(source)["current"] == 10


def test_spending_can_move_to_a_new_month_before_capacity_settlement():
    source = configured()
    target = datetime(2026, 12, 2, 12, tzinfo=JST)
    newer_spending = settle_spending(source, target)
    assert read_capacity(newer_spending)["period"] == "2026-11"
    assert initialize_capacity(newer_spending) == newer_spending
    settled = settle_capacity(newer_spending, target, recorder([]))
    assert read_capacity(settled)["period"] == "2026-12"


def test_read_returns_an_isolated_ledger():
    source = configured()
    ledger = read_capacity(source)
    ledger["current"] = 99
    assert read_capacity(source)["current"] == 10


def test_grant_adds_only_next_month_capacity_and_records_the_instant():
    source = configured()
    before = deepcopy(source)
    updated = grant_next_capacity(source, 40, NOW + timedelta(milliseconds=1))
    expected = deepcopy(source)
    expected["monthly_capacity"].update(next=60, last_time=source["monthly_capacity"]["last_time"] + 1)
    expected["tables"]["game_param"][0][NEXT] = 60
    assert updated == expected
    assert updated["tables"]["game_param"][0]["NowCharisma"] == 1000
    assert source == before


@pytest.mark.parametrize("amount", [0, -1, True, False, "1", 1.0, INT32_MAX + 1])
def test_grant_requires_a_positive_native_integer(amount):
    source = configured()
    fail(source, lambda: grant_next_capacity(source, amount, NOW), "capacity_amount_invalid")


def test_missing_ledger_requires_initialization_before_grant():
    source = state()
    fail(source, lambda: grant_next_capacity(source, 1, NOW), "capacity_baseline_required")


def test_grant_overflow_is_rejected_without_clipping_the_reward():
    source = configured(next_value=INT32_MAX)
    fail(source, lambda: grant_next_capacity(source, 1, NOW), "capacity_overflow")
    source = configured(current=INT32_MAX, next_value=INT32_MAX - 1)
    updated = grant_next_capacity(source, 1, NOW)
    assert read_capacity(updated)["current"] == read_capacity(updated)["next"] == INT32_MAX


@pytest.mark.parametrize("target", [datetime(2026, 12, 1, 4, tzinfo=JST), datetime(2026, 10, 31, 12, tzinfo=JST)])
def test_grant_requires_period_to_be_settled_first(target):
    source = configured()
    fail(source, lambda: grant_next_capacity(source, 1, target), "capacity_refresh_required")


def test_same_month_clock_rollback_is_rejected_for_grant_and_settlement():
    source = configured()
    earlier = NOW - timedelta(milliseconds=1)
    fail(source, lambda: grant_next_capacity(source, 1, earlier), "capacity_clock_backwards")
    fail(source, lambda: settle_capacity(source, earlier, recorder([])), "capacity_clock_backwards")


def test_same_month_settlement_calls_raw_energy_once_with_current_capacity():
    source = configured()
    calls = []
    later = NOW + timedelta(days=2)
    result = settle_capacity(source, later, recorder(calls, modify=True))
    assert calls == [("2026-11", 10, 20, later)]
    assert read_capacity(result) == {**read_capacity(source), "last_time": int(later.timestamp() * 1000)}
    assert result["other"]["keep"] == [1, 10]
    assert source["other"]["keep"] == [1]


def test_month_boundary_settles_old_capacity_before_activating_next():
    source = configured()
    calls = []
    boundary = datetime(2026, 12, 1, 4, tzinfo=JST)
    later = datetime(2026, 12, 20, 12, tzinfo=JST)
    result = settle_capacity(source, later, recorder(calls, modify=True))
    assert calls == [("2026-11", 10, 20, boundary), ("2026-12", 20, 0, later)]
    assert read_capacity(result) == {"schema_version": 1, "period": "2026-12", "current": 20, "next": 0,
                                    "last_time": int(later.timestamp() * 1000)}
    assert result["tables"]["game_param"][0][CURRENT] == 20
    assert result["tables"]["game_param"][0][NEXT] == 0
    assert result["tables"]["game_param"][0]["NowCharisma"] == 1000
    assert result["other"]["keep"] == [1, 10, 20]


def test_exact_boundary_calls_old_then_new_capacity_at_the_same_instant():
    source = configured()
    calls = []
    target = datetime(2026, 12, 1, 4, tzinfo=JST)
    result = settle_capacity(source, target, recorder(calls))
    assert calls == [("2026-11", 10, 20, target), ("2026-12", 20, 0, target)]
    assert read_capacity(result)["current"] == 20


def test_after_another_month_the_bonus_expires_without_deducting_existing_energy():
    source = configured()
    calls = []
    target = datetime(2027, 1, 5, 12, tzinfo=JST)
    result = settle_capacity(source, target, recorder(calls))
    assert calls == [("2026-11", 10, 20, datetime(2026, 12, 1, 4, tzinfo=JST)),
                     ("2026-12", 20, 0, datetime(2027, 1, 1, 4, tzinfo=JST)),
                     ("2027-01", 0, 0, target)]
    assert read_capacity(result)["current"] == read_capacity(result)["next"] == 0
    assert result["tables"]["game_param"][0]["NowCharisma"] == 1000


def test_long_absence_uses_only_two_changed_capacity_boundaries_then_jumps_zero_months():
    source = configured()
    calls = []
    target = datetime(2030, 5, 6, 12, tzinfo=JST)
    result = settle_capacity(source, target, recorder(calls))
    assert calls == [("2026-11", 10, 20, datetime(2026, 12, 1, 4, tzinfo=JST)),
                     ("2026-12", 20, 0, datetime(2027, 1, 1, 4, tzinfo=JST)),
                     ("2030-05", 0, 0, target)]
    assert read_capacity(result)["period"] == "2030-05"


def test_new_grant_in_the_active_month_is_for_the_following_month_only():
    source = settle_capacity(configured(), datetime(2026, 12, 1, 4, tzinfo=JST), recorder([]))
    source = grant_next_capacity(source, 40, datetime(2026, 12, 1, 5, tzinfo=JST))
    calls = []
    target = datetime(2027, 1, 1, 4, tzinfo=JST)
    result = settle_capacity(source, target, recorder(calls))
    assert calls == [("2026-12", 20, 40, target), ("2027-01", 40, 0, target)]
    assert read_capacity(result)["current"] == 40


def test_leap_month_and_four_am_boundary_use_real_calendar_days():
    source = configured(now=datetime(2024, 2, 29, 12, tzinfo=JST))
    before = datetime(2024, 3, 1, 3, 59, 59, 999999, tzinfo=JST)
    calls = []
    result = settle_capacity(source, before, recorder(calls))
    assert calls == [("2024-02", 10, 20, before)]
    assert read_capacity(result)["period"] == "2024-02"
    target = datetime(2024, 3, 1, 4, tzinfo=JST)
    calls = []
    result = settle_capacity(result, target, recorder(calls))
    assert calls == [("2024-02", 10, 20, target), ("2024-03", 20, 0, target)]


@pytest.mark.parametrize("offset", [0, 8, 9])
def test_same_instant_in_different_timezones_produces_same_capacity_state(offset):
    source = configured()
    target = datetime(2026, 12, 1, 4, tzinfo=JST)
    local = target.astimezone(timezone(timedelta(hours=offset)))
    assert settle_capacity(source, local, recorder([])) == settle_capacity(source, target, recorder([]))


def test_naive_clock_is_explicit_project_jst_wall_clock():
    source = configured()
    target = datetime(2026, 12, 1, 4)
    assert settle_capacity(source, target, recorder([])) == settle_capacity(source, target.replace(tzinfo=JST), recorder([]))


def test_repeated_same_time_settlement_does_not_shift_or_duplicate_the_bonus():
    target = datetime(2026, 12, 1, 4, tzinfo=JST)
    result = settle_capacity(configured(), target, recorder([]))
    calls = []
    assert settle_capacity(result, target, recorder(calls)) == result
    assert calls == [("2026-12", 20, 0, target)]


def test_no_ledger_uses_raw_legacy_callback_without_guessing_a_month_or_initializing():
    source = state()
    source["tables"]["game_param"][0].update({CURRENT: 5, STAMINA: 2})
    source.pop("crystal_spending")
    before = deepcopy(source)
    calls = []
    target = datetime(2027, 1, 1, 4, tzinfo=JST)
    result = settle_capacity(source, target, recorder(calls, modify=True))
    assert calls == [(None, 5, 0, target)]
    assert "monthly_capacity" not in result
    assert result["tables"] == source["tables"]
    assert source == before


@pytest.mark.parametrize("mutation", ["wrong_schema", "bool_schema", "extra", "missing", "negative", "overflow",
    "bool_current", "float_next", "bad_period", "future_period", "bad_last", "projection", "stamina", "next_stamina"])
def test_invalid_ledger_and_projection_are_rejected_without_reinitializing(mutation):
    source = configured()
    ledger = source["monthly_capacity"]
    if mutation == "wrong_schema":
        ledger["schema_version"] = 2
    elif mutation == "bool_schema":
        ledger["schema_version"] = True
    elif mutation == "extra":
        ledger["extra"] = 0
    elif mutation == "missing":
        ledger.pop("next")
    elif mutation == "negative":
        ledger["current"] = -1
    elif mutation == "overflow":
        ledger["next"] = INT32_MAX + 1
    elif mutation == "bool_current":
        ledger["current"] = True
    elif mutation == "float_next":
        ledger["next"] = 20.0
    elif mutation == "bad_period":
        ledger["period"] = "2026-1"
    elif mutation == "future_period":
        ledger["period"] = "2026-12"
    elif mutation == "bad_last":
        ledger["last_time"] = True
    elif mutation == "projection":
        source["tables"]["game_param"][0][CURRENT] = 11
    else:
        source["tables"]["game_param"][0][STAMINA if mutation == "stamina" else NEXT_STAMINA] = 1
    fail(source, lambda: initialize_capacity(source), "capacity_state_invalid")
    fail(source, lambda: settle_capacity(source, NOW, recorder([])), "capacity_state_invalid")


@pytest.mark.parametrize("value", [None, [], {}, {"schema_version": 1}])
def test_present_but_bad_ledger_never_uses_legacy_fallback(value):
    source = state()
    source["monthly_capacity"] = value
    fail(source, lambda: settle_capacity(source, NOW, lambda *_: pytest.fail("Bad ledger used legacy path")), "capacity_state_invalid")


@pytest.mark.parametrize("clock", [None, "2026-12-01", True, datetime(1, 1, 1)])
def test_bad_clock_cannot_modify_an_initialized_ledger(clock):
    source = configured()
    fail(source, lambda: settle_capacity(source, clock, recorder([])), "capacity_clock_invalid")


@pytest.mark.parametrize("clock,month", MONTH_TURNS)
def test_the_capacity_month_turns_at_four_on_the_first_in_every_representation(clock, month):
    source = configured()
    settled = [settle_capacity(source, moment, recorder([])) for moment in representations(clock)]
    assert all(result == settled[0] for result in settled) and read_capacity(settled[0])["period"] == month


@pytest.mark.parametrize("clock", OUTSIDE_CLIENT_CALENDAR)
def test_instants_outside_the_client_calendar_are_capacity_clock_errors(clock):
    source = configured()
    error = fail(source, lambda: settle_capacity(source, clock, recorder([])), "capacity_clock_invalid")
    assert error.__cause__.code == "business_clock_invalid"


def test_callback_failure_after_first_boundary_does_not_change_the_input():
    source = configured()
    before = deepcopy(source)
    calls = []
    def broken(candidate, now):
        calls.append(now)
        candidate["other"]["keep"].append(99)
        if len(calls) == 2:
            raise ProtocolError("test_energy_failure", "Fixture failure", 503)
        return candidate
    fail(source, lambda: settle_capacity(source, datetime(2027, 1, 1, 4, tzinfo=JST), broken), "test_energy_failure")
    assert source == before


@pytest.mark.parametrize("kind", ["not_callable", "not_state", "capacity_write", "ledger_write"])
def test_callback_cannot_hide_unsupported_capacity_side_effects(kind):
    source = configured()
    if kind == "not_callable":
        callback = None
    else:
        def callback(candidate, now):
            if kind == "not_state":
                return None
            if kind == "capacity_write":
                candidate["tables"]["game_param"][0][NEXT] += 1
            else:
                candidate["monthly_capacity"]["next"] += 1
            return candidate
    fail(source, lambda: settle_capacity(source, NOW, callback), "capacity_callback_invalid")


def test_legacy_callback_cannot_invent_a_baseline():
    source = state()
    def callback(candidate, now):
        candidate["monthly_capacity"] = {}
        return candidate
    fail(source, lambda: settle_capacity(source, NOW, callback), "capacity_callback_invalid")


def following_boundary(period):
    year, month = map(int, period.split("-"))
    return datetime(year + (month == MONTHS_PER_YEAR), month % MONTHS_PER_YEAR + 1, 1, BUSINESS_DAY_START_HOUR,
                    tzinfo=JST)


def milliseconds(moment):
    return int(moment.timestamp() * 1000)


@pytest.mark.parametrize("distance", [1, 2, 3, 5])
@pytest.mark.parametrize("month", range(1, MONTHS_PER_YEAR + 1))
def test_month_turns_follow_plain_calendar_arithmetic_through_every_year_end(month, distance):
    source = configured(now=datetime(2026, month, 15, 12, tzinfo=JST))
    total = month - 1 + distance
    target = datetime(2026 + total // MONTHS_PER_YEAR, total % MONTHS_PER_YEAR + 1, 2, 12, tzinfo=JST)
    calls = []
    result = settle_capacity(source, target, recorder(calls))
    expected, period = [], f"2026-{month:02d}"
    for current, upcoming in ((10, 20), (20, 0))[:distance]:
        boundary = following_boundary(period)
        expected.append((period, current, upcoming, boundary))
        period = month_key(boundary.date())
    expected.append((month_key(target.date()), *((20, 0) if distance == 1 else (0, 0)), target))
    assert calls == expected
    assert read_capacity(result)["period"] == month_key(target.date())


def legacy(**values):
    source = state()
    source["tables"]["game_param"][0].update(values)
    return source


def test_a_baseline_in_the_business_month_keeps_the_legacy_values_from_now():
    source = legacy(**{CURRENT: 30, NEXT: 50})
    before = deepcopy(source)
    updated = establish_baseline(source, "2026-11", NOW)
    assert read_capacity(updated) == {"schema_version": 1, "period": "2026-11", "current": 30, "next": 50,
                                      "last_time": milliseconds(NOW)}
    assert {key: value for key, value in updated.items() if key != "monthly_capacity"} == before
    assert source == before and initialize_capacity(updated) == updated


def test_an_earlier_baseline_month_turns_its_boundaries_on_the_next_settlement():
    updated = establish_baseline(legacy(**{CURRENT: 30, NEXT: 50}), "2026-10", NOW)
    start = datetime(2026, 10, 1, BUSINESS_DAY_START_HOUR, tzinfo=JST)
    assert read_capacity(updated) == {"schema_version": 1, "period": "2026-10", "current": 30, "next": 50,
                                      "last_time": milliseconds(start)}
    calls = []
    settled = settle_capacity(updated, NOW, recorder(calls))
    assert calls == [("2026-10", 30, 50, following_boundary("2026-10")), ("2026-11", 50, 0, NOW)]
    assert read_capacity(settled)["current"] == settled["tables"]["game_param"][0][CURRENT] == 50
    with pytest.raises(ProtocolError) as caught:
        grant_next_capacity(updated, 1, NOW)
    assert caught.value.code == "capacity_refresh_required"


def test_both_monthly_ledgers_start_an_earlier_month_at_the_business_day_hour(monkeypatch):
    hour = BUSINESS_DAY_START_HOUR + 2
    monkeypatch.setattr(business_calendar, "BUSINESS_DAY_START_HOUR", hour)
    monkeypatch.setattr(business_calendar, "BUSINESS_DAY_START", timedelta(hours=hour))
    start, boundary = (datetime(2026, month, 1, hour, tzinfo=JST) for month in (10, 11))
    spending = state()
    del spending["crystal_spending"]
    spending["tables"]["game_param2"][0]["StampCardGrade"] = (800 << 16) | 51
    spending = initialize_spending(spending, NOW, baseline_period="2026-10")
    assert read_spending(spending)["last_time"] == milliseconds(start)
    assert read_spending(settle_spending(spending, NOW))["previous_spent"] == 51
    capacity = establish_baseline(legacy(**{CURRENT: 30, NEXT: 50}), "2026-10", NOW)
    assert read_capacity(capacity)["last_time"] == milliseconds(start)
    calls = []
    settle_capacity(capacity, NOW, recorder(calls))
    assert calls == [("2026-10", 30, 50, boundary), ("2026-11", 50, 0, NOW)]


@pytest.mark.parametrize("values", [{CURRENT: 30, STAMINA: 4, NEXT_STAMINA: 2}, {STAMINA: 3}])
def test_the_baseline_clears_stamina_extensions_the_ledger_cannot_hold(values):
    source = legacy(**values)
    assert legacy_extensions(source) == {CURRENT: 0, NEXT: 0, STAMINA: 0, NEXT_STAMINA: 0, **values}
    updated = establish_baseline(source, "2026-11", NOW)
    row = updated["tables"]["game_param"][0]
    assert [row[name] for name in (CURRENT, NEXT, *STAMINA_FIELDS)] == [values.get(CURRENT, 0), 0, 0, 0]
    assert read_capacity(updated)["current"] == values.get(CURRENT, 0)


def test_extension_fields_absent_from_an_old_row_read_as_zero():
    source = legacy(**{CURRENT: 30})
    for name in (NEXT, *STAMINA_FIELDS):
        source["tables"]["game_param"][0].pop(name)
    assert legacy_extensions(source) == {CURRENT: 30, NEXT: 0, STAMINA: 0, NEXT_STAMINA: 0}
    row = establish_baseline(source, "2026-11", NOW)["tables"]["game_param"][0]
    assert [row[name] for name in (CURRENT, NEXT, *STAMINA_FIELDS)] == [30, 0, 0, 0]


@pytest.mark.parametrize("period,code,status", [
    ("2026-12", PERIOD_AHEAD, 409), ("2030-01", PERIOD_AHEAD, 409), ("2026-13", PERIOD_INVALID, 400),
    ("0000-01", PERIOD_INVALID, 400), ("26-11", PERIOD_INVALID, 400), ("1969-12", PERIOD_INVALID, 400),
    (None, PERIOD_INVALID, 400)])
def test_baseline_months_must_be_canonical_client_months_not_ahead_of_the_business_month(period, code, status):
    source = legacy(**{CURRENT: 30})
    assert fail(source, lambda: establish_baseline(source, period, NOW), code).status == status


@pytest.mark.parametrize("build", [configured, state], ids=["existing_ledger", "zero_legacy"])
def test_a_baseline_is_refused_when_none_is_missing(build):
    source = build()
    assert fail(source, lambda: establish_baseline(source, "2026-11", NOW), BASELINE_UNNEEDED).status == 409


def test_a_bad_clock_cannot_establish_a_baseline():
    source = legacy(**{CURRENT: 30})
    fail(source, lambda: establish_baseline(source, "2026-11", None), "capacity_clock_invalid")
