from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re

from .alxml import Column, Container
from .energy_clock import instant
from .errors import ProtocolError
from .native_numbers import INT32_MAX
from .presents import MAX_PRESENTS, PRESENT_CAPACITY_ERRORS as CAPACITY_ERRORS
from .vote_cooking_rules import VoteCookingRules
from .vote_state import read_vote_history, vote_key


MAX_GROUPS = 4096
MAX_SUPERSEDED = MAX_GROUPS
SUPERSEDED_KEY = "superseded"
TRACKING, PENDING, DELIVERED, INELIGIBLE = "tracking", "pending", "delivered", "ineligible"
GRANT_FIELDS = frozenset(("item_id", "group_id", "definition", "card_id", "count", "receive_text_id", "mail_ids"))
ENTRY_FIELDS = frozenset(("group_id", "event_id", "definition", "source", "source_catalog", "baseline", "counts", "started_at",
    "last_time", "vote_end", "release_at", "minimum_votes", "copies", "status", "winner", "winning_votes",
    "reason", "mail_ids", "delivered_at", "receive_text_id", "description"))
DEFINITION_FIELDS = ("source_catalog", "source", "event_id", "group_id", "minimum_votes", "copies", "receive_text_id",
                     "description")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[1-9][0-9]{0,3}\Z")
_REASON = re.compile(r"[a-zA-Z0-9_]{1,128}\Z")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _integer(value, minimum=0, maximum=INT32_MAX):
    return type(value) is int and minimum <= value <= maximum


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _text(value):
    try:
        return type(value) is str and 0 < len(value.encode("utf-8")) <= 4096
    except UnicodeError:
        return False


def _invalid(message="Stored vote cooking state is invalid"):
    raise ProtocolError("vote_cooking_state_invalid", message, 503)


def _parameters_valid(parameters):
    return (_integer(parameters["group_id"], 1, 4) and _integer(parameters["event_id"], 1)
            and _hash(parameters["source"]) and _hash(parameters["source_catalog"])
            and _integer(parameters["minimum_votes"], 1, 999) and _integer(parameters["copies"], 1, 10)
            and _integer(parameters["receive_text_id"]) and _text(parameters["description"]))


def _time(value):
    try:
        return instant(value).astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, TypeError, OverflowError, ProtocolError) as error:
        raise ProtocolError("vote_cooking_clock_invalid", "Vote cooking requires a valid instant") from error


def _saved_time(value):
    try:
        if type(value) is not str or len(value) != 32 or _time(datetime.fromisoformat(value)) != value:
            raise ValueError("Noncanonical time")
        return value
    except (ValueError, TypeError, ProtocolError):
        _invalid("Vote cooking timestamps require canonical UTC instants")


def _counts(state, item, group_id):
    rows = [row for row in read_vote_history(state) if row["itemId"] == item]
    if any(row["groupId"] != group_id for row in rows):
        _invalid("Vote cooking ticket belongs to a different group")
    return {str(row["cardId"]): row["count"] for row in rows}


def _winner(entry):
    eligible = [(count - entry["baseline"].get(card, 0), int(card)) for card, count in entry["counts"].items()
                if count - entry["baseline"].get(card, 0) >= entry["minimum_votes"]]
    if not eligible:
        return None, 0
    votes, card = min(eligible, key=lambda pair: (-pair[0], pair[1]))
    return card, votes


def validate_vote_cooking_grants(grants):
    if not isinstance(grants, list) or len(grants) > MAX_PRESENTS:
        _invalid("Vote cooking grants require a bounded list")
    ids, items = [], set()
    for grant in grants:
        if (not isinstance(grant, dict) or set(grant) != GRANT_FIELDS or not _hash(grant["definition"])
                or not _integer(grant["group_id"], 1, 4) or not _integer(grant["card_id"], 1, 9999)
                or not _integer(grant["count"], 1, 10) or not _integer(grant["receive_text_id"])
                or not isinstance(grant["mail_ids"], list) or len(grant["mail_ids"]) != grant["count"]
                or any(not _integer(identifier, 1) for identifier in grant["mail_ids"])):
            _invalid("Vote cooking grant fields are invalid")
        vote_key(grant["item_id"], grant["card_id"])
        if grant["item_id"] in items:
            _invalid("Vote cooking grant repeats a group")
        items.add(grant["item_id"])
        ids.extend(grant["mail_ids"])
    if len(ids) > MAX_PRESENTS or len(set(ids)) != len(ids):
        _invalid("Vote cooking grants reuse mail identities")
    return ids


def vote_cooking_response(grants, *, receive_text_id=None):
    validate_vote_cooking_grants(grants)
    if receive_text_id is not None and not _integer(receive_text_id):
        _invalid("Vote cooking notification requires a verified text index")
    return Container("VoteGiftParam", tuple(Column(name, "I", tuple(
        receive_text_id if key == "receive_text_id" and receive_text_id is not None else grant[key]
        for grant in grants)) for name, key in (
            ("CardId", "card_id"), ("Count", "count"), ("ReceiveTextID", "receive_text_id"))))


class VoteCookingService:
    def __init__(self, catalog, recipes, presents, rules=None):
        self.catalog, self.recipes, self.presents = catalog, recipes, presents
        self.rules = VoteCookingRules() if rules is None else rules
        if not isinstance(self.rules, VoteCookingRules):
            raise ValueError("Vote cooking requires verified rules")
        self.content_version = self._content()
        self._ready()

    def _content(self):
        return _digest({"schema": 1, "catalog": self.catalog.content_version, "recipes": self.recipes.content_version,
            "presents": self.presents.asset_sha256, "rules": self.rules.to_dict(), "source": "new_57_votes_only",
            "selection": "group_increment_max_then_lowest_card_id", "release": "original_group_result_start",
            "delivery": "one_group_per_115_atomic_mail", "present_type": 21})

    def _ready(self):
        self.catalog._ready()
        self.recipes._ready()
        self.presents._ready()
        if (self.presents.vote_cooking_catalog is not self.recipes or self._content() != self.content_version):
            raise ProtocolError("vote_cooking_dependencies_changed", "Vote cooking sources changed after setup", 503)

    def _recipe(self):
        return {"source": self.recipes.content_version, "receive_text_id": self.recipes.receive_text_id,
                "description": self.recipes.description}

    def _parameters(self, group):
        return {"source_catalog": self.catalog.source_content_version, "event_id": self.catalog.event.event_id,
                "group_id": group.group_id, "minimum_votes": self.rules.minimum_votes, "copies": self.rules.copies,
                **self._recipe()}

    def _definition(self, group):
        return self._saved_definition(group.item_id, self._parameters(group))

    @staticmethod
    def _saved_definition(item, entry):
        return _digest({"schema": 1, "source": entry["source_catalog"], "recipes": entry["source"],
            "event": entry["event_id"], "item": item, "group": entry["group_id"],
            "minimum_votes": entry["minimum_votes"], "copies": entry["copies"],
            "receive_text_id": entry["receive_text_id"], "description": entry["description"],
            "selection": "increment_max_then_lowest_card_id"})

    def _definitions(self, item, entry):
        return {entry["definition"], *(self._saved_definition(item, old) for old in entry.get(SUPERSEDED_KEY, ()))}

    def ledger(self, state):
        self._ready()
        read_vote_history(state)
        if "vote_cooking" not in state:
            return {}
        journal = state["vote_cooking"]
        if (not isinstance(journal, dict) or set(journal) != {"schema_version", "groups"}
                or type(journal["schema_version"]) is not int or journal["schema_version"] != 1
                or not isinstance(journal["groups"], dict) or not 0 < len(journal["groups"]) <= MAX_GROUPS):
            _invalid()
        mail_ids = set()
        mail = {row["PresentID"]: row for row in self.presents.rows(state)}
        for item, entry in journal["groups"].items():
            vote_key(item, 1)
            if (not isinstance(entry, dict) or set(entry) - {SUPERSEDED_KEY} != ENTRY_FIELDS
                    or not _parameters_valid(entry) or not _hash(entry["definition"])
                    or entry["winner"] is not None and not _integer(entry["winner"], 1, 9999)
                    or not _integer(entry["winning_votes"], 0, 999)
                    or entry["status"] not in (TRACKING, PENDING, DELIVERED, INELIGIBLE)
                    or not isinstance(entry["mail_ids"], list)):
                _invalid()
            if entry["definition"] != self._saved_definition(item, entry):
                _invalid("Vote cooking frozen parameters differ from their definition")
            superseded = entry.get(SUPERSEDED_KEY, [])
            if (not isinstance(superseded, list) or SUPERSEDED_KEY in entry and not 0 < len(superseded) <= MAX_SUPERSEDED
                    or any(not isinstance(old, dict) or set(old) != set(DEFINITION_FIELDS) or not _parameters_valid(old)
                           or old["group_id"] != entry["group_id"] for old in superseded)
                    or len(self._definitions(item, entry)) != len(superseded) + 1):
                _invalid("Vote cooking superseded definitions are invalid")
            for name in ("baseline", "counts"):
                mapping = entry[name]
                if (not isinstance(mapping, dict) or len(mapping) > 4096
                        or any(type(card) is not str or not _ID.fullmatch(card) or not _integer(count, 0, 999)
                               for card, count in mapping.items())):
                    _invalid("Vote cooking counts are invalid")
            if (not entry["baseline"].keys() <= entry["counts"].keys()
                    or any(entry["counts"][card] < count for card, count in entry["baseline"].items())
                    or entry["counts"] != _counts(state, item, entry["group_id"])):
                _invalid("Vote cooking progress differs from native history")
            start, last, end, release = (_saved_time(entry[name]) for name in
                                        ("started_at", "last_time", "vote_end", "release_at"))
            if not start <= last or not start < end <= release:
                _invalid("Vote cooking observation order is invalid")
            if entry["status"] == TRACKING:
                if entry["winner"] is not None or entry["winning_votes"] != 0 or entry["reason"] is not None:
                    _invalid("Tracking vote cooking cannot already select a winner")
            else:
                if (last < release or (entry["winner"], entry["winning_votes"]) != _winner(entry)
                        or (entry["winner"] is None) != (entry["status"] == INELIGIBLE)):
                    _invalid("Vote cooking winner differs from its frozen eligible votes")
            if entry["status"] == PENDING:
                if type(entry["reason"]) is not str or not _REASON.fullmatch(entry["reason"]):
                    _invalid("Pending vote cooking requires a diagnostic")
            elif entry["reason"] is not None:
                _invalid("Completed vote cooking cannot have a pending reason")
            if entry["status"] != DELIVERED:
                if entry["mail_ids"] or entry["delivered_at"] is not None:
                    _invalid("Undelivered cooking cannot have mail receipts")
                continue
            if (_saved_time(entry["delivered_at"]) != last or len(entry["mail_ids"]) != entry["copies"]
                    or any(not _integer(identifier, 1) or identifier > state.get("last_present_id", 0)
                           for identifier in entry["mail_ids"])):
                _invalid("Vote cooking mail proof is invalid")
            for identifier in entry["mail_ids"]:
                expected = {"PresentID": identifier, "Type": 21, "Content": entry["winner"],
                            "Description": entry["description"]}
                if identifier in mail_ids or identifier in mail and mail[identifier] != expected:
                    _invalid("Vote cooking mail differs from its permanent proof")
                mail_ids.add(identifier)
        return journal["groups"]

    def initialize(self, state):
        self.ledger(state)
        return deepcopy(state)

    def record_vote(self, before, after, group, now):
        records = self.ledger(before)
        if (self.catalog.groups_by_id.get(group.group_id) != group
                or before.get("vote_cooking") != after.get("vote_cooking")):
            _invalid("Vote cooking transition changed its source identity")
        previous, current = _counts(before, group.item_id, group.group_id), _counts(after, group.item_id, group.group_id)
        changed = [card for card, count in current.items() if count != previous.get(card, 0)]
        if (previous.keys() - current.keys() or len(changed) != 1
                or current[changed[0]] <= previous.get(changed[0], 0)):
            _invalid("Vote cooking must follow one increased candidate total")
        updated = deepcopy(after)
        existing = records.get(group.item_id)
        if not self.rules.enabled and existing is None:
            return updated
        stamp = _time(now)
        if existing is None:
            if len(records) >= MAX_GROUPS:
                raise ProtocolError("vote_cooking_capacity", "Vote cooking identity history is full", 409)
            existing = {**self._parameters(group), "definition": self._definition(group),
                "baseline": previous, "counts": previous, "started_at": stamp, "last_time": stamp,
                "vote_end": _time(group.vote_end), "release_at": _time(group.result_start), "status": TRACKING,
                "winner": None, "winning_votes": 0, "reason": None, "mail_ids": [], "delivered_at": None}
        if (existing["definition"] != self._definition(group) or existing["status"] != TRACKING
                or not existing["last_time"] <= stamp < existing["vote_end"]):
            raise ProtocolError("vote_cooking_progress_closed", "Vote cooking progress requires its original open rules", 409)
        entry = deepcopy(existing)
        if not self.rules.enabled:
            card = changed[0]
            entry["baseline"][card] = entry["baseline"].get(card, 0) + current[card] - previous.get(card, 0)
        entry.update(counts=current, last_time=stamp)
        updated.setdefault("vote_cooking", {"schema_version": 1, "groups": {}})["groups"][group.item_id] = entry
        self.ledger(updated)
        return updated

    def validate_vote_receipt(self, state, item, definition):
        record = self.ledger(state).get(item)
        # Votes committed before an adoption keep the definition they were cast under.
        if record is None or definition not in self._definitions(item, record):
            _invalid("Vote receipt lost its cooking eligibility proof")

    @staticmethod
    def _grant(item, entry):
        return {"item_id": item, "group_id": entry["group_id"], "definition": entry["definition"],
            "card_id": entry["winner"], "count": entry["copies"], "receive_text_id": entry["receive_text_id"],
            "mail_ids": list(entry["mail_ids"])}

    def settle(self, state, now):
        records = self.ledger(state)
        stamp = _time(now)
        if any(stamp < record["last_time"] for record in records.values()):
            raise ProtocolError("vote_cooking_clock_reversed", "Vote cooking clock precedes saved progress", 409)
        updated, grants = deepcopy(state), []
        if not records:
            return updated, grants
        entries = updated["vote_cooking"]["groups"]
        for item in sorted(entries, key=lambda key: (entries[key]["release_at"], entries[key]["group_id"], key)):
            entry = entries[item]
            if entry["status"] == TRACKING and stamp >= entry["release_at"]:
                winner, count = _winner(entry)
                entry.update(status=PENDING if winner is not None else INELIGIBLE, winner=winner,
                    winning_votes=count, last_time=stamp, reason="awaiting_delivery" if winner is not None else None)
            if entry["status"] != PENDING or grants:
                continue
            if entry["source"] != self.recipes.content_version:
                entry["reason"] = "vote_cooking_recipe_changed"
                continue
            updated, grant = self._deliver(updated, item, stamp)
            entries = updated["vote_cooking"]["groups"]
            if grant is not None:
                grants.append(grant)
        self.ledger(updated)
        return updated, grants

    def _mail(self, state, entry):
        rewards = [{"kind": "vote_cooking", "card_id": entry["winner"], "description": entry["description"]}] * entry["copies"]
        candidate, additions = self.presents.enqueue(deepcopy(state), rewards)
        first = state.get("last_present_id", 0) + 1
        expected_rows = [{"PresentID": first + index, "Type": 21, "Content": entry["winner"],
                          "Description": entry["description"]} for index in range(entry["copies"])]
        expected = deepcopy(state)
        expected.update(presents_schema=1, last_present_id=first + entry["copies"] - 1)
        expected["tables"]["presents"] = self.presents.rows(state) + expected_rows
        if additions != expected_rows or candidate != expected:
            raise ProtocolError("vote_cooking_delivery_invalid", "Vote cooking mail changed unrelated state", 503)
        return candidate, [row["PresentID"] for row in additions]

    def _deliver(self, state, item, stamp):
        entry = state["vote_cooking"]["groups"][item]
        try:
            candidate, ids = self._mail(state, entry)
        except ProtocolError as error:
            if error.code not in CAPACITY_ERRORS:
                raise
            # A full mailbox keeps the whole group pending under its diagnostic; copies are never mailed apart.
            entry["reason"] = error.code
            return state, None
        delivered = candidate["vote_cooking"]["groups"][item]
        delivered.update(status=DELIVERED, reason=None, last_time=stamp, delivered_at=stamp, mail_ids=ids)
        return candidate, self._grant(item, delivered)

    def adopt_current(self, state, now):
        records = self.ledger(state)
        stamp = _time(now)
        if any(stamp < record["last_time"] for record in records.values()):
            raise ProtocolError("vote_cooking_clock_reversed", "Vote cooking clock precedes saved progress", 409)
        updated, report = deepcopy(state), []
        if not records:
            return updated, report
        groups = {group.item_id: group for group in self.catalog.groups}
        entries = updated["vote_cooking"]["groups"]
        for item in sorted(entries, key=lambda key: (entries[key]["release_at"], entries[key]["group_id"], key)):
            entry, group = entries[item], groups.get(item)
            frozen = {name: entry[name] for name in DEFINITION_FIELDS}
            if entry["status"] == TRACKING and group is not None and group.group_id == entry["group_id"]:
                adopted = self._parameters(group)
            elif entry["status"] == PENDING:
                # The winner and its copies were frozen with the eligibility; only the recipe source moves on.
                adopted = {**frozen, **self._recipe()}
            else:
                continue
            definition = self._saved_definition(item, adopted)
            if definition == entry["definition"]:
                continue
            status = entry["status"]
            # A definition adopted again is current once more, and each definition is kept only once.
            superseded = [old for old in entry.get(SUPERSEDED_KEY, ()) if self._saved_definition(item, old) != definition]
            entry.update(adopted, definition=definition)
            entry[SUPERSEDED_KEY] = [*superseded, frozen]
            if status == PENDING:
                updated, _ = self._deliver(updated, item, stamp)
                entries = updated["vote_cooking"]["groups"]
            result = entries[item]
            report.append({"item_id": item, "group_id": result["group_id"],
                           "status": {"before": status, "after": result["status"]}, "reason": result["reason"],
                           "mail_ids": list(result["mail_ids"])})
        self.ledger(updated)
        return updated, report

    def validate_receipt(self, state, grants):
        validate_vote_cooking_grants(grants)
        records = self.ledger(state)
        for grant in grants:
            saved = records.get(grant["item_id"])
            if saved is None or saved["status"] != DELIVERED or grant != self._grant(grant["item_id"], saved):
                _invalid("Cooking notification differs from its permanent delivery")

    def response(self, grants):
        self._ready()
        return vote_cooking_response(grants, receive_text_id=self.recipes.receive_text_id)

    def status(self):
        self._ready()
        return {"ready": True, "content_version": self.content_version, "rules": self.rules.to_dict(),
            "source": "Only votes tracked after the first new 57 request; old history is a baseline",
            "selection": "Each group: most new votes, ties use lowest CardID",
            "delivery": "First 115 after the captured result start; at most one group per request",
            "pending": "Frozen eligibility survives disabled rules or moved windows", "present_type": 21,
            "local_rule": True, "native_tested": False}
