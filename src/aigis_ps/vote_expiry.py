from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from types import MappingProxyType
import unicodedata

from .al_format import decompress_all4, parse_table
from .alxml import Column, Container
from .energy_clock import UNIX_EPOCH, instant, unix_milliseconds
from .errors import ProtocolError
from .native_numbers import INT32_MAX
from .presents import PRESENT_CAPACITY_ERRORS as CAPACITY_ERRORS
from .vote_catalog import LIMITS
from .vote_state import read_vote_history, vote_key


GOLD_PER_TICKET = 50
MAX_RECORDS = 4096
DESCRIPTION = "闘兵券期限終了交換"
GOLD_PRESENT_TYPE = 2
NOTIFICATION_KEY = "vote_expiry_notifications"
ARCHIVED_KEY = "archived"
REOPEN_UNNEEDED = "vote_expiry_reopen_unneeded"
# 116 registers this group for 6996B0, which shows SystemText 4651 only while Gold is positive.
NOTICE_GROUP, NOTICE_COLUMN = "ItemExchange", "Gold"
DELIVERY_INVALID = ("vote_expiry_delivery_invalid", "Vote expiry mail differs from its frozen conversion")
DEPENDENCIES_CHANGED = "vote_expiry_dependencies_changed"
TICKET_CLOSED = "vote_ticket_closed"
PENDING, SETTLED, CLOSED = "pending", "settled", "closed"
ASSAULT_KEY = "vote_assault_expiry"
ASSAULT_NOTIFICATION_KEY = "vote_assault_notifications"
ASSAULT_DESCRIPTION = "強襲ミッション挑戦権変換"
ASSAULT_GOLD_PER_RIGHT = 3000
ASSAULT_SOURCE_TEXT = "SystemTextAssaultInfomation"
ASSAULT_SOURCE_PHRASES = ("開催終了時に使用していない挑戦権は", "ゴールドはプレゼントボックスへ一括で送られます。")
ASSAULT_DELIVERY_INVALID = ("vote_assault_expiry_delivery_invalid", "Assault conversion mail differs from its frozen rights")
# 116 registers RET for 6996B0, which shows SystemText 4585 once per response for an EMC mission of Type_Quest 7.
RET_GROUP, RET_COLUMN = "RET", "msid"
ASSAULT_QUEST_TYPE = 7
# AssaultMissionConfig names each event's mission with mixed-width digits, so names compare after NFKC.
ASSAULT_MISSION_NAME = "第{}回人気投票ミッション"
MISSION_SOURCES = MappingProxyType({
    "AssaultMissionConfig.atb": MappingProxyType({"MissionID": 1, "Name": 32}),
    "AssaultMissionQuestList.atb": MappingProxyType({"MissionID": 1, "QuestID": 1}),
    "EmcMissionConfig.atb": MappingProxyType({"ID_Mission": 1, "ID_QuestFirst": 1, "ID_QuestLast": 1, "Type_Quest": 1})})
_NOTICE_RECORD_FIELDS = frozenset(("gold", "mail_ids", "notified_at"))
_NOTICE_FIELDS = _NOTICE_RECORD_FIELDS | {"item_id"}
_ENTRY_FIELDS = frozenset(("group_id", "definition", "source_window_end", "window_end", "observed_at", "amount", "rate",
                           "status", "reason", "settled_at", "mail_ids"))
_ARCHIVED_FIELDS = _ENTRY_FIELDS | {"item_id", "archived_at"}
_COMPLETED = frozenset((SETTLED, CLOSED))
_ASSAULT_FIELDS = frozenset(("definition", "source_window_end", "window_end", "observed_at", "earned", "spent",
                             "amount", "rate", "status", "reason", "settled_at", "mail_ids"))
_ASSAULT_NOTICE_RECORD_FIELDS = frozenset(("mission_id", "gold", "mail_ids", "notified_at"))
_ASSAULT_NOTICE_FIELDS = _ASSAULT_NOTICE_RECORD_FIELDS | {"event_id"}
_ASSAULT_RATE = re.compile(r"挑戦権1回につき(\d+)Gへと変換されます。", re.ASCII)
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_REASON = re.compile(r"[a-zA-Z0-9_]{1,128}\Z")
_EVENT_KEY = re.compile(r"[1-9][0-9]{0,9}\Z")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _integer(value, minimum=0, maximum=INT32_MAX):
    return type(value) is int and minimum <= value <= maximum


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _event_key(value):
    return type(value) is str and _EVENT_KEY.fullmatch(value) is not None and int(value) <= INT32_MAX


def _invalid(message="Stored vote expiry state is invalid"):
    raise ProtocolError("vote_expiry_state_invalid", message, 503)


def _notification_invalid(message="Stored vote expiry notices are invalid"):
    raise ProtocolError("vote_expiry_notifications_invalid", message, 503)


def _assault_invalid(message="Stored assault conversions are invalid"):
    raise ProtocolError("vote_assault_expiry_state_invalid", message, 503)


def _assault_notification_invalid(message="Stored assault conversion notices are invalid"):
    raise ProtocolError("vote_assault_notifications_invalid", message, 503)


def _clock(value):
    try:
        current = instant(value).astimezone(timezone.utc)
        return current, unix_milliseconds(current)
    except (TypeError, ValueError, OverflowError, ProtocolError) as error:
        raise ProtocolError("vote_expiry_clock_invalid", "Vote expiry requires a representable instant") from error


def _date(value):
    return _clock(value)[0].isoformat(timespec="seconds")


def _canonical_date(value):
    try:
        if type(value) is not str or not value.endswith("+00:00"):
            raise ValueError("Expected canonical UTC date")
        parsed = datetime.fromisoformat(value)
        if parsed.microsecond or parsed.isoformat(timespec="seconds") != value:
            raise ValueError("Expected whole seconds")
        return parsed
    except (ValueError, OverflowError, TypeError):
        return None


def _stored_date(value):
    parsed = _canonical_date(value)
    if parsed is None:
        _invalid("Vote expiry windows require canonical UTC timestamps")
    return parsed


def _moment(value):
    try:
        return UNIX_EPOCH + timedelta(milliseconds=value) if type(value) is int else None
    except OverflowError:
        return None


def _stored_stamp(value):
    moment = _moment(value)
    if moment is None:
        _invalid("Vote expiry observations require representable UTC milliseconds")
    return moment


def _one_mail(value):
    return type(value) is list and len(value) == 1 and _integer(value[0], 1)


def _issued(state, identifier, seen):
    return identifier not in seen and _integer(state.get("last_present_id"), 1) and identifier <= state["last_present_id"]


def _definition(item_id, group_id, rate, source_window_end):
    return _digest({"schema": 1, "item_id": item_id, "group_id": group_id, "rate": rate,
                    "source_window_end": source_window_end, "description": DESCRIPTION})


def _assault_definition(event_id, rate, source_window_end):
    return _digest({"schema": 1, "event_id": event_id, "rate": rate,
                    "source_window_end": source_window_end, "description": ASSAULT_DESCRIPTION})


def _catalog_rows(catalog, name):
    raw = catalog._resources[name]
    return parse_table(decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw, LIMITS)["rows"]


def _catalog_texts(catalog, names):
    texts = _catalog_rows(catalog, "SystemText.atb")
    implants = {row["_ID"]: row["_TextID"] for row in _catalog_rows(catalog, "cfgImplantText.atb")}
    return [texts[implants[name]]["Data_Text"] for name in names]


def _gold_row(identifier, gold, description):
    return {"PresentID": identifier, "Type": GOLD_PRESENT_TYPE, "Content": gold, "Description": description}


def _gold_mail(presents, state, gold, description, failure):
    reward = {"kind": "gold", "amount": gold, "description": description}
    updated, groups = presents.enqueue_groups(deepcopy(state), [[reward]])
    last = state.get("last_present_id", 0)
    addition = _gold_row(last + 1, gold, description)
    expected = deepcopy(state)
    expected.update(presents_schema=1, last_present_id=last + 1)
    expected["tables"]["presents"] = presents.rows(state) + [addition]
    if groups != [[addition]] or updated != expected:
        raise ProtocolError(*failure, 503)
    return updated, [last + 1]


def _archive(ledger, record):
    ledger[ARCHIVED_KEY] = [*ledger.get(ARCHIVED_KEY, ()), record]


def _converted(ledger):
    if ledger is None:
        return {}, {}
    current = {item: {"gold": entry["amount"] * entry["rate"], "mail_ids": list(entry["mail_ids"]),
                      "settled_at": entry["settled_at"]}
               for item, entry in ledger["items"].items() if entry["status"] == SETTLED}
    archived = {record["mail_ids"][0]: {"item_id": record["item_id"], "gold": record["amount"] * record["rate"],
                                        "settled_at": record["settled_at"], "archived_at": record["archived_at"]}
                for record in ledger.get(ARCHIVED_KEY, ()) if record["status"] == SETTLED}
    return current, archived


def _notices(state, conversions, archived):
    if NOTIFICATION_KEY not in state:
        return {}, []
    ledger = state[NOTIFICATION_KEY]
    if (type(ledger) is not dict or set(ledger) - {ARCHIVED_KEY} != {"schema_version", "records"}
            or type(ledger["schema_version"]) is not int or ledger["schema_version"] != 1
            or type(ledger["records"]) is not dict
            or ARCHIVED_KEY in ledger and (type(ledger[ARCHIVED_KEY]) is not list or not ledger[ARCHIVED_KEY])
            or not 0 < len(ledger["records"]) + len(ledger.get(ARCHIVED_KEY, ())) <= MAX_RECORDS):
        _notification_invalid()
    for item, record in ledger["records"].items():
        conversion = conversions.get(item)
        if (conversion is None or type(record) is not dict or set(record) != _NOTICE_RECORD_FIELDS
                or not _integer(record["gold"], 1) or record["gold"] != conversion["gold"]
                or not _one_mail(record["mail_ids"]) or record["mail_ids"] != conversion["mail_ids"]
                or _moment(record["notified_at"]) is None or record["notified_at"] < conversion["settled_at"]):
            _notification_invalid("Expiry notices must reference their permanent settled conversion")
    announced = set()
    for notice in ledger.get(ARCHIVED_KEY, ()):
        valid = type(notice) is dict and set(notice) == _NOTICE_FIELDS and _one_mail(notice["mail_ids"])
        conversion = archived.get(notice["mail_ids"][0]) if valid else None
        if (conversion is None or notice["mail_ids"][0] in announced or notice["item_id"] != conversion["item_id"]
                or not _integer(notice["gold"], 1) or notice["gold"] != conversion["gold"]
                or _moment(notice["notified_at"]) is None
                or not conversion["settled_at"] <= notice["notified_at"] <= conversion["archived_at"]):
            _notification_invalid("Archived expiry notices must reference their archived settled conversion")
        announced.add(notice["mail_ids"][0])
    return ledger["records"], ledger.get(ARCHIVED_KEY, [])


def validate_vote_expiry_notifications(notices, count):
    if type(notices) is not list or not _integer(count, maximum=MAX_RECORDS) or len(notices) > count:
        _notification_invalid("Vote expiry notices require a bounded permanent proof count")
    items, moments, total = set(), set(), 0
    for notice in notices:
        if (type(notice) is not dict or set(notice) != _NOTICE_FIELDS
                or type(notice["item_id"]) is not str or not notice["item_id"] or notice["item_id"] in items
                or not _integer(notice["gold"], 1) or not _one_mail(notice["mail_ids"])
                or _moment(notice["notified_at"]) is None):
            _notification_invalid("Vote expiry notice fields are invalid")
        items.add(notice["item_id"])
        moments.add(notice["notified_at"])
        total += notice["gold"]
    if len(moments) > 1 or total > INT32_MAX:
        _notification_invalid("One response notifies a single batch of converted gold")
    return total


class VoteExpiryService:
    def __init__(self, catalog, presents, possessions):
        self.catalog, self.presents, self.possessions = catalog, presents, possessions
        try:
            descriptions = _catalog_texts(catalog, ("SystemTextVoteInfomation", "SystemTextVoteInfomation5"))
            if (descriptions[0] != descriptions[1]
                    or "1枚50ゴールドへと変換されます。" not in descriptions[0]
                    or "各グループの投票期間終了直後の\nメンテナンス後に配布されます。" not in descriptions[0]):
                raise ValueError("Vote expiry source no longer specifies the verified conversion")
            raw = catalog._resources["ItemConfigTable.atb"]
            self._item_hash = hashlib.sha256(decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw).hexdigest()
            self._ordinary_items = frozenset(row["ItemID"] for row in _catalog_rows(catalog, "ItemConfigTable.atb")
                if row["Category"] == 1925062086 and row["EffectType"] == 10 and row["Stack"] == 999
                and row["CanAllGet"] == 1 and row["ReceiveAction"] == 0 and row["EventID"] == ""
                and row["ReceiveConfirmation"] == 0)
            self.content_version = self._content()
            self._ready()
        except ProtocolError:
            raise
        except (AttributeError, KeyError, IndexError, TypeError, ValueError, OSError) as error:
            raise ProtocolError("vote_expiry_source_invalid", "Vote expiry source or dependencies are unavailable", 503) from error

    def _content(self):
        try:
            versions = {"catalog": self.catalog.content_version, "presents": self.presents.asset_sha256,
                        "possessions": self.possessions.asset_sha256}
            if any(not _hash(value) for value in versions.values()):
                raise ValueError("Missing dependency fingerprints")
            return _digest({"schema": 1, "dependencies": versions, "rate": GOLD_PER_TICKET,
                "description": DESCRIPTION, "observation": "first_observed_at_or_after_vote_end",
                "delivery": "one_gold_mail_atomic_group_debit", "empty": "close_participated_item_only",
                "restart": "item_identity_never_reopens"})
        except (AttributeError, TypeError, ValueError) as error:
            raise ProtocolError("vote_expiry_dependencies_invalid", "Vote expiry requires stable source fingerprints", 503) from error

    def _ready(self):
        self.catalog._ready()
        self.presents._ready()
        if (self._content() != self.content_version or not self.possessions.is_ready()
                or self.possessions.asset_sha256 != self._item_hash
                or self.presents.possessions is not self.possessions
                or any(group.item_id not in self._ordinary_items for group in self.catalog.groups)
                or any(self.possessions.stack(group.item_id) != 999 for group in self.catalog.groups)):
            raise ProtocolError(DEPENDENCIES_CHANGED, "Vote expiry dependencies changed after setup", 503)

    def _observed(self, item, entry, fields):
        try:
            vote_key(item, 1)
            self.possessions.stack(item, stored=True)
            if item not in self._ordinary_items:
                _invalid("Vote expiry records require a verified ordinary vote ticket")
        except ProtocolError as error:
            raise ProtocolError("vote_expiry_state_invalid", "Vote expiry item identity is unavailable", 503) from error
        if (not isinstance(entry, dict) or set(entry) != fields
                or not _integer(entry["group_id"], 1) or not _integer(entry["amount"], maximum=999)
                or type(entry["rate"]) is not int or entry["rate"] != GOLD_PER_TICKET
                or entry["status"] not in (PENDING, SETTLED, CLOSED)
                or not isinstance(entry["mail_ids"], list)):
            _invalid("Vote expiry entry fields are invalid")
        _stored_date(entry["source_window_end"])
        window_end = _stored_date(entry["window_end"])
        observed = _stored_stamp(entry["observed_at"])
        if observed < window_end or entry["definition"] != _definition(item, entry["group_id"], entry["rate"], entry["source_window_end"]):
            _invalid("Vote expiry eligibility or frozen definition is invalid")
        return observed

    @staticmethod
    def _completed(state, entry, observed, stock, mail_ids, presents):
        settled = _stored_stamp(entry["settled_at"])
        if settled < observed or entry["reason"] is not None:
            _invalid("Completed vote expiry has an invalid completion time")
        if stock:
            raise ProtocolError("vote_expiry_already_closed", "A settled vote ticket reappeared and requires an explicit new instance", 503)
        if entry["status"] == CLOSED:
            if entry["amount"] or entry["mail_ids"]:
                _invalid("Zero-stock vote closure cannot have a reward")
        else:
            if not entry["amount"] or len(entry["mail_ids"]) != 1 or not _integer(entry["mail_ids"][0], 1):
                _invalid("Settled vote expiry requires exactly one gold mail")
            identifier = entry["mail_ids"][0]
            if not _issued(state, identifier, mail_ids):
                _invalid("Vote expiry mail identity is invalid or reused")
            mail_ids.add(identifier)
            expected = _gold_row(identifier, entry["amount"] * entry["rate"], DESCRIPTION)
            if identifier in presents and presents[identifier] != expected:
                _invalid("Vote expiry mail differs from its permanent record")
        return settled

    def _state(self, state, *, reopening=False):
        self._ready()
        stock = self.possessions.counts(state)
        participating = set(stock) | {row["itemId"] for row in read_vote_history(state)}
        current_items = {group.item_id for group in self.catalog.groups}
        if any(stock.get(item, 0) > 999 for item in current_items):
            _invalid("Vote group ticket stock exceeds its verified capacity")
        if "vote_expiry" not in state:
            return stock, participating, None
        ledger = state["vote_expiry"]
        if (not isinstance(ledger, dict) or set(ledger) - {ARCHIVED_KEY} != {"schema_version", "items"}
                or type(ledger["schema_version"]) is not int or ledger["schema_version"] != 1
                or not isinstance(ledger["items"], dict)
                or ARCHIVED_KEY in ledger and (type(ledger[ARCHIVED_KEY]) is not list or not ledger[ARCHIVED_KEY])
                or len(ledger["items"]) + len(ledger.get(ARCHIVED_KEY, ())) > MAX_RECORDS):
            _invalid("Vote expiry journal fields are invalid")
        mail_ids, reopened = set(), {}
        presents = {row["PresentID"]: row for row in self.presents.rows(state)}
        for record in ledger.get(ARCHIVED_KEY, ()):
            if type(record) is not dict or set(record) != _ARCHIVED_FIELDS or record["status"] not in _COMPLETED:
                _invalid("Archived vote expiry records are invalid")
            item = record["item_id"]
            observed = self._observed(item, record, _ARCHIVED_FIELDS)
            settled = self._completed(state, record, observed, 0, mail_ids, presents)
            archived = _stored_stamp(record["archived_at"])
            if archived < settled or observed < reopened.get(item, observed):
                _invalid("Archived vote expiry records must follow each other in time")
            reopened[item] = archived
        for item, entry in ledger["items"].items():
            observed = self._observed(item, entry, _ENTRY_FIELDS)
            if observed < reopened.get(item, observed):
                _invalid("A reopened vote ticket cannot be observed before its archive")
            if entry["status"] == PENDING:
                if (not entry["amount"] or entry["settled_at"] is not None or entry["mail_ids"]
                        or type(entry["reason"]) is not str or _REASON.fullmatch(entry["reason"]) is None):
                    _invalid("Pending vote expiry has an invalid delivery state")
                if stock.get(item, 0) != entry["amount"]:
                    raise ProtocolError("vote_expiry_inventory_changed", "Pending expired ticket stock changed after observation", 503)
            else:
                self._completed(state, entry, observed, 0 if reopening else stock.get(item, 0), mail_ids, presents)
        return stock, participating, ledger

    def ledger(self, state):
        journal = self._state(state)[2]
        return {} if journal is None else journal["items"]

    def archived(self, state):
        journal = self._state(state)[2]
        return [] if journal is None else journal.get(ARCHIVED_KEY, [])

    def initialize(self, state):
        self._state(state)
        return deepcopy(state)

    def conversions(self, state):
        return self.settled_conversions(state)[0]

    def settled_conversions(self, state):
        return _converted(self._state(state)[2])

    def reopen_settled(self, state, item_id, now):
        # Every reappeared ticket blocks the save, so each reopen tolerates the others until all of them are reopened.
        stock, _, ledger = self._state(state, reopening=True)
        timestamp = _clock(now)[1]
        entry = None if ledger is None else ledger["items"].get(item_id)
        if entry is None or entry["status"] not in _COMPLETED or not stock.get(item_id, 0):
            raise ProtocolError(REOPEN_UNNEEDED, "Only a completed vote ticket whose stock reappeared reopens", 409)
        notice = _notices(state, *_converted(ledger))[0].get(item_id)
        if timestamp < max((entry["settled_at"], *(() if notice is None else (notice["notified_at"],)))):
            raise ProtocolError("vote_expiry_clock_reversed", "Vote expiry observation predates saved progress", 409)
        updated = deepcopy(state)
        journal = updated["vote_expiry"]
        record = {"item_id": item_id, **journal["items"].pop(item_id), "archived_at": timestamp}
        # The new instance starts from the reappeared stock; its proof and notice stay readable in the archive.
        _archive(journal, record)
        if notice is not None:
            notices = updated[NOTIFICATION_KEY]
            notice = {"item_id": item_id, **notices["records"].pop(item_id)}
            _archive(notices, notice)
        _notices(updated, *_converted(self._state(updated, reopening=True)[2]))
        reappeared = sorted(item for item, other in journal["items"].items()
                            if other["status"] in _COMPLETED and stock.get(item, 0))
        return updated, {"archived": deepcopy(record), "notice": deepcopy(notice), "reappeared": reappeared}

    def require_open(self, state, group):
        _, _, ledger = self._state(state)
        try:
            if self.catalog.groups_by_id.get(group.group_id) != group:
                raise ValueError("Unknown vote group")
        except (AttributeError, TypeError, ValueError) as error:
            raise ProtocolError("vote_expiry_group_invalid", "Vote eligibility requires a verified catalog group") from error
        if ledger is not None and group.item_id in ledger["items"]:
            raise ProtocolError(TICKET_CLOSED, "This vote ticket identity has already reached expiry", 409)

    def _debit(self, state, item, amount):
        expected = deepcopy(state)
        row = next(row for row in expected["tables"]["possessions"] if row["ItemID"] == item)
        row["Count"] -= amount
        updated = self.possessions.debit(deepcopy(state), {item: amount})
        if updated != expected:
            raise ProtocolError("vote_expiry_inventory_invalid", "Vote expiry debit changed unrelated state", 503)
        return updated

    def settle(self, state, now):
        stock, participating, ledger = self._state(state)
        current, timestamp = _clock(now)
        updated = deepcopy(state)
        report = {"grants": [], "pending": [], "closed": []}
        if ledger is not None:
            moments = [entry["settled_at"] if entry["settled_at"] is not None else entry["observed_at"]
                       for entry in ledger["items"].values()]
            if any(timestamp < moment for moment in (*moments, *(record["archived_at"] for record in ledger.get(ARCHIVED_KEY, ())))):
                raise ProtocolError("vote_expiry_clock_reversed", "Vote expiry observation predates saved progress", 409)
        if self.catalog.rules.enabled:
            for group in self.catalog.groups:
                if current < group.vote_end or group.item_id not in participating:
                    continue
                journal = updated.setdefault("vote_expiry", {"schema_version": 1, "items": {}})
                items = journal["items"]
                if group.item_id in items:
                    continue
                if len(items) + len(journal.get(ARCHIVED_KEY, ())) >= MAX_RECORDS:
                    raise ProtocolError("vote_expiry_capacity", "Vote expiry identity history is full", 503)
                amount = stock.get(group.item_id, 0)
                source_end = _date(group.source_vote_end)
                items[group.item_id] = {"group_id": group.group_id,
                    "definition": _definition(group.item_id, group.group_id, GOLD_PER_TICKET, source_end),
                    "source_window_end": source_end, "window_end": _date(group.vote_end), "observed_at": timestamp,
                    "amount": amount, "rate": GOLD_PER_TICKET, "status": PENDING if amount else CLOSED,
                    "reason": "awaiting_delivery" if amount else None,
                    "settled_at": None if amount else timestamp, "mail_ids": []}
                if not amount:
                    report["closed"].append(group.item_id)
        if "vote_expiry" not in updated:
            return updated, report
        for item, record in tuple(updated["vote_expiry"]["items"].items()):
            if record["status"] != PENDING:
                continue
            try:
                candidate = self._debit(updated, item, record["amount"])
                candidate, ids = _gold_mail(self.presents, candidate, record["amount"] * record["rate"], DESCRIPTION,
                                            DELIVERY_INVALID)
                candidate["vote_expiry"]["items"][item].update(status=SETTLED, reason=None,
                    settled_at=timestamp, mail_ids=ids)
                self._state(candidate)
            except ProtocolError as failure:
                if failure.code not in CAPACITY_ERRORS:
                    raise
                updated["vote_expiry"]["items"][item]["reason"] = failure.code
                report["pending"].append({"item_id": item, "reason": failure.code})
                continue
            updated = candidate
            report["grants"].append({"item_id": item, "group_id": record["group_id"], "amount": record["amount"],
                                     "rate": record["rate"], "mail_ids": ids})
        self._state(updated)
        return updated, report

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "content_version": self.content_version,
            "gold_per_ticket": GOLD_PER_TICKET, "policy": "First observed expiry; one atomic gold mail per participated group",
            "pending_policy": "Frozen ticket stock retries after expiry, disabled rules or revival window changes",
            "closed_policy": "A ticket identity cannot settle twice; reappearing stock requires explicit migration",
            "native_tested": False}


class VoteExpiryNotifications:
    def __init__(self, expiry):
        self.expiry = expiry
        self.presents = expiry.presents
        self.content_version = self._content()
        self._snapshot = (self.expiry, self.presents, self.content_version)
        self._ready()

    def _content(self):
        version = getattr(self.expiry, "content_version", None)
        if not _hash(version):
            raise ProtocolError("vote_expiry_notifications_unavailable", "Expiry notices require a verified conversion", 503)
        return _digest({"schema": 1, "expiry": version, "group": NOTICE_GROUP, "column": NOTICE_COLUMN,
            "notification": "every_unnotified_settled_conversion_once", "amount": "sum_of_converted_gold",
            "empty": "omit_group", "legacy": "settled_conversion_notifies_once", "proof": "permanent_settled_conversion"})

    def _ready(self):
        if (any(left is not right for left, right in zip(
                (self.expiry, self.presents, self.content_version), self._snapshot))
                or self.presents is not self.expiry.presents or self._content() != self.content_version):
            raise ProtocolError("vote_expiry_notifications_changed", "Expiry notice dependencies changed after setup", 503)
        self.expiry._ready()

    def _records(self, state):
        self._ready()
        conversions, archived = self.expiry.settled_conversions(state)
        return (conversions, *_notices(state, conversions, archived))

    def records(self, state):
        return self._records(state)[1]

    def archived(self, state):
        return self._records(state)[2]

    def initialize(self, state):
        self._records(state)
        return deepcopy(state)

    def count(self, state):
        _, records, archived = self._records(state)
        return len(records) + len(archived)

    def settle(self, state, now):
        conversions, records, archived = self._records(state)
        timestamp = _clock(now)[1]
        pending = sorted((conversion["settled_at"], item) for item, conversion in conversions.items() if item not in records)
        updated = deepcopy(state)
        if not pending:
            return updated, []
        if timestamp < max((pending[-1][0], *(record["notified_at"] for record in (*records.values(), *archived)))):
            raise ProtocolError("vote_expiry_notification_clock_reversed", "Expiry notice predates saved progress", 409)
        ledger = updated.setdefault(NOTIFICATION_KEY, {"schema_version": 1, "records": {}})
        notices = []
        for _, item in pending:
            record = {"gold": conversions[item]["gold"], "mail_ids": list(conversions[item]["mail_ids"]),
                      "notified_at": timestamp}
            ledger["records"][item] = record
            notices.append({"item_id": item, **deepcopy(record)})
        self.validate_receipt(updated, notices, len(ledger["records"]) + len(archived), exact=True)
        return updated, notices

    def validate_receipt(self, state, notices, count, *, exact=False):
        validate_vote_expiry_notifications(notices, count)
        _, records, archived = self._records(state)
        proofs = len(records) + len(archived)
        if proofs < count or exact and proofs != count:
            raise ProtocolError("vote_expiry_notification_proof_lost", "Expiry notice history lost permanent proof", 503)
        for notice in notices:
            if records.get(notice["item_id"]) != {name: notice[name] for name in _NOTICE_RECORD_FIELDS} and notice not in archived:
                _notification_invalid("Expiry notice lost its permanent record")

    def response(self, notices):
        self._ready()
        total = validate_vote_expiry_notifications(notices, len(notices))
        if not notices:
            _notification_invalid("An empty expiry notice omits its native group")
        return Container(NOTICE_GROUP, (Column(NOTICE_COLUMN, "I", (total,)),))

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "content_version": self.content_version,
            "expiry_content_version": self.expiry.content_version, "group": f"{NOTICE_GROUP}.{NOTICE_COLUMN}",
            "notification": "The next new 116 sums the gold of every settled conversion not yet notified, once",
            "legacy": "Conversions settled before notices existed notify once on the next new 116",
            "proof": "Permanent record per settled conversion; the 116 marker keeps the cumulative count",
            "native_tested": False}


def validate_vote_assault_notifications(notices, count):
    if type(notices) is not list or not _integer(count, maximum=MAX_RECORDS) or len(notices) > count:
        _assault_notification_invalid("Assault conversion notices require a bounded permanent proof count")
    events, missions, moments = set(), set(), set()
    for notice in notices:
        if (type(notice) is not dict or set(notice) != _ASSAULT_NOTICE_FIELDS
                or not _integer(notice["event_id"], 1) or notice["event_id"] in events
                or not _integer(notice["mission_id"], 1) or notice["mission_id"] in missions
                or not _integer(notice["gold"], 1) or not _one_mail(notice["mail_ids"])
                or _moment(notice["notified_at"]) is None):
            _assault_notification_invalid("Assault conversion notice fields are invalid")
        events.add(notice["event_id"])
        missions.add(notice["mission_id"])
        moments.add(notice["notified_at"])
    if len(moments) > 1:
        _assault_notification_invalid("One response notifies a single batch of converted rights")
    return tuple(notice["mission_id"] for notice in notices)


def _assault_ledger(state):
    if ASSAULT_KEY not in state:
        return None
    ledger = state[ASSAULT_KEY]
    if (type(ledger) is not dict or set(ledger) != {"schema_version", "events"}
            or type(ledger["schema_version"]) is not int or ledger["schema_version"] != 1
            or type(ledger["events"]) is not dict or not 0 < len(ledger["events"]) <= MAX_RECORDS):
        _assault_invalid("Assault conversion journal fields are invalid")
    for key, entry in ledger["events"].items():
        if (not _event_key(key) or type(entry) is not dict or set(entry) != _ASSAULT_FIELDS
                or not _integer(entry["earned"]) or not _integer(entry["spent"])
                or not _integer(entry["amount"], 1) or entry["amount"] != entry["earned"] - entry["spent"]
                or type(entry["rate"]) is not int or entry["rate"] != ASSAULT_GOLD_PER_RIGHT
                or not _integer(entry["amount"] * entry["rate"], 1)
                or entry["status"] not in (PENDING, SETTLED) or type(entry["mail_ids"]) is not list
                or entry["definition"] != _assault_definition(int(key), entry["rate"], entry["source_window_end"])):
            _assault_invalid("Assault conversion entry fields are invalid")
        window_end, observed = _canonical_date(entry["window_end"]), _moment(entry["observed_at"])
        if (_canonical_date(entry["source_window_end"]) is None or window_end is None or observed is None
                or observed < window_end):
            _assault_invalid("Assault conversion eligibility is invalid")
        if entry["status"] == PENDING:
            if (entry["settled_at"] is not None or entry["mail_ids"]
                    or type(entry["reason"]) is not str or _REASON.fullmatch(entry["reason"]) is None):
                _assault_invalid("Pending assault conversion has an invalid delivery state")
        else:
            settled = _moment(entry["settled_at"])
            if settled is None or settled < observed or entry["reason"] is not None or not _one_mail(entry["mail_ids"]):
                _assault_invalid("Settled assault conversion requires one gold mail after its observation")
    return ledger


def converted_assault_rights(state, event_id):
    ledger = _assault_ledger(state)
    entry = None if ledger is None else ledger["events"].get(str(event_id))
    return entry["amount"] if entry is not None and entry["status"] == SETTLED else 0


class VoteAssaultExpiryService:
    def __init__(self, rewards):
        self.rewards = rewards
        try:
            self.catalog, self.presents = rewards.catalog, rewards.presents
            source = _catalog_texts(self.catalog, (ASSAULT_SOURCE_TEXT,))[0]
            if ([int(rate) for rate in _ASSAULT_RATE.findall(source)] != [ASSAULT_GOLD_PER_RIGHT]
                    or any(phrase not in source for phrase in ASSAULT_SOURCE_PHRASES)):
                raise ValueError("Assault conversion source no longer specifies the verified rate")
            self.content_version = self._content()
            self._ready()
        except ProtocolError:
            raise
        except (AttributeError, KeyError, IndexError, TypeError, ValueError, OSError) as error:
            raise ProtocolError("vote_assault_expiry_source_invalid",
                                "Assault conversion source or dependencies are unavailable", 503) from error

    def content_version_for(self, rewards_version):
        if not _hash(rewards_version):
            raise ProtocolError("vote_assault_expiry_dependencies_invalid", "Assault conversion requires verified rewards", 503)
        return _digest({"schema": 1, "rewards": rewards_version, "rate": ASSAULT_GOLD_PER_RIGHT,
            "description": ASSAULT_DESCRIPTION, "observation": "first_observed_at_or_after_final_vote_end",
            "amount": "earned_minus_spent", "delivery": "one_gold_mail_per_event", "empty": "no_record",
            "restart": "event_identity_never_reopens"})

    def _content(self):
        return self.content_version_for(getattr(self.rewards, "content_version", None))

    def replayable_content_versions(self):
        return frozenset(map(self.content_version_for, self.rewards.replayable_content_versions()))

    def _ready(self):
        self.rewards._ready()
        if (self.rewards.catalog is not self.catalog or self.rewards.presents is not self.presents
                or self._content() != self.content_version):
            raise ProtocolError("vote_assault_expiry_dependencies_changed",
                                "Assault conversion dependencies changed after setup", 503)

    def _state(self, state):
        self._ready()
        rights = self.rewards.assault_rights(state)
        ledger = _assault_ledger(state)
        if ledger is None:
            return rights, None
        presents = {row["PresentID"]: row for row in self.presents.rows(state)}
        mail_ids = set()
        for key, entry in ledger["events"].items():
            if key not in rights:
                _assault_invalid("Assault conversion has no saved challenge rights")
            if rights[key] != {"earned": entry["earned"], "spent": entry["spent"]}:
                raise ProtocolError("vote_assault_expiry_rights_changed",
                                    "Challenge rights changed after their conversion was observed", 503)
            if entry["status"] == SETTLED:
                identifier = entry["mail_ids"][0]
                if not _issued(state, identifier, mail_ids):
                    _assault_invalid("Assault conversion mail identity is invalid or reused")
                mail_ids.add(identifier)
                expected = _gold_row(identifier, entry["amount"] * entry["rate"], ASSAULT_DESCRIPTION)
                if identifier in presents and presents[identifier] != expected:
                    _assault_invalid("Assault conversion mail differs from its permanent record")
        return rights, ledger

    def ledger(self, state):
        journal = self._state(state)[1]
        return {} if journal is None else journal["events"]

    def initialize(self, state):
        self._state(state)
        return deepcopy(state)

    def conversions(self, state):
        _, ledger = self._state(state)
        if ledger is None:
            return {}
        return {key: {"gold": entry["amount"] * entry["rate"], "mail_ids": list(entry["mail_ids"]),
                      "settled_at": entry["settled_at"]}
                for key, entry in ledger["events"].items() if entry["status"] == SETTLED}

    def require_open(self, state):
        _, ledger = self._state(state)
        if ledger is not None and str(self.catalog.event.event_id) in ledger["events"]:
            raise ProtocolError("vote_assault_closed", "This vote event already converted its challenge rights", 409)

    def settle(self, state, now):
        rights, ledger = self._state(state)
        current, timestamp = _clock(now)
        if ledger is not None and any(
                timestamp < (entry["observed_at"] if entry["settled_at"] is None else entry["settled_at"])
                for entry in ledger["events"].values()):
            raise ProtocolError("vote_assault_expiry_clock_reversed", "Assault conversion predates saved progress", 409)
        updated = deepcopy(state)
        report = {"grants": [], "pending": []}
        event = self.catalog.event
        event_key = str(event.event_id)
        entitlement = rights.get(event_key)
        if (self.catalog.rules.enabled and current >= event.end_at and entitlement is not None
                and entitlement["earned"] > entitlement["spent"] and (ledger is None or event_key not in ledger["events"])):
            events = updated.setdefault(ASSAULT_KEY, {"schema_version": 1, "events": {}})["events"]
            if len(events) >= MAX_RECORDS:
                raise ProtocolError("vote_assault_expiry_capacity", "Assault conversion history is full", 503)
            source_end = _date(event.source_end)
            events[event_key] = {"definition": _assault_definition(event.event_id, ASSAULT_GOLD_PER_RIGHT, source_end),
                "source_window_end": source_end, "window_end": _date(event.end_at), "observed_at": timestamp,
                "earned": entitlement["earned"], "spent": entitlement["spent"],
                "amount": entitlement["earned"] - entitlement["spent"], "rate": ASSAULT_GOLD_PER_RIGHT,
                "status": PENDING, "reason": "awaiting_delivery", "settled_at": None, "mail_ids": []}
        if ASSAULT_KEY not in updated:
            return updated, report
        for key, record in tuple(updated[ASSAULT_KEY]["events"].items()):
            if record["status"] != PENDING:
                continue
            try:
                candidate, ids = _gold_mail(self.presents, updated, record["amount"] * record["rate"],
                                            ASSAULT_DESCRIPTION, ASSAULT_DELIVERY_INVALID)
                candidate[ASSAULT_KEY]["events"][key].update(status=SETTLED, reason=None, settled_at=timestamp,
                                                             mail_ids=ids)
                self._state(candidate)
            except ProtocolError as failure:
                if failure.code not in CAPACITY_ERRORS:
                    raise
                updated[ASSAULT_KEY]["events"][key]["reason"] = failure.code
                report["pending"].append({"event_id": int(key), "reason": failure.code})
                continue
            updated = candidate
            report["grants"].append({"event_id": int(key), "amount": record["amount"], "rate": record["rate"],
                                     "mail_ids": ids})
        self._state(updated)
        return updated, report

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "content_version": self.content_version,
            "gold_per_right": ASSAULT_GOLD_PER_RIGHT,
            "policy": "The first 57 or 137 at or after the final vote end mails earned minus spent rights as one gold present",
            "pending_policy": "Frozen rights retry after a full mailbox, disabled rules or revival window changes",
            "closed_policy": "A converted event never reopens voting; zero unused rights leave no record",
            "native_tested": False}


def _mission_tables(asset_reader):
    tables, hashes = {}, {}
    for name, expected in MISSION_SOURCES.items():
        raw = asset_reader(name)
        if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
            raise ValueError("Assault mission sources must be bounded bytes")
        parsed = parse_table(decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw, LIMITS)
        fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
        if len(fields) != len(parsed["fields"]) or any(fields.get(key) != kind for key, kind in expected.items()):
            raise ValueError("Assault mission source fields differ from the verified schema")
        tables[name], hashes[name] = parsed["rows"], hashlib.sha256(raw).hexdigest()
    return tables, MappingProxyType(hashes)


def _assault_mission(event_id, tables):
    name = ASSAULT_MISSION_NAME.format(event_id)
    configs = [row["MissionID"] for row in tables["AssaultMissionConfig.atb"]
               if type(row["Name"]) is str and unicodedata.normalize("NFKC", row["Name"]) == name]
    if len(configs) != 1:
        raise ValueError("The vote event must name exactly one assault mission")
    quests = [row["QuestID"] for row in tables["AssaultMissionQuestList.atb"] if row["MissionID"] == configs[0]]
    if len(quests) != 1:
        raise ValueError("The assault mission must list exactly one quest")
    missions = [row["ID_Mission"] for row in tables["EmcMissionConfig.atb"]
                if row["Type_Quest"] == ASSAULT_QUEST_TYPE and row["ID_QuestFirst"] <= quests[0] <= row["ID_QuestLast"]]
    if len(missions) != 1 or not _integer(missions[0], 1):
        raise ValueError("The assault quest must belong to exactly one Type_Quest 7 mission")
    return missions[0]


class VoteAssaultNotifications:
    def __init__(self, expiry, asset_reader):
        self.expiry = expiry
        try:
            self.presents = expiry.presents
            self.event_key = str(expiry.catalog.event.event_id)
            tables, self._sources = _mission_tables(asset_reader)
            self.mission_id = _assault_mission(expiry.catalog.event.event_id, tables)
        except (AttributeError, KeyError, TypeError, ValueError, OSError, OverflowError) as error:
            raise ProtocolError("vote_assault_notifications_source_invalid",
                                "Assault notices require the event's verified Type_Quest 7 mission", 503) from error
        self.content_version = self._content()
        self._snapshot = (self.expiry, self.presents, self.content_version)
        self._ready()

    def content_version_for(self, expiry_version):
        if not _hash(expiry_version):
            raise ProtocolError("vote_assault_notifications_unavailable", "Assault notices require a verified conversion", 503)
        return _digest({"schema": 1, "expiry": expiry_version, "group": RET_GROUP, "column": RET_COLUMN,
            "quest_type": ASSAULT_QUEST_TYPE, "mission_id": self.mission_id, "sources": dict(self._sources),
            "notification": "event_conversion_once", "empty": "omit_group", "proof": "permanent_settled_conversion"})

    def _content(self):
        return self.content_version_for(getattr(self.expiry, "content_version", None))

    def replayable_content_versions(self):
        return frozenset(map(self.content_version_for, self.expiry.replayable_content_versions()))

    def _ready(self):
        if (any(left is not right for left, right in zip(
                (self.expiry, self.presents, self.content_version), self._snapshot))
                or self.presents is not self.expiry.presents or self._content() != self.content_version):
            raise ProtocolError("vote_assault_notifications_changed", "Assault notice dependencies changed after setup", 503)
        self.expiry._ready()

    def _records(self, state):
        self._ready()
        conversions = self.expiry.conversions(state)
        if ASSAULT_NOTIFICATION_KEY not in state:
            return conversions, {}
        ledger = state[ASSAULT_NOTIFICATION_KEY]
        if (type(ledger) is not dict or set(ledger) != {"schema_version", "records"}
                or type(ledger["schema_version"]) is not int or ledger["schema_version"] != 1
                or type(ledger["records"]) is not dict or not 0 < len(ledger["records"]) <= MAX_RECORDS):
            _assault_notification_invalid()
        missions = set()
        for key, record in ledger["records"].items():
            conversion = conversions.get(key)
            if (conversion is None or type(record) is not dict or set(record) != _ASSAULT_NOTICE_RECORD_FIELDS
                    or not _integer(record["mission_id"], 1) or record["mission_id"] in missions
                    or not _integer(record["gold"], 1) or record["gold"] != conversion["gold"]
                    or not _one_mail(record["mail_ids"]) or record["mail_ids"] != conversion["mail_ids"]
                    or _moment(record["notified_at"]) is None or record["notified_at"] < conversion["settled_at"]):
                _assault_notification_invalid("Assault notices must reference their permanent settled conversion")
            missions.add(record["mission_id"])
        return conversions, ledger["records"]

    def records(self, state):
        return self._records(state)[1]

    def initialize(self, state):
        self._records(state)
        return deepcopy(state)

    def count(self, state):
        return len(self.records(state))

    def settle(self, state, now):
        conversions, records = self._records(state)
        timestamp = _clock(now)[1]
        updated = deepcopy(state)
        conversion = conversions.get(self.event_key)
        if conversion is None or self.event_key in records:
            return updated, []
        if timestamp < max((conversion["settled_at"], *(record["notified_at"] for record in records.values()))):
            raise ProtocolError("vote_assault_notification_clock_reversed", "Assault notice predates saved progress", 409)
        record = {"mission_id": self.mission_id, "gold": conversion["gold"], "mail_ids": list(conversion["mail_ids"]),
                  "notified_at": timestamp}
        ledger = updated.setdefault(ASSAULT_NOTIFICATION_KEY, {"schema_version": 1, "records": {}})
        ledger["records"][self.event_key] = record
        notices = [{"event_id": int(self.event_key), **deepcopy(record)}]
        self.validate_receipt(updated, notices, len(ledger["records"]), exact=True)
        return updated, notices

    def validate_receipt(self, state, notices, count, *, exact=False):
        validate_vote_assault_notifications(notices, count)
        _, records = self._records(state)
        if len(records) < count or exact and len(records) != count:
            raise ProtocolError("vote_assault_notification_proof_lost", "Assault notice history lost permanent proof", 503)
        for notice in notices:
            if records.get(str(notice["event_id"])) != {name: notice[name] for name in _ASSAULT_NOTICE_RECORD_FIELDS}:
                _assault_notification_invalid("Assault notice lost its permanent record")

    def response(self, notices):
        self._ready()
        missions = validate_vote_assault_notifications(notices, len(notices))
        if not notices:
            _assault_notification_invalid("An empty assault notice omits its native group")
        return Container(RET_GROUP, (Column(RET_COLUMN, "I", missions),))

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "content_version": self.content_version,
            "expiry_content_version": self.expiry.content_version, "group": f"{RET_GROUP}.{RET_COLUMN}",
            "mission_id": self.mission_id,
            "notification": "The next new 116 after the event's settled conversion carries one RET row, once",
            "proof": "Permanent record per settled conversion; the 116 marker keeps the cumulative count",
            "native_tested": False}
