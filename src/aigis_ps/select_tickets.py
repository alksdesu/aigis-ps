from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import logging
import re

from .alxml import Column, Container, dumps
from .errors import ProtocolError
from .native_numbers import int32
from .player_state import UNIT_BOOLEANS, UNIT_FIELDS
from .presents import CLAIM_LIMITS, MAX_PRESENTS, PRESENT_LIMITS
from .protocol import ProtocolRegistry, encode_response
from .request_fields import fields, invalid, registered, scalar, values
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .unit_grants import UnitGrants


RESPONSE_LIMIT = 64 * 1024
HISTORY_NAMESPACE = "select_ticket_history"
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_UNIT_NAMES = frozenset(name for name, _ in UNIT_FIELDS)
_RECEIPT_FIELDS = ("request", "profile", "content", "state", "revision", "response",
                   "mail_id", "config_id", "select_id", "card_id", "unit")
log = logging.getLogger("aigis_ps")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _state_digest(state):
    return _digest({key: value for key, value in state.items() if key != HISTORY_NAMESPACE})


def _bad_history():
    raise ProtocolError("select_ticket_history_invalid", "Stored selection ticket receipts are invalid", 503)


def _dependencies_changed():
    return ProtocolError("select_ticket_dependencies_changed", "Selection ticket definitions changed after setup", 503)


def _receipt_valid(entry):
    unit = entry["unit"]
    return (type(entry["profile"]) is str and _HASH.fullmatch(entry["profile"]) is not None
            and all(int32(entry[name]) and entry[name] > 0 for name in ("mail_id", "config_id", "select_id", "card_id"))
            and isinstance(unit, dict) and unit.keys() == _UNIT_NAMES
            and all(type(value) is bool if name in UNIT_BOOLEANS else int32(value) for name, value in unit.items()))


class SelectTicketService:
    def __init__(self, bootstrap, players, presents, catalog):
        self.bootstrap, self.players, self.presents, self.catalog = bootstrap, players, presents, catalog
        self.grants = UnitGrants(players, error_prefix="select_ticket")
        self._initial_values = {}
        self.content_version = self._content()
        self._ready()
        self._prepare_candidates(candidate for config in self.catalog.configs.values() for candidate in config.candidates)
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (1, 41, 42)}
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "select_ticket_history_invalid",
                                     "Stored selection ticket receipts are invalid", fields=_RECEIPT_FIELDS,
                                     response_limit=RESPONSE_LIMIT, check=_receipt_valid)

    def _content(self):
        versions = {"catalog": self.catalog.content_version, "initialization": self.grants.initialization_version}
        if any(type(value) is not str or not _HASH.fullmatch(value) for value in versions.values()):
            raise ProtocolError("select_ticket_dependencies_invalid", "Selection tickets require fixed fingerprints", 503)
        return _digest({"policy": 1, "dependencies": versions, "grant": "one_selected_unit_per_mail",
                        "selector": "one_based_group_order_and_card_id", "response": "SYSTEM_PBP_PPU",
                        "duplicates": "new_instance_without_rainbow", "content": 1})

    def _ready(self):
        self.grants.ready()
        try:
            self.catalog.ready()
        except ProtocolError as error:
            raise _dependencies_changed() from error
        if self.grants.players is not self.players or self._content() != self.content_version:
            raise _dependencies_changed()

    def _prepare_candidates(self, candidates):
        missing = dict.fromkeys((candidate.card_id, candidate.level, candidate.evolution_count)
                                for candidate in candidates
                                if (candidate.card_id, candidate.level, candidate.evolution_count) not in self._initial_values)
        if missing:
            resolved = self.grants.initial_batch([{"card_id": card, "level": level, "evolution_count": stage}
                                                   for card, level, stage in missing])
            self._initial_values.update(zip(missing, resolved))

    def validate_config(self, config_id):
        self._ready()
        config = self.catalog.require_supported(config_id)
        self._prepare_candidates(config.candidates)
        return config

    def _selection(self, config_id, select_id, card_id):
        config = self.validate_config(config_id)
        if not int32(select_id) or not 1 <= select_id <= len(config.candidates):
            raise ProtocolError("select_ticket_choice_invalid", "Selection index is outside this ticket's candidates", 409)
        candidate = config.candidates[select_id - 1]
        if not int32(card_id) or candidate.card_id != card_id:
            raise ProtocolError("select_ticket_choice_invalid", "Selection index and card identity do not match", 409)
        return candidate

    def status(self):
        try:
            self._ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        return {"ready": ready, "error": error, "content_version": self.content_version,
                "catalog": self.catalog.status(), "initialization_variants": len(self._initial_values),
                "claim_policy": "One selected candidate from one ticket per request",
                "selection_index": "One-based original group order", "history_limit": HISTORY_LIMIT,
                "processed_request_limit": PROCESSED_LIMIT, "request_order": REQUEST_ORDER, "native_tested": False}

    @staticmethod
    def _key(envelope):
        identity = _digest(asdict(envelope.profile))
        return identity, hashlib.sha256(identity.encode() + b":" + envelope.xml).hexdigest()

    def matches_request(self, profile, envelope, selected):
        _, key = self._key(envelope)
        if self._ledger.processed(self._ledger.read(profile.state, profile.revision), key):
            return True
        return any(row["Type"] == 20 and row["PresentID"] in selected for row in self.presents.rows(profile.state))

    @staticmethod
    def _request(envelope, selected):
        if envelope.profile.registration_keys != (1, 2):
            raise ProtocolError("select_ticket_batch_unsupported", "Selection tickets require individual claims", 501)
        root = registered(envelope, {1, 2}, {"RT"}, {"RT"})
        rt = fields(root["RT"], {"PT2"}, {"PT2"})
        table = fields(rt["PT2"], {"PresentID", "SelectID", "SelectCardID"}, {"PresentID", "SelectID", "SelectCardID"})
        requested = values(table["PresentID"], "I", minimum=1, maximum=MAX_PRESENTS)
        if (not isinstance(selected, (tuple, list)) or tuple(selected) != requested
                or any(identifier <= 0 for identifier in requested) or len(set(requested)) != len(requested)):
            invalid("Selection ticket identities do not match the request")
        if len(requested) != 1:
            raise ProtocolError("select_ticket_single_required", "Claim one selection ticket at a time", 501)
        select_id, card_id = scalar(table["SelectID"], "I"), scalar(table["SelectCardID"], "I")
        if select_id <= 0 or card_id <= 0:
            invalid("Selection index and card identity must be positive")
        return requested[0], select_id, card_id

    def _preflight(self, profile, state):
        candidate = replace(profile, state=state)
        player = self.players.validate(state)
        encode_response(self._profiles[41], self.players.unit_response(player["units"]))
        encode_response(self._profiles[42], dumps(Container(children=(
            self.bootstrap._system(candidate), self.presents.response(state))), limits=PRESENT_LIMITS))

    def _response(self, profile, state, unit):
        payload = dumps(Container(children=(self.bootstrap._system(profile), Container("PBP", (
            Column("A1", "S", ("UX",)), Column("A2", "I", (state["player_state"]["battle"]["UX"],)))),
            Container("PPU", tuple(Column(alias, "B" if name in UNIT_BOOLEANS else "I", (unit[name],))
                                   for name, alias in UNIT_FIELDS)))), limits=CLAIM_LIMITS)
        if len(payload) > RESPONSE_LIMIT:
            raise ProtocolError("select_ticket_response_capacity", "Selection response exceeds its receipt limit", 503)
        encode_response(self._profiles[1], payload)
        return payload

    def _expected_unit(self, candidate, unit):
        key = candidate.card_id, candidate.level, candidate.evolution_count
        return self.grants.expected_unit(candidate.card_id, self._initial_values[key], unit["GetIndex"], unit["UniqueID"])

    def _replay(self, entry, profile, envelope, mail_id, select_id, card_id, profile_key):
        if entry["revision"] != profile.revision or entry["content"] != self.content_version:
            raise ProtocolError("select_ticket_retry_stale", "Selection state or resources changed after this claim", 409)
        if (entry["state"] != _state_digest(profile.state) or entry["profile"] != profile_key
                or entry["mail_id"] != mail_id or entry["select_id"] != select_id or entry["card_id"] != card_id
                or any(row["PresentID"] == mail_id for row in self.presents.rows(profile.state))):
            _bad_history()
        candidate = self._selection(entry["config_id"], select_id, card_id)
        unit, player = entry["unit"], self.players.validate(profile.state)
        if (unit != player["units"][-1] or unit["GetIndex"] != player["battle"]["UX"]
                or unit["UniqueID"] != player.get("last_unique_id") or unit != self._expected_unit(candidate, unit)):
            _bad_history()
        self._preflight(profile, profile.state)
        payload = self._response(profile, profile.state, unit)
        if payload != entry["response"].encode("utf-8"):
            _bad_history()
        encode_response(envelope.profile, payload)
        return payload

    def receive(self, envelope, store, selected):
        mail_id, select_id, card_id = self._request(envelope, selected)
        profile_key, key = self._key(envelope)
        frozen_mail = None
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            self.players.validate(profile.state)
            rows = self.presents.rows(profile.state)
            history = self._ledger.read(profile.state, profile.revision)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                return self._replay(cached, profile, envelope, mail_id, select_id, card_id, profile_key)
            if self._ledger.processed(history, key):
                log.warning("Refused request 1/2: it repeats a processed selection claim whose receipt was evicted")
                raise ProtocolError("select_ticket_request_stale", "Selection request was already processed", 409)
            mail = next((row for row in rows if row["PresentID"] == mail_id), None)
            if mail is None:
                raise ProtocolError("present_not_found", "Selection ticket is unavailable or already claimed", 409)
            if mail["Type"] != 20 or mail["Content"] != 1:
                raise ProtocolError("select_ticket_mail_unsupported", "One selection ticket per mail is required", 501)
            candidate = self._selection(mail["SelectionConfigID"], select_id, card_id)
            if frozen_mail is None:
                frozen_mail = deepcopy(mail)
            elif frozen_mail != mail:
                raise ProtocolError("select_ticket_changed", "Selection ticket changed during the claim", 409)
            updated, added = self.grants.grant(profile.state, [{"card_id": candidate.card_id, "level": candidate.level,
                "evolution_count": candidate.evolution_count, "count": 1}])
            updated["tables"]["presents"] = [row for row in rows if row["PresentID"] != mail_id]
            self._preflight(profile, updated)
            payload = self._response(profile, updated, added[0])
            encode_response(envelope.profile, payload)
            self._ledger.record(updated, history, {"request": key, "profile": profile_key, "content": self.content_version,
                "state": _state_digest(updated), "revision": profile.revision + 1, "response": payload.decode("utf-8"),
                "mail_id": mail_id, "config_id": mail["SelectionConfigID"],
                "select_id": select_id, "card_id": card_id, "unit": deepcopy(added[0])})
            try:
                store.update_state(updated, profile.revision)
                return payload
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while claiming a selection ticket", 409)
