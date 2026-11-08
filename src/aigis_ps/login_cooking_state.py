from __future__ import annotations

from calendar import monthrange
from collections.abc import Callable
from copy import deepcopy
from datetime import date, datetime, timedelta

from .business_calendar import DAY_TEXT, MONTH_TEXT, business_date, civil_date, month_key
from .crystal_spending import (INT64_MAX, LEGACY_EXPLICIT as EXPLICIT_LEGACY, LEGACY_ZERO, NEW_LOCAL, PROJECTION_CAP,
                               read_spending)
from .energy_clock import UNIX_EPOCH, instant, unix_milliseconds
from .errors import ProtocolError
from .login_reward_state import NEW_LOCAL as LOGIN_NEW_LOCAL, read_login_rewards
from .native_numbers import int32
from .vote_cooking_catalog import MAX_CARD_ID


LEDGER_KEY = "login_cooking"
RULES_CHANGED = "login_cooking_rules_changed"
SENDER_FIELD = "last_sender"
MAX_COOKING = 25
CRYSTALS_PER_COOKING = 15
POLICY_VERSION = 1
_FIELDS = frozenset(("schema_version", "period", "quota", "issued", "last_claim_day",
                     "last_civil_day", "last_time", "baseline_source", "border"))
_SOURCES = frozenset((NEW_LOCAL, LEGACY_ZERO, EXPLICIT_LEGACY))


def _invalid():
    raise ProtocolError("login_cooking_invalid", "Login cooking state is invalid or inconsistent", 503)


def _border(value):
    if not int32(value) or value < 0:
        raise ProtocolError("login_cooking_border_invalid", "Cooking border requires a nonnegative int32 value")
    return value


def _day(value):
    if type(value) is not str or DAY_TEXT.fullmatch(value) is None:
        raise ValueError("Cooking claim day must be a canonical date")
    return date.fromisoformat(value)


def _month(value):
    if type(value) is not str or MONTH_TEXT.fullmatch(value) is None:
        raise ValueError("Cooking period must be a canonical month")
    return date.fromisoformat(value + "-01")


def _clock(now):
    try:
        current = instant(now)
        return business_date(current), civil_date(current), unix_milliseconds(current)
    except (ValueError, TypeError, OverflowError, ProtocolError) as error:
        raise ProtocolError("login_cooking_clock_invalid", "Cooking requires a representable server instant") from error


def _quota(previous, border):
    return max(0, min(MAX_COOKING, (min(previous, PROJECTION_CAP) - border) // CRYSTALS_PER_COOKING))


def _row(state):
    if not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1:
        _invalid()
    tables = state.get("tables")
    rows = tables.get("game_param2") if isinstance(tables, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        _invalid()
    row = rows[0]
    if (not int32(row.get("HomeCookingReceivedAmount")) or not 0 <= row["HomeCookingReceivedAmount"] <= MAX_COOKING
            or not int32(row.get("HomeCookingReceivedDate")) or row["HomeCookingReceivedDate"] < 0):
        _invalid()
    return row


def read_cooking(state: dict) -> dict:
    row = _row(state)
    if LEDGER_KEY not in state:
        raise ProtocolError("login_cooking_required", "Login cooking requires an initialized baseline", 503)
    ledger = state[LEDGER_KEY]
    try:
        if (not isinstance(ledger, dict) or ledger.keys() - {SENDER_FIELD} != _FIELDS
                or not int32(ledger["schema_version"]) or ledger["schema_version"] != 1
                or any(not int32(ledger[name]) or not 0 <= ledger[name] <= MAX_COOKING for name in ("quota", "issued"))
                or type(ledger["baseline_source"]) is not str or ledger["baseline_source"] not in _SOURCES
                or not int32(ledger["border"]) or ledger["border"] < 0
                or type(ledger["last_time"]) is not int or not -INT64_MAX - 1 <= ledger["last_time"] <= INT64_MAX
                or SENDER_FIELD in ledger and (not int32(ledger[SENDER_FIELD])
                                               or not 1 <= ledger[SENDER_FIELD] <= MAX_CARD_ID)):
            _invalid()
        period = _month(ledger["period"])
        observed, civil, _ = _clock(UNIX_EPOCH + timedelta(milliseconds=ledger["last_time"]))
        if period > _month(month_key(observed)) or row["HomeCookingReceivedAmount"] != ledger["issued"]:
            _invalid()
        legacy = ledger["baseline_source"] == EXPLICIT_LEGACY
        if ledger["issued"] > ledger["quota"] and not legacy:
            _invalid()
        if ledger["last_claim_day"] is None or ledger["last_civil_day"] is None:
            if (ledger["last_claim_day"] is not None or ledger["last_civil_day"] is not None
                    or row["HomeCookingReceivedDate"] != 0 or ledger["issued"] and not legacy
                    or SENDER_FIELD in ledger):
                _invalid()
        else:
            claimed, last_civil = _day(ledger["last_claim_day"]), _day(ledger["last_civil_day"])
            if (last_civil.toordinal() <= 1 or last_civil > civil or claimed > last_civil
                    or (last_civil - claimed).days not in (0, 1)
                    or month_key(claimed) > ledger["period"]
                    or claimed > observed and not legacy
                    or row["HomeCookingReceivedDate"] != last_civil.toordinal() - 1):
                _invalid()
            if ledger["issued"] and month_key(claimed) != ledger["period"]:
                _invalid()
    except (ValueError, TypeError, OverflowError, ProtocolError) as error:
        if isinstance(error, ProtocolError) and error.code == "login_cooking_invalid":
            raise
        raise ProtocolError("login_cooking_invalid", "Login cooking state is invalid or inconsistent", 503) from error
    return deepcopy(ledger)


def _replace(state, ledger):
    updated = deepcopy(state)
    updated[LEDGER_KEY] = deepcopy(ledger)
    row = _row(updated)
    row["HomeCookingReceivedAmount"] = ledger["issued"]
    row["HomeCookingReceivedDate"] = 0 if ledger["last_civil_day"] is None else _day(ledger["last_civil_day"]).toordinal() - 1
    read_cooking(updated)
    return updated


def _rules(ledger, border):
    if ledger["border"] != border:
        raise ProtocolError(RULES_CHANGED, "Cooking border changed and requires an explicit migration", 503)


def read_current_cooking(state: dict, *, border: int) -> dict:
    _border(border)
    ledger = read_cooking(state)
    _rules(ledger, border)
    return ledger


def initialize_cooking(state: dict, *, border: int, legacy_period=None) -> dict:
    _border(border)
    if isinstance(state, dict) and LEDGER_KEY in state:
        # A changed border stops only 115, so the save still loads and the console can migrate it.
        read_cooking(state)
        return deepcopy(state)
    row = _row(state)
    spending, login = read_spending(state), read_login_rewards(state)
    timestamp = max(spending["last_time"], login["last_time"])
    amount, encoded = row["HomeCookingReceivedAmount"], row["HomeCookingReceivedDate"]
    period, claimed, civil = spending["period"], None, None
    source = (NEW_LOCAL if spending["baseline_source"] == NEW_LOCAL and login["baseline_source"] == LOGIN_NEW_LOCAL
              else LEGACY_ZERO)
    if amount or encoded:
        if source == NEW_LOCAL:
            raise ProtocolError("login_cooking_baseline_invalid", "New local accounts require an empty cooking baseline", 503)
        if legacy_period is None:
            raise ProtocolError("login_cooking_baseline_required", "Nonzero legacy cooking fields require an explicit period", 503)
        try:
            first = _month(legacy_period)
            if first > _month(period):
                _invalid()
            period, source = legacy_period, EXPLICIT_LEGACY
            if encoded:
                civil = date.fromordinal(encoded + 1)
                last = first.replace(day=monthrange(first.year, first.month)[1])
                if civil > last:
                    if civil != last + timedelta(days=1):
                        _invalid()
                    claimed = last
                else:
                    claimed = civil
        except (ValueError, TypeError, OverflowError) as error:
            raise ProtocolError("login_cooking_invalid", "Legacy cooking period or civil date is invalid", 503) from error
    elif legacy_period is not None:
        try:
            if _month(legacy_period) > _month(period):
                _invalid()
        except (ValueError, TypeError, OverflowError) as error:
            raise ProtocolError("login_cooking_invalid", "Legacy cooking period is invalid", 503) from error
    ledger = {"schema_version": 1, "period": period,
        "quota": _quota(spending["previous_spent"], border) if period == spending["period"] else 0,
        "issued": amount, "last_claim_day": claimed.isoformat() if claimed else None,
        "last_civil_day": civil.isoformat() if civil else None, "last_time": timestamp,
        "baseline_source": source, "border": border}
    return _replace(state, ledger)


def sync_cooking(state: dict, now: datetime, *, border: int, touch_clock=False) -> dict:
    _border(border)
    if type(touch_clock) is not bool:
        raise ProtocolError("login_cooking_clock_invalid", "Clock update policy requires a boolean")
    ledger = read_cooking(state)
    _rules(ledger, border)
    spending, login = read_spending(state), read_login_rewards(state)
    day, _, timestamp = _clock(now)
    period = month_key(day)
    if timestamp < max(ledger["last_time"], spending["last_time"], login["last_time"]):
        raise ProtocolError("login_cooking_clock_backwards", "Cooking clock precedes its saved state", 409)
    if spending["period"] != period:
        raise ProtocolError("login_cooking_refresh_required", "Monthly crystal spending must be settled before cooking", 409)
    quota = _quota(spending["previous_spent"], border)
    if ledger["period"] > period:
        raise ProtocolError("login_cooking_clock_backwards", "Cooking period precedes its saved state", 409)
    if ledger["period"] != period:
        ledger.update(period=period, quota=quota, issued=0, last_time=timestamp)
    elif ledger["quota"] != quota:
        raise ProtocolError("login_cooking_invalid", "Cooking quota disagrees with the saved previous-month spending", 503)
    elif touch_clock:
        ledger["last_time"] = timestamp
    return _replace(state, ledger)


def advance_cooking(state: dict, now: datetime, *, border: int, eligible: bool,
                    sender: Callable[[str, int], int] | None = None) -> tuple[dict, bool]:
    if type(eligible) is not bool:
        raise ProtocolError("login_cooking_eligible_invalid", "Cooking eligibility requires the login new-day boolean")
    if sender is not None and not callable(sender):
        raise ProtocolError("login_cooking_sender_invalid", "Cooking senders are chosen by a callable")
    updated = sync_cooking(state, now, border=border, touch_clock=True)
    ledger = read_cooking(updated)
    day, civil, _ = _clock(now)
    if not eligible or ledger["issued"] >= ledger["quota"] or ledger["last_claim_day"] is not None and day.isoformat() <= ledger["last_claim_day"]:
        return updated, False
    ledger.update(issued=ledger["issued"] + 1, last_claim_day=day.isoformat(), last_civil_day=civil.isoformat())
    # The sender belongs to the latest delivery, so an item delivery drops the one an earlier delivery recorded.
    ledger.pop(SENDER_FIELD, None)
    if sender is not None:
        ledger[SENDER_FIELD] = sender(ledger["last_claim_day"], ledger["issued"])
    return _replace(updated, ledger), True


def migrate_cooking(state: dict, now: datetime, *, border: int) -> dict:
    _border(border)
    ledger, spending, login = read_cooking(state), read_spending(state), read_login_rewards(state)
    day, _, timestamp = _clock(now)
    if timestamp < max(ledger["last_time"], spending["last_time"], login["last_time"]):
        raise ProtocolError("login_cooking_clock_backwards", "Cooking clock precedes its saved state", 409)
    # An ended month restarts at its next sync, so only the current month's quota is recounted.
    if ledger["period"] == spending["period"] == month_key(day):
        quota = _quota(spending["previous_spent"], border)
        if ledger["issued"] > quota and ledger["baseline_source"] != EXPLICIT_LEGACY:
            raise ProtocolError("login_cooking_quota_below_issued",
                                "The migrated quota is below the cookings already issued this month", 409)
        ledger["quota"] = quota
    ledger["border"] = border
    return _replace(state, ledger)
