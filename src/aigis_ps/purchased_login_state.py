from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
import re

from .business_calendar import NATIVE_DATETIME, business_date, wall_clock
from .energy_clock import UNIX_EPOCH, instant, unix_milliseconds
from .errors import ProtocolError
from .event_login_state import voided_delivery
from .native_numbers import int32


LEDGER_KEY = "purchased_login"
DAILY_LIMIT = "purchased_login_daily_limit"
MAX_PURCHASES = 4096
MAX_REWARDS = 366
MAX_MAIL_IDS = 65535
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[1-9][0-9]{0,9}\Z")
_REASON = re.compile(r"[a-zA-Z0-9_]{1,128}\Z")
GRANT_FIELDS = frozenset(("campaign_id", "purchase_id", "login_count", "purchase_time", "definition",
                         "earned_day", "delivery_day", "mail_ids"))


def _invalid(message="Purchased login state is invalid"):
    raise ProtocolError("purchased_login_state_invalid", message, 503)


def _integer(value, minimum=0):
    return int32(value) and value >= minimum


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _text(value):
    try:
        return type(value) is str and 0 < len(value.encode("utf-8")) <= 256
    except UnicodeError:
        return False


def _day(value):
    try:
        if type(value) is not str or (parsed := date.fromisoformat(value)).isoformat() != value:
            raise ValueError("Invalid date")
        return parsed
    except ValueError:
        _invalid("Purchased login dates require canonical calendar days")


def purchased_login_clock(now):
    try:
        current = instant(now)
        return business_date(current).isoformat(), unix_milliseconds(current)
    except (ValueError, TypeError, OverflowError, ProtocolError) as error:
        raise ProtocolError("purchased_login_clock_invalid", "Purchased login requires a representable instant") from error


def _from_timestamp(value):
    if type(value) is not int:
        _invalid("Purchased login timestamps must be integer milliseconds")
    try:
        return wall_clock(UNIX_EPOCH + timedelta(milliseconds=value))
    except (OverflowError, ProtocolError):
        _invalid("Purchased login timestamp exceeds the supported calendar")


def purchased_login_time(value):
    return _from_timestamp(value).strftime(NATIVE_DATETIME)


def _parse_time(value):
    try:
        if type(value) is not str or not value:
            raise ValueError("Missing purchase time")
        parsed = datetime.strptime(value, NATIVE_DATETIME)
        if parsed.strftime(NATIVE_DATETIME) != value:
            raise ValueError("Noncanonical purchase time")
        return parsed
    except ValueError:
        _invalid("PurchaseTime requires a canonical native timestamp")


def purchased_login_identity(campaign_id):
    if not _integer(campaign_id, 1):
        _invalid("Purchased login identity requires a positive campaign ID")
    return hashlib.sha256(json.dumps({"schema_version": 1, "kind": "purchased_login", "campaign_id": campaign_id},
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def purchase_window(purchased_at, source_start, source_end, expiration):
    if not _integer(expiration, 1):
        _invalid("Purchased login duration must be positive")
    try:
        next_day = _day(purchased_login_clock(_from_timestamp(purchased_at))[0]) + timedelta(days=1)
        start = max(next_day, _day(source_start)) if source_start is not None else next_day
        end = next_day + timedelta(days=expiration)
        if source_end is not None:
            end = min(end, _day(source_end))
        return start.isoformat(), end.isoformat()
    except OverflowError:
        _invalid("Purchased login window exceeds the supported calendar")


def _mail_ids(value):
    return (isinstance(value, list) and 0 < len(value) <= MAX_MAIL_IDS
            and all(_integer(number, 1) for number in value) and len(set(value)) == len(value))


def validate_purchased_login_grants(grants):
    if not isinstance(grants, list) or len(grants) > MAX_PURCHASES:
        _invalid("Purchased login grants must be a bounded sequence")
    previous, result, seen, campaigns = None, [], set(), set()
    for grant in grants:
        if (not isinstance(grant, dict) or set(grant) != GRANT_FIELDS
                or not _integer(grant["campaign_id"], 1) or not _integer(grant["purchase_id"], 1)
                or not _integer(grant["login_count"], 1) or not _hash(grant["definition"])
                or not _mail_ids(grant["mail_ids"])):
            _invalid("Purchased login grant fields are invalid")
        _parse_time(grant["purchase_time"])
        order = grant["campaign_id"], grant["purchase_id"]
        if previous is not None and order <= previous or grant["campaign_id"] in campaigns:
            _invalid("Purchased login grants require one ordered instance per campaign")
        if _day(grant["earned_day"]) > _day(grant["delivery_day"]):
            _invalid("Purchased login delivery precedes eligibility")
        if seen.intersection(grant["mail_ids"]) or len(result) + len(grant["mail_ids"]) > MAX_MAIL_IDS:
            _invalid("Purchased login grants reuse or exceed mail identities")
        previous = order
        campaigns.add(grant["campaign_id"])
        seen.update(grant["mail_ids"])
        result.extend(grant["mail_ids"])
    return result


def read_purchased_logins(state):
    if (not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int
            or state["bootstrap_schema"] != 1 or not isinstance(state.get("tables"), dict)):
        _invalid("Purchased login requires a verified bootstrap state")
    if LEDGER_KEY not in state:
        return {"schema_version": 1, "next_purchase_id": 1, "last_time": None, "purchases": {}}
    ledger = state[LEDGER_KEY]
    if (not isinstance(ledger, dict) or set(ledger) != {"schema_version", "next_purchase_id", "last_time", "purchases"}
            or type(ledger["schema_version"]) is not int or ledger["schema_version"] != 1
            or not isinstance(ledger["purchases"], dict) or len(ledger["purchases"]) > MAX_PURCHASES
            or not _integer(ledger["next_purchase_id"], 1) or ledger["next_purchase_id"] > MAX_PURCHASES + 1):
        _invalid("Purchased login journal schema is invalid")
    observed = None if ledger["last_time"] is None else purchased_login_clock(_from_timestamp(ledger["last_time"]))[0]
    ids, requests, mails, days = [], set(), set(), set()
    for key, record in ledger["purchases"].items():
        if type(key) is not str or not _ID.fullmatch(key) or not _integer(int(key), 1):
            _invalid("Purchased login instance identity is invalid")
        purchase_id = int(key)
        ids.append(purchase_id)
        expected = {"campaign_id", "identity", "sku", "package_id", "flag_id", "request", "purchased_at",
                    "purchase_time", "source_start", "source_end", "expiration", "start_day", "end_day",
                    "reward_definitions", "deliveries", "pending"}
        if (not isinstance(record, dict) or set(record) != expected
                or not _integer(record["campaign_id"], 1) or not _integer(record["package_id"], 1)
                or not _integer(record["flag_id"], 1) or not _text(record["sku"])
                or record["identity"] != purchased_login_identity(record["campaign_id"])
                or not _hash(record["request"]) or record["request"] in requests
                or not isinstance(record["reward_definitions"], list)
                or not 1 <= len(record["reward_definitions"]) <= MAX_REWARDS
                or any(not _hash(value) for value in record["reward_definitions"])
                or not isinstance(record["deliveries"], dict)):
            _invalid("Purchased login instance metadata is invalid")
        _from_timestamp(record["purchased_at"])
        window = purchase_window(record["purchased_at"], record["source_start"], record["source_end"], record["expiration"])
        if (record["purchase_time"] != purchased_login_time(record["purchased_at"])
                or (record["start_day"], record["end_day"]) != window or window[0] >= window[1]
                or observed is None or record["purchased_at"] > ledger["last_time"]):
            _invalid("Purchased login instance window is inconsistent")
        requests.add(record["request"])
        count = len(record["deliveries"])
        if count > len(record["reward_definitions"]) or set(record["deliveries"]) != {str(number) for number in range(1, count+1)}:
            _invalid("Purchased login reward sequence contains gaps")
        previous_day = None
        for ordinal in range(1, count+1):
            delivery = record["deliveries"][str(ordinal)]
            if not isinstance(delivery, dict) or set(delivery) != {"definition", "earned_day", "delivery_day", "mail_ids"}:
                _invalid("Purchased login delivery fields are invalid")
            voided = voided_delivery(delivery)
            if voided:
                if delivery["delivery_day"] != _day(delivery["earned_day"]).isoformat():
                    _invalid("Voided purchased login reward is invalid")
                numbers = []
            else:
                grant = {"campaign_id": record["campaign_id"], "purchase_id": purchase_id, "login_count": ordinal,
                         "purchase_time": record["purchase_time"], **delivery}
                numbers = validate_purchased_login_grants([grant])
            earned, day = delivery["earned_day"], delivery["delivery_day"]
            pair = record["campaign_id"], day
            # A voided ordinal mails nothing, so it takes no part in the one delivery per campaign and day.
            if (not record["start_day"] <= earned < record["end_day"] or day > observed
                    or previous_day is not None and earned <= previous_day
                    or delivery["definition"] != record["reward_definitions"][ordinal-1]
                    or mails.intersection(numbers) or not voided and pair in days):
                _invalid("Purchased login delivery history is inconsistent")
            previous_day = day
            if not voided:
                days.add(pair)
            mails.update(numbers)
        pending = record["pending"]
        if pending is not None:
            if (not isinstance(pending, dict) or set(pending) != {"ordinal", "definition", "earned_day", "reason"}
                    or not _integer(pending["ordinal"], 1) or pending["ordinal"] != count+1
                    or pending["ordinal"] > len(record["reward_definitions"])
                    or pending["definition"] != record["reward_definitions"][pending["ordinal"]-1]
                    or pending["reason"] is not None and (type(pending["reason"]) is not str or not _REASON.fullmatch(pending["reason"]))):
                _invalid("Purchased login pending reward is invalid")
            earned = _day(pending["earned_day"]).isoformat()
            if not record["start_day"] <= earned < record["end_day"] or earned > observed or previous_day is not None and earned <= previous_day:
                _invalid("Purchased login pending day is inconsistent")
    if sorted(ids) != list(range(1, ledger["next_purchase_id"])) or len(mails) > MAX_MAIL_IDS:
        _invalid("Purchased login identities are missing or exceed capacity")
    previous_time, previous_campaigns = None, {}
    for identifier in sorted(ids):
        record = ledger["purchases"][str(identifier)]
        if previous_time is not None and record["purchased_at"] < previous_time:
            _invalid("Purchased login acquisition order is inconsistent")
        previous = previous_campaigns.get(record["campaign_id"])
        acquired_day = purchased_login_clock(_from_timestamp(record["purchased_at"]))[0]
        if previous is not None and acquired_day < previous["end_day"]:
            _invalid("Purchased login qualifications cannot overlap")
        previous_time = record["purchased_at"]
        previous_campaigns[record["campaign_id"]] = record
    return deepcopy(ledger)


def initialize_purchased_logins(state):
    read_purchased_logins(state)
    return deepcopy(state)


def _replace(state, ledger):
    result = deepcopy(state)
    result[LEDGER_KEY] = deepcopy(ledger)
    read_purchased_logins(result)
    return result


def _timed(state, now):
    ledger = read_purchased_logins(state)
    day, timestamp = purchased_login_clock(now)
    if ledger["last_time"] is not None and timestamp < ledger["last_time"]:
        raise ProtocolError("purchased_login_clock_backwards", "Purchased login clock precedes its watermark", 409)
    return ledger, day, timestamp


def add_purchased_login(state, entry, now):
    ledger, _, timestamp = _timed(state, now)
    purchase_id = ledger["next_purchase_id"]
    if purchase_id > MAX_PURCHASES:
        _invalid("Purchased login history exceeds capacity")
    ledger["purchases"][str(purchase_id)] = deepcopy(entry)
    ledger.update(next_purchase_id=purchase_id+1, last_time=timestamp)
    return _replace(state, ledger), purchase_id


def purchased_login_delivered_day(ledger, campaign_id, day):
    return any(record["campaign_id"] == campaign_id and any(
        delivery["delivery_day"] == day and not voided_delivery(delivery) for delivery in record["deliveries"].values())
        for record in ledger["purchases"].values())


def purchased_login_settled_day(record, day):
    # A voided ordinal mails nothing, but the next ordinal of its purchase is still earned on a later day.
    deliveries = record["deliveries"]
    return bool(deliveries) and deliveries[str(len(deliveries))]["delivery_day"] >= day


def begin_purchased_reward(state, purchase_id, now):
    ledger, day, timestamp = _timed(state, now)
    record = ledger["purchases"].get(str(purchase_id)) if _integer(purchase_id, 1) else None
    if record is None:
        _invalid("Purchased login instance is unknown")
    if record["pending"] is not None:
        return deepcopy(state)
    if not record["start_day"] <= day < record["end_day"]:
        raise ProtocolError("purchased_login_inactive", "Purchased login window is inactive", 409)
    if purchased_login_delivered_day(ledger, record["campaign_id"], day) or purchased_login_settled_day(record, day):
        raise ProtocolError(DAILY_LIMIT, "This campaign already delivered today", 409)
    ordinal = len(record["deliveries"])+1
    if ordinal > len(record["reward_definitions"]):
        raise ProtocolError("purchased_login_complete", "Purchased login schedule is complete", 409)
    record["pending"] = {"ordinal": ordinal, "definition": record["reward_definitions"][ordinal-1], "earned_day": day, "reason": None}
    ledger["last_time"] = timestamp
    return _replace(state, ledger)


def _pending_purchase(ledger, purchase_id):
    record = ledger["purchases"].get(str(purchase_id)) if _integer(purchase_id, 1) else None
    if record is None or record["pending"] is None:
        _invalid("Purchased login has no earned pending reward")
    return record


def _close_pending(record, delivery_day, mail_ids):
    pending = record["pending"]
    delivery = {key: pending[key] for key in ("definition", "earned_day")}
    delivery.update(delivery_day=delivery_day, mail_ids=list(mail_ids))
    record["deliveries"][str(pending["ordinal"])] = delivery
    record["pending"] = None
    return pending["ordinal"], delivery


def defer_purchased_reward(state, purchase_id, reason):
    ledger = read_purchased_logins(state)
    record = ledger["purchases"].get(str(purchase_id)) if _integer(purchase_id, 1) else None
    if record is None or record["pending"] is None or type(reason) is not str or not _REASON.fullmatch(reason):
        _invalid("Purchased login pending reason is invalid")
    record["pending"]["reason"] = reason
    return _replace(state, ledger)


def rebind_purchased_reward(state, purchase_id, reward_definitions):
    ledger = read_purchased_logins(state)
    record = _pending_purchase(ledger, purchase_id)
    delivered = len(record["deliveries"])
    if (not isinstance(reward_definitions, list) or len(reward_definitions) != len(record["reward_definitions"])
            or any(not _hash(value) for value in reward_definitions)
            or reward_definitions[:delivered] != record["reward_definitions"][:delivered]):
        _invalid("Purchased login rebinding keeps the length and the delivered part of its schedule")
    record["reward_definitions"] = list(reward_definitions)
    record["pending"].update(definition=reward_definitions[record["pending"]["ordinal"]-1], reason=None)
    return _replace(state, ledger)


def void_purchased_reward(state, purchase_id):
    ledger = read_purchased_logins(state)
    record = _pending_purchase(ledger, purchase_id)
    # The ordinal stays used, so the purchase continues with the next one without mail for this one.
    _close_pending(record, record["pending"]["earned_day"], [])
    return _replace(state, ledger)


def complete_purchased_reward(state, purchase_id, mail_ids, now):
    ledger, day, timestamp = _timed(state, now)
    record = ledger["purchases"].get(str(purchase_id)) if _integer(purchase_id, 1) else None
    if record is None or record["pending"] is None or not _mail_ids(mail_ids):
        _invalid("Purchased login completion requires an earned pending reward")
    if purchased_login_delivered_day(ledger, record["campaign_id"], day):
        raise ProtocolError(DAILY_LIMIT, "This campaign already delivered today", 409)
    ordinal, delivery = _close_pending(record, day, mail_ids)
    ledger["last_time"] = timestamp
    result = _replace(state, ledger)
    grant = {"campaign_id": record["campaign_id"], "purchase_id": purchase_id, "login_count": ordinal,
             "purchase_time": record["purchase_time"], **delivery}
    return result, grant


def validate_purchased_login_receipt(state, grants):
    validate_purchased_login_grants(grants)
    ledger = read_purchased_logins(state)
    for grant in grants:
        record = ledger["purchases"].get(str(grant["purchase_id"]))
        expected = {key: grant[key] for key in ("definition", "earned_day", "delivery_day", "mail_ids")}
        if (record is None or record["campaign_id"] != grant["campaign_id"] or record["purchase_time"] != grant["purchase_time"]
                or record["deliveries"].get(str(grant["login_count"])) != expected):
            _invalid("Purchased login receipt differs from its permanent delivery")
