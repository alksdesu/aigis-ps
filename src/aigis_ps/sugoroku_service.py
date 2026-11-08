from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import logging
import re
from types import MappingProxyType

from .alxml import Column, Container, dumps
from .business_calendar import CALENDAR_MONTH_TEXT
from .crystal_spending import POLICY_VERSION, _clock, read_spending, settle_spending
from .energy_clock import instant
from .errors import ProtocolError
from .monthly_capacity import grant_next_capacity, initialize_capacity, read_capacity
from .native_numbers import INT32_MAX, int32
from .presents import MAX_PRESENTS, PRESENT_LIMITS
from .protocol import ProtocolRegistry, encode_response
from .quest_economy import settle_energy
from .request_fields import fields, invalid, registered, values
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .sugoroku_state import (
    initialize_sugoroku, mark_sugoroku, read_sugoroku, snapshot_sugoroku, sync_sugoroku,
)


RESPONSE_LIMIT = 256 * 1024
MAX_REWARDS = 4096
DESCRIPTION = "王国警備報酬"
CLAIMED = 2
SEEN_STATUSES = frozenset((1, CLAIMED))
CAPACITY_REWARD = "next_month_capacity"
HISTORY_NAMESPACE = "sugoroku_history"
HISTORY_INVALID = ("sugoroku_history_invalid", "Stored reward board receipts are invalid")
RECEIPT_FIELDS = ("request", "content", "state", "revision", "response", "operation", "period", "ids", "mail_ids",
                  "granted")
LEGACY_RECEIPT_DEFAULTS = {"granted": None}
_HASH = re.compile(r"[a-f0-9]{64}\Z")
log = logging.getLogger("aigis_ps")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _state_digest(state):
    return _digest({key: value for key, value in state.items() if key != HISTORY_NAMESPACE})


def _history_invalid():
    raise ProtocolError(*HISTORY_INVALID, 503)


def _identifiers(values, limit):
    return (isinstance(values, list) and len(values) <= limit
            and all(int32(value) and value > 0 for value in values) and len(set(values)) == len(values))


def _receipt_valid(entry):
    operation, shown, granted = entry["operation"], entry["ids"], entry["granted"]
    if (type(operation) is not int or operation not in (63, 64)
            or not isinstance(entry["period"], str) or not CALENDAR_MONTH_TEXT.fullmatch(entry["period"])
            or not _identifiers(shown, MAX_REWARDS) or not _identifiers(entry["mail_ids"], MAX_PRESENTS)):
        return False
    if granted is not None and (not _identifiers(granted, MAX_REWARDS) or not set(granted) <= set(shown)
                                or bool(granted) != (operation == 63)):
        return False
    return not entry["mail_ids"] or (operation == 63 and bool(shown))


@dataclass(frozen=True)
class BoardSelection:
    state: dict
    requested: tuple[int, ...]
    shown: tuple[int, ...]
    candidates: tuple[int, ...]

    @property
    def skipped(self) -> tuple[int, ...]:
        chosen = set(self.candidates)
        return tuple(identifier for identifier in self.requested if identifier not in chosen)


@dataclass(frozen=True)
class BoardClaim:
    state: dict
    granted: tuple[int, ...]
    rewards: tuple[dict, ...]
    deliveries: Mapping[int, tuple[int, ...]]
    unsupported: tuple[int, ...]

    @property
    def mail_ids(self) -> list[int]:
        return [mail_id for identifier in self.granted for mail_id in self.deliveries[identifier]]


class SugorokuService:
    def __init__(self, bootstrap, players, presents, catalog, *, recovery_seconds=180):
        if not int32(recovery_seconds) or not 1 <= recovery_seconds <= 86400:
            raise ValueError("Recovery interval must be between one second and one day")
        self.bootstrap, self.players, self.presents, self.catalog = bootstrap, players, presents, catalog
        self.recovery_seconds = recovery_seconds
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (33, 34, 35, 42, 62, 63, 64)}
        self._ledger = RequestLedger(HISTORY_NAMESPACE, *HISTORY_INVALID, fields=RECEIPT_FIELDS,
                                     response_limit=RESPONSE_LIMIT, check=_receipt_valid,
                                     legacy_defaults=LEGACY_RECEIPT_DEFAULTS)
        self.content_version = self._content()
        self._ready()

    def _content(self, items=None):
        try:
            versions = {"catalog": self.catalog.content_version, "presents": self.presents.asset_sha256,
                        "items": self.presents.item_catalog.content_version if items is None else items}
            if any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in versions.values()):
                raise ValueError("Board dependencies require stable fingerprints")
            return _digest({"policy": 2, "dependencies": versions, "templates": dict(self.presents.templates),
                "spending_policy": POLICY_VERSION, "month": "JST_04", "delivery": "mail_and_next_month_capacity",
                "recovery_seconds": self.recovery_seconds,
                "legacy": "no_unknown_backfill", "unit_growth": "standard_initial_template",
                "description": DESCRIPTION})
        except (AttributeError, TypeError, ValueError) as error:
            raise ProtocolError("sugoroku_dependencies_invalid", "Reward board dependencies are unavailable", 503) from error

    def _ready(self):
        self.presents._ready()
        if not self.catalog.is_ready() or self._content() != self.content_version:
            raise ProtocolError("sugoroku_dependencies_changed", "Reward board definitions changed after setup", 503)

    def _replayable(self, content):
        if content == self.content_version:
            return True
        # A receipt keeps the digest of its commit, which emblem mail attached since then cannot reach.
        return any(self._content(items) == content for items in self.presents.item_catalog.replayable_content_versions())

    def handlers(self):
        return {62: self.read, 63: self.claim, 64: self.mark_read}

    def initialize(self, state):
        self._ready()
        return initialize_capacity(initialize_sugoroku(state))

    def _synchronize(self, state, now):
        updated = sync_sugoroku(self.initialize(state), self.catalog, now)
        capacity = read_capacity(updated)
        period, timestamp = _clock(now)
        if capacity["period"] != period:
            raise ProtocolError("capacity_refresh_required", "Monthly capacity must be refreshed before board actions", 409)
        if timestamp < capacity["last_time"]:
            raise ProtocolError("capacity_clock_backwards", "Board time precedes the saved capacity state", 409)
        return updated

    def prepare_month(self, state, now):
        self._ready()
        updated = self.initialize(state)
        ledger = read_spending(updated)
        period, timestamp = _clock(now)
        if timestamp < ledger["last_time"]:
            raise ProtocolError("crystal_spending_clock_backwards", "Spending clock precedes the saved instant", 409)
        capacity = read_capacity(updated)
        if timestamp < capacity["last_time"]:
            raise ProtocolError("capacity_clock_backwards", "Capacity clock precedes the saved instant", 409)
        if period != capacity["period"]:
            updated = settle_energy(updated, self.bootstrap.rank_rows, now, recovery_seconds=self.recovery_seconds)
        return settle_spending(updated, now) if period != ledger["period"] else updated

    def status(self):
        try:
            self._ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        return {"ready": ready, "error": error, "implemented_operations": [62, 63, 64],
                "content_version": self.content_version, "catalog": self.catalog.status(),
                "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT,
                "request_order": REQUEST_ORDER,
                "claim_policy": "63 claims the reached unclaimed rewards it names, reports claimed ones and omits unavailable ones; "
                                "nothing claimable answers without a receipt and logs a warning",
                "delivery": "Mail, except type 5 directly records next-month capacity", "month_boundary": "JST 04:00",
                "capacity_policy": "Local rule: claim once, replace current capacity next month, expire the following month",
                "recovery_seconds": self.recovery_seconds,
                "legacy_policy": "Do not backfill rewards against unknown prior claim history",
                "native_tested": False}

    @staticmethod
    def _reward_rows(rows):
        return Container("REWARD", tuple(Column(key, "I", tuple(row[key] for row in rows))
                                         for key in ("ID", "Status")))

    def _response(self, operation, profile, state, now, identifiers=None, mail_ids=()):
        rows = snapshot_sugoroku(state, self.catalog, now)
        if identifiers is not None:
            indexed = {row["ID"]: row for row in rows}
            if set(identifiers) - indexed.keys():
                _history_invalid()
            rows = [indexed[identifier] for identifier in identifiers]
        children = [self.bootstrap._system(profile), self._reward_rows(rows)]
        if mail_ids:
            children.append(self.presents.response(state, mail_ids))
        payload = dumps(Container(children=tuple(children)), limits=PRESENT_LIMITS)
        if len(payload) > RESPONSE_LIMIT:
            raise ProtocolError("sugoroku_response_capacity", "Reward board response exceeds its receipt capacity", 503)
        encode_response(self._profiles[operation], payload)
        return payload

    def _preflight(self, profile, state, now):
        self._response(62, profile, state, now)
        candidate = replace(profile, state=state)
        encode_response(self._profiles[35], dumps(Container(children=self.bootstrap.columns(
            "game_param2", self.bootstrap._rows(candidate, "game_param2")))))
        encode_response(self._profiles[34], dumps(Container(children=self.bootstrap.columns(
            "game_param", [self.bootstrap._player_row(candidate)]))))
        encode_response(self._profiles[42], dumps(Container(children=(
            self.bootstrap._system(profile), self.presents.response(state))), limits=PRESENT_LIMITS))

    def read(self, envelope, store):
        registered(envelope, {62}, ())
        now = instant(self.bootstrap.clock())
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            self.players.validate(profile.state)
            updated = self._synchronize(profile.state, now)
            response = self._response(62, profile, updated, now)
            if updated == profile.state:
                return response
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while loading the reward board", 409)

    def _reward(self, definition):
        if definition.unsupported_reason is not None:
            raise ProtocolError("sugoroku_reward_unsupported", definition.unsupported_reason, 501)
        if definition.reward_type == 1:
            reward = {"kind": "item", "item_id": definition.item_id, "amount": definition.count}
        elif definition.reward_type == 2:
            reward = {"kind": "unit", "template_id": self.presents.initial_unit_template(definition.card_id),
                      "count": definition.count}
        elif definition.reward_type == 4:
            reward = {"kind": "gold", "amount": definition.count}
        elif definition.reward_type == 5:
            return {"kind": CAPACITY_REWARD, "amount": definition.count}
        else:
            raise ProtocolError("sugoroku_reward_unsupported", "This reward delivery has not been verified", 501)
        return {**reward, "description": DESCRIPTION}

    def _claimable(self, state, identifiers):
        # Native 7DC400 submits every reached reward, so a 501 reward stays reached instead of failing the batch.
        granted, rewards, refusal = [], [], None
        queued = len(self.presents.rows(state))
        for identifier in identifiers:
            try:
                reward = self._reward(self.catalog.by_id[identifier])
                if reward["kind"] != CAPACITY_REWARD:
                    queued += len(self.presents.preview([reward], queued))
            except ProtocolError as error:
                if error.status != 501:
                    raise
                refusal = refusal or error
                continue
            granted.append(identifier)
            rewards.append(reward)
        if not granted and refusal is not None:
            raise refusal
        return tuple(granted), rewards

    def select_rewards(self, state: dict, identifiers: Sequence[int], now: datetime) -> BoardSelection:
        requested = tuple(identifiers)
        if not _identifiers(list(requested), MAX_REWARDS):
            raise ProtocolError("sugoroku_selection_invalid",
                                "Reward-board selection must contain distinct native identities")
        updated = self._synchronize(state, now)
        rows = {row["ID"]: row["Status"] for row in snapshot_sugoroku(updated, self.catalog, now)}
        shown = tuple(identifier for identifier in requested if identifier in rows)
        candidates = tuple(identifier for identifier in shown if rows[identifier] != CLAIMED)
        return BoardSelection(updated, requested, shown, candidates)

    def grant_rewards(self, selection: BoardSelection, now: datetime) -> BoardClaim:
        updated = selection.state
        granted, rewards = self._claimable(updated, selection.candidates)
        deliveries = {}
        for identifier, reward in zip(granted, rewards):
            if reward["kind"] == CAPACITY_REWARD:
                updated = grant_next_capacity(updated, reward["amount"], now)
                row = updated["tables"]["game_param"][0]
                charisma = self.bootstrap.rank_rows[row["Rank"] - 1]["CharismaMax"]
                if charisma + row["ExtendedMaxCharismaNextMonth"] > INT32_MAX:
                    raise ProtocolError("capacity_overflow", "Next-month capacity exceeds the native int32 field", 409)
                deliveries[identifier] = []
            else:
                updated, added = self.presents.enqueue(updated, [reward])
                deliveries[identifier] = [row["PresentID"] for row in added]
        updated = mark_sugoroku(updated, granted, claimed=True, deliveries=deliveries)
        chosen = set(granted)
        return BoardClaim(updated, granted, tuple(rewards),
                          MappingProxyType({identifier: tuple(mails) for identifier, mails in deliveries.items()}),
                          tuple(identifier for identifier in selection.candidates if identifier not in chosen))

    def _write(self, operation, envelope, store):
        try:
            return self._commit(operation, envelope, store)
        except ProtocolError as error:
            log.warning("Reward board %d refused with %s; the native client shows a communication error and resends "
                        "the same request", operation, error.code)
            raise

    def _unchanged(self, profile, identifiers, shown, now):
        # Native 7DC400 sends stale or tier-wide selections and resends any refused request, so it gets the current rows.
        log.warning("Reward board 63 found nothing claimable among %s; replying with their current rows", list(identifiers))
        return self._response(63, profile, profile.state, now, shown)

    def _replay(self, cached, operation, profile, identifiers, period, now):
        if (cached["revision"] != profile.revision or not self._replayable(cached["content"])
                or cached["period"] != period):
            raise ProtocolError("sugoroku_retry_stale", "Reward board state, period or content changed", 409)
        shown, granted = tuple(cached["ids"]), cached["granted"]
        listed = set(shown)
        expected_shown = identifiers if granted is None else tuple(value for value in identifiers if value in listed)
        if cached["state"] != _state_digest(profile.state) or cached["operation"] != operation or shown != expected_shown:
            _history_invalid()
        saved_records = read_sugoroku(profile.state)["periods"].get(period, {}).get("records", {})
        expected_mail = []
        if operation == 63:
            if any(str(identifier) not in saved_records for identifier in shown):
                _history_invalid()
            if granted is None:
                # Schema 1 receipts list every requested reward, and each claimed one among them came from this request.
                granted = [identifier for identifier in shown if saved_records[str(identifier)]["status"] == CLAIMED]
                if shown and not granted:
                    _history_invalid()
            elif any(saved_records[str(identifier)]["status"] != CLAIMED for identifier in granted):
                _history_invalid()
            expected_mail = [mail_id for identifier in granted for mail_id in saved_records[str(identifier)]["mail_ids"]]
        if cached["mail_ids"] != expected_mail:
            _history_invalid()
        rows = {row["ID"]: row["Status"] for row in snapshot_sugoroku(profile.state, self.catalog, now)}
        if listed - rows.keys():
            raise ProtocolError("sugoroku_retry_stale", "A reward is no longer inside its active window", 409)
        if operation == 64 and any(rows[identifier] not in SEEN_STATUSES for identifier in shown):
            _history_invalid()
        self._preflight(profile, profile.state, now)
        response = self._response(operation, profile, profile.state, now, shown, cached["mail_ids"])
        if response != cached["response"].encode("utf-8"):
            _history_invalid()
        return response

    def _commit(self, operation, envelope, store):
        request = registered(envelope, {operation}, {"REWARD"}, {"REWARD"})
        group = fields(request["REWARD"], {"ID"})
        identifiers = values(group["ID"], "I", maximum=MAX_REWARDS) if "ID" in group else ()
        if any(identifier <= 0 for identifier in identifiers) or len(set(identifiers)) != len(identifiers):
            invalid("Reward identifiers must be distinct positive integers")
        key = hashlib.sha256(f"{operation}:{envelope.profile.route}:{envelope.profile.st}:".encode() + envelope.xml).hexdigest()
        now = instant(self.bootstrap.clock())
        period = _clock(now)[0]
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            self.players.validate(profile.state)
            history = self._ledger.read(profile.state, profile.revision)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                return self._replay(cached, operation, profile, identifiers, period, now)
            if self._ledger.processed(history, key):
                raise ProtocolError("sugoroku_request_stale", "Reward request was already processed", 409)
            selection = self.select_rewards(profile.state, identifiers, now)
            updated, shown, mail_ids, granted = selection.state, selection.shown, [], ()
            if operation == 64:
                if len(shown) != len(identifiers):
                    raise ProtocolError("sugoroku_reward_unavailable", "A reward is unknown, inactive or not yet reached", 409)
                updated = mark_sugoroku(updated, identifiers)
            else:
                if not selection.candidates:
                    return self._unchanged(profile, identifiers, shown, now)
                if selection.skipped:
                    log.warning("Reward board 63 skips %s as claimed or unavailable and claims the rest",
                                list(selection.skipped))
                claim = self.grant_rewards(selection, now)
                updated, granted, mail_ids = claim.state, claim.granted, claim.mail_ids
            self._preflight(profile, updated, now)
            response = self._response(operation, profile, updated, now, shown, mail_ids)
            self._ledger.record(updated, history, {
                "request": key, "content": self.content_version, "state": _state_digest(updated),
                "revision": profile.revision + 1, "response": response.decode("utf-8"), "operation": operation,
                "period": period, "ids": list(shown), "mail_ids": mail_ids, "granted": list(granted)})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while updating reward board progress", 409)

    def claim(self, envelope, store):
        return self._write(63, envelope, store)

    def mark_read(self, envelope, store):
        return self._write(64, envelope, store)
