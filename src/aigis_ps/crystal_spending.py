from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta

from .alxml import Column, Container
from .business_calendar import MONTH_TEXT, MONTHS_PER_YEAR, business_month, month_start
from .crystal_wallet import debit_crystals, wallet_totals
from .energy_clock import UNIX_EPOCH, instant, unix_milliseconds
from .errors import ProtocolError
from .native_numbers import INT32_MAX


POLICY_VERSION = 1
PROJECTION_CAP = 32767
INT64_MAX = (1 << 63) - 1
LEGACY = "legacy"
NEW_LOCAL, LEGACY_ZERO, LEGACY_EXPLICIT = "new_local", "legacy_zero", "legacy_explicit"
_FIELDS = frozenset(("schema_version", "period", "current_spent", "previous_spent", "last_time", "baseline_source"))
_SOURCES = frozenset((NEW_LOCAL, LEGACY_ZERO, LEGACY_EXPLICIT))


def _invalid():
    raise ProtocolError("crystal_spending_invalid", "Monthly crystal spending state is invalid or unsupported", 503)


def _month(value):
    match = MONTH_TEXT.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("Monthly spending requires a canonical period")
    year, month = (int(part) for part in match.groups())
    return month_start(date(year, month, 1))


def _clock(now):
    try:
        current = instant(now)
        return business_month(current), unix_milliseconds(current)
    except (ValueError, TypeError, OverflowError, ProtocolError) as error:
        raise ProtocolError("crystal_spending_clock_invalid", "Monthly spending requires a valid server instant") from error


def _game_param(state):
    if not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1:
        _invalid()
    tables = state.get("tables")
    rows = tables.get("game_param2") if isinstance(tables, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        _invalid()
    packed = rows[0].get("StampCardGrade")
    if type(packed) is not int or not 0 <= packed <= INT32_MAX or (packed & 0xFFFF) > PROJECTION_CAP:
        _invalid()
    return rows[0]


def _projection(ledger):
    return (min(ledger["previous_spent"], PROJECTION_CAP) << 16) | min(ledger["current_spent"], PROJECTION_CAP)


def read_spending(state: dict) -> dict:
    row = _game_param(state)
    if "crystal_spending" not in state:
        raise ProtocolError("crystal_spending_required", "Monthly crystal spending requires an initialized baseline", 503)
    ledger = state["crystal_spending"]
    try:
        if (not isinstance(ledger, dict) or ledger.keys() != _FIELDS
                or type(ledger["schema_version"]) is not int or ledger["schema_version"] != 1
                or not isinstance(ledger["baseline_source"], str) or ledger["baseline_source"] not in _SOURCES):
            _invalid()
        _month(ledger["period"])
        if any(type(ledger[key]) is not int or not 0 <= ledger[key] <= INT64_MAX
               for key in ("current_spent", "previous_spent")):
            _invalid()
        last = ledger["last_time"]
        if type(last) is not int or not -INT64_MAX - 1 <= last <= INT64_MAX:
            _invalid()
        if _clock(UNIX_EPOCH + timedelta(milliseconds=last))[0] != ledger["period"] or row["StampCardGrade"] != _projection(ledger):
            _invalid()
    except (ValueError, TypeError, OverflowError) as error:
        raise ProtocolError("crystal_spending_invalid", "Monthly crystal spending state is invalid or unsupported", 503) from error
    return deepcopy(ledger)


def _replace(state, ledger):
    updated = deepcopy(state)
    updated["crystal_spending"] = deepcopy(ledger)
    updated["tables"]["game_param2"][0]["StampCardGrade"] = _projection(ledger)
    read_spending(updated)
    return updated


def initialize_spending(state: dict, now: datetime, *, origin=LEGACY, baseline_period=None) -> dict:
    if isinstance(state, dict) and "crystal_spending" in state:
        read_spending(state)
        return deepcopy(state)
    row = _game_param(state)
    if origin not in (LEGACY, NEW_LOCAL) or type(origin) is not str:
        raise ProtocolError("crystal_spending_origin_invalid", "Spending origin must be legacy or new_local")
    period, timestamp = _clock(now)
    packed = row["StampCardGrade"]
    if origin == NEW_LOCAL:
        if packed or baseline_period is not None:
            raise ProtocolError("crystal_spending_baseline_invalid", "New accounts require a zero spending baseline")
        source = NEW_LOCAL
    else:
        source = LEGACY_ZERO
        if baseline_period is not None:
            try:
                baseline = _month(baseline_period)
                if baseline_period > period:
                    raise ValueError("Spending baseline cannot be in the future")
            except (ValueError, TypeError, OverflowError) as error:
                raise ProtocolError("crystal_spending_baseline_invalid", "Spending baseline period is invalid") from error
        if packed:
            if baseline_period is None:
                raise ProtocolError("crystal_spending_baseline_required", "Nonzero legacy spending requires an explicit baseline period", 503)
            source = LEGACY_EXPLICIT
            if baseline_period != period:
                timestamp = _clock(baseline)[1]
            period = baseline_period
    return _replace(state, {"schema_version": 1, "period": period, "current_spent": packed & 0xFFFF,
                            "previous_spent": packed >> 16, "last_time": timestamp, "baseline_source": source})


def settle_spending(state: dict, now: datetime) -> dict:
    initialized = initialize_spending(state, now)
    ledger = read_spending(initialized)
    period, timestamp = _clock(now)
    if timestamp < ledger["last_time"]:
        raise ProtocolError("crystal_spending_clock_backwards", "Spending clock precedes the saved instant", 409)
    current, previous = _month(period), _month(ledger["period"])
    distance = (current.year - previous.year) * MONTHS_PER_YEAR + current.month - previous.month
    if distance:
        ledger["previous_spent"] = ledger["current_spent"] if distance == 1 else 0
        ledger["current_spent"] = 0
        ledger["period"] = period
    ledger["last_time"] = timestamp
    return _replace(initialized, ledger)


def spend_crystals(state: dict, amount: int, now: datetime, *, paid_only=False, free_first=True) -> dict:
    if (type(amount) is not int or not 0 <= amount <= INT32_MAX
            or type(paid_only) is not bool or type(free_first) is not bool):
        raise ProtocolError("invalid_crystal_debit", "Crystal debit requires a nonnegative amount and boolean policies")
    settled = settle_spending(state, now)
    ledger = read_spending(settled)
    if ledger["current_spent"] + amount > INT64_MAX:
        raise ProtocolError("crystal_spending_overflow", "Monthly crystal consumption exceeds the supported counter limit", 409)
    before = wallet_totals(settled)["total"]
    updated = debit_crystals(settled, amount, paid_only=paid_only, free_first=free_first)
    if before - wallet_totals(updated)["total"] != amount or read_spending(updated) != ledger:
        raise ProtocolError("crystal_spending_debit_invalid", "Crystal debit does not match the requested spending", 503)
    ledger["current_spent"] += amount
    return _replace(updated, ledger)


def spending_response(state: dict, *, style="key_values") -> Container:
    packed = _projection(read_spending(state))
    if style == "key_values":
        return Container("PGP2", (Column("A1", "S", ("C1",)), Column("A2", "S", (str(packed),))))
    if style == "columns":
        return Container("PGP2", (Column("C1", "I", (packed,)),))
    raise ProtocolError("crystal_spending_style_invalid", "Unknown monthly spending response style")
