from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import re

from .business_calendar import NATIVE_DATETIME, business_date, wall_clock
from .energy_clock import UNIX_EPOCH, instant, unix_milliseconds
from .errors import ProtocolError
from .event_login_state import voided_delivery
from .native_numbers import int32


LEDGER_KEY = "comeback_login"
DAILY_LIMIT = "comeback_login_daily_limit"
MAX_ROUNDS = 4096
MAX_REWARDS = 366
MAX_DELIVERIES = 65535
MAX_MAIL_IDS = 65535
DATE_FIELD = "LatestComebackLoginBonusDateTime"
DAYS_FIELD = "LatestComebackLoginBonusDays"
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[1-9][0-9]{0,9}\Z")
_REASON = re.compile(r"[a-zA-Z0-9_]{1,128}\Z")
GRANT_FIELDS = frozenset(("campaign_id", "round_id", "login_count", "purchase_time", "definition",
                         "earned_day", "delivery_day", "mail_ids", "notify"))


def _invalid(message="Comeback login state is invalid"):
    raise ProtocolError("comeback_login_state_invalid", message, 503)


def _integer(value, minimum=0):
    return int32(value) and value >= minimum


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _day(value):
    if type(value) is not str:
        _invalid("Comeback dates must be canonical logical days")
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("Noncanonical date")
        return parsed
    except ValueError:
        _invalid("Comeback date does not exist")


def comeback_clock(now):
    try:
        current = instant(now)
        return business_date(current).isoformat(), unix_milliseconds(current)
    except (ValueError, TypeError, OverflowError, ProtocolError) as error:
        raise ProtocolError("comeback_login_clock_invalid", "Comeback login requires a representable instant") from error


def _from_timestamp(value):
    if type(value) is not int:
        _invalid("Comeback timestamps must be integer milliseconds")
    try:
        return wall_clock(UNIX_EPOCH + timedelta(milliseconds=value))
    except (OverflowError, ProtocolError):
        _invalid("Comeback timestamp exceeds the supported calendar")


def _native_timestamp(value):
    return _from_timestamp(value).strftime(NATIVE_DATETIME)


def _projection(state):
    if (not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int
            or state["bootstrap_schema"] != 1 or not isinstance(state.get("tables"), dict)):
        _invalid("Comeback login requires a verified bootstrap state")
    rows = state["tables"].get("game_param2")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        _invalid("Comeback login requires one game parameter row")
    text, days = rows[0].get(DATE_FIELD), rows[0].get(DAYS_FIELD)
    if type(text) is not str or not _integer(days):
        _invalid("Comeback native projection has invalid types")
    if text:
        try:
            parsed = datetime.strptime(text, NATIVE_DATETIME)
            if parsed.strftime(NATIVE_DATETIME) != text:
                raise ValueError("Noncanonical datetime")
        except ValueError:
            _invalid("Comeback native date is not canonical")
    return {"date_time": text, "days": days}


def _mail_ids(value):
    return (isinstance(value, list) and 0 < len(value) <= MAX_MAIL_IDS
            and all(_integer(number, 1) for number in value) and len(set(value)) == len(value))


def validate_comeback_login_grants(grants):
    if not isinstance(grants, list) or len(grants) > MAX_ROUNDS:
        _invalid("Comeback grants must be a bounded sequence")
    previous, result, seen, campaigns = None, [], set(), set()
    for grant in grants:
        if (not isinstance(grant, dict) or set(grant) != GRANT_FIELDS
                or not _integer(grant["campaign_id"], 1) or not _integer(grant["round_id"], 1)
                or not _integer(grant["login_count"], 1) or type(grant["notify"]) is not bool
                or grant["purchase_time"] != "" or not _hash(grant["definition"])
                or not _mail_ids(grant["mail_ids"])):
            _invalid("Comeback grant fields are invalid")
        order = grant["campaign_id"], grant["round_id"]
        if previous is not None and order <= previous or grant["campaign_id"] in campaigns:
            _invalid("Comeback grants must have one ordered round per campaign")
        if _day(grant["earned_day"]) > _day(grant["delivery_day"]):
            _invalid("Comeback delivery precedes eligibility")
        if seen.intersection(grant["mail_ids"]) or len(result) + len(grant["mail_ids"]) > MAX_MAIL_IDS:
            _invalid("Comeback grants reuse or exceed mail identities")
        previous = order
        campaigns.add(grant["campaign_id"])
        seen.update(grant["mail_ids"])
        result.extend(grant["mail_ids"])
    return result


def read_comeback_logins(state):
    projection = _projection(state)
    if LEDGER_KEY not in state:
        return None
    ledger = state[LEDGER_KEY]
    fields = {"schema_version", "legacy_projection", "last_seen_day", "last_seen_at", "last_time",
              "next_round_id", "current_round_id", "rounds"}
    if (not isinstance(ledger, dict) or set(ledger) != fields
            or type(ledger["schema_version"]) is not int or ledger["schema_version"] != 1
            or not isinstance(ledger["legacy_projection"], dict)
            or set(ledger["legacy_projection"]) != {"date_time", "days"}
            or not isinstance(ledger["rounds"], dict) or len(ledger["rounds"]) > MAX_ROUNDS
            or not _integer(ledger["next_round_id"], 1) or ledger["next_round_id"] > MAX_ROUNDS + 1):
        _invalid("Comeback journal schema is invalid")
    _projection({"bootstrap_schema": 1, "tables": {"game_param2": [{
        DATE_FIELD: ledger["legacy_projection"]["date_time"], DAYS_FIELD: ledger["legacy_projection"]["days"]}]}})
    seen_day = _day(ledger["last_seen_day"])
    if comeback_clock(_from_timestamp(ledger["last_seen_at"]))[0] != seen_day.isoformat():
        _invalid("Comeback observation day and timestamp disagree")
    observed = _day(comeback_clock(_from_timestamp(ledger["last_time"]))[0])
    if ledger["last_seen_at"] > ledger["last_time"]:
        _invalid("Comeback observation exceeds its clock watermark")
    seen_mail, seen_days, total = set(), set(), 0
    ids = []
    for key, record in ledger["rounds"].items():
        if type(key) is not str or not _ID.fullmatch(key) or not _integer(int(key), 1):
            _invalid("Comeback round identity is invalid")
        round_id = int(key)
        ids.append(round_id)
        expected = {"campaign_id", "pause_period", "absence_days", "started_at", "start_day", "end_day",
                    "reward_definitions", "deliveries", "pending"}
        if (not isinstance(record, dict) or set(record) != expected
                or not _integer(record["campaign_id"], 1) or not _integer(record["pause_period"])
                or not _integer(record["absence_days"], 1) or record["pause_period"] > record["absence_days"]
                or not isinstance(record["reward_definitions"], list)
                or not 1 <= len(record["reward_definitions"]) <= MAX_REWARDS
                or any(not _hash(value) for value in record["reward_definitions"])
                or not isinstance(record["deliveries"], dict)):
            _invalid("Comeback round metadata is invalid")
        start, end = _day(record["start_day"]), _day(record["end_day"])
        started_day = comeback_clock(_from_timestamp(record["started_at"]))[0]
        if start >= end or record["started_at"] > ledger["last_seen_at"] or started_day != start.isoformat():
            _invalid("Comeback round window is invalid")
        count = len(record["deliveries"])
        if count > len(record["reward_definitions"]) or set(record["deliveries"]) != {str(number) for number in range(1, count + 1)}:
            _invalid("Comeback delivery sequence contains gaps")
        previous_day = None
        for ordinal in range(1, count + 1):
            delivery = record["deliveries"][str(ordinal)]
            if not isinstance(delivery, dict) or set(delivery) != {"definition", "earned_day", "delivery_day", "mail_ids", "notify"}:
                _invalid("Comeback delivery fields are invalid")
            voided = voided_delivery(delivery)
            if voided:
                if delivery["notify"] is not False or delivery["delivery_day"] != delivery["earned_day"]:
                    _invalid("Voided comeback reward is invalid")
                mail = []
            else:
                grant = {"campaign_id": record["campaign_id"], "round_id": round_id, "login_count": ordinal,
                         "purchase_time": "", **delivery}
                mail = validate_comeback_login_grants([grant])
            earned, day = _day(delivery["earned_day"]), _day(delivery["delivery_day"])
            identity = record["campaign_id"], day
            # A voided ordinal mails nothing, so it takes no part in the one delivery per campaign and day.
            if (not start <= earned < end or day > observed or previous_day is not None and earned <= previous_day
                    or delivery["definition"] != record["reward_definitions"][ordinal - 1]
                    or seen_mail.intersection(mail) or not voided and identity in seen_days):
                _invalid("Comeback delivery history is inconsistent")
            previous_day = day
            if not voided:
                seen_days.add(identity)
            seen_mail.update(mail)
            total += 1
        pending = record["pending"]
        if pending is not None:
            if (not isinstance(pending, dict) or set(pending) != {"ordinal", "definition", "earned_day", "reason"}
                    or not _integer(pending["ordinal"], 1) or pending["ordinal"] != count + 1
                    or pending["ordinal"] > len(record["reward_definitions"])
                    or pending["definition"] != record["reward_definitions"][pending["ordinal"] - 1]
                    or pending["reason"] is not None and (type(pending["reason"]) is not str or not _REASON.fullmatch(pending["reason"]))):
                _invalid("Comeback pending reward is invalid")
            earned = _day(pending["earned_day"])
            if not start <= earned < end or earned > observed or previous_day is not None and earned <= previous_day:
                _invalid("Comeback pending eligibility day is invalid")
    if total > MAX_DELIVERIES or len(seen_mail) > MAX_MAIL_IDS:
        _invalid("Comeback history exceeds its capacity")
    if sorted(ids) != list(range(1, ledger["next_round_id"])):
        _invalid("Comeback round sequence cannot discard past identities")
    for previous, current in zip(sorted(ids), sorted(ids)[1:]):
        if ledger["rounds"][str(previous)]["end_day"] > ledger["rounds"][str(current)]["start_day"]:
            _invalid("Comeback rounds cannot have overlapping eligibility windows")
    current = ledger["current_round_id"]
    if not ids:
        if current is not None or projection != ledger["legacy_projection"]:
            _invalid("Comeback baseline projection changed without a round")
    else:
        if type(current) is not int or current != max(ids):
            _invalid("Comeback current round is not the latest round")
        record = ledger["rounds"][str(current)]
        if projection != {"date_time": _native_timestamp(record["started_at"]), "days": record["absence_days"]}:
            _invalid("Comeback native projection differs from its current round")
    return deepcopy(ledger)


def initialize_comeback_logins(state):
    read_comeback_logins(state)
    return deepcopy(state)


def _replace(state, ledger):
    result = deepcopy(state)
    result[LEDGER_KEY] = deepcopy(ledger)
    current = ledger["current_round_id"]
    if current is not None:
        record = ledger["rounds"][str(current)]
        result["tables"]["game_param2"][0].update({DATE_FIELD: _native_timestamp(record["started_at"]),
                                                  DAYS_FIELD: record["absence_days"]})
    read_comeback_logins(result)
    return result


def _timed(state, now):
    ledger = read_comeback_logins(state)
    day, timestamp = comeback_clock(now)
    if ledger is None:
        _invalid("Comeback observation baseline is required")
    if timestamp < ledger["last_time"]:
        raise ProtocolError("comeback_login_clock_backwards", "Comeback clock precedes the saved instant", 409)
    return ledger, day, timestamp


def observe_comeback_login(state, now):
    ledger = read_comeback_logins(state)
    day, timestamp = comeback_clock(now)
    if ledger is None:
        ledger = {"schema_version": 1, "legacy_projection": _projection(state), "last_seen_day": day,
                  "last_seen_at": timestamp, "last_time": timestamp, "next_round_id": 1,
                  "current_round_id": None, "rounds": {}}
        return _replace(state, ledger), None
    if timestamp < ledger["last_time"]:
        raise ProtocolError("comeback_login_clock_backwards", "Comeback clock precedes the saved instant", 409)
    if day == ledger["last_seen_day"]:
        # Eligibility counts JST04 days, so another login on the same day has nothing to record.
        return deepcopy(state), 0
    gap = (_day(day) - _day(ledger["last_seen_day"])).days
    ledger.update(last_seen_day=day, last_seen_at=timestamp, last_time=timestamp)
    return _replace(state, ledger), gap


def begin_comeback_round(state, now, *, campaign_id, pause_period, absence_days, expiration, reward_definitions):
    ledger, day, timestamp = _timed(state, now)
    if (not _integer(campaign_id, 1) or not _integer(pause_period) or not _integer(absence_days, 1)
            or pause_period > absence_days or not _integer(expiration, 1)
            or not isinstance(reward_definitions, list) or not 1 <= len(reward_definitions) <= MAX_REWARDS
            or any(not _hash(value) for value in reward_definitions)):
        _invalid("Comeback round requires a verified reward schedule")
    if ledger["last_seen_at"] != timestamp:
        _invalid("Comeback round must originate in the current observation")
    current = ledger["current_round_id"]
    if current is not None and _day(day) < _day(ledger["rounds"][str(current)]["end_day"]):
        raise ProtocolError("comeback_login_round_active", "The previous comeback round has not ended", 409)
    round_id = ledger["next_round_id"]
    if round_id > MAX_ROUNDS:
        _invalid("Comeback round history exceeds its capacity")
    try:
        return_date = _from_timestamp(timestamp).date()
        end = (return_date + timedelta(days=expiration)).isoformat()
    except OverflowError:
        _invalid("Comeback round expiration exceeds the calendar")
    ledger["rounds"][str(round_id)] = {"campaign_id": campaign_id, "pause_period": pause_period,
        "absence_days": absence_days, "started_at": timestamp, "start_day": day, "end_day": end,
        "reward_definitions": list(reward_definitions), "deliveries": {}, "pending": None}
    ledger.update(next_round_id=round_id + 1, current_round_id=round_id, last_time=timestamp)
    return _replace(state, ledger)


def delivered_comeback_day(ledger, campaign_id, day):
    return any(record["campaign_id"] == campaign_id and any(
        delivery["delivery_day"] == day and not voided_delivery(delivery) for delivery in record["deliveries"].values())
        for record in ledger["rounds"].values())


def settled_comeback_day(record, day):
    # A voided ordinal mails nothing, but the next ordinal of its round is still earned on a later day.
    deliveries = record["deliveries"]
    return bool(deliveries) and deliveries[str(len(deliveries))]["delivery_day"] >= day


def begin_comeback_reward(state, round_id, now):
    ledger, day, timestamp = _timed(state, now)
    record = ledger["rounds"].get(str(round_id)) if _integer(round_id, 1) else None
    if record is None:
        _invalid("Comeback reward round does not exist")
    if record["pending"] is not None:
        return deepcopy(state)
    if ledger["current_round_id"] != round_id or not record["start_day"] <= day < record["end_day"]:
        raise ProtocolError("comeback_login_round_inactive", "Comeback reward eligibility window has ended", 409)
    if delivered_comeback_day(ledger, record["campaign_id"], day) or settled_comeback_day(record, day):
        raise ProtocolError(DAILY_LIMIT, "This campaign already delivered a reward today", 409)
    ordinal = len(record["deliveries"]) + 1
    if ordinal > len(record["reward_definitions"]):
        raise ProtocolError("comeback_login_complete", "This comeback reward schedule is complete", 409)
    record["pending"] = {"ordinal": ordinal, "definition": record["reward_definitions"][ordinal - 1],
                          "earned_day": day, "reason": None}
    ledger["last_time"] = timestamp
    return _replace(state, ledger)


def _pending_round(ledger, round_id):
    record = ledger["rounds"].get(str(round_id)) if ledger is not None and _integer(round_id, 1) else None
    if record is None or record["pending"] is None:
        _invalid("Comeback round has no pending reward")
    return record


def _close_pending(record, delivery_day, mail_ids, notify):
    pending = record["pending"]
    delivery = {key: pending[key] for key in ("definition", "earned_day")}
    delivery.update(delivery_day=delivery_day, mail_ids=list(mail_ids), notify=notify)
    record["deliveries"][str(pending["ordinal"])] = delivery
    record["pending"] = None
    return pending["ordinal"], delivery


def defer_comeback_reward(state, round_id, reason):
    ledger = read_comeback_logins(state)
    if ledger is None or not _integer(round_id, 1) or type(reason) is not str or not _REASON.fullmatch(reason):
        _invalid("Comeback pending reason is invalid")
    _pending_round(ledger, round_id)["pending"]["reason"] = reason
    return _replace(state, ledger)


def rebind_comeback_reward(state, round_id, reward_definitions):
    ledger = read_comeback_logins(state)
    record = _pending_round(ledger, round_id)
    delivered = len(record["deliveries"])
    if (not isinstance(reward_definitions, list) or len(reward_definitions) != len(record["reward_definitions"])
            or any(not _hash(value) for value in reward_definitions)
            or reward_definitions[:delivered] != record["reward_definitions"][:delivered]):
        _invalid("Comeback rebinding keeps the length and the delivered part of its schedule")
    record["reward_definitions"] = list(reward_definitions)
    record["pending"].update(definition=reward_definitions[record["pending"]["ordinal"] - 1], reason=None)
    return _replace(state, ledger)


def void_comeback_reward(state, round_id):
    ledger = read_comeback_logins(state)
    record = _pending_round(ledger, round_id)
    # The ordinal stays used, so the round continues with the next one without mail for this one.
    _close_pending(record, record["pending"]["earned_day"], [], False)
    return _replace(state, ledger)


def complete_comeback_reward(state, round_id, mail_ids, now):
    ledger, day, timestamp = _timed(state, now)
    record = ledger["rounds"].get(str(round_id)) if _integer(round_id, 1) else None
    if record is None or record["pending"] is None or not _mail_ids(mail_ids):
        _invalid("Comeback completion requires a pending reward and distinct mail IDs")
    if delivered_comeback_day(ledger, record["campaign_id"], day):
        raise ProtocolError(DAILY_LIMIT, "This campaign already delivered a reward today", 409)
    ordinal, delivery = _close_pending(record, day, mail_ids, round_id == ledger["current_round_id"])
    ledger["last_time"] = timestamp
    result = _replace(state, ledger)
    grant = {"campaign_id": record["campaign_id"], "round_id": round_id, "login_count": ordinal,
             "purchase_time": "", **delivery}
    return result, grant


def validate_comeback_login_receipt(state, grants):
    validate_comeback_login_grants(grants)
    ledger = read_comeback_logins(state)
    if ledger is None and grants:
        _invalid("Comeback receipt has no permanent journal")
    for grant in grants:
        record = ledger["rounds"].get(str(grant["round_id"]))
        delivery = record["deliveries"].get(str(grant["login_count"])) if record else None
        expected = {key: grant[key] for key in ("definition", "earned_day", "delivery_day", "mail_ids", "notify")}
        if record is None or record["campaign_id"] != grant["campaign_id"] or delivery != expected:
            _invalid("Comeback receipt differs from its permanent delivery")
