from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import re

from .business_calendar import MONTH_TEXT, business_month
from .crystal_spending import INT64_MAX, NEW_LOCAL as SPENDING_NEW_LOCAL, PROJECTION_CAP, read_spending
from .energy_clock import UNIX_EPOCH, instant, unix_milliseconds
from .errors import ProtocolError
from .native_numbers import INT32_MAX


MAX_PERIODS = 1200
MAX_RECORDS = 65535
EARNED, BASELINE = "earned", "baseline"
_ID = re.compile(r"[1-9][0-9]{0,9}\Z")


def _integer(value, minimum=0, maximum=INT32_MAX):
    return type(value) is int and minimum <= value <= maximum


def _invalid():
    raise ProtocolError("sugoroku_state_invalid", "Reward-board state is invalid or unsupported", 503)


def _month(value):
    match = MONTH_TEXT.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("Reward-board periods require YYYY-MM")
    year, month = map(int, match.groups())
    datetime(year, month, 1)


def _clock(now):
    try:
        current = instant(now)
        return business_month(current), unix_milliseconds(current)
    except (ValueError, TypeError, OverflowError, ProtocolError) as error:
        raise ProtocolError("sugoroku_clock_invalid", "Reward-board time must be a valid server instant") from error


def read_sugoroku(state: dict) -> dict:
    spending = read_spending(state)
    if "sugoroku" not in state:
        raise ProtocolError("sugoroku_state_required", "Reward-board state requires an initialized baseline", 503)
    ledger = state["sugoroku"]
    try:
        if (not isinstance(ledger, dict) or set(ledger) != {"schema_version", "last_time", "periods"}
                or not _integer(ledger["schema_version"], 1, 1)
                or not _integer(ledger["last_time"], -INT64_MAX - 1, INT64_MAX)
                or not isinstance(ledger["periods"], dict) or not 1 <= len(ledger["periods"]) <= MAX_PERIODS):
            _invalid()
        observed_period, _ = _clock(UNIX_EPOCH + timedelta(milliseconds=ledger["last_time"]))
        if observed_period > spending["period"]:
            _invalid()
        count, mail_count, seen_mails = 0, 0, set()
        for period, page in ledger["periods"].items():
            _month(period)
            if (period > spending["period"] or period > observed_period or not isinstance(page, dict)
                    or set(page) != {"baseline_spent", "records"}
                    or not _integer(page["baseline_spent"], 0, INT64_MAX)
                    or not isinstance(page["records"], dict)):
                _invalid()
            if period == spending["period"] and page["baseline_spent"] > spending["current_spent"]:
                _invalid()
            count += len(page["records"])
            if count > MAX_RECORDS:
                _invalid()
            for identifier, record in page["records"].items():
                if (not isinstance(identifier, str) or not _ID.fullmatch(identifier) or int(identifier) > INT32_MAX
                        or not isinstance(record, dict) or set(record) != {"status", "source", "mail_ids"}
                        or not _integer(record["status"], 0, 2) or record["source"] not in (EARNED, BASELINE)
                        or not isinstance(record["mail_ids"], list)):
                    _invalid()
                ids = record["mail_ids"]
                mail_count += len(ids)
                if (mail_count > MAX_RECORDS or any(not _integer(value, 1) for value in ids)
                        or len(set(ids)) != len(ids) or seen_mails.intersection(ids)):
                    _invalid()
                seen_mails.update(ids)
                if record["source"] == BASELINE:
                    if record["status"] != 2 or ids or page["baseline_spent"] == 0:
                        _invalid()
                elif record["status"] != 2 and ids:
                    _invalid()
        return deepcopy(ledger)
    except (ValueError, TypeError, OverflowError) as error:
        raise ProtocolError("sugoroku_state_invalid", "Reward-board state is invalid or unsupported", 503) from error


def _replace(state, ledger):
    updated = deepcopy(state)
    updated["sugoroku"] = deepcopy(ledger)
    read_sugoroku(updated)
    return updated


def initialize_sugoroku(state: dict) -> dict:
    spending = read_spending(state)
    if "sugoroku" in state:
        read_sugoroku(state)
        return deepcopy(state)
    baseline = 0 if spending["baseline_source"] == SPENDING_NEW_LOCAL else spending["current_spent"]
    return _replace(state, {"schema_version": 1, "last_time": spending["last_time"],
        "periods": {spending["period"]: {"baseline_spent": baseline, "records": {}}}})


def _active(catalog, now):
    try:
        entries = catalog.active(now)
        if not isinstance(entries, (tuple, list)) or len(entries) > MAX_RECORDS:
            raise ValueError("Reward-board definitions require a bounded sequence")
        seen = set()
        for entry in entries:
            if (not _integer(entry.reward_id, 1) or not _integer(entry.consume_crystal)
                    or entry.reward_id in seen):
                raise ValueError("Reward-board identities and thresholds must be valid and distinct")
            seen.add(entry.reward_id)
        return entries
    except (AttributeError, TypeError, ValueError) as error:
        if isinstance(error, ProtocolError):
            raise
        raise ProtocolError("sugoroku_catalog_invalid", "Reward-board definitions are invalid", 503) from error


def sync_sugoroku(state: dict, catalog, now: datetime) -> dict:
    spending = read_spending(state)
    period, timestamp = _clock(now)
    if period != spending["period"]:
        raise ProtocolError("sugoroku_refresh_required", "Monthly spending must be refreshed before opening the reward board", 409)
    if timestamp < spending["last_time"]:
        raise ProtocolError("sugoroku_clock_backwards", "Reward-board time precedes the spending state", 409)
    initialized = initialize_sugoroku(state)
    ledger = read_sugoroku(initialized)
    if timestamp < ledger["last_time"]:
        raise ProtocolError("sugoroku_clock_backwards", "Reward-board time precedes its saved state", 409)
    if period not in ledger["periods"]:
        if len(ledger["periods"]) >= MAX_PERIODS:
            raise ProtocolError("sugoroku_capacity", "Reward-board history reached the supported period limit", 409)
        ledger["periods"][period] = {"baseline_spent": 0, "records": {}}
    page = ledger["periods"][period]
    reached = min(spending["current_spent"], PROJECTION_CAP)
    baseline = min(page["baseline_spent"], PROJECTION_CAP)
    for entry in _active(catalog, now):
        identifier = str(entry.reward_id)
        if entry.consume_crystal <= reached and identifier not in page["records"]:
            historical = baseline > 0 and entry.consume_crystal <= baseline
            page["records"][identifier] = {"status": 2 if historical else 0,
                "source": BASELINE if historical else EARNED, "mail_ids": []}
    if sum(len(page["records"]) for page in ledger["periods"].values()) > MAX_RECORDS:
        raise ProtocolError("sugoroku_capacity", "Reward-board history reached the supported record limit", 409)
    ledger["last_time"] = timestamp
    return _replace(initialized, ledger)


def snapshot_sugoroku(state: dict, catalog, now: datetime) -> list[dict]:
    updated = sync_sugoroku(state, catalog, now)
    spending = read_spending(updated)
    records = read_sugoroku(updated)["periods"][spending["period"]]["records"]
    reached = min(spending["current_spent"], PROJECTION_CAP)
    return [{"ID": entry.reward_id, "Status": records[str(entry.reward_id)]["status"]}
            for entry in _active(catalog, now) if entry.consume_crystal <= reached]


def mark_sugoroku(state: dict, ids, *, claimed=False, deliveries=None) -> dict:
    ledger = read_sugoroku(state)
    period = read_spending(state)["period"]
    if period not in ledger["periods"]:
        raise ProtocolError("sugoroku_refresh_required", "Reward-board period must be synchronized first", 409)
    if (not isinstance(ids, (tuple, list)) or len(ids) > MAX_RECORDS or any(not _integer(value, 1) for value in ids)
            or len(set(ids)) != len(ids) or type(claimed) is not bool):
        raise ProtocolError("sugoroku_selection_invalid", "Reward-board selection must contain distinct native identities")
    if claimed:
        if (not isinstance(deliveries, dict) or any(not _integer(key, 1) for key in deliveries)
                or set(deliveries) != set(ids) or any(not isinstance(value, list) for value in deliveries.values())):
            raise ProtocolError("sugoroku_deliveries_invalid", "Every claimed reward requires its explicit mail identity list")
    elif deliveries not in (None, {}):
        raise ProtocolError("sugoroku_deliveries_invalid", "Read acknowledgements cannot record reward deliveries")
    records = ledger["periods"][period]["records"]
    if any(str(identifier) not in records for identifier in ids):
        raise ProtocolError("sugoroku_not_reached", "A selected reward has not been reached", 409)
    for identifier in ids:
        record = records[str(identifier)]
        if claimed:
            supplied = deliveries[identifier]
            if (len(supplied) > MAX_RECORDS or any(not _integer(value, 1) for value in supplied)
                    or len(set(supplied)) != len(supplied)):
                raise ProtocolError("sugoroku_deliveries_invalid", "Reward deliveries require distinct native mail identities")
            if record["status"] == 2 and record["mail_ids"] != supplied:
                raise ProtocolError("sugoroku_already_claimed", "A claimed reward cannot be assigned another delivery", 409)
            record["status"] = 2
            record["mail_ids"] = deepcopy(supplied)
        elif record["status"] == 0:
            record["status"] = 1
    return _replace(state, ledger)
