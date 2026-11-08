from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import re

from .business_calendar import DAY_TEXT, business_date
from .energy_clock import UNIX_EPOCH, instant, unix_milliseconds
from .errors import ProtocolError
from .native_numbers import int32


LEDGER_KEY = "event_login"
DAILY_LIMIT = "event_login_daily_limit"
MAX_CAMPAIGNS = 4096
MAX_DELIVERIES = 65535
MAX_MAIL_IDS = 65535
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[1-9][0-9]{0,9}\Z")
_REASON = re.compile(r"[a-zA-Z0-9_]{1,128}\Z")
GRANT_FIELDS = frozenset(("campaign_id", "login_count", "purchase_time", "definition",
                         "earned_day", "delivery_day", "mail_ids"))


def _invalid(message="Event login state is invalid"):
    raise ProtocolError("event_login_state_invalid", message, 503)


def _integer(value, minimum=1):
    return int32(value) and value >= minimum


def _definition(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _day(value):
    if type(value) is not str or not DAY_TEXT.fullmatch(value):
        _invalid("Event login dates must be canonical logical days")
    try:
        return date.fromisoformat(value)
    except ValueError:
        _invalid("Event login date does not exist")


def event_login_clock(now):
    try:
        current = instant(now)
        return business_date(current).isoformat(), unix_milliseconds(current)
    except (ValueError, TypeError, OverflowError, ProtocolError) as error:
        raise ProtocolError("event_login_clock_invalid", "Event login requires a representable server instant") from error


def _mail_ids(value):
    return (isinstance(value, list) and 0 < len(value) <= MAX_MAIL_IDS
            and all(_integer(number) for number in value) and len(set(value)) == len(value))


def voided_delivery(delivery):
    return delivery["mail_ids"] == []


def validate_event_login_grants(grants):
    if not isinstance(grants, list) or len(grants) > MAX_CAMPAIGNS:
        _invalid("Event login grants must be a bounded sequence")
    previous, result, seen = 0, [], set()
    for grant in grants:
        if (not isinstance(grant, dict) or set(grant) != GRANT_FIELDS
                or not _integer(grant["campaign_id"]) or grant["campaign_id"] <= previous
                or not _integer(grant["login_count"]) or grant["purchase_time"] != ""
                or not _definition(grant["definition"]) or not _mail_ids(grant["mail_ids"])):
            _invalid("Event login grant is invalid")
        if _day(grant["earned_day"]) > _day(grant["delivery_day"]):
            _invalid("Event login delivery precedes eligibility")
        if seen.intersection(grant["mail_ids"]) or len(result) + len(grant["mail_ids"]) > MAX_MAIL_IDS:
            _invalid("Event login grants reuse or exceed mail identities")
        previous = grant["campaign_id"]
        seen.update(grant["mail_ids"])
        result.extend(grant["mail_ids"])
    return result


def read_event_logins(state):
    if (not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int
            or state["bootstrap_schema"] != 1 or not isinstance(state.get("tables"), dict)):
        _invalid("Event login requires a verified bootstrap state")
    if LEDGER_KEY not in state:
        return {"schema_version": 1, "last_time": None, "campaigns": {}}
    ledger = state[LEDGER_KEY]
    if (not isinstance(ledger, dict) or set(ledger) != {"schema_version", "last_time", "campaigns"}
            or type(ledger["schema_version"]) is not int or ledger["schema_version"] != 1
            or not isinstance(ledger["campaigns"], dict) or len(ledger["campaigns"]) > MAX_CAMPAIGNS):
        _invalid("Event login journal is invalid")
    observed = None
    if ledger["last_time"] is not None:
        if type(ledger["last_time"]) is not int:
            _invalid("Event login clock must be integer milliseconds")
        try:
            observed = _day(event_login_clock(UNIX_EPOCH + timedelta(milliseconds=ledger["last_time"]))[0])
        except (OverflowError, ProtocolError):
            _invalid("Event login clock is outside the supported calendar")
    seen, deliveries = set(), 0
    for key, record in ledger["campaigns"].items():
        if (type(key) is not str or not _ID.fullmatch(key) or not _integer(int(key))
                or not isinstance(record, dict) or set(record) != {"deliveries", "pending"}
                or not isinstance(record["deliveries"], dict) or len(record["deliveries"]) > MAX_DELIVERIES):
            _invalid("Event login campaign history is invalid")
        if set(record["deliveries"]) != {str(number) for number in range(1, len(record["deliveries"]) + 1)}:
            _invalid("Event login reward sequence contains gaps")
        previous_day = None
        for ordinal in range(1, len(record["deliveries"]) + 1):
            delivery = record["deliveries"][str(ordinal)]
            if (not isinstance(delivery, dict)
                    or set(delivery) != {"definition", "earned_day", "delivery_day", "mail_ids"}):
                _invalid("Event login delivery fields are invalid")
            if voided_delivery(delivery):
                if not _definition(delivery["definition"]) or delivery["delivery_day"] != delivery["earned_day"]:
                    _invalid("Voided event login reward is invalid")
                ids = []
            else:
                grant = {"campaign_id": int(key), "login_count": ordinal, "purchase_time": "", **delivery}
                ids = validate_event_login_grants([grant])
            earned, day = _day(delivery["earned_day"]), _day(delivery["delivery_day"])
            if observed is None or day > observed or previous_day is not None and earned <= previous_day:
                _invalid("Event login deliveries violate the daily sequence")
            if seen.intersection(ids):
                _invalid("Event login history reuses mail identities")
            previous_day = day
            seen.update(ids)
            deliveries += 1
            if deliveries > MAX_DELIVERIES or len(seen) > MAX_MAIL_IDS:
                _invalid("Event login history exceeds its storage limit")
        pending = record["pending"]
        if pending is not None:
            if (not isinstance(pending, dict) or set(pending) != {"ordinal", "definition", "earned_day", "reason"}
                    or not _integer(pending["ordinal"]) or pending["ordinal"] != len(record["deliveries"]) + 1
                    or not _definition(pending["definition"])
                    or pending["reason"] is not None and (type(pending["reason"]) is not str or not _REASON.fullmatch(pending["reason"]))):
                _invalid("Pending event login reward is invalid")
            earned = _day(pending["earned_day"])
            if observed is None or earned > observed or previous_day is not None and earned <= previous_day:
                _invalid("Pending event login reward violates its eligible day")
        elif not record["deliveries"]:
            _invalid("Empty event login campaign entries are not persisted")
    return deepcopy(ledger)


def initialize_event_logins(state):
    read_event_logins(state)
    return deepcopy(state)


def _timed(state, now):
    ledger = read_event_logins(state)
    day, timestamp = event_login_clock(now)
    if ledger["last_time"] is not None and timestamp < ledger["last_time"]:
        raise ProtocolError("event_login_clock_backwards", "Event login clock precedes the saved instant", 409)
    return ledger, day, timestamp


def _replace(state, ledger):
    result = deepcopy(state)
    result[LEDGER_KEY] = deepcopy(ledger)
    read_event_logins(result)
    return result


def begin_event_login(state, campaign_id, ordinal, definition, now):
    if not _integer(campaign_id) or not _integer(ordinal) or not _definition(definition):
        _invalid("Event login eligibility requires a verified reward identity")
    ledger, day, timestamp = _timed(state, now)
    record = ledger["campaigns"].get(str(campaign_id), {"deliveries": {}, "pending": None})
    if record["pending"] is not None:
        if (record["pending"]["ordinal"], record["pending"]["definition"]) == (ordinal, definition):
            return deepcopy(state)
        _invalid("Event login already has a different pending reward")
    if ordinal != len(record["deliveries"]) + 1:
        _invalid("Event login eligibility cannot skip reward ordinals")
    if record["deliveries"] and record["deliveries"][str(ordinal - 1)]["delivery_day"] >= day:
        raise ProtocolError(DAILY_LIMIT, "This campaign already delivered a reward on this day", 409)
    record["pending"] = {"ordinal": ordinal, "definition": definition, "earned_day": day, "reason": None}
    ledger["campaigns"][str(campaign_id)] = record
    ledger["last_time"] = timestamp
    return _replace(state, ledger)


def _pending_record(ledger, campaign_id):
    record = ledger["campaigns"].get(str(campaign_id))
    if record is None or record["pending"] is None:
        _invalid("Event login has no eligible pending reward")
    return record


def _close_pending(record, delivery_day, mail_ids):
    pending = record["pending"]
    delivery = {key: pending[key] for key in ("definition", "earned_day")}
    delivery.update(delivery_day=delivery_day, mail_ids=list(mail_ids))
    record["deliveries"][str(pending["ordinal"])] = delivery
    record["pending"] = None
    return pending["ordinal"], delivery


def defer_event_login(state, campaign_id, reason):
    if not _integer(campaign_id) or type(reason) is not str or not _REASON.fullmatch(reason):
        _invalid("Event login pending reason is invalid")
    ledger = read_event_logins(state)
    _pending_record(ledger, campaign_id)["pending"]["reason"] = reason
    return _replace(state, ledger)


def rebind_event_login(state, campaign_id, definition):
    if not _integer(campaign_id) or not _definition(definition):
        _invalid("Event login rebinding requires a verified reward identity")
    ledger = read_event_logins(state)
    _pending_record(ledger, campaign_id)["pending"].update(definition=definition, reason=None)
    return _replace(state, ledger)


def void_event_login(state, campaign_id):
    if not _integer(campaign_id):
        _invalid("Event login voiding requires a positive campaign ID")
    ledger = read_event_logins(state)
    record = _pending_record(ledger, campaign_id)
    # The count stays used, so the next login day earns the next count without mail for this one.
    _close_pending(record, record["pending"]["earned_day"], [])
    return _replace(state, ledger)


def complete_event_login(state, campaign_id, mail_ids, now):
    if not _integer(campaign_id) or not _mail_ids(mail_ids):
        _invalid("Event login delivery requires distinct mail identities")
    ledger, day, timestamp = _timed(state, now)
    ordinal, delivery = _close_pending(_pending_record(ledger, campaign_id), day, mail_ids)
    ledger["last_time"] = timestamp
    result = _replace(state, ledger)
    grant = {"campaign_id": campaign_id, "login_count": ordinal, "purchase_time": "", **delivery}
    return result, grant


def validate_event_login_receipt(state, grants):
    validate_event_login_grants(grants)
    ledger = read_event_logins(state)
    for grant in grants:
        record = ledger["campaigns"].get(str(grant["campaign_id"]))
        delivery = record["deliveries"].get(str(grant["login_count"])) if record else None
        expected = {key: grant[key] for key in ("definition", "earned_day", "delivery_day", "mail_ids")}
        if delivery != expected:
            _invalid("Event login receipt does not match its permanent delivery")
