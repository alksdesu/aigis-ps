from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import re

from .energy_clock import UNIX_EPOCH, unix_milliseconds
from .errors import ProtocolError
from .letstd_rules import GROUPS, PARTNER_ORDER, PARTNERS
from .native_numbers import int32
from .presents import MAX_PRESENTS


LEDGER_KEY = "letstd"
SCHEMA_VERSION = 1
SOURCE = "local_simulation"
SOURCES = (115, 116)
SCHEDULED, PENDING, ENDED, DELIVERED = "scheduled", "pending", "ended", "delivered"
MAX_LEDGER_ROWS = 65535
MAX_MAIL_IDS = 65535
LEDGER_FIELDS = frozenset(("schema_version", "last_time", "partners"))
GROUP_FIELDS = frozenset(("source", "armed_at", "rearmed_at", "rules_version", "resource_sha256", "rows"))
ROW_FIELDS = frozenset(("definition", "start_at", "end_at", "status", "reason", "dependency_version",
                        "mail_ids", "delivered_at", "via", "popup", "needs_adaptation"))
SPEC_FIELDS = frozenset(("reward_id", "definition", "start_at", "end_at"))
GRANT_FIELDS = frozenset(("partner", "group", "reward_id", "definition", "delivered_at", "via", "popup", "mail_ids"))
_GROUP_KEYS = frozenset(str(group) for group in GROUPS)
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[1-9][0-9]{0,9}\Z")
_REASON = re.compile(r"[a-zA-Z0-9_]{1,128}\Z")


def _invalid(message="LetsTD state is invalid"):
    raise ProtocolError("letstd_state_invalid", message, 503)


def _positive(value):
    return int32(value) and value >= 1


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _timestamp(value):
    if type(value) is not int:
        return False
    try:
        UNIX_EPOCH + timedelta(milliseconds=value)
    except OverflowError:
        return False
    return True


def _mail_ids(value):
    return (isinstance(value, list) and 0 < len(value) <= MAX_PRESENTS
            and all(_positive(number) for number in value)
            and all(earlier < later for earlier, later in zip(value, value[1:])))


def letstd_clock(now):
    try:
        return unix_milliseconds(now)
    except (ValueError, TypeError, OverflowError, ProtocolError) as error:
        raise ProtocolError("letstd_clock_invalid", "LetsTD requires a representable instant") from error


def _group_key(partner, group):
    if partner not in PARTNERS or type(group) is not int or group not in GROUPS:
        _invalid("LetsTD partner or reward group is unknown")
    return str(group)


def _check_group(record, last):
    if (not isinstance(record, dict) or set(record) != GROUP_FIELDS or record["source"] != SOURCE
            or not _hash(record["rules_version"]) or not _hash(record["resource_sha256"])
            or not _timestamp(record["armed_at"]) or record["armed_at"] > last
            or record["rearmed_at"] is not None and (not _timestamp(record["rearmed_at"])
                or not record["armed_at"] <= record["rearmed_at"] <= last)
            or not isinstance(record["rows"], dict)):
        _invalid("LetsTD reward group record is invalid")


def _check_row(key, row, armed_at, last):
    if type(key) is not str or not _ID.fullmatch(key) or not _positive(int(key)):
        _invalid("LetsTD reward identities must be canonical positive integers")
    if (not isinstance(row, dict) or set(row) != ROW_FIELDS or not _hash(row["definition"])
            or not _timestamp(row["start_at"]) or row["end_at"] is not None and not _timestamp(row["end_at"])
            or type(row["needs_adaptation"]) is not bool):
        _invalid("LetsTD reward fields are invalid")
    status = row["status"]
    if status == DELIVERED:
        if (row["reason"] is not None or row["dependency_version"] is not None or not _mail_ids(row["mail_ids"])
                or not _timestamp(row["delivered_at"])
                or not max(armed_at, row["start_at"]) <= row["delivered_at"] <= last
                or type(row["via"]) is not int or row["via"] not in SOURCES or type(row["popup"]) is not bool):
            _invalid("Delivered LetsTD reward has invalid delivery fields")
        return
    if (row["mail_ids"] != [] or row["delivered_at"] is not None or row["via"] is not None
            or row["popup"] is not None or row["needs_adaptation"]):
        _invalid("Undelivered LetsTD reward carries delivery fields")
    if status == PENDING:
        reason, version = row["reason"], row["dependency_version"]
        if (row["start_at"] > last or (reason is None) != (version is None) or reason is not None
                and (type(reason) is not str or not _REASON.fullmatch(reason) or not _hash(version))):
            _invalid("Pending LetsTD reward has an invalid deferral")
    elif status in (SCHEDULED, ENDED):
        if (row["reason"] is not None or row["dependency_version"] is not None
                or status == ENDED and (row["end_at"] is None or row["end_at"] >= last)):
            _invalid("Scheduled or ended LetsTD reward has invalid fields")
    else:
        _invalid("LetsTD reward status is unknown")


def read_letstd(state):
    if (not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int
            or state["bootstrap_schema"] != 1 or not isinstance(state.get("tables"), dict)):
        _invalid("LetsTD requires a verified bootstrap state")
    if LEDGER_KEY not in state:
        return {"schema_version": SCHEMA_VERSION, "last_time": None, "partners": {}}
    ledger = state[LEDGER_KEY]
    if (not isinstance(ledger, dict) or set(ledger) != LEDGER_FIELDS
            or type(ledger["schema_version"]) is not int or ledger["schema_version"] != SCHEMA_VERSION
            or not _timestamp(ledger["last_time"]) or not isinstance(ledger["partners"], dict)
            or not ledger["partners"] or not set(ledger["partners"]) <= set(PARTNERS)):
        _invalid("LetsTD ledger schema is invalid")
    last, rows, mails = ledger["last_time"], 0, set()
    for groups in ledger["partners"].values():
        if not isinstance(groups, dict) or not groups or not set(groups) <= _GROUP_KEYS:
            _invalid("LetsTD partner groups are invalid")
        for record in groups.values():
            _check_group(record, last)
            rows += len(record["rows"])
            if rows > MAX_LEDGER_ROWS:
                _invalid("LetsTD ledger exceeds its row capacity")
            for key, row in record["rows"].items():
                _check_row(key, row, record["armed_at"], last)
                if mails.intersection(row["mail_ids"]):
                    _invalid("LetsTD ledger reuses mail identities")
                mails.update(row["mail_ids"])
                if len(mails) > MAX_MAIL_IDS:
                    _invalid("LetsTD ledger exceeds its mail identity capacity")
    return deepcopy(ledger)


def initialize_letstd(state):
    read_letstd(state)
    return deepcopy(state)


def group_record(ledger, partner, group):
    key = _group_key(partner, group)
    return ledger["partners"].get(partner, {}).get(key)


def _armed(ledger, partner, group):
    record = group_record(ledger, partner, group)
    if record is None:
        raise ProtocolError("letstd_group_unarmed", "This LetsTD reward group has not been armed", 409)
    return record


def _pending(ledger, partner, group, reward_id):
    record = group_record(ledger, partner, group)
    row = None if record is None or not _positive(reward_id) else record["rows"].get(str(reward_id))
    if row is None or row["status"] != PENDING:
        _invalid("LetsTD reward has no matching pending eligibility")
    return row


def _replace(state, ledger):
    updated = deepcopy(state)
    updated[LEDGER_KEY] = ledger
    read_letstd(updated)
    return updated


def letstd_time(ledger, now):
    timestamp = letstd_clock(now)
    if ledger["last_time"] is not None and timestamp < ledger["last_time"]:
        raise ProtocolError("letstd_clock_backwards", "LetsTD clock precedes its watermark", 409)
    return timestamp


def _timed(state, now):
    ledger = read_letstd(state)
    return ledger, letstd_time(ledger, now)


def _status(start_at, end_at, timestamp):
    if end_at is not None and end_at < timestamp:
        return ENDED
    return SCHEDULED if start_at > timestamp else PENDING


def _fresh(spec, timestamp):
    return {"definition": spec["definition"], "start_at": spec["start_at"], "end_at": spec["end_at"],
            "status": _status(spec["start_at"], spec["end_at"], timestamp), "reason": None,
            "dependency_version": None, "mail_ids": [], "delivered_at": None, "via": None, "popup": None,
            "needs_adaptation": False}


def _specs(rows):
    if not isinstance(rows, (list, tuple)) or len(rows) > MAX_LEDGER_ROWS:
        _invalid("LetsTD row specifications must be a bounded sequence")
    seen = set()
    for spec in rows:
        if (not isinstance(spec, dict) or set(spec) != SPEC_FIELDS or not _positive(spec["reward_id"])
                or spec["reward_id"] in seen or not _hash(spec["definition"]) or not _timestamp(spec["start_at"])
                or spec["end_at"] is not None and not _timestamp(spec["end_at"])):
            _invalid("LetsTD row specification is invalid")
        seen.add(spec["reward_id"])
    return {str(spec["reward_id"]): spec for spec in sorted(rows, key=lambda spec: spec["reward_id"])}


def _fingerprints(rules_version, resource_sha256):
    if not _hash(rules_version) or not _hash(resource_sha256):
        _invalid("LetsTD arming requires stable rule and resource fingerprints")


def _capacity(ledger):
    if sum(len(record["rows"]) for groups in ledger["partners"].values() for record in groups.values()) > MAX_LEDGER_ROWS:
        raise ProtocolError("letstd_journal_capacity", "LetsTD ledger row capacity is exhausted", 409)


def arm_group(state, partner, group, rows, rules_version, resource_sha256, now):
    key = _group_key(partner, group)
    _fingerprints(rules_version, resource_sha256)
    specs = _specs(rows)
    ledger, timestamp = _timed(state, now)
    if group_record(ledger, partner, group) is not None:
        raise ProtocolError("letstd_group_armed", "This LetsTD reward group is already armed", 409)
    ledger["partners"].setdefault(partner, {})[key] = {"source": SOURCE, "armed_at": timestamp, "rearmed_at": None,
        "rules_version": rules_version, "resource_sha256": resource_sha256,
        "rows": {reward: _fresh(spec, timestamp) for reward, spec in specs.items()}}
    ledger["last_time"] = timestamp
    _capacity(ledger)
    return _replace(state, ledger)


def rearm_group(state, partner, group, rows, rules_version, resource_sha256, now):
    _group_key(partner, group)
    _fingerprints(rules_version, resource_sha256)
    specs = _specs(rows)
    ledger, timestamp = _timed(state, now)
    record = _armed(ledger, partner, group)
    for reward, row in record["rows"].items():
        if row["status"] == DELIVERED:
            row["needs_adaptation"] = reward not in specs or specs[reward]["definition"] != row["definition"]
    for reward, spec in specs.items():
        row = record["rows"].get(reward)
        if row is None or row["status"] != DELIVERED and row["definition"] != spec["definition"]:
            record["rows"][reward] = _fresh(spec, timestamp)
    record.update(rearmed_at=timestamp, rules_version=rules_version, resource_sha256=resource_sha256,
                  rows=dict(sorted(record["rows"].items(), key=lambda item: int(item[0]))))
    ledger["last_time"] = timestamp
    _capacity(ledger)
    return _replace(state, ledger)


def due_rows(record, timestamp):
    return [row for row in record["rows"].values() if row["status"] == SCHEDULED and row["start_at"] <= timestamp]


def schedule(state, partner, group, now):
    _group_key(partner, group)
    ledger, timestamp = _timed(state, now)
    due = due_rows(_armed(ledger, partner, group), timestamp)
    if not due:
        return deepcopy(state)
    for row in due:
        row["status"] = _status(row["start_at"], row["end_at"], timestamp)
    ledger["last_time"] = timestamp
    return _replace(state, ledger)


def defer_rows(state, deferrals, dependency_version):
    if (not isinstance(deferrals, (list, tuple)) or len(deferrals) > MAX_LEDGER_ROWS
            or not _hash(dependency_version)):
        _invalid("LetsTD deferrals require a bounded sequence and a dependency fingerprint")
    ledger = read_letstd(state)
    if not deferrals:
        return deepcopy(state)
    for deferral in deferrals:
        if not isinstance(deferral, tuple) or len(deferral) != 4:
            _invalid("LetsTD deferral entries are invalid")
        partner, group, reward_id, reason = deferral
        if type(reason) is not str or not _REASON.fullmatch(reason):
            _invalid("LetsTD pending reason is invalid")
        _pending(ledger, partner, group, reward_id).update(reason=reason, dependency_version=dependency_version)
    return _replace(state, ledger)


def defer_row(state, partner, group, reward_id, reason, dependency_version):
    return defer_rows(state, [(partner, group, reward_id, reason)], dependency_version)


def complete_rows(state, deliveries, now, via, popup):
    if (type(via) is not int or via not in SOURCES or type(popup) is not bool
            or not isinstance(deliveries, (list, tuple)) or not deliveries or len(deliveries) > MAX_LEDGER_ROWS):
        _invalid("LetsTD deliveries require a login source, a popup flag and bounded rows")
    ledger, timestamp = _timed(state, now)
    total = sum(len(row["mail_ids"]) for groups in ledger["partners"].values()
                for record in groups.values() for row in record["rows"].values())
    grants = []
    for delivery in deliveries:
        if not isinstance(delivery, tuple) or len(delivery) != 4:
            _invalid("LetsTD delivery entries are invalid")
        partner, group, reward_id, mail_ids = delivery
        row = _pending(ledger, partner, group, reward_id)
        if not _mail_ids(mail_ids) or row["start_at"] > timestamp:
            _invalid("LetsTD delivery requires ordered mail identities after its start")
        total += len(mail_ids)
        if total > MAX_MAIL_IDS:
            raise ProtocolError("letstd_journal_capacity", "LetsTD mail identity capacity is exhausted", 409)
        row.update(status=DELIVERED, reason=None, dependency_version=None, mail_ids=list(mail_ids),
                   delivered_at=timestamp, via=via, popup=popup)
        grants.append({"partner": partner, "group": group, "reward_id": reward_id, "definition": row["definition"],
                       "delivered_at": timestamp, "via": via, "popup": popup, "mail_ids": list(mail_ids)})
    validate_letstd_grants(grants)
    ledger["last_time"] = timestamp
    return _replace(state, ledger), grants


def complete_row(state, partner, group, reward_id, mail_ids, now, via, popup):
    updated, grants = complete_rows(state, [(partner, group, reward_id, mail_ids)], now, via, popup)
    return updated, grants[0]


def validate_letstd_grants(grants):
    if not isinstance(grants, list) or len(grants) > MAX_LEDGER_ROWS:
        _invalid("LetsTD grants must be a bounded sequence")
    previous, settlement, ids = None, None, []
    for grant in grants:
        if (not isinstance(grant, dict) or set(grant) != GRANT_FIELDS or grant["partner"] not in PARTNERS
                or type(grant["group"]) is not int or grant["group"] not in GROUPS or not _positive(grant["reward_id"])
                or not _hash(grant["definition"]) or not _timestamp(grant["delivered_at"])
                or type(grant["via"]) is not int or grant["via"] not in SOURCES or type(grant["popup"]) is not bool
                or not _mail_ids(grant["mail_ids"])):
            _invalid("LetsTD grant fields are invalid")
        order = PARTNER_ORDER[grant["partner"]], grant["group"], grant["reward_id"]
        context = grant["delivered_at"], grant["via"], grant["popup"]
        if previous is not None and (order <= previous or context != settlement):
            _invalid("LetsTD grants must be ordered rows of one settlement")
        if ids and grant["mail_ids"][0] <= ids[-1]:
            _invalid("LetsTD grants reuse or reorder mail identities")
        previous, settlement = order, context
        ids.extend(grant["mail_ids"])
    if len(ids) > MAX_PRESENTS:
        _invalid("LetsTD grants exceed the mailbox capacity")
    return ids


def validate_letstd_receipt(state, grants):
    validate_letstd_grants(grants)
    ledger = read_letstd(state)
    for grant in grants:
        record = group_record(ledger, grant["partner"], grant["group"])
        row = None if record is None else record["rows"].get(str(grant["reward_id"]))
        if (row is None or row["status"] != DELIVERED
                or any(row[key] != grant[key] for key in ("definition", "delivered_at", "via", "popup", "mail_ids"))):
            _invalid("LetsTD receipt differs from its permanent delivery")
