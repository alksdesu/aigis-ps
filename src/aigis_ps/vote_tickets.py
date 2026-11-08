from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .business_calendar import business_date
from .energy_clock import UNIX_EPOCH, instant, unix_milliseconds
from .errors import ProtocolError
from .native_numbers import INT32_MAX
from .possessions import ITEM_RESOURCE_LIMIT
from .system_literals import command_id


LEDGER_KEY = "vote_tickets"
DAILY_ITEM = "item_10000000"
SCHEMA_VERSION = 1
LOCAL_ORIGIN, BASELINE_ORIGIN = "local", "baseline"
AVAILABLE, CONSUMED, EXPIRED, UNRESOLVED = "available", "consumed", "expired", "unresolved"
UNOBSERVED = "unobserved"
_FIELDS = {"ItemID": 32, "EventID": 32, **{name: 1 for name in ("ID", "Category", "EffectType", "Stack",
    "CanAllGet", "ReceiveAction", "ReceiveConfirmation", "InvisibleContaint", "NoAutoDelete")}}
_LIMITS = ParseLimits(max_input_bytes=ITEM_RESOURCE_LIMIT, max_decompressed_bytes=ITEM_RESOURCE_LIMIT)


def _invalid(message="Stored vote ticket state is invalid"):
    raise ProtocolError("vote_ticket_state_invalid", message, 503)


def _clock(now):
    try:
        current = instant(now)
        return current, business_date(current).isoformat(), unix_milliseconds(current)
    except (TypeError, ValueError, OverflowError, ProtocolError) as error:
        raise ProtocolError("vote_ticket_clock_invalid", "Vote tickets require a representable instant") from error


def _saved_day(value):
    if type(value) is not str:
        _invalid("Vote ticket dates require ISO calendar strings")
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("Noncanonical date")
    except ValueError:
        _invalid("Vote ticket date is invalid")
    return value


def _validate_ledger(ledger, count):
    if (not isinstance(ledger, dict) or set(ledger) != {"schema_version", "last_time", "ticket_day", "status", "origin"}
            or type(ledger["schema_version"]) is not int or ledger["schema_version"] != SCHEMA_VERSION
            or type(ledger["last_time"]) is not int
            or ledger["status"] not in (AVAILABLE, CONSUMED, EXPIRED, UNRESOLVED)
            or ledger["origin"] not in (LOCAL_ORIGIN, BASELINE_ORIGIN)):
        _invalid("Vote ticket journal fields are invalid")
    try:
        saved = UNIX_EPOCH + timedelta(milliseconds=ledger["last_time"])
        observed_day = _clock(saved)[1]
    except (OverflowError, ValueError, ProtocolError):
        _invalid("Vote ticket observation timestamp is invalid")
    if ledger["status"] == UNRESOLVED:
        if ledger["ticket_day"] is not None or ledger["origin"] != BASELINE_ORIGIN or count != 1:
            _invalid("Undated vote tickets must retain their original inventory")
    else:
        day = _saved_day(ledger["ticket_day"])
        if day > observed_day or count != int(ledger["status"] == AVAILABLE):
            _invalid("Vote ticket dates or remaining inventory disagree")
        if ledger["status"] == AVAILABLE and day != observed_day:
            _invalid("Available vote tickets must match their last observed day")
        if ledger["status"] == EXPIRED and day >= observed_day:
            _invalid("Expired vote tickets require a later observed day")


def validate_vote_ticket_observation(value):
    if value is not None:
        if not isinstance(value, dict):
            _invalid("Vote ticket observations require a ledger snapshot")
        _validate_ledger(value, int(value.get("status") in (AVAILABLE, UNRESOLVED)))


class VoteTicketService:
    def __init__(self, possessions, catalog):
        self.possessions, self.catalog = possessions, catalog
        try:
            catalog._ready()
            raw = catalog._resources["ItemConfigTable.atb"]
            if type(raw) is not bytes or not 0 < len(raw) <= ITEM_RESOURCE_LIMIT:
                raise ValueError("Vote tickets require a bounded source snapshot")
            decoded = decompress_all4(raw, _LIMITS) if raw.startswith(b"ALL4") else raw
            table = parse_table(decoded, _LIMITS)
            fields = {field["name"]: field["type_code"] for field in table["fields"]}
            if len(fields) != len(table["fields"]) or any(fields.get(name) != kind for name, kind in _FIELDS.items()):
                raise ValueError("Vote ticket fields differ from the source contract")
            matching = [row for row in table["rows"] if row["ItemID"] == DAILY_ITEM]
            if len(matching) != 1:
                raise ValueError("Vote daily ticket must have one canonical definition")
            row = matching[0]
            expected = {"ID": command_id(DAILY_ITEM),
                "Category": 1925062086, "EffectType": 0, "Stack": 1, "CanAllGet": 1,
                "ReceiveAction": 0, "ReceiveConfirmation": 0, "InvisibleContaint": 0, "NoAutoDelete": 0}
            if any(type(row[name]) is not int or row[name] != value for name, value in expected.items()) or row["EventID"] != "":
                raise ValueError("Vote daily ticket behavior differs from the source contract")
            self._resource = raw
            self._resource_sha256 = hashlib.sha256(decoded).hexdigest()
            self._catalog_version = catalog.content_version
            self._groups = MappingProxyType({group.item_id: group for group in catalog.groups})
            if len(self._groups) != len(catalog.groups) or DAILY_ITEM in self._groups:
                raise ValueError("Vote group items must be distinct from the daily item")
            self.content_version = hashlib.sha256(json.dumps({"schema": 1, "catalog": self._catalog_version,
                "items": self._resource_sha256, "policy": "jst04_observed_daily_ticket_v1",
                "legacy": "adopt_only_during_qualified_event", "consume": "daily_one_then_group"},
                sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            self._snapshot = (self.possessions, self.catalog, self._resource, self._resource_sha256,
                              self._catalog_version, self._groups, self.content_version)
            self._ready()
        except ProtocolError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError, OSError, OverflowError) as error:
            raise ProtocolError("vote_ticket_definition_invalid", "Vote daily ticket resources are unavailable", 503) from error

    def _ready(self):
        current = (self.possessions, self.catalog, self._resource, self._resource_sha256,
                   self._catalog_version, self._groups, self.content_version)
        if any(left is not right for left, right in zip(current, self._snapshot)):
            raise ProtocolError("vote_ticket_dependencies_changed", "Vote ticket dependencies changed after setup", 503)
        self.catalog._ready()
        try:
            if (self.catalog.content_version != self._catalog_version
                    or self.catalog._resources["ItemConfigTable.atb"] is not self._resource
                    or self.possessions.asset_sha256 != self._resource_sha256
                    or not self.possessions.is_ready()
                    or self.possessions.stack(DAILY_ITEM) != 1
                    or any(self.possessions.stack(item) != 999 for item in self._groups)):
                raise ValueError("Vote inventory or source snapshot changed")
        except (AttributeError, KeyError, TypeError, ValueError, ProtocolError) as error:
            raise ProtocolError("vote_ticket_dependencies_changed", "Vote ticket dependencies changed after setup", 503) from error

    def _state(self, state):
        self._ready()
        count = self.possessions.counts(state).get(DAILY_ITEM, 0)
        if count not in (0, 1):
            _invalid("Daily vote tickets cannot exceed their verified stack of one")
        if LEDGER_KEY not in state:
            return count, None
        ledger = state[LEDGER_KEY]
        _validate_ledger(ledger, count)
        return count, ledger

    def initialize(self, state):
        self._state(state)
        return deepcopy(state)

    def observe(self, state, now):
        updated, _ = self.settle(state, now)
        observation = deepcopy(updated.get(LEDGER_KEY))
        self.validate_observation(updated, observation, exact=True)
        return updated, observation

    def validate_observation(self, state, observation, *, exact=False):
        validate_vote_ticket_observation(observation)
        _, current = self._state(state)
        if exact and current != observation:
            _invalid("Vote ticket receipt differs from the committed observation")
        if observation is not None:
            if current is None or current["last_time"] < observation["last_time"]:
                _invalid("Vote ticket receipt lost its observation history")
            if observation["ticket_day"] is not None and (
                    current["ticket_day"] is None or current["ticket_day"] < observation["ticket_day"]):
                _invalid("Vote ticket day precedes its saved receipt")
            if observation["ticket_day"] is not None and current["ticket_day"] == observation["ticket_day"]:
                allowed = {AVAILABLE: (AVAILABLE, CONSUMED, EXPIRED),
                           CONSUMED: (CONSUMED, EXPIRED), EXPIRED: (EXPIRED,)}
                if current["status"] not in allowed[observation["status"]] or current["origin"] != observation["origin"]:
                    _invalid("Vote ticket receipt cannot regain a consumed or expired ticket")

    def _inventory(self, state, *, debit=None, credit=None):
        before = deepcopy(state)
        expected = deepcopy(state)
        indexed = {row["ItemID"]: row for row in expected["tables"]["possessions"]}
        for amounts, sign in ((debit or {}, -1), (credit or {}, 1)):
            for item, amount in amounts.items():
                if item not in indexed:
                    indexed[item] = {"ItemID": item, "Count": 0}
                    expected["tables"]["possessions"].append(indexed[item])
                indexed[item]["Count"] += sign * amount
        updated = self.possessions.debit(deepcopy(state), debit) if debit else deepcopy(state)
        updated = self.possessions.credit(updated, credit) if credit else updated
        if state != before or updated != expected:
            raise ProtocolError("vote_ticket_inventory_invalid", "Vote ticket inventory operation changed unrelated state", 503)
        return updated

    def settle(self, state, now):
        count, ledger = self._state(state)
        current, day, timestamp = _clock(now)
        if ledger is not None and timestamp < ledger["last_time"]:
            raise ProtocolError("vote_ticket_clock_reversed", "Vote ticket clock moved behind its saved watermark", 409)
        eligible = (self.catalog.rules.enabled and not self.catalog.unresolved_candidates
                    and self.catalog.event.start_at <= current < self.catalog.event.end_at)
        updated, changed = deepcopy(state), False
        if ledger is None:
            if not eligible and not count:
                return updated, ()
            if not eligible:
                updated[LEDGER_KEY] = {"schema_version": SCHEMA_VERSION, "last_time": timestamp, "ticket_day": None,
                                       "status": UNRESOLVED, "origin": BASELINE_ORIGIN}
                return updated, ()
            if not count:
                updated = self._inventory(updated, credit={DAILY_ITEM: 1})
                changed = True
            updated[LEDGER_KEY] = {"schema_version": SCHEMA_VERSION, "last_time": timestamp, "ticket_day": day,
                                   "status": AVAILABLE, "origin": BASELINE_ORIGIN if count else LOCAL_ORIGIN}
        elif ledger["status"] == UNRESOLVED:
            if eligible:
                updated[LEDGER_KEY].update(last_time=timestamp, ticket_day=day, status=AVAILABLE)
        else:
            ticket_day = ledger["ticket_day"]
            if day > ticket_day and count:
                updated = self._inventory(updated, debit={DAILY_ITEM: 1})
                updated[LEDGER_KEY].update(last_time=timestamp, status=EXPIRED)
                changed = True
            if eligible and day > ticket_day:
                updated = self._inventory(updated, credit={DAILY_ITEM: 1})
                updated[LEDGER_KEY].update(last_time=timestamp, ticket_day=day, status=AVAILABLE, origin=LOCAL_ORIGIN)
                changed = True
        self._state(updated)
        return updated, (DAILY_ITEM,) if changed else ()

    def consume(self, state, group_item_id, count, now):
        self._ready()
        if type(count) is not int or not 0 < count <= INT32_MAX:
            raise ProtocolError("vote_ticket_count_invalid", "Vote ticket consumption requires a positive native count")
        if type(group_item_id) is not str or group_item_id not in self._groups:
            raise ProtocolError("vote_ticket_group_invalid", "Vote ticket consumption requires a known group item")
        current = _clock(now)[0]
        if not self.catalog.active(current):
            raise ProtocolError("vote_event_closed", "Votes require an active event", 409)
        group = self._groups[group_item_id]
        if not group.vote_start <= current < group.vote_end:
            raise ProtocolError("vote_group_closed", "Votes require the selected group's voting window", 409)
        updated, _ = self.settle(state, current)
        available, _ = self._state(updated)
        daily_used = min(1, available, count)
        ordinary_used = count - daily_used
        costs = ({DAILY_ITEM: daily_used} if daily_used else {}) | ({group_item_id: ordinary_used} if ordinary_used else {})
        updated = self._inventory(updated, debit=costs)
        updated[LEDGER_KEY]["last_time"] = _clock(current)[2]
        if daily_used:
            updated[LEDGER_KEY]["status"] = CONSUMED
        self._state(updated)
        return updated, daily_used, ordinary_used

    def ticket_status(self, state):
        count, ledger = self._state(state)
        unresolved = count == 1 and (ledger is None or ledger["status"] == UNRESOLVED)
        return {"item_id": DAILY_ITEM, "count": count, "ticket_day": ledger["ticket_day"] if ledger else None,
                "status": ledger["status"] if ledger else UNOBSERVED,
                "reason": "legacy_ticket_date_unknown" if unresolved else None}

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "content_version": self.content_version,
                "item_id": DAILY_ITEM, "resource_sha256": self._resource_sha256,
                "policy": "One ticket per observed JST04 event day; daily ticket first; no missed-day catch-up"}
