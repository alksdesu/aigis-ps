from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from operator import attrgetter
import re
from types import MappingProxyType

from .al_format import decompress_all4, parse_table
from .energy_clock import instant
from .errors import ProtocolError
from .native_numbers import INT32_MAX, int32
from .vote_catalog import CANDIDATES_UNRESOLVED, CATALOG_CHANGED, LIMITS
from .vote_expiry import DEPENDENCIES_CHANGED as EXPIRY_DEPENDENCIES_CHANGED, TICKET_CLOSED as CLOSED


SOURCE_FIELDS = MappingProxyType({
    "SystemText.atb": MappingProxyType({"Data_Text": 32, "Recode_Index": 1}),
    "cfgImplantText.atb": MappingProxyType({"_ID": 32, "_TextID": 1})})
TEXT_SOURCES = ("SystemTextVoteInfomation", "SystemTextVoteInfomation5")
# Both vote notices grant one ticket per charisma rate and cap holdings at 999, the Stack of every group ticket.
_RATE = re.compile(r"消費カリスマ(\d+)につき「闘兵券」を(\d+)枚", re.ASCII)
_CAP = re.compile(r"闘兵券の最大所持数は(\d+)", re.ASCII)
TICKETS_PER_RATE = 1
TICKET_CAP = 999
WINDOW_REASONS = frozenset(("disabled", "candidates_unresolved", "no_window"))
REASONS = WINDOW_REASONS | {"issued", CLOSED, "zero_charisma", "below_rate"}
POLICY = MappingProxyType({"count": "floor_charisma_per_run_times_runs", "eligibility": "enabled_and_resolved",
                           "window": "first_ascending_group_dispersion", "closed": "expiry_record_blocks_identity"})
SOURCE_INVALID = "vote_quest_ticket_source_invalid"
DEFINITION_INVALID = "vote_quest_ticket_definition_invalid"
DEPENDENCIES_CHANGED = "vote_quest_ticket_dependencies_changed"
DEPENDENCIES_UNAVAILABLE = "vote_quest_ticket_dependencies_unavailable"
# Vote catalog and expiry readiness codes pass through a provider build unchanged.
SETUP_ERRORS = (SOURCE_INVALID, DEFINITION_INVALID, DEPENDENCIES_CHANGED, CATALOG_CHANGED, CANDIDATES_UNRESOLVED,
                EXPIRY_DEPENDENCIES_CHANGED)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _native(value, minimum):
    return int32(value) and value >= minimum


def _instant(value):
    try:
        return instant(value)
    except ProtocolError as error:
        raise ProtocolError("vote_quest_ticket_clock_invalid", "Quest ticket windows require a datetime", 503) from error


def _moment(value):
    return value.isoformat(timespec="seconds")


def _window(group):
    return {"group_id": group.group_id, "item_id": group.item_id, "final": group.final,
            "dispersion_start": _moment(group.dispersion_start), "dispersion_end": _moment(group.dispersion_end),
            "source_dispersion_start": _moment(group.source_dispersion_start),
            "source_dispersion_end": _moment(group.source_dispersion_end)}


def _dependencies_changed():
    return ProtocolError(DEPENDENCIES_CHANGED, "Quest ticket dependencies changed after setup", 503)


def _request_invalid():
    return ProtocolError("vote_quest_ticket_request_invalid", "Quest tickets require native charisma and run counts", 503)


def _source_texts(catalog, asset_reader):
    try:
        tables = {}
        for name, expected in SOURCE_FIELDS.items():
            raw = asset_reader(name)
            if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
                raise ValueError("Quest ticket sources must be bounded bytes")
            if hashlib.sha256(raw).hexdigest() != catalog.resource_hashes[name]:
                raise ValueError("Quest ticket sources differ from the vote catalog snapshot")
            parsed = parse_table(decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw, LIMITS)
            fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
            if len(fields) != len(parsed["fields"]) or any(fields.get(key) != kind for key, kind in expected.items()):
                raise ValueError("Quest ticket source fields differ from the verified schema")
            tables[name] = parsed["rows"]
        implants = tables["cfgImplantText.atb"]
        pointers = {row["_ID"]: row["_TextID"] for row in implants}
        if len(pointers) != len(implants):
            raise ValueError("Vote text identities must be unique")
        texts, result = tables["SystemText.atb"], {}
        for name in TEXT_SOURCES:
            index = pointers[name]
            if not _native(index, 0) or index >= len(texts) or type(texts[index]["Data_Text"]) is not str:
                raise ValueError("Vote text pointer is unavailable")
            result[name] = index, texts[index]["Data_Text"]
        return result
    except (AttributeError, KeyError, IndexError, TypeError, ValueError, OSError, OverflowError) as error:
        raise ProtocolError(SOURCE_INVALID,
                            "Quest ticket text differs from the verified vote sources", 503) from error


def _ticket_definition(texts, catalog, possessions):
    try:
        parsed = set()
        for _, text in texts.values():
            rates, caps = _RATE.findall(text), _CAP.findall(text)
            if len(rates) != 1 or len(caps) != 1:
                raise ValueError("Each vote notice must state one ticket rate and one cap")
            parsed.add((int(rates[0][0]), int(rates[0][1]), int(caps[0])))
        if len(parsed) != 1:
            raise ValueError("Vote notices disagree on quest tickets")
        rate, tickets, cap = parsed.pop()
        if not _native(rate, 1) or tickets != TICKETS_PER_RATE or cap != TICKET_CAP:
            raise ValueError("Quest ticket rate or cap differs from the verified contract")
        groups = tuple(sorted(catalog.groups, key=attrgetter("group_id")))
        by_item = MappingProxyType({group.item_id: group for group in groups})
        if (not groups or len(by_item) != len(groups)
                or any(possessions.stack(item, stored=True) != cap for item in by_item)):
            raise ValueError("Every group ticket must hold exactly the stated cap")
        return rate, cap, groups, by_item
    except (AttributeError, TypeError, ValueError) as error:
        raise ProtocolError(DEFINITION_INVALID,
                            "Quest ticket rate or cap differs from the verified contract", 503) from error


@dataclass(frozen=True)
class VoteTicketGrant:
    item_id: str | None
    count: int
    reason: str

    def __post_init__(self):
        if self.reason not in REASONS:
            raise ValueError("Quest ticket grants require a known reason")
        if not _native(self.count, 0) or (self.count > 0) != (self.reason == "issued"):
            raise ValueError("Quest ticket counts must be positive exactly when issued")
        if self.reason in WINDOW_REASONS:
            if self.item_id is not None:
                raise ValueError("Quest ticket grants without an open group cannot name a ticket")
        elif type(self.item_id) is not str or not self.item_id:
            raise ValueError("Quest ticket grants with an open group must name its ticket")


class VoteQuestTickets:
    def __init__(self, catalog, possessions, expiry, asset_reader):
        self.catalog, self.possessions, self.expiry = catalog, possessions, expiry
        self._dependencies()
        texts = _source_texts(catalog, asset_reader)
        self.text_rows = MappingProxyType({name: row for name, (row, _) in texts.items()})
        self.source_hashes = MappingProxyType({name: catalog.resource_hashes[name] for name in SOURCE_FIELDS})
        self.rate, self.cap, self._groups, self._groups_by_item = _ticket_definition(texts, catalog, possessions)
        self.content_version = self._content()
        self._snapshot = self._fields()
        self._ready()

    def _content(self):
        return _digest({"schema": 1, "rate": self.rate, "tickets_per_rate": TICKETS_PER_RATE, "cap": self.cap,
                        "catalog": self.catalog.content_version, "expiry": self.expiry.content_version,
                        "policy": dict(POLICY)})

    def _fields(self):
        return (self.catalog, self.possessions, self.expiry, self.rate, self.cap, self.text_rows,
                self.source_hashes, self._groups, self._groups_by_item, self.content_version)

    def _dependencies(self):
        try:
            self.catalog._ready()
            self.expiry._ready()
            wired = self.expiry.catalog is self.catalog and self.expiry.possessions is self.possessions
        except ProtocolError:
            raise
        except (AttributeError, TypeError) as error:
            raise _dependencies_changed() from error
        if not wired:
            raise _dependencies_changed()

    def _ready(self):
        if any(left is not right for left, right in zip(self._fields(), self._snapshot)):
            raise _dependencies_changed()
        self._dependencies()
        try:
            if (self._content() != self.content_version
                    or any(self.possessions.stack(item, stored=True) != self.cap for item in self._groups_by_item)):
                raise ValueError("Quest ticket definitions changed")
        except (AttributeError, TypeError, ValueError) as error:
            raise _dependencies_changed() from error

    def _group_at(self, current):
        return next((group for group in self._groups if group.dispersion_start <= current < group.dispersion_end), None)

    def _unavailable(self, group):
        if not self.catalog.rules.enabled:
            return "disabled"
        if group is None:
            return "no_window"
        # Like the vote event gates, unresolved candidates only block an open window.
        return "candidates_unresolved" if self.catalog.unresolved_candidates else None

    def _closed(self, state, group):
        try:
            self.expiry.require_open(state, group)
        except ProtocolError as error:
            if error.code != CLOSED:
                raise
            return True
        return False

    def ticket_group(self, now):
        self._ready()
        return self._group_at(_instant(now))

    def evaluate(self, state, *, charisma_per_run, runs, now):
        self._ready()
        if not _native(charisma_per_run, 0) or not _native(runs, 1):
            raise _request_invalid()
        count = charisma_per_run // self.rate * TICKETS_PER_RATE * runs
        if count > INT32_MAX:
            raise _request_invalid()
        group = self._group_at(_instant(now))
        reason = self._unavailable(group)
        if reason is not None:
            return VoteTicketGrant(None, 0, reason)
        # An expired ticket identity never reopens; more stock would break its frozen expiry record.
        if self._closed(state, group):
            return VoteTicketGrant(group.item_id, 0, CLOSED)
        if not charisma_per_run:
            return VoteTicketGrant(group.item_id, 0, "zero_charisma")
        return VoteTicketGrant(group.item_id, count, "issued" if count else "below_rate")

    def grant(self, state, item_id, count):
        self._ready()
        group = self._groups_by_item.get(item_id) if type(item_id) is str else None
        if group is None:
            raise ProtocolError("vote_quest_ticket_item_invalid", "Quest tickets credit only a verified vote group ticket")
        if not _native(count, 1):
            raise ProtocolError("vote_quest_ticket_count_invalid", "Quest ticket grants require a positive native count")
        self.expiry.require_open(state, group)
        return self.possessions.credit_capped(state, {item_id: count})

    def status(self, now=None, *, state=None):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        current = group = reason = state_error = None
        if error is None and now is not None:
            current = _instant(now)
            group = self._group_at(current)
            reason = self._unavailable(group)
            if reason is None and state is not None:
                try:
                    reason = CLOSED if self._closed(state, group) else None
                except ProtocolError as failure:
                    state_error = failure.code
        return {"ready": error is None, "error": error, "content_version": self.content_version,
                "rate": self.rate, "tickets_per_rate": TICKETS_PER_RATE, "cap": self.cap,
                "text_rows": dict(self.text_rows), "source_hashes": dict(self.source_hashes),
                "groups": [_window(entry) for entry in self._groups],
                "now": None if current is None else _moment(current),
                "current_group": None if group is None else {"group_id": group.group_id, "item_id": group.item_id},
                "unavailable_reason": reason, "state_error": state_error,
                "policy": dict(POLICY), "native_tested": False}
