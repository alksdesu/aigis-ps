from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import re

from .alxml import Container, dumps
from .config_state import configuration_delta, configuration_row, patch_configuration
from .errors import ProtocolError
from .native_numbers import int32
from .possessions import MAX_ITEM_NAME_BYTES, MAX_POSSESSION_ITEMS
from .protocol import ProtocolRegistry, encode_response
from .request_fields import fields, invalid, registered, scalar, values
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger


RESPONSE_LIMIT = 256 * 1024
FILTER_FIELD = "_BattleGodBlessFilter"
HISTORY_NAMESPACE = "blessing_history"
INVALID_SETTINGS = "Blessing settings require distinct names and matching zero-or-one flags"
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _state_digest(state):
    return _digest({key: value for key, value in state.items() if key != HISTORY_NAMESPACE})


class BlessingService:
    def __init__(self, bootstrap, players, possessions, catalog):
        self.bootstrap, self.players = bootstrap, players
        self.possessions, self.catalog = possessions, catalog
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (36, 129, 137, 148)}
        self.content_version = self._content()
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "blessing_history_invalid",
                                     "Stored blessing-setting receipts are invalid",
                                     fields=("request", "content", "state", "revision", "response"),
                                     response_limit=RESPONSE_LIMIT)
        self._ready()

    def _content(self):
        try:
            items, filters = self.catalog.item_ids, self.catalog.filter_values
            versions = {"catalog": self.catalog.content_version, "possessions": self.possessions.asset_sha256}
            if (any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in versions.values())
                    or not isinstance(items, frozenset) or not 0 < len(items) <= MAX_POSSESSION_ITEMS
                    or any(not isinstance(name, str) or not name or len(name.encode("utf-8")) > MAX_ITEM_NAME_BYTES
                           for name in items)
                    or not isinstance(filters, frozenset) or not filters
                    or any(not int32(value) or value < 0 for value in filters)):
                raise ValueError("Blessing settings require fixed verified definitions")
            return _digest({"policy": 1, "dependencies": versions, "items": sorted(items),
                            "filters": sorted(filters), "ownership": "positive_count_even_when_disabling",
                            "noop": "no_write", "empty_vectors": "paired_only"})
        except (AttributeError, TypeError, ValueError, UnicodeError) as error:
            raise ProtocolError("blessing_dependencies_invalid", "Blessing-setting dependencies are invalid", 503) from error

    def _ready(self):
        if (not self.catalog.is_ready() or not self.possessions.is_ready()
                or self._content() != self.content_version):
            raise ProtocolError("blessing_dependencies_changed", "Blessing-setting dependencies changed after setup", 503)

    def handlers(self):
        return {148: self.save}

    def status(self):
        try:
            self._ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        return {"ready": ready, "error": error, "implemented_operations": [148],
                "content_version": self.content_version, "catalog": self.catalog.status(),
                "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT,
                "request_order": REQUEST_ORDER,
                "noop_policy": "Return authoritative settings without writing a receipt or revision",
                "retry_policy": "Exact request at its committed revision, state and content",
                "empty_vectors": "Both ItemID and Enable may be empty together", "native_tested": False}

    def _request(self, envelope):
        if envelope.profile.registration_keys != (148,):
            invalid("Expected the native blessing-settings operation")
        root = registered(envelope, {148}, {"BGB"}, {"BGB"})
        supplied = fields(root["BGB"], {"FI", "ItemID", "Enable"})
        if ("ItemID" in supplied) != ("Enable" in supplied):
            invalid("Blessing item names and enabled flags must appear together")
        selected_filter = scalar(supplied["FI"], "I") if "FI" in supplied else None
        self._check_filter(selected_filter)
        settings = {}
        if "ItemID" in supplied:
            names = values(supplied["ItemID"], "S", maximum=len(self.catalog.item_ids), max_bytes=MAX_ITEM_NAME_BYTES)
            flags = values(supplied["Enable"], "I", maximum=len(self.catalog.item_ids))
            if len(names) != len(flags) or len(set(names)) != len(names):
                invalid(INVALID_SETTINGS)
            settings = dict(zip(names, flags))
        self._check_settings(settings)
        return selected_filter, settings

    def _check_filter(self, selected_filter):
        if selected_filter is not None and (not int32(selected_filter)
                                            or selected_filter not in self.catalog.filter_values):
            invalid("Blessing filter is outside the verified choices")

    def _check_settings(self, settings):
        if (not isinstance(settings, dict) or len(settings) > len(self.catalog.item_ids)
                or any(type(name) is not str or not name for name in settings)
                or any(type(flag) is not int or flag not in (0, 1) for flag in settings.values())):
            invalid(INVALID_SETTINGS)

    def _validate(self, state, settings):
        row = configuration_row(state)
        current = row.get(FILTER_FIELD)
        if not int32(current) or current not in self.catalog.filter_values:
            raise ProtocolError("blessing_filter_state_invalid", "Saved blessing filter is invalid", 503)
        self.catalog.validate(state, settings)

    def _candidate(self, state, selected_filter, settings):
        updated = self.possessions.set_enabled(state, settings)
        if selected_filter is not None:
            updated = patch_configuration(updated, {FILTER_FIELD: selected_filter})
        return updated

    def _settled(self, state, selected_filter, settings):
        updated = self._candidate(state, selected_filter, settings)
        self._validate(updated, settings)
        return updated

    def set_settings(self, state, selected_filter, settings):
        self._ready()
        self._check_filter(selected_filter)
        self._check_settings(settings)
        self.players.validate(state)
        self._validate(state, settings)
        return self._settled(state, selected_filter, settings)

    def _response(self, profile, state, settings):
        payload = dumps(Container(children=(self.bootstrap._system(profile),
            configuration_delta(configuration_row(state), (FILTER_FIELD,)),
            self.possessions.response(state, tuple(settings), include_enabled=True))))
        if len(payload) > RESPONSE_LIMIT:
            raise ProtocolError("blessing_response_capacity", "Blessing response exceeds its receipt limit", 503)
        encode_response(self._profiles[148], payload)
        return payload

    def _preflight(self, profile, state):
        candidate = replace(profile, state=state)
        config = dumps(Container(children=self.bootstrap.columns("config", self.bootstrap._rows(candidate, "config"))))
        encode_response(self._profiles[36], config)
        inventory = dumps(Container(children=(self.bootstrap._system(candidate), self.bootstrap._possessions(candidate))))
        encode_response(self._profiles[129], inventory)
        encode_response(self._profiles[137], self.bootstrap._configuration_response(candidate))

    def save(self, envelope, store):
        self._ready()
        selected_filter, settings = self._request(envelope)
        key = hashlib.sha256(f"148:{envelope.profile.route}:{envelope.profile.st}:".encode() + envelope.xml).hexdigest()
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            self.players.validate(profile.state)
            self._validate(profile.state, settings)
            history = self._ledger.read(profile.state, profile.revision)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                if cached["revision"] != profile.revision or cached["content"] != self.content_version:
                    raise ProtocolError("blessing_retry_stale", "Blessing state or content changed after this request", 409)
                if (cached["state"] != _state_digest(profile.state)
                        or self._candidate(profile.state, selected_filter, settings) != profile.state):
                    self._ledger.invalid()
                self._preflight(profile, profile.state)
                response = self._response(profile, profile.state, settings)
                if response != cached["response"].encode("utf-8"):
                    self._ledger.invalid()
                return response
            if self._ledger.processed(history, key):
                raise ProtocolError("blessing_request_stale", "Blessing request was already processed", 409)
            updated = self._settled(profile.state, selected_filter, settings)
            self._preflight(profile, updated)
            response = self._response(profile, updated, settings)
            if updated == profile.state:
                return response
            self._ledger.record(updated, history, {"request": key, "content": self.content_version,
                                                   "state": _state_digest(updated), "revision": profile.revision + 1,
                                                   "response": response.decode("utf-8")})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving blessing settings", 409)
