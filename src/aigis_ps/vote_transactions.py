from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from functools import partial
import hashlib
import json
import logging
import re
import sys

from .alxml import Container, dumps, loads
from .bootstrap import STARTUP_COLLECTION_LIMITS
from .energy_clock import instant
from .errors import ProtocolError
from .native_numbers import int32
from .possessions import STORED_ITEM_NAME_BYTES
from .presents import MAX_PRESENTS, PRESENT_LIMITS
from .protocol import ProtocolRegistry, encode_response
from .request_fields import registered, scalar
from .request_ledger import RequestLedger
from .vote_expiry import VoteAssaultExpiryService
from .vote_state import read_vote_history, record_vote, vote_history_response
from .vote_tickets import DAILY_ITEM


RESPONSE_LIMIT = 2 * 1024 * 1024
HISTORY_NAMESPACE = "vote_transactions"
HISTORY_INVALID = ("vote_receipts_invalid", "Stored vote receipts are invalid")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
ACTION_FIELDS = frozenset(("request", "definition", "group_id", "item_id", "card_id", "count", "daily_used",
    "ordinary_used", "previous_count", "result_count", "award_rows", "mail_ids", "cooking_definition"))
# Votes cast without a cooking service stored no cooking definition at all.
LEGACY_ACTION_DEFAULTS = {"cooking_definition": None}
RECEIPT_FIELDS = ACTION_FIELDS | {"content", "state", "revision", "response"}
# State initialization has no store revision to bound the receipts it validates.
UNBOUNDED_REVISION = sys.maxsize
log = logging.getLogger("aigis_ps")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _state_digest(state):
    return _digest({key: value for key, value in state.items() if key != HISTORY_NAMESPACE})


def _invalid():
    raise ProtocolError(*HISTORY_INVALID, 503)


def _text(value, maximum):
    try:
        return type(value) is str and 0 < len(value.encode("utf-8")) <= maximum
    except UnicodeError:
        return False


def _hash_value(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _action_shape_valid(action):
    return (isinstance(action, dict) and set(action) == ACTION_FIELDS
            and all(_hash_value(action[key]) for key in ("request", "definition"))
            and all(int32(action[key]) and action[key] > 0 for key in ("group_id", "card_id", "count", "result_count"))
            and int32(action["previous_count"]) and action["previous_count"] >= 0
            and type(action["daily_used"]) is int and action["daily_used"] in (0, 1)
            and int32(action["ordinary_used"]) and action["ordinary_used"] >= 0
            and action["count"] == action["daily_used"] + action["ordinary_used"]
            and action["result_count"] == action["previous_count"] + action["count"]
            and _text(action["item_id"], STORED_ITEM_NAME_BYTES)
            and isinstance(action["award_rows"], list) and len(action["award_rows"]) <= MAX_PRESENTS
            and isinstance(action["mail_ids"], list) and len(action["mail_ids"]) <= MAX_PRESENTS
            and all(int32(key) and key > 0 for key in action["mail_ids"])
            and len(set(action["mail_ids"])) == len(action["mail_ids"])
            and (action["cooking_definition"] is None or _hash_value(action["cooking_definition"])))


def _receipt_valid(entry):
    return _action_shape_valid({key: entry[key] for key in ACTION_FIELDS})


class VoteTransactions:
    def __init__(self, owner, possessions, rewards, tickets, finalists, expiry, *, cooking=None, assault_expiry=None):
        self.owner, self.possessions, self.rewards, self.tickets = owner, possessions, rewards, tickets
        self.finalists, self.expiry = finalists, expiry
        self.cooking = cooking
        self.assault_expiry = VoteAssaultExpiryService(rewards) if assault_expiry is None else assault_expiry
        self.bootstrap, self.players, self.catalog = owner.bootstrap, owner.players, owner.catalog
        registry = ProtocolRegistry.load()
        self._profiles = {key: registry.by_registration[key] for key in (42, 57, 129, 137)}
        self._ledger = RequestLedger(HISTORY_NAMESPACE, *HISTORY_INVALID, fields=RECEIPT_FIELDS,
                                     response_limit=RESPONSE_LIMIT, check=_receipt_valid,
                                     legacy_defaults=LEGACY_ACTION_DEFAULTS)
        # VoteService installs this as the 137 handler, so it records its own startup load.
        self.startup = self.bootstrap.startup_diagnostics.wrap(137, self._startup)
        self.content_version = self._content()
        self._ready()

    def _content(self):
        return self._assault_content(self._cooking_content())

    def _replayable(self, content):
        if content == self.content_version:
            return True
        return any(content in self._replayable_contents(version) for version in self.rewards.replayable_content_versions())

    def _replayable_contents(self, rewards_version=None):
        # Receipts keep their committed digest; schema 3 and 4 receipts may have skipped the cooking step.
        before_assault = {self._closing_content(rewards_version), self._cooking_content(rewards_version)}
        return frozenset({self._legacy_content(rewards_version), *before_assault,
                          *(self._assault_content(previous, rewards_version) for previous in before_assault)})

    def _assault_content(self, previous, rewards_version=None):
        assault = (self.assault_expiry.content_version if rewards_version is None
                   else self.assault_expiry.content_version_for(rewards_version))
        return _digest({"schema": 4, "previous": previous,
            "assault_expiry": assault, "assault_observation": [57, 137]})

    def _cooking_content(self, rewards_version=None):
        previous = self._closing_content(rewards_version)
        return previous if self.cooking is None else _digest({"schema": 3, "previous": previous,
            "cooking": self.cooking.content_version, "cooking_source": "new_committed_vote_deltas"})

    def _closing_content(self, rewards_version=None):
        return _digest({"schema": 2, "transactions": self._legacy_content(rewards_version),
            "finalists": self.finalists.content_version, "expiry": self.expiry.content_version,
            "observation": [55, 57, 137]})

    def _legacy_content(self, rewards_version=None):
        return _digest({"schema": 1, "catalog": self.catalog.content_version,
            "possessions": self.possessions.asset_sha256,
            "rewards": self.rewards.content_version if rewards_version is None else rewards_version,
            "tickets": self.tickets.content_version, "prerequisite": 1004,
            "delivery": "atomic_mail", "retry": "exact_committed_revision",
            "daily_ticket_observation": [137, 57]})

    def _ready(self):
        self.owner._ready()
        self.rewards._ready()
        self.tickets._ready()
        self.finalists._ready()
        self.expiry._ready()
        self.assault_expiry._ready()
        if self.cooking is not None:
            self.cooking._ready()
            if self.cooking.catalog is not self.catalog or self.cooking.presents is not self.rewards.presents:
                raise ProtocolError("vote_transaction_dependencies_changed", "Vote cooking must share verified dependencies", 503)
        if (self.rewards.catalog is not self.catalog or self.tickets.catalog is not self.catalog
                or self.tickets.possessions is not self.possessions
                or self.finalists.catalog is not self.catalog or self.expiry.catalog is not self.catalog
                or self.rewards.finalists is not self.finalists or self.expiry.possessions is not self.possessions
                or self.expiry.presents is not self.rewards.presents or self.assault_expiry.rewards is not self.rewards
                or self._content() != self.content_version):
            raise ProtocolError("vote_transaction_dependencies_changed", "Vote transaction dependencies changed", 503)

    def initialize(self, state):
        self._ready()
        if (any(key in state for key in (HISTORY_NAMESPACE, "vote_last_action"))
                and not {"vote_rewards", "vote_tickets", "vote_state"} <= state.keys()):
            _invalid()
        updated = self.rewards.initialize(self.tickets.initialize(state))
        updated = self.assault_expiry.initialize(self.expiry.initialize(self.finalists.initialize(updated)))
        if self.cooking is not None:
            updated = self.cooking.initialize(updated)
        elif "vote_cooking" in updated:
            raise ProtocolError("vote_cooking_service_unavailable", "Existing cooking progress requires its service", 503)
        if HISTORY_NAMESPACE in updated or "vote_last_action" in updated:
            self.history(updated, UNBOUNDED_REVISION)
        return updated

    def observe_rankings(self, state, now):
        relevant = {group.item_id for group in self.catalog.groups}
        if (self.catalog.rules.revival_start is not None or "vote_finalists" in state
                or any(row["itemId"] in relevant for row in read_vote_history(state))):
            return self.finalists.settle(state, now)[0]
        return state

    def _observe(self, state, now):
        updated = self.observe_rankings(state, now)
        updated, _ = self.expiry.settle(updated, now)
        updated, _ = self.assault_expiry.settle(updated, now)
        return self.tickets.settle(updated, now)[0]

    def _validate_action(self, state, action):
        if not _action_shape_valid(action):
            _invalid()
        if action["group_id"] == 4:
            self.finalists.validate_qualification(state, action["card_id"])
        if action["cooking_definition"] is not None:
            if self.cooking is None:
                raise ProtocolError("vote_cooking_service_unavailable", "Vote receipt requires its cooking eligibility", 503)
            self.cooking.validate_vote_receipt(state, action["item_id"], action["cooking_definition"])
        self.rewards.validate_delivery(state, action["item_id"], action["award_rows"], action["mail_ids"], verify_current=False)

    def history(self, state, revision):
        if HISTORY_NAMESPACE not in state:
            if "vote_last_action" in state:
                _invalid()
            return self._ledger.read(state, revision)
        if not {"vote_rewards", "vote_tickets", "vote_state"} <= state.keys():
            _invalid()
        history = self._ledger.read(state, revision)
        if not history["receipts"]:
            _invalid()
        for saved in history["receipts"]:
            self._validate_action(state, {key: saved[key] for key in ACTION_FIELDS})
        latest = {key: history["receipts"][-1][key] for key in ACTION_FIELDS}
        last_action = state.get("vote_last_action")
        if not isinstance(last_action, dict) or {**LEGACY_ACTION_DEFAULTS, **last_action} != latest:
            _invalid()
        rows = read_vote_history(state)
        matching = [row for row in rows if row["itemId"] == latest["item_id"] and row["cardId"] == latest["card_id"]]
        if (len(matching) != 1 or matching[0]["groupId"] != latest["group_id"]
                or matching[0]["count"] != latest["result_count"]):
            _invalid()
        return history

    def _preflight(self, profile, state):
        candidate = replace(profile, state=state)
        startup = self.bootstrap._configuration_response(candidate)
        tables = {child.name: child for child in loads(startup, limits=STARTUP_COLLECTION_LIMITS).children}
        if (tables.get("PVH") != vote_history_response(state)
                or tables.get("POSSESSION") != self.possessions.startup_response(state)):
            raise ProtocolError("vote_provider_inconsistent", "Startup vote data differs from the saved state", 503)
        encode_response(self._profiles[137], startup)
        encode_response(self._profiles[129], dumps(Container(children=(self.bootstrap._system(candidate), self.possessions.startup_response(state)))))
        encode_response(self._profiles[42], dumps(Container(children=(self.bootstrap._system(candidate),
            self.rewards.presents.response(state))), limits=PRESENT_LIMITS))
        return startup

    def _response(self, profile, state, action, *, legacy_possessions=False):
        from .vote_service import REPLY_LIMITS, VRC_FIELDS, _table
        group = self.catalog.groups_by_id[action["group_id"]]
        expired = "vote_expiry" in state
        children = (self.bootstrap._system(profile), vote_history_response(state),
            self.possessions.response(state, None if expired else (group.item_id, DAILY_ITEM), legacy=legacy_possessions),
            _table("VRC", VRC_FIELDS, action["award_rows"]),
            self.rewards.presents.response(state) if expired else self.rewards.presents.response(state, action["mail_ids"]),
            self.rewards.assault_response(state, group))
        response = dumps(Container(children=children), limits=REPLY_LIMITS)
        if len(response) > RESPONSE_LIMIT:
            raise ProtocolError("vote_response_capacity", "Vote response exceeds receipt capacity", 503)
        encode_response(self._profiles[57], response)
        return response

    def _startup(self, envelope, store):
        registered(envelope, {137}, ())
        now = None
        for _ in range(3):
            profile = self.bootstrap._collection_profile(envelope, store)
            # 137 returns no units, so card references must not stall the native startup loop.
            self.players.validate_player(profile.state.get("player_state"), references=False)
            try:
                self._ready()
            except ProtocolError as error:
                # The saved state stays authoritative: serve it without the day's ticket refresh.
                self.bootstrap.startup_diagnostics.degrade(error)
                return self.bootstrap._configuration_response(profile)
            if now is None:
                now = instant(self.bootstrap.clock())
            updated = self._observe(profile.state, now)
            response = self._preflight(profile, updated)
            if updated == profile.state:
                return response
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while refreshing vote tickets", 409)

    def submit(self, envelope, store):
        try:
            return self._submit(envelope, store)
        except ProtocolError as error:
            log.warning("Vote 57 refused with %s; the native client shows a communication error and resends "
                        "the same request", error.code)
            raise

    def _submit(self, envelope, store):
        self._ready()
        request = registered(envelope, {57}, {"GID", "CID", "CNT"}, {"GID", "CID", "CNT"})
        group_id, card_id, count = (scalar(request[name], "I") for name in ("GID", "CID", "CNT"))
        if group_id <= 0 or card_id <= 0 or not 0 < count <= self.catalog.rules.vote_limit_count:
            raise ProtocolError("vote_request_invalid", "Vote requires a group, candidate and bounded positive count")
        key = hashlib.sha256(f"57:{envelope.profile.route}:{envelope.profile.st}:".encode() + envelope.xml).hexdigest()
        now = None
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            self.players.validate(profile.state)
            history = self.history(profile.state, profile.revision)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                if cached["revision"] != profile.revision or not self._replayable(cached["content"]):
                    raise ProtocolError("vote_retry_stale", "Vote state changed after this request", 409)
                if ((cached["group_id"], cached["card_id"], cached["count"]) != (group_id, card_id, count)
                        or cached["state"] != _state_digest(profile.state)):
                    _invalid()
                self._preflight(profile, profile.state)
                self.rewards.validate_delivery(profile.state, cached["item_id"], cached["award_rows"], cached["mail_ids"])
                saved = cached["response"].encode("utf-8")
                rebuild = partial(self._response, profile, profile.state, cached)
                # Receipts saved before every POSSESSION carried Enabled keep their two-column table.
                if rebuild() != saved and rebuild(legacy_possessions=True) != saved:
                    _invalid()
                return saved
            if self._ledger.processed(history, key):
                raise ProtocolError("vote_request_stale", "Vote request was already processed", 409)
            if now is None:
                now = instant(self.bootstrap.clock())
            if not self.catalog.active(now):
                raise ProtocolError("vote_inactive", "Vote event is outside its active window", 409)
            group = self.catalog.groups_by_id.get(group_id)
            if group is None:
                raise ProtocolError("vote_candidate_invalid", "Candidate does not belong to the selected group")
            if not group.vote_start <= now < group.vote_end:
                raise ProtocolError("vote_group_inactive", "Vote group is outside its voting window", 409)
            observed = self._observe(profile.state, now)
            self.finalists.require_open(observed, group)
            self.expiry.require_open(observed, group)
            self.assault_expiry.require_open(observed)
            if not self.finalists.is_candidate(observed, group, card_id):
                raise ProtocolError("vote_candidate_invalid", "Candidate does not belong to the selected group")
            records = self.bootstrap._rows(profile, "quest_records", single=False)
            if (self.bootstrap._player_row(profile)["Rank"] < self.catalog.rules.required_rank
                    or not any(row["QuestID"] == 1004 and row["MaxRank"] > 0 for row in records)):
                raise ProtocolError("vote_not_unlocked", "Vote requires the configured rank and quest 1004", 409)
            previous = next((row["count"] for row in read_vote_history(profile.state)
                if row["itemId"] == group.item_id and row["cardId"] == card_id), 0)
            if previous + count > self.catalog.rules.vote_limit_count:
                raise ProtocolError("vote_candidate_limit", "Vote exceeds the candidate's per-group limit", 409)
            updated, daily_used, ordinary_used = self.tickets.consume(observed, group.item_id, count, now)
            definition = self.rewards.group_definition(group)
            updated = record_vote(updated, item_id=group.item_id, group_id=group_id, card_id=card_id,
                count=previous + count, definition=definition)
            updated, awards, mail_ids = self.rewards.apply_vote(observed, updated, group)
            if self.cooking is not None:
                updated = self.cooking.record_vote(observed, updated, group, now)
            self._preflight(profile, updated)
            cooking = (None if self.cooking is None else
                       updated.get("vote_cooking", {}).get("groups", {}).get(group.item_id, {}).get("definition"))
            action = {"request": key, "definition": definition, "group_id": group_id, "item_id": group.item_id,
                "card_id": card_id, "count": count, "daily_used": daily_used, "ordinary_used": ordinary_used,
                "previous_count": previous, "result_count": previous + count, "award_rows": awards, "mail_ids": mail_ids,
                "cooking_definition": cooking}
            response = self._response(profile, updated, action)
            updated["vote_last_action"] = deepcopy(action)
            self._ledger.record(updated, history, {**action, "content": self.content_version, "state": _state_digest(updated),
                                                   "revision": profile.revision + 1, "response": response.decode("utf-8")})
            self.history(updated, profile.revision + 1)
            self._ready()
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while voting", 409)
