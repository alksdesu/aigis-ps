from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta

from .business_calendar import MONTH_TEXT, MONTHS_PER_YEAR, business_month, month_start, next_month
from .crystal_spending import INT64_MAX, read_spending
from .energy_clock import UNIX_EPOCH, instant, unix_milliseconds
from .errors import ProtocolError
from .native_numbers import INT32_MAX


POLICY_VERSION = 1
BASELINE_REQUIRED = "capacity_baseline_required"
BASELINE_UNNEEDED = "capacity_baseline_unneeded"
PERIOD_INVALID = "capacity_period_invalid"
PERIOD_AHEAD = "capacity_period_ahead"
CHARISMA_FIELDS = ("ExtendedMaxCharisma", "ExtendedMaxCharismaNextMonth")
STAMINA_FIELDS = ("ExtendedMaxStamina", "ExtendedMaxStaminaNextMonth")
_EXTENSIONS = (*CHARISMA_FIELDS, *STAMINA_FIELDS)


def _invalid():
    raise ProtocolError("capacity_state_invalid", "Monthly capacity state is invalid or unsupported", 503)


def _integer(value, minimum=0, maximum=INT32_MAX):
    return type(value) is int and minimum <= value <= maximum


def _month(value):
    match = MONTH_TEXT.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("Monthly capacity requires a canonical period")
    year, month = map(int, match.groups())
    return month_start(date(year, month, 1))


def _clock(now):
    try:
        current = instant(now)
        return business_month(current), unix_milliseconds(current)
    except (ValueError, TypeError, OverflowError, ProtocolError) as error:
        raise ProtocolError("capacity_clock_invalid", "Monthly capacity requires a valid server instant") from error


def _row(state):
    if not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1:
        _invalid()
    tables = state.get("tables")
    rows = tables.get("game_param") if isinstance(tables, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        _invalid()
    return rows[0]


def _values(row, *, allow_missing=False):
    values = tuple(row.get(name, 0) if allow_missing else row.get(name) for name in _EXTENSIONS)
    if any(not _integer(value) for value in values):
        _invalid()
    return values


def legacy_extensions(state: dict) -> dict:
    return dict(zip(_EXTENSIONS, _values(_row(state), allow_missing=True)))


def read_capacity(state: dict) -> dict:
    row = _row(state)
    if "monthly_capacity" not in state:
        raise ProtocolError(BASELINE_REQUIRED, "Monthly capacity requires an initialized baseline", 503)
    ledger = state["monthly_capacity"]
    try:
        if (not isinstance(ledger, dict) or set(ledger) != {"schema_version", "period", "current", "next", "last_time"}
                or not _integer(ledger["schema_version"], 1, 1)
                or not _integer(ledger["current"]) or not _integer(ledger["next"])
                or not _integer(ledger["last_time"], -INT64_MAX - 1, INT64_MAX)):
            _invalid()
        _month(ledger["period"])
        if _clock(UNIX_EPOCH + timedelta(milliseconds=ledger["last_time"]))[0] != ledger["period"]:
            _invalid()
        if _values(row) != (ledger["current"], ledger["next"], 0, 0):
            _invalid()
    except (ValueError, TypeError, OverflowError) as error:
        raise ProtocolError("capacity_state_invalid", "Monthly capacity state is invalid or unsupported", 503) from error
    return deepcopy(ledger)


def _replace(state, ledger):
    updated = deepcopy(state)
    updated["monthly_capacity"] = deepcopy(ledger)
    row = _row(updated)
    row.update(zip(_EXTENSIONS, (ledger["current"], ledger["next"], 0, 0)))
    read_capacity(updated)
    return updated


def initialize_capacity(state: dict) -> dict:
    if isinstance(state, dict) and "monthly_capacity" in state:
        read_capacity(state)
        return deepcopy(state)
    row = _row(state)
    if any(_values(row, allow_missing=True)):
        raise ProtocolError(BASELINE_REQUIRED, "Nonzero legacy capacity requires an explicit baseline", 503)
    spending = read_spending(state)
    return _replace(state, {"schema_version": 1, "period": spending["period"],
                           "current": 0, "next": 0, "last_time": spending["last_time"]})


def establish_baseline(state: dict, period: str, now: datetime) -> dict:
    row = _row(state)
    if "monthly_capacity" in state:
        raise ProtocolError(BASELINE_UNNEEDED, "Monthly capacity already has a baseline", 409)
    values = _values(row, allow_missing=True)
    if not any(values):
        raise ProtocolError(BASELINE_UNNEEDED, "Zero legacy capacity starts its baseline automatically", 409)
    business, timestamp = _clock(now)
    try:
        start = _month(period)
        source, start_time = _clock(start)
    except ValueError as error:
        raise ProtocolError(PERIOD_INVALID, "Capacity baselines need a YYYY-MM month inside the client calendar") from error
    if start > _month(business):
        raise ProtocolError(PERIOD_AHEAD, "A capacity baseline cannot start after the current business month", 409)
    current, upcoming = values[:len(CHARISMA_FIELDS)]
    # An earlier month is recorded at its own start, so the next settlement still turns every month after it.
    return _replace(state, {"schema_version": 1, "period": source, "current": current, "next": upcoming,
                           "last_time": timestamp if source == business else start_time})


def _settle(state, now, callback):
    candidate = deepcopy(state)
    result = callback(candidate, now)
    if not isinstance(result, dict):
        raise ProtocolError("capacity_callback_invalid", "Energy settlement must return a candidate state", 503)
    if "monthly_capacity" in state:
        if result.get("monthly_capacity") != state["monthly_capacity"] or _values(_row(result)) != _values(_row(state)):
            raise ProtocolError("capacity_callback_invalid", "Energy settlement cannot rewrite monthly capacity", 503)
    elif "monthly_capacity" in result:
        raise ProtocolError("capacity_callback_invalid", "Legacy settlement cannot invent a monthly capacity baseline", 503)
    return deepcopy(result)


def settle_capacity(state: dict, now: datetime, settle_callback) -> dict:
    if not callable(settle_callback):
        raise ProtocolError("capacity_callback_invalid", "Energy settlement requires a callable callback", 503)
    if not isinstance(state, dict):
        _invalid()
    if "monthly_capacity" not in state:
        return _settle(state, now, settle_callback)
    ledger = read_capacity(state)
    period, timestamp = _clock(now)
    if timestamp < ledger["last_time"]:
        raise ProtocolError("capacity_clock_backwards", "Capacity clock precedes its saved state", 409)
    start, target = _month(ledger["period"]), _month(period)
    distance = (target.year - start.year) * MONTHS_PER_YEAR + target.month - start.month
    updated = deepcopy(state)
    for _ in range(min(distance, 2)):
        boundary = month_start(next_month(_month(ledger["period"]).date()))
        updated = _settle(updated, boundary, settle_callback)
        boundary_period, boundary_time = _clock(boundary)
        ledger.update(period=boundary_period, current=ledger["next"], next=0, last_time=boundary_time)
        updated = _replace(updated, ledger)
    if distance > 2:
        ledger.update(period=period, last_time=timestamp)
        updated = _replace(updated, ledger)
    updated = _settle(updated, now, settle_callback)
    ledger["last_time"] = timestamp
    return _replace(updated, ledger)


def grant_next_capacity(state: dict, amount: int, now: datetime) -> dict:
    if not _integer(amount, 1):
        raise ProtocolError("capacity_amount_invalid", "Capacity rewards require a positive int32 amount")
    ledger = read_capacity(state)
    period, timestamp = _clock(now)
    if period != ledger["period"]:
        raise ProtocolError("capacity_refresh_required", "Monthly capacity must be settled before granting the next reward", 409)
    if timestamp < ledger["last_time"]:
        raise ProtocolError("capacity_clock_backwards", "Capacity clock precedes its saved state", 409)
    if ledger["next"] + amount > INT32_MAX:
        raise ProtocolError("capacity_overflow", "Next-month capacity would exceed int32", 409)
    ledger["next"] += amount
    ledger["last_time"] = timestamp
    return _replace(state, ledger)
