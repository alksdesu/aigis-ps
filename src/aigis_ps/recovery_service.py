from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import logging

from .alxml import Container, dumps, loads
from .client_versions import CURRENT_CLIENT_SHA256, require_client_version
from .crystal_spending import POLICY_VERSION, PROJECTION_CAP, spend_crystals, spending_response
from .crystal_wallet import free_crystal_response, paid_crystal_response
from .errors import ProtocolError
from .monthly_capacity import POLICY_VERSION as CAPACITY_POLICY_VERSION
from .energy_clock import ENCODING, UTC_OFFSET_MINUTES
from .native_numbers import int32
from .protocol import ProtocolRegistry, encode_response
from .quest_economy import MAX_RANK_ROWS, _anchor, _now_seconds, _set_anchor, game_param_response, settle_energy
from .request_fields import registered
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger


CHARISMA_LIMIT = 2000
CRYSTAL_PRICE = 1
RESPONSE_LIMIT = 128 * 1024
HISTORY_NAMESPACE = "recovery_history"
log = logging.getLogger("aigis_ps")


@dataclass(frozen=True)
class RecoveryRules:
    charisma_limit: int = CHARISMA_LIMIT

    def __post_init__(self):
        if not int32(self.charisma_limit) or self.charisma_limit <= 0:
            raise ValueError("Recovery charisma limit must be a positive int32 value")


def _rank_rows(rows):
    if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= MAX_RANK_ROWS:
        raise ValueError("Recovery requires bounded rank data")
    result = []
    for row in rows:
        if not isinstance(row, dict) or any(not int32(row.get(key)) or row[key] <= 0 for key in ("NextExp", "CharismaMax")):
            raise ValueError("Recovery rank data requires positive experience thresholds and capacities")
        result.append({"NextExp": row["NextExp"], "CharismaMax": row["CharismaMax"]})
    return result


class RecoveryService:
    def __init__(self, bootstrap, *, recovery_seconds=180, clock=None, rules=None, client_sha256=CURRENT_CLIENT_SHA256):
        self.bootstrap = bootstrap
        try:
            self.client_sha256 = require_client_version(client_sha256)
            if not int32(recovery_seconds) or recovery_seconds <= 0:
                raise ValueError("Recovery interval must be a positive int32 number of seconds")
            self.rules = RecoveryRules() if rules is None else rules
            if not isinstance(self.rules, RecoveryRules):
                raise ValueError("Recovery rules require a verified limit configuration")
            self.clock = bootstrap.clock if clock is None else clock
            if not callable(self.clock):
                raise ValueError("Recovery requires a clock")
            self.recovery_seconds = recovery_seconds
            self._ranks = _rank_rows(bootstrap.rank_rows)
            content = {"policy": 1, "client": self.client_sha256, "ranks": self._ranks,
                       "charisma_limit": self.rules.charisma_limit, "crystal_price": CRYSTAL_PRICE,
                       "recovery_seconds": recovery_seconds, "free_first": True,
                       "date_order": "oldest_first", "clock_backwards": "reject",
                       "energy_clock": {"encoding": ENCODING, "utc_offset_minutes": UTC_OFFSET_MINUTES},
                       "monthly_capacity_policy": CAPACITY_POLICY_VERSION}
            self.content_version = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
            self.purchase_content_version = hashlib.sha256(json.dumps({"recovery": self.content_version,
                "crystal_spending": POLICY_VERSION, "projection_cap": PROJECTION_CAP}, sort_keys=True).encode()).hexdigest()
            registry = ProtocolRegistry.load()
            self._load_profiles = {number: registry.by_registration[number] for number in (34, 35, 43, 44)}
        except (AttributeError, TypeError, ValueError) as error:
            raise ProtocolError("recovery_catalog_invalid", "Recovery rank data or local rules are unavailable", 503) from error
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "recovery_history_invalid", "Recovery receipt history is invalid",
                                     fields=("request", "content", "revision", "response"), response_limit=RESPONSE_LIMIT)

    def handlers(self):
        return {85: self.recover}

    def restore_state(self, state, now):
        self._ready()
        before = self.bootstrap.player_row(state)
        current = _now_seconds(now)
        if current < _anchor(before):
            raise ProtocolError("recovery_clock_backwards", "Recovery clock precedes the saved energy anchor", 409)
        updated = settle_energy(state, self._ranks, now, recovery_seconds=self.recovery_seconds)
        row = updated["tables"]["game_param"][0]
        amount = self._ranks[row["Rank"] - 1]["CharismaMax"] + row.get("ExtendedMaxCharisma", 0)
        if row["NowCharisma"] + amount > self.rules.charisma_limit:
            raise ProtocolError("recovery_cap_exceeded", "The complete recovery would exceed the configured charisma limit", 409)
        row["NowCharisma"] += amount
        _set_anchor(row, current)
        return updated

    def _response_document(self, profile, state):
        return Container(children=(self.bootstrap._system(profile),
            paid_crystal_response(state, profile.player_id), free_crystal_response(state, profile.player_id),
            game_param_response(state), spending_response(state, style="key_values")))

    def _preflight(self, profile, state):
        candidate = replace(profile, state=state)
        game = self.bootstrap._player_row(candidate)
        encode_response(self._load_profiles[34], dumps(Container(children=self.bootstrap.columns("game_param", [game]))))
        game2 = self.bootstrap._rows(candidate, "game_param2")
        encode_response(self._load_profiles[35], dumps(Container(children=self.bootstrap.columns("game_param2", game2))))
        wallet = self.bootstrap._rows(candidate, "crystals", single=False)
        for number, free in ((43, False), (44, True)):
            selected = [row for row in wallet if row["FreeFlag"] is free]
            encode_response(self._load_profiles[number], dumps(Container(children=self.bootstrap.columns("crystals", selected))))

    def _ready(self):
        try:
            if _rank_rows(self.bootstrap.rank_rows) == self._ranks:
                return
        except (AttributeError, TypeError, ValueError):
            pass
        raise ProtocolError("recovery_catalog_changed", "Rank data changed after recovery service setup", 503)

    def status(self):
        try:
            self._ready()
            ready = True
        except ProtocolError:
            ready = False
        return {"ready": ready, "implemented_operations": [85],
                "native_exclusions": ["Operation 86 stamina recovery: its quest senders need a nonzero ActionPoint cost "
                                      "and its shop sender needs a shop-top button tagged 1001, "
                                      "which current resources do not provide"],
                "content_version": self.content_version,
                "purchase_content_version": self.purchase_content_version,
                "native_client_sha256": self.client_sha256, "charisma_limit": self.rules.charisma_limit,
                "crystal_price": CRYSTAL_PRICE, "natural_recovery_seconds": self.recovery_seconds,
                "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT,
                "request_order": REQUEST_ORDER,
                "response_limit": RESPONSE_LIMIT, "reload_operations": [34, 35, 43, 44],
                "energy_clock": {"encoding": ENCODING, "utc_offset_minutes": UTC_OFFSET_MINUTES},
                "recovery": "Settle natural recovery, then add the current rank maximum plus extended capacity",
                "local_rules": {"crystals": "Free first, oldest acquisition dates first",
                                "monthly_spending": {"policy_version": POLICY_VERSION, "projection_cap": PROJECTION_CAP},
                                "charisma_limit": self.rules.charisma_limit,
                                "limit_source": "Configurable local policy; the operation 85 server limit is not verified",
                                "clock_backwards": "Reject without mutation",
                                "same_body_same_second": "Treat as retry; identical new action requires a new CT"},
                "native_tested": False}

    def recover(self, envelope, store):
        registered(envelope, {85}, ())
        identity = f"85:{envelope.profile.route}:{envelope.profile.st}".encode()
        key = hashlib.sha256(identity + b"\0" + envelope.xml).hexdigest()
        now = None
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            self.bootstrap._player_row(profile)
            history = self._ledger.read(profile.state, profile.revision)
            receipt = self._ledger.receipt(history, key)
            if receipt is not None:
                if receipt["revision"] != profile.revision or receipt["content"] != self.purchase_content_version:
                    raise ProtocolError("recovery_retry_stale", "Recovery state or content changed after this request", 409)
                response = receipt["response"].encode("utf-8")
                try:
                    if loads(response) != self._response_document(profile, profile.state):
                        raise ValueError("Recovery receipt disagrees with the current state")
                    self._preflight(profile, profile.state)
                    encode_response(envelope.profile, response)
                except (ValueError, ProtocolError) as error:
                    raise ProtocolError("recovery_history_invalid", "Saved recovery response is invalid", 503) from error
                return response
            if self._ledger.processed(history, key):
                log.warning("Refused request 85: it repeats a processed request whose receipt was evicted")
                raise ProtocolError("recovery_request_stale", "Recovery request was already processed", 409)
            if now is None:
                now = self.clock()
            updated = self.restore_state(profile.state, now)
            updated = spend_crystals(updated, CRYSTAL_PRICE, now, free_first=True)
            self._preflight(profile, updated)
            response = dumps(self._response_document(profile, updated))
            encode_response(envelope.profile, response)
            if len(response) > RESPONSE_LIMIT:
                raise ProtocolError("recovery_response_capacity", "Recovery response exceeds its receipt limit", 503)
            self._ledger.record(updated, history, {"request": key, "content": self.purchase_content_version,
                                                   "revision": profile.revision + 1, "response": response.decode("utf-8")})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving recovery", 409)
