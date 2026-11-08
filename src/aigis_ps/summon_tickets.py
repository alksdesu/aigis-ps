from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import logging
import re
import secrets

from .alxml import Column, Container, dumps
from .errors import ProtocolError, caught
from .native_numbers import int32
from .player_state import UNIT_BOOLEANS, UNIT_FIELDS
from .presents import CLAIM_LIMITS, MAX_PRESENTS, PRESENT_LIMITS
from .protocol import ProtocolRegistry, encode_response
from .request_fields import fields, invalid, registered, values
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .summon_pool_catalog import MAX_POOLS, SummonPool
from .summon_grants import SummonGrants


RESPONSE_LIMIT = 64 * 1024
HISTORY_NAMESPACE = "summon_ticket_history"
HISTORY_INVALID = ("summon_ticket_history_invalid", "Stored summon-ticket receipts are invalid")
RECEIPT_FIELDS = ("request", "profile", "content", "state", "revision", "response", "mail_id", "pool_id",
                  "candidate_index", "unit")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_UNIT_NAMES = frozenset(name for name, _ in UNIT_FIELDS)
log = logging.getLogger("aigis_ps")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _state_digest(state):
    return _digest({key: value for key, value in state.items() if key != HISTORY_NAMESPACE})


def _bad_history():
    raise ProtocolError(*HISTORY_INVALID, 503)


def _receipt_valid(entry):
    unit = entry["unit"]
    return (type(entry["profile"]) is str and _HASH.fullmatch(entry["profile"]) is not None
            and all(int32(entry[name]) and entry[name] > 0 for name in ("mail_id", "pool_id"))
            and int32(entry["candidate_index"]) and entry["candidate_index"] >= 0
            and isinstance(unit, dict) and unit.keys() == _UNIT_NAMES
            and all(type(value) is bool if name in UNIT_BOOLEANS else int32(value) for name, value in unit.items()))


@dataclass(frozen=True)
class SummonTicketRules:
    enabled_pools: tuple[int, ...] = (350,)

    def __post_init__(self):
        entries = self.enabled_pools
        if (not isinstance(entries, (tuple, list)) or len(entries) > MAX_POOLS
                or any(not int32(value) or value <= 0 for value in entries)
                or len(set(entries)) != len(entries)):
            raise ValueError("Enabled ticket pools must be bounded distinct positive integers")
        object.__setattr__(self, "enabled_pools", tuple(sorted(entries)))


class SummonTicketService:
    def __init__(self, bootstrap, players, presents, pools, *, rules=None, randbelow=None):
        self.bootstrap, self.players, self.presents, self.pools = bootstrap, players, presents, pools
        self.rules = SummonTicketRules() if rules is None else rules
        self.randbelow = secrets.randbelow if randbelow is None else randbelow
        if not isinstance(self.rules, SummonTicketRules) or not callable(self.randbelow):
            raise ValueError("Ticket service requires verified rules and a callable random source")
        self._enabled_pools = self.rules.enabled_pools
        self.grants = SummonGrants(players, pools, self._enabled_pools)
        self._validated_pools = self.grants.validated_pools
        self._initial_values = self.grants.initial_values
        self._pool_version = self.grants.pool_version
        self._initialization_version = self.grants.initialization_version
        self.content_version = _digest({"policy": 1, "pools": self._pool_version,
            "initialization": self._initialization_version, "rules": asdict(self.rules),
            "grant": "one_card_per_mail", "rainbow_compensation": 0,
            "response": "SYSTEM_PBP_PPU", "retry": "same_revision_state_content"})
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (1, 41, 42)}
        self._ledger = RequestLedger(HISTORY_NAMESPACE, *HISTORY_INVALID, fields=RECEIPT_FIELDS,
                                     response_limit=RESPONSE_LIMIT, check=_receipt_valid)
        self._ready()
        for pool_id in self.rules.enabled_pools:
            self.validate_pool(pool_id)

    def _ready(self):
        self.grants.ready()
        if (self.grants.players is not self.players or self.grants.pools is not self.pools
                or self.grants.enabled_pools != self._enabled_pools
                or not isinstance(self.rules, SummonTicketRules) or self.rules.enabled_pools != self._enabled_pools
                or not self.pools.is_ready() or self.pools.content_version != self._pool_version
                or self.players.initialization_version != self._initialization_version):
            raise ProtocolError("summon_ticket_dependencies_changed", "Ticket resources changed after setup", 503)

    def validate_pool(self, pool_id: int) -> SummonPool:
        self._ready()
        return self.grants.validate_pool(pool_id)

    @staticmethod
    def _key(envelope):
        identity = _digest(asdict(envelope.profile))
        return identity, hashlib.sha256(identity.encode() + b":" + envelope.xml).hexdigest()

    def matches_request(self, profile, envelope, selected) -> bool:
        _, key = self._key(envelope)
        if self._ledger.processed(self._ledger.read(profile.state, profile.revision), key):
            return True
        return any(row["Type"] == 16 and row["PresentID"] in selected for row in self.presents.rows(profile.state))

    @staticmethod
    def _request(envelope, selected):
        if envelope.profile.registration_keys != (1, 2):
            raise ProtocolError("summon_ticket_batch_unsupported", "Summon tickets require individual claims", 501)
        root = registered(envelope, {1, 2}, {"RT"}, {"RT"})
        rt = fields(root["RT"], {"PT2"}, {"PT2"})
        pt = fields(rt["PT2"], {"PresentID"}, {"PresentID"})
        requested = values(pt["PresentID"], "I", minimum=1, maximum=MAX_PRESENTS)
        if (not isinstance(selected, (tuple, list)) or tuple(selected) != requested
                or any(value <= 0 for value in requested) or len(set(requested)) != len(requested)):
            invalid("Summon ticket selection does not match the request")
        if len(requested) != 1:
            raise ProtocolError("summon_ticket_single_required", "Claim exactly one summon ticket at a time", 501)
        return requested[0]

    def _preflight(self, profile, state):
        candidate = replace(profile, state=state)
        player = self.players.validate(state)
        encode_response(self._profiles[41], self.players.unit_response(player["units"]))
        listing = dumps(Container(children=(self.bootstrap._system(candidate), self.presents.response(state))), limits=PRESENT_LIMITS)
        encode_response(self._profiles[42], listing)

    def _response(self, profile, state, unit):
        payload = dumps(Container(children=(self.bootstrap._system(profile), Container("PBP", (
            Column("A1", "S", ("UX",)), Column("A2", "I", (state["player_state"]["battle"]["UX"],)))),
            Container("PPU", tuple(Column(alias, "B" if name in UNIT_BOOLEANS else "I", (unit[name],))
                                   for name, alias in UNIT_FIELDS)))), limits=CLAIM_LIMITS)
        if len(payload) > RESPONSE_LIMIT:
            raise ProtocolError("summon_ticket_response_capacity", "Ticket response exceeds its receipt limit", 503)
        encode_response(self._profiles[1], payload)
        return payload

    def _expected_unit(self, pool_id, index, get_index, unique_id):
        return self.grants.expected_unit(pool_id, index, get_index, unique_id)

    def _replay(self, entry, profile, envelope, mail_id, profile_key):
        if entry["revision"] != profile.revision or entry["content"] != self.content_version:
            raise ProtocolError("summon_ticket_retry_stale", "Ticket state or resources changed after this claim", 409)
        if (entry["state"] != _state_digest(profile.state) or entry["profile"] != profile_key
                or entry["mail_id"] != mail_id
                or any(row["PresentID"] == mail_id for row in self.presents.rows(profile.state))):
            _bad_history()
        pool = self.validate_pool(entry["pool_id"])
        index, unit = entry["candidate_index"], entry["unit"]
        if index >= len(pool.candidates) or pool.candidates[index].weight <= 0:
            _bad_history()
        player = profile.state["player_state"]
        if (unit not in player["units"] or unit["GetIndex"] != player["battle"]["UX"]
                or unit["UniqueID"] != player.get("last_unique_id")
                or unit["CardID"] != pool.candidates[index].card_id):
            _bad_history()
        if unit != self._expected_unit(entry["pool_id"], index, unit["GetIndex"], unit["UniqueID"]):
            _bad_history()
        self._preflight(profile, profile.state)
        payload = self._response(profile, profile.state, unit)
        if payload != entry["response"].encode("utf-8"):
            _bad_history()
        encode_response(envelope.profile, payload)
        return payload

    def receive(self, envelope, store, selected) -> bytes:
        try:
            return self._receive(envelope, store, selected)
        except ProtocolError as error:
            log.warning("Summon ticket claim %d refused with %s; the native client shows a communication error and "
                        "resends the same request", envelope.profile.registration_keys[0], error.code)
            raise

    def _receive(self, envelope, store, selected) -> bytes:
        mail_id = self._request(envelope, selected)
        profile_key, key = self._key(envelope)
        frozen = None
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            self.players.validate(profile.state)
            rows = self.presents.rows(profile.state)
            history = self._ledger.read(profile.state, profile.revision)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                return self._replay(cached, profile, envelope, mail_id, profile_key)
            if self._ledger.processed(history, key):
                log.warning("Refused request 1/2: it repeats a processed summon claim whose receipt was evicted")
                raise ProtocolError("summon_ticket_request_stale", "Ticket claim was already processed", 409)
            mail = next((row for row in rows if row["PresentID"] == mail_id), None)
            if mail is None:
                raise ProtocolError("present_not_found", "Summon ticket is unavailable or already claimed", 409)
            if mail["Type"] != 16 or mail["Content"] != 1:
                raise ProtocolError("summon_ticket_invalid", "This mail is not a single-card summon ticket", 409)
            pool = self.validate_pool(mail["SummonID"])
            self.players.require_unit_space(profile.state, 1)
            if frozen is None:
                chosen = self.pools.draw(pool.pool_id, self.randbelow)
                if chosen not in pool.candidates or chosen.weight <= 0:
                    raise ProtocolError("summon_ticket_result_invalid", "Draw result is not a positive-weight candidate", 503)
                frozen = deepcopy(mail), pool.candidates.index(chosen)
            elif frozen[0] != mail:
                raise ProtocolError("summon_ticket_changed", "Ticket changed during the claim", 409)
            index = frozen[1]
            updated, added = self.grants.grant(profile.state, pool.pool_id, (index,))
            updated["tables"]["presents"] = [row for row in rows if row["PresentID"] != mail_id]
            self._preflight(profile, updated)
            payload = self._response(profile, updated, added[0])
            encode_response(envelope.profile, payload)
            self._ledger.record(updated, history, {
                "request": key, "profile": profile_key, "content": self.content_version,
                "state": _state_digest(updated), "revision": profile.revision + 1, "response": payload.decode("utf-8"),
                "mail_id": mail_id, "pool_id": pool.pool_id, "candidate_index": index, "unit": deepcopy(added[0])})
            try:
                store.update_state(updated, profile.revision)
                return payload
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while claiming a summon ticket", 409)

    def is_ready(self) -> bool:
        return caught(self._ready) is None

    def status(self):
        try:
            self._ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        return {"ready": ready, "error": error, "enabled_pools": list(self.rules.enabled_pools),
            "content_version": self.content_version, "initialization_version": self._initialization_version,
            "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT, "request_order": REQUEST_ORDER,
            "duplicate_card_policy": "new_instance_without_rainbow_compensation",
            "claim_policy": "one_ticket_per_request_no_batch", "native_tested": False}
