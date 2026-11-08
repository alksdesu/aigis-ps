from __future__ import annotations

from copy import deepcopy
import re

from .alxml import Column, Container, dumps
from .errors import ProtocolError
from .native_numbers import int32
# Passes saved under the earlier 256-byte bound keep loading and advancing; only new passes must fit the native cell.
from .possessions import STORED_ITEM_NAME_BYTES as STORED_ITEM_BYTES
from .protocol import NATIVE_CELL_BYTES


MAX_PASSES = 4096
MAX_DELIVERIES = 65535
MAX_MAIL_IDS = 65535
MAX_REWARD_IDS = 65535
MAX_ARCHIVED = MAX_PASSES
MAX_ITEM_BYTES = NATIVE_CELL_BYTES
LEDGER_KEY = "event_pass_state"
IDENTITY_UNREPLAYABLE = "event_pass_identity_unreplayable"
DEFINITION_CHANGED = "event_pass_definition_changed"
REBIND_UNNEEDED = "event_pass_rebind_unneeded"
RESPONSE_CAPACITY = "event_pass_response_capacity"
ARCHIVED_KEY = "archived"
TRACKS = {"normal": "ReceivedNormalPoint", "premium": "ReceivedPremiumPoint"}
_RECORD_FIELDS = ("definition", "baseline_normal", "baseline_premium", "deliveries")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_POINT = re.compile(r"[1-9][0-9]{0,9}\Z")


def _invalid(message="Event pass state is invalid"):
    raise ProtocolError("event_pass_state_invalid", message, 503)


class _Usage:
    def __init__(self):
        self.deliveries = self.rewards = 0
        self.mail = set()

    def add(self, delivery):
        self.deliveries += 1
        self.rewards += len(delivery["reward_ids"])
        self.mail.update(delivery["mail_ids"])
        if self.deliveries > MAX_DELIVERIES or len(self.mail) > MAX_MAIL_IDS or self.rewards > MAX_REWARD_IDS:
            _invalid("Event pass delivery history exceeds its storage limit")


def _integer(value, minimum=0):
    return int32(value) and value >= minimum


def _identifier(value):
    try:
        if type(value) is not str or not value or len(value.encode("utf-8")) > STORED_ITEM_BYTES:
            raise ValueError("Invalid event pass identity")
        dumps(Container(children=(Column("ID", "S", (value,)),)))
    except (TypeError, ValueError, UnicodeError):
        _invalid("Event pass identities require bounded valid text")


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _ids(values):
    return (isinstance(values, list) and 0 < len(values) <= MAX_MAIL_IDS
            and all(_integer(value, 1) for value in values) and len(set(values)) == len(values))


def _native_rows(state):
    if (not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int
            or state["bootstrap_schema"] != 1 or not isinstance(state.get("tables"), dict)):
        _invalid("Event passes require a verified bootstrap state")
    rows = state["tables"].get("event_pass_status")
    if not isinstance(rows, list) or len(rows) > MAX_PASSES:
        _invalid("Event pass rows exceed their storage limit")
    names = set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"ID", *TRACKS.values()}
                or any(not _integer(row[field]) for field in TRACKS.values())):
            _invalid("Event pass rows require their three native fields")
        _identifier(row["ID"])
        if row["ID"] in names:
            _invalid("Event pass identities must be distinct")
        names.add(row["ID"])
    return rows


def _baseline(row):
    return {"definition": None, "baseline_normal": row["ReceivedNormalPoint"],
            "baseline_premium": row["ReceivedPremiumPoint"],
            "deliveries": {"normal": {}, "premium": {}}}


def _watermarks(record, usage, *, bound):
    if (not isinstance(record, dict) or set(record) != set(_RECORD_FIELDS)
            or (bound or record["definition"] is not None) and not _hash(record["definition"])
            or any(not _integer(record[f"baseline_{track}"]) for track in TRACKS)
            or not isinstance(record["deliveries"], dict) or set(record["deliveries"]) != set(TRACKS)):
        _invalid("Event pass baseline is invalid")
    reward_ids, watermarks = set(), {}
    for track in TRACKS:
        deliveries, baseline = record["deliveries"][track], record[f"baseline_{track}"]
        if not isinstance(deliveries, dict):
            _invalid("Event pass deliveries require threshold mappings")
        for key, delivery in deliveries.items():
            if (type(key) is not str or not _POINT.fullmatch(key) or not _integer(int(key), 1)
                    or int(key) <= baseline or record["definition"] is None
                    or not isinstance(delivery, dict)
                    or set(delivery) != {"definition", "reward_ids", "mail_ids"}
                    or not _hash(delivery["definition"])
                    or not _ids(delivery["reward_ids"]) or not _ids(delivery["mail_ids"])):
                _invalid("Event pass delivery is invalid")
            if reward_ids.intersection(delivery["reward_ids"]) or usage.mail.intersection(delivery["mail_ids"]):
                _invalid("Event pass deliveries cannot reuse rewards or mail")
            reward_ids.update(delivery["reward_ids"])
            usage.add(delivery)
        watermarks[track] = max((baseline, *map(int, deliveries)))
    return watermarks


def _records(entry):
    if not isinstance(entry, dict):
        _invalid("Event pass baseline is invalid")
    archived = entry.get(ARCHIVED_KEY, [])
    if not isinstance(archived, list) or ARCHIVED_KEY in entry and not archived:
        _invalid("Archived event pass identities are invalid")
    return [*archived, {name: value for name, value in entry.items() if name != ARCHIVED_KEY}]


def _delivered(entry):
    return {mail for record in _records(entry) for deliveries in record["deliveries"].values()
            for delivery in deliveries.values() for mail in delivery["mail_ids"]}


def _validated(state):
    rows = _native_rows(state)
    if LEDGER_KEY not in state:
        return rows, None
    journal = state[LEDGER_KEY]
    if (not isinstance(journal, dict) or set(journal) != {"schema_version", "passes"}
            or type(journal["schema_version"]) is not int or journal["schema_version"] != 1
            or not isinstance(journal["passes"], dict)
            or set(journal["passes"]) != {row["ID"] for row in rows}):
        _invalid("Event pass journal does not match native identities")
    usage, archived = _Usage(), 0
    for row in rows:
        records = _records(journal["passes"][row["ID"]])
        archived += len(records) - 1
        if archived > MAX_ARCHIVED:
            _invalid("Archived event pass identities exceed their storage limit")
        reached = None
        for index, record in enumerate(records):
            watermarks = _watermarks(record, usage, bound=index < len(records) - 1)
            # Each later identity starts where the earlier one left the native watermark.
            if reached is not None and any(record[f"baseline_{track}"] != reached[track] for track in TRACKS):
                _invalid("Archived event pass identities do not lead to their successor")
            reached = watermarks
        if any(row[column] != reached[track] for track, column in TRACKS.items()):
            _invalid("Event pass native watermark disagrees with its journal")
    return rows, journal


def read_event_passes(state):
    return deepcopy(_validated(state)[0])


def initialize_event_passes(state):
    rows, journal = _validated(state)
    updated = deepcopy(state)
    if journal is None:
        updated[LEDGER_KEY] = {"schema_version": 1, "passes": {row["ID"]: _baseline(row) for row in rows}}
    return updated


def mark_event_pass(state, identifier, *, definition, normal_deliveries=None, premium_deliveries=None):
    _identifier(identifier)
    if not _hash(definition):
        raise ProtocolError("event_pass_definition_invalid", "Event pass identity requires a semantic fingerprint", 503)
    updates = {"normal": {} if normal_deliveries is None else normal_deliveries,
               "premium": {} if premium_deliveries is None else premium_deliveries}
    for deliveries in updates.values():
        if not isinstance(deliveries, dict) or len(deliveries) > MAX_DELIVERIES:
            raise ProtocolError("event_pass_selection_invalid", "Event pass deliveries require a bounded threshold mapping")
        for point, delivery in deliveries.items():
            if (not _integer(point, 1) or not isinstance(delivery, dict)
                    or set(delivery) != {"definition", "reward_ids", "mail_ids"}
                    or not _hash(delivery["definition"])
                    or not _ids(delivery["reward_ids"]) or not _ids(delivery["mail_ids"])):
                raise ProtocolError("event_pass_selection_invalid", "Event pass deliveries require complete reward and mail identities")
    _validated(state)
    if not any(updates.values()):
        return deepcopy(state)
    updated = initialize_event_passes(state)
    rows, journal = updated["tables"]["event_pass_status"], updated[LEDGER_KEY]
    row = next((row for row in rows if row["ID"] == identifier), None)
    if row is None:
        if len(identifier.encode("utf-8")) > MAX_ITEM_BYTES:
            raise ProtocolError(IDENTITY_UNREPLAYABLE, "New event passes need identities that fit the native cell", 503)
        if len(rows) >= MAX_PASSES:
            raise ProtocolError("event_pass_history_capacity", "Event pass identity history is full", 503)
        row = {"ID": identifier, "ReceivedNormalPoint": 0, "ReceivedPremiumPoint": 0}
        rows.append(row)
        journal["passes"][identifier] = _baseline(row)
    entry = journal["passes"][identifier]
    if entry["definition"] is not None and entry["definition"] != definition:
        raise ProtocolError(DEFINITION_CHANGED, "Event pass identity changed while delivery history exists", 503)
    entry["definition"] = definition
    seen_rewards = {reward for values in entry["deliveries"].values()
                    for delivery in values.values() for reward in delivery["reward_ids"]}
    seen_mail = set().union(*map(_delivered, journal["passes"].values()))
    for track, field in TRACKS.items():
        watermark = row[field]
        for point, delivery in sorted(updates[track].items()):
            key = str(point)
            existing = entry["deliveries"][track].get(key)
            if existing is not None:
                if existing != delivery:
                    raise ProtocolError("event_pass_delivery_changed", "An event pass reward was already delivered with different contents", 409)
                continue
            if point <= watermark:
                raise ProtocolError("event_pass_already_received", "Event pass rewards at or below the saved watermark cannot be backfilled", 409)
            if seen_rewards.intersection(delivery["reward_ids"]) or seen_mail.intersection(delivery["mail_ids"]):
                raise ProtocolError("event_pass_delivery_duplicate", "An event pass reward or mail was already delivered", 409)
            entry["deliveries"][track][key] = deepcopy(delivery)
            seen_rewards.update(delivery["reward_ids"])
            seen_mail.update(delivery["mail_ids"])
            row[field] = max(row[field], point)
    _validated(updated)
    return updated


def read_pass_journal(state, identifier):
    _identifier(identifier)
    _, journal = _validated(state)
    entry = None if journal is None else journal["passes"].get(identifier)
    return deepcopy(entry)


def delivered_mail_ids(state):
    _, journal = _validated(state)
    passes = {} if journal is None else journal["passes"]
    return {identifier: frozenset(_delivered(entry)) for identifier, entry in passes.items()}


def rebind_definition(state, pass_id, identity):
    _identifier(pass_id)
    if not _hash(identity):
        raise ProtocolError("event_pass_definition_invalid", "Event pass identity requires a semantic fingerprint", 503)
    rows, journal = _validated(state)
    entry = None if journal is None else journal["passes"].get(pass_id)
    if entry is None or entry["definition"] in (None, identity):
        raise ProtocolError(REBIND_UNNEEDED, "The saved event pass history already follows this identity", 409)
    row = next(row for row in rows if row["ID"] == pass_id)
    archived = [*entry.get(ARCHIVED_KEY, ()), {name: entry[name] for name in _RECORD_FIELDS}]
    # Delivered thresholds stay received, so the new identity starts from the native watermark.
    rebound = {"definition": identity, **{f"baseline_{track}": row[column] for track, column in TRACKS.items()},
               "deliveries": {track: {} for track in TRACKS}, ARCHIVED_KEY: deepcopy(archived)}
    updated = deepcopy(state)
    updated[LEDGER_KEY]["passes"][pass_id] = rebound
    _validated(updated)
    return updated
