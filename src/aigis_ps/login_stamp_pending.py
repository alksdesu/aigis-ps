from __future__ import annotations

from copy import deepcopy
from datetime import date
import re

from .business_calendar import DAY_TEXT
from .errors import ProtocolError
from .login_reward_state import read_login_rewards
from .native_numbers import int32


PENDING_KEY = "login_stamp_pending"
SCHEMA_VERSION = 1
# Three years of claim days; a longer gap means the stamp tables are long overdue, so 115 refuses instead of growing the save.
MAX_PENDING_STAMPS = 3 * 366
CAPACITY_EXCEEDED = "login_stamp_pending_capacity"
LEDGER_FIELDS = frozenset(("schema_version", "entries"))
ENTRY_FIELDS = frozenset(("day", "grade", "month", "ordinal", "definition", "reason"))
IDENTITY_FIELDS = ("day", "grade", "month", "ordinal", "definition")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_REASON = re.compile(r"[a-z0-9_]{1,128}\Z")


def _invalid(message="Pending login stamps are invalid"):
    raise ProtocolError("login_stamp_pending_invalid", message, 503)


def _entry_day(entry) -> date:
    if (not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS
            or type(entry["day"]) is not str or not DAY_TEXT.fullmatch(entry["day"])):
        _invalid("Pending login stamp fields are invalid")
    try:
        day = date.fromisoformat(entry["day"])
    except ValueError:
        _invalid("Pending login stamp day does not exist")
    if (not int32(entry["grade"]) or entry["grade"] < 0
            or type(entry["month"]) is not int or entry["month"] != day.month
            or type(entry["ordinal"]) is not int or not 1 <= entry["ordinal"] <= day.day
            or entry["definition"] is not None and (type(entry["definition"]) is not str
                                                    or not _HASH.fullmatch(entry["definition"]))
            or type(entry["reason"]) is not str or not _REASON.fullmatch(entry["reason"])):
        _invalid("Pending login stamp values are invalid")
    return day


def read_pending_stamps(state) -> list[dict]:
    if not isinstance(state, dict):
        _invalid()
    if PENDING_KEY not in state:
        return []
    ledger = state[PENDING_KEY]
    if (not isinstance(ledger, dict) or set(ledger) != LEDGER_FIELDS
            or type(ledger["schema_version"]) is not int or ledger["schema_version"] != SCHEMA_VERSION
            or not isinstance(ledger["entries"], list) or not 0 < len(ledger["entries"]) <= MAX_PENDING_STAMPS):
        _invalid()
    progress = read_login_rewards(state)
    previous = None
    for entry in ledger["entries"]:
        day = _entry_day(entry)
        if previous is not None and (day <= previous[0] or (day.year, day.month) == previous[1]
                                     and entry["ordinal"] <= previous[2]):
            _invalid("Pending login stamps must follow the claim order")
        previous = day, (day.year, day.month), entry["ordinal"]
    last = ledger["entries"][-1]
    if (progress["last_claim_day"] is None or last["day"] > progress["last_claim_day"]
            or last["day"] == progress["last_claim_day"] and last["ordinal"] != progress["stamp_count"]):
        _invalid("Pending login stamps must belong to claimed days")
    return deepcopy(ledger["entries"])


def _replace(state, entries):
    updated = deepcopy(state)
    if entries:
        updated[PENDING_KEY] = {"schema_version": SCHEMA_VERSION, "entries": deepcopy(entries)}
    else:
        updated.pop(PENDING_KEY, None)
    read_pending_stamps(updated)
    return updated


def hold_stamp(state, *, day, grade, month, ordinal, definition, reason) -> dict:
    entry = {"day": day, "grade": grade, "month": month, "ordinal": ordinal, "definition": definition, "reason": reason}
    _entry_day(entry)
    entries = read_pending_stamps(state)
    current = next((item for item in entries if item["day"] == day), None)
    if current is None:
        if entries and entries[-1]["day"] >= day:
            _invalid("New pending login stamps must follow the latest held day")
        if len(entries) >= MAX_PENDING_STAMPS:
            raise ProtocolError(CAPACITY_EXCEEDED, "Too many login stamps are waiting for their reward tables", 503)
        entries.append(entry)
    elif any(current[name] != entry[name] for name in IDENTITY_FIELDS):
        _invalid("A claim day cannot hold a different login stamp")
    else:
        current["reason"] = reason
    return _replace(state, entries)


def _held_index(entries, day) -> int:
    index = next((position for position, item in enumerate(entries) if item["day"] == day), None)
    if index is None:
        _invalid("No login stamp is pending for this claim day")
    return index


def rebind_stamp(state, day, definition, reason) -> dict:
    if type(definition) is not str or not _HASH.fullmatch(definition):
        _invalid("A rebound login stamp needs a resolved reward definition")
    entries = read_pending_stamps(state)
    entries[_held_index(entries, day)].update(definition=definition, reason=reason)
    return _replace(state, entries)


def release_stamp(state, day) -> tuple[dict, dict]:
    entries = read_pending_stamps(state)
    entry = entries.pop(_held_index(entries, day))
    return _replace(state, entries), entry
