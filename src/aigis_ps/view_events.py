from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import re
from types import SimpleNamespace

from .alxml import Column, Container, dumps
from .config_state import configuration_row, patch_configuration
from .errors import ProtocolError
from .native_numbers import int32
from .protocol import ProtocolRegistry, encode_response
from .request_fields import invalid, registered, scalar
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger


MAX_REQUEST_BYTES = 255
RESPONSE_LIMIT = 4096
HISTORY_NAMESPACE = "view_event_history"
EVENT_VIEW_FIELD = "EventViewRequest"
NO_REQUEST = ""
REQUEST_KEYS = ("CardId", "Id", "Mode", "Male")
EPISODES, SWITCHES = (1, 2, 3), (0, 1)
_INTEGER = re.compile(r"[0-9]{1,10}\Z")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _state_digest(state):
    return _digest({key: value for key, value in state.items() if key != HISTORY_NAMESPACE})


def _text(value):
    if type(value) is not str or len(value.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("Event view requests require a bounded string")
    dumps(Container(children=(Column("F8", "S", (value,)),)))


def parse_view_request(value):
    try:
        _text(value)
        fields = {}
        for component in value.split(","):
            key, separator, number = component.partition("=")
            if (not separator or key not in REQUEST_KEYS
                    or key in fields or not _INTEGER.fullmatch(number)):
                raise ValueError("Unexpected event view request field")
            fields[key] = int(number)
        if (set(fields) != set(REQUEST_KEYS)
                or any(not int32(number) for number in fields.values())
                or fields["CardId"] <= 0 or fields["Id"] not in EPISODES
                or fields["Mode"] not in SWITCHES or fields["Male"] not in SWITCHES):
            raise ValueError("Unsupported event view request values")
        return fields
    except (TypeError, ValueError, UnicodeError) as error:
        raise ProtocolError("view_event_request_invalid", "Event view request does not match its native format") from error


def _receipt_action_valid(entry):
    _text(entry["value"])
    return (type(entry["operation"]) is int and entry["operation"] in (23, 24)
            and (entry["operation"] == 23 or entry["value"] == NO_REQUEST))


def _with_request(state, value):
    return patch_configuration(state, {EVENT_VIEW_FIELD: value})


class ViewEventService:
    def __init__(self, bootstrap, players):
        self.bootstrap, self.players = bootstrap, players
        self._configuration_profile = ProtocolRegistry.load().by_registration[36]
        self.content_version = self._content()
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "view_event_history_invalid", "Stored event view receipts are invalid",
                                     fields=("request", "content", "state", "revision", "response", "operation", "value"),
                                     response_limit=RESPONSE_LIMIT, check=_receipt_action_valid)

    def _content(self):
        try:
            schema = self.bootstrap.schemas["config"]
            field = next(field for field in schema.fields if field.name == EVENT_VIEW_FIELD)
            if field.type_code != 33 or field.size != MAX_REQUEST_BYTES + 1:
                raise ValueError("Event view configuration differs from the native field")
            return _digest({"policy": 1, "schema": asdict(schema), "field": EVENT_VIEW_FIELD,
                            "operations": [23, 24], "response": "SYSTEM", "same_value": "record_action",
                            "format": ",".join(REQUEST_KEYS), "episodes": list(EPISODES), "modes": list(SWITCHES),
                            "eligibility": "Persist intent without granting eligibility"})
        except (KeyError, StopIteration, TypeError, ValueError, AttributeError) as error:
            raise ProtocolError("view_event_configuration_unavailable", "Verified event view configuration is unavailable", 503) from error

    def _ready(self):
        if self._content() != self.content_version:
            raise ProtocolError("view_event_configuration_changed", "Event view configuration changed after setup", 503)

    def handlers(self):
        return {23: self.save, 24: self.save}

    def status(self):
        try:
            self._ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        return {"ready": ready, "error": error, "implemented_operations": [23, 24],
                "content_version": self.content_version, "reload_operation": 36,
                "max_request_bytes": MAX_REQUEST_BYTES, "history_limit": HISTORY_LIMIT,
                "processed_request_limit": PROCESSED_LIMIT, "native_tested": False,
                "scope": "Save and clear event playback handoff; no rewards or read flags",
                "request_order": REQUEST_ORDER,
                "ordering_limit": "Native clear requests contain no target nonce"}

    def _config(self, profile):
        self.bootstrap._rows(profile, "config")
        return configuration_row(profile.state)

    def _stored_config(self, state):
        return self._config(SimpleNamespace(state=state))

    def initialize(self, state):
        self._ready()
        self._stored_config(state)
        return deepcopy(state)

    def set_request(self, state, value):
        self._ready()
        parse_view_request(value)
        self._stored_config(state)
        return _with_request(state, value)

    def clear_request(self, state):
        self._ready()
        self._stored_config(state)
        return _with_request(state, NO_REQUEST)

    def _preflight(self, profile, state):
        candidate = replace(profile, state=state)
        self._config(candidate)
        data = dumps(Container(children=self.bootstrap.columns("config", self.bootstrap._rows(candidate, "config"))))
        encode_response(self._configuration_profile, data)

    def save(self, envelope, store):
        self._ready()
        request = registered(envelope, {23, 24}, {"F8"}, {"F8"})
        operations = envelope.profile.registration_keys
        if operations not in ((23,), (24,)):
            invalid("Event view requests require an unambiguous operation")
        number = operations[0]
        value = scalar(request["F8"], "S", max_bytes=MAX_REQUEST_BYTES)
        if number == 24:
            if value:
                invalid("Event view clear requests require an empty F8")
        else:
            parse_view_request(value)
        key = hashlib.sha256(f"{number}:{envelope.profile.route}:{envelope.profile.st}:".encode() + envelope.xml).hexdigest()
        expected_value = None
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            row = self._config(profile)
            history = self._ledger.read(profile.state, profile.revision)
            response = dumps(Container(children=(self.bootstrap._system(profile),)))
            encode_response(envelope.profile, response)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                if cached["revision"] != profile.revision or cached["content"] != self.content_version:
                    raise ProtocolError("view_event_retry_stale", "Event view state changed after this request", 409)
                if (cached["state"] != _state_digest(profile.state)
                        or cached["operation"] != number or cached["value"] != value
                        or row[EVENT_VIEW_FIELD] != value or cached["response"].encode("utf-8") != response):
                    self._ledger.invalid()
                self._preflight(profile, profile.state)
                return response
            if self._ledger.processed(history, key):
                raise ProtocolError("view_event_request_stale", "Event view request was already processed", 409)
            if expected_value is None:
                expected_value = row[EVENT_VIEW_FIELD]
            elif row[EVENT_VIEW_FIELD] != expected_value:
                raise ProtocolError("view_event_changed", "Another operation changed the pending event view", 409)
            updated = _with_request(profile.state, value)
            self._preflight(profile, updated)
            self._ledger.record(updated, history, {"request": key, "content": self.content_version,
                                                   "state": _state_digest(updated), "revision": profile.revision + 1,
                                                   "response": response.decode("utf-8"), "operation": number,
                                                   "value": value})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Configuration changed while saving the event view request", 409)
