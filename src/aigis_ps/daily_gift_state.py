from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import re

from .business_calendar import business_date
from .energy_clock import UNIX_EPOCH, instant, unix_milliseconds
from .errors import ProtocolError
from .native_numbers import int32


LEDGER_KEY = "daily_gift"
PENDING, DELIVERED = "pending", "delivered"
MAX_GIFTS = 4096
MAX_ENTRIES = 65535
MAX_MAIL_IDS = 65535
MAX_GRANT_MAIL_IDS = 500
GRANT_FIELDS = frozenset(("gift_id", "definition", "earned_day", "delivery_day", "mail_ids"))
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[1-9][0-9]{0,9}\Z")
_REASON = re.compile(r"[a-zA-Z0-9_]{1,128}\Z")


def _invalid(message="Daily gift state is invalid"):
    raise ProtocolError("daily_gift_state_invalid", message, 503)


def _positive(value):
    return int32(value) and value >= 1


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _day(value):
    try:
        if type(value) is not str or (parsed := date.fromisoformat(value)).isoformat() != value:
            raise ValueError("Noncanonical day")
        return parsed
    except ValueError:
        _invalid("Daily gift dates require canonical calendar days")


def daily_gift_clock(now):
    try:
        current = instant(now)
        return business_date(current).isoformat(), unix_milliseconds(current)
    except (ValueError, TypeError, OverflowError, ProtocolError) as error:
        raise ProtocolError("daily_gift_clock_invalid", "Daily gifts require a representable instant") from error


def _mail_ids(value):
    return (isinstance(value, list) and 0 < len(value) <= MAX_GRANT_MAIL_IDS
            and all(_positive(number) for number in value) and len(set(value)) == len(value))


def validate_daily_gift_grants(grants):
    if not isinstance(grants, list) or len(grants) > MAX_GRANT_MAIL_IDS:
        _invalid("Daily gift grants must be a bounded sequence")
    previous, ids = None, []
    for grant in grants:
        if (not isinstance(grant, dict) or set(grant) != GRANT_FIELDS or not _positive(grant["gift_id"])
                or not _hash(grant["definition"]) or not _mail_ids(grant["mail_ids"])):
            _invalid("Daily gift grant fields are invalid")
        earned, delivered = _day(grant["earned_day"]), _day(grant["delivery_day"])
        order = grant["gift_id"], earned
        if earned > delivered or previous is not None and order <= previous:
            _invalid("Daily gift grants are unordered or precede their eligibility")
        previous = order
        ids.extend(grant["mail_ids"])
    if len(ids) > MAX_GRANT_MAIL_IDS or len(set(ids)) != len(ids):
        _invalid("Daily gift grants reuse or exceed mail identities")
    return ids


def read_daily_gifts(state):
    if (not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int
            or state["bootstrap_schema"] != 1 or not isinstance(state.get("tables"), dict)):
        _invalid("Daily gifts require a verified bootstrap state")
    if LEDGER_KEY not in state:
        return {"schema_version": 1, "last_time": None, "gifts": {}}
    ledger = state[LEDGER_KEY]
    if (not isinstance(ledger, dict) or set(ledger) != {"schema_version", "last_time", "gifts"}
            or type(ledger["schema_version"]) is not int or ledger["schema_version"] != 1
            or not isinstance(ledger["gifts"], dict) or len(ledger["gifts"]) > MAX_GIFTS):
        _invalid("Daily gift journal schema is invalid")
    observed = None
    if ledger["last_time"] is not None:
        if type(ledger["last_time"]) is not int:
            _invalid("Daily gift watermark must use integer milliseconds")
        try:
            observed = daily_gift_clock(UNIX_EPOCH + timedelta(milliseconds=ledger["last_time"]))[0]
        except (OverflowError, ProtocolError):
            _invalid("Daily gift watermark exceeds the supported calendar")
    total, mail_ids = 0, set()
    for key, records in ledger["gifts"].items():
        if (type(key) is not str or not _ID.fullmatch(key) or not _positive(int(key))
                or not isinstance(records, dict) or not records or observed is None):
            _invalid("Daily gift identities or record groups are invalid")
        total += len(records)
        if total > MAX_ENTRIES:
            _invalid("Daily gift journal exceeds its record capacity")
        for earned_day, record in records.items():
            earned = _day(earned_day)
            fields = {"definition", "window_start", "window_end", "status", "reason", "delivery_day", "mail_ids"}
            if not isinstance(record, dict) or set(record) != fields or not _hash(record["definition"]):
                _invalid("Daily gift record fields are invalid")
            if not _day(record["window_start"]) <= earned <= _day(record["window_end"]) or earned_day > observed:
                _invalid("Daily gift eligibility is outside its recorded window")
            if record["status"] == PENDING:
                if (record["delivery_day"] is not None or record["mail_ids"] != []
                        or record["reason"] is not None and (type(record["reason"]) is not str
                            or not _REASON.fullmatch(record["reason"]))):
                    _invalid("Pending daily gift has invalid delivery fields")
            elif record["status"] == DELIVERED:
                delivered = _day(record["delivery_day"])
                if (record["reason"] is not None or not earned <= delivered <= _day(observed)
                        or not _mail_ids(record["mail_ids"]) or mail_ids.intersection(record["mail_ids"])):
                    _invalid("Delivered daily gift has invalid mail identities or dates")
                mail_ids.update(record["mail_ids"])
                if len(mail_ids) > MAX_MAIL_IDS:
                    _invalid("Daily gift journal exceeds its mail identity capacity")
            else:
                _invalid("Daily gift status is unknown")
    if not ledger["gifts"] and ledger["last_time"] is not None:
        _invalid("An empty daily gift journal cannot have a watermark")
    return deepcopy(ledger)


def initialize_daily_gifts(state):
    read_daily_gifts(state)
    return deepcopy(state)


def _timed(state, now):
    ledger = read_daily_gifts(state)
    day, timestamp = daily_gift_clock(now)
    if ledger["last_time"] is not None and timestamp < ledger["last_time"]:
        raise ProtocolError("daily_gift_clock_backwards", "Daily gift clock precedes its watermark", 409)
    return ledger, day, timestamp


def _replace(state, ledger):
    updated = deepcopy(state)
    updated[LEDGER_KEY] = ledger
    read_daily_gifts(updated)
    return updated


def begin_daily_gift(state, gift_id, definition, window_start, window_end, now):
    if not _positive(gift_id) or not _hash(definition):
        _invalid("Daily gift eligibility requires a stable source")
    ledger, day, timestamp = _timed(state, now)
    if not _day(window_start) <= _day(day) <= _day(window_end):
        _invalid("Daily gift eligibility is outside its current window")
    key = str(gift_id)
    if day in ledger["gifts"].get(key, {}):
        return deepcopy(state)
    if ((key not in ledger["gifts"] and len(ledger["gifts"]) >= MAX_GIFTS)
            or sum(len(records) for records in ledger["gifts"].values()) >= MAX_ENTRIES):
        raise ProtocolError("daily_gift_journal_capacity", "Daily gift journal capacity is exhausted", 409)
    ledger["gifts"].setdefault(key, {})[day] = {"definition": definition, "window_start": window_start,
        "window_end": window_end, "status": PENDING, "reason": None, "delivery_day": None, "mail_ids": []}
    ledger["last_time"] = timestamp
    return _replace(state, ledger)


def defer_daily_gift(state, gift_id, earned_day, reason):
    if not _positive(gift_id) or type(reason) is not str or not _REASON.fullmatch(reason):
        _invalid("Daily gift pending reason is invalid")
    _day(earned_day)
    ledger = read_daily_gifts(state)
    record = ledger["gifts"].get(str(gift_id), {}).get(earned_day)
    if record is None or record["status"] != PENDING:
        _invalid("Daily gift has no matching pending eligibility")
    record["reason"] = reason
    return _replace(state, ledger)


def complete_daily_gift(state, gift_id, earned_day, mail_ids, now):
    if not _positive(gift_id) or not _mail_ids(mail_ids):
        _invalid("Daily gift delivery requires distinct mail identities")
    _day(earned_day)
    ledger, day, timestamp = _timed(state, now)
    record = ledger["gifts"].get(str(gift_id), {}).get(earned_day)
    if record is None or record["status"] != PENDING:
        _invalid("Daily gift has no matching pending eligibility")
    if sum(len(value["mail_ids"]) for records in ledger["gifts"].values() for value in records.values()) + len(mail_ids) > MAX_MAIL_IDS:
        raise ProtocolError("daily_gift_journal_capacity", "Daily gift mail identity capacity is exhausted", 409)
    record.update(status=DELIVERED, reason=None, delivery_day=day, mail_ids=list(mail_ids))
    ledger["last_time"] = timestamp
    updated = _replace(state, ledger)
    grant = {"gift_id": gift_id, "definition": record["definition"], "earned_day": earned_day,
             "delivery_day": day, "mail_ids": list(mail_ids)}
    return updated, grant


def validate_daily_gift_receipt(state, grants):
    validate_daily_gift_grants(grants)
    ledger = read_daily_gifts(state)
    for grant in grants:
        record = ledger["gifts"].get(str(grant["gift_id"]), {}).get(grant["earned_day"])
        if (record is None or record["status"] != DELIVERED
                or any(record[key] != grant[key] for key in ("definition", "delivery_day", "mail_ids"))):
            _invalid("Daily gift receipt differs from its permanent delivery")
